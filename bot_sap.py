import os
import json
import time
import traceback
from datetime import datetime, timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.remote_connection import RemoteConnection

# El cliente HTTP de Selenium (Python -> chromedriver) tiene un timeout fijo
# de 120s por defecto, INDEPENDIENTE del page_load_timeout de Chrome. Si
# driver.get() tarda más que esto, urllib3 lanza ReadTimeoutError antes de
# que nuestro page_load_timeout pueda actuar. Lo subimos a 300s.
# (La API cambió entre versiones de Selenium, probamos ambas formas).
try:
    RemoteConnection.client_config.timeout = 300
except Exception:
    try:
        RemoteConnection.set_timeout(300)
    except Exception:
        pass

# ──────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────
# Prioridad de configuración:
#   1) data/config.json -> escrito automáticamente por el sistema
#      GestiClaro (módulo "Configuración Sistemas AMX") cada vez que el
#      usuario cambia usuario/contraseña/rangos ahí.
#   2) GitHub Secrets (SAP_USER, SAP_PASS, SAP_RANGO_INICIO, SAP_RANGO_FIN)
#   3) Valores fijos de respaldo (último recurso).
CONFIG_JSON = os.path.join("data", "config.json")


def cargar_config():
    config = {
        "sap_usuario": os.getenv("SAP_USER", "AGED049128"),
        "sap_password": os.getenv("SAP_PASS", "Lunes18/"),
        "rango_inicio": os.getenv("SAP_RANGO_INICIO", ""),
        "rango_fin": os.getenv("SAP_RANGO_FIN", ""),
    }
    if os.path.exists(CONFIG_JSON):
        try:
            with open(CONFIG_JSON, "r", encoding="utf-8") as f:
                datos = json.load(f)
            if datos.get("sap_user"):
                config["sap_usuario"] = datos["sap_user"]
            if datos.get("sap_pass"):
                config["sap_password"] = datos["sap_pass"]
            # Los rangos pueden ser legítimamente vacíos, así que los
            # tomamos del JSON si la clave existe, sin importar el valor.
            if "rango_inicio" in datos:
                config["rango_inicio"] = datos["rango_inicio"]
            if "rango_fin" in datos:
                config["rango_fin"] = datos["rango_fin"]
            print(f"[CONFIG] Cargada desde {CONFIG_JSON} (actualizado: {datos.get('actualizado', '?')})")
        except Exception as e:
            print(f"[CONFIG] No se pudo leer {CONFIG_JSON}, se usan valores por defecto/Secrets: {e}")
    else:
        print(f"[CONFIG] {CONFIG_JSON} no existe todavía, se usan Secrets/valores por defecto")
    return config


_CFG = cargar_config()
USUARIO = _CFG["sap_usuario"]
PASSWORD = _CFG["sap_password"]
RANGO_INICIO = _CFG["rango_inicio"]
RANGO_FIN = _CFG["rango_fin"]

URL_HOME = "https://flpnwc-d62f4ebf3.dispatcher.us2.hana.ondemand.com/sites/agentes#home-Display"
URL_STOCK = "https://flpnwc-d62f4ebf3.dispatcher.us2.hana.ondemand.com/sites/agentes#stock_antiguedad-Display"

SALIDA_JSON = os.path.join("data", "stock_sap.json")
SALIDA_LOG = os.path.join("data", "ultimo_log.txt")

COLUMNAS_SAP = [
    "Material", "Serial", "Texto", "Centro", "Almacen", "Movimiento",
    "Mov_texto", "Modelo", "Origen", "Precio", "Dias_Antiguedad",
    "Semaforo", "Fecha_Antiguedad", "Nro_Pedido"
]
COLUMNAS_RELEVANTES = {
    "Material", "Serial", "Texto", "Centro", "Precio",
    "Dias_Antiguedad", "Semaforo", "Fecha_Antiguedad", "Nro_Pedido"
}

