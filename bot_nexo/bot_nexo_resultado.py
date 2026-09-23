"""
Bot NEXO — Revisión de resultados (IMAP + recuperación por GLP)
=================================================================
Corre cada 15 minutos por cron. Por cada caja en estado "pendiente":
  1. Busca en la bandeja de entrada un mail de claro._it@claro.com.ar
     con asunto "Resultado activacion. <nombre_archivo>" que mencione
     el nombre de archivo que le subimos a NEXO para esa caja.
  2. Descarga el adjunto CSV (formato: SIM;ESTADO;RESULTADO;)
  3. Para cada SIM SIN NIM todavía: si ESTADO contiene "ACTIVO" -> nim=RESULTADO,
     estado='activa', fecha_activacion=hoy, fecha_vencimiento=+8 meses.
     Si no -> estado='error', se guarda el texto crudo en nexo_estado_raw.
     (Las SIMs que YA tienen NIM nunca se tocan, por si este resultado
     corresponde a un reenvío del rango completo.)
  4. Si TODAS las sims de la caja quedaron activas -> caja pasa a "activa".
     Si alguna falló -> caja pasa a "error" con el detalle.
  5. Marca el mail como leído para no reprocesarlo.
  6. RECUPERACIÓN AUTOMÁTICA: si a esta altura una caja sigue con SIMs sin NIM
     y ya pasaron 20+ minutos desde el envío, entra a NEXO/GLP → "Generar
     Stickers / Archivo Lote", busca el archivo exacto que se subió, y dispara
     "Generar Log Salida" — esto hace que NEXO reenvíe el resultado completo
     por mail, que el próximo run va a procesar normalmente. Cubre tanto el
     caso "nunca llegó ningún mail" como "llegó pero algunas SIMs quedaron con
     un error que en realidad ya está resuelto del lado de NEXO" (ej: "Ya se
     encuentra activa" al reenviar el rango completo).
  7. Limpieza: borra de nexo_uploads/ (en el repo) los CSV de cajas que ya
     quedaron resueltas (Activa o Error) — evita que se acumule basura en
     el repo con el paso del tiempo.

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY (service role)
  GITHUB_TOKEN, GITHUB_REPOSITORY (los provee GitHub Actions automáticamente,
  no hace falta cargar nada a mano — se usan solo para la limpieza de archivos)
La casilla de mail (usuario + contraseña de aplicación) se lee desde
Supabase (Configuración → Distribución), no desde secrets de GitHub,
para que se pueda cambiar sin tocar el repo.
Para la recuperación por GLP hace falta además nexo_cookies.json en la raíz
del repo (mismo archivo que usa bot_nexo_subir.py) — si no existe o venció,
ese paso se salta con un aviso, sin frenar el resto del bot.
"""
import os
import re
import csv
import json
import email
import imaplib
import requests
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # formato "owner/repo"

REMITENTE_ESPERADO = "claro._it@claro.com.ar"
ASUNTO_PREFIJO = "Resultado activacion"
IMAP_HOST = "imap.gmail.com"

# ── Recuperación por GLP (cuando el mail de resultado no llega solo) ────────
# Si a los N minutos de haber enviado la caja a NEXO todavía quedan SIMs sin
# NIM (nunca llegó mail, o llegó pero con errores "falsos" — ej: NEXO dice
# "Ya se encuentra activa" para una SIM que en realidad sí tiene NIM asignado
# adentro de NEXO, solo que el mail automático no lo trae bien), se intenta
# recuperar el resultado real desde el panel "Generar Stickers / Archivo Lote"
# de GLP — ahí SÍ está el dato completo, filtrando por archivo y reenviando
# el log de salida por mail.
UMBRAL_RECUPERACION_MINUTOS = 20

URL_NEXO_HOME = "https://nexostealth-claroaup.msappproxy.net/"
XPATH_BTN_GLP = '//*[@id="app"]/div[2]/div[2]/ul/a[2]'
XPATH_BTN_PRESUSPENSION_LATERAL = '//*[@id="root"]/div/div[2]/nav/a[2]/img'
XPATH_BTN_BUSCAR_STICKERS = '//*[@id="panel2bh-content"]/div/div/form/div[4]/button[1]'
XPATH_EMAIL_STICKERS = '//*[@id="email"]'
XPATH_BTN_GENERAR_LOG_SALIDA = '//*[@id="panel2bh-content"]/div/div/div[2]/div[2]/form/button'

CARPETA_CAPTURAS_RECUPERACION = Path(__file__).resolve().parent / "capturas_recuperacion"
CARPETA_CAPTURAS_RECUPERACION.mkdir(exist_ok=True)


