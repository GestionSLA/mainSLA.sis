"""
Bot Caminantes — Catálogo + Stock actual (ITEC WalkerStock)
=============================================================
Bot 1 de 3 del módulo Caminantes. Entra a https://itec.claro.com.ar/WalkerStock:
  1. Elige Backoffice "Clara Maonis" y Sucursal "49128 - U.S.B. S.R.L."
  2. Lee el LISTADO COMPLETO de caminantes directo del <select> nativo que
     Select2 envuelve (id="WalkerID") — más rápido y confiable que abrir
     el desplegable visual y leer cada <li> uno por uno; de paso, el
     `value` de cada <option> es el ID interno de ITEC para ese caminante,
     que vamos a necesitar más adelante para el bot que asigna SIMs.
  3. Para cada caminante: lo selecciona (dispara la carga de "Información
     del Vendedor" en #resume-panel), y lee "Total $" (Saldo) y "Lotes"
     (Sims x Lotes).
  4. Guarda/actualiza todo en Supabase, tabla `caminantes` — upsert por
     nombre, PISANDO saldo y sims_lotes en cada corrida (es una foto del
     momento, no algo que se acumule). NUNCA pisa `activo` de un
     caminante que ya existía (para no reactivar a alguien que se
     deshabilitó a mano en GestionSLA).

ITEC NO requiere self-hosted: tiene login propio (usuario+contraseña, sin
MFA), no pasa por el SSO de Microsoft/Azure AD — el Conditional Access que
sí obliga a self-hosted para NEXO/Webcom/PowerApps no le aplica acá.

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY   -> service role
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

URL_LOGIN = "https://itec.claro.com.ar/Home/Login?ReturnUrl=%2f"
URL_WALKER_STOCK = "https://itec.claro.com.ar/WalkerStock"

BACKOFFICE_TEXTO = "Clara Maonis"
SUCURSAL_CODIGO = "491280"

XPATH_USERNAME = '//*[@id="Username"]'
XPATH_PASSWORD = '//*[@id="Password"]'
XPATH_BTN_LOGIN = '/html/body/div/div/div/div/div[2]/form/div[5]/div/button'

XPATH_BTN_BACKOFFICE = '//*[@id="s2id_BackofficeID"]/a/span[2]/b'
XPATH_BTN_WAREHOUSE = '//*[@id="s2id_WarehouseID"]/a/span[2]/b'
XPATH_BTN_WALKER = '//*[@id="s2id_WalkerID"]/a/span[2]/b'
XPATH_INPUT_SALDO = '//*[@id="resume-panel"]/form/div[1]/div/input'
XPATH_INPUT_LOTES = '//*[@id="resume-panel"]/form/div[2]/div/input'

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


def _diag(page, etiqueta):
    try:
        print(f"🔎 [{etiqueta}] URL actual: {page.url}")
        page.screenshot(path=str(CARPETA_CAPTURAS / f"{etiqueta}.png"), full_page=True)
    except Exception as e:
        print(f"🔎 [{etiqueta}] No se pudo diagnosticar: {e}")


# ── Select2 sin depender de ids autogenerados (mismo criterio ya probado
# en bot_itec_cargar.py) ──────────────────────────────────────────────
def _abrir_select2(page, boton_xpath, timeout_ms=8000):
    page.locator(f'xpath={boton_xpath}').click(timeout=timeout_ms)
    page.wait_for_timeout(400)


def _select2_elegir(page, texto_buscar=None, texto_opcion=None, espera_ms=1200):
    """Con el combo YA ABIERTO: escribe en el buscador (si corresponde) y
    elige la opción por texto. Sin scoping extra — la versión "escopeada a
    .select2-drop-active" se probó y rompió Backoffice/Sucursal (esa clase
    no envuelve los resultados como se asumió); se vuelve a la versión
    simple, que sí está confirmada funcionando para esos dos combos."""
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


def _derivar_apellido_y_nombre_pila(nombre_itec):
    """ITEC devuelve 'Apellido, Nombre' (ej: 'Carballo, Susana'). Se separa
    por la primera coma; nombre_pila toma solo la PRIMERA palabra después
    de la coma (por si hubiera más de un nombre de pila), porque la
    planilla de cobranzas trae un solo nombre de pila suelto (ej:
    'susana'), no el nombre completo."""
    if "," in nombre_itec:
        apellido, resto = nombre_itec.split(",", 1)
    else:
        apellido, resto = nombre_itec, ""
    apellido = apellido.strip()
    primer_nombre = resto.strip().split(" ")[0] if resto.strip() else ""
    return apellido, primer_nombre.lower()


def _texto_seleccionado_select2(page, id_original):
    """Lee lo que el combo Select2 muestra actualmente como elegido — para
    confirmar en el LOG (no solo por screenshot) si la selección realmente
    prendió o si se quedó en el placeholder / eligió otra cosa."""
    try:
        return page.locator(f'#s2id_{id_original} .select2-chosen').inner_text(timeout=2000).strip()
    except Exception:
        return None


def _seleccionar_caminante_con_verificacion(page, nombre_itec, intentos=3):
    """Selecciona un caminante en el combo y CONFIRMA que el texto que
    quedó mostrado coincide con el pedido — un click que no aterriza bien
    puede no tirar ningún error y sin embargo no seleccionar nada real, y
    ahí ESPERAR no sirve de nada: el saldo nunca va a aparecer si nunca se
    seleccionó al caminante correcto. Si no coincide, reintenta (hasta
    `intentos` veces) antes de rendirse. Devuelve el texto confirmado, o
    None si nunca se pudo confirmar."""
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


def guardar_caminante(nombre_itec, saldo, sims_lotes):
    apellido, nombre_pila = _derivar_apellido_y_nombre_pila(nombre_itec)
    payload = {

        "nombre": nombre_itec,
        "apellido": apellido,
        "nombre_pila": nombre_pila,
        "saldo": saldo,
        "sims_lotes": sims_lotes,
        "actualizado_en": datetime.now(timezone.utc).isoformat(),
        # "activo" NO se manda acá a propósito: así el upsert nunca pisa el
        # valor que ya tenga guardado (por default true en la fila nueva).
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/caminantes?on_conflict=nombre",
        headers={**headers_supabase(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=payload, timeout=30,
    )
    if not r.ok:
        print(f"⚠️ No se pudo guardar {nombre_itec}: {r.status_code} {r.text[:300]}")
        return False
    return True


def _parsear_numero(texto):
    """Los inputs de ITEC pueden traer separador de miles ('104.000') —
    sacamos puntos y comas antes de convertir."""
    if texto is None:
        return None
    texto = str(texto).strip().replace(".", "").replace(",", ".")
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def main():
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
            # ── LOGIN ──────────────────────────────────────────────
            page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(f'xpath={XPATH_USERNAME}', timeout=30000)
            page.fill(f'xpath={XPATH_USERNAME}', usuario)
            page.fill(f'xpath={XPATH_PASSWORD}', password)
            page.locator(f'xpath={XPATH_BTN_LOGIN}').click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            _diag(page, "00_tras_login")

            if page.locator(f'xpath={XPATH_USERNAME}').count() > 0:
                raise RuntimeError("El login de ITEC no funcionó (usuario/contraseña).")

            # ── WalkerStock ────────────────────────────────────────
            page.goto(URL_WALKER_STOCK, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            _diag(page, "01_walker_stock")

            # Backoffice: "Clara Maonis"
            _abrir_select2(page, XPATH_BTN_BACKOFFICE)
            _select2_elegir(page, texto_buscar=BACKOFFICE_TEXTO, texto_opcion=BACKOFFICE_TEXTO)
            page.wait_for_timeout(1500)
            _diag(page, "02_backoffice_elegido")

            # Sucursal: única opción "49128 - U.S.B. S.R.L."
            _abrir_select2(page, XPATH_BTN_WAREHOUSE)
            _select2_elegir(page, texto_buscar=SUCURSAL_CODIGO, texto_opcion=SUCURSAL_CODIGO)
            page.wait_for_timeout(2000)  # tras elegir sucursal, ITEC recarga el combo de Caminante
            _diag(page, "03_sucursal_elegida")

            # ── Catálogo completo de caminantes ──────────────────────
            # AJUSTAR si el <select> real no tiene id="WalkerID" — revisar
            # el screenshot/HTML de este paso (03_sucursal_elegida) para
            # confirmar el id real si esto no encuentra nada.
            opciones = page.eval_on_selector_all(
                '#WalkerID option',
                "opts => opts.map(o => ({value: o.value, texto: o.textContent.trim()}))"
                        ".filter(o => o.value)"  # descarta el placeholder vacío
            )
            print(f"🚶 {len(opciones)} caminante(s) encontrados en el combo.")
            if not opciones:
                _diag(page, "03b_sin_opciones_walker")
                raise RuntimeError("El combo de Caminante no tiene opciones — revisar selección de Backoffice/Sucursal.")

            guardados, fallidos = 0, 0
            for i, opcion in enumerate(opciones):
                nombre_itec = opcion["texto"]
                print(f"— [{i+1}/{len(opciones)}] {nombre_itec}")

                # PRIMERO: confirmar que la selección realmente prendió (no
                # alcanza con que el click "no haya tirado error"). Si no se
                # puede confirmar tras varios intentos, se omite este
                # caminante — esperar más tiempo no serviría de nada, porque
                # nunca se seleccionó de verdad.
                confirmado = _seleccionar_caminante_con_verificacion(page, nombre_itec)
                if i < 2:
                    _diag(page, f"04_caminante_{i+1:02d}")

                if confirmado is None:
                    print(f"  ❌ No se pudo confirmar la selección de {nombre_itec} tras varios intentos — se omite.")
                    _diag(page, f"04_seleccion_fallida_{i+1}_{nombre_itec[:20]}")
                    fallidos += 1
                    continue
                print(f"  ✅ Selección confirmada: {confirmado!r}")

                # RECIÉN ACÁ, con la selección ya confirmada, tiene sentido
                # esperar a que "Información del Vendedor" termine de cargar
                # (sondeo en vez de tiempo fijo). Las capturas de la corrida
                # anterior mostraron el spinner de carga todavía girando a
                # los 8s — no era un problema de selección, solo hacía
                # falta más margen. Se sube el techo a 25s.
                saldo_txt = ""
                for _ in range(50):  # 50 x 500ms = 25s techo
                    page.wait_for_timeout(500)
                    try:
                        saldo_txt = page.locator(f'xpath={XPATH_INPUT_SALDO}').input_value(timeout=1500)
                    except Exception:
                        saldo_txt = ""
                    if saldo_txt.strip():
                        break

                if not saldo_txt.strip():
                    print(f"  ⚠️ Saldo siguió vacío tras 25s de sondeo para {nombre_itec}, aunque la selección estaba confirmada — revisar el HTML de resume-panel en la captura.")

                try:
                    lotes_txt = page.locator(f'xpath={XPATH_INPUT_LOTES}').input_value(timeout=5000)
                except Exception as e:
                    print(f"  ⚠️ No se pudo leer Lotes para {nombre_itec}: {e}")
                    _diag(page, f"04_error_{i+1}_{nombre_itec[:20]}")
                    fallidos += 1
                    continue

                saldo = _parsear_numero(saldo_txt)
                lotes = _parsear_numero(lotes_txt)
                print(f"  💰 Saldo: {saldo}  📦 Lotes: {lotes}")

                if guardar_caminante(nombre_itec, saldo, int(lotes) if lotes is not None else None):
                    guardados += 1
                else:
                    fallidos += 1

            print(f"✅ Listo: {guardados} caminante(s) guardados, {fallidos} fallido(s).")
            if fallidos:
                sys.exit(1)

        except Exception as e:
            _diag(page, "99_error_general")
            print(f"❌ ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
