"""
Bot Caminantes — Saldo entregado hoy (ITEC Traceability)
============================================================
Bot 2 de 3 del módulo Caminantes. Entra a https://itec.claro.com.ar/Traceability:
  1. Selecciona la fecha de HOY en el calendario (dtpDate).
  2. Sucursal: única opción "491280 - U.S.B. S.R.L."
  3. Tipo de Producto: "Carga Virtual"
  4. Producto: única opción "SV000 - Carga Virtual"
  5. Click en "Ver Trazabilidad" (btnShow).
  6. Lee la tabla de movimientos — columna "Cantidad" sumada por caminante
     (columna "Detalle", que trae "Apellido, Nombre" — mismo formato que
     ya guardó el Bot 1 en Supabase), filtrando por columna "Fecha" = hoy
     como control extra (además del filtro ya aplicado en el buscador).

Guarda SOLO la columna `saldo_entregado` de caminantes_saldo_diario (upsert
por caminante_id+fecha) — NUNCA toca `recaudado`, que la escribe el Bot 3.

ITEC NO requiere self-hosted por el mecanismo de login (usuario+contraseña
propio, sin MFA) — pero SÍ requiere self-hosted porque itec.claro.com.ar
bloquea por IP el acceso desde la nube de GitHub (confirmado empíricamente
con el Bot 1: la página nunca respondía en ubuntu-latest).

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY   -> service role
"""
import os
import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

URL_LOGIN = "https://itec.claro.com.ar/Home/Login?ReturnUrl=%2f"
URL_TRACEABILITY = "https://itec.claro.com.ar/Traceability"

SUCURSAL_CODIGO = "491280"  # mismo código confirmado en WalkerStock

XPATH_USERNAME = '//*[@id="Username"]'
XPATH_PASSWORD = '//*[@id="Password"]'
XPATH_BTN_LOGIN = '/html/body/div/div/div/div/div[2]/form/div[5]/div/button'

XPATH_CALENDARIO = '//*[@id="dtpDate"]/div/span'
XPATH_BTN_WAREHOUSE = '//*[@id="s2id_WarehouseID"]/a/span[2]/b'
XPATH_BTN_TIPO_PRODUCTO = '//*[@id="s2id_ProductType"]/a/span[2]/b'
XPATH_BTN_PRODUCTO = '//*[@id="s2id_ProductID"]/a/span[2]/b'
XPATH_BTN_VER_TRAZABILIDAD = '//*[@id="btnShow"]'

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


def obtener_caminantes_activos():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/caminantes?activo=eq.true&select=id,nombre",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _diag(page, etiqueta):
    try:
        print(f"🔎 [{etiqueta}] URL actual: {page.url}")
        page.screenshot(path=str(CARPETA_CAPTURAS / f"{etiqueta}.png"), full_page=True)
    except Exception as e:
        print(f"🔎 [{etiqueta}] No se pudo diagnosticar: {e}")


# ── Select2 (mismos helpers ya confirmados funcionando en Bot 1 — sin
# scoping extra, la versión simple es la que anda) ─────────────────────
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


def _seleccionar_fecha_hoy(page):
    """AJUSTAR — territorio nuevo, no confirmado en vivo todavía. Abre el
    datepicker (bootstrap-datetimepicker, a juzgar por el CSS que carga
    ITEC) y clickea la celda de HOY. Se prueban, en orden: la clase
    estándar '.day.today' de ese widget, y como respaldo, cualquier celda
    '.day' visible cuyo texto sea el número de día de hoy (sin clases
    'old'/'new', que marcan días de otro mes)."""
    hoy = datetime.now(TZ_AR)
    page.locator(f'xpath={XPATH_CALENDARIO}').click(timeout=8000)
    page.wait_for_timeout(600)

    try:
        page.locator('.day.today').first.click(timeout=3000)
        page.wait_for_timeout(400)
        return True
    except Exception:
        pass

    try:
        celda = page.locator('.day:not(.old):not(.new)', has_text=str(hoy.day)).first
        celda.click(timeout=3000)
        page.wait_for_timeout(400)
        return True
    except Exception as e:
        print(f"  ⚠️ No se pudo seleccionar la fecha de hoy en el calendario: {e}")
        return False


