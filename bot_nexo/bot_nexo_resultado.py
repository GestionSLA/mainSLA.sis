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
    (mismo criterio que ya usamos en bot_itec_cargar.py para campos sin id fijo)."""
    campo = page.locator(f'xpath=//label[contains(normalize-space(.),"{texto_label}")]/following::input[1]')
    if campo.count() == 0:
        # Alternativa: buscar por el texto suelto (no necesariamente un <label>)
        campo = page.locator(f'xpath=//*[contains(normalize-space(text()),"{texto_label}")]/following::input[1]')
    campo.fill(fecha_iso)


def intentar_recuperar_resultado_glp(caja):
    """Entra a GLP → 'Generar Stickers / Archivo Lote', busca el archivo exacto
    que le subimos a esta caja, y dispara 'Generar Log Salida' para que NEXO
    reenvíe el resultado completo por mail — sin que nadie tenga que tocar nada,
    el próximo run del bot (15 min después) va a procesar ese mail normalmente."""
    nombre_archivo = caja.get("nombre_archivo")
    if not nombre_archivo:
        print(f"⚠️ Caja {caja['numero_caja']}: no tiene nombre_archivo registrado, no se puede buscar en GLP.")
        return

    cookies = cargar_cookies_nexo()
    if not cookies:
        return

    fecha_envio = caja.get("fecha_envio_nexo")
    try:
        fecha_desde = datetime.fromisoformat(fecha_envio.replace("Z", "+00:00")).strftime("%Y-%m-%d") if fecha_envio else \
                      (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    except Exception:
        fecha_desde = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    fecha_hasta = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        email_resultado, _ = obtener_config_email()
    except Exception:
        email_resultado = None
    if not email_resultado:
        print(f"⚠️ Caja {caja['numero_caja']}: no se pudo determinar el email de resultados, se aborta la recuperación.")
        return

    print(f"🌐 Caja {caja['numero_caja']}: intentando recuperar resultado desde GLP "
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
            _diag_recuperacion(page, f"r00_nexo_{caja['numero_caja']}")

            # Si hay pantalla de login acá, las cookies vencieron — no hay nada
            # más que hacer que esperar a que se renueve la sesión.
            if page.locator('#username, input[name="username"], input[type="email"]').first.count() > 0 and \
               page.locator('#username, input[name="username"], input[type="email"]').first.is_visible():
                print(f"❌ Caja {caja['numero_caja']}: la sesión de NEXO parece vencida — no se puede recuperar por GLP hasta renovarla.")
                browser.close()
                return

            existe_glp = page.locator(f'xpath={XPATH_BTN_GLP}').count() > 0
            if not existe_glp:
                print(f"⚠️ Caja {caja['numero_caja']}: no se encontró el link GLP — se aborta la recuperación.")
                _diag_recuperacion(page, f"r00b_sin_glp_{caja['numero_caja']}")
                browser.close()
                return

            with context.expect_page() as nueva_pagina_info:
                page.locator(f'xpath={XPATH_BTN_GLP}').click(timeout=45000)
            glp = nueva_pagina_info.value
            glp.wait_for_load_state("domcontentloaded", timeout=60000)
            glp.wait_for_timeout(2000)
            _diag_recuperacion(glp, f"r01_glp_{caja['numero_caja']}")

            try:
                glp.get_by_title("levanta presuspension masiva", exact=False).click(timeout=15000)
            except PWTimeout:
                glp.locator(f'xpath={XPATH_BTN_PRESUSPENSION_LATERAL}').click(timeout=15000)
            glp.wait_for_timeout(2000)

            # Panel "Generar Stickers / Archivo Lote" (el otro, NO "Levantar Presuspensión")
            glp.get_by_text("Generar Stickers", exact=False).click(timeout=15000)
            glp.wait_for_timeout(1500)
            _diag_recuperacion(glp, f"r02_panel_stickers_{caja['numero_caja']}")

            _llenar_campo_fecha(glp, "Fecha Desde", fecha_desde)
            _llenar_campo_fecha(glp, "Fecha Hasta", fecha_hasta)
            _diag_recuperacion(glp, f"r03_fechas_{caja['numero_caja']}")

            glp.locator(f'xpath={XPATH_BTN_BUSCAR_STICKERS}').click()
            glp.wait_for_timeout(4000)
            _diag_recuperacion(glp, f"r04_resultados_{caja['numero_caja']}")

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
                print(f"⚠️ Caja {caja['numero_caja']}: no se encontró el archivo '{nombre_archivo}' en la tabla de GLP "
                      f"(rango {fecha_desde} a {fecha_hasta}) — puede que todavía no aparezca, se reintentará en el próximo run.")
                _diag_recuperacion(glp, f"r05_no_encontrado_{caja['numero_caja']}")
                browser.close()
                return

            fila_encontrada.locator('input[type="radio"]').click()
            _diag_recuperacion(glp, f"r06_fila_seleccionada_{caja['numero_caja']}")

            glp.locator(f'xpath={XPATH_EMAIL_STICKERS}').fill(email_resultado)
            glp.locator(f'xpath={XPATH_BTN_GENERAR_LOG_SALIDA}').click()
            glp.wait_for_timeout(4000)
            _diag_recuperacion(glp, f"r07_generado_{caja['numero_caja']}")

            print(f"✅ Caja {caja['numero_caja']}: se disparó 'Generar Log Salida' para '{nombre_archivo}' — "
                  f"el mail nuevo debería llegar y procesarse en el próximo run.")

        except Exception as e:
            print(f"❌ Caja {caja['numero_caja']}: error intentando recuperar por GLP: {e}")
            try:
                _diag_recuperacion(page, f"r99_error_{caja['numero_caja']}")
            except Exception:
                pass
        finally:
            browser.close()


def main():
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

        # Matchear el asunto contra el nombre de archivo de alguna caja pendiente
        caja_match = None
        for nombre_archivo, caja in por_archivo.items():
            if nombre_archivo and nombre_archivo in asunto:
                caja_match = caja
                break
        # Fallback: también intentar matchear por número de caja dentro del asunto
        if not caja_match:
            for caja in cajas_pendientes:
                if caja["numero_caja"] in asunto:
                    caja_match = caja
                    break

        if not caja_match:
            print(f"⚠️ Mail '{asunto}' no matchea con ninguna caja pendiente conocida — se deja sin leer.")
            continue

        adjunto_procesado = False
        partes_vistas = []
        for parte in msg.walk():
            nombre_parte = decodificar(parte.get_filename() or "")
            partes_vistas.append(f"content_type={parte.get_content_type()} disposition={parte.get_content_disposition()} filename={nombre_parte!r}")
            # No filtramos por Content-Disposition == "attachment": algunos sistemas
            # mandan el archivo como "inline" o sin esa cabecera, y por eso antes no
            # lo encontrábamos aunque el adjunto SÍ estaba en el mail.
            if nombre_parte.lower().endswith(".csv"):
                procesar_caja(caja_match, parte.get_payload(decode=True))
                adjunto_procesado = True

        if adjunto_procesado:
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
    for caja in cajas_pendientes:
        faltantes = sims_sin_nim(caja["id"])
        if faltantes == 0:
            continue
        fecha_envio = caja.get("fecha_envio_nexo")
        if not fecha_envio:
            continue
        try:
            enviado = datetime.fromisoformat(fecha_envio.replace("Z", "+00:00"))
        except Exception:
            continue
        minutos_transcurridos = (datetime.now(timezone.utc) - enviado).total_seconds() / 60
        if minutos_transcurridos < UMBRAL_RECUPERACION_MINUTOS:
            continue
        print(f"⏳ Caja {caja['numero_caja']}: {faltantes} SIM(s) sin NIM después de "
              f"{int(minutos_transcurridos)} min — intentando recuperar por GLP...")
        intentar_recuperar_resultado_glp(caja)

    limpiar_archivos_resueltos()


if __name__ == "__main__":
    main()
