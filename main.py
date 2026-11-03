# -*- coding: utf-8 -*-
"""
⚡ SA SHOP — Tienda automática 24/7 para Telegram
💎 Pagos verificados con Binance Pay (historial de transacciones recibidas)

Variables de entorno requeridas (Railway -> Variables):
  TELEGRAM_TOKEN, ADMIN_ID, ADMIN_USERNAME, BINANCE_PAY_ID,
  BINANCE_API_KEY, BINANCE_SECRET_KEY, DATABASE_URL
Opcionales:
  BINANCE_BASE_URL, QR_URL, TITULAR, BOT_THREADS (16), DB_POOL_MAX (20),
  ICONOS_PERSONALIZADOS (1 = usar íconos premium si los configuras con /iconos, 0 = nunca)

Modalidades de entrega:
  email_pass  -> el stock son líneas  correo@dominio.com:contraseña  (formato validado)
  key         -> el stock son keys (Windows, Office…) o texto normal, una por línea (o bloques separados por ---)
  link        -> el stock son links   https://...                    (formato validado)
  correo      -> el cliente deja su correo; el admin envía la invitación a mano
  dominio     -> el cliente deja su @usuario de Telegram; el admin entrega a mano
Stock infinito (solo email_pass / link): se guarda UN contenido (link o texto) y se entrega igual a todos.
"""

import io
import os
import re
import json
import time
import hmac
import html
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from threading import Thread, Lock
from urllib.parse import urlencode

import psycopg2
import psycopg2.errors
from psycopg2 import pool as pg_pool
from psycopg2.extras import execute_values
import requests
import telebot
from telebot import apihelper
from telebot.apihelper import ApiTelegramException
from flask import Flask
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton as _TgButton,
    BotCommand,
    BotCommandScopeChat,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sashop")
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# =====================================================================
# 🔐 CONFIGURACIÓN
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "uservipxd")
BINANCE_PAY_ID = os.environ.get("BINANCE_PAY_ID", "884195829")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://api.binance.com")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
QR_URL = os.environ.get("QR_URL", "https://i.ibb.co/3yz54KHy/qr-binance.jpg")
TITULAR = os.environ.get("TITULAR", "Saul")
BOT_THREADS = int(os.environ.get("BOT_THREADS", "16"))
CANAL_REFERENCIAS = os.environ.get("CANAL_REFERENCIAS", "")      # opcional: ID/@usuario del canal (también se puede fijar con /canal)
ZONA_HORARIA_UTC = float(os.environ.get("ZONA_HORARIA_UTC", "-5"))  # Perú = -5
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "20"))

CACHE_TTL = 15               # segundos (el caché además se invalida al vender/cargar)
DIAS_HISTORIAL = 3           # días hacia atrás que se buscan en el historial de Binance
MAX_CANT_INFINITO = 10       # cantidad máxima por compra en productos de stock infinito
LIMITE_DESC = 1500           # caracteres máximos de la descripción
LIMITE_CONTENIDO = 3000      # caracteres máximos del contenido infinito
LIMITE_KEY = 500             # caracteres máximos de cada key / texto de stock
ICONOS_ACTIVOS_INICIAL = os.environ.get("ICONOS_PERSONALIZADOS", "1") != "0"

if not TELEGRAM_TOKEN or not DATABASE_URL:
    raise SystemExit("❌ Faltan las variables TELEGRAM_TOKEN y/o DATABASE_URL (Railway -> Variables).")

# ---------------------------------------------------------------------
# Modalidades
# ---------------------------------------------------------------------
MODALIDADES = {
    "email_pass": "Email:Pass · cuenta lista para usar",
    "link": "Link de activación",
    "key": "Key de activación (Windows, Office…)",
    "correo": "Activación a correo del cliente",
    "dominio": "A dominio · entrega manual",
}
MOD_CORTO = {"email_pass": "email:pass", "link": "link", "key": "key", "correo": "a correo", "dominio": "a dominio"}
ICONO_MOD = {"email_pass": "stock", "link": "link", "key": "key", "correo": "correo", "dominio": "dominio"}
MANUALES = ("correo", "dominio")

# Catálogo inicial (solo se usa si la base de datos está vacía)
CATALOGO_INICIAL = {
    "Apple TV 1 Mes - Link activación": (1.00, "link", False),
    "Canva Pro a correo 12 meses": (1.00, "correo", False),
    "Crunchyroll Link 1 mes - card required": (0.50, "link", False),
    "Gemini Pro 18 month Link": (3.00, "link", False),
    "METODO APPLE TV": (3.50, "link", True),
    "METODO GEMINI PRO + 5TB x 12 Meses": (5.00, "link", True),
    "Paramount+ 1 Mes": (2.00, "email_pass", False),
    "Prime Video 6 Meses": (2.00, "email_pass", False),
    "Spotify Individual": (1.30, "email_pass", False),
    "Surf Shark VPN 10 days": (1.00, "email_pass", False),
    "Surf Shark VPN 12 meses": (9.00, "email_pass", False),
}

# ---------------------------------------------------------------------
# Validación del stock
# ---------------------------------------------------------------------
RE_EMAIL_PASS = re.compile(r"^[^\s:@]+@[^\s:@]+\.[^\s:@]+:\S+$")
RE_LINK = re.compile(r"^https://\S+$", re.I)
RE_CORREO = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
RE_USUARIO_TG = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")

# =====================================================================
# 📝 DESCRIPCIONES AUTOMÁTICAS (puedes cambiarlas con /modificar_descripcion)
# =====================================================================
# Sintaxis de la descripción:  "+ texto" = ✅   "- texto" = ❌   "**texto**" = negrita
_PERFILES = {
    "surf": ("Surfshark VPN", "Cuenta privada para 10 dispositivos. Empieza a usarla inmediatamente después de la compra.", "10"),
    "prime": ("Prime Video", "Disfruta Prime Video con una cuenta lista para usar desde el primer momento.", None),
    "spotify": ("Spotify Premium", "Música sin anuncios en tu cuenta. Empieza a disfrutarla apenas la recibes.", None),
    "paramount": ("Paramount+", "Acceso a Paramount+ con cuenta lista para usar.", None),
    "crunchy": ("Crunchyroll", "Anime sin límites. Activa tu acceso siguiendo el link entregado (puede requerir tarjeta para activar).", None),
    "apple": ("Apple TV+", "Disfruta Apple TV+ activándolo con tu propio link.", None),
    "gemini": ("Gemini Pro", "Activa Gemini Pro (Google AI) con tu link de activación personal.", None),
    "canva": ("Canva Pro", "Canva Pro activado directamente en tu correo.", None),
    "youtube": ("YouTube Premium", "YouTube Premium sin anuncios, agregado a nuestro plan familiar mediante tu correo.", None),
    "deezer": ("Deezer Premium", "Deezer Premium agregado a nuestro plan mediante tu correo.", None),
    "max": ("Max", "Cuenta completa de Max lista para usar.", None),
}


def descripcion_auto(nombre, modalidad, infinito=False):
    n = nombre.casefold()
    clave = next((k for k in _PERFILES if k in n), None)
    servicio, blurb, disp = _PERFILES.get(clave, (nombre, f"Acceso a {nombre}.", None))
    if "metodo" in n or "método" in n:
        titulo = f"Método {servicio}"
        blurb = f"Método paso a paso para conseguir {servicio}. Recibes el acceso apenas se confirma el pago."
    else:
        titulo = servicio
    formato = {
        "email_pass": "Email:Pass (cuenta completa)",
        "link": "Link de activación" if not infinito else "Link / guía de acceso",
        "key": "Key de activación / código",
        "correo": "Activación directa a tu correo",
        "dominio": "A dominio (entrega manual)",
    }.get(modalidad, "Cuenta")
    tiempo = {
        "email_pass": "Inmediata, una vez confirmado el pago",
        "link": "Inmediata, una vez confirmado el pago",
        "key": "Inmediata, una vez confirmado el pago",
        "correo": "Manual: el administrador te envía la invitación tras tu pago",
        "dominio": "Manual: coordinas con el administrador tras tu pago",
    }.get(modalidad, "Inmediata")
    ls = [f"**{titulo}**", blurb, "", f"**Formato de entrega:** {formato}", f"**Tiempo de entrega:** {tiempo}"]
    if disp:
        ls.append(f"**Dispositivos:** {disp}")
    ls.append("")
    if modalidad == "email_pass":
        ls += ["+ Email y Password listos para usar", "+ Entrega automática después de la confirmación del pago",
               "- **Sin garantía** después de la entrega"]
    elif modalidad == "link":
        ls += ["+ Link de activación personal" if not infinito else "+ Acceso inmediato tras el pago",
               "+ Entrega automática después de la confirmación del pago",
               "- **Sin garantía** después de la entrega"]
    elif modalidad == "key":
        ls += ["+ Key de activación lista para usar", "+ Entrega automática después de la confirmación del pago",
               "- **Sin garantía** después de la entrega"]
    elif modalidad == "correo":
        ls += ["+ Activación directa a tu correo", "+ Te pediremos tu correo al momento de comprar",
               "+ Recibirás un aviso cuando esté listo"]
    else:
        ls += ["+ Cambio de credenciales a tus datos", "+ Tras pagar, contacta directamente con el administrador",
               "+ Coordinas tus datos de forma segura por Telegram"]
    return "\n".join(ls)


def inferir_modalidad(producto, vieja_desc):
    n, d = producto.casefold(), (vieja_desc or "").casefold()
    if "canva" in n or "correo" in d:
        return "correo"
    if any(k in n for k in ("windows", "office", "licencia", "license", " key", "key ")):
        return "key"
    if "link" in n or "link" in d or "activacion" in d or "activación" in d or "metodo" in n or "método" in n:
        return "link"
    return "email_pass"


# =====================================================================
# 🌐 MINI SERVIDOR WEB (mantiene vivo el servicio en Railway)
# =====================================================================
app = Flask(__name__)


@app.route("/")
@app.route("/health")
def home():
    return "⚡ SA SHOP Bot activo 24/7"


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)


def keep_alive():
    Thread(target=run_flask, daemon=True).start()


# =====================================================================
# ⚙️ BOT
# =====================================================================
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=True, num_threads=BOT_THREADS)


def esc(x):
    return html.escape(str(x), quote=False)


# =====================================================================
# 🗄️ BASE DE DATOS (pool de conexiones)
# =====================================================================
_pool = pg_pool.ThreadedConnectionPool(
    1, DB_POOL_MAX, dsn=DATABASE_URL, connect_timeout=10,
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
)
_last_used = {}


def _getconn_espera():
    for _ in range(40):
        try:
            return _pool.getconn()
        except pg_pool.PoolError:
            time.sleep(0.15)
    raise RuntimeError("Pool de base de datos agotado")


def _tomar_conexion():
    conn = _getconn_espera()
    inactiva = time.time() - _last_used.get(id(conn), 0)
    if conn.closed or inactiva > 25:
        sana = False
        if not conn.closed:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.rollback()
                sana = True
            except Exception:
                sana = False
        if not sana:
            try:
                _pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = _getconn_espera()
    return conn


@contextmanager
def db():
    """Conexión del pool; confirma al salir o revierte si hay error."""
    conn = _tomar_conexion()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _last_used[id(conn)] = time.time()
        try:
            _pool.putconn(conn, close=bool(conn.closed))
        except Exception:
            pass


def q(sql, params=None, fetch="all"):
    """Consulta con reintento si la conexión estaba caída. fetch: 'all' | 'one' | None (filas afectadas)."""
    for intento in (1, 2):
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if fetch == "all":
                        return cur.fetchall()
                    if fetch == "one":
                        return cur.fetchone()
                    return cur.rowcount
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if intento == 2:
                raise


def _mantener_conexiones_calientes():
    """Cada 20 s despierta varias conexiones del pool para que el primer clic nunca espere a la BD."""
    while True:
        time.sleep(20)
        tomadas = []
        try:
            for _ in range(min(4, DB_POOL_MAX)):
                try:
                    tomadas.append(_tomar_conexion())
                except Exception:
                    break
            for conn in tomadas:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                    conn.rollback()
                except Exception:
                    pass
        finally:
            for conn in tomadas:
                _last_used[id(conn)] = time.time()
                try:
                    _pool.putconn(conn, close=bool(conn.closed))
                except Exception:
                    pass


