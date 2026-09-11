"""
Bot Caminantes — Recaudado diario (planilla de Rendición de Cobranzas)
========================================================================
Lee la planilla de Google Sheets vía la API oficial (Service Account — sin
login interactivo, sin cookies, sin riesgo de captcha) y calcula cuánto
recaudó cada caminante EN EL DÍA DE HOY, sumando los importes de las filas
de hoy cuyo "Tipo de rendición" sea uno de:
  Cliente a CBU / Envío de efectivo / Depósito Bancario
Cualquier otro tipo (ej: "Recupero de Viáticos...") descarta la fila entera.

Matching de nombre (decisión del usuario): primero por nombre de pila
(como aparece en la planilla, columna G), y si no matchea con ningún
caminante activo, se prueba por apellido. Si matchea con más de uno
(ambiguo), la fila se descarta y se loguea para revisión manual — nunca
se adivina.

El resultado se guarda en Supabase, tabla caminantes_saldo_diario, columna
`recaudado`, con upsert por (caminante_id, fecha) — NUNCA toca la columna
`saldo_entregado`, que la escribe el Bot 2 (ITEC Traceability), aunque
ambos escriban en la misma fila del mismo día.

No necesita self-hosted: esto es una llamada a la API de Google, no toca
ningún portal de Claro — puede correr en un runner normal de GitHub.

Variables de entorno esperadas:
  SUPABASE_URL, SUPABASE_KEY          -> service role
  GOOGLE_SHEETS_CREDENTIALS           -> el JSON completo de la Service Account (como texto)
"""
import os
import re
import sys
import json
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import gspread
from google.oauth2.service_account import Credentials

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")

# Cualquier "Tipo de rendición" que NO esté en esta lista descarta la fila
# completa (ej: "Recupero de Viáticos (Autorización requerida)").
TIPOS_VALIDOS = {"cliente a cbu", "envio de efectivo", "deposito bancario"}

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GOOGLE_SHEETS_CREDENTIALS = os.environ["GOOGLE_SHEETS_CREDENTIALS"]

URL_PLANILLA_DEFAULT = (
    "https://docs.google.com/spreadsheets/d/1t4aonrMCzgana32LJmVpBRGtrK2OnKlPpo3ZuroLlpk/"
    "edit?gid=413083242#gid=413083242"
)