def headers_supabase():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def obtener_credenciales_sap():
    """Usuario/contraseña de SAP — se reutilizan para el login intermedio de
    NEXO (pantalla Keycloak "Claro" y/o Microsoft), mismo criterio que ya
    usa bot_nexo_subir.py. No sirve para saltar el MFA real: si las cookies
    están genuinamente vencidas y Azure AD pide MFA de nuevo, esto no lo
    resuelve — solo cubre el caso (más común) en que la sesión sigue viva
    pero Azure AD/Keycloak igual muestra una pantalla de por medio."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=sap_user,sap_pass",
        headers=headers_supabase(), timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None, None
    return rows[0].get("sap_user"), rows[0].get("sap_pass")


def reportar_estado_nexo(ok, detalle):
    """Guarda el ÚNICO estado de sesión que es de verdad confiable: el
    resultado de haber intentado USARLA hace un instante (no una fecha de
    expiración teórica de la cookie, que puede no reflejar que NEXO la
    invalidó antes por otro motivo — que es justo lo que pasó una vez)."""
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global",
            headers=headers_supabase(),
            json={
                "nexo_sesion_ok": ok,
                "nexo_sesion_verificada_en": datetime.now(timezone.utc).isoformat(),
                "nexo_sesion_detalle": detalle,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"⚠️ No se pudo reportar el estado de la sesión de NEXO: {e}")


def notificar_bot(bot, tipo, mensaje):
    """Deja un aviso para la barra de mensajes del sistema — se lee y se
    muestra la próxima vez que alguien entra a GestionSLA. tipo: 'exito' o
    'error'."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/bot_notificaciones",
            headers=headers_supabase(),
            json={
                "id": f"{bot}_{int(datetime.now(timezone.utc).timestamp())}",
                "bot": bot, "tipo": tipo, "mensaje": mensaje,
                "fecha": datetime.now(timezone.utc).isoformat(), "leido": False,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"⚠️ No se pudo dejar la notificación del bot: {e}")


def sumar_meses_con_ajuste(fecha, meses=8):
    """Igual criterio que en la app: si el mes destino no tiene ese día, usa el último día del mes."""
    anio = fecha.year + (fecha.month - 1 + meses) // 12
    mes = (fecha.month - 1 + meses) % 12 + 1
    # último día del mes destino
    if mes == 12:
        ultimo_dia = (datetime(anio + 1, 1, 1) - timedelta(days=1)).day
    else:
        ultimo_dia = (datetime(anio, mes + 1, 1) - timedelta(days=1)).day
    dia = min(fecha.day, ultimo_dia)
    return datetime(anio, mes, dia).date().isoformat()


def obtener_config_email():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=distribucion_config",
        headers=headers_supabase(),
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError("No se encontró la config global en Supabase")
    raw = rows[0].get("distribucion_config") or "{}"
    cfg = json.loads(raw) if isinstance(raw, str) else raw
    email_user = cfg.get("email_resultado")
    email_pass = cfg.get("email_password_app")
    if not email_user or not email_pass:
        raise RuntimeError("Falta email_resultado / email_password_app en Configuración → Distribución")
    return email_user, email_pass


