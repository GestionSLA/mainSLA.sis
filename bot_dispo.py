"""
Bot Dispo — Captura lista de materiales disponibles desde portal SAP
Guarda en Supabase: configuracion.dispo_materiales + dispo_ultima_actualizacion

Flujo:
1. Login en SAP con credenciales guardadas en Supabase
2. Navegar formulario de consulta de disponibilidad
3. Capturar lista completa del dropdown (scroll incluido)
4. Parsear items "codigo - nombre"
5. Guardar en Supabase
"""

import asyncio, json, os, sys, re
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ── Configuración ─────────────────────────────────────────────────────────────
SAP_URL      = "https://flpnwc-d62f4ebf3.dispatcher.us2.hana.ondemand.com/sites/agentes#PedidoWeb-Display"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/") or "https://iebfyjbkmjuicrrbezbi.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_KEY_SECRET", "")

# XPaths de navegación
XP = {
    # Login
    "login_btn":    '//*[@id="headerLoginButton"]/span',
    "username":     '//*[@id="j_username"]',
    "password":     '//*[@id="j_password"]',
    "login_submit": '//*[@id="logOnFormSubmit"]/div',

    # Pestaña 1 — Tipo de pedido
    "tab1":         '//*[@id="application-PedidoWeb-Display-component---Main--Tab1-icon"]',
    "tipo_arrow":   '//*[@id="application-PedidoWeb-Display-component---Main--tipo-arrow"]',
    "tipo_bodega":  '//*[@id="__item35-content"]/div/div',
    "grupo_arrow":  '//*[@id="application-PedidoWeb-Display-component---Main--grupo-arrow"]',
    "grupo_cons":   '//*[@id="__item38-content"]/div/div',
    "oper_arrow":   '//*[@id="application-PedidoWeb-Display-component---Main--operatoria-arrow"]',
    "oper_bodega":  '//*[@id="__item43-content"]/div/div',

    # Pestaña 2 — Materiales
    "tab2":         '//*[@id="application-PedidoWeb-Display-component---Main--Tab2-icon"]',
    "mat_desde":    '//*[@id="application-PedidoWeb-Display-component---Main--materialdesde-inner"]',
    "mat_hasta":    '//*[@id="application-PedidoWeb-Display-component---Main--materialhasta-inner"]',
    "buscar_btn":   '//*[@id="application-PedidoWeb-Display-component---Main--boton1-content"]',
    "sel_todos":    '//*[@id="__button7-BDI-content"]',
    "consultar":    '//*[@id="__button5-BDI-content"]',

    # Pestaña destinatario
    "destino_arrow": '//*[@id="application-PedidoWeb-Display-component---Main--destino-arrow"]',
    "destino_sel":  '//*[@id="__item44-content"]/div/div',
    "verificar":    '//*[@id="application-PedidoWeb-Display-component---Main--verificar-BDI-content"]',

    # Dropdown materiales disponibles
    "dispo_arrow":  '//*[@id="application-PedidoWeb-Display-component---Main--cmbmaterial-application-PedidoWeb-Display-component---Main--__table2-0-arrow"]',
    "dispo_scroll": '//*[@id="application-PedidoWeb-Display-component---Main--cmbmaterial-application-PedidoWeb-Display-component---Main--__table2-0-popup-cont"]',
}

# ── Supabase helpers ──────────────────────────────────────────────────────────
async def sb_get_config():
    """Lee configuración SAP de Supabase."""
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=sap_user,sap_pass,rango_inicio,rango_fin"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
        return data[0] if data else {}

