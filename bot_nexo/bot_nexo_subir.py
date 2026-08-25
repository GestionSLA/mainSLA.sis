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

URL_LOGIN_INICIAL = (
    "https://ingreso-claroaup.msappproxy.net/auth/realms/claro-ad/protocol/openid-connect/auth"
    "?state=4c38f8bf734669c02cdb328a7a22ba01&scope=openid&client_id=container-chome"
    "&nonce=5642571526c9f2081f783c4adce5e43e"
    "&redirect_uri=https%3A%2F%2Fnexostealth.claro.com.ar%2Fauth&response_type=code"
)
URL_NEXO_HOME = "https://nexostealth-claroaup.msappproxy.net/"

XPATH_BTN_GLP = '//*[@id="app"]/div[2]/div[2]/ul/a[2]'
XPATH_BTN_PRESUSPENSION_LATERAL = '//*[@id="root"]/div/div[2]/nav/a[2]/img'
XPATH_PANEL_LEVANTAR = '//*[@id="panel1bh-header"]/div[1]'
XPATH_BTN_SELECCIONAR_ARCHIVO = '//*[@id="upload-form"]/div[1]/div[1]/div[1]/label/button'
XPATH_INPUT_EMAIL = '//*[@id="email"]'
XPATH_BTN_PROCESAR = '//*[@id="upload-form"]/div[2]/button[1]'

CARPETA_CAPTURAS = Path(__file__).resolve().parent.parent / "capturas"
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


def main():
    usuario, password, email_resultado = obtener_config()
    ruta_csv = Path(__file__).resolve().parent.parent / "nexo_uploads" / NOMBRE_ARCHIVO
    if not ruta_csv.exists():
        marcar_error(f"No se encontró el archivo {ruta_csv} en el repo (¿se commiteó bien desde la app?)")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            # 1) Login — paso 1: usuario
            page.goto(URL_LOGIN_INICIAL, wait_until="networkidle", timeout=60000)
            page.wait_for_selector('input[type="text"], input[type="email"], #username', timeout=30000)
            campo_usuario = page.locator('#username, input[name="username"], input[type="text"]').first
            campo_usuario.fill(usuario)
            btn_siguiente = page.locator('button[type="submit"], input[type="submit"]').first
            btn_siguiente.click()

            # 2) Login — paso 2: contraseña
            page.wait_for_selector('#password, input[type="password"]', timeout=30000)
            page.locator('#password, input[type="password"]').first.fill(password)
            page.locator('button[type="submit"], input[type="submit"]').first.click()

            # 3) Confirmar que llegamos a NEXO
            page.wait_for_url(lambda u: "nexostealth" in u, timeout=60000)
            page.screenshot(path=str(CARPETA_CAPTURAS / "01_login_ok.png"))

            # 4) Click en "GLP" — se abre pestaña nueva
            with context.expect_page() as nueva_pagina_info:
                page.locator(f'xpath={XPATH_BTN_GLP}').click()
            glp = nueva_pagina_info.value
            glp.wait_for_load_state("networkidle", timeout=60000)

            # 5) Click en el ícono lateral "Levanta presuspensión masiva"
            glp.locator(f'xpath={XPATH_BTN_PRESUSPENSION_LATERAL}').click()

            # 6) Desplegar el panel "Levantar Presuspensión"
            glp.locator(f'xpath={XPATH_PANEL_LEVANTAR}').click()
            glp.wait_for_selector(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}', timeout=15000)

            # 7) Subir el archivo
            with glp.expect_file_chooser() as fc_info:
                glp.locator(f'xpath={XPATH_BTN_SELECCIONAR_ARCHIVO}').click()
            file_chooser = fc_info.value
            file_chooser.set_files(str(ruta_csv))

            # 8) Completar el email de resultados
            glp.locator(f'xpath={XPATH_INPUT_EMAIL}').fill(email_resultado)
            glp.screenshot(path=str(CARPETA_CAPTURAS / "02_formulario_completo.png"))

            # 9) Click en "Procesar"
            glp.locator(f'xpath={XPATH_BTN_PROCESAR}').click()

            # 10) Esperar el mensaje de éxito (toast inferior izquierdo)
            try:
                glp.wait_for_selector("text=/éxito|exitosa|procesad/i", timeout=30000)
            except PWTimeout:
                glp.screenshot(path=str(CARPETA_CAPTURAS / "03_sin_confirmacion.png"))
                marcar_error("No se detectó el mensaje de confirmación de NEXO tras 'Procesar' — revisar captura 03_sin_confirmacion.png")

            glp.screenshot(path=str(CARPETA_CAPTURAS / "04_exito.png"))
            print(f"✅ Caja {NUMERO_CAJA} subida a NEXO correctamente. Queda pendiente del mail de resultado.")

        except Exception as e:
            try:
                page.screenshot(path=str(CARPETA_CAPTURAS / "99_error_general.png"))
            except Exception:
                pass
            marcar_error(f"Excepción durante la automatización: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