def headers_supabase():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def obtener_url_planilla():
    """La URL de la planilla es configurable desde GestionSLA (Configuración
    → Sistemas AMX → Distribución), mismo bloque que ya usa ITEC. Si no está
    configurada todavía, se usa la default."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=distribucion_config",
            headers=headers_supabase(), timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if rows:
            raw = rows[0].get("distribucion_config") or "{}"
            cfg = json.loads(raw) if isinstance(raw, str) else raw
            return cfg.get("planilla_url") or URL_PLANILLA_DEFAULT
    except Exception as e:
        print(f"⚠️ No se pudo leer la URL de la planilla desde Supabase, se usa la default: {e}")
    return URL_PLANILLA_DEFAULT


def parse_id_y_gid(url):
    m_id = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    m_gid = re.search(r"[?&#]gid=(\d+)", url)
    if not m_id:
        raise RuntimeError(f"No se pudo extraer el ID de la planilla de la URL: {url}")
    spreadsheet_id = m_id.group(1)
    gid = int(m_gid.group(1)) if m_gid else 0
    return spreadsheet_id, gid


def _normalizar(texto):
    """minúsculas, sin tildes, sin espacios de más — para comparar nombres
    de forma tolerante a diferencias menores de tipeo entre ITEC y la
    planilla (cargada a mano)."""
    texto = (texto or "").strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", texto)


def _parsear_importe(texto):
    """La planilla muestra los importes en formato argentino: '122.500,00'.
    Saca los puntos de miles y cambia la coma decimal por punto."""
    if texto is None:
        return None
    texto = str(texto).strip()
    if not texto:
        return None
    texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return None


def _col(fila, i):
    return fila[i] if len(fila) > i else ""


def obtener_caminantes_activos():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/caminantes?activo=eq.true&select=id,nombre,apellido,nombre_pila",
        headers=headers_supabase(), timeout=30,
    )
    r.raise_for_status()
    return r.json()


def matchear_caminante(nombre_planilla, caminantes):
    """Primero por nombre de pila (como aparece en la planilla), y si no
    encuentra nada, por apellido — orden decidido por el usuario, ya que
    la carga manual de la planilla toma el recaudo de no duplicar nombres
    de pila entre caminantes activos. Si es ambiguo (matchea más de uno),
    se descarta y se loguea — nunca se adivina."""
    n = _normalizar(nombre_planilla)
    if not n:
        return None

    por_nombre = [c for c in caminantes if _normalizar(c.get("nombre_pila")) == n]
    if len(por_nombre) == 1:
        return por_nombre[0]
    if len(por_nombre) > 1:
        print(f"⚠️ '{nombre_planilla}' matchea por NOMBRE con más de un caminante activo — "
              f"fila descartada (ambigua): {[c['nombre'] for c in por_nombre]}")
        return None

    por_apellido = [c for c in caminantes if _normalizar(c.get("apellido")) == n]
    if len(por_apellido) == 1:
        return por_apellido[0]
    if len(por_apellido) > 1:
        print(f"⚠️ '{nombre_planilla}' matchea por APELLIDO con más de un caminante activo — "
              f"fila descartada (ambigua): {[c['nombre'] for c in por_apellido]}")
        return None

    return None


def main():
    hoy = datetime.now(TZ_AR).date()
    print(f"📅 Calculando recaudado para el día: {hoy.strftime('%d/%m/%Y')} (America/Argentina/Buenos_Aires)")

    url_planilla = obtener_url_planilla()
    spreadsheet_id, gid = parse_id_y_gid(url_planilla)
    print(f"📄 Planilla: {spreadsheet_id}  (gid={gid})")

    creds_info = json.loads(GOOGLE_SHEETS_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    # Resolver el nombre real de la hoja a partir del gid de la URL — no
    # asumimos el título ("Estado de cuenta") por si en algún momento cambia.
    hoja = next((h for h in sh.worksheets() if h.id == gid), None)
    if hoja is None:
        raise RuntimeError(f"No se encontró ninguna hoja con gid={gid} en la planilla.")
    print(f"📄 Hoja encontrada: {hoja.title!r}")

    # Columnas: A=Marca temporal, C=Tipo de rendición, D=IMPORTE, G=Vendedor
    valores = hoja.get("A:G", value_render_option="FORMATTED_VALUE")
    print(f"📊 {len(valores)} fila(s) leídas de la planilla.")

    caminantes = obtener_caminantes_activos()
    print(f"🚶 {len(caminantes)} caminante(s) activo(s) en Supabase.")
    if not caminantes:
        print("⚠️ No hay caminantes activos cargados en Supabase todavía (correr primero el Bot 1 — WalkerStock).")
        return

    totales = {}        # caminante_id -> suma de importe
    no_matcheados = {}   # nombre_planilla -> suma (solo para el log, no se guarda)
    filas_hoy = 0

    for fila in valores:
        if len(fila) < 7:
            continue
        fecha_txt = _col(fila, 0)
        tipo_txt = _col(fila, 2)
        importe_txt = _col(fila, 3)
        nombre_txt = _col(fila, 6)

        try:
            fecha_fila = datetime.strptime(fecha_txt.strip(), "%d/%m/%Y %H:%M:%S").date()
        except Exception:
            continue  # fila de encabezado, vacía, o con otro formato — se ignora
        if fecha_fila != hoy:
            continue
        filas_hoy += 1

        if _normalizar(tipo_txt) not in TIPOS_VALIDOS:
            continue

        importe = _parsear_importe(importe_txt)
        if importe is None:
            continue

        caminante = matchear_caminante(nombre_txt, caminantes)
        if caminante is None:
            no_matcheados[nombre_txt] = no_matcheados.get(nombre_txt, 0) + importe
            continue

        totales[caminante["id"]] = totales.get(caminante["id"], 0) + importe

    print(f"🔎 {filas_hoy} fila(s) de HOY encontradas en la planilla (de cualquier tipo).")
    if no_matcheados:
        print("⚠️ Nombres de la planilla que NO se pudieron matchear con ningún caminante activo "
              "(revisar manualmente — puede ser un caminante nuevo, deshabilitado, o un error de tipeo):")
        for nombre, monto in no_matcheados.items():
            print(f"   - {nombre!r}: ${monto:,.2f}")

    if not totales:
        print("ℹ️ No hay recaudado para registrar hoy.")
        return

    for caminante_id, monto in totales.items():
        payload = {"caminante_id": caminante_id, "fecha": hoy.isoformat(), "recaudado": round(monto, 2)}
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/caminantes_saldo_diario",
            headers={**headers_supabase(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload, timeout=30,
        )
        nombre = next((c["nombre"] for c in caminantes if c["id"] == caminante_id), caminante_id)
        if not r.ok:
            print(f"⚠️ No se pudo guardar el recaudado de {nombre}: {r.status_code} {r.text[:300]}")
        else:
            print(f"💾 {nombre}: recaudado hoy = ${monto:,.2f}")

    print("✅ Listo.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
