"""
Bot Caminantes — Asignación de SIMs en ITEC (Paso 2)
=======================================================
Recibe CAJA_ID (una caja de distribucion_cajas ya asignada a un caminante
EN GESTIONSLA — paso 1, manual, desde el Drawer del modal de detalle).
Entra a https://itec.claro.com.ar/WalkerStock, se para en el caminante
correcto, y ahí, en "Stock sucursal", abre "Recargar lotes" del material
de la caja — selecciona la Lista de Precios, pasa el tamaño de página a
500, y busca (paginando si hace falta) los números de lote que
corresponden a las SIMs de esta caja, marcándolos uno por uno.

Reutiliza login + helpers de Select2 + selección de caminante de
bot_caminantes_walkerstock.py (Bot 1) — mismo patrón ya confirmado
funcionando.

⚠️ TERRITORIO NUEVO: el flujo de "Recargar lotes" (desde el botón de la
fila de Stock sucursal en adelante) nunca se probó en vivo. Los XPaths de
Lista de Precios, Tamaño de Página, "página siguiente" y "Aceptar"
(#btn-accept-reload-by-batch) los dio el usuario directamente inspeccionando
la pantalla — se usan tal cual. Lo que sigue sin confirmar en vivo
(encontrar la fila del material, detectar que terminó de cargar, ubicar
el checkbox de cada lote) es la mejor estimación posible — marcado
explícitamente "AJUSTAR" y con diagnóstico (screenshot + HTML) en cada
punto de incertidumbre, para poder ajustar rápido con el log real en vez
de adivinar de nuevo.

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY   -> service role
  CAJA_ID                       -> id de la caja en distribucion_cajas
"""
import os
import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CAJA_ID = os.environ["CAJA_ID"]

URL_LOGIN = "https://itec.claro.com.ar/Home/Login?ReturnUrl=%2f"
URL_WALKER_STOCK = "https://itec.claro.com.ar/WalkerStock"

BACKOFFICE_TEXTO = "Clara Maonis"
SUCURSAL_CODIGO = "491280"
LISTA_PRECIOS_TEXTO = "LISTA BASE AR"  # ¡ojo! NO confundir con "LISTA BASE" (sin AR) — son dos opciones distintas
TAMANO_PAGINA = "500"

XPATH_USERNAME = '//*[@id="Username"]'
XPATH_PASSWORD = '//*[@id="Password"]'
XPATH_BTN_LOGIN = '/html/body/div/div/div/div/div[2]/form/div[5]/div/button'

XPATH_BTN_BACKOFFICE = '//*[@id="s2id_BackofficeID"]/a/span[2]/b'
XPATH_BTN_WAREHOUSE = '//*[@id="s2id_WarehouseID"]/a/span[2]/b'
XPATH_BTN_WALKER = '//*[@id="s2id_WalkerID"]/a/span[2]/b'
XPATH_BTN_LISTA_PRECIOS = '//*[@id="s2id_PriceListID"]/a/span[2]/b'
XPATH_BTN_TAMANO_PAGINA = '//*[@id="s2id_cmbPageSize"]/a/span[2]/b'
XPATH_BTN_PAGINA_SIGUIENTE = '//*[@id="batchPager"]/div[2]/p/button[2]'

CARPETA_CAPTURAS = Path(__file__).resolve().parent / "capturas_caminantes"
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
    if not usuario or not password:
        raise RuntimeError("Faltan credenciales de ITEC en Configuración → Sistemas AMX → Distribución")
    return usuario, password


def obtener_caja_y_lotes():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}&select=*",
        headers=headers_supabase(), timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError(f"No se encontró la caja {CAJA_ID} en Supabase")
    caja = rows[0]
    if not caja.get("caminante_id") or not caja.get("caminante_nombre"):
        raise RuntimeError("Esta caja todavía no tiene un caminante asignado en GestionSLA")

    r2 = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_sims?caja_id=eq.{CAJA_ID}&select=lote",
        headers=headers_supabase(), timeout=30,
    )
    r2.raise_for_status()
    lotes = sorted({s["lote"].strip().upper() for s in r2.json() if s.get("lote")})
    if not lotes:
        raise RuntimeError(
            "Esta caja no tiene lotes sincronizados todavía — correr primero "
            "'🔄 Sincronizar lotes' en GestionSLA (Etapa 3 de ITEC)."
        )
    return caja, lotes


