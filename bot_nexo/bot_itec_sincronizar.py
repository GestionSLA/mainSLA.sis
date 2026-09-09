"""
Bot ITEC — Etapa 3: Sincronizar Lotes
=======================================
Requisito previo: Etapas 1 (carga masiva) y 2 (generación de lotes) ya
completadas para esta caja (itec_etapa1_ok / itec_etapa2_ok en Supabase).
Si no lo están, este script se detiene de entrada sin abrir el navegador,
con un mensaje específico según cuál etapa falta.

Flujo:
  1. Login en ITEC (usuario/contraseña, sin MFA)
  2. Entrar a ProductItem → Filtros → Agregar Filtro Por → "Caja"
  3. Escribir el número de caja en el filtro y esperar el resultado
  4. Subir "Tamaño de Página" a 500 (o el máximo necesario)
  5. Scrollear la tabla hasta cargar todas las filas
  6. Extraer N° Serie + Lote de cada fila (validando que la columna "Caja"
     coincida, como control extra)
  7. Guardar el lote de cada SIM en Supabase (tabla distribucion_sims)

Variables de entorno:
  SUPABASE_URL, SUPABASE_KEY (service role)
  CAJA_ID, NUMERO_CAJA
"""
import os
import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CAJA_ID = os.environ["CAJA_ID"]
NUMERO_CAJA = os.environ["NUMERO_CAJA"]

URL_LOGIN = "https://itec.claro.com.ar/Home/Login?ReturnUrl=%2f"
URL_PRODUCT_ITEM = "https://itec.claro.com.ar/ProductItem"

XPATH_USERNAME = '//*[@id="Username"]'
XPATH_PASSWORD = '//*[@id="Password"]'
XPATH_BTN_LOGIN = '/html/body/div/div/div/div/div[2]/form/div[5]/div/button'

XPATH_BTN_FILTROS = '//*[@id="divFilters"]/span/a'
XPATH_CMB_AGREGAR_FILTRO = '//*[@id="cmbAddFilter"]'
XPATH_INPUT_CAJA = '//*[@id="FilterExpressions_0__StringValue"]'
XPATH_BTN_APLICAR_FILTRO = '//*[@id="filtersContainer"]/div[4]/button[1]'
XPATH_TABLA_CONTENEDOR = '//*[@id="divContainer"]'
XPATH_PAGE_SIZE_ARROW = '//*[@id="s2id_cmbPageSize"]/a/span[2]/b'

ESPERA_TRAS_FILTRO_MS = 90_000    # 1m30s — lo que tarda ITEC en traer los resultados
ESPERA_MAX_CARGANDO_MS = 120_000  # techo extra de seguridad (sondeo)
INTERVALO_POLLING_MS = 4_000

CARPETA_CAPTURAS = Path(__file__).resolve().parent / "capturas_itec_sync"
CARPETA_CAPTURAS.mkdir(exist_ok=True)
ARCHIVO_LOG_LOCAL = Path(__file__).resolve().parent / "log_fallos_itec_sync.txt"


def _log_local(mensaje):
    try:
        with open(ARCHIVO_LOG_LOCAL, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] Caja {NUMERO_CAJA}: {mensaje}\n")
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
    if not usuario or not password:
        raise RuntimeError("Faltan credenciales de ITEC en Configuración → Sistemas AMX → Distribución")
    return usuario, password


def obtener_estado_caja():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}&select=itec_etapa1_ok,itec_etapa2_ok,cantidad",
        headers=headers_supabase(), timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError(f"No se encontró la caja {CAJA_ID} en Supabase")
    return rows[0]


def marcar_lotes_error(mensaje_usuario, detalle_tecnico=None):
    """Usa campos SEPARADOS de itec_estado/itec_error_mensaje — no pisa el
    resultado de las Etapas 1 y 2, que ya quedaron marcadas como exitosas."""
    _log_local(detalle_tecnico or mensaje_usuario)
    print(f"❌ ERROR: {mensaje_usuario}", file=sys.stderr)
    if detalle_tecnico:
        print(f"   (detalle técnico, no visible para el usuario): {detalle_tecnico}", file=sys.stderr)
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={"itec_lotes_estado": "error", "itec_lotes_error_mensaje": mensaje_usuario[:500]},
        timeout=30,
    )
    if not r.ok:
        print(f"⚠️ ADEMÁS, no se pudo guardar el error en Supabase: {r.status_code} {r.text[:300]}", file=sys.stderr)
    sys.exit(1)


