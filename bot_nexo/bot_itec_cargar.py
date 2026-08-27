"""
Bot ITEC — Etapas 1 y 2: Carga Masiva + Generación de Lotes
=============================================================
Etapa 1 (ProductItem → Carga Masiva): registra el rango de SIMs de la caja en ITEC.
Etapa 2 (Batch → Generar Lotes): agrupa esas SIMs en lotes del tamaño configurado.
Etapa 3 (consultar qué lote le tocó a cada SIM) todavía NO está en este script —
se agrega en una vuelta posterior (botón "Sincronizar Lotes" en la app).

ITEC es un sistema propio de Claro, con sus propias credenciales (no comparte
login con SAP/Webcom/NEXO) y sin MFA — el login es usuario + contraseña directo.

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY   -> service role
  CAJA_ID, NUMERO_CAJA, MATERIAL, SIM_DESDE, SIM_HASTA, CANTIDAD
"""
import os
import sys
import json
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Windows suele usar cp1252 en consola, que no soporta emojis — forzamos UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CAJA_ID = os.environ["CAJA_ID"]
NUMERO_CAJA = os.environ["NUMERO_CAJA"]
MATERIAL = os.environ["MATERIAL"]
SIM_DESDE = os.environ["SIM_DESDE"]
SIM_HASTA = os.environ["SIM_HASTA"]
CANTIDAD = int(os.environ["CANTIDAD"])

URL_LOGIN = "https://itec.claro.com.ar/Home/Login?ReturnUrl=%2f"
URL_PRODUCT_ITEM = "https://itec.claro.com.ar/ProductItem"
URL_BATCH = "https://itec.claro.com.ar/Batch"
SUCURSAL_CODIGO = "491280"
SUCURSAL_TEXTO = "491280 - U.S.B.S.R.L."

XPATH_USERNAME = '//*[@id="Username"]'
XPATH_PASSWORD = '//*[@id="Password"]'
XPATH_BTN_LOGIN = '/html/body/div/div/div/div/div[2]/form/div[5]/div/button'

XPATH_BTN_MENU_CARGA_MASIVA = '//*[@id="buttonsRow"]/div[2]/div/div[2]/button/i'
XPATH_OPT_CARGA_MASIVA = '//*[@id="buttonsRow"]/div[2]/div/div[2]/ul/li[1]/a'
XPATH_NUMERO_CAJA = '//*[@id="Number"]'
XPATH_DESDE = '//*[@id="From"]'
XPATH_HASTA = '//*[@id="To"]'
XPATH_BTN_GENERAR_CARGA = '//*[@id="btnSubmitBoxEdit"]'

XPATH_BTN_MENU_LOTES = '//*[@id="buttonsRow"]/div[2]/div[2]/button/i'
XPATH_OPT_GENERAR_LOTES = '//*[@id="buttonsRow"]/div[2]/div[2]/ul/li[1]/a'
XPATH_ITEMS_DISPONIBLES = '//*[@id="Items"]'
XPATH_BATCH_SIZE = '//*[@id="BatchSize"]'
XPATH_QUANTITY = '//*[@id="Quantity"]'
XPATH_BTN_GENERAR_LOTES = '//*[@id="modal-generate"]/div[3]/button[2]'

CARPETA_CAPTURAS = Path(__file__).resolve().parent / "capturas_itec"
CARPETA_CAPTURAS.mkdir(exist_ok=True)


def headers_supabase():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def obtener_credenciales_itec():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=distribucion_config",
        headers=headers_supabase(), timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError("No se encontró la config global en Supabase")
    raw = rows[0].get("distribucion_config") or "{}"
    cfg = json.loads(raw) if isinstance(raw, str) else raw
    usuario = cfg.get("itec_user")
    password = cfg.get("itec_pass")
    tamano_lote = cfg.get("itec_tamano_lote") or 1
    if not usuario or not password:
        raise RuntimeError("Faltan credenciales de ITEC en Configuración → Sistemas AMX → Distribución")
    return usuario, password, int(tamano_lote)


def marcar_itec_error(mensaje):
    print(f"❌ ERROR: {mensaje}", file=sys.stderr)
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={"itec_estado": "error", "itec_error_mensaje": mensaje[:500]},
        timeout=30,
    )
    sys.exit(1)


def marcar_itec_cargada():
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={"itec_estado": "cargada", "itec_error_mensaje": None},
        timeout=30,
    )