def obtener_credenciales_nexo():
    """Mismas credenciales corporativas que usa RenovarSesionGestionSLA.pyw
    para loguearse — así el bot puede reloguearse solo cuando la sesión se
    cae, sin depender de que alguien corra la herramienta local a tiempo."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=webcom_email,webcom_password",
            headers=headers_supabase(),
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return "", ""
        return rows[0].get("webcom_email") or "", rows[0].get("webcom_password") or ""
    except Exception as e:
        print(f"⚠️ No se pudieron leer las credenciales de NEXO: {e}")
        return "", ""


def obtener_cajas_pendientes():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?estado=eq.pendiente&select=*",
        headers=headers_supabase(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def decodificar(valor):
    partes = decode_header(valor)
    return "".join(
        (p.decode(enc or "utf-8", errors="ignore") if isinstance(p, bytes) else p)
        for p, enc in partes
    )


def _matchear_caja_por_iccids(csv_bytes, cajas_pendientes):
    """Fallback cuando el asunto del mail no menciona ni el nombre de
    archivo ni el número de caja de ninguna caja pendiente — pasa sobre
    todo con la recuperación manual por GLP, donde el asunto que arma NEXO
    es el nombre de archivo que se le pidió buscar, que puede no coincidir
    con el nombre_archivo guardado en el sistema (ej: la caja se activó
    por fuera del flujo normal).

    Se comparan los ICCID del CSV recibido contra las SIMs SIN NIM de cada
    caja pendiente — si coinciden con UNA sola caja, se matchea ahí. Si
    coinciden con más de una (ambiguo) o con ninguna, no se matchea nada
    y queda para revisión manual."""
    try:
        contenido = csv_bytes.decode("utf-8", errors="ignore")
        lector = csv.DictReader(StringIO(contenido), delimiter=";")
        iccids_csv = {(fila.get("SIM") or "").strip() for fila in lector if (fila.get("SIM") or "").strip()}
    except Exception as e:
        print(f"⚠️ No se pudo leer el CSV para matchear por ICCID: {e}")
        return None
    if not iccids_csv:
        return None

    cajas_con_coincidencias = {}
    for caja in cajas_pendientes:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/distribucion_sims?caja_id=eq.{caja['id']}&nim=is.null&select=iccid",
            headers=headers_supabase(), timeout=30,
        )
        r.raise_for_status()
        iccids_caja = {fila["iccid"] for fila in r.json() if fila.get("iccid")}
        coincidencias = iccids_csv & iccids_caja
        if coincidencias:
            cajas_con_coincidencias[caja["numero_caja"]] = (caja, len(coincidencias))

    if not cajas_con_coincidencias:
        return None
    if len(cajas_con_coincidencias) > 1:
        detalle = ", ".join(f"{n} ({c[1]} SIM)" for n, c in cajas_con_coincidencias.items())
        print(f"⚠️ El CSV matchea por ICCID con MÁS DE UNA caja pendiente ({detalle}) — ambiguo, se deja sin procesar para revisión manual.")
        return None

    numero_caja, (caja, cantidad) = next(iter(cajas_con_coincidencias.items()))
    print(f"✅ Matcheado por ICCID: {cantidad} SIM(s) del CSV coinciden con la Caja {numero_caja} (el asunto del mail no lo mencionaba).")
    return caja


def procesar_caja(caja, csv_bytes):
    contenido = csv_bytes.decode("utf-8", errors="ignore")
    lector = csv.DictReader(StringIO(contenido), delimiter=";")
    hoy_iso = datetime.now(timezone.utc).isoformat()
    hoy = datetime.now(timezone.utc).date()
    vencimiento = sumar_meses_con_ajuste(hoy, 8)

    # Como ahora se puede reenviar el RANGO COMPLETO a NEXO (para reintentar
    # solo las que fallaron, sin necesitar armar un archivo con una lista
    # suelta de ICCIDs), este resultado puede volver a traer SIMs que ya
    # habían quedado activadas en una corrida anterior. Por eso el filtro de
    # cada PATCH agrega "nim=is.null": si la SIM YA tiene un NIM cargado, esta
    # fila del resultado se ignora — nunca se pisa una SIM que ya está bien.
    filas_ok, filas_error = 0, 0
    for fila in lector:
        sim = (fila.get("SIM") or "").strip()
        estado_raw = (fila.get("ESTADO") or "").strip()
        resultado = (fila.get("RESULTADO") or "").strip()
        if not sim:
            continue
        exito = "ACTIVO" in estado_raw.upper()
        patch = {"nexo_estado_raw": estado_raw}
        if exito:
            patch.update({
                "nim": resultado,
                "estado": "activa",
                "fecha_activacion": hoy_iso,
                "fecha_vencimiento": vencimiento,
            })
            filas_ok += 1
        else:
            patch.update({"estado": "error"})
            filas_error += 1
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/distribucion_sims?iccid=eq.{sim}&caja_id=eq.{caja['id']}&nim=is.null",
            headers=headers_supabase(),
            json=patch,
            timeout=30,
        )

    estado_final = "activa" if filas_error == 0 and filas_ok > 0 else "error"
    mensaje = None if estado_final == "activa" else f"{filas_error} SIM(s) con resultado distinto de NIM ACTIVO"
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{caja['id']}",
        headers=headers_supabase(),
        json={"estado": estado_final, "error_mensaje": mensaje},
        timeout=30,
    )
    print(f"Caja {caja['numero_caja']}: {filas_ok} OK, {filas_error} con error -> estado final '{estado_final}'")
    if estado_final == "activa":
        notificar_bot("bot_nexo_resultado", "exito", f"✅ Caja {caja['numero_caja']} resuelta — {filas_ok} SIM(s) activadas.")
    else:
        notificar_bot("bot_nexo_resultado", "error", f"Caja {caja['numero_caja']}: {mensaje}")


def limpiar_archivos_resueltos():
    """Borra de nexo_uploads/ (en el repo) todo archivo que NO sea el CSV vigente
    de una caja actualmente en 'pendiente'. Esto cubre dos casos de basura:
      1) Cajas ya resueltas (Activa/Error) — su archivo ya cumplió su función.
      2) Archivos HUÉRFANOS de reintentos anteriores — cada vez que se reintenta
         un envío se genera un nombre de archivo nuevo, y el campo nombre_archivo
         de la caja se sobreescribe con el último. Los archivos de intentos previos
         quedan sin ninguna caja que los referencie, y por eso no se borraban antes."""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("Sin GITHUB_TOKEN/GITHUB_REPOSITORY — se salta la limpieza de archivos.")
        return

    headers_gh = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    listado_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/nexo_uploads"
    r = requests.get(listado_url, headers=headers_gh, timeout=30)
    if r.status_code == 404:
        return  # todavía no existe la carpeta, nada que limpiar
    r.raise_for_status()
    archivos = r.json()

    r2 = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?select=nombre_archivo,estado",
        headers=headers_supabase(),
        timeout=30,
    )
    r2.raise_for_status()
    cajas = r2.json()
    # Únicos nombres de archivo que deben sobrevivir: los de cajas AÚN pendientes
    archivos_vigentes = {c["nombre_archivo"] for c in cajas if c.get("estado") == "pendiente" and c.get("nombre_archivo")}

    borrados = 0
    for archivo in archivos:
        nombre = archivo["name"]
        if nombre in archivos_vigentes:
            continue  # caja todavía en curso, no tocar
        del_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/nexo_uploads/{nombre}"
        resp = requests.delete(
            del_url,
            headers=headers_gh,
            json={"message": f"Limpieza automática: archivo no vigente ({nombre})", "sha": archivo["sha"]},
            timeout=30,
        )
        if resp.ok:
            borrados += 1
        else:
            print(f"No se pudo borrar {nombre}: {resp.text}")
    print(f"Limpieza: {borrados} archivo(s) borrado(s) de nexo_uploads/ (huérfanos o de cajas ya resueltas).")


def cargar_cookies_nexo():
    """Mismo archivo que usa bot_nexo_subir.py — generado por RenovarSesionGestionSLA.pyw."""
    ruta = Path(__file__).resolve().parent.parent / "nexo_cookies.json"
    if not ruta.exists():
        print("⚠️ No existe nexo_cookies.json — no se puede intentar la recuperación por GLP.")
        return None
    try:
        cookies = json.loads(ruta.read_text(encoding="utf-8"))
        return cookies if cookies else None
    except Exception as e:
        print(f"⚠️ nexo_cookies.json no se pudo leer: {e}")
        return None


def sims_sin_nim(caja_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_sims?caja_id=eq.{caja_id}&nim=is.null&select=id",
        headers=headers_supabase(), timeout=30,
    )
    r.raise_for_status()
    return len(r.json())


def _diag_recuperacion(page, etiqueta):
    try:
        page.screenshot(path=str(CARPETA_CAPTURAS_RECUPERACION / f"{etiqueta}.png"), full_page=True)
    except Exception:
        pass


def _llenar_campo_fecha(page, texto_label, fecha_iso):
    """Busca el <input type='date'> más cercano después de la etiqueta de texto
    (mismo criterio que ya usamos en bot_itec_cargar.py para campos sin id fijo).

    GLP es una app Angular — el value quedaba bien puesto por fuera (fill()
    y hasta el value-set por JS + dispatchEvent lo confirmaban con
    input_value()), PERO Angular nunca se enteraba del cambio: la búsqueda
    seguía corriendo con la fecha vieja, porque dispatchEvent() disparado
    desde afuera de la zona de Angular (zone.js) no siempre lo detecta.
    La única forma confiable es tipear como lo haría una persona de
    verdad — eso sí pasa por el pipeline normal del navegador, que
    zone.js SÍ intercepta. Un <input type='date'> nativo se tipea por
    SEGMENTOS (día, mes, año) en el orden que muestra el campo (acá
    DD/MM/AAAA) — no como el string ISO.
    """
    # .first: algunos campos (confirmado con "Correo Electrónico") aparecen
    # DUPLICADOS en el DOM con el mismo id (típico de Ant Design) — sin
    # .first, Playwright tira "strict mode violation" al encontrar más de
    # un elemento y corta todo el intento con una excepción.
    campo = page.locator(f'xpath=//label[contains(normalize-space(.),"{texto_label}")]/following::input[1]').first
    if campo.count() == 0:
        campo = page.locator(f'xpath=//*[contains(normalize-space(text()),"{texto_label}")]/following::input[1]').first
    if campo.count() == 0:
        print(f"   ⚠️ Fecha '{texto_label}': no se encontró ningún input después de la etiqueta.")
        return

    anio, mes, dia = fecha_iso.split("-")
    fecha_con_barras = f"{dia}/{mes}/{anio}"  # formato que el campo espera de verdad (confirmado: no es un <input type=date> nativo, es texto con barras)

    campo.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.wait_for_timeout(200)
    # Se tipea el string completo CON barras, como lo haría un usuario de
    # verdad tipeando en el campo — antes se tipeaba solo los dígitos
    # (pensando que era un <input type=date> nativo que arma el formato
    # solo), pero el campo se quedó con los dígitos pelados sin barras,
    # así que en realidad espera que las barras se tipeen también.
    page.keyboard.type(fecha_con_barras, delay=80)
    page.keyboard.press("Escape")  # cierra el calendario nativo si quedó abierto
    page.wait_for_timeout(300)

    valor_resultante = campo.input_value()
    if valor_resultante == fecha_con_barras:
        print(f"   ✅ Fecha '{texto_label}' completada correctamente: {valor_resultante}")
    else:
        print(f"   ❌ Fecha '{texto_label}': no se pudo completar tipeando — quedó en '{valor_resultante}' en vez de '{fecha_con_barras}'.")


def _llenar_campo_texto_por_label(page, texto_label, valor):
    """Igual criterio que _llenar_campo_fecha (buscar por label en vez de un
    id fijo, y tipear como una persona de verdad) — para el campo "Correo
    Electrónico" del panel "Log Salida", que dejó de encontrarse por
    XPATH_EMAIL_STICKERS (probablemente cambió el id con la actualización de
    GLP a v.1.16.0). Devuelve True/False según si quedó bien completado.

    Confirmado con el error real de Playwright: hay DOS elementos con el
    mismo id="email" en el DOM — uno visible (el del panel "Log Salida" que
    se ve en pantalla) y otro que no lo es (probablemente una copia interna
    de Ant Design, o de otro panel colapsado). .first tomaba el que
    aparecía primero en el DOM, que resultó ser el oculto, no el visible —
    por eso Playwright esperaba 30 segundos a que "se vuelva visible" y
    nunca pasaba. Ahora se filtra explícitamente por el que SÍ es visible.
    """
    candidatos = page.locator(f'xpath=//label[contains(normalize-space(.),"{texto_label}")]/following::input[1]')
    if candidatos.count() == 0:
        candidatos = page.locator(f'xpath=//*[contains(normalize-space(text()),"{texto_label}")]/following::input[1]')
    total = candidatos.count()
    if total == 0:
        print(f"   ⚠️ Campo '{texto_label}': no se encontró ningún input después de la etiqueta.")
        return False

    campo = None
    for i in range(total):
        candidato = candidatos.nth(i)
        if candidato.is_visible():
            campo = candidato
            break
    if campo is None:
        print(f"   ⚠️ Campo '{texto_label}': se encontraron {total} input(s) pero ninguno está visible.")
        return False

    campo.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.wait_for_timeout(200)
    page.keyboard.type(valor, delay=60)
    page.wait_for_timeout(300)

    valor_resultante = campo.input_value()
    if valor_resultante == valor:
        print(f"   ✅ Campo '{texto_label}' completado correctamente: {valor_resultante}")
        return True
    print(f"   ❌ Campo '{texto_label}': no se pudo completar tipeando — quedó en '{valor_resultante}' en vez de '{valor}'.")
    return False


def _recuperar_glp_core(etiqueta, nombre_archivo, fecha_desde, fecha_hasta, email_resultado):
    """El trabajo de verdad: entra a GLP, busca el archivo por nombre en el
    rango de fechas dado, y dispara 'Generar Log Salida'. Usado tanto por
    la recuperación automática (intentar_recuperar_resultado_glp) como por
    la manual (recuperar_glp_manual) — es la MISMA navegación en los dos
    casos, lo único que cambia es de dónde salen nombre_archivo/fechas."""
    cookies = cargar_cookies_nexo()
    if not cookies:
        return

    print(f"🌐 {etiqueta}: intentando recuperar resultado desde GLP "
          f"(archivo: {nombre_archivo}, rango {fecha_desde} → {fecha_hasta})...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            page.goto(URL_NEXO_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            _diag_recuperacion(page, f"r00_nexo_{etiqueta}")

            # Puede aparecer una pantalla de login de por medio aunque las
            # cookies sigan siendo válidas (Keycloak "Claro" y/o Microsoft) —
            # se completa con las credenciales de SAP reutilizadas, mismo
            # patrón que ya usa bot_nexo_subir.py, hasta 4 pantallas
            # encadenadas. Si DESPUÉS de esto sigue habiendo un campo de
            # login visible, ahí sí es una sesión genuinamente vencida (o
            # pide MFA real, que esto no puede resolver).
            SELECTOR_USUARIO = '#username, input[name="username"], input[type="email"]'
            SELECTOR_PASSWORD = '#password, input[name="password"], input[type="password"]'
            SELECTOR_SUBMIT = 'button[type="submit"], input[type="submit"], #kc-login'

            campo_usuario_inicial = page.locator(SELECTOR_USUARIO).first
            if campo_usuario_inicial.count() > 0 and campo_usuario_inicial.is_visible():
                sap_user, sap_pass = obtener_credenciales_sap()
                if not sap_user or not sap_pass:
                    print(f"⚠️ {etiqueta}: apareció login y no hay credenciales de SAP configuradas para completarlo automáticamente.")
                else:
                    for intento in range(4):  # como máximo 4 pantallas encadenadas (Keycloak + MS)
                        campo_usuario = page.locator(SELECTOR_USUARIO).first
                        campo_password = page.locator(SELECTOR_PASSWORD).first
                        hay_usuario = campo_usuario.count() > 0 and campo_usuario.is_visible()
                        hay_password = campo_password.count() > 0 and campo_password.is_visible()
                        if not hay_usuario and not hay_password:
                            break  # ya no hay más pantallas de login a la vista

                        if hay_usuario:
                            campo_usuario.fill(sap_user)
                            if hay_password:
                                campo_password.fill(sap_pass)
                        elif hay_password:
                            campo_password.fill(sap_pass)

                        try:
                            with context.expect_page(timeout=8000) as pagina_nueva_info:
                                page.locator(SELECTOR_SUBMIT).first.click()
                            page = pagina_nueva_info.value
                        except PWTimeout:
                            pass
                        page.wait_for_load_state("domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)

                        # Posible prompt "¿Seguir conectado?" (KMSI) de Microsoft
                        if page.locator('#idBtn_Back').count() > 0:
                            page.locator('#idBtn_Back').click()
                            page.wait_for_load_state("domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2000)

                    _diag_recuperacion(page, f"r00b_tras_login_{etiqueta}")

            # Si a pesar de todo seguimos viendo un campo de login, ahí sí es
            # una sesión genuinamente vencida (o pide MFA real).
            if page.locator(SELECTOR_USUARIO).first.count() > 0 and page.locator(SELECTOR_USUARIO).first.is_visible():
                print(f"❌ {etiqueta}: la sesión de NEXO parece vencida — no se puede recuperar por GLP hasta renovarla.")
                reportar_estado_nexo(False, f"Detectado al intentar recuperar '{etiqueta}' por GLP — apareció la pantalla de login.")
                notificar_bot("bot_nexo_resultado", "error",
                    f"🍪 La sesión de NEXO expiró — hay que renovarla localmente (requiere 2FA). Detectado al recuperar '{etiqueta}'.")
                browser.close()
                return

            # Llegamos pasando el chequeo de login — la sesión está viva de
            # verdad en este momento, no en teoría.
            reportar_estado_nexo(True, f"Verificado al recuperar '{etiqueta}' por GLP.")

            existe_glp = page.locator(f'xpath={XPATH_BTN_GLP}').count() > 0
            if not existe_glp:
                print(f"⚠️ {etiqueta}: no se encontró el link GLP — se aborta la recuperación.")
                _diag_recuperacion(page, f"r00b_sin_glp_{etiqueta}")
                browser.close()
                return

            with context.expect_page() as nueva_pagina_info:
                page.locator(f'xpath={XPATH_BTN_GLP}').click(timeout=45000)
            glp = nueva_pagina_info.value
            glp.wait_for_load_state("domcontentloaded", timeout=60000)
            glp.wait_for_timeout(2000)
            _diag_recuperacion(glp, f"r01_glp_{etiqueta}")

            try:
                glp.get_by_title("levanta presuspension masiva", exact=False).click(timeout=15000)
            except PWTimeout:
                glp.locator(f'xpath={XPATH_BTN_PRESUSPENSION_LATERAL}').click(timeout=15000)
            glp.wait_for_timeout(2000)

            # Panel "Generar Stickers / Archivo Lote" (el otro, NO "Levantar Presuspensión")
            glp.get_by_text("Generar Stickers", exact=False).click(timeout=15000)
            glp.wait_for_timeout(1500)
            _diag_recuperacion(glp, f"r02_panel_stickers_{etiqueta}")

            _llenar_campo_fecha(glp, "Fecha Desde", fecha_desde)
            _llenar_campo_fecha(glp, "Fecha Hasta", fecha_hasta)
            _diag_recuperacion(glp, f"r03_fechas_{etiqueta}")

            glp.locator(f'xpath={XPATH_BTN_BUSCAR_STICKERS}').click()
            glp.wait_for_timeout(4000)
            _diag_recuperacion(glp, f"r04_resultados_{etiqueta}")

            # Buscar la fila cuyo nombre de archivo coincide con el que enviamos
            # (NEXO lo muestra en mayúsculas — comparamos sin importar el caso)
            filas = glp.locator("table tbody tr")
            total = filas.count()
            fila_encontrada = None
            for i in range(total):
                texto_fila = filas.nth(i).inner_text(timeout=3000)
                if nombre_archivo.upper() in texto_fila.upper():
                    fila_encontrada = filas.nth(i)
                    break

            if not fila_encontrada:
                print(f"⚠️ {etiqueta}: no se encontró el archivo '{nombre_archivo}' en la tabla de GLP "
                      f"(rango {fecha_desde} a {fecha_hasta}) — puede que todavía no aparezca, se reintentará en el próximo run.")
                _diag_recuperacion(glp, f"r05_no_encontrado_{etiqueta}")
                browser.close()
                return

            fila_encontrada.locator('input[type="radio"]').click()
            # Hay una pequeña animación entre que se selecciona el archivo y
            # que aparece el panel con el campo de mail — sin esperar acá,
            # el campo todavía no existía en el DOM cuando se intentaba
            # completar. (No usamos wait_for(visible) acá porque hay DOS
            # elementos con id="email" en el DOM, uno oculto — si ese fuera
            # el primero, esperaríamos 8s de más para nada; la visibilidad
            # de verdad ya se filtra abajo, en _llenar_campo_texto_por_label.)
            glp.wait_for_timeout(1500)
            _diag_recuperacion(glp, f"r06_fila_seleccionada_{etiqueta}")

            # "Correo Electrónico" (panel "Log Salida") — se busca por label
            # en vez de un id fijo (XPATH_EMAIL_STICKERS quedó viejo con la
            # actualización de GLP a v.1.16.0). Si por algún motivo tampoco
            # se encuentra así, se cae al xpath viejo como última red.
            completado = _llenar_campo_texto_por_label(glp, "Correo Electrónico", email_resultado)
            if not completado:
                try:
                    candidatos_fallback = glp.locator(f'xpath={XPATH_EMAIL_STICKERS}')
                    for i in range(candidatos_fallback.count()):
                        if candidatos_fallback.nth(i).is_visible():
                            candidatos_fallback.nth(i).fill(email_resultado)
                            break
                except Exception:
                    pass
            _diag_recuperacion(glp, f"r06b_email_completado_{etiqueta}")

            glp.locator(f'xpath={XPATH_BTN_GENERAR_LOG_SALIDA}').click()
            glp.wait_for_timeout(4000)
            _diag_recuperacion(glp, f"r07_generado_{etiqueta}")

            print(f"✅ {etiqueta}: se disparó 'Generar Log Salida' para '{nombre_archivo}' — "
                  f"el mail nuevo debería llegar y procesarse en el próximo run.")

        except Exception as e:
            print(f"❌ {etiqueta}: error intentando recuperar por GLP: {e}")
            # Antes esto sacaba la foto de "page" (la pestaña original de
            # NEXO) sin importar en qué pestaña haya ocurrido el error de
            # verdad — por eso la captura de error mostraba la home de NEXO
            # en vez de la pantalla de GLP donde realmente se cortó. "glp"
            # es la pestaña que se abre después, y sigue existiendo aunque
            # el error haya sido en un paso posterior de esa misma pestaña.
            try:
                _diag_recuperacion(glp, f"r99_error_{etiqueta}")
            except Exception:
                try:
                    _diag_recuperacion(page, f"r99_error_{etiqueta}")
                except Exception:
                    pass
        finally:
            browser.close()


def intentar_recuperar_resultado_glp(caja):
    """Entra a GLP → 'Generar Stickers / Archivo Lote', busca el archivo exacto
    que le subimos a esta caja, y dispara 'Generar Log Salida' para que NEXO
    reenvíe el resultado completo por mail — sin que nadie tenga que tocar nada,
    el próximo run del bot (15 min después) va a procesar ese mail normalmente."""
    nombre_archivo = caja.get("nombre_archivo")
    if not nombre_archivo:
        print(f"⚠️ Caja {caja['numero_caja']}: no tiene nombre_archivo registrado, no se puede buscar en GLP.")
        return

    # Rango fijo: 15 días atrás hasta hoy — buscar "desde/hasta = hoy" (como
    # se hacía antes, tomando fecha_envio_nexo tal cual) NUNCA iba a
    # encontrar nada: el archivo aparece en la tabla de GLP con su propia
    # "Fecha Procesamiento", que no necesariamente coincide con el día en
    # que se envió. 15 días es el máximo que acepta el buscador de GLP, así
    # que se usa el rango completo para maximizar las chances de encontrarlo.
    fecha_desde = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%d")
    fecha_hasta = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        email_resultado, _ = obtener_config_email()
    except Exception:
        email_resultado = None
    if not email_resultado:
        print(f"⚠️ Caja {caja['numero_caja']}: no se pudo determinar el email de resultados, se aborta la recuperación.")
        return

    _recuperar_glp_core(f"Caja {caja['numero_caja']}", nombre_archivo, fecha_desde, fecha_hasta, email_resultado)


def recuperar_glp_manual(nombre_archivo, fecha_desde_str, fecha_hasta_str):
    """Recuperación DISPARADA A MANO — para el caso de una caja que se
    activó fuera del flujo normal del sistema (sin fecha_envio_nexo
    registrada, por eso la recuperación automática nunca la agarra). Se le
    pasa el nombre de archivo tal cual figura en NEXO y el rango de fechas
    a buscar — el rango NUNCA puede superar 15 días, es una restricción de
    NEXO (el buscador de GLP no devuelve nada útil con rangos más largos)."""
    try:
        fecha_desde = datetime.strptime(fecha_desde_str, "%Y-%m-%d").date()
        fecha_hasta = datetime.strptime(fecha_hasta_str, "%Y-%m-%d").date()
    except Exception:
        print(f"❌ Recuperación manual: las fechas deben venir en formato YYYY-MM-DD (recibido: '{fecha_desde_str}' → '{fecha_hasta_str}').")
        return
    if fecha_hasta < fecha_desde:
        print(f"❌ Recuperación manual: 'hasta' ({fecha_hasta}) es anterior a 'desde' ({fecha_desde}).")
        return
    if (fecha_hasta - fecha_desde).days > 15:
        print(f"❌ Recuperación manual: el rango ({(fecha_hasta - fecha_desde).days} días) supera el máximo de 15 días que acepta el buscador de GLP.")
        return

    try:
        email_resultado, _ = obtener_config_email()
    except Exception:
        email_resultado = None
    if not email_resultado:
        print("⚠️ Recuperación manual: no se pudo determinar el email de resultados, se aborta.")
        return

    _recuperar_glp_core(
        f"Manual '{nombre_archivo}'", nombre_archivo,
        fecha_desde.strftime("%Y-%m-%d"), fecha_hasta.strftime("%Y-%m-%d"),
        email_resultado,
    )


def main():
    # Recuperación MANUAL — se dispara con 3 variables de entorno
    # (pensadas para pasarlas como inputs de un workflow_dispatch en
    # GitHub Actions): RECUPERAR_ARCHIVO, RECUPERAR_DESDE, RECUPERAR_HASTA.
    # Corre SIEMPRE que estén presentes, antes que nada más — es
    # independiente del flujo normal (no necesita que la caja tenga
    # fecha_envio_nexo, ni siquiera que exista una caja "pendiente" —
    # justo el caso de una caja que se activó por fuera del sistema).
    archivo_manual = os.environ.get("RECUPERAR_ARCHIVO", "").strip()
    if archivo_manual:
        desde_manual = os.environ.get("RECUPERAR_DESDE", "").strip()
        hasta_manual = os.environ.get("RECUPERAR_HASTA", "").strip()
        recuperar_glp_manual(archivo_manual, desde_manual, hasta_manual)

    email_user, email_pass = obtener_config_email()
    cajas_pendientes = obtener_cajas_pendientes()
    if not cajas_pendientes:
        print("No hay cajas pendientes. Nada para revisar por mail.")
        limpiar_archivos_resueltos()
        return

    por_archivo = {c["nombre_archivo"]: c for c in cajas_pendientes if c.get("nombre_archivo")}

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(email_user, email_pass)
    imap.select("INBOX")

    # Buscamos por remitente + fecha reciente, NO por "no leído": si alguien abre el
    # mail para mirarlo (Gmail web, celular, etc.) deja de estar "unseen" y el bot
    # nunca más lo encontraría aunque siga ahí. La protección real contra reprocesar
    # ya está dada por el matcheo contra cajas_pendientes: una caja resuelta nunca
    # vuelve a matchear, así que revisar mails ya leídos es seguro (no duplica nada).
    fecha_desde = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%d-%b-%Y")
    _, datos = imap.search(None, f'(FROM "{REMITENTE_ESPERADO}" SINCE {fecha_desde})')
    ids = datos[0].split()
    print(f"Mails de {REMITENTE_ESPERADO} en los últimos 7 días: {len(ids)}")

    for mid in ids:
        _, msg_data = imap.fetch(mid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        asunto = decodificar(msg.get("Subject", ""))
        if ASUNTO_PREFIJO.lower() not in asunto.lower():
            continue

        # Se busca el adjunto CSV PRIMERO, sin importar si ya sabemos a qué
        # caja pertenece — hace falta de todas formas para el fallback por
        # ICCID de más abajo.
        csv_bytes = None
        partes_vistas = []
        for parte in msg.walk():
            nombre_parte = decodificar(parte.get_filename() or "")
            partes_vistas.append(f"content_type={parte.get_content_type()} disposition={parte.get_content_disposition()} filename={nombre_parte!r}")
            # No filtramos por Content-Disposition == "attachment": algunos sistemas
            # mandan el archivo como "inline" o sin esa cabecera, y por eso antes no
            # lo encontrábamos aunque el adjunto SÍ estaba en el mail.
            if nombre_parte.lower().endswith(".csv"):
                csv_bytes = parte.get_payload(decode=True)
                break

        # Matchear el asunto contra el nombre de archivo de alguna caja pendiente
        caja_match = None
        for nombre_archivo, caja in por_archivo.items():
            if nombre_archivo and nombre_archivo in asunto:
                caja_match = caja
                break
        # Fallback 1: también intentar matchear por número de caja dentro del asunto
        if not caja_match:
            for caja in cajas_pendientes:
                if caja["numero_caja"] in asunto:
                    caja_match = caja
                    break
        # Fallback 2: cuando el asunto no menciona ni el nombre de archivo ni
        # el número de caja (pasa con la recuperación manual por GLP — el
        # asunto que arma NEXO ahí es el nombre de archivo que se le pidió
        # buscar, que puede no ser igual al nombre_archivo guardado en el
        # sistema) — se comparan los ICCID del CSV contra las SIMs sin NIM
        # de cada caja pendiente. Si coinciden con una sola, se matchea ahí.
        if not caja_match and csv_bytes:
            caja_match = _matchear_caja_por_iccids(csv_bytes, cajas_pendientes)

        if not caja_match:
            print(f"⚠️ Mail '{asunto}' no matchea con ninguna caja pendiente conocida (ni por asunto ni por ICCID) — se deja sin leer.")
            continue

        if csv_bytes:
            procesar_caja(caja_match, csv_bytes)
            imap.store(mid, '+FLAGS', '\\Seen')
        else:
            print(f"⚠️ Mail '{asunto}' matcheó pero no se encontró ningún archivo .csv en sus partes.")
            print(f"   Estructura real del mail (para diagnóstico):")
            for p in partes_vistas:
                print(f"   - {p}")

    imap.logout()

    # ── Recuperación automática por GLP ───────────────────────────────────
    # Para cada caja que arrancó este run como "pendiente": si todavía le
    # quedan SIMs sin NIM (nunca llegó mail, o llegó con errores que en
    # realidad son casos ya resueltos del lado de NEXO) y ya pasó suficiente
    # tiempo desde el envío, se intenta recuperar el resultado real desde GLP.
    # Cada motivo de salteo ahora se imprime — antes los 3 "continue" eran
    # mudos, así que un log sin ningún intento de recuperación no decía NADA
    # sobre por qué (¿ya no faltaba nada? ¿sin fecha de envío? ¿faltaba
    # tiempo?) — imposible de diagnosticar desde acá.
    for caja in cajas_pendientes:
        faltantes = sims_sin_nim(caja["id"])
        if faltantes == 0:
            print(f"ℹ️ Caja {caja['numero_caja']}: ya no le faltan SIMs con NIM — no hace falta recuperar nada.")
            continue
        fecha_envio = caja.get("fecha_envio_nexo")
        if not fecha_envio:
            print(f"⚠️ Caja {caja['numero_caja']}: {faltantes} SIM(s) sin NIM, pero no tiene fecha_envio_nexo registrada — no se puede calcular si ya pasó el umbral, se saltea.")
            continue
        try:
            enviado = datetime.fromisoformat(fecha_envio.replace("Z", "+00:00"))
        except Exception:
            print(f"⚠️ Caja {caja['numero_caja']}: {faltantes} SIM(s) sin NIM, pero fecha_envio_nexo ('{fecha_envio}') no se pudo interpretar — se saltea.")
            continue
        minutos_transcurridos = (datetime.now(timezone.utc) - enviado).total_seconds() / 60
        if minutos_transcurridos < UMBRAL_RECUPERACION_MINUTOS:
            print(f"⏳ Caja {caja['numero_caja']}: {faltantes} SIM(s) sin NIM, pero solo pasaron {int(minutos_transcurridos)} min de {UMBRAL_RECUPERACION_MINUTOS} necesarios — todavía no toca intentar GLP.")
            continue
        print(f"⏳ Caja {caja['numero_caja']}: {faltantes} SIM(s) sin NIM después de "
              f"{int(minutos_transcurridos)} min — intentando recuperar por GLP...")
        intentar_recuperar_resultado_glp(caja)

    limpiar_archivos_resueltos()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Cualquier falla del run completo (no solo la sesión de NEXO) deja
        # aviso en la barra de mensajes del sistema — antes esto solo se
        # veía en el log de GitHub Actions, que nadie mira si no anda mal
        # algo puntual.
        notificar_bot("bot_nexo_resultado", "error", f"El bot terminó con un error: {e}")
        raise
