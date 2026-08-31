"""
Bot de Impresión de Etiquetas — Brother QL-800
=================================================
Reemplaza el intento de imprimir directo desde el navegador (b-PAC SDK +
extensión), que nunca logramos que se comunicara de forma confiable. En
cambio, corre en la PC del runner (mismo patrón que los bots de NEXO/ITEC) y
le habla a la impresora directo por USB con la librería `brother_ql` — sin
depender del driver de Brother, ni de ninguna extensión de navegador.

Etiqueta: 62x29mm (die-cut) → área imprimible real: 696x271 px (dato oficial
de la librería, no una estimación).

Layout (igual al diseño que ya usaban en P-touch Editor):
  - Lote (arriba, chico, izquierda)
  - NIM  (arriba, grande, derecha)
  - "Venc. DD/MM/AAAA" (grande, centrado)
  - ICCID / N° de SIM (chico, centrado)
  - Leyenda (más chica todavía, centrado, abajo)

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
from PIL import Image, ImageDraw, ImageFont

from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster
from brother_ql.devicedependent import label_type_specs

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CAJA_ID = os.environ["CAJA_ID"]
NUMERO_CAJA = os.environ["NUMERO_CAJA"]

# Impresora — confirmada en la propia PC del runner:
# USB\VID_04F9&PID_209B (Brother QL-800)
PRINTER_IDENTIFIER = "usb://0x04f9:0x209b"
PRINTER_MODEL = "QL-800"
LABEL_NAME = "62x29"

CARPETA_LOG = Path(__file__).resolve().parent
ARCHIVO_LOG_LOCAL = CARPETA_LOG / "log_fallos_etiquetas.txt"


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


def obtener_leyenda():
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/configuracion?id=eq.global&select=distribucion_config",
            headers=headers_supabase(), timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        raw = rows[0].get("distribucion_config") or "{}" if rows else "{}"
        cfg = json.loads(raw) if isinstance(raw, str) else raw
        return cfg.get("etiqueta_leyenda") or "USB S.R.L. 0223-5403796"
    except Exception:
        return "USB S.R.L. 0223-5403796"


def obtener_sims_a_imprimir():
    # Traer la caja (para ver si hay una selección puntual de ICCIDs)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_cajas?id=eq.{CAJA_ID}&select=etiquetas_iccids_seleccion",
        headers=headers_supabase(), timeout=30,
    )
    r.raise_for_status()
    filas = r.json()
    seleccion = filas[0].get("etiquetas_iccids_seleccion") if filas else None

    r2 = requests.get(
        f"{SUPABASE_URL}/rest/v1/distribucion_sims?caja_id=eq.{CAJA_ID}&select=iccid,nim,lote,fecha_vencimiento",
        headers=headers_supabase(), timeout=30,
    )
    r2.raise_for_status()
    sims = r2.json()

    if seleccion:
        seleccion_set = set(seleccion)
        sims = [s for s in sims if s["iccid"] in seleccion_set]

    return sims


def marcar_error(mensaje):
    print(f"❌ ERROR: {mensaje}", file=sys.stderr)
    _log_local(mensaje)
    sys.exit(1)


def _fuente(tamano, negrita=False):
    """DejaVuSans viene instalada por default en los runners de GitHub/la
    mayoría de instalaciones Python con Pillow — si no la encuentra, cae a la
    fuente default de Pillow (más fea, pero no rompe el bot)."""
    nombres = ["DejaVuSans-Bold.ttf" if negrita else "DejaVuSans.ttf"]
    for nombre in nombres:
        try:
            return ImageFont.truetype(nombre, tamano)
        except Exception:
            continue
    return ImageFont.load_default()


def generar_imagen_etiqueta(sim, leyenda):
    ancho, alto = label_type_specs[LABEL_NAME]["dots_printable"]
    img = Image.new("RGB", (ancho, alto), "white")
    draw = ImageDraw.Draw(img)

    lote = sim.get("lote") or "—"
    nim = sim.get("nim") or "—"
    iccid = sim.get("iccid") or "—"
    if sim.get("fecha_vencimiento"):
        try:
            f = datetime.strptime(sim["fecha_vencimiento"][:10], "%Y-%m-%d")
            vencimiento = f"Venc. {f.strftime('%d/%m/%Y')}"
        except Exception:
            vencimiento = f"Venc. {sim['fecha_vencimiento']}"
    else:
        vencimiento = "Venc. —"

    margen = 14
    # Fila superior: Lote (izquierda, chico) / NIM (derecha, grande)
    draw.text((margen, 8), lote, font=_fuente(28, negrita=True), fill="black")
    f_nim = _fuente(48, negrita=True)
    ancho_nim = draw.textlength(nim, font=f_nim)
    draw.text((ancho - margen - ancho_nim, 4), nim, font=f_nim, fill="black")

    # Vencimiento — grande, centrado
    f_venc = _fuente(34, negrita=True)
    ancho_venc = draw.textlength(vencimiento, font=f_venc)
    draw.text(((ancho - ancho_venc) / 2, 92), vencimiento, font=f_venc, fill="black")

    # ICCID — chico, centrado
    f_iccid = _fuente(22)
    ancho_iccid = draw.textlength(iccid, font=f_iccid)
    draw.text(((ancho - ancho_iccid) / 2, 150), iccid, font=f_iccid, fill="black")

    # Leyenda — más chica todavía, centrado, abajo
    f_leyenda = _fuente(18)
    ancho_leyenda = draw.textlength(leyenda, font=f_leyenda)
    draw.text(((ancho - ancho_leyenda) / 2, 190), leyenda, font=f_leyenda, fill="black")

    return img


def imprimir_etiqueta(img):
    qlr = BrotherQLRaster(PRINTER_MODEL)
    qlr.exception_on_warning = True
    instrucciones = convert(qlr=qlr, images=[img], label=LABEL_NAME, rotate="0", threshold=70.0, dither=False, compress=False, red=False, cut=True)
    send(instructions=instrucciones, printer_identifier=PRINTER_IDENTIFIER, backend_identifier="pyusb", blocking=True)


def main():
    sims = obtener_sims_a_imprimir()
    if not sims:
        marcar_error(f"No se encontraron SIMs para imprimir en la caja {NUMERO_CAJA} (¿selección vacía o caja sin SIMs cargadas?).")

    leyenda = obtener_leyenda()
    print(f"🖨️ Imprimiendo {len(sims)} etiqueta(s) de la caja {NUMERO_CAJA}...")

    impresas, fallidas = 0, 0
    for sim in sims:
        try:
            img = generar_imagen_etiqueta(sim, leyenda)
            imprimir_etiqueta(img)
            impresas += 1
        except Exception as e:
            fallidas += 1
            _log_local(f"Error imprimiendo SIM {sim.get('iccid')}: {type(e).__name__}: {e}")
            print(f"⚠️ Error imprimiendo SIM {sim.get('iccid')}: {e}", file=sys.stderr)

    print(f"✅ Impresión terminada: {impresas} etiqueta(s) ok, {fallidas} con error.")
    if impresas == 0:
        marcar_error(f"Ninguna etiqueta se imprimió correctamente ({fallidas} error(es)). Revisar log local y conexión de la impresora.")


if __name__ == "__main__":
    main()