def _diag(page, etiqueta):
    """Deja rastro en el LOG (visible directo en Actions) + una captura."""
    try:
        print(f"🔎 [{etiqueta}] URL actual: {page.url}")
        print(f"🔎 [{etiqueta}] Título: {page.title()}")
        page.screenshot(path=str(CARPETA_CAPTURAS / f"{etiqueta}.png"), full_page=True)
    except Exception as e:
        print(f"🔎 [{etiqueta}] No se pudo diagnosticar: {e}")


# ── Select2 sin depender de ids autogenerados ────────────────────────────────
# Los ids "select2-chosen-XXXX" y "s2id_autogenXXXX_search" son un contador
# global de la librería: cambian de sesión a sesión según cuántos combos Select2
# se hayan inicializado antes en la página. Por eso NUNCA hay que hardcodearlos.
# En cambio: abrimos el combo por el texto de su placeholder (ej: "Seleccione un
# producto") o por el <label> del campo, y una vez abierto usamos las clases CSS
# fijas de Select2 (.select2-drop-active, .select2-results) que sí son estables.

def _abrir_select2(page, placeholder_texto=None, label_texto=None, timeout_ms=8000):
    """Abre un combo Select2 probando, en orden, varias formas de ubicarlo sin
    depender de ids autogenerados."""
    errores = []

    if placeholder_texto:
        try:
            page.get_by_text(placeholder_texto, exact=True).first.click(timeout=timeout_ms)
            page.wait_for_timeout(400)
            return
        except Exception as e:
            errores.append(f"placeholder {placeholder_texto!r}: {e}")

    if label_texto:
        try:
            label = page.locator(
                f'xpath=//label[contains(normalize-space(.), "{label_texto}")]'
            ).first
            # El combo suele estar en el mismo bloque del formulario que el label,
            # o inmediatamente después en el DOM — probamos ambas rutas.
            contenedor = label.locator(
                'xpath=ancestor::div[contains(@class,"form-group") or contains(@class,"row")][1]'
            )
            combo = contenedor.locator('.select2-chosen, .select2-choice, a.select2-choice').first
            if combo.count() == 0:
                combo = label.locator(
                    'xpath=following::*[contains(@class,"select2-chosen") or contains(@class,"select2-choice")][1]'
                )
            combo.click(timeout=timeout_ms)
            page.wait_for_timeout(400)
            return
        except Exception as e:
            errores.append(f"label {label_texto!r}: {e}")

    raise RuntimeError(f"No se pudo abrir el combo Select2. Intentos: {errores}")


def _select2_buscar_y_elegir(page, texto=None, opcion_texto=None, espera_ms=1200):
    """Con el combo YA ABIERTO: escribe en el buscador (si corresponde) y elige
    una opción. Usa clases CSS estándar de Select2, no ids autogenerados."""
    buscador = page.locator(
        '.select2-drop-active input.select2-input, '
        '.select2-container-active input.select2-input, '
        'input.select2-focused'
    ).first
    if texto:
        try:
            buscador.fill(texto, timeout=4000)
            page.wait_for_timeout(espera_ms)
        except Exception:
            pass
    if opcion_texto:
        try:
            page.locator('.select2-results li', has_text=opcion_texto).first.click(timeout=5000)
            return
        except Exception:
            pass
    page.keyboard.press("Enter")


