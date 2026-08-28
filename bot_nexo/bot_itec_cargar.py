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
from datetime import datetime
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

# ITEC tarda bastante en procesar cajas grandes (~500 SIMs) — estos son TECHOS
# MÁXIMOS de seguridad; el bot sondea "Cargando..." y sigue apenas termina, no
# espera el techo completo salvo que realmente haga falta.
ESPERA_MATERIALES_LOTEO_MS = 300_000  # 5min techo (~2m30s observado, duplicado por margen)
ESPERA_LOTEAR_POR_MS = 300_000        # 5min techo (ídem)
ESPERA_GENERAR_LOTES_MS = 180_000     # 3min  — tras tocar "Generar" en Generar Lotes
ESPERA_MAX_DETECCION_SIN_MATERIAL_MS = 60_000  # 1min — para confirmar que Etapa 2 ya está hecha
INTERVALO_POLLING_MS = 4_000

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
ARCHIVO_LOG_LOCAL = Path(__file__).resolve().parent / "log_fallos_itec.txt"


def _log_local(mensaje):
    """Log técnico completo — NO llega a la app, solo queda en este archivo
    (se sube como artifact junto con las capturas para que un admin lo revise
    sin que el usuario final vea excepciones crudas de Python)."""
    try:
        with open(ARCHIVO_LOG_LOCAL, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.utcnow().isoformat()}] Caja {NUMERO_CAJA}: {mensaje}\n")
    except Exception:
        pass


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


def marcar_itec_error(mensaje_usuario, detalle_tecnico=None):
    """Guarda en Supabase un mensaje CORTO y amigable (lo que ve el usuario en la
    app). El detalle técnico completo (excepción, stack, etc.) va al log local,
    nunca a la app."""
    _log_local(detalle_tecnico or mensaje_usuario)
    print(f"❌ ERROR: {mensaje_usuario}", file=sys.stderr)
    if detalle_tecnico:
        print(f"   (detalle técnico en log local, no visible para el usuario): {detalle_tecnico}", file=sys.stderr)
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={"itec_estado": "error", "itec_error_mensaje": mensaje_usuario[:500]},
        timeout=30,
    )
    sys.exit(1)


def marcar_itec_cargada():
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={"itec_estado": "cargada", "itec_error_mensaje": None, "itec_etapa1_ok": True, "itec_etapa2_ok": True},
        timeout=30,
    )


def marcar_progreso_etapa(etapa1_ok=None, etapa2_ok=None):
    """Registra qué etapa se completó, para que el bot de sincronización de lotes
    (Etapa 3) sepa si puede correr o tiene que avisar 'SIMs no cargadas'/'no loteadas'."""
    body = {}
    if etapa1_ok is not None:
        body["itec_etapa1_ok"] = etapa1_ok
    if etapa2_ok is not None:
        body["itec_etapa2_ok"] = etapa2_ok
    if not body:
        return
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json=body,
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


def _seleccionar_material_o_detectar_sin_stock(page, material):
    """Abre el combo 'Productos con materiales disponibles para lotear', escribe
    el material, y espera hasta 1 minuto a que aparezca una opción real.
    Devuelve True si encontró y seleccionó una opción (hay que lotear).
    Devuelve False si no apareció ninguna en todo ese tiempo — significa que esas
    SIMs ya fueron loteadas antes (Etapa 2 ya está hecha, hay que saltarla)."""
    _abrir_select2(page, label_texto="Productos con materiales disponibles para lotear")
    buscador = page.locator(
        '.select2-drop-active input.select2-input, '
        '.select2-container-active input.select2-input, '
        'input.select2-focused'
    ).first
    try:
        buscador.fill(material, timeout=4000)
    except Exception:
        pass

    transcurrido_ms = 0
    while transcurrido_ms < ESPERA_MAX_DETECCION_SIN_MATERIAL_MS:
        page.wait_for_timeout(INTERVALO_POLLING_MS)
        transcurrido_ms += INTERVALO_POLLING_MS
        opciones_reales = page.locator(
            '.select2-results li:not(.select2-no-results):not(.select2-searching)'
        )
        if opciones_reales.count() > 0:
            opciones_reales.first.click()
            return True
        print(f"🔎 Esperando opciones de material... ({transcurrido_ms // 1000}s / {ESPERA_MAX_DETECCION_SIN_MATERIAL_MS // 1000}s)")

    return False