LOG_LINES = []


def log(msg):
    print(msg)
    LOG_LINES.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def guardar_log():
    os.makedirs("data", exist_ok=True)
    with open(SALIDA_LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES))


def click_resiliente(driver, by, selector, timeout=15, intentos=3, descripcion=""):
    """Espera que un elemento sea clickeable y le hace click, reintentando
    si encuentra errores transitorios (overlay tapando, elemento stale, etc)."""
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, selector))
            )
            el.click()
            return el
        except Exception as e:
            ultimo_error = e
            log(f"  Click en '{descripcion or selector}' falló (intento {intento}/{intentos}): {type(e).__name__}")
            time.sleep(2)
    raise Exception(f"No se pudo hacer click en '{descripcion or selector}' tras {intentos} intentos: {ultimo_error}")


def esperar_resultados_tabla(driver, nombre_consulta="consulta", timeout=40):
    """
    Después de hacer click en 'Consultar', SAP UI5 dispara una llamada al
    backend (OData) que puede tardar varios segundos. La tabla aparece en
    el DOM casi inmediatamente pero solo con la fila de encabezado; los
    datos reales se insertan después. Esperamos activamente hasta detectar
    al menos una fila con datos (no solo el encabezado), o hasta el timeout.
    """
    fin = time.time() + timeout
    ultimo_conteo_filas = 0
    while time.time() < fin:
        try:
            tabla_body = driver.find_element(
                By.XPATH,
                "//table[contains(@class, 'sapUiTableCtrl')]//tbody | "
                "//table[contains(@class, 'sapMListTbl')]//tbody"
            )
            filas = tabla_body.find_elements(By.TAG_NAME, "tr")
            ultimo_conteo_filas = len(filas)
            if len(filas) > 1:
                # Verificar que al menos la segunda fila tenga texto real
                # (no solo la cabecera repetida ni filas vacías).
                celdas = filas[1].find_elements(By.TAG_NAME, "td")
                textos = [c.text.strip() for c in celdas]
                if any(textos):
                    log(f"  '{nombre_consulta}': datos detectados tras "
                        f"{timeout - (fin - time.time()):.1f}s ({len(filas)} filas)")
                    return True
        except Exception:
            pass
        time.sleep(1)
    log(f"  '{nombre_consulta}': timeout esperando datos ({timeout}s), "
        f"última lectura: {ultimo_conteo_filas} fila(s) en el DOM")
    return False


def obtener_encabezados_visibles(driver):
    """
    Lee los nombres de columna actualmente visibles en el header de la
    tabla SAP UI5, en orden. Esto nos permite saber a qué campo corresponde
    cada celda visible en la posición de scroll horizontal actual.
    """
    xpath_headers = (
        "//table[contains(@class,'sapUiTableColHdrTbl')]//th | "
        "//*[@id='__xmlview4--TabReport-tableCCnt']//thead//th | "
        "//div[contains(@class,'sapUiTableColHdrCnt')]//div[contains(@class,'sapUiTableHeaderDataCell')]"
    )
    try:
        headers_el = driver.find_elements(By.XPATH, xpath_headers)
        nombres = [h.text.strip() for h in headers_el if h.text.strip()]
        return nombres
    except Exception:
        return []


def mapear_columna_sap(nombre_visible):
    """Normaliza el nombre de columna mostrado en pantalla (ej. 'Días
    Antiguedad') a la clave interna usada en COLUMNAS_SAP (ej.
    'Dias_Antiguedad')."""
    n = (nombre_visible or "").strip().lower()
    equivalencias = {
        "material": "Material",
        "serial": "Serial",
        "texto": "Texto",
        "centro": "Centro",
        "almacén": "Almacen",
        "almacen": "Almacen",
        "movimiento": "Movimiento",
        "mov. texto": "Mov_texto",
        "mov texto": "Mov_texto",
        "modelo": "Modelo",
        "origen": "Origen",
        "precio": "Precio",
        "días antiguedad": "Dias_Antiguedad",
        "dias antiguedad": "Dias_Antiguedad",
        "días antigüedad": "Dias_Antiguedad",
        "dias antigüedad": "Dias_Antiguedad",
        "semáforo": "Semaforo",
        "semaforo": "Semaforo",
        "fecha antiguedad": "Fecha_Antiguedad",
        "fecha antigüedad": "Fecha_Antiguedad",
        "nro. pedido": "Nro_Pedido",
        "nro pedido": "Nro_Pedido",
    }
    return equivalencias.get(n)