def marcar_lotes_sincronizados():
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(),
        json={
            "itec_lotes_estado": "sincronizado",
            "itec_lotes_error_mensaje": None,
            "itec_lotes_fecha": datetime.now(timezone.utc).isoformat(),
        },
        timeout=30,
    )
    if not r.ok:
        print(f"⚠️ No se pudo marcar itec_lotes_estado='sincronizado' en Supabase: {r.status_code} {r.text[:300]}", file=sys.stderr)


def _diag(page, etiqueta):
    try:
        print(f"🔎 [{etiqueta}] URL actual: {page.url}")
        print(f"🔎 [{etiqueta}] Título: {page.title()}")
        page.screenshot(path=str(CARPETA_CAPTURAS / f"{etiqueta}.png"), full_page=True)
    except Exception as e:
        print(f"🔎 [{etiqueta}] No se pudo diagnosticar: {e}")


def _esperar_fin_carga(page, texto_carga="Cargando", tiempo_max_ms=ESPERA_MAX_CARGANDO_MS, intervalo_ms=INTERVALO_POLLING_MS):
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
        print(f"🔎 Todavía cargando... ({transcurrido_ms // 1000}s / {tiempo_max_ms // 1000}s)")
    return False


def _abrir_select2(page, placeholder_texto=None, label_texto=None, timeout_ms=8000):
    """Igual criterio que en bot_itec_cargar.py: por texto visible, nunca por
    ids autogenerados (select2-chosen-XXXX cambia de sesión a sesión)."""
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
            label = page.locator(f'xpath=//label[contains(normalize-space(.), "{label_texto}")]').first
            contenedor = label.locator('xpath=ancestor::div[contains(@class,"form-group") or contains(@class,"row")][1]')
            combo = contenedor.locator('.select2-chosen, .select2-choice, a.select2-choice').first
            if combo.count() == 0:
                combo = label.locator('xpath=following::*[contains(@class,"select2-chosen") or contains(@class,"select2-choice")][1]')
            combo.click(timeout=timeout_ms)
            page.wait_for_timeout(400)
            return
        except Exception as e:
            errores.append(f"label {label_texto!r}: {e}")
        try:
            elemento = page.locator(f'xpath=//*[contains(normalize-space(text()), "{label_texto}")]').first
            contenedor = elemento.locator('xpath=ancestor::div[1]')
            combo = contenedor.locator('.select2-chosen, .select2-choice, a.select2-choice').first
            if combo.count() == 0:
                combo = elemento.locator('xpath=following::*[contains(@class,"select2-chosen") or contains(@class,"select2-choice")][1]')
            combo.click(timeout=timeout_ms)
            page.wait_for_timeout(400)
            return
        except Exception as e:
            errores.append(f"texto genérico {label_texto!r}: {e}")
    raise RuntimeError(f"No se pudo abrir el combo Select2. Intentos: {errores}")


def _select2_buscar_y_elegir(page, texto=None, opcion_texto=None, espera_ms=1200):
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


def _contar_filas_tabla():
    def _contar(page):
        n = page.locator('#tableToScroll tbody tr').count()
        if n > 0:
            return n
        n2 = page.locator('#tableToScroll tr').count()
        return max(0, n2 - 1)
    return _contar


_contar_filas = _contar_filas_tabla()


def _scrollear_hasta_cargar_todo(page, cantidad_esperada, tope_intentos=80):
    contenedor = page.locator(f'xpath={XPATH_TABLA_CONTENEDOR}')
    filas_previas = 0
    intentos_sin_cambio = 0
    for intento in range(tope_intentos):
        filas_actuales = _contar_filas(page)
        try:
            contenedor.evaluate("el => el.scrollTop = el.scrollHeight")
        except Exception as e:
            if intento == 0:
                print(f"⚠️ No se pudo scrollear el contenedor de resultados ({e}) — sigo contando lo que ya esté visible.")
        page.wait_for_timeout(700)
        filas_actuales = _contar_filas(page)
        if filas_actuales == filas_previas:
            intentos_sin_cambio += 1
            if intentos_sin_cambio >= 5:
                break
        else:
            intentos_sin_cambio = 0
        filas_previas = filas_actuales
        if filas_actuales >= cantidad_esperada:
            break
    print(f"🔎 Filas cargadas en la tabla tras scrollear: {filas_previas} (esperadas: {cantidad_esperada})")
    return filas_previas