def _esperar_fin_carga_modal(page, texto_carga="Cargando", tiempo_max_ms=180_000, intervalo_ms=5_000):
    """Espera a que desaparezca el indicador 'Cargando...' del modal. ITEC vuelve
    a pedir datos al servidor después de elegir ciertos campos (ej: Tipo de
    Producto), y con volúmenes grandes de SIMs puede tardar bastante — por eso
    sondeamos en vez de usar una espera fija (podría no alcanzar o ser de más).
    Devuelve True si terminó de cargar, False si se agotó el tiempo máximo."""
    transcurrido_ms = 0
    while transcurrido_ms < tiempo_max_ms:
        try:
            sigue_cargando = page.get_by_text(texto_carga, exact=False).first.is_visible()
        except Exception:
            sigue_cargando = False
        if not sigue_cargando:
            return True
        page.wait_for_timeout(intervalo_ms)
        transcurrido_ms += intervalo_ms
        print(f"🔎 Modal todavía dice 'Cargando...' ({transcurrido_ms // 1000}s / {tiempo_max_ms // 1000}s)")
    return False


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
                    "El login de ITEC no funcionó. Revisar usuario/contraseña en Configuración → Sistemas AMX → Distribución.",
                    detalle_tecnico="Tras enviar el login, seguimos viendo el formulario de usuario/contraseña.",
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
            page.wait_for_timeout(5000)  # tiempo corto para que aparezca el error si lo hay
            _diag(page, "07a_tras_generar_carga")

            # ¿ITEC dice que la caja ya existía? -> no es un error real, se salta Etapa 1
            texto_pagina = ""
            try:
                texto_pagina = page.locator("body").inner_text()
            except Exception:
                pass

            if "se han detectado errores" in texto_pagina.lower():
                if "superpon" in texto_pagina.lower():
                    print(f"ℹ️ ITEC indica que el rango de la caja {NUMERO_CAJA} ya estaba cargado — se omite la Etapa 1 y se continúa con la Etapa 2.")
                    _diag(page, "07b_etapa1_ya_existia")
                else:
                    _diag(page, "07c_error_real_etapa1")
                    marcar_itec_error(
                        "ITEC devolvió un error al cargar la caja en la Etapa 1.",
                        detalle_tecnico=f"Texto de error detectado en la página: {texto_pagina[:400]!r}",
                    )
            else:
                page.wait_for_timeout(30000)  # esperar a que termine de procesar la carga
                _diag(page, "07_carga_masiva_generada")

            marcar_progreso_etapa(etapa1_ok=True)

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

            if not _esperar_fin_carga_modal(page):
                _diag(page, "10b_timeout_cargando_tipo_producto")
                marcar_itec_error(
                    "ITEC tardó demasiado en recargar el formulario tras elegir el Tipo de Producto.",
                    detalle_tecnico=f"El modal siguió mostrando 'Cargando...' por más de {180}s tras seleccionar SIM.",
                )
            _diag(page, "10c_modal_recargado")

            # Sucursal
            _abrir_select2(page, placeholder_texto="Seleccione una sucursal", label_texto="Sucursal")
            _select2_buscar_y_elegir(page, texto=SUCURSAL_CODIGO, opcion_texto=SUCURSAL_TEXTO)
            _diag(page, "11_sucursal_lotes")

            if not _esperar_fin_carga_modal(page):
                _diag(page, "11b_timeout_cargando_sucursal")
                marcar_itec_error(
                    "ITEC tardó demasiado en recargar el formulario tras elegir la Sucursal.",
                    detalle_tecnico=f"El modal siguió mostrando 'Cargando...' por más de {180}s tras seleccionar la sucursal.",
                )
            _diag(page, "11c_modal_recargado")

            # Productos con materiales disponibles para lotear (material) — con
            # detección de "Etapa 2 ya hecha" si no aparece ninguna opción
            hay_material_para_lotear = _seleccionar_material_o_detectar_sin_stock(page, MATERIAL)
            _diag(page, "12_material_seleccionado_o_sin_stock")

            if not hay_material_para_lotear:
                print(f"ℹ️ No aparecieron materiales disponibles para lotear tras {ESPERA_MAX_DETECCION_SIN_MATERIAL_MS//1000}s — "
                      f"la Etapa 2 ya estaba hecha para la caja {NUMERO_CAJA}. Se omite la generación de lotes.")
                marcar_itec_cargada()
                print(f"✅ Caja {NUMERO_CAJA}: no había nada pendiente de lotear (ya estaba hecho). Marcada como Cargada.")
                return

            if not _esperar_fin_carga_modal(page, tiempo_max_ms=ESPERA_MATERIALES_LOTEO_MS):
                _diag(page, "12b2_timeout_cargando_material")
                marcar_itec_error(
                    "ITEC tardó demasiado en calcular los materiales disponibles para lotear.",
                    detalle_tecnico=f"El modal siguió mostrando 'Cargando...' por más de {ESPERA_MATERIALES_LOTEO_MS//1000}s tras elegir el material.",
                )
            _diag(page, "12c_modal_recargado")

            # Verificación de cantidad — corte de seguridad antes de lotear
            try:
                valor_items = page.locator(f'xpath={XPATH_ITEMS_DISPONIBLES}').input_value(timeout=5000)
            except Exception:
                valor_items = None
            print(f"🔎 Materiales Disponibles en ITEC: {valor_items!r} (esperado: {CANTIDAD})")
            if valor_items and valor_items.strip().isdigit() and int(valor_items.strip()) != CANTIDAD:
                _diag(page, "12c_cantidad_no_coincide")
                marcar_itec_error(
                    "La cantidad de SIMs disponibles en ITEC no coincide con la cantidad esperada de la caja. "
                    "Se detuvo antes de lotear para evitar un error mayor — revisar a mano.",
                    detalle_tecnico=f"Items disponibles en ITEC: {valor_items!r}, esperado: {CANTIDAD}",
                )

            # Lotear Por: Por Cantidad
            _abrir_select2(page, label_texto="Lotear Por")
            _select2_buscar_y_elegir(page, texto="Por Cantidad", opcion_texto="Por cantidad")
            _diag(page, "13a_lotear_por_elegido")

            if not _esperar_fin_carga_modal(page, tiempo_max_ms=ESPERA_LOTEAR_POR_MS):
                _diag(page, "13b_timeout_cargando_lotear_por")
                marcar_itec_error(
                    "ITEC tardó demasiado en recargar el formulario tras elegir 'Por Cantidad'.",
                    detalle_tecnico=f"El modal siguió mostrando 'Cargando...' por más de {ESPERA_LOTEAR_POR_MS//1000}s tras elegir Lotear Por.",
                )
            _diag(page, "13_lotear_por_cantidad")

            # Tamaño de Lote (configurable desde GestionSLA, default 1) y Cantidad total
            page.fill(f'xpath={XPATH_BATCH_SIZE}', str(tamano_lote))
            page.fill(f'xpath={XPATH_QUANTITY}', str(CANTIDAD))
            _diag(page, "14_tamanos_completados")

            page.locator(f'xpath={XPATH_BTN_GENERAR_LOTES}').click()
            page.wait_for_timeout(ESPERA_GENERAR_LOTES_MS)
            _diag(page, "15_lotes_generados")

            marcar_itec_cargada()
            print(f"✅ Caja {NUMERO_CAJA} cargada en ITEC y lotes generados correctamente (tamaño de lote: {tamano_lote}).")

        except Exception as e:
            _diag(page, "99_error_general")
            marcar_itec_error(
                "Ocurrió un problema técnico al automatizar ITEC para esta caja. Revisá el log del bot (artifact de la corrida) para más detalle, o reintentá.",
                detalle_tecnico=f"{type(e).__name__}: {e}",
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