def leer_filas_visibles(driver, nombre_consulta):
    """
    Lee las filas y columnas actualmente visibles en la tabla (según la
    posición de scroll actual) y devuelve una lista de dicts parciales,
    usando 'Serial' como clave para poder fusionarlas después con lo leído
    en otras posiciones de scroll.
    """
    xpath_contenedor = '//*[@id="__xmlview4--TabReport-tableCCnt"]'
    xpath_tabla_fallback = (
        "//table[contains(@class, 'sapUiTableCtrl') and not(contains(@class,'sapUiTableColHdrCnt'))]//tbody"
    )
    try:
        try:
            contenedor = driver.find_element(By.XPATH, xpath_contenedor)
            tabla_body = contenedor.find_element(By.TAG_NAME, "tbody")
        except Exception:
            tabla_body = driver.find_element(By.XPATH, xpath_tabla_fallback)
    except Exception:
        return []

    headers_visibles = obtener_encabezados_visibles(driver)
    columnas_mapeadas = [mapear_columna_sap(h) for h in headers_visibles]

    filas = tabla_body.find_elements(By.TAG_NAME, "tr")
    parciales = []

    for fila in filas:
        celdas = fila.find_elements(By.TAG_NAME, "td")
        if not celdas:
            continue
        valores = [c.text.strip() for c in celdas]
        if not any(valores):
            continue

        registro = {}
        if columnas_mapeadas and len(columnas_mapeadas) == len(valores):
            # Emparejamos por nombre de columna leído del header visible
            for col, val in zip(columnas_mapeadas, valores):
                if col and col in COLUMNAS_RELEVANTES:
                    registro[col] = val
        else:
            # Fallback posicional si no pudimos leer headers (orden fijo conocido)
            for i, val in enumerate(valores):
                if i < len(COLUMNAS_SAP) and COLUMNAS_SAP[i] in COLUMNAS_RELEVANTES:
                    registro[COLUMNAS_SAP[i]] = val

        if registro.get("Serial") and len(registro.get("Serial", "")) > 5:
            parciales.append(registro)

    return parciales


