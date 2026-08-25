"""
Bot NEXO — Subida de archivo de presuspensión masiva
=====================================================
Flujo:
  1. Login SSO en 2 pasos (usuario / contraseña — mismas credenciales que Bot SAP)
  2. Entrar a GLP (se abre en pestaña nueva)
  3. Click en el ícono lateral "Levanta presuspensión masiva"
  4. Desplegar el panel "Levantar Presuspensión"
  5. Subir el CSV (ya commiteado en nexo_uploads/<NOMBRE_ARCHIVO> por la app)
  6. Completar el email de resultados
  7. Click en "Procesar" y esperar el mensaje de éxito
  8. Actualizar el estado en Supabase (pendiente confirmado / error)

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY   -> service role (para poder hacer UPDATE sin RLS de usuario)
  CAJA_ID, NUMERO_CAJA, NOMBRE_ARCHIVO
"""
import os
import sys
import time
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CAJA_ID = os.environ["CAJA_ID"]
NUMERO_CAJA = os.environ["NUMERO_CAJA"]
NOMBRE_ARCHIVO = os.environ["NOMBRE_ARCHIVO"]

URL_NEXO_HOME = "https://nexostealth-claroaup.msappproxy.net/"

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
    """Trae usuario/contraseña de SAP (reutilizadas para NEXO) y el email de resultados."""
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
    dist_cfg_raw = cfg.get("distribucion_config") or "{}"
    import json
    dist_cfg = json.loads(dist_cfg_raw) if isinstance(dist_cfg_raw, str) else dist_cfg_raw
    email_resultado = dist_cfg.get("email_resultado")
    if not usuario or not password:
        raise RuntimeError("Faltan credenciales de SAP en Supabase (se reutilizan para NEXO)")
    if not email_resultado:
        raise RuntimeError("Falta el email de resultados en Configuración → Distribución")
    return usuario, password, email_resultado


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
    ruta_csv = Path(__file__).resolve().parent.parent / "nexo_uploads" / NOMBRE_ARCHIVO
    if not ruta_csv.exists():
        marcar_error(f"No se encontró el archivo {ruta_csv} en el repo (¿se commiteó bien desde la app?)")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        try:
            # 1) Entrar a NEXO. Si no hay sesión, el propio sitio redirige al login que
            #    corresponda (no usamos una URL de login "pre-armada": esas traen un
            #    state/nonce de una sesión anterior que ya está vencido y confunde el flujo)
            page.goto(URL_NEXO_HOME, wait_until="networkidle", timeout=60000)
            _diag(page, "00_pantalla_inicial")

            # 2) Login de Microsoft (login.microsoftonline.com) — pantalla de EMAIL
            #    Selectores específicos y únicos del formulario de Microsoft (no genéricos,
            #    para no engancharnos con otro campo de la página por error como pasó antes)
            page.wait_for_selector('input[type="email"]', timeout=45000)
            page.fill('input[type="email"]', usuario)
            _diag(page, "01_usuario_completado")
            page.click('input[type="submit"]')

            # 3) Login de Microsoft — pantalla de CONTRASEÑA
            page.wait_for_selector('input[type="password"]', timeout=45000)
            _diag(page, "02_pantalla_password")
            page.fill('input[type="password"]', password)
            page.click('input[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=45000)
            _diag(page, "02b_tras_enviar_password")

            # 3b) Microsoft suele preguntar "¿Seguir conectado?" (KMSI) después del login.
            #     Es opcional — si no aparece, seguimos de largo. Si aparece, tocamos "No".
            if page.locator('#idBtn_Back').count() > 0:
                _diag(page, "02c_prompt_seguir_conectado")
                page.locator('#idBtn_Back').click()
                page.wait_for_load_state("networkidle", timeout=30000)

            # 4) Confirmar que llegamos a NEXO
            page.wait_for_url(lambda u: "nexostealth" in u, timeout=60000)
            # La SPA suele seguir cargando contenido (menú, permisos del usuario, etc.)
            # después de que la URL ya cambió — le damos tiempo extra antes de buscar el link.
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            _diag(page, "03_login_ok")

            # 5) Click en "GLP" — se abre pestaña nueva
            existe_glp = page.locator(f'xpath={XPATH_BTN_GLP}').count() > 0
            print(f"🔎 ¿Existe el link GLP en el DOM? {existe_glp}")
            if not existe_glp:
                _diag(page, "03b_glp_no_encontrado")
                marcar_error("Llegamos a NEXO pero el link 'GLP' no está en la página (revisar captura 03_login_ok.png / 03b_glp_no_encontrado.png — puede que el menú tenga otra estructura o el usuario no tenga permiso de ver esa opción)")
            with context.expect_page() as nueva_pagina_info:
                page.locator(f'xpath={XPATH_BTN_GLP}').click(timeout=45000)
            glp = nueva_pagina_info.value
            glp.wait_for_load_state("networkidle", timeout=60000)
            _diag(glp, "04_glp_abierto")

            # 6) Click en el ícono lateral "Levanta presuspensión masiva"
            glp.locator(f'xpath={XPATH_BTN_PRESUSPENSION_LATERAL}').click()

            # 7) Desplegar el panel "Levantar Presuspensión"
            glp.locator(f'xpath={XPATH_PANEL_LEVANTAR}').click()
            glp.wait_for_selector(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}', timeout=15000)
            _diag(glp, "05_panel_desplegado")

            # 8) Subir el archivo
            with glp.expect_file_chooser() as fc_info:
                glp.locator(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}').click()
            file_chooser = fc_info.value
            file_chooser.set_files(str(ruta_csv))

            # 9) Completar el email de resultados
            glp.locator(f'xpath={XPATH_INPUT_EMAIL}').fill(email_resultado)
            _diag(glp, "06_formulario_completo")

            # 10) Click en "Procesar"
            glp.locator(f'xpath={XPATH_BTN_PROCESAR}').click()

            # 11) Esperar el mensaje de éxito (toast inferior izquierdo)
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