def marcar_estado(estado, mensaje=None):
    payload = {"itec_asignacion_estado": estado}
    if estado == "asignado":
        payload["itec_asignacion_fecha"] = datetime.now(timezone.utc).isoformat()
        payload["itec_asignacion_error_mensaje"] = None
    if mensaje is not None:
        payload["itec_asignacion_error_mensaje"] = mensaje
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}",
        headers=headers_supabase(), json=payload, timeout=30,
    )
    if not r.ok:
        print(f"⚠️ No se pudo actualizar el estado en Supabase: {r.status_code} {r.text[:300]}", file=sys.stderr)


def _diag(page, etiqueta):
    try:
        print(f"🔎 [{etiqueta}] URL actual: {page.url}")
        page.screenshot(path=str(CARPETA_CAPTURAS / f"{etiqueta}.png"), full_page=True)
    except Exception as e:
        print(f"🔎 [{etiqueta}] No se pudo diagnosticar: {e}")


# ── Select2 (mismos helpers ya confirmados funcionando en Bot 1) ───────
def _abrir_select2(page, boton_xpath, timeout_ms=8000):
    page.locator(f'xpath={boton_xpath}').click(timeout=timeout_ms)
    page.wait_for_timeout(400)


def _select2_elegir(page, texto_buscar=None, texto_opcion=None, espera_ms=1200):
    buscador = page.locator(
        '.select2-drop-active input.select2-input, '
        '.select2-container-active input.select2-input, '
        'input.select2-focused'
    ).first
    if texto_buscar:
        try:
            buscador.fill(texto_buscar, timeout=4000)
            page.wait_for_timeout(espera_ms)
        except Exception:
            pass
    if texto_opcion:
        page.locator('.select2-results li', has_text=texto_opcion).first.click(timeout=5000)
    else:
        page.keyboard.press("Enter")


def _texto_seleccionado_select2(page, id_original):
    try:
        return page.locator(f'#s2id_{id_original} .select2-chosen').inner_text(timeout=2000).strip()
    except Exception:
        return None


def _seleccionar_caminante_con_verificacion(page, nombre_itec, intentos=3):
    for intento in range(1, intentos + 1):
        _abrir_select2(page, XPATH_BTN_WALKER)
        _select2_elegir(page, texto_buscar=nombre_itec, texto_opcion=nombre_itec)
        page.wait_for_timeout(600)
        mostrado = _texto_seleccionado_select2(page, "WalkerID")
        if mostrado and mostrado.strip().lower() == nombre_itec.strip().lower():
            return mostrado
        print(f"  ⚠️ Intento {intento}/{intentos}: el combo quedó mostrando {mostrado!r}, "
              f"no coincide con {nombre_itec!r} — reintentando la selección.")
    return None


def _esperar_espere_por_favor(page, timeout_ms=60000):
    """Sondea hasta que desaparezca el cartel 'Espere por favor...' que
    tapa la pantalla al abrir 'Recargar lotes' — confirmado en vivo (la
    captura a05 mostró el spinner TODAVÍA girando en el momento exacto en
    que el bot ya intentaba clickear Lista de Precios, y por eso el click
    se quedó esperando 8s contra un elemento tapado/no interactuable)."""
    transcurrido = 0
    intervalo = 1000
    while transcurrido < timeout_ms:
        try:
            visible = page.get_by_text("Espere por favor", exact=False).first.is_visible()
        except Exception:
            visible = False
        if not visible:
            return True
        page.wait_for_timeout(intervalo)
        transcurrido += intervalo
    return False


def _abrir_recargar_lotes(page, material):
    """AJUSTAR — busca en la tabla 'Stock sucursal' (#tblWrhStock) la fila
    cuyo Código coincide con el material de la caja, y clickea el botón de
    esa fila (5ta celda) que abre el modal de 'Recargar lotes'. NO se
    hardcodea la posición de fila (el XPath original del usuario usaba
    tr[2], que varía según cuántas filas haya) — se busca por texto."""
    fila = page.locator('#tblWrhStock tbody tr', has_text=material).first
    fila.wait_for(state='visible', timeout=15000)
    boton = fila.locator('td').nth(4).locator('button')
    boton.click(timeout=8000)