def main():
    estado = obtener_estado_caja()
    if not estado.get("itec_etapa1_ok"):
        marcar_lotes_error("SIMs no cargadas en ITEC", detalle_tecnico="itec_etapa1_ok es false — no se completó la Etapa 1 (Carga Masiva).")
    if not estado.get("itec_etapa2_ok"):
        marcar_lotes_error("SIMs no loteadas", detalle_tecnico="itec_etapa2_ok es false — no se completó la Etapa 2 (Generación de Lotes).")

    cantidad_esperada = int(estado.get("cantidad") or 0)
    usuario, password = obtener_credenciales_itec()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        try:
            page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(f'xpath={XPATH_USERNAME}', timeout=30000)
            page.fill(f'xpath={XPATH_USERNAME}', usuario)
            page.fill(f'xpath={XPATH_PASSWORD}', password)
            page.locator(f'xpath={XPATH_BTN_LOGIN}').click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            _diag(page, "00_tras_login")

            if page.locator(f'xpath={XPATH_USERNAME}').count() > 0:
                marcar_lotes_error(
                    "El login de ITEC no funcionó. Revisar credenciales en Configuración → Sistemas AMX → Distribución.",
                    detalle_tecnico="Tras enviar el login, seguimos viendo el formulario de usuario/contraseña.",
                )

            page.goto(URL_PRODUCT_ITEM, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(23000)
            _diag(page, "01_product_item")

            page.locator(f'xpath={XPATH_BTN_FILTROS}').click()
            page.wait_for_timeout(800)
            _diag(page, "02_filtros_abiertos")

            page.locator(f'xpath={XPATH_CMB_AGREGAR_FILTRO}').click()
            page.wait_for_timeout(500)
            try:
                page.locator(f'xpath={XPATH_CMB_AGREGAR_FILTRO}').select_option(label="Caja")
            except Exception:
                page.get_by_text("Caja", exact=True).first.click(timeout=5000)
            page.wait_for_timeout(1000)
            _diag(page, "03_filtro_caja_elegido")

            page.fill(f'xpath={XPATH_INPUT_CAJA}', NUMERO_CAJA)
            _diag(page, "04_numero_caja_ingresado")

            page.locator(f'xpath={XPATH_BTN_APLICAR_FILTRO}').click()
            page.wait_for_timeout(ESPERA_TRAS_FILTRO_MS)
            _esperar_fin_carga(page, texto_carga="Espere por favor")
            _esperar_fin_carga(page, texto_carga="Cargando")
            _diag(page, "05_resultados_filtrados")

            try:
                primera_fila = page.locator('#tableToScroll tbody tr').first
                caja_en_resultado = primera_fila.locator('td').nth(3).inner_text(timeout=5000).strip()
            except Exception:
                caja_en_resultado = None
            print(f"🔎 Control de filtro: caja pedida={NUMERO_CAJA!r}, caja en el primer resultado={caja_en_resultado!r}")
            if caja_en_resultado is not None and caja_en_resultado != NUMERO_CAJA:
                _diag(page, "05b_filtro_no_aplico")
                marcar_lotes_error(
                    "El filtro por número de caja en ITEC no trajo los resultados esperados (parecían de otra consulta).",
                    detalle_tecnico=f"Caja pedida={NUMERO_CAJA!r}, caja mostrada en el primer resultado={caja_en_resultado!r}.",
                )

            try:
                page.locator(f'xpath={XPATH_PAGE_SIZE_ARROW}').click(timeout=8000)
                page.wait_for_timeout(400)
                _select2_buscar_y_elegir(page, texto="500", opcion_texto="500")
                page.wait_for_timeout(2000)
                _esperar_fin_carga(page, texto_carga="Espere por favor")
                _esperar_fin_carga(page, texto_carga="Cargando")
                _diag(page, "06_tamano_pagina_500")
            except Exception as e:
                print(f"⚠️ No se pudo cambiar el Tamaño de Página a 500, sigo con lo que haya: {e}")

            filas_cargadas = _scrollear_hasta_cargar_todo(page, cantidad_esperada or 500)
            _diag(page, "07_tabla_completa")

            if filas_cargadas <= 0:
                marcar_lotes_error(
                    "No se encontraron resultados en ITEC para esta caja.",
                    detalle_tecnico=f"La tabla quedó con {filas_cargadas} filas tras filtrar por caja {NUMERO_CAJA} y scrollear.",
                )

            filas = page.locator('#tableToScroll tbody tr')
            total_filas = filas.count()
            if total_filas == 0:
                filas = page.locator('#tableToScroll tr')
                total_filas = filas.count()
                indice_inicio = 1
            else:
                indice_inicio = 0
            resultados = {}  # { iccid: lote }
            filas_caja_distinta = 0
            for i in range(indice_inicio, total_filas):
                fila = filas.nth(i)
                celdas = fila.locator('td')
                try:
                    serie = celdas.nth(0).inner_text(timeout=3000).strip()
                    lote = celdas.nth(2).inner_text(timeout=3000).strip()
                    caja_fila = celdas.nth(3).inner_text(timeout=3000).strip()
                except Exception:
                    continue
                if caja_fila and caja_fila != NUMERO_CAJA:
                    filas_caja_distinta += 1
                    continue
                if serie and lote:
                    resultados[serie] = lote

            print(f"🔎 Filas procesadas: {total_filas - indice_inicio} — con lote válido: {len(resultados)} — de otra caja (descartadas): {filas_caja_distinta}")
            if filas_caja_distinta > 0:
                print(f"⚠️ Se descartaron {filas_caja_distinta} filas que no correspondían a la caja {NUMERO_CAJA} (control por columna 'Caja').")

            if not resultados:
                marcar_lotes_error(
                    "No se pudo extraer ningún lote válido de la tabla de ITEC.",
                    detalle_tecnico=f"Filas totales: {total_filas}, ninguna con serie+lote+caja coincidente con {NUMERO_CAJA}.",
                )

            # ── Guardar los lotes en Supabase (distribucion_sims) ────────────
            # BUG ENCONTRADO Y CORREGIDO: antes esto contaba "actualizadas" con
            # solo mirar si el PATCH respondía 200/204 (r.ok) — pero PostgREST
            # devuelve OK aunque el WHERE no encuentre NINGUNA fila para
            # actualizar (un PATCH que no matchea nada no es un error para
            # PostgREST). Si el ICCID de ITEC no coincidía exacto con el
            # guardado en distribucion_sims, cada PATCH "tenía éxito" sin
            # tocar nada — "actualizadas" quedaba en un número mentiroso, la
            # caja se marcaba itec_lotes_estado='sincronizado', pero ningún
            # lote se había guardado de verdad. Ahora se pide
            # Prefer: return=representation y se revisa si realmente vino una
            # fila en la respuesta — esa es la única forma real de confirmar
            # que el PATCH afectó algo.
            actualizadas, sin_match = 0, 0
            ejemplos_sin_match = []
            headers_patch = {**headers_supabase(), "Prefer": "return=representation"}
            for iccid, lote in resultados.items():
                r = requests.patch(
                    f"{SUPABASE_URL}/rest/v1/distribucion_sims?caja_id=eq.{CAJA_ID}&iccid=eq.{iccid}",
                    headers=headers_patch,
                    json={"lote": lote},
                    timeout=30,
                )
                filas_afectadas = []
                if r.ok:
                    try:
                        filas_afectadas = r.json()
                    except Exception:
                        filas_afectadas = []
                if filas_afectadas:
                    actualizadas += 1
                else:
                    sin_match += 1
                    if len(ejemplos_sin_match) < 5:
                        ejemplos_sin_match.append(iccid)

            print(f"✅ Lotes guardados en Supabase (confirmado con return=representation): {actualizadas} — "
                  f"sin coincidencia real en nuestra base: {sin_match}")
            if ejemplos_sin_match:
                print(f"🔎 Ejemplos de ICCID de ITEC que NO matchearon ningún registro en distribucion_sims: {ejemplos_sin_match}")
                print(f"   (comparar el formato exacto contra cómo está guardado el iccid en Supabase — mayúsculas, espacios, longitud, etc.)")

            if actualizadas == 0:
                marcar_lotes_error(
                    "Se encontraron lotes en ITEC pero ninguno coincidió de verdad con las SIMs de esta caja en nuestra base "
                    "(posible diferencia de formato en el ICCID).",
                    detalle_tecnico=f"{len(resultados)} lotes extraídos de ITEC, 0 confirmados con return=representation "
                                     f"contra distribucion_sims (caja {CAJA_ID}). Ejemplos sin match: {ejemplos_sin_match}",
                )

            marcar_lotes_sincronizados()
            print(f"✅ Caja {NUMERO_CAJA}: {actualizadas} SIMs sincronizadas con su lote correspondiente.")

        except Exception as e:
            _diag(page, "99_error_general")
            marcar_lotes_error(
                "Ocurrió un problema técnico al sincronizar los lotes — intentar manualmente.",
                detalle_tecnico=f"{type(e).__name__}: {e}",
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