def _parsear_numero(texto):
    if texto is None:
        return None
    texto = str(texto).strip().replace(".", "").replace(",", ".")
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def guardar_entregado(caminante_id, monto):
    payload = {"caminante_id": caminante_id, "fecha": datetime.now(TZ_AR).date().isoformat(), "saldo_entregado": round(monto, 2)}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/caminantes_saldo_diario?on_conflict=caminante_id,fecha",
        headers={**headers_supabase(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=payload, timeout=30,
    )
    return r.ok, (r.status_code, r.text[:300] if not r.ok else "")


def main():
    usuario, password = obtener_credenciales_itec()
    hoy = datetime.now(TZ_AR).date()
    print(f"📅 Buscando movimientos de HOY: {hoy.strftime('%d/%m/%Y')}")

    caminantes = obtener_caminantes_activos()
    print(f"🚶 {len(caminantes)} caminante(s) activo(s) en Supabase (correr primero el Bot 1 si esto da 0).")
    mapa_nombres = {c["nombre"].strip().lower(): c for c in caminantes}

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

            # ── Traceability ───────────────────────────────────────
            page.goto(URL_TRACEABILITY, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            _diag(page, "20_traceability")

            if not _seleccionar_fecha_hoy(page):
                _diag(page, "20b_fecha_fallida")
                raise RuntimeError("No se pudo seleccionar la fecha de hoy en el calendario — revisar captura 20b.")
            _diag(page, "21_fecha_elegida")

            _abrir_select2(page, XPATH_BTN_WAREHOUSE)
            _select2_elegir(page, texto_buscar=SUCURSAL_CODIGO, texto_opcion=SUCURSAL_CODIGO)
            page.wait_for_timeout(1000)
            _diag(page, "22_sucursal_elegida")

            _abrir_select2(page, XPATH_BTN_TIPO_PRODUCTO)
            _select2_elegir(page, texto_buscar="Carga Virtual", texto_opcion="Carga Virtual")
            page.wait_for_timeout(1000)
            _diag(page, "23_tipo_producto_elegido")

            _abrir_select2(page, XPATH_BTN_PRODUCTO)
            _select2_elegir(page, texto_buscar="SV000", texto_opcion="SV000")
            page.wait_for_timeout(1000)
            _diag(page, "24_producto_elegido")

            page.locator(f'xpath={XPATH_BTN_VER_TRAZABILIDAD}').click(timeout=8000)

            # Sondeo: esperar a que aparezca "Se encontraron X materiales"
            # (hasta 30s — mismo criterio de "sondear, no adivinar tiempo
            # fijo" que ya usamos en el Bot 1).
            encontrado_texto = None
            for _ in range(60):
                page.wait_for_timeout(500)
                try:
                    candidato = page.get_by_text("Se encontraron", exact=False).first
                    if candidato.is_visible():
                        encontrado_texto = candidato.inner_text(timeout=1000)
                        break
                except Exception:
                    continue
            _diag(page, "25_resultado_trazabilidad")

            if encontrado_texto:
                print(f"🔎 {encontrado_texto.strip()}")
            else:
                print("⚠️ No apareció el texto 'Se encontraron X materiales' tras 30s — se intenta leer la tabla igual.")

            # ── Tabla de movimientos ─────────────────────────────────
            # AJUSTAR si no hay filas: no se confirmó el id real de esta
            # tabla — se prueba con el mismo id que usan otras pantallas
            # de ITEC (#tableToScroll) y, si no hay nada, se cae a
            # cualquier tabla visible en la página.
            filas = page.locator('#tableToScroll tbody tr')
            if filas.count() == 0:
                filas = page.locator('table tbody tr')
            total_filas = filas.count()
            print(f"📊 {total_filas} fila(s) encontradas en la tabla.")

            if total_filas == 0:
                _diag(page, "25b_sin_filas")
                html = page.content()
                Path(CARPETA_CAPTURAS / "25b_pagina_completa.html").write_text(html, encoding="utf-8")
                print("💾 HTML completo de la página guardado para diagnóstico (25b_pagina_completa.html).")
                print("ℹ️ Puede ser normal si hoy no hubo ningún movimiento — o puede que el selector de tabla esté mal.")

            totales = {}       # caminante_id -> suma
            no_matcheados = {}  # detalle -> suma (solo log)
            filas_hoy = 0

            for i in range(total_filas):
                fila = filas.nth(i)
                celdas = fila.locator('td')
                if celdas.count() < 6:
                    continue
                try:
                    cantidad_txt = celdas.nth(2).inner_text(timeout=2000).strip()
                    detalle_txt = celdas.nth(4).inner_text(timeout=2000).strip()
                    fecha_txt = celdas.nth(5).inner_text(timeout=2000).strip()
                except Exception:
                    continue

                try:
                    fecha_fila = datetime.strptime(fecha_txt.strip(), "%d/%m/%Y %H:%M:%S").date()
                except Exception:
                    continue
                if fecha_fila != hoy:
                    continue
                filas_hoy += 1

                cantidad = _parsear_numero(cantidad_txt)
                if cantidad is None:
                    continue

                caminante = mapa_nombres.get(detalle_txt.strip().lower())
                if caminante is None:
                    no_matcheados[detalle_txt] = no_matcheados.get(detalle_txt, 0) + cantidad
                    continue

                totales[caminante["id"]] = totales.get(caminante["id"], 0) + cantidad

            print(f"🔎 {filas_hoy} fila(s) de HOY encontradas en la tabla (de cualquier producto).")
            if no_matcheados:
                print("⚠️ Nombres en 'Detalle' que NO matchearon con ningún caminante activo (revisar manualmente):")
                for nombre, monto in no_matcheados.items():
                    print(f"   - {nombre!r}: ${monto:,.2f}")

            if not totales:
                print("ℹ️ No hay saldo entregado para registrar hoy.")
                return

            for caminante_id, monto in totales.items():
                nombre = next((c["nombre"] for c in caminantes if c["id"] == caminante_id), caminante_id)
                ok, err = guardar_entregado(caminante_id, monto)
                if ok:
                    print(f"💾 {nombre}: saldo entregado hoy = ${monto:,.2f}")
                else:
                    print(f"⚠️ No se pudo guardar el saldo entregado de {nombre}: {err}")

            print("✅ Listo.")

        except Exception as e:
            _diag(page, "99_error_general")
            print(f"❌ ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