def _esperar_tabla_lotes_cargada(page, timeout_ms=45000):
    """Sondea hasta que aparezcan filas reales en la tabla de lotes del
    modal 'Recargar lotes' — evidencia directa de que terminó de cargar,
    en vez de adivinar cuánto tarda la rueda de 'Cargando'."""
    transcurrido = 0
    intervalo = 1000
    while transcurrido < timeout_ms:
        try:
            filas = page.locator('table tbody tr').count()
            if filas > 0:
                # Confirmar que al menos una fila tiene texto real (no placeholder)
                primera = page.locator('table tbody tr').first
                if primera.inner_text(timeout=1000).strip():
                    return True
        except Exception:
            pass
        page.wait_for_timeout(intervalo)
        transcurrido += intervalo
    return False


def _leer_lotes_visibles(page):
    """AJUSTAR — devuelve lista de (texto_lote, locator_checkbox) para cada
    fila visible de la tabla de lotes. Estructura asumida (según el
    screenshot): checkbox | Nº Lote | Tamaño | Fecha Creación."""
    resultado = []
    filas = page.locator('table tbody tr')
    total = filas.count()
    for i in range(total):
        fila = filas.nth(i)
        celdas = fila.locator('td')
        if celdas.count() < 2:
            continue
        try:
            texto_lote = celdas.nth(1).inner_text(timeout=1000).strip().upper()
        except Exception:
            continue
        if not texto_lote:
            continue
        checkbox = celdas.nth(0).locator('input[type="checkbox"]')
        resultado.append((texto_lote, checkbox))
    return resultado


