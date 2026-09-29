"""
Renovador de Sesión — GestionSLA (Webcom + NEXO)
=================================================
Doble click para abrir. No requiere consola.

Webcom y NEXO comparten el mismo login corporativo (mismo tenant de Azure AD) —
una sola sesión sirve para los dos, solo cambia DÓNDE se inyecta la cookie.
Por eso ya no hace falta elegir "sistema": un solo botón renueva una vez y
guarda el resultado en los 3 puntos que lo necesitan:
  1. Localmente en esta PC (los dos archivos: webcom_cookies.json y nexo_cookies.json)
  2. Repo bot-webcom (para el Bot Webcom)
  3. Repo mainSLA.sis (para los Bots NEXO/ITEC)

Por qué hace falta un humano en el medio: el login pasa por Microsoft, y aunque
esta sesión en particular no pide MFA, sigue siendo un login real — un bot 100%
automático (headless) no puede completarlo solo con seguridad.
"""

import customtkinter as ctk
import threading, json, asyncio, requests, base64, os, sys
from datetime import datetime
from pathlib import Path

# ── Configuración general ───────────────────────────────────────────────────
CONFIG_FILE  = Path(os.getenv("APPDATA", ".")) / "GestionSLA" / "config.json"
SUPABASE_URL = "https://iebfyjbkmjuicrrbezbi.supabase.co"
# Seguridad: el programa NO lleva ninguna clave secreta ni token. Se identifica
# con el usuario y la contraseña de GestionLTA contra una función de Supabase
# ("renovador-sesion") que valida al usuario y hace del lado del servidor todo
# lo que necesita la clave secreta o los tokens de GitHub. La única clave que
# viaja en el programa es la PÚBLICA (publishable), que es pública por diseño.
APP_VERSION  = "2.0.0"
SUPABASE_PUBLICA = "sb_publishable_zi7bb1bTdazL5dwGu7-t8w_xp-MoPnl"
URL_FUNCION  = f"{SUPABASE_URL}/functions/v1/renovador-sesion"