async def sb_guardar_dispo(materiales: list):
    """Guarda lista de materiales en configuracion.dispo_materiales."""
    import urllib.request
    ahora = datetime.now(tz=__import__('timezone', fromlist=['UTC']).UTC if False else __import__('datetime').timezone.utc).isoformat().replace('+00:00', 'Z')
    payload = json.dumps({
        "dispo_materiales":          json.dumps(materiales),
        "dispo_ultima_actualizacion": ahora,
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global",
        data=payload,
        method="PATCH",
        headers={
            "apikey":       SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer":       "return=minimal",
        }
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status

# ── Parsear item del dropdown ─────────────────────────────────────────────────
def parsear_item(texto: str) -> dict | None:
    """
    Convierte '000000000070016780 - MOT SGT1TXT2603-2 V' en
    {id, codigo, nombre, marca}
    """
    texto = texto.strip()
    if not texto or texto == "Seleccionar":
        return None
    m = re.match(r'^(\d{18})\s*-\s*(.+)$', texto)
    if not m:
        return None
    codigo = m.group(1).lstrip("0") or m.group(1)
    nombre = m.group(2).strip()
    # Inferir marca por prefijo del código de producto
    marca = ""
    prefijos = {"APP": "Apple", "SAM": "Samsung", "SMG": "Samsung", "MOT": "Motorola",
                "TCL": "TCL", "HUA": "Huawei", "LGE": "LG", "XIA": "Xiaomi",
                "NOK": "Nokia", "ZTE": "ZTE", "HOR": "Honor"}
    for p, m_ in prefijos.items():
        if nombre.upper().startswith(p):
            marca = m_
            break
    return {"id": m.group(1), "codigo": codigo, "nombre": nombre, "marca": marca}

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print(f"🕐 Inicio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("🔗 Leyendo configuración SAP desde Supabase...")

    cfg = await sb_get_config()
    sap_user = cfg.get("sap_user", "") or os.environ.get("SAP_USER", "")
    sap_pass = cfg.get("sap_pass", "") or os.environ.get("SAP_PASS", "")
    rango_ini = cfg.get("rango_inicio", "")
    rango_fin = cfg.get("rango_fin", "")

    if not sap_user or not sap_pass:
        print("❌ No se encontraron credenciales SAP en Supabase")
        sys.exit(1)

    print(f"✅ Usuario SAP: {sap_user}")
    print(f"📦 Rango materiales: {rango_ini or '(vacío)'} → {rango_fin or '(vacío)'}")

    materiales = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        # ── 1. Navegar al portal SAP ──────────────────────────────────────
        print("🌐 Navegando al portal SAP...")
        await page.goto(SAP_URL, wait_until="domcontentloaded", timeout=60000)
        print("⏳ Esperando página de login (5 segundos)...")
        await page.wait_for_timeout(5000)

        # ── 2. Login ──────────────────────────────────────────────────────
        print("🔐 Iniciando sesión...")
        try:
            # Intentar click en botón de login
            login_btn = page.locator(f"xpath={XP['login_btn']}")
            await login_btn.wait_for(timeout=15000)
            await login_btn.click()
            await page.wait_for_timeout(3000)
            print("   Click en botón login OK")
        except Exception as e:
            print(f"   Botón login no encontrado ({e})")
            print("   Buscando formulario de login directo...")

        # Completar credenciales (puede estar en modal o directo en página)
        try:
            user_field = page.locator(f"xpath={XP['username']}")
            await user_field.wait_for(timeout=8000)
            await user_field.fill(sap_user)
            await page.locator(f"xpath={XP['password']}").fill(sap_pass)
            await page.locator(f"xpath={XP['login_submit']}").click()
            print("   Credenciales enviadas, esperando login...")
            await page.wait_for_timeout(5000)
            print("✅ Login completado")
        except Exception as e:
            print(f"⚠️  Formulario login no encontrado: {e}")
            print("   Asumiendo sesión ya activa...")
            await page.wait_for_timeout(3000)

        # Esperar que cargue la aplicación principal
        print("⏳ Esperando que cargue la aplicación principal (8 segundos)...")
        await page.wait_for_timeout(8000)

        # ── 3. Click en Tab 1 ─────────────────────────────────────────────
        print("\n📋 Tab 1 — Configurando tipo de pedido...")
        print("   Esperando que el elemento Tab1 sea visible...")
        tab1 = page.locator(f"xpath={XP['tab1']}")
        await tab1.wait_for(timeout=30000)
        await tab1.click()
        await page.wait_for_timeout(3000)

        # Tipo → Bodega
        await page.locator(f"xpath={XP['tipo_arrow']}").click()
        await page.wait_for_timeout(1500)
        await page.get_by_role("option").filter(has_text="Bodega").first.click()
        await page.wait_for_timeout(1500)

        # Grupo → Consignación
        await page.locator(f"xpath={XP['grupo_arrow']}").click()
        await page.wait_for_timeout(1500)
        await page.get_by_role("option").filter(has_text="Consignación").first.click()
        await page.wait_for_timeout(1500)

        # Operatoria → BODEGA
        await page.locator(f"xpath={XP['oper_arrow']}").click()
        await page.wait_for_timeout(1500)
        await page.get_by_role("option").filter(has_text="BODEGA").first.click()
        await page.wait_for_timeout(1500)
        print("✅ Tipo de pedido configurado")

        # ── 4. Click en Tab 2 ─────────────────────────────────────────────
        print("\n📦 Tab 2 — Configurando rango de materiales...")
        await page.locator(f"xpath={XP['tab2']}").click()
        await page.wait_for_timeout(2000)

        if rango_ini:
            await page.locator(f"xpath={XP['mat_desde']}").fill(rango_ini)
        if rango_fin:
            await page.locator(f"xpath={XP['mat_hasta']}").fill(rango_fin)

        print("🔍 Buscando materiales... (puede demorar ~3 minutos)")
        await page.locator(f"xpath={XP['buscar_btn']}").click()
        await page.wait_for_timeout(180000)  # 3 minutos

        # Seleccionar todos — buscar por texto ya que el ID es dinámico
        print("☑️  Seleccionando todos los materiales...")
        try:
            await page.get_by_role("button").filter(has_text="Seleccionar todo").first.click(timeout=15000)
        except Exception:
            try:
                await page.get_by_role("button").filter(has_text="Select All").first.click(timeout=10000)
            except Exception:
                await page.locator(f"xpath={XP['sel_todos']}").click(timeout=10000)

        # Consultar disponibilidad
        print("📊 Consultando disponibilidad...")
        await page.wait_for_timeout(1500)
        try:
            await page.get_by_role("button").filter(has_text="Realizar Consulta").first.click(timeout=10000)
        except Exception:
            await page.locator(f"xpath={XP['consultar']}").click(timeout=10000)
        await page.wait_for_timeout(5000)

        # ── 5. Destinatario ───────────────────────────────────────────────
        print("\n🏢 Seleccionando destinatario...")
        await page.locator(f"xpath={XP['destino_arrow']}").click()
        await page.wait_for_timeout(1500)
        try:
            await page.get_by_role("option").filter(has_text="U.S.B. S.R.L.").first.click(timeout=10000)
        except Exception:
            await page.get_by_role("option").first.click(timeout=10000)
        await page.wait_for_timeout(1500)

        # Verificar — SAP Fiori bloquea clicks normales, intentar múltiples métodos
        print("✅ Verificando disponibilidad... (35 segundos)")
        try:
            # Método 1: dispatchEvent
            clicked = await page.evaluate("""() => {
                const btn = document.querySelector('[id*="verificar"]');
                if (!btn) return 'no encontrado';
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return 'dispatched';
            }""")
            print(f"   Verificar JS: {clicked}")
            await page.wait_for_timeout(2000)

            # Método 2: focus + enter
            await page.evaluate("""() => {
                const btn = document.querySelector('[id*="verificar"]');
                if (btn) { btn.focus(); btn.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true})); }
            }""")
            await page.wait_for_timeout(2000)

            # Método 3: click con offset para evitar overlay
            verificar = page.locator('[id*="verificar"]').first
            await verificar.scroll_into_view_if_needed()
            await page.wait_for_timeout(500)
            box = await verificar.bounding_box()
            if box:
                await page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
                print(f"   Mouse click en ({box['x']:.0f}, {box['y']:.0f})")
        except Exception as e:
            print(f"   Error: {e}")

        print("   Esperando respuesta de SAP (35 segundos)...")
        await page.wait_for_timeout(35000)

        # ── 6. Capturar dropdown de materiales ────────────────────────────
        print("\n📋 Abriendo dropdown de materiales disponibles...")
        try:
            await page.locator(f"xpath={XP['dispo_arrow']}").click(timeout=15000)
        except Exception:
            await page.evaluate("""() => {
                const arrow = document.querySelector('[id*="cmbmaterial"][id*="arrow"]');
                if (arrow) arrow.click();
            }""")
        await page.wait_for_timeout(3000)

        # Scroll y captura de todos los items
        print("🔄 Capturando lista completa...")
        scroll_container = page.locator(f"xpath={XP['dispo_scroll']}")

        items_vistos = set()
        sin_cambios  = 0

        while sin_cambios < 3:
            # Extraer items visibles
            textos = await page.evaluate("""() => {
                const cont = document.evaluate(
                    '//*[@id="application-PedidoWeb-Display-component---Main--cmbmaterial-application-PedidoWeb-Display-component---Main--__table2-0-popup-cont"]',
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (!cont) return [];
                return Array.from(cont.querySelectorAll('li, [role="option"], .sapMSLITitleOnly'))
                    .map(el => (el.innerText || el.textContent || '').trim())
                    .filter(t => t.length > 5);
            }""")

            antes = len(items_vistos)
            for t in textos:
                items_vistos.add(t)

            if len(items_vistos) == antes:
                sin_cambios += 1
            else:
                sin_cambios = 0
                print(f"   {len(items_vistos)} items capturados...")

            # Scroll hacia abajo
            await page.evaluate("""() => {
                const cont = document.evaluate(
                    '//*[@id="application-PedidoWeb-Display-component---Main--cmbmaterial-application-PedidoWeb-Display-component---Main--__table2-0-popup-cont"]',
                    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                ).singleNodeValue;
                if (cont) cont.scrollTop += 300;
            }""")
            await page.wait_for_timeout(500)

        # Parsear items
        for texto in items_vistos:
            item = parsear_item(texto)
            if item:
                materiales.append(item)

        await browser.close()

    # Ordenar por código
    materiales.sort(key=lambda x: x["codigo"])
    materiales = list({m["id"]: m for m in materiales}.values())  # deduplicar

    print(f"\n✅ {len(materiales)} materiales disponibles capturados")

    if not materiales:
        print("⚠️  Lista vacía — no se guarda en Supabase")
        sys.exit(1)

    # Guardar en Supabase
    print("💾 Guardando en Supabase...")
    await sb_guardar_dispo(materiales)
    print(f"✅ Guardado en Supabase — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Guardar JSON local para debug
    out = Path("data")
    out.mkdir(exist_ok=True)
    (out / "dispo.json").write_text(
        json.dumps(materiales, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"💾 {len(materiales)} materiales → data/dispo.json")

if __name__ == "__main__":
    asyncio.run(main())