def main():
    usuario, password = obtener_credenciales_itec()
    caja, lotes_objetivo = obtener_caja_y_lotes()
    print(f"📦 Caja {caja['numero_caja']} → caminante {caja['caminante_nombre']!r}")
    print(f"🔖 {len(lotes_objetivo)} lote(s) a buscar: {', '.join(lotes_objetivo[:10])}"
          f"{' ...' if len(lotes_objetivo) > 10 else ''}")

    pendientes = set(lotes_objetivo)
    encontrados = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        try:
            # ── LOGIN ──────────────────────────────────────────────
            page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(f'xpath={XPATH_USERNAME}', timeout=30000)
            page.fill(f'xpath={XPATH_USERNAME}', usuario)
            page.fill(f'xpath={XPATH_PASSWORD}', password)
            page.locator(f'xpath={XPATH_BTN_LOGIN}').click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            if page.locator(f'xpath={XPATH_USERNAME}').count() > 0:
                raise RuntimeError("El login de ITEC no funcionó (usuario/contraseña).")

            # ── WalkerStock ────────────────────────────────────────
            page.goto(URL_WALKER_STOCK, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            _diag(page, "a01_walker_stock")

            _abrir_select2(page, XPATH_BTN_BACKOFFICE)
            _select2_elegir(page, texto_buscar=BACKOFFICE_TEXTO, texto_opcion=BACKOFFICE_TEXTO)
            page.wait_for_timeout(1500)

            _abrir_select2(page, XPATH_BTN_WAREHOUSE)
            _select2_elegir(page, texto_buscar=SUCURSAL_CODIGO, texto_opcion=SUCURSAL_CODIGO)
            page.wait_for_timeout(2000)
            _diag(page, "a02_backoffice_sucursal")

            confirmado = _seleccionar_caminante_con_verificacion(page, caja["caminante_nombre"])
            if confirmado is None:
                _diag(page, "a03_seleccion_caminante_fallida")
                raise RuntimeError(f"No se pudo seleccionar al caminante {caja['caminante_nombre']!r} en el combo.")
            print(f"✅ Caminante seleccionado: {confirmado!r}")
            page.wait_for_timeout(3000)  # dar tiempo a que Stock sucursal termine de refrescar
            _diag(page, "a04_caminante_elegido")

            # ── Abrir "Recargar lotes" del material de la caja ──────
            _abrir_recargar_lotes(page, caja["material"])
            page.wait_for_timeout(1000)
            if not _esperar_espere_por_favor(page):
                _diag(page, "a05b_timeout_espere_por_favor")
                raise RuntimeError("El cartel 'Espere por favor...' no desapareció tras 60s al abrir 'Recargar lotes'.")
            _diag(page, "a05_modal_recargar_lotes")

            # ── Lista de Precios: LISTA BASE AR (¡no "LISTA BASE" sin AR!) ──
            _abrir_select2(page, XPATH_BTN_LISTA_PRECIOS)
            _select2_elegir(page, texto_buscar=LISTA_PRECIOS_TEXTO, texto_opcion=LISTA_PRECIOS_TEXTO)
            page.wait_for_timeout(1000)
            _diag(page, "a06_lista_precios_elegida")

            # ── Tamaño de página: 500 ────────────────────────────────
            # AJUSTAR (encontrado en vivo): con "escribir 500 + Enter" el
            # combo se quedaba en 10 — este combo puntual necesita el click
            # directo sobre la opción "500" del desplegable, igual que
            # Lista de Precios (que sí funcionó así).
            _abrir_select2(page, XPATH_BTN_TAMANO_PAGINA)
            _select2_elegir(page, texto_buscar=TAMANO_PAGINA, texto_opcion=TAMANO_PAGINA)
            page.wait_for_timeout(1500)

            if not _esperar_tabla_lotes_cargada(page):
                _diag(page, "a07_timeout_tabla_lotes")
                raise RuntimeError("La tabla de lotes no terminó de cargar tras 45s.")

            # Verificar de verdad que el tamaño de página cambió — no asumir.
            filas_visibles = page.locator('table tbody tr').count()
            print(f"  🔎 Filas visibles tras fijar tamaño de página a {TAMANO_PAGINA}: {filas_visibles}")
            if filas_visibles < 100:
                _diag(page, "a06b_tamano_pagina_no_aplico")
                print("  ⚠️ El tamaño de página no parece haber cambiado (se esperaban ~500 filas) — "
                      "revisar la captura a06b. Se continúa igual con lo que haya, paginando de a 10 si hace falta.")
            _diag(page, "a07_tabla_lotes_cargada")

            # ── Buscar y marcar los lotes, paginando si hace falta ──
            MAX_PAGINAS = 40
            pagina = 1
            while pendientes and pagina <= MAX_PAGINAS:
                lotes_pagina = _leer_lotes_visibles(page)
                if pagina == 1:
                    print(f"  🔎 Ejemplo de lotes leídos en la página 1: {[l for l,_ in lotes_pagina[:5]]}")
                for texto_lote, checkbox in lotes_pagina:
                    if texto_lote in pendientes:
                        try:
                            checkbox.check(timeout=3000)
                            pendientes.discard(texto_lote)
                            encontrados.add(texto_lote)
                        except Exception as e:
                            print(f"  ⚠️ No se pudo marcar el checkbox de {texto_lote}: {e}")

                print(f"  📄 Página {pagina}: {len(encontrados)}/{len(lotes_objetivo)} lote(s) encontrados hasta ahora.")

                if not pendientes:
                    break

                btn_siguiente = page.locator(f'xpath={XPATH_BTN_PAGINA_SIGUIENTE}')
                cant_btn = btn_siguiente.count()
                habilitado = btn_siguiente.is_enabled() if cant_btn else False
                if cant_btn == 0 or not habilitado:
                    print(f"  ℹ️ No hay más páginas (botón 'siguiente': encontrados={cant_btn}, habilitado={habilitado}).")
                    break
                btn_siguiente.click(timeout=8000)
                page.wait_for_timeout(1500)
                _esperar_tabla_lotes_cargada(page)
                if pagina == 1:
                    # Diagnóstico puntual: confirmar visualmente que la
                    # página realmente cambió (y no quedó en la misma).
                    _diag(page, "a07b_tras_primer_pagina_siguiente")
                pagina += 1

            _diag(page, "a08_lotes_marcados")

            if pendientes:
                print(f"⚠️ {len(pendientes)} lote(s) NO se encontraron en ninguna página: {', '.join(sorted(pendientes))}")

            if not encontrados:
                raise RuntimeError("No se encontró NINGÚN lote de esta caja en ITEC — revisar manualmente antes de reintentar.")

            # ── Aceptar ──────────────────────────────────────────────
            boton_aceptar = page.locator('#btn-accept-reload-by-batch')
            boton_aceptar.wait_for(state='visible', timeout=8000)
            boton_aceptar.click(timeout=8000)
            page.wait_for_timeout(3000)
            _diag(page, "a09_tras_aceptar")

            if pendientes:
                marcar_estado("error", f"Se asignaron {len(encontrados)}/{len(lotes_objetivo)} lotes — no se encontraron: {', '.join(sorted(pendientes))}")
                print(f"⚠️ Asignación PARCIAL: {len(encontrados)}/{len(lotes_objetivo)} lotes.")
                sys.exit(1)
            else:
                marcar_estado("asignado")
                print(f"✅ Listo: {len(encontrados)} lote(s) asignados a {caja['caminante_nombre']} en ITEC.")

        except Exception as e:
            _diag(page, "z99_error_general")
            print(f"❌ ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            marcar_estado("error", f"{type(e).__name__}: {e}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
