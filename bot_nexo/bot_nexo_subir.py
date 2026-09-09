"""
Bot NEXO — Subida de archivo de presuspensión masiva
=====================================================
Flujo:
  1. Cargar las cookies de sesión de NEXO (nexo_cookies.json, en la raíz del repo,
     generadas por la herramienta de escritorio RenovarSesionGestionSLA.pyw — el
     login de NEXO pasa por Microsoft con MFA, así que un bot headless no puede
     loguearse solo; reutilizamos una sesión ya autenticada por un humano)
  2. Entrar a NEXO ya autenticado por cookies (sin pasar por la pantalla de login)
  3. Entrar a GLP (se abre en pestaña nueva)
  4. Click en el ícono lateral "Levanta presuspensión masiva"
  5. Desplegar el panel "Levantar Presuspensión"
  6. Subir el CSV (ya commiteado en nexo_uploads/<NOMBRE_ARCHIVO> por la app)
  7. Completar el email de resultados
  8. Click en "Procesar" y esperar el mensaje de éxito
  9. Actualizar el estado en Supabase (pendiente confirmado / error)

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY   -> service role (para poder hacer UPDATE sin RLS de usuario)
  CAJA_ID, NUMERO_CAJA, NOMBRE_ARCHIVO

Si las cookies vencieron o no existen, el bot marca la caja como "error" con un
mensaje pidiendo correr de nuevo la herramienta de renovación — no intenta loguear
usuario/contraseña solo, porque NEXO pide MFA y eso no se puede automatizar.
"""
import os
import sys
import json
import base64
import random
import string
import requests
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Windows suele usar cp1252 en la consola, que no soporta emojis (🔎, ❌, etc.)
# Forzamos UTF-8 en stdout/stderr para que los prints con emoji no rompan el bot.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CAJA_ID = os.environ["CAJA_ID"]
NUMERO_CAJA = os.environ["NUMERO_CAJA"]
NOMBRE_ARCHIVO = os.environ["NOMBRE_ARCHIVO"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # "owner/repo", lo provee GitHub Actions solo

URL_NEXO_HOME = "https://nexostealth-claroaup.msappproxy.net/"
URL_WEBCOM = "https://claroaup.sharepoint.com/sites/webcom/SitePages/Inicio.aspx"
URL_WEBCOM_PORTAL_AGENTES = "https://claroaup.sharepoint.com/sites/webcom/SitePages/Portal-Agentes.aspx"

XPATH_BTN_GLP = '//*[@id="app"]/div[2]/div[2]/ul/a[2]'
XPATH_BTN_PRESUSPENSION_LATERAL = '//*[@id="root"]/div/div[2]/nav/a[2]/img'
URL_GLP_MASSIVE_PRESUSP = "https://glp-claroaup.msappproxy.net/massive-presusp"
XPATH_PANEL_LEVANTAR = '//*[@id="panel1bh-header"]/div[1]'
XPATH_BTN_SELECCIONAR_ARCHIVO = '//*[@id="upload-form"]/div[1]/div[1]/div[1]/label/button'
XPATH_INPUT_EMAIL = '//*[@id="email"]'
XPATH_BTN_PROCESAR = '//*[@id="upload-form"]/div[2]/button[1]'

CARPETA_CAPTURAS = Path(__file__).resolve().parent / "capturas"
CARPETA_CAPTURAS.mkdir(exist_ok=True)


def headers_supabase():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def obtener_config():
    """Trae usuario/contraseña de SAP (reutilizadas para el login intermedio de NEXO)
    y el email de resultados desde Configuración → Distribución."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=*",
        headers=headers_supabase(),
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError("No se encontró la config global en Supabase")
    cfg = rows[0]
    usuario = cfg.get("sap_user")
    password = cfg.get("sap_pass")
    raw = cfg.get("distribucion_config") or "{}"
    dist_cfg = json.loads(raw) if isinstance(raw, str) else raw
    email_resultado = dist_cfg.get("email_resultado")
    if not usuario or not password:
        raise RuntimeError("Faltan credenciales de SAP en Supabase (se reutilizan para el login de NEXO)")
    if not email_resultado:
        raise RuntimeError("Falta el email de resultados en Configuración → Distribución")
    return usuario, password, email_resultado


def cargar_cookies_nexo():
    """Lee nexo_cookies.json (raíz del repo, generado por RenovarSesionGestionSLA.pyw)."""
    ruta = Path(__file__).resolve().parent.parent / "nexo_cookies.json"
    if not ruta.exists():
        marcar_error(
            "No existe nexo_cookies.json en el repo. Corré la herramienta "
            "RenovarSesionGestionSLA.pyw (opción NEXO) para generar una sesión nueva."
        )
    try:
        cookies = json.loads(ruta.read_text(encoding="utf-8"))
    except Exception as e:
        marcar_error(f"nexo_cookies.json existe pero no se pudo leer: {e}")
    if not cookies:
        marcar_error("nexo_cookies.json está vacío. Corré la herramienta de renovación de sesión.")
    return cookies


def generar_nombre_nuevo():
    """Mismo criterio que usa GestionSLA al generar el nombre original: timestamp
    + sufijo random, para que NUNCA pueda coincidir con uno ya usado antes."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    random4 = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"Presuspension_{NUMERO_CAJA}_{ts}{random4}.csv"


def subir_csv_renombrado_al_repo(contenido_bytes, nombre_nuevo):
    """Sube el MISMO contenido del CSV, pero con un nombre nuevo, al repo — para
    que NEXO ya no lo rechace como 'archivo ya procesado'."""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return False, "Sin GITHUB_TOKEN/GITHUB_REPOSITORY — no se pudo subir el archivo renombrado."
    try:
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/nexo_uploads/{nombre_nuevo}"
        body = {
            "message": f"Reintento automático (archivo renombrado) — caja {NUMERO_CAJA}",
            "content": base64.b64encode(contenido_bytes).decode(),
        }
        r = requests.put(url, headers=headers, json=body, timeout=30)
        if r.status_code in (200, 201):
            return True, None
        return False, f"GitHub respondió {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def actualizar_nombre_archivo_en_supabase(nombre_nuevo):
    """Tiene que quedar registrado en la caja: es lo que bot_nexo_resultado.py
    usa para reconocer el mail de respuesta de NEXO cuando llegue."""
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={"nombre_archivo": nombre_nuevo},
        timeout=30,
    )