def leer_columnas_ocultas(driver, nombre_consulta):
    """
    Lee las 4 columnas ocultas directamente por XPath de celda.
    Las columnas tienen IDs fijos: col10=Dias, col12=Fecha, col13=NroPedido.
    Devuelve dict {serial: {Dias_Antiguedad, Fecha_Antiguedad, Nro_Pedido}}
    Si no encuentra col9 (Precio), la fila no existe y se omite.
    El ID del view puede ser __xmlview3 o __xmlview4 segun el contexto.
    """
    resultado = {}

    # Detectar prefijo del view (puede variar entre __xmlview3 y __xmlview4)
    prefijo = None
    for px in ["__xmlview3--TabReport", "__xmlview4--TabReport"]:
        el = driver.execute_script(
            f"return document.getElementById('{px}-rows-row0-col9');"
        )
        if el:
            prefijo = px
            break

    if not prefijo:
        log(f"  '{nombre_consulta}': no se encontro col9 (tabla vacia o prefijo desconocido)")
        return resultado

    log(f"  '{nombre_consulta}': prefijo detectado = {prefijo}")

    # Hacer click en col9 (Precio) y presionar END para exponer columnas ocultas
    try:
        col9 = driver.find_element(By.ID, f"{prefijo}-rows-row0-col9")
        driver.execute_script("arguments[0].click();", col9)
        time.sleep(0.3)
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).send_keys(Keys.END).perform()
        time.sleep(0.8)
        log(f"  '{nombre_consulta}': click col9 + END ejecutado")
    except Exception as e:
        log(f"  '{nombre_consulta}': error en click col9 + END: {e}")
        return resultado

    # Leer fila por fila usando los XPaths de col10, col12, col13
    # y el serial de col1 para identificar la fila
    fila = 0
    while True:
        try:
            # Serial esta en col1
            el_serial = driver.execute_script(
                f"return document.getElementById('{prefijo}-rows-row{fila}-col1');"
            )
            if not el_serial:
                break  # no hay mas filas visibles

            serial = (el_serial.get_attribute("textContent") or "").strip()
            if not serial or len(serial) < 5:
                fila += 1
                continue

            dias = ""
            fecha = ""
            nro_pedido = ""

            el_dias = driver.execute_script(
                f"return document.getElementById('{prefijo}-rows-row{fila}-col10');"
            )
            if el_dias:
                dias = (el_dias.get_attribute("textContent") or "").strip()

            el_fecha = driver.execute_script(
                f"return document.getElementById('{prefijo}-rows-row{fila}-col12');"
            )
            if el_fecha:
                fecha = (el_fecha.get_attribute("textContent") or "").strip()

            el_pedido = driver.execute_script(
                f"return document.getElementById('{prefijo}-rows-row{fila}-col13');"
            )
            if el_pedido:
                nro_pedido = (el_pedido.get_attribute("textContent") or "").strip()

            resultado[serial] = {
                "Dias_Antiguedad": dias,
                "Fecha_Antiguedad": fecha,
                "Nro_Pedido": nro_pedido,
            }
            fila += 1

        except Exception as e:
            log(f"  '{nombre_consulta}': error leyendo fila {fila}: {e}")
            break

    log(f"  '{nombre_consulta}': columnas ocultas leidas para {len(resultado)} seriales")
    return resultado


def scroll_vertical(driver, posicion):
    """Mueve la scrollbar vertical de la tabla a una posición absoluta (px)."""
    try:
        driver.execute_script("""
            const sb = document.getElementById('__xmlview4--TabReport-vsb');
            if (sb) { sb.scrollTop = arguments[0]; sb.dispatchEvent(new Event('scroll', {bubbles:true})); }
        """, posicion)
        return True
    except Exception:
        return False


def scroll_horizontal(driver, posicion):
    """
    Mueve el scroll horizontal de la tabla. SAP UI5 grid table sincroniza
    varias capas (header, body, scrollbar visual). Scrolleamos directamente
    el contenedor de filas de datos (tableCCnt) que es el que controla qué
    columnas son visibles en el DOM, y luego disparamos el evento en el hsb
    para que SAP sincronice el header con las celdas.
    """
    try:
        driver.execute_script("""
            const pos = arguments[0];
            // 1. Scrollear el contenedor principal de datos
            const cnt = document.getElementById('__xmlview4--TabReport-tableCCnt');
            if (cnt) {
                cnt.scrollLeft = pos;
                cnt.dispatchEvent(new Event('scroll', {bubbles: true}));
            }
            // 2. Sincronizar la scrollbar visual horizontal (hsb)
            const hsb = document.getElementById('__xmlview4--TabReport-hsb');
            if (hsb) {
                hsb.scrollLeft = pos;
                hsb.dispatchEvent(new Event('scroll', {bubbles: true}));
            }
            // 3. Sincronizar el header de columnas para que coincida
            const hdr = document.getElementById('__xmlview4--TabReport-sapUiTableColHdrScr');
            if (hdr) {
                hdr.scrollLeft = pos;
            }
        """, posicion)
        return True
    except Exception as e:
        log(f"  Error en scroll_horizontal({posicion}): {e}")
        return False