def main():
    usuario, password, tamano_lote = obtener_credenciales_itec()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        try:
            # ── LOGIN ──────────────────────────────────────────────────────
            page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(f'xpath={XPATH_USERNAME}', timeout=30000)
            page.fill(f'xpath={XPATH_USERNAME}', usuario)
            page.fill(f'xpath={XPATH_PASSWORD}', password)
            _diag(page, "00_login_completado")
            page.locator(f'xpath={XPATH_BTN_LOGIN}').click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            _diag(page, "01_tras_login")

            if page.locator(f'xpath={XPATH_USERNAME}').count() > 0:
                marcar_itec_error(
                    "El login de ITEC no funcionó — seguimos viendo el formulario de usuario/contraseña. "
                    "Revisar credenciales en Configuración → Sistemas AMX → Distribución."
                )

            # ── ETAPA 1: Carga Masiva (ProductItem) ─────────────────────────
            page.goto(URL_PRODUCT_ITEM, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(23000)  # la página tarda ~23s en terminar de cargar
            _diag(page, "02_product_item")

            page.locator(f'xpath={XPATH_BTN_MENU_CARGA_MASIVA}').click()
            page.wait_for_timeout(800)
            page.locator(f'xpath={XPATH_OPT_CARGA_MASIVA}').click()
            page.wait_for_timeout(1500)
            _diag(page, "03_modal_carga_masiva")

            # Producto (material) — ej: 7001355. Placeholder visible: "Seleccione un producto"
            _abrir_select2(page, placeholder_texto="Seleccione un producto", label_texto="Producto")
            _select2_buscar_y_elegir(page, texto=MATERIAL)
            page.wait_for_timeout(1000)
            _diag(page, "04_producto_seleccionado")

            # Nro. de Caja / Desde / Hasta
            page.fill(f'xpath={XPATH_NUMERO_CAJA}', NUMERO_CAJA)
            page.fill(f'xpath={XPATH_DESDE}', SIM_DESDE)
            page.fill(f'xpath={XPATH_HASTA}', SIM_HASTA)
            _diag(page, "05_rango_completado")

            # Sucursal (única opción: 491280 - U.S.B.S.R.L.). Placeholder: "Seleccione una sucursal"
            _abrir_select2(page, placeholder_texto="Seleccione una sucursal", label_texto="Sucursal")
            _select2_buscar_y_elegir(page, texto=SUCURSAL_CODIGO, opcion_texto=SUCURSAL_TEXTO)
            _diag(page, "06_sucursal_seleccionada")

            page.locator(f'xpath={XPATH_BTN_GENERAR_CARGA}').click()
            page.wait_for_timeout(30000)
            _diag(page, "07_carga_masiva_generada")

            # ── ETAPA 2: Generar Lotes (Batch) ──────────────────────────────
            page.goto(URL_BATCH, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            _diag(page, "08_batch")

            page.locator(f'xpath={XPATH_BTN_MENU_LOTES}').click()
            page.wait_for_timeout(800)
            page.locator(f'xpath={XPATH_OPT_GENERAR_LOTES}').click()
            page.wait_for_timeout(1500)
            _diag(page, "09_modal_generar_lotes")

            # Tipo de Producto: SIM
            _abrir_select2(page, label_texto="Tipo de Producto")
            _select2_buscar_y_elegir(page, texto="SIM", opcion_texto="SIM")
            _diag(page, "10_tipo_producto_sim")

            # Sucursal
            _abrir_select2(page, placeholder_texto="Seleccione una sucursal", label_texto="Sucursal")
            _select2_buscar_y_elegir(page, texto=SUCURSAL_CODIGO, opcion_texto=SUCURSAL_TEXTO)
            _diag(page, "11_sucursal_lotes")

            # Productos con materiales disponibles para lotear (material)
            _abrir_select2(page, label_texto="Productos con materiales disponibles para lotear")
            _select2_buscar_y_elegir(page, texto=MATERIAL)
            page.wait_for_timeout(30000)  # tarda ~30s en traer los materiales disponibles
            _diag(page, "12_material_seleccionado")

            # Verificación de cantidad — corte de seguridad antes de lotear
            try:
                valor_items = page.locator(f'xpath={XPATH_ITEMS_DISPONIBLES}').input_value(timeout=5000)
            except Exception:
                valor_items = None
            print(f"🔎 Materiales Disponibles en ITEC: {valor_items!r} (esperado: {CANTIDAD})")
            if valor_items and valor_items.strip().isdigit() and int(valor_items.strip()) != CANTIDAD:
                _diag(page, "12b_cantidad_no_coincide")
                marcar_itec_error(
                    f"La cantidad de 'Materiales Disponibles' en ITEC ({valor_items}) no coincide con la "
                    f"cantidad esperada de la caja ({CANTIDAD}). Se detiene antes de lotear para revisar a mano."
                )

            # Lotear Por: Por Cantidad
            _abrir_select2(page, label_texto="Lotear Por")
            _select2_buscar_y_elegir(page, texto="Por Cantidad", opcion_texto="Por cantidad")
            page.wait_for_timeout(10000)
            _diag(page, "13_lotear_por_cantidad")

            # Tamaño de Lote (configurable desde GestionSLA, default 1) y Cantidad total
            page.fill(f'xpath={XPATH_BATCH_SIZE}', str(tamano_lote))
            page.fill(f'xpath={XPATH_QUANTITY}', str(CANTIDAD))
            _diag(page, "14_tamanos_completados")

            page.locator(f'xpath={XPATH_BTN_GENERAR_LOTES}').click()
            page.wait_for_timeout(30000)
            _diag(page, "15_lotes_generados")

            marcar_itec_cargada()
            print(f"✅ Caja {NUMERO_CAJA} cargada en ITEC y lotes generados correctamente (tamaño de lote: {tamano_lote}).")

        except Exception as e:
            _diag(page, "99_error_general")
            marcar_itec_error(f"Excepción durante la automatización de ITEC: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
