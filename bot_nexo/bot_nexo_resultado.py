"""
Bot NEXO — Revisión de resultados (IMAP)
=========================================
Corre cada 15 minutos por cron. Por cada caja en estado "pendiente":
  1. Busca en la bandeja de entrada un mail de claro._it@claro.com.ar
     con asunto "Resultado activacion. <nombre_archivo>" que mencione
     el nombre de archivo que le subimos a NEXO para esa caja.
  2. Descarga el adjunto CSV (formato: SIM;ESTADO;RESULTADO;)
  3. Para cada SIM: si ESTADO contiene "ACTIVO" -> nim=RESULTADO,
     estado='activa', fecha_activacion=hoy, fecha_vencimiento=+8 meses.
     Si no -> estado='error', se guarda el texto crudo en nexo_estado_raw.
  4. Si TODAS las sims de la caja quedaron activas -> caja pasa a "activa".
     Si alguna falló -> caja pasa a "error" con el detalle.
  5. Marca el mail como leído para no reprocesarlo.
  6. Limpieza: borra de nexo_uploads/ (en el repo) los CSV de cajas que ya
     quedaron resueltas (Activa o Error) — evita que se acumule basura en
     el repo con el paso del tiempo.

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY (service role)
  GITHUB_TOKEN, GITHUB_REPOSITORY (los provee GitHub Actions automáticamente,
  no hace falta cargar nada a mano — se usan solo para la limpieza de archivos)
La casilla de mail (usuario + contraseña de aplicación) se lee desde
Supabase (Configuración → Distribución), no desde secrets de GitHub,
para que se pueda cambiar sin tocar el repo.
"""
import os
import re
import csv
import json
import email
import imaplib
import requests
from io import StringIO
from datetime import datetime, timedelta
from email.header import decode_header

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # formato "owner/repo"

REMITENTE_ESPERADO = "claro._it@claro.com.ar"
ASUNTO_PREFIJO = "Resultado activacion"
IMAP_HOST = "imap.gmail.com"


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
    hoy_iso = datetime.utcnow().isoformat()
    hoy = datetime.utcnow().date()
    vencimiento = sumar_meses_con_ajuste(hoy, 8)

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
            f"{SUPABASE_URL}/rest/v1/distribucion_sims?iccid=eq.{sim}&caja_id=eq.{caja['id']}",
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
    fecha_desde = (datetime.utcnow() - timedelta(days=7)).strftime("%d-%b-%Y")
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
        for parte in msg.walk():
            if parte.get_content_disposition() == "attachment":
                nombre = decodificar(parte.get_filename() or "")
                if nombre.lower().endswith(".csv"):
                    procesar_caja(caja_match, parte.get_payload(decode=True))
                    adjunto_procesado = True

        if adjunto_procesado:
            imap.store(mid, '+FLAGS', '\\Seen')
        else:
            print(f"⚠️ Mail '{asunto}' matcheó pero no traía adjunto CSV — se deja sin leer para revisar a mano.")

    imap.logout()
    limpiar_archivos_resueltos()


if __name__ == "__main__":
    main()