def obtener_metricas_scroll_horizontal(driver):
    """
    Lee el ancho scrolleable del contenedor de datos de la tabla (tableCCnt),
    que es más confiable que leer el hsb directamente.
    """
    try:
        return driver.execute_script("""
            const cnt = document.getElementById('__xmlview4--TabReport-tableCCnt');
            if (!cnt) return null;
            return [cnt.scrollLeft, cnt.scrollWidth - cnt.clientWidth, cnt.clientWidth];
        """)
    except Exception:
        return None


def obtener_metricas_scroll(driver, eje):
    """eje: 'vsb' (vertical) o 'hsb' (horizontal). Devuelve (scrollActual, scrollMax, tamañoVisible)."""
    try:
        return driver.execute_script("""
            const sb = document.getElementById(arguments[0]);
            if (!sb) return null;
            const prop = arguments[0].endsWith('hsb') ? 'scrollLeft' : 'scrollTop';
            const max = arguments[0].endsWith('hsb')
                ? (sb.scrollWidth - sb.clientWidth)
                : (sb.scrollHeight - sb.clientHeight);
            const visible = arguments[0].endsWith('hsb') ? sb.clientWidth : sb.clientHeight;
            return [sb[prop], max, visible];
        """, f"__xmlview4--TabReport-{eje}")
    except Exception:
        return None


def extraer_datos_tabla(driver, nombre_consulta="consulta", max_pasos_v=60, max_pasos_h=15):
    """
    Extrae TODOS los datos de la tabla SAP recorriendo ambas scrollbars
    (vertical y horizontal), ya que la tabla es virtualizada: el DOM solo
    contiene las filas/columnas actualmente visibles en pantalla.

    Estrategia:
      1. Por cada posición vertical (de a "páginas" de filas visibles):
         2. Por cada posición horizontal (de a "páginas" de columnas visibles):
            - Leer filas/columnas visibles, emparejando por nombre de columna.
            - Fusionar con lo ya leído de esa fila (mismo Serial) en otras
              posiciones horizontales.
      3. Avanzar verticalmente hasta cubrir todo el scrollHeight.
    """
    os.makedirs("data", exist_ok=True)

    # Guardamos un HTML de referencia para diagnóstico (estado inicial)
    try:
        contenedor = driver.find_element(By.XPATH, '//*[@id="__xmlview4--TabReport-tableCCnt"]')
        with open(f"data/debug_tabla_{nombre_consulta}.html", "w", encoding="utf-8") as f:
            f.write(contenedor.get_attribute("outerHTML"))
    except Exception as e:
        log(f"  No se pudo guardar HTML de depuración: {e}")

    metr_v = obtener_metricas_scroll(driver, "vsb")

    # Bajar el scroll de la PÁGINA hasta el final (xpath __page2-cont) para
    # que la scrollbar horizontal de la tabla quede visible en pantalla — sin
    # esto el hsb no es accesible y las columnas de la derecha (Dias_Antiguedad,
    # Semaforo, Fecha_Antiguedad, Nro_Pedido) nunca se renderizan en el DOM.
    try:
        driver.execute_script("""
            const pg = document.getElementById('__page2-cont');
            if (pg) {
                pg.scrollTop = pg.scrollHeight;
                pg.dispatchEvent(new Event('scroll', {bubbles: true}));
            } else {
                window.scrollTo(0, document.body.scrollHeight);
            }
        """)
        time.sleep(0.8)
        log(f"  '{nombre_consulta}': scroll de página bajado al fondo para exponer hsb")
    except Exception as e:
        log(f"  '{nombre_consulta}': no se pudo bajar scroll de página: {e}")

    metr_h = obtener_metricas_scroll_horizontal(driver)
    log(f"  '{nombre_consulta}': métricas hsb (tableCCnt) = {metr_h}")

    if not metr_v or metr_v[1] is None:
        log(f"  '{nombre_consulta}': no se detectó scrollbar vertical, se lee una sola vez")
        filas_por_serial = {}
        for reg in leer_filas_visibles(driver, nombre_consulta):
            filas_por_serial.setdefault(reg["Serial"], {}).update(reg)
        resultados = list(filas_por_serial.values())
        log(f"  '{nombre_consulta}' -> {len(resultados)} registro(s) (sin scroll)")
        return resultados

    scroll_max_v, paso_v = metr_v[1], max(metr_v[2] or 300, 100)
    scroll_max_h, paso_h = (metr_h[1] if metr_h else 0), max((metr_h[2] if metr_h else 300), 100)

    log(f"  '{nombre_consulta}': scroll vertical max={scroll_max_v}px (paso~{paso_v}px), "
        f"horizontal max={scroll_max_h}px (paso~{paso_h}px)")

    filas_por_serial = {}
    pos_v = 0
    paso_count_v = 0

    while True:
        scroll_vertical(driver, pos_v)
        time.sleep(0.6)

        # Lectura 1: columnas izquierdas visibles (Material, Serial, Texto,
        # Centro, Almacen, Movimiento, Mov_texto, Modelo, Origen, Precio)
        for reg in leer_filas_visibles(driver, nombre_consulta):
            serial = reg.get("Serial")
            if not serial:
                continue
            filas_por_serial.setdefault(serial, {}).update(reg)

        # Lectura 2: columnas ocultas (Dias_Antiguedad col10, Fecha_Antiguedad
        # col12, Nro_Pedido col13) leídas directamente por ID de celda.
        # No necesita scroll — lee el DOM directamente por XPath de celda.
        ocultas = leer_columnas_ocultas(driver, nombre_consulta)
        for serial, datos in ocultas.items():
            filas_por_serial.setdefault(serial, {}).update(datos)

        if pos_v >= scroll_max_v or paso_count_v >= max_pasos_v:
            break
        pos_v = min(pos_v + paso_v, scroll_max_v)
        paso_count_v += 1

    resultados = list(filas_por_serial.values())
    completos = sum(1 for r in resultados if len(r) >= 6)
    log(f"  '{nombre_consulta}' -> {len(resultados)} registro(s) únicos por Serial "
        f"({completos} con 6+ campos completos), tras {paso_count_v + 1} paso(s) verticales")

    return resultados