def _leer_config_local() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _guardar_config_local(datos: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    actual = _leer_config_local()
    actual.update(datos)
    for k in [k for k, v in actual.items() if v is None]:
        actual.pop(k)
    CONFIG_FILE.write_text(json.dumps(actual, ensure_ascii=False, indent=2), encoding="utf-8")


# ── "Recordar contraseña": cifrada con DPAPI de Windows (solo la puede
# descifrar el mismo usuario de Windows en la misma PC) ───────────────────────
def _dpapi(data: bytes, cifrar: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    entrada = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    salida = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if cifrar else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(entrada), None, None, None, None, 0, ctypes.byref(salida)):
        raise OSError("DPAPI falló")
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(salida.pbData)


def _guardar_credenciales(usuario: str, password: str, recordar: bool):
    datos = {"gestion_usuario": usuario, "gestion_pass": None, "supabase_key": None}
    if recordar:
        try:
            datos["gestion_pass"] = base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode()
        except Exception:
            pass
    _guardar_config_local(datos)


def _credenciales_guardadas():
    c = _leer_config_local()
    usuario = c.get("gestion_usuario", "")
    password = ""
    if c.get("gestion_pass"):
        try:
            password = _dpapi(base64.b64decode(c["gestion_pass"]), False).decode("utf-8")
        except Exception:
            password = ""
    return usuario, password


def llamar_funcion(accion: str, usuario: str, password: str, **extra) -> dict:
    try:
        r = requests.post(
            URL_FUNCION,
            headers={"apikey": SUPABASE_PUBLICA, "Content-Type": "application/json"},
            json={"accion": accion, "usuario": usuario, "password": password, **extra},
            timeout=60,
        )
        try:
            datos = r.json()
        except Exception:
            datos = {"ok": False, "error": f"Respuesta inesperada del servidor ({r.status_code})"}
        datos["_status"] = r.status_code
        return datos
    except Exception as e:
        return {"ok": False, "error": f"Sin conexión con el sistema: {e}", "_status": 0}


def _ruta_recurso(nombre: str) -> Path:
    """Archivos que viajan dentro del .exe (ícono)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / nombre

URL_LOGIN = "https://claroaup.sharepoint.com/sites/webcom/SitePages/Inicio.aspx"  # Webcom: no pide MFA, login más simple — sirve para las dos sesiones
# La cookie de NEXO tiene que capturarse VISITANDO NEXO de verdad — antes esto
# no pasaba: solo se entraba a SharePoint y se subía esa misma lista de
# cookies a los dos repos por igual. Como Azure AD App Proxy (msappproxy.net)
# emite una cookie de sesión PROPIA por aplicación (no una genérica de
# tenant), el "rtFa" que terminaba en nexo_cookies.json era el de SharePoint,
# no el de NEXO — por eso el chequeo local decía "sesión activa" con fecha
# real (esa cookie sí es válida, para SharePoint) pero el bot, que entra a
# ESTA url puntual, se encontraba sin sesión ahí.
URL_NEXO_HOME = "https://nexostealth-claroaup.msappproxy.net/"  # misma URL que usa bot_nexo_resultado.py (URL_NEXO_HOME)

# Los 3 puntos donde tiene que quedar guardada la MISMA cookie
PUNTOS = [
    {
        "id": "local",
        "titulo": "Guardado local (esta PC)",
    },
    {
        "id": "bot-webcom",
        "titulo": "Repo bot-webcom",
        "repo_key": "webcom_repo",
        "token_key": "webcom_gh_token",
        "cookies_repo_path": "webcom_cookies.json",
    },
    {
        "id": "mainSLA.sis",
        "titulo": "Repo mainSLA.sis (NEXO/ITEC)",
        "repo_key": "gh_repo",
        "token_key": "gh_token",
        "cookies_repo_path": "nexo_cookies.json",
    },
]

ARCHIVO_LOCAL_WEBCOM = Path(os.getenv("APPDATA", ".")) / "GestionSLA" / "webcom_cookies.json"
ARCHIVO_LOCAL_NEXO   = Path(os.getenv("APPDATA", ".")) / "GestionSLA" / "nexo_cookies.json"

# Paleta para el efecto "RGB arcoíris" en los tildes de éxito
COLORES_ARCOIRIS = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4", "#3b82f6", "#a855f7", "#ec4899"]


# ── Helpers genéricos ────────────────────────────────────────────────────────
def verificar_expiracion(cookies, nombre_cookie="rtFa", dominio_contiene=None):
    # Antes chequeaba "ESTSAUTHPERSISTENT", que no refleja el vencimiento
    # real de la sesión — RenovarSesion.pyw (el que mostraba bien el
    # estado, con fecha y hora exactas) usa "rtFa", que es la cookie que
    # NEXO de verdad usa para cortar la sesión.
    #
    # Ahora la lista de cookies puede traer MÁS de una "rtFa" (una por
    # dominio visitado — SharePoint y NEXO). Si se pasa dominio_contiene,
    # se prioriza la cookie de ESE dominio puntual en vez de quedarse con
    # la primera que aparezca en la lista, que podía ser la de otro lado.
    candidatas = [c for c in cookies if c.get("name") == nombre_cookie and c.get("expires", 0) > 0]
    if dominio_contiene:
        de_ese_dominio = [c for c in candidatas if dominio_contiene in (c.get("domain") or "")]
        if de_ese_dominio:
            candidatas = de_ese_dominio
    for c in candidatas:
        expira = datetime.fromtimestamp(c["expires"])
        horas  = (expira - datetime.now()).total_seconds() / 3600
        if horas <= 0:
            return False, f"Sesión EXPIRADA — {expira.strftime('%d/%m/%Y %H:%M')}"
        dias, resto = int(horas // 24), int(horas % 24)
        label = f"{dias}d {resto}hs" if dias > 0 else f"{horas:.0f}hs"
        return True, f"Sesión activa — expira en {label} ({expira.strftime('%d/%m/%Y %H:%M')})"
    return False, "Sin información de sesión"


async def renovar_cookies_async(log_fn, url, usuario="", password=""):
    from playwright.async_api import async_playwright
    log_fn("🌐 Abriendo browser...")
    async with async_playwright() as p:
        # Usa el navegador que ya tiene la PC (Edge viene con Windows; si no,
        # Chrome). Así el instalador no necesita traer un Chromium propio.
        browser = None
        for canal in ("msedge", "chrome"):
            try:
                browser = await p.chromium.launch(headless=False, channel=canal)
                log_fn(f"🧭 Usando {'Microsoft Edge' if canal == 'msedge' else 'Google Chrome'}")
                break
            except Exception:
                continue
        if browser is None:
            log_fn("❌ No se encontró Microsoft Edge ni Google Chrome en esta PC")
            return None
        ctx  = await browser.new_context()
        page = await ctx.new_page()
        log_fn("⏳ Navegando...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(4000)
        if usuario and "login" in page.url.lower():
            try:
                await page.fill('input[type="email"]', usuario)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2000)
                log_fn("📧 Usuario ingresado automáticamente")
                if password:
                    try:
                        await page.fill('input[type="password"]', password)
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(2000)
                        log_fn("🔑 Contraseña ingresada automáticamente")
                    except Exception:
                        pass
            except Exception:
                pass
        # Esperar login (por si de todas formas pide algo) hasta 3 minutos
        for i in range(36):
            if "login" not in page.url.lower():
                break
            log_fn("🔐 Esperando que completes el login..." if i == 0 else f"   Esperando... ({i*5}s)")
            await page.wait_for_timeout(5000)
        if "login" in page.url.lower():
            await browser.close()
            log_fn("❌ Tiempo de espera agotado")
            return None

        # Hasta acá solo se visitó SharePoint — el browser todavía no tiene
        # ninguna cookie propia de NEXO (los cookies son por dominio, y ese
        # dominio nunca se visitó). Se entra ahora a NEXO en el MISMO
        # contexto ya autenticado — la idea era que el SSO pasara solo, pero
        # en la práctica NEXO SÍ pide login de nuevo — así que ahora se
        # completa con la MISMA cuenta corporativa (usuario/password), igual
        # que se hace para SharePoint arriba.
        log_fn("🌐 Entrando a NEXO para capturar su propia cookie de sesión...")
        try:
            await page.goto(URL_NEXO_HOME, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            if usuario and "login" in page.url.lower():
                try:
                    await page.fill('input[type="email"]', usuario)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(2000)
                    log_fn("📧 Usuario ingresado automáticamente en NEXO")
                    if password:
                        try:
                            await page.fill('input[type="password"]', password)
                            await page.keyboard.press("Enter")
                            await page.wait_for_timeout(2000)
                            log_fn("🔑 Contraseña ingresada automáticamente en NEXO")
                        except Exception:
                            pass
                except Exception:
                    pass
            if "login" in page.url.lower():
                log_fn("🔐 Esperando que NEXO termine de loguear...")
                for i in range(36):
                    if "login" not in page.url.lower():
                        break
                    log_fn(f"   Esperando NEXO... ({i*5}s)" if i > 0 else "   Esperando NEXO...")
                    await page.wait_for_timeout(5000)
                if "login" in page.url.lower():
                    log_fn("⚠️ NEXO se quedó pidiendo login — si la cuenta pide algo distinto a usuario/contraseña ahí (MFA, otra pantalla), esa cookie puede no quedar bien capturada.")
        except Exception as e:
            log_fn(f"⚠️ No se pudo entrar a NEXO para su captura puntual: {e}")

        log_fn("✅ Sesión activa — capturando cookies...")
        await page.wait_for_timeout(2000)
        cookies = await ctx.cookies()
        await browser.close()
        log_fn(f"🍪 {len(cookies)} cookies capturadas")
        return cookies


# ── GUI ───────────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"Renovar Sesión — GestionSLA  v{APP_VERSION}")
        try:
            self.iconbitmap(str(_ruta_recurso("icono.ico")))
        except Exception:
            pass
        self.geometry("500x600")
        self.resizable(False, False)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.cfg_sb_raw = {}
        self._usuario, self._password, self._recordar = "", "", True
        self._checks_labels = {}
        self._animaciones_activas = {}
        self._build_ui()
        self.after(300, self._asegurar_clave_y_cargar)

    def _asegurar_clave_y_cargar(self):
        """Pide usuario/contraseña de GestionLTA si no están guardados y carga la config."""
        usuario, password = _credenciales_guardadas()
        if usuario and password:
            self._usuario, self._password = usuario, password
            self._cargar_desde_supabase()
        else:
            self._pedir_login(usuario)

    def _pedir_login(self, usuario_previo="", mensaje=""):
        win = ctk.CTkToplevel(self)
        win.title("Ingresá con tu usuario de GestionLTA")
        win.geometry("380x330")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        ctk.CTkLabel(win, text="🔐  Ingresá con tu usuario de GestionLTA",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(18, 4))
        ctk.CTkLabel(win, text=mensaje or "El mismo correo y contraseña que usás en el sistema.",
                     font=ctk.CTkFont(size=11), text_color="#f87171" if mensaje else "gray",
                     wraplength=330).pack(pady=(0, 10))
        e_user = ctk.CTkEntry(win, placeholder_text="Correo", width=300)
        e_user.pack(pady=4)
        if usuario_previo:
            e_user.insert(0, usuario_previo)
        e_pass = ctk.CTkEntry(win, placeholder_text="Contraseña", show="•", width=300)
        e_pass.pack(pady=4)
        recordar = ctk.CTkCheckBox(win, text="Recordar en esta PC")
        recordar.select()
        recordar.pack(pady=8)

        def entrar(_=None):
            u, p = e_user.get().strip(), e_pass.get()
            if not u or not p:
                return
            self._usuario, self._password, self._recordar = u, p, bool(recordar.get())
            win.grab_release()
            win.destroy()
            self._cargar_desde_supabase()

        ctk.CTkButton(win, text="Ingresar", width=300, height=38, fg_color="#7c3aed",
                      hover_color="#6d28d9", command=entrar).pack(pady=(6, 4))
        e_pass.bind("<Return>", entrar)
        (e_pass if usuario_previo else e_user).focus()

    def _build_ui(self):
        h = ctk.CTkFrame(self, fg_color="#1e1b4b", corner_radius=0)
        h.pack(fill="x")
        ctk.CTkLabel(h, text="🤖  Renovar Sesión",
                     font=ctk.CTkFont(size=17, weight="bold"),
                     text_color="white").pack(pady=14)

        # Estado sesión
        f1 = ctk.CTkFrame(self, corner_radius=10)
        f1.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(f1, text="Estado de la sesión:",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=14, pady=(10, 2))
        self.lbl_estado = ctk.CTkLabel(f1, text="Verificando...",
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        wraplength=440)
        self.lbl_estado.pack(anchor="w", padx=14, pady=(0, 10))

        # Config (cargada desde Supabase)
        f2 = ctk.CTkFrame(self, corner_radius=10)
        f2.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(f2, text="Configuración (cargada automáticamente desde el sistema):",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=14, pady=(10, 2))
        self.lbl_config = ctk.CTkLabel(f2, text="Conectando con Supabase...",
                                        font=ctk.CTkFont(size=11), text_color="#94a3b8",
                                        wraplength=440)
        self.lbl_config.pack(anchor="w", padx=14, pady=(0, 10))

        # Los 3 puntos de guardado, con su tilde de éxito
        f3 = ctk.CTkFrame(self, corner_radius=10)
        f3.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(f3, text="Puntos donde se guarda la sesión:",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=14, pady=(10, 4))
        for punto in PUNTOS:
            fila = ctk.CTkFrame(f3, fg_color="transparent")
            fila.pack(fill="x", padx=14, pady=2)
            check = ctk.CTkLabel(fila, text="○", font=ctk.CTkFont(size=15, weight="bold"),
                                  text_color="#4b5563", width=24)
            check.pack(side="left")
            ctk.CTkLabel(fila, text=punto["titulo"], font=ctk.CTkFont(size=12),
                         text_color="#cbd5e1").pack(side="left", padx=(4, 0))
            self._checks_labels[punto["id"]] = check
        f3.pack_configure(pady=(6, 10))

        # Botón principal
        self.btn = ctk.CTkButton(
            self, text="🔄   RENOVAR SESIÓN",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=52, corner_radius=10,
            fg_color="#7c3aed", hover_color="#6d28d9",
            command=self._iniciar
        )
        self.btn.pack(fill="x", padx=20, pady=10)

        # Mensaje de éxito final (arcoíris cuando los 3 puntos salen bien)
        self.lbl_exito = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_exito.pack(pady=(0, 6))

        # Log
        ctk.CTkLabel(self, text="Registro:", font=ctk.CTkFont(size=10),
                     text_color="gray").pack(anchor="w", padx=20)
        self.log = ctk.CTkTextbox(self, height=150,
                                   font=ctk.CTkFont(family="Courier", size=11))
        self.log.pack(fill="x", padx=20, pady=(2, 0))
        self.log.configure(state="disabled")

        ctk.CTkLabel(self, text="GestionSLA © 2026",
                     font=ctk.CTkFont(size=9), text_color="gray").pack(pady=8)

    def _log(self, msg):
        def _do():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _do)

    def _set_estado(self, texto, color="#94a3b8"):
        self.after(0, lambda: self.lbl_estado.configure(text=texto, text_color=color))

    def _cargar_desde_supabase(self):
        def _do():
            self._log("🔗 Conectando con GestionLTA...")
            r = llamar_funcion("config", self._usuario, self._password)
            if r.get("ok"):
                self.cfg_sb_raw = r
                _guardar_credenciales(self._usuario, self._password, self._recordar)
                self._log(f"✅ Hola {r.get('nombre') or self._usuario} — configuración cargada")
            else:
                self.cfg_sb_raw = {}
                self._log(f"⚠️ {r.get('error', 'No se pudo conectar')}")
                if r.get("_status") == 401:
                    _guardar_config_local({"gestion_pass": None})
                    self.after(0, lambda: self._pedir_login(self._usuario, "Usuario o contraseña incorrectos. Probá de nuevo."))
            self._actualizar_panel_config()
            self._verificar_estado_local()
        threading.Thread(target=_do, daemon=True).start()

    def _actualizar_panel_config(self):
        cfg = self.cfg_sb_raw
        gh_user = cfg.get("gh_user", "")
        ok_webcom = bool(cfg.get("listo_webcom"))
        ok_nexo   = bool(cfg.get("listo_nexo"))
        if gh_user and (ok_webcom or ok_nexo):
            info = f"GitHub: {gh_user}\n"
            info += f"bot-webcom: {'✅' if ok_webcom else '❌ sin repo/token'}   ·   mainSLA.sis: {'✅' if ok_nexo else '❌ sin repo/token'}"
            usuario = cfg.get("webcom_email", "")
            if usuario:
                info += f"\nUsuario: {usuario} (se completa solo)"
            self.lbl_config.configure(text=info, text_color="#4ade80")
        else:
            self.lbl_config.configure(
                text="⚠️ Sin conexión con GestionLTA o faltan datos de GitHub en la configuración del sistema",
                text_color="#f59e0b")

    def _verificar_estado_local(self):
        if ARCHIVO_LOCAL_NEXO.exists():
            try:
                cookies = json.loads(ARCHIVO_LOCAL_NEXO.read_text(encoding="utf-8"))
                activa, msg = verificar_expiracion(cookies, dominio_contiene="msappproxy")
                self._set_estado(("✅ " if activa else "🔴 ") + msg,
                                  "#4ade80" if activa else "#f87171")
                return
            except Exception:
                pass
        self._set_estado("⚠️ Sin sesión guardada — hacé click en Renovar", "#f59e0b")

    def _marcar_check(self, punto_id, ok):
        """Tilde en verde con un pulso arcoíris breve si salió bien; X roja si no."""
        label = self._checks_labels.get(punto_id)
        if not label:
            return
        if ok:
            self.after(0, lambda: label.configure(text="✓"))
            self._pulso_arcoiris(label, ciclos=10)
        else:
            self.after(0, lambda: label.configure(text="✗", text_color="#f87171"))

    def _pulso_arcoiris(self, label, ciclos=10, i=0):
        if i >= ciclos:
            self.after(0, lambda: label.configure(text_color="#4ade80"))  # se asienta en verde
            return
        color = COLORES_ARCOIRIS[i % len(COLORES_ARCOIRIS)]
        self.after(0, lambda: label.configure(text_color=color))
        self.after(120, lambda: self._pulso_arcoiris(label, ciclos, i + 1))

    def _mensaje_exito_arcoiris(self, texto, ciclos=16, i=0):
        if i == 0:
            self.after(0, lambda: self.lbl_exito.configure(text=texto))
        if i >= ciclos:
            self.after(0, lambda: self.lbl_exito.configure(text_color="#4ade80"))
            return
        color = COLORES_ARCOIRIS[i % len(COLORES_ARCOIRIS)]
        self.after(0, lambda: self.lbl_exito.configure(text_color=color))
        self.after(110, lambda: self._mensaje_exito_arcoiris(texto, ciclos, i + 1))

    def _resetear_checks(self):
        for label in self._checks_labels.values():
            label.configure(text="○", text_color="#4b5563")
        self.lbl_exito.configure(text="")

    def _iniciar(self):
        cfg = self.cfg_sb_raw
        if not cfg.get("ok"):
            self._log("❌ Primero tenés que ingresar con tu usuario de GestionLTA")
            self._pedir_login(self._usuario)
            return
        self._resetear_checks()
        self.btn.configure(state="disabled", text="⏳  Renovando...")
        self._log("─" * 42)
        self._log(f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        threading.Thread(target=self._proceso, daemon=True).start()

    def _proceso(self):
        cfg = self.cfg_sb_raw
        try:
            usuario  = cfg.get("webcom_email", "")
            password = cfg.get("webcom_password", "")
            loop = asyncio.new_event_loop()
            cookies = loop.run_until_complete(renovar_cookies_async(
                self._log, URL_LOGIN, usuario, password
            ))
            loop.close()

            if not cookies:
                self.after(0, lambda: self.btn.configure(state="normal", text="🔄   RENOVAR SESIÓN"))
                return

            activa, msg = verificar_expiracion(cookies, dominio_contiene="msappproxy")
            self._set_estado(("✅ " if activa else "🔴 ") + msg,
                              "#4ade80" if activa else "#f87171")
            self._log(f"📅 {msg}")
            cookies_json = json.dumps(cookies, ensure_ascii=False)

            # ── Punto 1: guardado local — SIEMPRE los dos archivos, sin importar
            # con qué URL se logueó, porque es la MISMA sesión para ambos sistemas.
            try:
                for archivo in (ARCHIVO_LOCAL_WEBCOM, ARCHIVO_LOCAL_NEXO):
                    archivo.parent.mkdir(parents=True, exist_ok=True)
                    archivo.write_text(cookies_json, encoding="utf-8")
                self._log("💾 Cookies guardadas localmente (Webcom y NEXO)")
                ok_local = True
            except Exception as e:
                self._log(f"❌ No se pudo guardar localmente: {e}")
                ok_local = False
            self._marcar_check("local", ok_local)

            # ── Puntos 2 y 3: la función de GestionLTA sube las cookies a los
            # dos repos con los tokens del sistema (el programa no los ve nunca).
            resultados = {"local": ok_local}
            self._log("📤 Subiendo la sesión a los repos (bot-webcom y mainSLA.sis)...")
            r = llamar_funcion("subir", self._usuario, self._password, cookies=cookies)
            detalle = r.get("resultados") or {}
            for punto in PUNTOS[1:]:
                res = detalle.get(punto["id"]) or {"ok": False, "msg": f"❌ {r.get('error', 'Sin respuesta')}"}
                self._log(res.get("msg", ""))
                self._marcar_check(punto["id"], bool(res.get("ok")))
                resultados[punto["id"]] = bool(res.get("ok"))

            if all(resultados.values()):
                self._mensaje_exito_arcoiris("🎉 ¡Sesión renovada e insertada en los 3 puntos con éxito!")
            else:
                fallidos = [k for k, v in resultados.items() if not v]
                self._log(f"⚠️ No se completaron todos los puntos: {', '.join(fallidos)}")

        except Exception as e:
            self._log(f"❌ Error inesperado: {str(e)}")
        finally:
            self.after(0, lambda: self.btn.configure(
                state="normal", text="🔄   RENOVAR SESIÓN"))


if __name__ == "__main__":
    app = App()
    app.mainloop()