def init_db():
    with db() as conn:
        with conn.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS usuarios (
                            user_id BIGINT PRIMARY KEY, username TEXT, nombre TEXT,
                            compras INTEGER DEFAULT 0, fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS stock (
                            id SERIAL PRIMARY KEY, producto TEXT, credenciales TEXT, vendido INTEGER DEFAULT 0)""")
            c.execute("""CREATE TABLE IF NOT EXISTS pagos_usados (
                            order_id TEXT PRIMARY KEY, user_id BIGINT, monto REAL,
                            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS historial_compras (
                            id SERIAL PRIMARY KEY, user_id BIGINT, producto TEXT, credenciales TEXT,
                            precio REAL, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS catalogo_db (
                            producto TEXT PRIMARY KEY, precio REAL, descripcion TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS entregas_pendientes (
                            order_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, producto TEXT NOT NULL,
                            credenciales TEXT NOT NULL, creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS ajustes (
                            clave TEXT PRIMARY KEY, valor TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS iconos (
                            clave TEXT PRIMARY KEY, emoji_id TEXT NOT NULL)""")
            # --- Columnas nuevas (modalidades + stock infinito) ---
            c.execute("ALTER TABLE catalogo_db ADD COLUMN IF NOT EXISTS modalidad TEXT")
            c.execute("ALTER TABLE catalogo_db ADD COLUMN IF NOT EXISTS infinito BOOLEAN DEFAULT FALSE")
            c.execute("ALTER TABLE catalogo_db ADD COLUMN IF NOT EXISTS entrega_infinita TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_stock_prod_vendido ON stock (producto, vendido)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_hist_user ON historial_compras (user_id)")

            c.execute("SELECT COUNT(*) FROM catalogo_db")
            if c.fetchone()[0] == 0:
                for prod, (precio, mod, inf) in CATALOGO_INICIAL.items():
                    c.execute(
                        "INSERT INTO catalogo_db (producto, precio, descripcion, modalidad, infinito) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (producto) DO NOTHING",
                        (prod, precio, descripcion_auto(prod, mod, inf), mod, inf))

            # --- Migración de plataformas antiguas (se hace una sola vez por plataforma) ---
            c.execute("SELECT producto, descripcion FROM catalogo_db WHERE modalidad IS NULL")
            for prod, vieja in c.fetchall():
                mod = inferir_modalidad(prod, vieja)
                inf = ("metodo" in prod.casefold() or "método" in prod.casefold()) and mod in ("link", "email_pass")
                contenido = None
                if inf:
                    c.execute("SELECT credenciales FROM stock WHERE producto = %s AND vendido = 0 ORDER BY id LIMIT 1", (prod,))
                    fila = c.fetchone()
                    contenido = fila[0] if fila else None
                c.execute("UPDATE catalogo_db SET modalidad = %s, infinito = %s, entrega_infinita = %s, descripcion = %s "
                          "WHERE producto = %s",
                          (mod, inf, contenido, descripcion_auto(prod, mod, inf), prod))
                log.info(f"Plataforma migrada: {prod} -> {mod}{' (infinito)' if inf else ''}")


init_db()


# =====================================================================
# 🎨 ÍCONOS PERSONALIZADOS (emojis premium) — se asignan con /iconos
# =====================================================================
# Sin íconos asignados, el bot usa los emojis normales de abajo (funciona igual).
ICONOS = {
    "tienda": ("🛍️", "Tienda / catálogo"),
    "perfil": ("👤", "Mi perfil"),
    "soporte": ("💬", "Soporte / contacto"),
    "menu": ("🏠", "Menú principal"),
    "admin": ("👑", "Panel de admin"),
    "pagar": ("💳", "Pagar"),
    "ok": ("✅", "Confirmado / check"),
    "error": ("❌", "Cancelar / error"),
    "volver": ("⬅️", "Volver"),
    "mas": ("➕", "Agregar"),
    "menos": ("➖", "Quitar"),
    "stock": ("📦", "Stock / cantidad"),
    "infinito": ("♾️", "Stock infinito"),
    "correo": ("✉️", "Activación a correo"),
    "dominio": ("🤝", "A dominio"),
    "link": ("🔗", "Link de activación"),
    "key": ("🔑", "Key de activación"),
    "rayo": ("⚡", "Entrega inmediata"),
    "info": ("ℹ️", "Información"),
}
_iconos_ids = {}
_iconos_on = [ICONOS_ACTIVOS_INICIAL]


def cargar_iconos():
    try:
        for clave, eid in q("SELECT clave, emoji_id FROM iconos"):
            if clave in ICONOS:
                _iconos_ids[clave] = eid
    except Exception as e:
        log.warning(f"No se pudieron cargar los íconos: {e}")


cargar_iconos()


def ic(clave):
    """Ícono para TEXTOS: emoji premium (si está asignado) o emoji normal."""
    fb = ICONOS[clave][0]
    eid = _iconos_ids.get(clave)
    if eid and _iconos_on[0]:
        return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'
    return fb


# --- Red de seguridad: si Telegram rechaza los íconos premium, se quitan y se reintenta ---
_RE_TGEMOJI = re.compile(r'<tg-emoji emoji-id="[^"]*">(.*?)</tg-emoji>', re.S)
_orig_make_request = apihelper._make_request


def _tiene_iconos(params):
    if not isinstance(params, dict):
        return False
    texto = f"{params.get('reply_markup', '')}{params.get('text', '')}{params.get('caption', '')}"
    return "icon_custom_emoji_id" in texto or "<tg-emoji" in texto


def _quitar_iconos(params):
    p = dict(params)
    rm = p.get("reply_markup")
    if rm:
        try:
            d = json.loads(rm) if isinstance(rm, str) else rm
            for fila in d.get("inline_keyboard", []):
                for b in fila:
                    b.pop("icon_custom_emoji_id", None)
            p["reply_markup"] = json.dumps(d) if isinstance(rm, str) else d
        except Exception:
            pass
    for k in ("text", "caption"):
        if isinstance(p.get(k), str):
            p[k] = _RE_TGEMOJI.sub(r"\1", p[k])
    return p


def _make_request_seguro(token, method_name, method="get", params=None, files=None):
    try:
        return _orig_make_request(token, method_name, method, params, files)
    except ApiTelegramException as e:
        desc = (getattr(e, "description", "") or "").lower()
        if (e.error_code == 400 and _tiene_iconos(params)
                and any(k in desc for k in ("emoji", "icon", "button", "entit", "parse"))):
            log.warning(f"Telegram rechazó íconos personalizados ({desc}); se reintenta con emojis normales.")
            res = _orig_make_request(token, method_name, method, _quitar_iconos(params), files)
            if _iconos_on[0]:
                _iconos_on[0] = False
                log.warning("Íconos personalizados DESACTIVADOS (tu cuenta/bot no los admite).")
            return res
        raise


apihelper._make_request = _make_request_seguro


# =====================================================================
# 🎨 BOTONES DE COLOR (verde = disponible · rojo = sin stock/cancelar · azul = navegación/infinito)
# =====================================================================
VERDE, ROJO, AZUL = "success", "danger", "primary"
_estilo_ok = [True]


def btn(texto, cb=None, url=None, style=None, icono=None, final=False, marca=""):
    """final=True: el ícono va al FINAL del texto (emoji normal); nada antes del texto."""
    kw = {}
    if cb is not None:
        kw["callback_data"] = cb
    if url is not None:
        kw["url"] = url
    if final:
        if icono in ICONOS:
            texto = f"{texto} {ICONOS[icono][0]}{marca}"
    elif icono in ICONOS:
        eid = _iconos_ids.get(icono) if _iconos_on[0] else None
        if eid:
            kw["icon_custom_emoji_id"] = eid
        else:
            texto = f"{ICONOS[icono][0]} {texto}"
    if style and _estilo_ok[0]:
        try:
            return _TgButton(texto, style=style, **kw)
        except TypeError:
            _estilo_ok[0] = False
            log.warning("Tu versión de pyTelegramBotAPI no soporta colores en botones; actualízala.")
    return _TgButton(texto, **kw)


def boton_volver_menu():
    m = InlineKeyboardMarkup()
    m.add(btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    return m


def boton_cancelar(destino="inicio"):
    m = InlineKeyboardMarkup()
    m.add(btn("Cancelar", destino, style=ROJO, icono="error"))
    return m


def boton_admin_url():
    return btn("Contactar al administrador", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}", style=AZUL, icono="soporte")


# =====================================================================
# 📚 CATÁLOGO + STOCK (1 sola consulta, con caché inteligente)
# =====================================================================
_cat = {"snap": None, "ver": 0, "bg": False}
_cat_lock = Lock()
_ver_lock = Lock()


_tok_cache = {}


def tok(nombre):
    t = _tok_cache.get(nombre)
    if t is None:
        t = _tok_cache[nombre] = hashlib.sha1(nombre.encode("utf-8")).hexdigest()[:12]
    return t


def _construir_catalogo():
    v = _cat["ver"]
    filas = q("""SELECT c.producto, c.precio, c.descripcion, c.modalidad, c.infinito, c.entrega_infinita, COALESCE(s.n, 0)
                 FROM catalogo_db c
                 LEFT JOIN (SELECT producto, COUNT(*) AS n FROM stock
                            WHERE vendido = 0 GROUP BY producto) s ON s.producto = c.producto
                 ORDER BY c.producto""")
    data = {}
    for prod, precio, desc, mod, inf, ent, n in filas:
        mod = mod if mod in MODALIDADES else "email_pass"
        manual = mod in MANUALES
        inf = bool(inf) and not manual
        tiene = bool((ent or "").strip())
        disponible = True if manual else (tiene if inf else int(n) > 0)
        data[prod] = {"precio": round(float(precio or 0), 2), "desc": desc or "", "modalidad": mod,
                      "manual": manual, "infinito": inf, "tiene_contenido": tiene,
                      "stock": int(n), "disponible": disponible}
    snap = (data, {tok(p): p for p in data}, time.time(), v)
    _cat["snap"] = snap
    return snap


def _refresco_bg():
    try:
        _construir_catalogo()
    except Exception as e:
        log.warning(f"Refresco de catálogo falló: {e}")
    finally:
        _cat["bg"] = False


def invalidar_cache():
    with _ver_lock:
        _cat["ver"] += 1


def catalogo_completo():
    snap = _cat["snap"]
    if snap is None or snap[3] != _cat["ver"]:
        with _cat_lock:
            snap = _cat["snap"]
            if snap is None or snap[3] != _cat["ver"]:
                snap = _construir_catalogo()
        return snap[0]
    if time.time() - snap[2] > CACHE_TTL and not _cat["bg"]:
        _cat["bg"] = True
        Thread(target=_refresco_bg, daemon=True).start()
    return snap[0]


def resolver_token(token):
    catalogo_completo()
    nombre = _cat["snap"][1].get(token)
    if nombre is None:
        invalidar_cache()
        catalogo_completo()
        nombre = _cat["snap"][1].get(token)
    return nombre


def info_fresca(producto):
    """Información del producto leída de la base de datos (sin caché)."""
    invalidar_cache()
    return catalogo_completo().get(producto)


def buscar_producto_nombre(texto_busqueda):
    t = texto_busqueda.strip().lower()
    for prod in catalogo_completo():
        if prod.lower() == t or t in prod.lower():
            return prod
    return None


def a_dec(x):
    return Decimal(str(round(float(x), 2)))


# =====================================================================
# 👤 USUARIOS
# =====================================================================
_usuarios_vistos = {}


def registrar_usuario(user):
    uname = (user.username or "").lower()
    nombre = user.first_name or "Usuario"
    if _usuarios_vistos.get(user.id) == (uname, nombre):
        return
    try:
        q("INSERT INTO usuarios (user_id, username, nombre, compras) VALUES (%s, %s, %s, 0) "
          "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, nombre = EXCLUDED.nombre",
          (user.id, uname, nombre), fetch=None)
        _usuarios_vistos[user.id] = (uname, nombre)
    except Exception as e:
        log.warning(f"No se pudo registrar usuario {user.id}: {e}")


def obtener_datos_usuario(user_id):
    fila = q("SELECT user_id, username, nombre, compras FROM usuarios WHERE user_id = %s", (user_id,), "one")
    if not fila:
        return user_id, "Sin @username", "Cliente", 0
    return fila[0], (f"@{fila[1]}" if fila[1] else "Sin @username"), (fila[2] or "Cliente"), (fila[3] or 0)


def buscar_id_por_id_o_username(identificador):
    limpio = identificador.strip().lstrip("@").lower()
    if limpio.isdigit():
        fila = q("SELECT user_id FROM usuarios WHERE user_id = %s", (int(limpio),), "one")
    else:
        fila = q("SELECT user_id FROM usuarios WHERE LOWER(username) = %s", (limpio,), "one")
    if fila:
        return fila[0]
    return int(limpio) if limpio.isdigit() else None


# =====================================================================
# 🧠 ESTADOS (un solo flujo activo por usuario)
# =====================================================================
estado = {}
_user_locks = {}
_pagos_en_proceso = set()
_pagos_lock = Lock()
_en_vuelo = set()
_ult_cb = {}


def set_estado(uid, flujo, **kw):
    estado[uid] = {"flujo": flujo, **kw}


def limpiar_estados_usuario(uid):
    estado.pop(uid, None)


def _lock_usuario(uid):
    return _user_locks.setdefault(uid, Lock())


def _es_duplicado(uid, data, ventana=0.7):
    ahora = time.time()
    clave = (uid, data)
    previo = _ult_cb.get(clave, 0)
    _ult_cb[clave] = ahora
    if len(_ult_cb) > 4000:
        for k in [k for k, t in list(_ult_cb.items()) if ahora - t > 5]:
            _ult_cb.pop(k, None)
    return ahora - previo < ventana


# =====================================================================
# 🧰 UTILIDADES DE TELEGRAM
# =====================================================================
def configurar_menu_comandos():
    try:
        publicos = [
            BotCommand("menu", "Menú principal"),
            BotCommand("buy", "Catálogo de compras"),
            BotCommand("stock", "Consultar stock"),
            BotCommand("mis_entregas", "Recuperar entrega pendiente"),
        ]
        bot.set_my_commands(publicos)
        admin = publicos + [
            BotCommand("cmds", "Panel de administración"),
            BotCommand("agregar_plataforma", "Agregar plataforma"),
            BotCommand("quitar_plataforma", "Eliminar plataforma"),
            BotCommand("modificar_nombre", "Cambiar nombre"),
            BotCommand("modificar_precio", "Cambiar precio"),
            BotCommand("modificar_descripcion", "Cambiar información"),
            BotCommand("modificar_modalidad", "Cambiar modalidad"),
            BotCommand("modificar_stock", "Stock infinito o normal"),
            BotCommand("cargar", "Subir stock"),
            BotCommand("cargar_lote", "Carga masiva"),
            BotCommand("noti", "Anuncio global"),
            BotCommand("canal", "Canal de referencias de compras"),
            BotCommand("stats", "Estadísticas"),
            BotCommand("iconos", "Íconos personalizados"),
            BotCommand("testbinance", "Probar Binance"),
            BotCommand("testip", "IP de Railway"),
        ]
        bot.set_my_commands(admin, scope=BotCommandScopeChat(ADMIN_ID))
    except Exception as e:
        log.warning(f"No se pudo configurar el menú de comandos: {e}")


def _responder(call, texto=None, alerta=False):
    if getattr(call, "_respondido", False):
        return
    call._respondido = True
    try:
        bot.answer_callback_query(call.id, texto, show_alert=alerta)
    except Exception:
        pass


def _responder_async(call, texto=None, alerta=False):
    """Quita el «cargando» del botón en un hilo aparte, sin frenar la respuesta."""
    if getattr(call, "_respondido", False):
        return
    Thread(target=_responder, args=(call, texto, alerta), daemon=True).start()
    call._respondido = True


def _borrar(chat_id, msg_id):
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


def editar(msg, texto, markup=None):
    """Edita el mensaje del botón; si era una foto (QR) lo borra y envía uno nuevo."""
    chat_id, msg_id = msg.chat.id, msg.message_id
    if getattr(msg, "content_type", "text") != "text":
        _borrar(chat_id, msg_id)
        bot.send_message(chat_id, texto, reply_markup=markup)
        return
    try:
        bot.edit_message_text(texto, chat_id, msg_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" in str(e):
            return
        _borrar(chat_id, msg_id)
        bot.send_message(chat_id, texto, reply_markup=markup)


def editar_o_enviar(chat_id, msg_id, texto, markup=None):
    try:
        bot.edit_message_text(texto, chat_id, msg_id, reply_markup=markup)
        return True
    except Exception as e:
        if "message is not modified" in str(e):
            return True
    try:
        bot.send_message(chat_id, texto, reply_markup=markup)
        return True
    except Exception as e:
        log.error(f"No se pudo entregar mensaje a {chat_id}: {e}")
        return False


def enviar_archivo_txt(chat_id, contenido, nombre_archivo, caption=""):
    archivo = io.BytesIO(contenido.encode("utf-8"))
    archivo.name = nombre_archivo
    bot.send_document(chat_id, archivo, caption=caption, reply_markup=boton_volver_menu())


def avisar_admin(texto, markup=None):
    try:
        bot.send_message(ADMIN_ID, texto, reply_markup=markup)
    except Exception as e:
        log.error(f"No se pudo avisar al admin: {e}")


# =====================================================================
# 📣 CANAL DE REFERENCIAS DE COMPRAS (100 % automático)
# =====================================================================
_ajustes = {}


def get_ajuste(clave, defecto=""):
    if clave in _ajustes:
        return _ajustes[clave]
    try:
        fila = q("SELECT valor FROM ajustes WHERE clave = %s", (clave,), "one")
        _ajustes[clave] = fila[0] if fila else defecto
    except Exception:
        return defecto
    return _ajustes[clave]


def set_ajuste(clave, valor):
    if valor is None:
        q("DELETE FROM ajustes WHERE clave = %s", (clave,), fetch=None)
        _ajustes[clave] = ""
        return
    q("INSERT INTO ajustes (clave, valor) VALUES (%s, %s) ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
      (clave, str(valor)), fetch=None)
    _ajustes[clave] = str(valor)


def canal_id():
    v = str(get_ajuste("canal_id", "") or CANAL_REFERENCIAS or "").strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else v


def canal_activo():
    return bool(canal_id()) and get_ajuste("canal_on", "1") == "1"


_bot_user = [None]


def bot_username():
    if _bot_user[0] is None:
        try:
            _bot_user[0] = bot.get_me().username
        except Exception:
            return None
    return _bot_user[0]


def _enmascarar(nombre):
    n = (nombre or "").strip()
    if len(n) <= 1:
        return "Cliente"
    return f"{n[:2]}{'*' * min(4, max(2, len(n) - 2))}"


def texto_referencia(producto, cantidad, monto, mod, nombre, numero, prueba=False):
    ahora = datetime.now(timezone(timedelta(hours=ZONA_HORARIA_UTC)))
    entrega = {"correo": "Activación manual a su correo", "dominio": "Entrega manual con el administrador"}.get(mod, "Automática e inmediata")
    cab = "🧪 <b>PRUEBA · así se verán las referencias</b>\n\n" if prueba else ""
    return (
        f"{cab}✅ <b>NUEVA COMPRA VERIFICADA</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛍 <b>Producto:</b> {esc(producto)}" + (f" (x{cantidad})" if cantidad > 1 else "") + "\n"
        f"💵 <b>Precio:</b> <code>{float(monto):.2f} USDT</code>\n"
        f"⚡ <b>Entrega:</b> {entrega}\n"
        "💎 <b>Pago:</b> Binance Pay · verificado\n"
        f"👤 <b>Cliente:</b> {esc(_enmascarar(nombre))}\n"
        f"🕒 <b>Fecha:</b> {ahora:%d/%m/%Y · %H:%M}\n\n"
        f"🏆 <b>Venta #{numero}</b> · tienda 100 % automática 24/7"
    )


def _teclado_canal():
    u = bot_username()
    if not u:
        return None
    m = InlineKeyboardMarkup()
    m.add(btn("🛒 Comprar ahora en el bot", url=f"https://t.me/{u}", style=VERDE))
    return m


_aviso_canal_ts = [0.0]


def _publicar_en_canal(texto):
    destino = canal_id()
    for intento in (1, 2):
        try:
            bot.send_message(destino, texto, reply_markup=_teclado_canal(), disable_web_page_preview=True)
            return True
        except ApiTelegramException as e:
            if e.error_code == 429 and intento == 1:
                time.sleep(int(getattr(e, "result_json", {}).get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            log.error(f"No se pudo publicar en el canal: {e}")
            if time.time() - _aviso_canal_ts[0] > 1800:
                _aviso_canal_ts[0] = time.time()
                avisar_admin("⚠️ <b>No pude publicar en el canal de referencias.</b>\n"
                             "Revisa que el bot siga siendo <b>administrador</b> con permiso de publicar. Usa /canal para comprobarlo.\n"
                             f"<code>{esc(str(e)[:200])}</code>")
            return False
        except Exception as e:
            log.error(f"No se pudo publicar en el canal: {e}")
            return False
    return False


def publicar_referencia(producto, cantidad, monto, mod, nombre):
    """Publica la venta en el canal sin frenar la entrega al cliente."""
    if not canal_activo():
        return

    def _tarea():
        try:
            numero = q("SELECT COUNT(*) FROM historial_compras", fetch="one")[0]
            _publicar_en_canal(texto_referencia(producto, cantidad, monto, mod, nombre, numero))
        except Exception as e:
            log.error(f"Referencia no publicada: {e}")
    Thread(target=_tarea, daemon=True).start()


def _estado_permisos_canal():
    """(ok, texto) — comprueba que el bot es admin del canal y puede publicar."""
    destino = canal_id()
    if not destino:
        return False, "Sin canal conectado"
    try:
        chat = bot.get_chat(destino)
        yo = bot.get_chat_member(chat.id, bot.get_me().id)
    except Exception as e:
        return False, f"No puedo acceder al canal ({esc(str(e)[:120])})"
    titulo = esc(getattr(chat, "title", None) or str(destino))
    if yo.status not in ("administrator", "creator"):
        return False, f"{titulo} — el bot NO es administrador"
    if yo.status == "administrator" and getattr(yo, "can_post_messages", True) is False:
        return False, f"{titulo} — el bot es admin pero sin permiso de publicar"
    return True, titulo


def vista_canal():
    destino = canal_id()
    if not destino:
        texto = (
            "📣 <b>CANAL DE REFERENCIAS</b>\n\n"
            "Cada compra verificada se publica sola en tu canal (producto, precio, entrega, cliente enmascarado).\n\n"
            "<b>Cómo conectarlo (1 minuto):</b>\n"
            "1️⃣ Crea tu canal en Telegram.\n"
            "2️⃣ Canal ➔ Administradores ➔ <b>Añadir administrador</b> ➔ busca este bot y activa <b>Publicar mensajes</b>.\n"
            "3️⃣ Pulsa <b>Conectar canal</b> y <b>reenvíame cualquier publicación del canal</b> (o escribe su ID <code>-100…</code> / <code>@usuario</code>)."
        )
        m = InlineKeyboardMarkup(row_width=1)
        m.add(btn("Conectar canal", "can:cambiar", style=VERDE, icono="ok"), btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
        return texto, m
    ok, detalle = _estado_permisos_canal()
    on = get_ajuste("canal_on", "1") == "1"
    texto = (
        "📣 <b>CANAL DE REFERENCIAS</b>\n\n"
        f"<b>Canal:</b> {detalle}\n"
        f"<b>ID:</b> <code>{esc(destino)}</code>\n"
        f"<b>Permisos del bot:</b> {'✅ correctos' if ok else '❌ revisar'}\n"
        f"<b>Publicación automática:</b> {'🟢 ACTIVADA' if on else '🔴 PAUSADA'}\n\n"
        "Cada venta verificada se publica sola. No se muestran cuentas, correos, ni IDs de pago."
    )
    m = InlineKeyboardMarkup(row_width=2)
    m.add(btn("Enviar prueba", "can:probar", style=VERDE, icono="ok"),
          btn("Pausar" if on else "Activar", "can:onoff", style=ROJO if on else VERDE, icono="error" if on else "ok"),
          btn("Cambiar canal", "can:cambiar", style=AZUL, icono="link"),
          btn("Quitar canal", "can:quitar", style=ROJO, icono="error"))
    m.add(btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    return texto, m


def flujo_canal(message, st):
    chat = None
    fo = getattr(message, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        chat = fo.chat
    elif getattr(message, "forward_from_chat", None) is not None:
        chat = message.forward_from_chat
    if chat is not None:
        destino = chat.id
    else:
        if message.content_type != "text":
            bot.reply_to(message, "⚠️ Reenvíame una publicación del canal o escribe su ID/@usuario.", reply_markup=boton_cancelar())
            return True
        destino = (message.text or "").strip()
        if re.fullmatch(r"-?\d{6,}", destino):
            destino = int(destino)
        elif re.fullmatch(r"@?[A-Za-z0-9_]{4,}", destino):
            destino = "@" + destino.lstrip("@")
        else:
            bot.reply_to(message, "❌ Formato no válido. Reenvía una publicación del canal o escribe <code>-1001234567890</code> / <code>@micanal</code>.",
                         reply_markup=boton_cancelar())
            return True
    try:
        info = bot.get_chat(destino)
        yo = bot.get_chat_member(info.id, bot.get_me().id)
    except Exception as e:
        bot.reply_to(message, f"❌ No pude acceder a ese canal. Añade el bot como <b>administrador</b> y vuelve a intentarlo.\n<code>{esc(str(e)[:150])}</code>",
                     reply_markup=boton_cancelar())
        return True
    if getattr(info, "type", "channel") != "channel":
        bot.reply_to(message, "❌ Eso no es un canal. Crea un <b>canal</b> (no grupo) y reenvíame una publicación suya.", reply_markup=boton_cancelar())
        return True
    if yo.status not in ("administrator", "creator") or getattr(yo, "can_post_messages", True) is False:
        bot.reply_to(message, "❌ El bot no puede publicar ahí. En el canal ➔ Administradores ➔ añade este bot con permiso <b>Publicar mensajes</b> y reenvíame otra publicación.",
                     reply_markup=boton_cancelar())
        return True
    estado.pop(ADMIN_ID, None)
    set_ajuste("canal_id", info.id)
    set_ajuste("canal_on", "1")
    texto, m = vista_canal()
    bot.reply_to(message, "✅ <b>¡Canal conectado!</b> Desde ahora cada compra se publica sola.\n\n" + texto, reply_markup=m)
    return True


# =====================================================================
# 💎 VERIFICACIÓN DE PAGOS EN BINANCE
# =====================================================================
_binance_lock = Lock()
_binance_last = [0.0]


def _binance_get(path, params):
    with _binance_lock:
        espera = 0.4 - (time.time() - _binance_last[0])
        if espera > 0:
            time.sleep(espera)
        try:
            params = dict(params)
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 30000
            query = urlencode(params)
            firma = hmac.new(BINANCE_SECRET_KEY.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            url = f"{BINANCE_BASE_URL}{path}?{query}&signature={firma}"
            r = requests.get(url, headers={"X-MBX-APIKEY": BINANCE_API_KEY}, timeout=15)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:300]}
            return r.status_code, data
        finally:
            _binance_last[0] = time.time()


def _coincide_id(tx, order_id):
    for key in ("transactionId", "orderId", "merchantTradeNo", "prepayId"):
        val = tx.get(key)
        if val is not None and str(val).strip() == order_id:
            return True
    return False


def verificar_pago_binance(order_id, monto_esperado, creado_ms=None):
    """Comprueba una transferencia recibida en el historial de Binance. -> (True, monto) | (False, mensaje)"""
    order_id = str(order_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", order_id):
        return False, "Identificador de operación inválido."
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return False, "El sistema de pagos no está configurado."
    try:
        esperado = Decimal(str(monto_esperado))
        if esperado <= 0:
            return False, "El precio del pedido no es válido."
    except InvalidOperation:
        return False, "El precio del pedido no es válido."

    ahora = int(time.time() * 1000)
    inicio = ahora - DIAS_HISTORIAL * 86400000
    fin = ahora
    encontrado = None
    try:
        for _ in range(4):
            status, data = _binance_get("/sapi/v1/pay/transactions",
                                        {"startTimestamp": inicio, "endTimestamp": fin, "limit": 100})
            if status != 200 or not isinstance(data, dict) or str(data.get("code", "000000")) not in ("000000", "0"):
                log.error(f"Binance status={status} resp={str(data)[:300]}")
                return False, "No se pudo consultar el historial de pagos. Inténtalo de nuevo en unos segundos."
            lista = data.get("data") or []
            if not isinstance(lista, list):
                return False, "Respuesta inesperada de Binance."
            for tx in lista:
                if isinstance(tx, dict) and _coincide_id(tx, order_id):
                    encontrado = tx
                    break
            if encontrado or len(lista) < 100:
                break
            tiempos = [int(t.get("transactionTime", fin)) for t in lista if isinstance(t, dict)]
            if not tiempos:
                break
            fin = min(tiempos) - 1
    except (requests.RequestException, ValueError, TypeError) as exc:
        log.error(f"Binance conexión: {type(exc).__name__}: {exc}")
        return False, "Error temporal consultando Binance. Inténtalo de nuevo."

    if not encontrado:
        return False, ("No se encontró ese ID en el historial. Comprueba el comprobante; "
                       "si pagaste hace segundos, espera 1 minuto y reintenta.")
    try:
        if str(encontrado.get("currency", "")).upper() != "USDT":
            return False, "La transacción no corresponde a USDT."
        monto = Decimal(str(encontrado.get("amount", "")))
        if not monto.is_finite() or monto <= 0:
            return False, "La operación no es un pago recibido."
        if monto != esperado:
            return False, f"El importe debe ser exactamente {esperado:.2f} USDT."
        estado_tx = str(encontrado.get("status", encontrado.get("transactionStatus", ""))).upper()
        if estado_tx and estado_tx not in ("SUCCESS", "SUCCESSFUL", "COMPLETED", "COMPLETE", "PAID"):
            return False, "La operación aún no figura como completada."
        if creado_ms is not None:
            tx_ms = encontrado.get("transactionTime")
            if tx_ms is None:
                return False, "Binance no proporcionó la fecha de la operación."
            tx_ms = int(tx_ms)
            if tx_ms < 100000000000:
                tx_ms *= 1000
            if tx_ms < int(creado_ms) - 120000 or tx_ms > ahora + 120000:
                return False, "La operación no corresponde al periodo de este pedido."
    except (InvalidOperation, ValueError, TypeError):
        return False, "Los datos del pago son inválidos."
    return True, float(monto)


# =====================================================================
# 🖼️ VISTAS
# =====================================================================
def formatear_descripcion(txt):
    """'+ ' = ✅, '- ' = ❌, '**negrita**'. Todo lo demás se escapa (seguro para HTML)."""
    salida = []
    for linea in (txt or "").split("\n"):
        linea = linea.rstrip()
        if not linea.strip():
            salida.append("")
            continue
        pref = ""
        if linea.startswith("+ "):
            pref, linea = ic("ok") + " ", linea[2:]
        elif linea.startswith("- "):
            pref, linea = ic("error") + " ", linea[2:]
        cuerpo = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(linea))
        salida.append(pref + cuerpo)
    return "\n".join(salida).strip()


def etiqueta_estado(info):
    """Texto corto con el estado de stock (para /stock)."""
    mod = info["modalidad"]
    if mod == "correo":
        return f"{ic('correo')} A correo"
    if mod == "dominio":
        return f"{ic('dominio')} A dominio"
    if info["infinito"]:
        return f"{ic('infinito')} Ilimitado" if info["tiene_contenido"] else "🔴 Sin contenido cargado"
    n = info["stock"]
    return f"{'🟢' if n > 0 else '🔴'} Stock {n}"


def vista_menu_principal(user):
    uname = f"@{user.username}" if user.username else "Sin @username"
    nombre = user.first_name or "Cliente"
    es_admin = user.id == ADMIN_ID
    rango = "Administrador" if es_admin else "Cliente"
    texto = (
        f"{ic('rayo')} <b>SA SHOP — TIENDA AUTOMÁTICA 24/7</b>\n\n"
        f"{ic('perfil')} <b>Usuario:</b> {esc(nombre)} (<code>{esc(uname)}</code>)\n"
        f"{ic('admin') if es_admin else ic('ok')} <b>Rol:</b> {rango}\n"
        f"{ic('info')} <b>ID:</b> <code>{user.id}</code>\n\n"
        "Selecciona una opción:"
    )
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        btn("Ver Catálogo y Comprar", "catalogo", style=VERDE, icono="tienda"),
        btn("Mi Perfil e Historial", "perfil", style=AZUL, icono="perfil"),
        btn("Soporte y Ayuda", "soporte", style=AZUL, icono="soporte"),
    )
    if es_admin:
        m.add(btn("Panel de Administración", "panel_admin", style=AZUL, icono="admin"))
    return texto, m


def vista_catalogo(aviso=""):
    m = InlineKeyboardMarkup(row_width=1)
    for prod, info in catalogo_completo().items():
        precio, mod = info["precio"], info["modalidad"]
        marca = ""
        if mod == "correo":
            txt, ico, est = f"{prod} | ${precio:.2f} | A correo", "correo", VERDE
        elif mod == "dominio":
            txt, ico, est = f"{prod} | ${precio:.2f} | A dominio", "dominio", VERDE
        elif info["infinito"] and info["tiene_contenido"]:
            txt, ico, est = f"{prod} | ${precio:.2f} | Ilimitado", "infinito", AZUL
            marca = "🟣"
        elif info["disponible"]:
            txt, ico, est = f"{prod} | ${precio:.2f} | {info['stock']}", "stock", VERDE
        else:
            txt, ico, est = f"{prod} | ${precio:.2f} | Sin stock", "error", ROJO
        m.add(btn(txt, f"sel:{tok(prod)}", style=est, icono=ico, final=True, marca=marca))
    m.add(btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    texto = (
        f"{aviso}"
        f"{ic('tienda')} <b>CATÁLOGO DE PRODUCTOS</b>\n\n"
        "🟢 Disponible   🔴 Sin stock   🟣 Stock ilimitado\n\n"
        "Selecciona el producto que deseas comprar:"
    )
    return texto, m


def vista_sin_stock(producto):
    texto = (
        "🔴 <b>SIN STOCK</b>\n\n"
        f"Por ahora <b>{esc(producto)}</b> no tiene unidades disponibles.\n\n"
        "Estamos reponiendo el inventario, vuelve a consultar en un rato.\n"
        "Si lo necesitas con urgencia, escríbele al administrador."
    )
    m = InlineKeyboardMarkup(row_width=1)
    m.add(btn("Volver al Catálogo", "catalogo", style=AZUL, icono="volver"),
          boton_admin_url(),
          btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    return texto, m


def vista_producto(nombre, info, cantidad=1):
    """Pantalla de la plataforma: información + cantidad + pagar."""
    mod = info["modalidad"]
    precio = info["precio"]
    if mod == "correo":
        entrega, linea_stock = "Activación manual a tu correo", f"{ic('correo')} <b>Modalidad:</b> A correo"
    elif mod == "dominio":
        entrega, linea_stock = "Entrega manual con el administrador", f"{ic('dominio')} <b>Modalidad:</b> A dominio"
    else:
        entrega = "Entrega automática instantánea"
        linea_stock = (f"{ic('infinito')} <b>Stock:</b> ilimitado" if info["infinito"]
                       else f"{ic('stock')} <b>Stock disponible:</b> {info['stock']}")
    partes = [
        f"{ic('tienda')} <b>{esc(nombre)}</b>\n",
        f"{ic('ok')} <b>Producto oficial de la tienda</b>",
        f"{ic('rayo')} {entrega}\n",
        f"<blockquote>{ic('info')} <b>Descripción</b>\n{formatear_descripcion(info['desc'])}</blockquote>\n",
        f"{ic('pagar')} <b>Precio:</b> <code>{precio:.2f} USDT</code>",
        linea_stock,
    ]
    m = InlineKeyboardMarkup(row_width=3)
    t = tok(nombre)
    if not info["manual"]:
        total = round(cantidad * precio, 2)
        partes.append(f"<b>Cantidad:</b> {cantidad}   ·   <b>Total:</b> <code>{total:.2f} USDT</code>")
        m.row(btn("Quitar", f"cant:menos:{t}", style=ROJO, icono="menos"),
              btn(str(cantidad), "nada"),
              btn("Agregar", f"cant:mas:{t}", style=VERDE, icono="mas"))
    else:
        total = precio
    if mod == "dominio":
        partes.append("\n<i>Tras pagar deberás contactar directamente con el administrador para coordinar la entrega.</i>")
    else:
        partes.append("\n<i>Entrega después de la confirmación del pago.</i>")
    m.row(btn(f"Pagar con Binance · {total:.2f} USDT", f"pagar:{t}", style=VERDE, icono="pagar"))
    if mod == "dominio":
        m.row(boton_admin_url())
    m.row(btn("Volver al Catálogo", "catalogo", style=AZUL, icono="volver"))
    return "\n".join(partes), m


def texto_instrucciones_pago(producto, cantidad, total, extra=""):
    return (
        f"{ic('pagar')} <b>PAGO CON BINANCE PAY</b>\n\n"
        f"{ic('tienda')} <b>Producto:</b> {esc(producto)} (x{cantidad})\n{extra}\n"
        "<b>DETALLES DE PAGO</b>\n"
        "• <b>Método:</b> Binance Pay\n"
        f"• <b>Monto:</b> <code>{total:.2f} USDT</code>\n"
        f"• <b>ID de Binance Pay:</b> <code>{esc(BINANCE_PAY_ID)}</code>\n"
        f"• <b>Titular:</b> {esc(TITULAR)}\n\n"
        "1. Escanea el QR o transfiere al ID indicado.\n"
        "2. Pulsa <b>Ya pagué</b> y envía tu <b>ID de orden</b> para verificar el pago."
    )


def teclado_pago():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(btn("Ya pagué", "yp", style=VERDE, icono="ok"),
          btn("Cancelar", "catalogo", style=ROJO, icono="error"))
    return m


def mostrar_pago(chat_id, st, texto_extra=""):
    st["paso"] = "pago"
    st["creado_ms"] = int(time.time() * 1000)
    texto = texto_instrucciones_pago(st["producto"], st.get("cantidad", 1), st["total"], texto_extra)
    try:
        bot.send_photo(chat_id, QR_URL, caption=texto, reply_markup=teclado_pago())
    except Exception:
        bot.send_message(chat_id, texto, reply_markup=teclado_pago())


def texto_pedir_id():
    return (
        f"{ic('pagar')} <b>VERIFICAR PAGO</b>\n\n"
        "Envía aquí el <b>ID de orden</b> de tu pago de Binance.\n"
        "<i>Lo encuentras en el comprobante, en la línea «ID de orden».</i>\n\n"
        "<b>Ejemplo:</b> <code>463440249244098560</code>"
    )


def texto_cmds_admin():
    return (
        f"{ic('admin')} <b>PANEL DE ADMINISTRACIÓN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>🗂 CATÁLOGO</b>\n"
        "/agregar_plataforma ➔ Crear una plataforma\n"
        "/quitar_plataforma ➔ Eliminar una plataforma\n"
        "/modificar_nombre ➔ Cambiar el nombre\n"
        "/modificar_precio ➔ Cambiar el precio\n"
        "/modificar_descripcion ➔ Cambiar la información\n"
        "/modificar_modalidad ➔ Cambiar la modalidad de entrega\n"
        "/modificar_stock ➔ Stock infinito o normal\n\n"
        "<b>📦 INVENTARIO</b>\n"
        "/cargar ➔ Subir cuentas o links (formato validado)\n"
        "/cargar_lote ➔ Carga masiva (30 a 1000+)\n"
        "/stock ➔ Ver inventario y descargar TXT\n"
        "/borrar [ID] ➔ Eliminar una cuenta\n"
        "/vaciar [nombre] ➔ Vaciar el stock no vendido\n\n"
        "<b>📢 COMUNICACIÓN</b>\n"
        "/noti ➔ Anuncio global a todos los clientes\n"
        "/canal ➔ Canal de referencias de compras (automático)\n"
        "/enviar [ID/@user] ➔ Mensaje privado a un cliente\n\n"
        "<b>👥 CLIENTES Y FINANZAS</b>\n"
        "/historial [ID/@user] ➔ Compras de un cliente\n"
        "/stats ➔ Métricas y finanzas de la tienda\n"
        "/usuarios ➔ Lista de clientes en TXT\n\n"
        "<b>⚙️ SISTEMA</b>\n"
        "/iconos ➔ Íconos personalizados (emojis premium)\n"
        "/testbinance ➔ Probar la conexión con Binance\n"
        "/testip ➔ Ver la IP pública de Railway\n"
        "/mis_entregas ➔ Recuperar una entrega pendiente\n\n"
        "<b>🛒 COMANDOS DE CLIENTES</b>\n"
        "/menu ➔ Menú principal\n"
        "/buy ➔ Catálogo de compras\n"
        "/stock ➔ Consultar stock"
    )


# =====================================================================
# 💬 COMANDOS PÚBLICOS
# =====================================================================
@bot.message_handler(commands=["start", "menu", "menú", "inicio"])
def cmd_start(message):
    registrar_usuario(message.from_user)
    limpiar_estados_usuario(message.from_user.id)
    texto, m = vista_menu_principal(message.from_user)
    bot.send_message(message.chat.id, texto, reply_markup=m)


@bot.message_handler(commands=["buy", "catalogo", "catálogo"])
def cmd_buy(message):
    registrar_usuario(message.from_user)
    limpiar_estados_usuario(message.from_user.id)
    texto, m = vista_catalogo()
    bot.send_message(message.chat.id, texto, reply_markup=m)


@bot.message_handler(commands=["stock"])
def cmd_stock(message):
    registrar_usuario(message.from_user)
    partes = message.text.split(maxsplit=1)
    es_admin = message.from_user.id == ADMIN_ID
    catalogo = catalogo_completo()

    if len(partes) == 1:
        lineas = [f"{ic('stock')} <b>ESTADO DEL INVENTARIO</b>\n"]
        for prod, info in catalogo.items():
            lineas.append(f"{etiqueta_estado(info)} · <b>{esc(prod)}</b> · <code>{info['precio']:.2f} USDT</code> · {MOD_CORTO[info['modalidad']]}")
        bot.reply_to(message, "\n".join(lineas), reply_markup=boton_volver_menu())

        if es_admin:
            filas = q("SELECT id, producto, credenciales FROM stock WHERE vendido = 0 ORDER BY producto, id")
            txt = ["=== REPORTE GENERAL DE STOCK DISPONIBLE ===", ""]
            for prod, info in catalogo.items():
                if info["infinito"]:
                    ent = q("SELECT entrega_infinita FROM catalogo_db WHERE producto = %s", (prod,), "one")
                    txt.append(f"\n--- [{prod.upper()}] (STOCK INFINITO) ---\nSe entrega a todos: {(ent[0] if ent else '') or '(sin contenido)'}")
            actual = ""
            for sid, prod, cred in filas:
                if catalogo.get(prod, {}).get("infinito"):
                    continue
                if prod != actual:
                    actual = prod
                    txt.append(f"\n--- [{prod.upper()}] ---")
                txt.append(f"ID: {sid} | {cred}")
            if len(txt) > 2:
                enviar_archivo_txt(message.chat.id, "\n".join(txt), "stock_general.txt", "🔐 <b>Cuentas en inventario</b>")
        return

    prod = buscar_producto_nombre(partes[1])
    if not prod:
        bot.reply_to(message, f"❌ No se encontró <code>{esc(partes[1])}</code> en el catálogo.", reply_markup=boton_volver_menu())
        return
    info = catalogo[prod]
    bot.reply_to(
        message,
        f"{ic('stock')} <b>Inventario:</b> {esc(prod)}\n<b>Precio:</b> <code>{info['precio']:.2f} USDT</code>\n"
        f"<b>Modalidad:</b> {MODALIDADES[info['modalidad']]}\n<b>Estado:</b> {etiqueta_estado(info)}",
        reply_markup=boton_volver_menu())
    if es_admin and not info["manual"]:
        if info["infinito"]:
            ent = q("SELECT entrega_infinita FROM catalogo_db WHERE producto = %s", (prod,), "one")
            contenido = f"=== {prod.upper()} (STOCK INFINITO) ===\n\nSe entrega a todos:\n{(ent[0] if ent else '') or '(sin contenido)'}"
            enviar_archivo_txt(message.chat.id, contenido, "stock_infinito.txt", f"🔐 <b>Contenido de {esc(prod)}</b>")
        elif info["stock"] > 0:
            filas = q("SELECT id, credenciales FROM stock WHERE producto = %s AND vendido = 0 ORDER BY id", (prod,))
            contenido = f"=== STOCK DE {prod.upper()} ({len(filas)}) ===\n\n" + "\n".join(f"ID: {f[0]} | {f[1]}" for f in filas)
            enviar_archivo_txt(message.chat.id, contenido, f"stock_{prod.lower().replace(' ', '_')}.txt", f"🔐 <b>Credenciales de {esc(prod)}</b>")


@bot.message_handler(commands=["mis_entregas"])
def cmd_mis_entregas(message):
    uid = message.from_user.id
    pendientes = q("SELECT order_id, producto, credenciales FROM entregas_pendientes WHERE user_id = %s ORDER BY creado LIMIT 10", (uid,))
    if not pendientes:
        bot.reply_to(message, "No tienes entregas pendientes. Consulta /menu para ver tu historial.", reply_markup=boton_volver_menu())
        return
    for oid, prod, cred in pendientes:
        bot.send_message(message.chat.id, f"{ic('stock')} <b>Entrega recuperada</b>\nProducto: {esc(prod)}\nAccesos: <code>{esc(cred)}</code>")
        q("DELETE FROM entregas_pendientes WHERE order_id = %s AND user_id = %s", (oid, uid), fetch=None)


# =====================================================================
# 👑 COMANDOS ADMIN
# =====================================================================
def solo_admin(message):
    return message.from_user.id == ADMIN_ID


@bot.message_handler(commands=["cmds", "admin", "ayuda"])
def cmd_cmds(message):
    if solo_admin(message):
        bot.reply_to(message, texto_cmds_admin(), reply_markup=boton_volver_menu())


@bot.message_handler(commands=["testip"])
def cmd_testip(message):
    if not solo_admin(message):
        return
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=10)
        r.raise_for_status()
        bot.reply_to(message, f"🌐 IP pública de Railway:\n<code>{esc(r.json()['ip'])}</code>")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {esc(e)}")


@bot.message_handler(commands=["testbinance"])
def cmd_testbinance(message):
    if not solo_admin(message):
        return
    msg = bot.reply_to(message, "🧪 Probando conexión con Binance...")
    try:
        ahora = int(time.time() * 1000)
        status, data = _binance_get("/sapi/v1/pay/transactions",
                                    {"startTimestamp": ahora - 7 * 24 * 3600 * 1000, "endTimestamp": ahora, "limit": 10})
        if status == 200 and str(data.get("code", "000000")) in ("000000", "0"):
            lista = data.get("data") or []
            resumen = "\n".join(
                f"• <code>{esc(t.get('transactionId'))}</code> | {esc(t.get('amount'))} {esc(t.get('currency'))} | {esc(t.get('orderType'))}"
                for t in lista[:5]) or "(sin movimientos en los últimos 7 días)"
            texto = f"✅ <b>Conexión con Binance OK</b>\n\nÚltimas transacciones detectadas ({len(lista)}):\n{resumen}"
        else:
            texto = f"❌ <b>Error de Binance</b>\nHTTP: <code>{status}</code>\n<code>{esc(str(data)[:500])}</code>"
    except Exception as e:
        texto = f"❌ Error de conexión: <code>{esc(e)}</code>"
    editar_o_enviar(message.chat.id, msg.message_id, texto, boton_volver_menu())


# ---------------------------------------------------------------------
# ➕ Agregar plataforma
# ---------------------------------------------------------------------
TEXTO_MODALIDADES = (
    "<b>Modalidad de entrega</b>\n\n"
    "• <b>Email:Pass</b> — subes cuentas <code>correo@dominio.com:contraseña</code> y el bot las entrega.\n"
    "• <b>Link de activación</b> — subes links <code>https://...</code> y el bot los entrega.\n"
    "• <b>Key de activación</b> — subes keys (Windows, Office…) o texto normal y el bot lo entrega tal cual.\n"
    "• <b>A correo</b> — el cliente deja su correo; tú envías la invitación y lo avisas con <i>Responder</i>.\n"
    "• <b>A dominio</b> — el cliente deja su usuario de Telegram; coordinas la entrega manual con él.\n\n"
    "👇 Elige la modalidad:"
)

TEXTO_SINTAXIS_DESC = (
    "Escribe la <b>descripción</b> (información de la plataforma que verá el cliente).\n\n"
    "<b>Formato:</b>\n"
    "• Línea que empieza con <code>+</code> ➔ ✅\n"
    "• Línea que empieza con <code>-</code> ➔ ❌\n"
    "• <code>**texto**</code> ➔ <b>negrita</b>\n\n"
    "<b>Ejemplo:</b>\n"
    "<code>**Surfshark VPN** — cuenta 60 días\n"
    "**Dispositivos:** 10\n"
    "+ Email y Password listos para usar\n"
    "+ Entrega inmediata tras el pago\n"
    "- Sin garantía después de la entrega</code>"
)


def teclado_modalidades(cb_fn):
    m = InlineKeyboardMarkup(row_width=1)
    for clave, nombre in MODALIDADES.items():
        m.add(btn(nombre, cb_fn(clave), style=AZUL, icono=ICONO_MOD[clave]))
    m.add(btn("Cancelar", "inicio", style=ROJO, icono="error"))
    return m


@bot.message_handler(commands=["agregar_plataforma"])
def cmd_agregar_plataforma(message):
    if not solo_admin(message):
        return
    set_estado(ADMIN_ID, "agregar", paso="nombre")
    bot.reply_to(message, f"{ic('mas')} <b>CREAR NUEVA PLATAFORMA</b>\n\n<b>Paso 1/5 · Nombre</b>\nEnvía el nombre (Ej: <code>Paramount+ 1 Mes</code>):",
                 reply_markup=boton_cancelar())


# ---------------------------------------------------------------------
# Menús de selección de plataforma (modificar / quitar)
# ---------------------------------------------------------------------
def _menu_catalogo_admin(message, prefijo, icono, titulo, estilo, extra_fn=None):
    limpiar_estados_usuario(ADMIN_ID)
    m = InlineKeyboardMarkup(row_width=1)
    for prod, info in catalogo_completo().items():
        extra = f" ({extra_fn(info)})" if extra_fn else ""
        m.add(btn(f"{prod}{extra}", f"{prefijo}:{tok(prod)}", style=estilo, icono=icono))
    m.add(btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    bot.reply_to(message, titulo, reply_markup=m)


@bot.message_handler(commands=["quitar_plataforma"])
def cmd_quitar(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "delplat", "error", "🗑️ <b>ELIMINAR PLATAFORMA</b>\n\nSelecciona la plataforma a retirar:", ROJO)


@bot.message_handler(commands=["modificar_nombre"])
def cmd_mod_nombre(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "modnom", "info", "✏️ <b>MODIFICAR NOMBRE</b>\n\nSelecciona la plataforma:", AZUL)


@bot.message_handler(commands=["modificar_precio"])
def cmd_mod_precio(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "prec", "pagar", "💵 <b>MODIFICAR PRECIO</b>\n\nSelecciona la plataforma:", AZUL,
                             lambda i: f"{i['precio']:.2f} USDT")


@bot.message_handler(commands=["modificar_descripcion"])
def cmd_mod_desc(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "moddesc", "info", "📌 <b>MODIFICAR DESCRIPCIÓN</b>\n\nSelecciona la plataforma (es la información que ve el cliente al comprar):", AZUL)


@bot.message_handler(commands=["modificar_modalidad"])
def cmd_mod_modalidad(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "mm", "link", "🔁 <b>MODIFICAR MODALIDAD</b>\n\nSelecciona la plataforma:", AZUL,
                             lambda i: MOD_CORTO[i["modalidad"]])


@bot.message_handler(commands=["modificar_stock"])
def cmd_mod_stock(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "ms", "infinito", "♾️ <b>MODIFICAR STOCK</b>\n\nSelecciona la plataforma:", AZUL,
                             lambda i: ("infinito" if i["infinito"] else MOD_CORTO[i["modalidad"]] if i["manual"] else f"Stock {i['stock']}"))


def _menu_carga(message, prefijo, titulo):
    limpiar_estados_usuario(ADMIN_ID)
    m = InlineKeyboardMarkup(row_width=2)
    botones = []
    for prod, info in catalogo_completo().items():
        if info["manual"]:
            continue
        if info["infinito"]:
            botones.append(btn(f"{prod} (contenido)", f"{prefijo}:{tok(prod)}", style=AZUL, icono="infinito"))
        else:
            n = info["stock"]
            botones.append(btn(f"{prod} ({n})", f"{prefijo}:{tok(prod)}", style=VERDE if n > 0 else ROJO,
                               icono="stock" if n > 0 else "error"))
    if botones:
        m.add(*botones)
    m.add(btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    bot.reply_to(message, titulo, reply_markup=m)


@bot.message_handler(commands=["cargar"])
def cmd_cargar(message):
    if solo_admin(message):
        _menu_carga(message, "cargar", f"{ic('stock')} <b>CARGA DE INVENTARIO</b>\n\nSelecciona la plataforma. Se valida el formato según su modalidad.\n"
                                       "<i>Las plataformas a correo / a dominio no usan stock.</i>")


@bot.message_handler(commands=["cargar_lote"])
def cmd_cargar_lote(message):
    if solo_admin(message):
        _menu_carga(message, "lotecar", f"{ic('stock')} <b>CARGA MASIVA</b>\n\nSelecciona la plataforma y pega tu lote completo (una línea por cuenta/link):")


@bot.message_handler(commands=["historial"])
def cmd_historial(message):
    if not solo_admin(message):
        return
    partes = message.text.split(maxsplit=1)
    if len(partes) < 2:
        bot.reply_to(message, "⚠️ Uso: <code>/historial [ID o @username]</code>", reply_markup=boton_volver_menu())
        return
    target = buscar_id_por_id_o_username(partes[1])
    if not target:
        bot.reply_to(message, "❌ Cliente no encontrado.", reply_markup=boton_volver_menu())
        return
    uid, uname, nombre, _ = obtener_datos_usuario(target)
    compras = q("SELECT id, producto, credenciales, precio, fecha FROM historial_compras WHERE user_id = %s ORDER BY id DESC", (uid,))
    bot.reply_to(message,
                 f"📋 <b>EXPEDIENTE DEL CLIENTE</b>\n\n👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>\n🛍️ Compras: <code>{len(compras)}</code>",
                 reply_markup=boton_volver_menu())
    if compras:
        txt = [f"=== HISTORIAL: {uid} ({uname}) ===", f"Cliente: {nombre} | Compras: {len(compras)}", "=" * 60, ""]
        for cid, prod, cred, prec, fec in compras:
            txt += [f"[#{cid}] {fec} | {prod} | {prec:.2f} USDT", f"Datos: {cred}", "-" * 50]
        enviar_archivo_txt(message.chat.id, "\n".join(txt), f"historial_{uid}.txt", f"📂 <b>Compras del usuario</b> <code>{uid}</code>")


@bot.message_handler(commands=["noti"])
def cmd_noti(message):
    if not solo_admin(message):
        return
    set_estado(ADMIN_ID, "noti_global")
    bot.reply_to(
        message,
        "📢 <b>ANUNCIO GLOBAL</b>\n\n"
        "Escribe aquí el anuncio con el <b>formato de Telegram</b> (negrita, cursiva, cita, emojis) "
        "o envía una <b>foto con texto</b>.\n\n"
        "El bot lo copiará <b>tal cual</b> a todos los clientes. "
        "Después elegirás el botón que llevará y verás una vista previa antes de enviar.",
        reply_markup=boton_cancelar())


@bot.message_handler(commands=["enviar"])
def cmd_enviar(message):
    if not solo_admin(message):
        return
    partes = message.text.split(maxsplit=1)
    if len(partes) < 2:
        bot.reply_to(message, "⚠️ Uso: <code>/enviar [ID o @username]</code>", reply_markup=boton_volver_menu())
        return
    target = buscar_id_por_id_o_username(partes[1])
    if not target:
        bot.reply_to(message, "❌ Usuario no encontrado.", reply_markup=boton_volver_menu())
        return
    set_estado(ADMIN_ID, "noti_user", target=target)
    bot.reply_to(message, f"📨 Escribe el mensaje o imagen para <code>{target}</code>:", reply_markup=boton_cancelar())


@bot.message_handler(commands=["borrar"])
def cmd_borrar(message):
    if not solo_admin(message):
        return
    partes = message.text.split()
    if len(partes) != 2 or not partes[1].isdigit():
        bot.reply_to(message, "⚠️ Uso: <code>/borrar [ID_DE_CUENTA]</code>")
        return
    n = q("DELETE FROM stock WHERE id = %s AND vendido = 0", (int(partes[1]),), fetch=None)
    invalidar_cache()
    bot.reply_to(message, f"🗑️ Cuenta <code>{partes[1]}</code> eliminada." if n else f"❌ No hay cuenta disponible con ID <code>{partes[1]}</code>.",
                 reply_markup=boton_volver_menu())


@bot.message_handler(commands=["vaciar"])
def cmd_vaciar(message):
    if not solo_admin(message):
        return
    partes = message.text.split(maxsplit=1)
    if len(partes) < 2:
        bot.reply_to(message, "⚠️ Uso: <code>/vaciar [nombre_plataforma]</code>")
        return
    prod = buscar_producto_nombre(partes[1])
    if not prod:
        bot.reply_to(message, "❌ Producto no encontrado.")
        return
    n = q("DELETE FROM stock WHERE producto = %s AND vendido = 0", (prod,), fetch=None)
    invalidar_cache()
    bot.reply_to(message, f"🗑️ Se eliminaron <code>{n}</code> cuentas no vendidas de <b>{esc(prod)}</b>.", reply_markup=boton_volver_menu())


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not solo_admin(message):
        return
    usuarios, ventas, ingresos, n_pagos, total_pagos, stock = q(
        """SELECT (SELECT COUNT(*) FROM usuarios),
                  (SELECT COUNT(*) FROM historial_compras),
                  (SELECT COALESCE(SUM(precio), 0) FROM historial_compras),
                  (SELECT COUNT(*) FROM pagos_usados),
                  (SELECT COALESCE(SUM(monto), 0) FROM pagos_usados),
                  (SELECT COUNT(*) FROM stock WHERE vendido = 0)""", fetch="one")
    bot.reply_to(
        message,
        "📈 <b>ESTADÍSTICAS DE LA TIENDA</b>\n\n"
        f"👥 Clientes: <code>{usuarios}</code>\n"
        f"🛍️ Productos vendidos: <code>{ventas}</code>\n"
        f"💵 Ingresos: <code>{ingresos:.2f} USDT</code>\n"
        f"💎 Pagos Binance verificados: <code>{n_pagos}</code> (<code>{total_pagos:.2f} USDT</code>)\n"
        f"📦 Cuentas en stock: <code>{stock}</code>",
        reply_markup=boton_volver_menu())


@bot.message_handler(commands=["usuarios"])
def cmd_usuarios(message):
    if not solo_admin(message):
        return
    filas = q("SELECT user_id, username, nombre, compras, fecha_registro FROM usuarios ORDER BY fecha_registro DESC")
    lineas = ["=== BASE DE DATOS DE CLIENTES ===", ""]
    for uid, uname, nom, comp, fec in filas:
        lineas.append(f"ID: {uid} | @{uname or 'Sin_user'} | {nom} | Compras: {comp} | Registro: {fec}")
    enviar_archivo_txt(message.chat.id, "\n".join(lineas), "clientes_tienda.txt", f"👥 <b>{len(filas)}</b> clientes registrados")


# ---------------------------------------------------------------------
# 🎨 /iconos — asignar emojis premium a los botones y textos
# ---------------------------------------------------------------------
def vista_iconos():
    estado_txt = "✅ activados" if _iconos_on[0] else "⛔ desactivados (Telegram los rechazó)"
    texto = (
        "🎨 <b>ÍCONOS PERSONALIZADOS</b>\n\n"
        "Reemplazan los emojis normales por <b>emojis premium</b> en botones y mensajes.\n"
        "Pulsa una opción y envíame <b>un emoji premium</b> (necesitas Telegram Premium para enviarlo).\n\n"
        f"Estado: {estado_txt}\n"
        "<i>Telegram solo los permite si el dueño del bot tiene Telegram Premium. Si no, se siguen viendo los emojis normales.</i>"
    )
    m = InlineKeyboardMarkup(row_width=2)
    for clave, (fb, nombre) in ICONOS.items():
        asignado = clave in _iconos_ids
        m.add(_TgButton(f"{'✓ ' if asignado else ''}{fb} {nombre}", callback_data=f"ico:{clave}",
                        **({"style": VERDE if asignado else AZUL} if _estilo_ok[0] else {})))
    m.row(btn("Probar íconos", "ico:_test", style=VERDE, icono="ok"),
          btn("Reactivar", "ico:_on", style=AZUL, icono="rayo"))
    m.row(btn("Quitar todos", "ico:_reset", style=ROJO, icono="error"),
          btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    return texto, m


@bot.message_handler(commands=["canal", "referencias"])
def cmd_canal(message):
    if not solo_admin(message):
        return
    limpiar_estados_usuario(ADMIN_ID)
    texto, m = vista_canal()
    bot.reply_to(message, texto, reply_markup=m)


@bot.message_handler(commands=["iconos"])
def cmd_iconos(message):
    if not solo_admin(message):
        return
    limpiar_estados_usuario(ADMIN_ID)
    texto, m = vista_iconos()
    bot.reply_to(message, texto, reply_markup=m)


# =====================================================================
# 🔘 CALLBACKS (botones)
# =====================================================================
_ACCIONES_RAPIDAS = {"inicio", "catalogo", "panel_admin", "perfil", "soporte", "noti"}
_ACCIONES_CON_ALERTA = {"sel", "cant", "pagar", "yp"}


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    data = call.data or ""
    uid = call.from_user.id
    accion = data.split(":", 1)[0]

    if accion == "nada":
        return _responder_async(call)

    # Acciones que nunca muestran alertas: se responde al instante (el botón deja de «cargar» ya).
    if accion in _ACCIONES_RAPIDAS or (uid == ADMIN_ID and accion not in _ACCIONES_CON_ALERTA):
        _responder_async(call)

    es_cantidad = data.startswith("cant:mas:") or data.startswith("cant:menos:")
    clave = (uid, data)

    if not es_cantidad:
        if clave in _en_vuelo or _es_duplicado(uid, data):
            return _responder_async(call)
        _en_vuelo.add(clave)
    try:
        if es_cantidad:
            with _lock_usuario(uid):
                _manejar_callback(call, data)
        else:
            _manejar_callback(call, data)
    except Exception as e:
        log.exception(f"Error en callback {data!r}: {e}")
        _responder(call, "⚠️ Error temporal. Inténtalo de nuevo.")
    finally:
        _en_vuelo.discard(clave)


def _sesion_compra(uid, nombre, pasos):
    st = estado.get(uid)
    if st and st.get("flujo") == "compra" and st.get("producto") == nombre and st.get("paso") in pasos:
        return st
    return None


def _manejar_callback(call, data):
    user = call.from_user
    uid = user.id
    msg = call.message
    chat_id = msg.chat.id
    es_admin = uid == ADMIN_ID
    partes = data.split(":")
    accion = partes[0]
    registrar_usuario(user)

    # ============================ CLIENTE ============================
    if accion == "inicio":
        _responder_async(call)
        limpiar_estados_usuario(uid)
        texto, m = vista_menu_principal(user)
        return editar(msg, texto, m)

    if accion == "catalogo":
        _responder_async(call)
        limpiar_estados_usuario(uid)
        texto, m = vista_catalogo()
        return editar(msg, texto, m)

    if accion == "panel_admin":
        _responder_async(call)
        if es_admin:
            editar(msg, texto_cmds_admin(), boton_volver_menu())
        return

    if accion == "perfil":
        _responder_async(call)
        uid_, uname, nombre, compras = obtener_datos_usuario(uid)
        ultimas = q("SELECT producto, credenciales FROM historial_compras WHERE user_id = %s ORDER BY id DESC LIMIT 5", (uid,))
        hist = ""
        if ultimas:
            hist = "\n\n📋 <b>Tus últimas compras:</b>\n" + "\n".join(f"• <b>{esc(p)}</b>: <code>{esc(cr)}</code>" for p, cr in ultimas)
        texto = (f"{ic('perfil')} <b>TU CUENTA</b>\n\n"
                 f"<b>Nombre:</b> {esc(nombre)}\n<b>Username:</b> <code>{esc(uname)}</code>\n"
                 f"<b>ID:</b> <code>{uid_}</code>\n<b>Compras totales:</b> <code>{compras}</code>{hist}")
        return editar(msg, texto, boton_volver_menu())

    if accion == "soporte":
        _responder_async(call)
        set_estado(uid, "soporte")
        m = InlineKeyboardMarkup()
        m.add(boton_admin_url(), btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
        return editar(msg, f"{ic('soporte')} <b>CENTRO DE SOPORTE</b>\n\nEscribe tu consulta o reporte aquí en el chat y el administrador te atenderá:", m)

    if accion == "noti":   # el admin pulsa «Responder» en un aviso
        _responder_async(call)
        if es_admin:
            try:
                target = int(partes[1])
            except (IndexError, ValueError):
                return
            set_estado(ADMIN_ID, "noti_user", target=target)
            bot.send_message(chat_id, f"📨 Escribe tu respuesta para <code>{target}</code>:", reply_markup=boton_cancelar())
        return

    # ---- Elegir plataforma: muestra la información ----
    if accion == "sel":
        nombre = resolver_token(partes[1]) if len(partes) > 1 else None
        info = catalogo_completo().get(nombre) if nombre else None
        if not info:
            _responder(call, "Este producto ya no está disponible.", True)
            texto, m = vista_catalogo()
            return editar(msg, texto, m)
        if not info["disponible"]:
            _responder(call, f"🔴 Sin stock en {nombre}")
            texto, m = vista_sin_stock(nombre)
            return editar(msg, texto, m)
        _responder_async(call)
        set_estado(uid, "compra", paso="elegir", producto=nombre, cantidad=1, modalidad=info["modalidad"])
        texto, m = vista_producto(nombre, info, 1)
        return editar(msg, texto, m)

    # ---- Cantidad ----
    if accion == "cant":
        sub = partes[1]
        nombre = resolver_token(partes[2]) if len(partes) > 2 else None
        st = _sesion_compra(uid, nombre, ("elegir",)) if nombre else None
        info = catalogo_completo().get(nombre) if nombre else None
        if not st or not info or info["manual"]:
            return _responder(call, "La sesión expiró. Vuelve al catálogo.", True)
        limite = MAX_CANT_INFINITO if info["infinito"] else info["stock"]
        cantidad = st.get("cantidad", 1)
        if sub == "mas":
            if cantidad >= limite:
                return _responder(call, f"Máximo disponible: {limite}")
            cantidad += 1
        elif sub == "menos":
            if cantidad <= 1:
                return _responder(call, "La cantidad mínima es 1")
            cantidad -= 1
        _responder_async(call)
        st["cantidad"] = cantidad
        texto, m = vista_producto(nombre, info, cantidad)
        return editar(msg, texto, m)

    # ---- Pagar con Binance ----
    if accion == "pagar":
        nombre = resolver_token(partes[1]) if len(partes) > 1 else None
        st = _sesion_compra(uid, nombre, ("elegir",)) if nombre else None
        if not st:
            return _responder(call, "La sesión expiró. Vuelve al catálogo.", True)
        _responder_async(call)
        info = info_fresca(nombre)
        if not info or not info["disponible"]:
            texto, m = vista_sin_stock(nombre)
            return editar(msg, texto, m)
        cantidad = st.get("cantidad", 1)
        if not info["manual"] and not info["infinito"] and info["stock"] < cantidad:
            st["cantidad"] = info["stock"]
            texto, m = vista_producto(nombre, info, info["stock"])
            return editar(msg, "⚠️ <i>El stock cambió, ajustamos tu cantidad.</i>\n\n" + texto, m)
        if info["manual"]:
            cantidad = st["cantidad"] = 1
        st["total"] = a_dec(info["precio"]) * cantidad
        st["modalidad"] = info["modalidad"]

        if info["modalidad"] == "correo":
            st["paso"] = "correo"
            return editar(msg,
                          f"{ic('correo')} <b>{esc(nombre.upper())}</b>\n\n"
                          "Envía tu <b>correo electrónico</b>. A ese correo te enviaremos la invitación/activación.\n\n"
                          "<b>Ejemplo:</b> <code>micorreo@gmail.com</code>",
                          boton_cancelar("catalogo"))
        if info["modalidad"] == "dominio":
            st["paso"] = "usuario"
            return editar(msg,
                          f"{ic('dominio')} <b>{esc(nombre.upper())}</b>\n\n"
                          "Envía tu <b>usuario de Telegram</b> (con @). Lo usaremos para contactarte y entregarte tus datos.\n\n"
                          "<b>Ejemplo:</b> <code>@miusuario</code>",
                          boton_cancelar("catalogo"))
        mostrar_pago(chat_id, st)
        return _borrar(chat_id, msg.message_id)

    # ---- «Ya pagué» → pide el ID de orden ----
    if accion == "yp":
        st = estado.get(uid)
        if not st or st.get("flujo") != "compra" or st.get("paso") != "pago":
            return _responder(call, "La sesión expiró. Vuelve al catálogo.", True)
        _responder_async(call)
        st["paso"] = "id"
        return editar(msg, texto_pedir_id(), boton_cancelar("catalogo"))

    # ============================ ADMIN ============================
    if not es_admin:
        return _responder(call)

    _responder_async(call)

    # ---- Íconos ----
    if accion == "ico":
        sub = partes[1] if len(partes) > 1 else ""
        if sub == "_test":
            m = InlineKeyboardMarkup(row_width=3)
            m.add(*[btn(nombre, "nada", style=AZUL, icono=c) for c, (_, nombre) in ICONOS.items()])
            bot.send_message(chat_id, f"🧪 <b>Prueba de íconos</b>\n{ic('ok')} {ic('tienda')} {ic('pagar')} {ic('infinito')}\n"
                                      "Si ves los íconos premium, funcionan. Si ves emojis normales, tu bot no los admite.", reply_markup=m)
            if not _iconos_on[0]:
                bot.send_message(chat_id, "⛔ Telegram rechazó los íconos y se desactivaron. Usa «Reactivar» para intentarlo de nuevo.")
            return
        if sub == "_on":
            _iconos_on[0] = True
            texto, m = vista_iconos()
            return editar(msg, texto, m)
        if sub == "_reset":
            q("DELETE FROM iconos", fetch=None)
            _iconos_ids.clear()
            texto, m = vista_iconos()
            return editar(msg, texto, m)
        if sub in ICONOS:
            set_estado(ADMIN_ID, "icono", clave=sub)
            return editar(msg, f"🎨 <b>Ícono para «{esc(ICONOS[sub][1])}»</b>\n\n"
                               "Envíame ahora <b>un emoji premium</b>.\n"
                               "Para volver al emoji normal escribe <code>quitar</code>.", boton_cancelar())
        return

    # ---- Anuncio global: botón, vista previa y envío ----
    if accion == "nt":
        st = estado.get(uid)
        if not st or st.get("flujo") != "noti_botones":
            return editar(msg, "⚠️ La sesión expiró. Usa /noti para empezar de nuevo.", boton_volver_menu())
        sub = partes[1] if len(partes) > 1 else ""
        if sub == "go":
            if st.get("opt") is None:
                return
            markup = _markup_difusion(st["opt"])
            estado.pop(uid, None)
            editar(msg, f"📢 Enviando a <code>{st['total']}</code> usuarios en segundo plano...\nPuedes seguir usando el bot.", None)
            Thread(target=_copiar_a_todos, daemon=True,
                   args=(st["src_chat"], st["src_msg"], markup, chat_id, msg.message_id)).start()
            return
        st["opt"] = sub
        try:
            apihelper.copy_message(TELEGRAM_TOKEN, ADMIN_ID, st["src_chat"], st["src_msg"], reply_markup=_markup_difusion(sub))
        except Exception as e:
            return editar(msg, f"❌ No pude generar la vista previa: <code>{esc(e)}</code>", boton_volver_menu())
        m = InlineKeyboardMarkup(row_width=1)
        m.add(btn(f"Enviar a {st['total']} usuarios", "nt:go", style=VERDE, icono="ok"),
              btn("Cancelar", "inicio", style=ROJO, icono="error"))
        return editar(msg, "👀 <b>Esa es la vista previa</b> (el mensaje de arriba).\n\n¿Lo envío a todos los clientes?", m)

    # ---- Agregar plataforma: respuestas con botones ----
    if accion in ("addm", "addd", "addi", "addc"):
        st = estado.get(uid)
        if not st or st.get("flujo") != "agregar":
            return editar(msg, "⚠️ La sesión expiró. Usa /agregar_plataforma para empezar de nuevo.", boton_volver_menu())
        sub = partes[1] if len(partes) > 1 else ""
        if accion == "addm" and st["paso"] == "modalidad" and sub in MODALIDADES:
            st["modalidad"] = sub
            st["paso"] = "desc"
            m = InlineKeyboardMarkup(row_width=1)
            m.add(btn("Descripción automática", "addd:auto", style=VERDE, icono="rayo"),
                  btn("Cancelar", "inicio", style=ROJO, icono="error"))
            return editar(msg, f"<b>Paso 4/5 · Descripción</b>\n\n{TEXTO_SINTAXIS_DESC}\n\n"
                               "O pulsa <b>Descripción automática</b> y la edito por ti (luego puedes cambiarla con /modificar_descripcion).", m)
        if accion == "addd" and st["paso"] == "desc" and sub == "auto":
            st["desc"] = descripcion_auto(st["nombre"], st["modalidad"], False)
            return _tras_descripcion(msg, st, True)
        if accion == "addi" and st["paso"] == "infinito" and sub in ("si", "no"):
            st["infinito"] = sub == "si"
            if not st["infinito"]:
                return _guardar_plataforma(msg, st, True)
            st["paso"] = "contenido"
            m = InlineKeyboardMarkup(row_width=1)
            m.add(btn("Omitir (lo cargo después)", "addc:skip", style=AZUL, icono="volver"),
                  btn("Cancelar", "inicio", style=ROJO, icono="error"))
            return editar(msg, "♾️ <b>Stock infinito</b>\n\nEnvía el <b>link o texto</b> que se entregará a <b>todos</b> los compradores.\n\n"
                               f"<b>Ejemplo:</b> <code>https://mi-sitio.com/metodo-123</code>", m)
        if accion == "addc" and st["paso"] == "contenido" and sub == "skip":
            st["contenido"] = None
            return _guardar_plataforma(msg, st, True)
        return

    # ---- Cambiar modalidad ----
    if accion == "mm":
        nombre = resolver_token(partes[1]) if len(partes) > 1 else None
        info = catalogo_completo().get(nombre) if nombre else None
        if not info:
            return editar(msg, "⚠️ Esa plataforma ya no existe.", boton_volver_menu())
        m = teclado_modalidades(lambda mod: f"mms:{tok(nombre)}:{mod}")
        return editar(msg, f"🔁 <b>MODALIDAD DE</b> <code>{esc(nombre)}</code>\n\n"
                           f"Actual: <b>{MODALIDADES[info['modalidad']]}</b>\n\n{TEXTO_MODALIDADES}", m)

    if accion == "mms":
        nombre = resolver_token(partes[1]) if len(partes) > 1 else None
        nueva = partes[2] if len(partes) > 2 else ""
        info = catalogo_completo().get(nombre) if nombre else None
        if not info or nueva not in MODALIDADES:
            return editar(msg, "⚠️ Esa plataforma ya no existe.", boton_volver_menu())
        if nueva in MANUALES:
            q("UPDATE catalogo_db SET modalidad = %s, infinito = FALSE, entrega_infinita = NULL WHERE producto = %s", (nueva, nombre), fetch=None)
        else:
            q("UPDATE catalogo_db SET modalidad = %s WHERE producto = %s", (nueva, nombre), fetch=None)
        invalidar_cache()
        aviso = ""
        if info["stock"] and nueva in MANUALES:
            aviso = "\n\n⚠️ <i>Esta modalidad no usa stock; las cuentas cargadas quedan guardadas pero no se entregan.</i>"
        elif info["stock"] and nueva != info["modalidad"]:
            aviso = "\n\n⚠️ <i>El stock ya cargado conserva su formato anterior. Revísalo con /stock.</i>"
        return editar(msg, f"✅ <b>Modalidad actualizada</b>\n\n<code>{esc(nombre)}</code>\n➔ <b>{MODALIDADES[nueva]}</b>{aviso}", boton_volver_menu())

    # ---- Stock infinito / normal ----
    if accion == "ms":
        nombre = resolver_token(partes[1]) if len(partes) > 1 else None
        info = catalogo_completo().get(nombre) if nombre else None
        if not info:
            return editar(msg, "⚠️ Esa plataforma ya no existe.", boton_volver_menu())
        return _pantalla_stock(msg, nombre, info)

    if accion == "stk":
        sub = partes[1] if len(partes) > 1 else ""
        nombre = resolver_token(partes[2]) if len(partes) > 2 else None
        info = catalogo_completo().get(nombre) if nombre else None
        if not info:
            return editar(msg, "⚠️ Esa plataforma ya no existe.", boton_volver_menu())
        if info["manual"]:
            return editar(msg, "ℹ️ Esta modalidad no usa stock.", boton_volver_menu())
        if sub == "inf":
            set_estado(uid, "inf_activar", producto=nombre)
            return editar(msg, f"♾️ <b>STOCK INFINITO PARA</b> <code>{esc(nombre)}</code>\n\n"
                               "Envía el <b>link o texto</b> que se entregará a <b>todos</b> los compradores (siempre el mismo).\n\n"
                               "<b>Ejemplo:</b> <code>https://mi-sitio.com/metodo-123</code>", boton_cancelar())
        if sub in ("cont", "carga"):
            set_estado(uid, "carga", producto=nombre)
            return editar(msg, _texto_pedir_carga(nombre, info), boton_cancelar())
        if sub == "normal":
            q("UPDATE catalogo_db SET infinito = FALSE, entrega_infinita = NULL WHERE producto = %s", (nombre,), fetch=None)
            invalidar_cache()
            return editar(msg, f"✅ <code>{esc(nombre)}</code> volvió a <b>stock normal</b>.\nCarga cuentas con /cargar.", boton_volver_menu())
        if sub == "vac":
            m = InlineKeyboardMarkup(row_width=2)
            m.add(btn("Sí, vaciar", f"stk:vacok:{tok(nombre)}", style=ROJO, icono="error"),
                  btn("Cancelar", "inicio", style=AZUL, icono="volver"))
            return editar(msg, f"⚠️ <b>¿Vaciar el stock disponible de</b> <code>{esc(nombre)}</code><b>?</b>\n"
                               f"Se eliminarán <code>{info['stock']}</code> cuentas no vendidas.", m)
        if sub == "vacok":
            n = q("DELETE FROM stock WHERE producto = %s AND vendido = 0", (nombre,), fetch=None)
            invalidar_cache()
            return editar(msg, f"🗑️ Se eliminaron <code>{n}</code> cuentas de <b>{esc(nombre)}</b>.", boton_volver_menu())
        return

    # ---- Canal de referencias ----
    if accion == "can":
        sub = partes[1] if len(partes) > 1 else ""
        if sub == "cambiar":
            set_estado(ADMIN_ID, "canal")
            return editar(msg, "📣 <b>CONECTAR CANAL</b>\n\nAñade este bot como <b>administrador</b> del canal (permiso <i>Publicar mensajes</i>) y luego "
                               "<b>reenvíame aquí cualquier publicación del canal</b>.\n\nTambién puedes escribir su ID (<code>-100…</code>) o <code>@usuario</code> si es público.",
                          boton_cancelar())
        if sub == "quitar":
            set_ajuste("canal_id", None)
            _ajustes["canal_id"] = ""
        elif sub == "onoff":
            set_ajuste("canal_on", "0" if get_ajuste("canal_on", "1") == "1" else "1")
        elif sub == "probar":
            if not canal_id():
                return editar(msg, "⚠️ Primero conecta un canal.", boton_volver_menu())
            ok = _publicar_en_canal(texto_referencia("Producto de ejemplo", 1, 1.00, "auto", "Cliente", 0, prueba=True))
            bot.send_message(chat_id, "✅ Prueba enviada al canal. Revisa cómo se ve." if ok else
                             "❌ No se pudo publicar. Revisa que el bot sea administrador con permiso de publicar.")
        texto, m = vista_canal()
        return editar(msg, texto, m)

    # ---- Resto: acciones sobre una plataforma (token en partes[1]) ----
    nombre = resolver_token(partes[1]) if len(partes) > 1 else None
    if accion in ("delplat", "delok", "modnom", "moddesc", "prec", "cargar", "lotecar") and not nombre:
        texto, m = vista_catalogo("⚠️ <i>Esa plataforma ya no existe.</i>\n\n")
        return editar(msg, texto, m)

    if accion == "delplat":
        m = InlineKeyboardMarkup(row_width=2)
        m.add(btn("Sí, eliminar", f"delok:{tok(nombre)}", style=ROJO, icono="error"),
              btn("Cancelar", "inicio", style=AZUL, icono="volver"))
        return editar(msg, f"⚠️ <b>¿Eliminar</b> <code>{esc(nombre)}</code> <b>del catálogo?</b>\n\nEsta acción no se puede deshacer.", m)

    if accion == "delok":
        q("DELETE FROM catalogo_db WHERE producto = %s", (nombre,), fetch=None)
        invalidar_cache()
        texto, m = vista_catalogo(f"🗑️ Plataforma <code>{esc(nombre)}</code> eliminada.\n\n")
        return editar(msg, texto, m)

    if accion == "modnom":
        set_estado(uid, "mod_nombre", producto=nombre)
        return editar(msg, f"✏️ <b>CAMBIAR NOMBRE DE:</b> <code>{esc(nombre)}</code>\n\n👉 Escribe el nuevo nombre:", boton_cancelar())

    if accion == "moddesc":
        set_estado(uid, "mod_desc", producto=nombre)
        actual = catalogo_completo()[nombre]["desc"]
        return editar(msg, f"📌 <b>CAMBIAR DESCRIPCIÓN DE:</b> <code>{esc(nombre)}</code>\n\n{TEXTO_SINTAXIS_DESC}\n\n"
                           f"<b>Descripción actual (puedes copiarla y editarla):</b>\n<code>{esc(actual)}</code>", boton_cancelar())

    if accion == "prec":
        set_estado(uid, "precio", producto=nombre)
        actual = catalogo_completo()[nombre]["precio"]
        return editar(msg, f"💵 <b>CAMBIAR PRECIO DE:</b> <code>{esc(nombre)}</code>\n\nActual: <code>{actual:.2f} USDT</code>\n\n👉 Escribe el nuevo precio (Ej: <code>3.50</code>):", boton_cancelar())

    if accion in ("cargar", "lotecar"):
        info = catalogo_completo()[nombre]
        if info["manual"]:
            return editar(msg, f"ℹ️ <b>{esc(nombre)}</b> es de modalidad <b>{MOD_CORTO[info['modalidad']]}</b> y no usa stock.", boton_volver_menu())
        set_estado(uid, "carga", producto=nombre)
        return editar(msg, _texto_pedir_carga(nombre, info), boton_cancelar())


def _texto_pedir_carga(nombre, info):
    if info["infinito"]:
        return (f"♾️ <b>CONTENIDO INFINITO DE</b> <code>{esc(nombre)}</code>\n\n"
                "Envía el <b>link o texto</b> que se entregará a todos los compradores (reemplaza al actual).\n\n"
                "<b>Ejemplo:</b> <code>https://mi-sitio.com/metodo-123</code>")
    if info["modalidad"] == "link":
        return (f"{ic('link')} <b>Cargando links para:</b> <code>{esc(nombre)}</code>\n\n"
                "Envía los links de activación, <b>uno por línea</b>. Solo se aceptan links que empiecen con <code>https://</code>.\n\n"
                "<b>Ejemplo:</b>\n<code>https://ejemplo.com/activar/abc123\nhttps://ejemplo.com/activar/def456</code>")
    if info["modalidad"] == "key":
        return (f"{ic('key')} <b>Cargando keys para:</b> <code>{esc(nombre)}</code>\n\n"
                "Envía las keys o textos, <b>uno por línea</b>. Cada línea se entrega a un comprador, tal cual la escribes.\n"
                "Si lo que entregas tiene <b>varias líneas</b>, separa cada entrega con una línea <code>---</code>.\n\n"
                "<b>Ejemplo (una por línea):</b>\n<code>XXXXX-XXXXX-XXXXX-XXXXX-XXXXX\nYYYYY-YYYYY-YYYYY-YYYYY-YYYYY</code>\n\n"
                "<b>Ejemplo (varias líneas):</b>\n<code>Key: XXXXX-XXXXX-XXXXX\nDescarga: https://sitio.com\n---\nKey: YYYYY-YYYYY-YYYYY\nDescarga: https://sitio.com</code>")
    return (f"{ic('stock')} <b>Cargando cuentas para:</b> <code>{esc(nombre)}</code>\n\n"
            "Envía las cuentas, <b>una por línea</b>, en formato <code>correo:contraseña</code>. Otros formatos serán rechazados.\n\n"
            "<b>Ejemplo:</b>\n<code>usuario1@gmail.com:Clave123\nusuario2@gmail.com:Clave456</code>")


def _pantalla_stock(msg, nombre, info):
    m = InlineKeyboardMarkup(row_width=1)
    t = tok(nombre)
    if info["manual"]:
        return editar(msg, f"ℹ️ <b>{esc(nombre)}</b> es de modalidad <b>{MOD_CORTO[info['modalidad']]}</b>.\n"
                           "Esta modalidad no usa stock (siempre está disponible).", boton_volver_menu())
    if info["infinito"]:
        estado_txt = "Ilimitado" if info["tiene_contenido"] else "Sin contenido cargado"
        m.add(btn("Cambiar contenido a entregar", f"stk:cont:{t}", style=AZUL, icono="infinito"),
              btn("Volver a stock normal", f"stk:normal:{t}", style=ROJO, icono="stock"))
    else:
        estado_txt = f"{info['stock']} disponibles"
        m.add(btn("Poner stock INFINITO", f"stk:inf:{t}", style=AZUL, icono="infinito"),
              btn("Cargar stock", f"stk:carga:{t}", style=VERDE, icono="stock"),
              btn("Vaciar stock disponible", f"stk:vac:{t}", style=ROJO, icono="error"))
    m.add(btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
    return editar(msg, f"♾️ <b>STOCK DE</b> <code>{esc(nombre)}</code>\n\n<b>Estado:</b> {estado_txt}\n"
                       f"<b>Modalidad:</b> {MODALIDADES[info['modalidad']]}", m)


# ---------------------------------------------------------------------
# 📢 Anuncio global (copia exacta del mensaje del admin)
# ---------------------------------------------------------------------
def _markup_difusion(opt):
    if opt in (None, "none"):
        return None
    m = InlineKeyboardMarkup(row_width=1)
    if opt == "cat":
        m.add(btn("Ver Catálogo", "catalogo", style=VERDE, icono="tienda"))
        return m
    nombre = resolver_token(opt)
    info = catalogo_completo().get(nombre) if nombre else None
    if not info:
        return None
    etiqueta = f"COMPRAR | ${info['precio']:.2f}"
    if not info["manual"] and not info["infinito"]:
        etiqueta += f" | {info['stock']}"
    m.add(btn(etiqueta, f"sel:{tok(nombre)}", style=VERDE, icono="tienda"),
          btn("Ver Catálogo", "catalogo", style=AZUL, icono="tienda"))
    return m


def _copiar_a_todos(src_chat, src_msg, markup, aviso_chat, aviso_id):
    todos = [r[0] for r in q("SELECT user_id FROM usuarios")]
    enviados = fallos = 0
    for destino in todos:
        for intento in (1, 2):
            try:
                apihelper.copy_message(TELEGRAM_TOKEN, destino, src_chat, src_msg, reply_markup=markup)
                enviados += 1
                break
            except ApiTelegramException as e:
                if e.error_code == 429 and intento == 1:
                    espera = (getattr(e, "result_json", {}).get("parameters") or {}).get("retry_after", 3)
                    time.sleep(min(int(espera) + 1, 30))
                    continue
                fallos += 1
                break
            except Exception:
                fallos += 1
                break
        time.sleep(0.05)
    editar_o_enviar(aviso_chat, aviso_id,
                    "✅ <b>Difusión completada</b>\n\n"
                    f"📨 Recibieron el anuncio: <code>{enviados}</code>\n"
                    f"🚫 No lo recibieron (bloquearon el bot): <code>{fallos}</code>", boton_volver_menu())


# =====================================================================
# ⌨️ FLUJOS DE TEXTO / FOTO
# =====================================================================
def _texto_o_aviso(message, aviso="⚠️ Envía el dato como texto."):
    if message.content_type != "text":
        bot.reply_to(message, aviso, reply_markup=boton_cancelar())
        return None
    return (message.text or "").strip()


def flujo_mod_nombre(message, st):
    texto = _texto_o_aviso(message)
    if texto is None:
        return True
    if not texto or len(texto) > 80:
        bot.reply_to(message, "❌ El nombre debe tener entre 1 y 80 caracteres.", reply_markup=boton_cancelar())
        return True
    estado.pop(ADMIN_ID, None)
    antiguo = st["producto"]
    try:
        with db() as conn:
            with conn.cursor() as c:
                c.execute("UPDATE catalogo_db SET producto = %s WHERE producto = %s", (texto, antiguo))
                c.execute("UPDATE stock SET producto = %s WHERE producto = %s", (texto, antiguo))
        invalidar_cache()
        bot.reply_to(message, f"✅ <b>Nombre actualizado</b>\n\n<code>{esc(antiguo)}</code> ➔ <code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
    except psycopg2.errors.UniqueViolation:
        bot.reply_to(message, "❌ Ya existe una plataforma con ese nombre.", reply_markup=boton_volver_menu())
    except Exception as e:
        bot.reply_to(message, f"❌ No se pudo renombrar: <code>{esc(e)}</code>", reply_markup=boton_volver_menu())
    return True


def flujo_mod_desc(message, st):
    texto = _texto_o_aviso(message)
    if texto is None:
        return True
    if len(texto) > LIMITE_DESC:
        bot.reply_to(message, f"❌ Muy larga ({len(texto)} caracteres). El máximo es {LIMITE_DESC}.", reply_markup=boton_cancelar())
        return True
    estado.pop(ADMIN_ID, None)
    q("UPDATE catalogo_db SET descripcion = %s WHERE producto = %s", (texto, st["producto"]), fetch=None)
    invalidar_cache()
    bot.reply_to(message, f"✅ <b>Descripción actualizada</b> — <code>{esc(st['producto'])}</code>\n\n"
                          f"<b>Así la verá el cliente:</b>\n<blockquote>{formatear_descripcion(texto)}</blockquote>", reply_markup=boton_volver_menu())
    return True


def flujo_precio(message, st):
    texto = _texto_o_aviso(message)
    if texto is None:
        return True
    try:
        nuevo = round(float(texto.replace(",", ".")), 2)
        if nuevo < 0:
            raise ValueError()
    except ValueError:
        bot.reply_to(message, "❌ Precio inválido. Envía un número (Ej: <code>3.50</code>):", reply_markup=boton_cancelar())
        return True
    estado.pop(ADMIN_ID, None)
    q("UPDATE catalogo_db SET precio = %s WHERE producto = %s", (nuevo, st["producto"]), fetch=None)
    invalidar_cache()
    bot.reply_to(message, f"✅ <b>Precio actualizado</b>\n\n<code>{esc(st['producto'])}</code>\n💵 <code>{nuevo:.2f} USDT</code>", reply_markup=boton_volver_menu())
    return True


# ---------- Agregar plataforma ----------
def _salida(obj, boton, texto, markup):
    """Si la acción viene de un botón se edita el mensaje del bot; si viene de un texto se responde."""
    if boton:
        return editar(obj, texto, markup)
    return bot.reply_to(obj, texto, reply_markup=markup)


def _tras_descripcion(obj, st, boton):
    """Después de la descripción: pregunta por stock infinito (solo email:pass / link) o guarda."""
    if st["modalidad"] in MANUALES:
        return _guardar_plataforma(obj, st, boton)
    st["paso"] = "infinito"
    m = InlineKeyboardMarkup(row_width=2)
    m.add(btn("Sí, infinito", "addi:si", style=AZUL, icono="infinito"),
          btn("No, manual", "addi:no", style=VERDE, icono="stock"))
    m.row(btn("Cancelar", "inicio", style=ROJO, icono="error"))
    texto = ("<b>Paso 5/5 · ¿Stock infinito?</b>\n\n"
             "♾️ <b>Sí</b> — nunca se agota: se entrega siempre el mismo link/texto a todos los compradores.\n"
             "📦 <b>No</b> — stock normal: cargas las cuentas/links tú mismo.")
    return _salida(obj, boton, texto, m)


def _guardar_plataforma(obj, st, boton):
    nombre, precio, mod = st["nombre"], st["precio"], st["modalidad"]
    inf = bool(st.get("infinito")) and mod not in MANUALES
    contenido = st.get("contenido") if inf else None
    q("""INSERT INTO catalogo_db (producto, precio, descripcion, modalidad, infinito, entrega_infinita)
         VALUES (%s, %s, %s, %s, %s, %s)
         ON CONFLICT (producto) DO UPDATE SET precio = EXCLUDED.precio, descripcion = EXCLUDED.descripcion,
              modalidad = EXCLUDED.modalidad, infinito = EXCLUDED.infinito, entrega_infinita = EXCLUDED.entrega_infinita""",
      (nombre, precio, st["desc"], mod, inf, contenido), fetch=None)
    estado.pop(ADMIN_ID, None)
    invalidar_cache()
    extra = ""
    if inf and not contenido:
        extra = "\n\n⚠️ <i>Aún no cargaste el contenido a entregar: la plataforma figura sin stock hasta que lo agregues (/modificar_stock).</i>"
    elif not inf and mod not in MANUALES:
        extra = "\n\n📦 <i>Carga el stock con /cargar.</i>"
    texto = (f"✅ <b>¡Plataforma agregada!</b>\n\n<b>Nombre:</b> <code>{esc(nombre)}</code>\n"
             f"<b>Precio:</b> <code>{precio:.2f} USDT</code>\n<b>Modalidad:</b> {MODALIDADES[mod]}\n"
             f"<b>Stock:</b> {'♾️ infinito' if inf else ('manual' if mod not in MANUALES else 'no aplica')}\n\n"
             f"<b>Descripción:</b>\n<blockquote>{formatear_descripcion(st['desc'])}</blockquote>{extra}")
    return _salida(obj, boton, texto, boton_volver_menu())


def flujo_agregar(message, st):
    paso = st["paso"]
    if paso in ("modalidad", "infinito"):
        bot.reply_to(message, "👆 Elige una opción con los botones del mensaje anterior.", reply_markup=boton_cancelar())
        return True
    texto = _texto_o_aviso(message)
    if texto is None:
        return True

    if paso == "nombre":
        if not texto or len(texto) > 80:
            bot.reply_to(message, "❌ El nombre debe tener entre 1 y 80 caracteres.", reply_markup=boton_cancelar())
            return True
        if texto in catalogo_completo():
            bot.reply_to(message, "❌ Ya existe una plataforma con ese nombre. Envía otro nombre:", reply_markup=boton_cancelar())
            return True
        st["nombre"], st["paso"] = texto, "precio"
        bot.reply_to(message, f"{ic('mas')} <code>{esc(texto)}</code>\n\n<b>Paso 2/5 · Precio</b>\nEnvía el precio en USDT (Ej: <code>3.50</code>):", reply_markup=boton_cancelar())
    elif paso == "precio":
        try:
            precio = round(float(texto.replace(",", ".")), 2)
            if precio < 0:
                raise ValueError()
        except ValueError:
            bot.reply_to(message, "❌ Precio inválido. Envía un número (Ej: <code>4.00</code>):", reply_markup=boton_cancelar())
            return True
        st["precio"], st["paso"] = precio, "modalidad"
        bot.reply_to(message, f"<b>Paso 3/5 ·</b> {TEXTO_MODALIDADES}", reply_markup=teclado_modalidades(lambda mod: f"addm:{mod}"))
    elif paso == "desc":
        if len(texto) > LIMITE_DESC:
            bot.reply_to(message, f"❌ Muy larga ({len(texto)} caracteres). El máximo es {LIMITE_DESC}.", reply_markup=boton_cancelar())
            return True
        st["desc"] = texto
        _tras_descripcion(message, st, False)
    elif paso == "contenido":
        if len(texto) > LIMITE_CONTENIDO:
            bot.reply_to(message, f"❌ Muy largo. El máximo es {LIMITE_CONTENIDO} caracteres.", reply_markup=boton_cancelar())
            return True
        st["contenido"] = texto
        _guardar_plataforma(message, st, False)
    return True


# ---------- Carga de stock (con validación de formato) ----------
def partir_keys(texto):
    """Key: una por línea; o bloques de varias líneas separados por una línea '---'."""
    lineas = [l.rstrip() for l in texto.split("\n")]
    if any(re.fullmatch(r"-{3,}", l.strip()) for l in lineas):
        bloques, actual = [], []
        for l in lineas:
            if re.fullmatch(r"-{3,}", l.strip()):
                bloques.append("\n".join(actual).strip())
                actual = []
            else:
                actual.append(l)
        bloques.append("\n".join(actual).strip())
        return [b for b in bloques if b]
    return [l.strip() for l in lineas if l.strip()]


def validar_lineas(modalidad, lineas):
    if modalidad == "key":
        validas = [l for l in lineas if len(l) <= LIMITE_KEY]
        errores = [(n, l) for n, l in enumerate(lineas, 1) if len(l) > LIMITE_KEY]
        return validas, errores
    regex = RE_EMAIL_PASS if modalidad == "email_pass" else RE_LINK
    validas, errores = [], []
    for n, l in enumerate(lineas, 1):
        (validas if regex.match(l) else errores).append(l if regex.match(l) else (n, l))
    return validas, errores


def flujo_carga(message, st):
    texto = _texto_o_aviso(message, "⚠️ Envía las cuentas/links como texto.")
    if texto is None:
        return True
    producto = st["producto"]
    info = info_fresca(producto)
    if not info:
        estado.pop(ADMIN_ID, None)
        bot.reply_to(message, "❌ Esa plataforma ya no existe.", reply_markup=boton_volver_menu())
        return True

    # --- Stock infinito: un solo contenido (link o texto) ---
    if info["infinito"]:
        if not texto or len(texto) > LIMITE_CONTENIDO:
            bot.reply_to(message, f"❌ Envía un texto de hasta {LIMITE_CONTENIDO} caracteres.", reply_markup=boton_cancelar())
            return True
        q("UPDATE catalogo_db SET entrega_infinita = %s WHERE producto = %s", (texto, producto), fetch=None)
        estado.pop(ADMIN_ID, None)
        invalidar_cache()
        bot.reply_to(message, f"✅ <b>Contenido actualizado</b>\n♾️ <b>{esc(producto)}</b> entregará esto a todos los compradores:\n<code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
        return True

    if info["manual"]:
        estado.pop(ADMIN_ID, None)
        bot.reply_to(message, "ℹ️ Esa modalidad no usa stock.", reply_markup=boton_volver_menu())
        return True

    # --- Stock normal: formato validado según la modalidad ---
    lineas = partir_keys(texto) if info["modalidad"] == "key" else [l.strip() for l in texto.split("\n") if l.strip()]
    if not lineas:
        bot.reply_to(message, "❌ El bloque está vacío.", reply_markup=boton_cancelar())
        return True
    validas, errores = validar_lineas(info["modalidad"], lineas)
    if errores:
        formato = ("<code>correo@dominio.com:contraseña</code>" if info["modalidad"] == "email_pass"
                   else f"un texto de máximo {LIMITE_KEY} caracteres" if info["modalidad"] == "key"
                   else "un link que empiece con <code>https://</code>")
        detalle = "\n".join(f"{n} ➔ <code>{esc(l[:70])}</code>" for n, l in errores[:10])
        mas = f"\n… y {len(errores) - 10} más" if len(errores) > 10 else ""
        bot.reply_to(message,
                     f"❌ <b>Formato inválido — no se cargó nada.</b>\n\nCada línea debe ser {formato}.\n\n"
                     f"<b>Líneas con error ({len(errores)}):</b>\n{detalle}{mas}\n\n"
                     "Corrígelas y envía el bloque completo de nuevo.", reply_markup=boton_cancelar())
        return True
    ya = {r[0] for r in q("SELECT credenciales FROM stock WHERE producto = %s AND vendido = 0", (producto,))}
    unicas = [l for l in dict.fromkeys(validas) if l not in ya]
    duplicadas = len(validas) - len(unicas)
    estado.pop(ADMIN_ID, None)
    if not unicas:
        bot.reply_to(message, "ℹ️ Todo lo enviado ya estaba cargado en el stock. No se agregó nada.", reply_markup=boton_volver_menu())
        return True
    with db() as conn:
        with conn.cursor() as c:
            execute_values(c, "INSERT INTO stock (producto, credenciales) VALUES %s",
                           [(producto, l) for l in unicas], page_size=500)
    invalidar_cache()
    dup = f"\n🔁 Se omitieron <code>{duplicadas}</code> repetidos (ya existentes o duplicados en el bloque)." if duplicadas else ""
    bot.reply_to(message, f"✅ <b>¡Carga exitosa!</b>\nSe agregaron <code>{len(unicas)}</code> accesos a <b>{esc(producto)}</b>.{dup}", reply_markup=boton_volver_menu())
    return True


def flujo_inf_activar(message, st):
    texto = _texto_o_aviso(message)
    if texto is None:
        return True
    if not texto or len(texto) > LIMITE_CONTENIDO:
        bot.reply_to(message, f"❌ Envía un texto de hasta {LIMITE_CONTENIDO} caracteres.", reply_markup=boton_cancelar())
        return True
    producto = st["producto"]
    estado.pop(ADMIN_ID, None)
    q("UPDATE catalogo_db SET infinito = TRUE, entrega_infinita = %s WHERE producto = %s", (texto, producto), fetch=None)
    invalidar_cache()
    bot.reply_to(message, f"✅ <b>Stock infinito activado</b>\n♾️ <b>{esc(producto)}</b> ya nunca se agota y entregará:\n<code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
    return True


# ---------- Íconos ----------
def flujo_icono(message, st):
    clave = st["clave"]
    if message.content_type == "text" and (message.text or "").strip().lower() == "quitar":
        q("DELETE FROM iconos WHERE clave = %s", (clave,), fetch=None)
        _iconos_ids.pop(clave, None)
        estado.pop(ADMIN_ID, None)
        bot.reply_to(message, f"✅ Ícono de «{esc(ICONOS[clave][1])}» quitado (vuelve el emoji normal).", reply_markup=boton_volver_menu())
        return True
    entidades = [e for e in (message.entities or []) if e.type == "custom_emoji"]
    if not entidades:
        bot.reply_to(message, "⚠️ No detecté un <b>emoji premium</b>. Envía un emoji personalizado (o escribe <code>quitar</code>).", reply_markup=boton_cancelar())
        return True
    eid = entidades[0].custom_emoji_id
    q("INSERT INTO iconos (clave, emoji_id) VALUES (%s, %s) ON CONFLICT (clave) DO UPDATE SET emoji_id = EXCLUDED.emoji_id", (clave, eid), fetch=None)
    _iconos_ids[clave] = eid
    estado.pop(ADMIN_ID, None)
    bot.reply_to(message, f"✅ Ícono asignado a «{esc(ICONOS[clave][1])}»: {ic(clave)}\n\nUsa /iconos para asignar más o «Probar íconos».", reply_markup=boton_volver_menu())
    return True


# ---------- Anuncio global ----------
def flujo_noti_global(message, st):
    """Paso 1: el admin envía el anuncio. Paso 2: elige el botón (callbacks 'nt:')."""
    total = q("SELECT COUNT(*) FROM usuarios", fetch="one")[0]
    set_estado(ADMIN_ID, "noti_botones", src_chat=message.chat.id, src_msg=message.message_id, total=total, opt=None)
    m = InlineKeyboardMarkup(row_width=1)
    m.add(btn("Botón: Ver Catálogo", "nt:cat", style=VERDE, icono="tienda"))
    for prod, info in catalogo_completo().items():
        if info["disponible"]:
            m.add(btn(f"Botón COMPRAR: {prod}", f"nt:{tok(prod)}", style=VERDE, icono="pagar"))
    m.add(btn("Enviar sin botones", "nt:none", style=AZUL, icono="rayo"), btn("Cancelar", "inicio", style=ROJO, icono="error"))
    bot.reply_to(message, "✅ <b>Anuncio recibido.</b>\n\n👇 Elige el botón que llevará el mensaje y te mostraré una vista previa:", reply_markup=m)
    return True


def flujo_noti_user(message, st):
    estado.pop(ADMIN_ID, None)
    target = st["target"]
    try:
        if message.content_type == "photo":
            bot.send_photo(target, message.photo[-1].file_id, caption=f"🔔 <b>MENSAJE DEL ADMIN</b>\n\n{esc(message.caption or '')}", reply_markup=boton_volver_menu())
        else:
            bot.send_message(target, f"🔔 <b>MENSAJE DEL ADMIN</b>\n\n{esc(message.text or '')}", reply_markup=boton_volver_menu())
        bot.reply_to(message, f"✅ Mensaje enviado a <code>{target}</code>.", reply_markup=boton_volver_menu())
    except Exception as e:
        bot.reply_to(message, f"❌ Error: <code>{esc(e)}</code>", reply_markup=boton_volver_menu())
    return True


def flujo_soporte(message, st):
    estado.pop(message.from_user.id, None)
    uid, uname, nombre, _ = obtener_datos_usuario(message.from_user.id)
    header = f"🆘 <b>REPORTE DE SOPORTE</b>\n\n👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>"
    m = InlineKeyboardMarkup()
    m.add(btn("Responder", f"noti:{uid}", style=VERDE, icono="soporte"))
    if message.content_type == "photo":
        bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"{header}\n\n{esc(message.caption or '')}", reply_markup=m)
    else:
        bot.send_message(ADMIN_ID, f"{header}\n\n{esc(message.text or '')}", reply_markup=m)
    bot.reply_to(message, "✅ Mensaje enviado a soporte. Te responderemos pronto.", reply_markup=boton_volver_menu())
    return True


# ---------- Compra ----------
def flujo_compra(message, st):
    user_id = message.from_user.id
    es_texto = message.content_type == "text"
    texto = (message.text or "").strip() if es_texto else ""
    paso = st.get("paso")

    if paso == "correo":
        if not es_texto:
            bot.reply_to(message, "⚠️ Envía tu correo como texto.\n<b>Ejemplo:</b> <code>micorreo@gmail.com</code>", reply_markup=boton_cancelar("catalogo"))
            return True
        if not RE_CORREO.match(texto) or len(texto) > 120:
            bot.reply_to(message, "⚠️ Correo inválido. Escríbelo así:\n<b>Ejemplo:</b> <code>micorreo@gmail.com</code>", reply_markup=boton_cancelar("catalogo"))
            return True
        st["correo"] = texto
        mostrar_pago(message.chat.id, st, f"{ic('correo')} <b>Correo:</b> <code>{esc(texto)}</code>\n")
        return True

    if paso == "usuario":
        if not es_texto or not RE_USUARIO_TG.match(texto):
            bot.reply_to(message, "⚠️ Usuario inválido. Envía tu @usuario de Telegram.\n<b>Ejemplo:</b> <code>@miusuario</code>", reply_markup=boton_cancelar("catalogo"))
            return True
        st["tg_user"] = "@" + texto.lstrip("@")
        mostrar_pago(message.chat.id, st, f"{ic('dominio')} <b>Usuario de Telegram:</b> <code>{esc(st['tg_user'])}</code>\n")
        return True

    if paso == "elegir":
        bot.reply_to(message, "👆 Usa los botones de la plataforma para elegir la cantidad y pagar.", reply_markup=boton_volver_menu())
        return True

    # paso "pago" o "id": se espera el ID de orden
    if not es_texto:
        bot.reply_to(message, "⚠️ Envía el <b>ID de orden</b> como texto.\n<b>Ejemplo:</b> <code>463440249244098560</code>", reply_markup=boton_cancelar("catalogo"))
        return True
    order_id = "".join(texto.split())
    if not order_id.isalnum() or len(order_id) < 8:
        if paso == "pago":
            bot.reply_to(message, "ℹ️ Cuando hayas pagado, pulsa <b>Ya pagué</b> en el mensaje del pago y envía tu ID de orden.", reply_markup=boton_cancelar("catalogo"))
        else:
            bot.reply_to(message, "⚠️ ID de orden no válido. Copia el <b>ID de orden</b> exacto de tu comprobante.\n<b>Ejemplo:</b> <code>463440249244098560</code>", reply_markup=boton_cancelar("catalogo"))
        return True

    with _pagos_lock:
        if user_id in _pagos_en_proceso:
            bot.reply_to(message, "⏳ Ya estamos verificando tu pago, espera un momento...")
            return True
        _pagos_en_proceso.add(user_id)
    try:
        procesar_pago(message, st, order_id)
    finally:
        _pagos_en_proceso.discard(user_id)
    return True


def procesar_pago(message, st, order_id):
    user_id = message.from_user.id
    chat_id = message.chat.id
    producto = st["producto"]
    cantidad = st.get("cantidad", 1)
    correo = st.get("correo")
    tg_user = st.get("tg_user")
    total = st["total"]

    if q("SELECT 1 FROM pagos_usados WHERE order_id = %s", (order_id,), "one"):
        bot.reply_to(message, "❌ <b>Este ID de orden ya fue utilizado.</b> No se puede reutilizar un comprobante.", reply_markup=boton_volver_menu())
        return

    espera = bot.reply_to(message, "⏳ <b>Verificando tu pago en Binance...</b>")
    ok, resultado = verificar_pago_binance(order_id, total, st.get("creado_ms"))
    if not ok:
        editar_o_enviar(chat_id, espera.message_id,
                        f"❌ <b>Pago no verificado</b>\n\n{esc(resultado)}\n\n🔁 Puedes pegar el ID de orden nuevamente.",
                        boton_cancelar("catalogo"))
        return

    monto_pagado = resultado
    precio_total = float(total)
    resultado_tx = None
    entregadas = []
    mod_final = None

    with db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO pagos_usados (order_id, user_id, monto) VALUES (%s, %s, %s) "
                      "ON CONFLICT (order_id) DO NOTHING", (order_id, user_id, monto_pagado))
            if c.rowcount == 0:
                resultado_tx = "usado"
            else:
                c.execute("SELECT modalidad, infinito, entrega_infinita FROM catalogo_db WHERE producto = %s", (producto,))
                fila = c.fetchone()
                if not fila:
                    conn.rollback()
                    resultado_tx = "sin_stock"
                else:
                    mod_final, inf_db, ent_db = fila
                    if mod_final in MANUALES:
                        detalle = (f"Activación a correo: {correo or '(no indicado)'}" if mod_final == "correo"
                                   else f"A dominio — Telegram: {tg_user or '(no indicado)'}")
                        c.execute("INSERT INTO historial_compras (user_id, producto, credenciales, precio) VALUES (%s, %s, %s, %s)",
                                  (user_id, producto, detalle, precio_total))
                        c.execute("UPDATE usuarios SET compras = compras + 1 WHERE user_id = %s", (user_id,))
                        resultado_tx = "ok_manual"
                    elif inf_db:
                        if not (ent_db or "").strip():
                            conn.rollback()
                            resultado_tx = "sin_stock"
                        else:
                            entregadas = [ent_db] * cantidad
                            c.execute("UPDATE usuarios SET compras = compras + 1 WHERE user_id = %s", (user_id,))
                            c.execute("INSERT INTO historial_compras (user_id, producto, credenciales, precio) VALUES (%s, %s, %s, %s)",
                                      (user_id, producto, " | ".join(entregadas), precio_total))
                            c.execute("INSERT INTO entregas_pendientes (order_id, user_id, producto, credenciales) "
                                      "VALUES (%s, %s, %s, %s) ON CONFLICT (order_id) DO NOTHING",
                                      (order_id, user_id, producto, " | ".join(entregadas)))
                            resultado_tx = "ok_stock"
                    else:
                        c.execute("SELECT id, credenciales FROM stock WHERE producto = %s AND vendido = 0 "
                                  "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED", (producto, cantidad))
                        filas = c.fetchall()
                        if len(filas) < cantidad:
                            conn.rollback()  # el pago NO queda consumido
                            resultado_tx = "sin_stock"
                        else:
                            ids = [f[0] for f in filas]
                            entregadas = [f[1] for f in filas]
                            c.execute("UPDATE stock SET vendido = 1 WHERE id = ANY(%s)", (ids,))
                            c.execute("UPDATE usuarios SET compras = compras + 1 WHERE user_id = %s", (user_id,))
                            c.execute("INSERT INTO historial_compras (user_id, producto, credenciales, precio) VALUES (%s, %s, %s, %s)",
                                      (user_id, producto, " | ".join(entregadas), precio_total))
                            c.execute("INSERT INTO entregas_pendientes (order_id, user_id, producto, credenciales) "
                                      "VALUES (%s, %s, %s, %s) ON CONFLICT (order_id) DO NOTHING",
                                      (order_id, user_id, producto, " | ".join(entregadas)))
                            resultado_tx = "ok_stock"

    # ---- La base de datos ya terminó; ahora se notifica ----
    if resultado_tx == "usado":
        editar_o_enviar(chat_id, espera.message_id, "❌ <b>Este ID de orden ya fue utilizado.</b>", boton_volver_menu())
        return

    if resultado_tx == "sin_stock":
        invalidar_cache()
        editar_o_enviar(
            chat_id, espera.message_id,
            "⚠️ <b>Pago verificado, pero el producto ya no está disponible.</b>\n\n"
            f"Contacta al administrador (@{esc(ADMIN_USERNAME.lstrip('@'))}) con tu Order ID <code>{order_id}</code> para reposición o reembolso.",
            boton_volver_menu())
        avisar_admin(f"⚠️ <b>PAGO SIN STOCK</b>\nUsuario <code>{user_id}</code> pagó <code>{monto_pagado:.2f} USDT</code> por <b>{esc(producto)}</b> x{cantidad}, pero no hay stock.\n🧾 <code>{order_id}</code>")
        return

    estado.pop(user_id, None)
    uid, uname, nombre, _ = obtener_datos_usuario(user_id)
    responder = InlineKeyboardMarkup()
    responder.add(btn("Responder al cliente", f"noti:{uid}", style=VERDE, icono="soporte"))

    if resultado_tx == "ok_manual":
        publicar_referencia(producto, 1, monto_pagado, mod_final, nombre)
        if mod_final == "correo":
            editar_o_enviar(
                chat_id, espera.message_id,
                f"{ic('ok')} <b>¡PAGO VERIFICADO CON ÉXITO!</b>\n\n"
                f"{ic('tienda')} <b>Producto:</b> {esc(producto)}\n"
                f"{ic('correo')} <b>Correo:</b> <code>{esc(correo)}</code>\n"
                f"{ic('pagar')} <b>Pagado:</b> <code>{monto_pagado:.2f} USDT</code>\n"
                f"<b>Order ID:</b> <code>{order_id}</code>\n\n"
                "⏳ El administrador te enviará la invitación/activación a tu correo y te avisará por aquí.",
                boton_volver_menu())
            avisar_admin(
                "🔔 <b>NUEVA VENTA · ACTIVACIÓN A CORREO</b>\n\n"
                f"👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>\n"
                f"📦 {esc(producto)}\n📧 <code>{esc(correo)}</code>\n"
                f"💵 <code>{monto_pagado:.2f} USDT</code>\n🧾 <code>{order_id}</code>\n\n"
                "👉 Envía la invitación y avísale con el botón.", responder)
        else:
            m = InlineKeyboardMarkup(row_width=1)
            m.add(boton_admin_url(), btn("Menú Principal", "inicio", style=AZUL, icono="menu"))
            editar_o_enviar(
                chat_id, espera.message_id,
                f"{ic('ok')} <b>¡PAGO VERIFICADO CON ÉXITO!</b>\n\n"
                f"{ic('tienda')} <b>Producto:</b> {esc(producto)}\n"
                f"{ic('dominio')} <b>Tu usuario:</b> <code>{esc(tg_user)}</code>\n"
                f"{ic('pagar')} <b>Pagado:</b> <code>{monto_pagado:.2f} USDT</code>\n"
                f"<b>Order ID:</b> <code>{order_id}</code>\n\n"
                "👇 <b>Contacta ahora con el administrador</b> para coordinar la entrega de tus datos. "
                f"Menciona tu Order ID.\n<b>Ejemplo de mensaje:</b> <code>Hola, compré {esc(producto)}. Mi Order ID es {order_id}</code>",
                m)
            avisar_admin(
                "🔔 <b>NUEVA VENTA · A DOMINIO</b>\n\n"
                f"👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>\n"
                f"📦 {esc(producto)}\n💬 Telegram del cliente: <code>{esc(tg_user)}</code>\n"
                f"💵 <code>{monto_pagado:.2f} USDT</code>\n🧾 <code>{order_id}</code>\n\n"
                "👉 El cliente te escribirá para coordinar. También puedes contactarlo con el botón.", responder)
        return

    # ok_stock (normal o infinito)
    invalidar_cache()
    publicar_referencia(producto, cantidad, monto_pagado, "auto", nombre)
    bloque = "\n".join(f"• <code>{esc(x)}</code>" for x in entregadas)
    entregado = editar_o_enviar(
        chat_id, espera.message_id,
        f"{ic('ok')} <b>SA SHOP · ENTREGA CONFIRMADA</b>\n\n"
        f"{ic('tienda')} <b>Producto:</b> <code>{esc(producto)}</code> (x{cantidad})\n"
        f"{ic('pagar')} <b>Pagado:</b> <code>{monto_pagado:.2f} USDT</code>\n"
        f"<b>Order ID:</b> <code>{order_id}</code>\n\n"
        f"{ic('rayo')} <b>Tus accesos:</b>\n{bloque}\n\n"
        "¡Gracias por tu compra en SA SHOP!",
        boton_volver_menu())
    if entregado:
        try:
            q("DELETE FROM entregas_pendientes WHERE order_id = %s", (order_id,), fetch=None)
        except Exception as e:
            log.warning(f"No se pudo limpiar entrega pendiente {order_id}: {e}")
    else:
        avisar_admin(f"⚠️ No se pudo entregar al usuario <code>{user_id}</code> la orden <code>{order_id}</code>. Quedó guardada: el cliente puede usar /mis_entregas.")
    avisar_admin(
        "💰 <b>VENTA AUTOMÁTICA VERIFICADA</b>\n\n"
        f"👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n"
        f"📦 {esc(producto)} (x{cantidad})\n"
        f"💵 <code>{monto_pagado:.2f} USDT</code>\n🧾 <code>{order_id}</code>")


FLUJOS = {
    "mod_nombre": flujo_mod_nombre,
    "mod_desc": flujo_mod_desc,
    "precio": flujo_precio,
    "agregar": flujo_agregar,
    "carga": flujo_carga,
    "inf_activar": flujo_inf_activar,
    "icono": flujo_icono,
    "canal": flujo_canal,
    "noti_global": flujo_noti_global,
    "noti_botones": flujo_noti_global,
    "noti_user": flujo_noti_user,
    "soporte": flujo_soporte,
    "compra": flujo_compra,
}


@bot.message_handler(content_types=["text", "photo"])
def manejar_entradas(message):
    user = message.from_user
    registrar_usuario(user)
    st = estado.get(user.id)
    if st:
        manejador = FLUJOS.get(st["flujo"])
        if manejador and manejador(message, st):
            return

    texto = (message.text or "").strip()
    if message.content_type == "text" and texto.isdigit() and len(texto) >= 15:
        bot.reply_to(message, "⚠️ No tienes ninguna compra en proceso. Elige un producto en el catálogo primero.", reply_markup=boton_volver_menu())
        return
    bot.reply_to(message, "💡 Usa /menu para ir al inicio.", reply_markup=boton_volver_menu())


# =====================================================================
# 🚀 ARRANQUE
# =====================================================================
if __name__ == "__main__":
    keep_alive()
    Thread(target=configurar_menu_comandos, daemon=True).start()
    Thread(target=_mantener_conexiones_calientes, daemon=True).start()
    try:
        catalogo_completo()  # precalienta el caché
    except Exception as e:
        log.warning(f"No se pudo precalentar el catálogo: {e}")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    log.info(f"🚀 SA SHOP Bot iniciado — {BOT_THREADS} hilos, pool BD {DB_POOL_MAX}")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True,
                                 allowed_updates=["message", "callback_query"])
        except Exception as e:
            log.error(f"⚠️ Error en polling: {e}")
            time.sleep(5)