def guardar_resultado(data, status, mensaje):
    os.makedirs("data", exist_ok=True)
    salida = {
        "status": status,
        "mensaje": mensaje,
        "actualizado": datetime.now(timezone.utc).isoformat(),
        "cantidad": len(data),
        "data": data,
    }
    with open(SALIDA_JSON, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)
    log(f"Guardado {SALIDA_JSON} ({len(data)} registros, status={status})")


def cargar_pagina(driver, url, timeout=180, reintentos=2):
    """
    Navega a una URL con timeout extendido. Si la página tarda demasiado
    en disparar el evento 'load' (común en SPAs pesadas de SAP), detiene
    la carga con window.stop() y continúa: el DOM suele estar usable
    igual aunque la página "siga cargando" recursos secundarios.
    """
    driver.set_page_load_timeout(timeout)
    for intento in range(1, reintentos + 1):
        try:
            log(f"Cargando {url} (intento {intento}/{reintentos}, timeout={timeout}s)...")
            driver.get(url)
            return True
        except (TimeoutException, Exception) as e:
            log(f"Error/timeout cargando la página (intento {intento}): {type(e).__name__}: {e}")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            # Verificar si al menos algo se cargó
            try:
                body_len = len(driver.execute_script("return document.body ? document.body.innerHTML.length : 0;"))
            except Exception:
                body_len = 0
            log(f"Contenido cargado tras detener: {body_len} caracteres en <body>")
            if body_len and body_len > 500:
                return True
            if intento == reintentos:
                log("Se alcanzó el máximo de reintentos. Se continúa con lo que haya cargado.")
                return False
            time.sleep(5)
    return False


