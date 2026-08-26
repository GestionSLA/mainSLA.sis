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
import requests
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

URL_NEXO_HOME = "https://nexostealth-claroaup.msappproxy.net/"
URL_WEBCOM = "https://claroaup.sharepoint.com/sites/webcom/SitePages/Inicio.aspx"
URL_WEBCOM_PORTAL_AGENTES = "https://claroaup.sharepoint.com/sites/webcom/SitePages/Portal-Agentes.aspx"

XPATH_BTN_GLP = '//*[@id="app"]/div[2]/div[2]/ul/a[2]'
XPATH_BTN_PRESUSPENSION_LATERAL = '//*[@id="root"]/div/div[2]/nav/a[2]/img'
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

            # 2) Si nos manda a login, completamos usuario/contraseña de SAP
            if "login.microsoftonline.com" in page.url or "login" in page.url.lower():
                page.wait_for_selector('input[type="email"]', timeout=45000)
                page.fill('input[type="email"]', usuario)
                _diag(page, "00b_usuario_completado")
                page.click('input[type="submit"]')

                page.wait_for_selector('input[type="password"]', timeout=45000)
                _diag(page, "00c_pantalla_password")
                page.fill('input[type="password"]', password)

                # El envío de la contraseña puede abrir una pestaña nueva ya autenticada.
                # Si no abre ninguna, seguimos en la misma página (fallback).
                try:
                    with context.expect_page(timeout=20000) as pagina_nexo_info:
                        page.click('input[type="submit"]')
                    page = pagina_nexo_info.value
                except PWTimeout:
                    pass
                page.wait_for_load_state("domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                _diag(page, "00d_tras_login")

                # Posible prompt "¿Seguir conectado?" (KMSI) — opcional
                if page.locator('#idBtn_Back').count() > 0:
                    _diag(page, "00e_prompt_seguir_conectado")
                    page.locator('#idBtn_Back').click()
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)

            _diag(page, "00f_nexo_listo")

            # 2b) Si a pesar de todo terminamos en login, ahí sí es un tema de sesión real
            if "login.microsoftonline.com" in page.url or "login" in page.url.lower():
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

            # 4) Click en el ícono lateral "Levanta presuspensión masiva"
            glp.locator(f'xpath={XPATH_BTN_PRESUSPENSION_LATERAL}').click()

            # 5) Desplegar el panel "Levantar Presuspensión"
            glp.locator(f'xpath={XPATH_PANEL_LEVANTAR}').click()
            glp.wait_for_selector(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}', timeout=15000)
            _diag(glp, "05_panel_desplegado")

            # 6) Subir el archivo
            with glp.expect_file_chooser() as fc_info:
                glp.locator(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}').click()
            file_chooser = fc_info.value
            file_chooser.set_files(str(ruta_csv))

            # 7) Completar el email de resultados
            glp.locator(f'xpath={XPATH_INPUT_EMAIL}').fill(email_resultado)
            _diag(glp, "06_formulario_completo")

            # 8) Click en "Procesar"
            glp.locator(f'xpath={XPATH_BTN_PROCESAR}').click()

            # 9) Esperar el mensaje de éxito (toast inferior izquierdo)
            try:
                glp.wait_for_selector("text=/éxito|exitosa|procesad/i", timeout=30000)
            except PWTimeout:
                _diag(glp, "07_sin_confirmacion")
                marcar_error("No se detectó el mensaje de confirmación de NEXO tras 'Procesar' — revisar captura 07_sin_confirmacion.png")

            _diag(glp, "08_exito")
            print(f"✅ Caja {NUMERO_CAJA} subida a NEXO correctamente. Queda pendiente del mail de resultado.")

        except Exception as e:
            _diag(page, "99_error_general")
            marcar_error(f"Excepción durante la automatización: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