def marcar_error(mensaje):
    print(f"❌ ERROR: {mensaje}", file=sys.stderr)
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={"estado": "error", "error_mensaje": mensaje[:500]},
        timeout=30,
    )
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_sims?caja_id=eq.{CAJA_ID}&select=id",
        headers=headers_supabase(),
        timeout=30,
    )
    if r.ok:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/distribucion_sims?caja_id=eq.{CAJA_ID}",
            headers=headers_supabase(),
            json={"estado": "error"},
            timeout=30,
        )
    sys.exit(1)


def _diag(page, etiqueta):
    """Deja rastro en el LOG (visible directo en Actions, sin bajar nada) + una captura."""
    try:
        print(f"🔎 [{etiqueta}] URL actual: {page.url}")
        print(f"🔎 [{etiqueta}] Título: {page.title()}")
        page.screenshot(path=str(CARPETA_CAPTURAS / f"{etiqueta}.png"), full_page=True)
    except Exception as e:
        print(f"🔎 [{etiqueta}] No se pudo diagnosticar: {e}")


def main():
    usuario, password, email_resultado = obtener_config()
    cookies = cargar_cookies_nexo()
    ruta_csv = Path(__file__).resolve().parent.parent / "nexo_uploads" / NOMBRE_ARCHIVO
    if not ruta_csv.exists():
        marcar_error(f"No se encontró el archivo {ruta_csv} en el repo (¿se commiteó bien desde la app?)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        # Cargar la sesión ya autenticada por un humano (NEXO pide MFA — un bot
        # headless no puede resolverlo solo, por eso reutilizamos cookies).
        context.add_cookies(cookies)
        # Verificación real de que las cookies quedaron cargadas en el navegador
        # (no solo que la llamada no tiró error) — para descartar que "no se lean".
        cookies_en_contexto = context.cookies()
        nombres_clave = {'ESTSAUTH', 'ESTSAUTHPERSISTENT', 'buid', 'SignInStateCookie'}
        presentes = [c['name'] for c in cookies_en_contexto if c['name'] in nombres_clave]
        print(f"🔎 Cookies cargadas en el contexto: {len(cookies_en_contexto)} de {len(cookies)} originales")
        print(f"🔎 Cookies clave de sesión Azure AD presentes: {presentes}")
        page = context.new_page()

        try:
            # 1) Entrar directo a NEXO. Ya no necesitamos el rodeo por Webcom: ese
            #    desafío de red (Conditional Access "Compliant Network") solo bloqueaba
            #    cuando el bot corría en la nube de GitHub — desde el runner propio,
            #    dentro de la red de Claro, se elimina ese bloqueo.
            #    Usamos "domcontentloaded" en vez de "networkidle": sitios como
            #    SharePoint/Azure nunca dejan de hacer pedidos de fondo (telemetría,
            #    polling, etc.), así que "networkidle" tiende a colgarse en timeout
            #    aunque la página ya esté completamente lista para usarse.
            page.goto(URL_NEXO_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            _diag(page, "00a_nexo_inicial")

            # 2) Completar login las veces que haga falta (puede ser: pantalla Keycloak
            #    de "Claro" con un solo campo de usuario -> pantalla de Microsoft con
            #    usuario y contraseña por separado -> ambas en cualquier combinación).
            #    Detectamos por PRESENCIA DE CAMPOS en el DOM, no por texto en la URL
            #    (la URL de Keycloak no contiene la palabra "login" en ningún lado).
            SELECTOR_USUARIO = '#username, input[name="username"], input[type="email"]'
            SELECTOR_PASSWORD = '#password, input[name="password"], input[type="password"]'
            SELECTOR_SUBMIT = 'button[type="submit"], input[type="submit"], #kc-login'

            for intento in range(4):  # como máximo 4 pantallas encadenadas (Keycloak + MS)
                campo_usuario = page.locator(SELECTOR_USUARIO).first
                campo_password = page.locator(SELECTOR_PASSWORD).first
                hay_usuario = campo_usuario.count() > 0 and campo_usuario.is_visible()
                hay_password = campo_password.count() > 0 and campo_password.is_visible()

                if not hay_usuario and not hay_password:
                    break  # ya no hay más pantallas de login a la vista

                if hay_usuario:
                    campo_usuario.fill(usuario)
                    _diag(page, f"00b_usuario_completado_intento{intento}")
                    # Si ADEMÁS hay contraseña en esta misma pantalla, la completamos ya
                    if hay_password:
                        campo_password.fill(password)
                        _diag(page, f"00c_password_completado_intento{intento}")
                elif hay_password:
                    campo_password.fill(password)
                    _diag(page, f"00c_password_completado_intento{intento}")

                # Enviar — puede abrir una pestaña nueva ya autenticada, o navegar en la misma
                try:
                    with context.expect_page(timeout=8000) as pagina_nueva_info:
                        page.locator(SELECTOR_SUBMIT).first.click()
                    page = pagina_nueva_info.value
                except PWTimeout:
                    pass
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                _diag(page, f"00d_tras_submit_intento{intento}")

                # Posible prompt "¿Seguir conectado?" (KMSI) de Microsoft — opcional
                if page.locator('#idBtn_Back').count() > 0:
                    _diag(page, f"00e_prompt_seguir_conectado_intento{intento}")
                    page.locator('#idBtn_Back').click()
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)

            _diag(page, "00f_nexo_listo")

            # 2b) Si a pesar de todo seguimos viendo un campo de login, hay un problema real
            if page.locator(SELECTOR_USUARIO).first.count() > 0 and page.locator(SELECTOR_USUARIO).first.is_visible():
                _diag(page, "00g_no_autentico")
                marcar_error(
                    "Entramos a NEXO pero seguimos en una pantalla de login tras completar "
                    "usuario/contraseña. Puede ser un tema de cookies vencidas, o que haga "
                    "falta un consentimiento/MFA la primera vez desde esta máquina."
                )

            # 3) Click en "GLP" — se abre pestaña nueva
            existe_glp = page.locator(f'xpath={XPATH_BTN_GLP}').count() > 0
            print(f"🔎 ¿Existe el link GLP en el DOM? {existe_glp}")
            if not existe_glp:
                _diag(page, "03b_glp_no_encontrado")
                marcar_error("Llegamos a NEXO pero el link 'GLP' no está en la página (revisar captura 00f_nexo_listo.png / 03b_glp_no_encontrado.png — puede que el menú tenga otra estructura o el usuario no tenga permiso de ver esa opción)")
            with context.expect_page() as nueva_pagina_info:
                page.locator(f'xpath={XPATH_BTN_GLP}').click(timeout=45000)
            glp = nueva_pagina_info.value
            glp.wait_for_load_state("domcontentloaded", timeout=60000)
            glp.wait_for_timeout(2000)
            _diag(glp, "04_glp_abierto")

            # 4) Click en el ícono lateral "Levanta presuspensión masiva" (sin texto,
            #    tiene tooltip). La URL directa a /massive-presusp no sirve: es una SPA
            #    con ruteo interno que necesita el click real para cargar los datos.
            try:
                glp.get_by_title("levanta presuspension masiva", exact=False).click(timeout=15000)
            except PWTimeout:
                glp.locator(f'xpath={XPATH_BTN_PRESUSPENSION_LATERAL}').click(timeout=15000)
            glp.wait_for_timeout(2000)
            _diag(glp, "04b_icono_lateral_clickeado")

            # 5) Desplegar el panel "Levantar Presuspensión"
            glp.get_by_role("button", name="Levantar Presuspensión").click()
            glp.wait_for_selector(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}', timeout=15000)
            _diag(glp, "05_panel_desplegado")

            # ── 6 a 9: subir archivo, completar email, procesar, y verificar
            # resultado. Es una función aparte porque si NEXO dice "el archivo
            # ya fue procesado", el bot reintenta ESTOS pasos solo, con un
            # nombre de archivo nuevo — sin que el usuario tenga que hacer nada.
            def _intentar_subir_y_procesar(ruta_archivo_local, sufijo_captura):
                with glp.expect_file_chooser() as fc_info:
                    glp.locator(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}').click()
                file_chooser = fc_info.value
                file_chooser.set_files(str(ruta_archivo_local))

                glp.locator(f'xpath={XPATH_INPUT_EMAIL}').fill(email_resultado)
                _diag(glp, f"06_formulario_completo{sufijo_captura}")

                glp.locator(f'xpath={XPATH_BTN_PROCESAR}').click()

                # CONFIRMADO por el usuario con captura real: el toast de éxito
                # dice "Archivo procesado exitosamente" / "Se generó el log del
                # archivo procesado". Es un toast — puede desaparecer solo en
                # unos segundos. Por eso ya NO se espera secuencialmente un
                # mensaje primero y el otro después (eso podía dejar pasar un
                # toast que ya se cerró mientras se esperaba el otro) — se
                # sondean los dos en paralelo, cada 500ms, y se toma el primero
                # que aparezca.
                tiempo_max_ms = 25000
                intervalo_ms = 500
                transcurrido_ms = 0
                while transcurrido_ms < tiempo_max_ms:
                    if glp.locator("text=/ya ha sido procesado/i").count() > 0:
                        _diag(glp, f"07_archivo_ya_procesado{sufijo_captura}")
                        return "archivo_repetido"
                    if glp.locator("text=/procesado exitosamente/i").count() > 0:
                        _diag(glp, f"08_exito{sufijo_captura}")
                        return "exito"
                    glp.wait_for_timeout(intervalo_ms)
                    transcurrido_ms += intervalo_ms

                _diag(glp, f"07_sin_confirmacion{sufijo_captura}")
                return "sin_confirmacion"

            resultado = _intentar_subir_y_procesar(ruta_csv, "")
            nombre_final = NOMBRE_ARCHIVO

            if resultado == "archivo_repetido":
                print(f"⚠️ NEXO indicó que '{NOMBRE_ARCHIVO}' ya había sido procesado antes — "
                      f"renombrando y reintentando solo, sin intervención del usuario...")
                nombre_nuevo = generar_nombre_nuevo()
                contenido_bytes = ruta_csv.read_bytes()

                ok_subida, error_subida = subir_csv_renombrado_al_repo(contenido_bytes, nombre_nuevo)
                if not ok_subida:
                    marcar_error(
                        f"El archivo '{NOMBRE_ARCHIVO}' ya había sido procesado por NEXO antes, y no se "
                        f"pudo subir un archivo renombrado para reintentar automáticamente: {error_subida}"
                    )

                # Guardar localmente con el nombre nuevo para poder adjuntarlo en el reintento
                ruta_csv_nueva = ruta_csv.parent / nombre_nuevo
                ruta_csv_nueva.write_bytes(contenido_bytes)
                actualizar_nombre_archivo_en_supabase(nombre_nuevo)
                print(f"🔁 Reintentando con el nombre nuevo: {nombre_nuevo}")

                resultado = _intentar_subir_y_procesar(ruta_csv_nueva, "_reintento")
                nombre_final = nombre_nuevo

                if resultado == "archivo_repetido":
                    # Extremadamente improbable con un nombre recién generado — si
                    # pasa igual, ahí sí es un problema real que necesita revisión.
                    marcar_error(
                        f"NEXO volvió a decir 'archivo ya procesado' incluso con el nombre nuevo "
                        f"({nombre_nuevo}). Esto no debería pasar — revisar manualmente."
                    )

            if resultado == "sin_confirmacion":
                marcar_error(f"No se detectó ni el Log poblándose con SIMs, ni éxito, ni 'archivo ya procesado' "
                              f"tras 'Procesar' (archivo: {nombre_final}) — revisar capturas del run.")

            print(f"✅ Caja {NUMERO_CAJA} subida a NEXO correctamente (archivo: {nombre_final}). "
                  f"Queda pendiente del mail de resultado (o de la recuperación automática por GLP si no llega solo).")

        except Exception as e:
            _diag(page, "99_error_general")
            marcar_error(f"Excepción durante la automatización: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