def main():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=2560,1440")

    driver = webdriver.Chrome(options=chrome_options)
    registros_stock_actual = []
    registros_transito = []

    try:
        log("Abriendo portal SAP Fiori de Claro...")
        cargar_pagina(driver, URL_HOME, timeout=180, reintentos=2)
        time.sleep(12)

        log("Paso 0: Verificando si requiere desplegar login corporativo...")
        try:
            boton_desplegar = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//*[@id="headerLoginButton"]/span | //*[@id="headerLoginButton"]')
                )
            )
            boton_desplegar.click()
            log("Botón superior encontrado y presionado.")
            time.sleep(4)
        except Exception:
            log("El botón superior no respondió. Buscando formulario directamente...")

        os.makedirs("data", exist_ok=True)
        driver.save_screenshot("data/pre_fill.png")

        log("Paso 1: Escribiendo credenciales e ingresando...")

        driver.switch_to.default_content()
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        if len(iframes) > 0:
            driver.switch_to.frame(iframes[0])

        # Esperar a que el campo sea VISIBLE Y CLICKEABLE (no solo que exista
        # en el DOM — puede estar ahí pero el iframe todavía no terminó de
        # renderizar el formulario completo).
        log("Esperando que el campo j_username esté visible y clickeable...")
        campo_user = WebDriverWait(driver, 60).until(
            EC.element_to_be_clickable((By.ID, "j_username"))
        )
        log("Campo j_username listo. Esperando 2s adicionales de seguridad...")
        time.sleep(2)

        os.makedirs("data", exist_ok=True)
        driver.save_screenshot("data/pre_fill.png")

        # Inyectar con el setter nativo del prototipo (funciona aunque el
        # framework sobrescriba la propiedad 'value', que es lo que hace SAP IAS).
        def inyectar(element_id, valor):
            return driver.execute_script("""
                const el = document.getElementById(arguments[0]);
                if (!el) return null;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, arguments[1]);
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur',   { bubbles: true }));
                return el.value;
            """, element_id, valor)

        log("Inyectando usuario...")
        val_user = inyectar("j_username", USUARIO)
        log(f"  -> j_username.value = '{val_user}'")

        log("Inyectando contraseña...")
        val_pass = inyectar("j_password", PASSWORD)
        log(f"  -> j_password.value longitud = {len(val_pass or '')}")

        time.sleep(2)
        driver.save_screenshot("data/filled.png")

        if not val_user:
            log("ADVERTENCIA: el campo de usuario sigue vacío tras inyección.")

        log("Presionando botón de ingreso 'Log On'...")
        time.sleep(1)

        try:
            boton_submit = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "logOnFormSubmit"))
            )
            driver.execute_script("arguments[0].click();", boton_submit)
        except Exception:
            try:
                boton_texto = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(text(), 'Log On')] | //input[@value='Log On']")
                    )
                )
                driver.execute_script("arguments[0].click();", boton_texto)
            except Exception:
                boton_clase = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CLASS_NAME, "comSapIdpIdpButtons"))
                )
                driver.execute_script("arguments[0].click();", boton_clase)

        log("Procesando login...")
        driver.switch_to.default_content()
        time.sleep(12)

        driver.save_screenshot("data/post_login.png")

        log("Navegando directo al módulo de stock por antigüedad (URL directa)...")
        cargar_pagina(driver, URL_STOCK, timeout=180, reintentos=2)
        time.sleep(22)
        driver.save_screenshot("data/post_stock_nav.png")

        xpath_btn_consultar = '//*[@id="__xmlview4--button2-BDI-content"]'
        xpath_reabrir_filtros = '//*[@id="__xmlview4--panelSel-CollapsedImg-img"]'

        log("Consultando Stock Disponible Principal...")
        campo_inicio = WebDriverWait(driver, 25).until(
            EC.element_to_be_clickable((By.XPATH, '//*[@id="__xmlview4--input0-inner"]'))
        )
        campo_inicio.clear()
        campo_inicio.send_keys(RANGO_INICIO)
        campo_fin = driver.find_element(By.XPATH, '//*[@id="__xmlview4--input1-inner"]')
        campo_fin.clear()
        campo_fin.send_keys(RANGO_FIN)
        click_resiliente(driver, By.XPATH, xpath_btn_consultar, descripcion="botón consultar (principal)")
        esperar_resultados_tabla(driver, "stock_principal", timeout=40)
        driver.save_screenshot("data/resultado_stock_principal.png")
        registros_principal = extraer_datos_tabla(driver, "stock_principal")
        registros_stock_actual.extend(registros_principal)
        log(f"Stock principal: {len(registros_stock_actual)} registros")

        log("Consultando Depósito de Reingreso...")
        time.sleep(3)  # dejar que la UI se asiente tras la consulta anterior
        click_resiliente(driver, By.XPATH, xpath_reabrir_filtros, descripcion="reabrir filtros (reingreso)")
        time.sleep(1)
        click_resiliente(driver, By.XPATH, '//*[@id="__xmlview4--rdb5-label-bdi"]', descripcion="radio reingreso")
        click_resiliente(driver, By.XPATH, xpath_btn_consultar, descripcion="botón consultar (reingreso)")
        esperar_resultados_tabla(driver, "deposito_reingreso", timeout=40)
        driver.save_screenshot("data/resultado_reingreso.png")
        registros_reingreso = extraer_datos_tabla(driver, "deposito_reingreso")
        registros_stock_actual.extend(registros_reingreso)
        log(f"Stock total acumulado: {len(registros_stock_actual)} registros")

        log("Consultando Stock en Tránsito...")
        time.sleep(3)
        click_resiliente(driver, By.XPATH, xpath_reabrir_filtros, descripcion="reabrir filtros (tránsito)")
        time.sleep(1)
        click_resiliente(driver, By.XPATH, '//*[@id="__xmlview4--rdb4-label-bdi"]', descripcion="radio tránsito 1")
        click_resiliente(driver, By.XPATH, '//*[@id="__xmlview4--rdb7-label-bdi"]', descripcion="radio tránsito 2")
        click_resiliente(driver, By.XPATH, xpath_btn_consultar, descripcion="botón consultar (tránsito)")
        esperar_resultados_tabla(driver, "stock_transito", timeout=40)
        driver.save_screenshot("data/resultado_transito.png")
        registros_transito = extraer_datos_tabla(driver, "stock_transito")
        log(f"Stock en tránsito: {len(registros_transito)} registros")

        resultado_total = []
        for r in registros_principal:
            r2 = dict(r)
            r2["Categoria"] = "Stock actual"
            r2["Origen"] = "principal"
            resultado_total.append(r2)
        for r in registros_reingreso:
            r2 = dict(r)
            r2["Categoria"] = "Stock actual"
            r2["Origen"] = "reingreso"
            resultado_total.append(r2)
        for r in registros_transito:
            r2 = dict(r)
            r2["Categoria"] = "Stock en Tránsito"
            r2["Origen"] = "transito"
            resultado_total.append(r2)

        guardar_resultado(resultado_total, "listo", f"Sincronización completada: {len(resultado_total)} registros")

    except Exception as e:
        traceback.print_exc()
        log(f"ERROR CRÍTICO: {e}")
        try:
            os.makedirs("data", exist_ok=True)
            driver.save_screenshot("data/error_sap.png")
            log(f"URL al momento del error: {driver.current_url}")
        except Exception:
            pass
        guardar_resultado([], "error", str(e))
    finally:
        guardar_log()
        driver.quit()


if __name__ == "__main__":
    main()
