# -*- coding: utf-8 -*-
"""
⚡ SA SHOP — Tienda automática 24/7 para Telegram
💎 Verificación de pagos con Binance Pay (historial de transacciones recibidas)

Variables de entorno requeridas (Railway -> Variables):
  TELEGRAM_TOKEN, ADMIN_ID, ADMIN_USERNAME, BINANCE_PAY_ID,
  BINANCE_API_KEY, BINANCE_SECRET_KEY, DATABASE_URL
Opcionales:
  BINANCE_BASE_URL (por defecto https://api.binance.com)
  QR_URL      (imagen del QR de pago)
  TITULAR     (nombre mostrado como titular)
  BOT_THREADS (hilos del bot, por defecto 16)
  DB_POOL_MAX (conexiones máximas a la BD, por defecto 20)

🚀 Optimizaciones de esta versión:
  • 16 hilos de atención (antes 2): los clics ya no se encolan.
  • Pool de conexiones a la base de datos (antes: 1 conexión nueva por consulta).
  • Catálogo + stock en 1 sola consulta, con caché que se invalida al vender/cargar.
  • Respuesta inmediata a cada botón (se quita el "relojito" al instante).
  • Anti doble-clic: un doble toque ya no duplica acciones.
  • Difusiones masivas en segundo plano (no bloquean al bot).
"""

import io
import os
import re
import time
import hmac
import html
import hashlib
import logging
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
# 🔐 CONFIGURACIÓN (desde variables de entorno)
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
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "20"))

CACHE_TTL = 15            # segundos (el caché además se invalida al vender/cargar)
DIAS_HISTORIAL = 3        # días hacia atrás que se buscan en el historial de Binance
PRODUCTOS_A_CORREO = ("canva",)   # productos de activación a correo (no usan stock)

if not TELEGRAM_TOKEN or not DATABASE_URL:
    raise SystemExit("❌ Faltan las variables TELEGRAM_TOKEN y/o DATABASE_URL (Railway -> Variables).")

CATALOGO_INICIAL = {
    "Gemini Pro link 18 month": (3.00, "link de activacion"),
    "Max Completa": (1.50, "Cuenta completa email:pass"),
    "Prime Video 6 month": (2.50, "Cuenta completa email:pass"),
    "Spotify Individual": (2.00, "Invitación o cuenta email:pass"),
    "Crunchyroll link 1 Month": (0.50, "Cuenta completa email:pass"),
    "Canva Pro a correo 12 meses": (1.00, "Activación directa a tu correo"),
    "Surf Shark 10 dias": (1.00, "Cuenta Privada email:pass"),
    "Surf Shark 12 meses": (9.00, "Cuenta Privada email:pass"),
    "Apple TV 1 mes Link": (1.00, "Link de Activacion"),
}

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
    """Escapa texto para HTML de Telegram."""
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
    """Conexión del pool; confirma (commit) al salir o revierte si hay error."""
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
    """Consulta con reintento automático si la conexión estaba caída.
    fetch: 'all' | 'one' | None (devuelve filas afectadas)."""
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


def init_db():
    with db() as conn:
        with conn.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS usuarios (
                            user_id BIGINT PRIMARY KEY,
                            username TEXT,
                            nombre TEXT,
                            compras INTEGER DEFAULT 0,
                            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS stock (
                            id SERIAL PRIMARY KEY,
                            producto TEXT,
                            credenciales TEXT,
                            vendido INTEGER DEFAULT 0)""")
            c.execute("""CREATE TABLE IF NOT EXISTS pagos_usados (
                            order_id TEXT PRIMARY KEY,
                            user_id BIGINT,
                            monto REAL,
                            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS historial_compras (
                            id SERIAL PRIMARY KEY,
                            user_id BIGINT,
                            producto TEXT,
                            credenciales TEXT,
                            precio REAL,
                            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS catalogo_db (
                            producto TEXT PRIMARY KEY,
                            precio REAL,
                            descripcion TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS entregas_pendientes (
                            order_id TEXT PRIMARY KEY,
                            user_id BIGINT NOT NULL,
                            producto TEXT NOT NULL,
                            credenciales TEXT NOT NULL,
                            creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            # Índices: consultas de stock e historial mucho más rápidas
            c.execute("CREATE INDEX IF NOT EXISTS idx_stock_prod_vendido ON stock (producto, vendido)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_hist_user ON historial_compras (user_id)")
            c.execute("SELECT COUNT(*) FROM catalogo_db")
            if c.fetchone()[0] == 0:
                for prod, (precio, desc) in CATALOGO_INICIAL.items():
                    c.execute(
                        "INSERT INTO catalogo_db (producto, precio, descripcion) VALUES (%s, %s, %s) "
                        "ON CONFLICT (producto) DO NOTHING", (prod, precio, desc))


init_db()


# =====================================================================
# 📚 CATÁLOGO + STOCK (1 sola consulta, con caché inteligente)
# =====================================================================
_cat = {"snap": None, "ver": 0, "bg": False}
_cat_lock = Lock()
_ver_lock = Lock()


def es_a_correo(producto):
    return any(k in producto.casefold() for k in PRODUCTOS_A_CORREO)


def _construir_catalogo():
    v = _cat["ver"]
    filas = q("""SELECT c.producto, c.precio, c.descripcion, COALESCE(s.n, 0)
                 FROM catalogo_db c
                 LEFT JOIN (SELECT producto, COUNT(*) AS n FROM stock
                            WHERE vendido = 0 GROUP BY producto) s ON s.producto = c.producto
                 ORDER BY c.producto""")
    data = {}
    for prod, precio, desc, n in filas:
        data[prod] = {"precio": round(float(precio or 0), 2), "desc": desc or "",
                      "stock": int(n), "correo": es_a_correo(prod)}
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
    """{producto: {precio, desc, stock, correo}} — instantáneo si está en caché."""
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
    """Convierte el token corto del botón en el nombre real del producto."""
    catalogo_completo()
    nombre = _cat["snap"][1].get(token)
    if nombre is None:
        invalidar_cache()
        catalogo_completo()
        nombre = _cat["snap"][1].get(token)
    return nombre


def tok(nombre):
    return hashlib.sha1(nombre.encode("utf-8")).hexdigest()[:12]


def buscar_producto_nombre(texto_busqueda):
    t = texto_busqueda.strip().lower()
    for prod in catalogo_completo():
        if prod.lower() == t or t in prod.lower():
            return prod
    return None


def precio_en_vivo(producto):
    fila = q("SELECT precio FROM catalogo_db WHERE producto = %s", (producto,), "one")
    return round(float(fila[0]), 2) if fila else 0.0


def stock_en_vivo(producto):
    return q("SELECT COUNT(*) FROM stock WHERE producto = %s AND vendido = 0", (producto,), "one")[0]


def a_dec(x):
    return Decimal(str(round(float(x), 2)))


# =====================================================================
# 👤 USUARIOS
# =====================================================================
_usuarios_vistos = {}


def registrar_usuario(user):
    """Guarda al usuario solo si es nuevo o cambió su nombre/username (sin consulta en cada clic)."""
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
# 🎨 BOTONES DE COLOR  (🟢 verde = bueno/stock · 🔴 rojo = sin stock/cancelar · 🔵 azul = navegación)
# =====================================================================
VERDE, ROJO, AZUL = "success", "danger", "primary"
_estilo_ok = [True]


def btn(texto, cb=None, url=None, style=None):
    kw = {}
    if cb is not None:
        kw["callback_data"] = cb
    if url is not None:
        kw["url"] = url
    if style and _estilo_ok[0]:
        try:
            return _TgButton(texto, style=style, **kw)
        except TypeError:
            _estilo_ok[0] = False
            log.warning("Tu versión de pyTelegramBotAPI no soporta colores en botones; actualízala.")
    return _TgButton(texto, **kw)


def boton_volver_menu():
    m = InlineKeyboardMarkup()
    m.add(btn("🏠 Menú Principal", "inicio", style=AZUL))
    return m


def boton_cancelar():
    m = InlineKeyboardMarkup()
    m.add(btn("❌ Cancelar", "inicio", style=ROJO))
    return m


# =====================================================================
# 🧰 UTILIDADES DE TELEGRAM
# =====================================================================
def configurar_menu_comandos():
    try:
        publicos = [
            BotCommand("menu", "🏠 Menú Principal"),
            BotCommand("buy", "🛒 Catálogo de Compras"),
            BotCommand("stock", "📊 Consultar Stock"),
            BotCommand("mis_entregas", "📦 Recuperar entrega pendiente"),
        ]
        bot.set_my_commands(publicos)
        admin = publicos + [
            BotCommand("cmds", "👑 Panel de Administración"),
            BotCommand("cargar", "📥 Subir Stock"),
            BotCommand("cargar_lote", "📦 Carga Masiva"),
            BotCommand("agregar_plataforma", "➕ Agregar plataforma"),
            BotCommand("quitar_plataforma", "🗑️ Eliminar plataforma"),
            BotCommand("modificar_nombre", "✏️ Cambiar nombre"),
            BotCommand("modificar_precio", "💵 Cambiar precio"),
            BotCommand("modificar_descripcion", "📌 Cambiar descripción"),
            BotCommand("noti", "📢 Anuncio Global"),
            BotCommand("stats", "📈 Estadísticas"),
            BotCommand("testbinance", "🧪 Probar conexión Binance"),
            BotCommand("testip", "🌐 Ver IP de Railway"),
        ]
        bot.set_my_commands(admin, scope=BotCommandScopeChat(ADMIN_ID))
    except Exception as e:
        log.warning(f"No se pudo configurar el menú de comandos: {e}")


def _responder(call, texto=None, alerta=False):
    try:
        bot.answer_callback_query(call.id, texto, show_alert=alerta)
    except Exception:
        pass


def _responder_async(call, texto=None, alerta=False):
    """Quita el 'relojito' del botón en paralelo, sin retrasar la respuesta real."""
    Thread(target=_responder, args=(call, texto, alerta), daemon=True).start()


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
    """Intenta editar; si falla, envía un mensaje nuevo. Devuelve True si el usuario lo recibió."""
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


def avisar_admin(texto):
    try:
        bot.send_message(ADMIN_ID, texto)
    except Exception as e:
        log.error(f"No se pudo avisar al admin: {e}")


# =====================================================================
# 💎 VERIFICACIÓN DE PAGOS EN BINANCE
# =====================================================================
# El pago normal por Binance Pay (el "ID de orden" de la app) se consulta en el
# historial de TU cuenta: GET /sapi/v1/pay/transactions
# La API Key debe ser una API normal de Binance con permiso "Enable Reading".
# =====================================================================
_binance_lock = Lock()
_binance_last = [0.0]


def _binance_get(path, params):
    # Se serializan las consultas con una pausa mínima para no exceder el límite de Binance.
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
    """Comprueba una transferencia recibida en el historial autenticado de Binance.
    Retorna (True, monto_float) o (False, mensaje)."""
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
            status, data = _binance_get(
                "/sapi/v1/pay/transactions",
                {"startTimestamp": inicio, "endTimestamp": fin, "limit": 100},
            )
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
        moneda = str(encontrado.get("currency", "")).upper()
        if moneda != "USDT":
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
def vista_menu_principal(user):
    uname = f"@{user.username}" if user.username else "Sin @username"
    nombre = user.first_name or "Cliente"
    es_admin = user.id == ADMIN_ID
    rango = "👑 ADMINISTRADOR" if es_admin else "👤 Cliente"
    texto = (
        "⚡ <b>SA SHOP — TIENDA AUTOMÁTICA 24/7</b> ⚡\n\n"
        f"┏ 👤 <b>Usuario:</b> {esc(nombre)} (<code>{esc(uname)}</code>)\n"
        f"┣ 🎖️ <b>Rol:</b> {rango}\n"
        f"┗ 🆔 <b>ID:</b> <code>{user.id}</code>\n\n"
        "👇 Selecciona una opción:"
    )
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        btn("🛒 Ver Catálogo y Comprar", "catalogo", style=VERDE),
        btn("👤 Mi Perfil e Historial", "perfil", style=AZUL),
        btn("🛠️ Soporte y Ayuda", "soporte", style=AZUL),
    )
    if es_admin:
        m.add(btn("👑 Panel de Administración", "panel_admin", style=AZUL))
    return texto, m


def vista_catalogo(aviso=""):
    m = InlineKeyboardMarkup(row_width=1)
    for prod, info in catalogo_completo().items():
        precio = info["precio"]
        if info["correo"]:
            txt, ok = f"🟢 {prod} | ${precio:.2f} | ✉️ A correo", True
        elif info["stock"] > 0:
            txt, ok = f"🟢 {prod} | ${precio:.2f} | 📦 {info['stock']}", True
        else:
            txt, ok = f"🔴 {prod} | ${precio:.2f} | ❌ Sin stock", False
        m.add(btn(txt, f"sel:{tok(prod)}", style=VERDE if ok else ROJO))
    m.add(btn("🏠 Menú Principal", "inicio", style=AZUL))
    texto = (
        f"{aviso}"
        "🛒 <b>CATÁLOGO DE PRODUCTOS</b>\n\n"
        "🟢 Disponible  |  🔴 Sin stock\n"
        "📦 Unidades en stock  |  ✉️ Activación a correo\n\n"
        "👇 Selecciona el producto que deseas comprar:"
    )
    return texto, m


def vista_sin_stock(producto):
    texto = (
        "🔴 <b>SIN STOCK</b>\n\n"
        f"😔 Por ahora <b>{esc(producto)}</b> no tiene unidades disponibles.\n\n"
        "🔄 Estamos reponiendo el inventario, vuelve a consultar en un rato.\n"
        "💬 Si lo necesitas con urgencia, escríbele al administrador."
    )
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        btn("⬅️ Volver al Catálogo", "catalogo", style=AZUL),
        btn("💬 Contactar al Administrador", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}", style=AZUL),
        btn("🏠 Menú Principal", "inicio", style=AZUL),
    )
    return texto, m


def menu_cantidad(producto, cantidad, precio_unit, stock):
    total = round(cantidad * precio_unit, 2)
    texto = (
        "📦 <b>SELECCIONA LA CANTIDAD</b>\n\n"
        f"• Producto: <code>{esc(producto)}</code>\n"
        f"• Stock disponible: <code>{stock}</code>\n"
        f"• Precio unitario: <code>{precio_unit:.2f} USDT</code>\n\n"
        f"🔢 Cantidad: <b>{cantidad}</b>\n"
        f"💵 <b>Total a pagar:</b> <code>{total:.2f} USDT</code>"
    )
    t = tok(producto)
    m = InlineKeyboardMarkup(row_width=3)
    m.add(
        btn("➖ Quitar", f"cant:menos:{t}", style=ROJO),
        btn(f"🔢 {cantidad}", "nada"),
        btn("➕ Agregar", f"cant:mas:{t}", style=VERDE),
    )
    m.add(btn(f"💳 Pagar {total:.2f} USDT con Binance Pay", f"cant:ok:{t}", style=VERDE))
    m.add(btn("⬅️ Volver al Catálogo", "catalogo", style=AZUL))
    return texto, m


def texto_instrucciones_pago(producto, cantidad, total, correo=None):
    extra = f"📧 Correo: <code>{esc(correo)}</code>\n" if correo else ""
    return (
        "💎 <b>PAGO DIRECTO CON BINANCE PAY</b> 💎\n\n"
        f"📦 <b>Producto:</b> {esc(producto)} (x{cantidad})\n{extra}\n"
        "📌 <b>DETALLES DE PAGO:</b>\n"
        "• <b>MÉTODO:</b> Binance Pay\n"
        f"• <b>MONTO:</b> <code>{total:.2f} USDT</code>\n"
        f"• <b>ID DE BINANCE PAY:</b> <code>{esc(BINANCE_PAY_ID)}</code>\n"
        f"• <b>TITULAR:</b> {esc(TITULAR)}\n\n"
        "📸 Escanea el QR o transfiere al ID indicado.\n\n"
        "👇 Luego pega aquí el <b>ID de orden</b> de tu pago para validarlo y recibir tu entrega automática."
    )


def enviar_pago(chat_id, texto):
    m = InlineKeyboardMarkup()
    m.add(btn("❌ Cancelar", "catalogo", style=ROJO))
    try:
        bot.send_photo(chat_id, QR_URL, caption=texto, reply_markup=m)
    except Exception:
        bot.send_message(chat_id, texto, reply_markup=m)


TEXTO_CMDS_ADMIN = (
    "👑 <b>PANEL DE CONTROL ADMIN</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "📦 <b>CATÁLOGO Y STOCK</b>\n"
    "• /agregar_plataforma ➔ Crear producto\n"
    "• /quitar_plataforma ➔ Eliminar producto\n"
    "• /modificar_nombre ➔ Cambiar nombre\n"
    "• /modificar_precio ➔ Cambiar precio\n"
    "• /modificar_descripcion ➔ Cambiar descripción\n"
    "• /cargar ➔ Subir cuentas\n"
    "• /cargar_lote ➔ Carga masiva\n"
    "• /stock ➔ Reporte + TXT general\n"
    "• <code>/borrar [ID]</code> ➔ Eliminar cuenta\n"
    "• <code>/vaciar [nombre]</code> ➔ Limpiar stock no vendido\n\n"
    "📢 <b>COMUNICACIÓN</b>\n"
    "• /noti ➔ Anuncio global\n"
    "• <code>/enviar [ID/@user]</code> ➔ Mensaje privado\n\n"
    "👥 <b>CLIENTES Y FINANZAS</b>\n"
    "• <code>/historial [ID/@user]</code> ➔ Compras del cliente\n"
    "• /stats ➔ Métricas de la tienda\n"
    "• /usuarios ➔ Base de clientes en TXT\n\n"
    "🧪 <b>DIAGNÓSTICO</b>\n"
    "• /testbinance ➔ Probar conexión con Binance\n"
    "• /testip ➔ Ver IP pública de Railway\n"
    "• /mis_entregas ➔ Recuperar entrega pendiente"
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
        lineas = ["📊 <b>ESTADO DEL INVENTARIO</b>\n"]
        for prod, info in catalogo.items():
            if info["correo"]:
                lineas.append(f"🟢 <b>{esc(prod)}</b> ➔ <code>{info['precio']:.2f} USDT</code> | ✉️ {esc(info['desc'])}")
            else:
                cant = info["stock"]
                color = "🟢" if cant > 0 else "🔴"
                lineas.append(f"{color} <b>{esc(prod)}</b> ➔ <code>{cant}</code> disp. (<code>{info['precio']:.2f} USDT</code>) | 📌 {esc(info['desc'])}")
        bot.reply_to(message, "\n".join(lineas), reply_markup=boton_volver_menu())

        if es_admin:
            filas = q("SELECT id, producto, credenciales FROM stock WHERE vendido = 0 ORDER BY producto, id")
            if filas:
                txt = ["=== REPORTE GENERAL DE STOCK DISPONIBLE ===", ""]
                actual = ""
                for sid, prod, cred in filas:
                    if prod != actual:
                        actual = prod
                        txt.append(f"\n--- [{prod.upper()}] ---")
                    txt.append(f"ID: {sid} | {cred}")
                enviar_archivo_txt(message.chat.id, "\n".join(txt), "stock_general.txt", "🔐 <b>Cuentas en inventario</b>")
        return

    prod = buscar_producto_nombre(partes[1])
    if not prod:
        bot.reply_to(message, f"❌ No se encontró <code>{esc(partes[1])}</code> en el catálogo.", reply_markup=boton_volver_menu())
        return

    info = catalogo[prod]
    if info["correo"]:
        bot.reply_to(message, f"📦 <b>{esc(prod)}</b>\n💵 Precio: <code>{info['precio']:.2f} USDT</code>\n📌 {esc(info['desc'])}", reply_markup=boton_volver_menu())
        return

    cant = info["stock"]
    color = "🟢" if cant > 0 else "🔴"
    bot.reply_to(
        message,
        f"📦 <b>Inventario:</b> {esc(prod)}\n💵 <b>Precio:</b> <code>{info['precio']:.2f} USDT</code>\n"
        f"📌 <b>Tipo:</b> {esc(info['desc'])}\n{color} <b>Disponibles:</b> <code>{cant}</code>",
        reply_markup=boton_volver_menu(),
    )
    if es_admin and cant > 0:
        filas = q("SELECT id, credenciales FROM stock WHERE producto = %s AND vendido = 0 ORDER BY id", (prod,))
        contenido = f"=== STOCK DE {prod.upper()} ({len(filas)}) ===\n\n" + "\n".join(f"ID: {f[0]} | {f[1]}" for f in filas)
        enviar_archivo_txt(message.chat.id, contenido, f"stock_{prod.lower().replace(' ', '_')}.txt", f"🔐 <b>Credenciales de {esc(prod)}</b>")


@bot.message_handler(commands=["mis_entregas"])
def cmd_mis_entregas(message):
    """Reenvía entregas que quedaron pendientes tras un error de Telegram."""
    uid = message.from_user.id
    pendientes = q("SELECT order_id, producto, credenciales FROM entregas_pendientes WHERE user_id = %s ORDER BY creado LIMIT 10", (uid,))
    if not pendientes:
        bot.reply_to(message, "📦 No tienes entregas pendientes. Consulta /menu para ver tu historial.", reply_markup=boton_volver_menu())
        return
    for oid, prod, cred in pendientes:
        bot.send_message(message.chat.id, f"📦 <b>Entrega recuperada</b>\nProducto: {esc(prod)}\nAccesos: <code>{esc(cred)}</code>")
        q("DELETE FROM entregas_pendientes WHERE order_id = %s AND user_id = %s", (oid, uid), fetch=None)


# =====================================================================
# 👑 COMANDOS ADMIN
# =====================================================================
def solo_admin(message):
    return message.from_user.id == ADMIN_ID


@bot.message_handler(commands=["cmds", "admin", "ayuda"])
def cmd_cmds(message):
    if solo_admin(message):
        bot.reply_to(message, TEXTO_CMDS_ADMIN, reply_markup=boton_volver_menu())


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
        status, data = _binance_get(
            "/sapi/v1/pay/transactions",
            {"startTimestamp": ahora - 7 * 24 * 3600 * 1000, "endTimestamp": ahora, "limit": 10},
        )
        if status == 200 and str(data.get("code", "000000")) in ("000000", "0"):
            lista = data.get("data") or []
            resumen = "\n".join(
                f"• <code>{esc(t.get('transactionId'))}</code> | {esc(t.get('amount'))} {esc(t.get('currency'))} | {esc(t.get('orderType'))}"
                for t in lista[:5]
            ) or "(sin movimientos en los últimos 7 días)"
            texto = f"✅ <b>Conexión con Binance OK</b>\n\nÚltimas transacciones detectadas ({len(lista)}):\n{resumen}"
        else:
            texto = f"❌ <b>Error de Binance</b>\nHTTP: <code>{status}</code>\n<code>{esc(str(data)[:500])}</code>"
    except Exception as e:
        texto = f"❌ Error de conexión: <code>{esc(e)}</code>"
    editar_o_enviar(message.chat.id, msg.message_id, texto, boton_volver_menu())


@bot.message_handler(commands=["agregar_plataforma"])
def cmd_agregar_plataforma(message):
    if not solo_admin(message):
        return
    set_estado(ADMIN_ID, "agregar", paso="nombre")
    bot.reply_to(message, "➕ <b>CREAR NUEVA PLATAFORMA</b>\n\nPaso 1/3: Envía el <b>nombre</b> (Ej: <code>Paramount+</code>):", reply_markup=boton_cancelar())


def _menu_catalogo_admin(message, prefijo, icono, titulo, estilo, con_precio=False, con_desc=False):
    limpiar_estados_usuario(ADMIN_ID)
    m = InlineKeyboardMarkup(row_width=1)
    for prod, info in catalogo_completo().items():
        extra = f" ({info['precio']:.2f} USDT)" if con_precio else (f" ({info['desc']})" if con_desc else "")
        m.add(btn(f"{icono} {prod}{extra}", f"{prefijo}:{tok(prod)}", style=estilo))
    m.add(btn("🏠 Menú Principal", "inicio", style=AZUL))
    bot.reply_to(message, titulo, reply_markup=m)


@bot.message_handler(commands=["quitar_plataforma"])
def cmd_quitar(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "delplat", "🗑️", "🗑️ <b>ELIMINAR PLATAFORMA</b>\n\nSelecciona el producto a retirar:", ROJO)


@bot.message_handler(commands=["modificar_nombre"])
def cmd_mod_nombre(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "modnom", "✏️", "✏️ <b>MODIFICAR NOMBRE</b>\n\nSelecciona el producto:", AZUL)


@bot.message_handler(commands=["modificar_precio"])
def cmd_mod_precio(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "prec", "💵", "💵 <b>MODIFICAR PRECIO</b>\n\nSelecciona el producto:", AZUL, con_precio=True)


@bot.message_handler(commands=["modificar_descripcion"])
def cmd_mod_desc(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "moddesc", "📌", "📌 <b>MODIFICAR DESCRIPCIÓN</b>\n\nSelecciona el producto:", AZUL, con_desc=True)


def _menu_carga(message, prefijo, titulo):
    limpiar_estados_usuario(ADMIN_ID)
    m = InlineKeyboardMarkup(row_width=2)
    botones = []
    for prod, info in catalogo_completo().items():
        if info["correo"]:
            continue
        cant = info["stock"]
        botones.append(btn(f"{'🟢' if cant > 0 else '🔴'} {prod} ({cant})", f"{prefijo}:{tok(prod)}",
                           style=VERDE if cant > 0 else ROJO))
    if botones:
        m.add(*botones)
    m.add(btn("🏠 Menú Principal", "inicio", style=AZUL))
    bot.reply_to(message, titulo, reply_markup=m)


@bot.message_handler(commands=["cargar"])
def cmd_cargar(message):
    if solo_admin(message):
        _menu_carga(message, "cargar", "📥 <b>CARGA DE INVENTARIO</b>\n\nSelecciona el producto (formato <code>email:pass</code> o <code>link</code>):")


@bot.message_handler(commands=["cargar_lote"])
def cmd_cargar_lote(message):
    if solo_admin(message):
        _menu_carga(message, "lotecar", "📦 <b>CARGA MASIVA</b>\n\nSelecciona el producto para el lote:")


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
    bot.reply_to(
        message,
        f"📋 <b>EXPEDIENTE DEL CLIENTE</b>\n\n👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>\n🛍️ Compras: <code>{len(compras)}</code>",
        reply_markup=boton_volver_menu(),
    )
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
    bot.reply_to(message, "📢 <b>ANUNCIO GLOBAL</b>\n\nEnvía la <b>imagen con texto</b> o el <b>mensaje</b> a difundir:", reply_markup=boton_cancelar())


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
    bot.reply_to(message, f"🗑️ Cuenta <code>{partes[1]}</code> eliminada." if n else f"❌ No hay cuenta disponible con ID <code>{partes[1]}</code>.", reply_markup=boton_volver_menu())


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
        reply_markup=boton_volver_menu(),
    )


@bot.message_handler(commands=["usuarios"])
def cmd_usuarios(message):
    if not solo_admin(message):
        return
    filas = q("SELECT user_id, username, nombre, compras, fecha_registro FROM usuarios ORDER BY fecha_registro DESC")
    lineas = ["=== BASE DE DATOS DE CLIENTES ===", ""]
    for uid, uname, nom, comp, fec in filas:
        lineas.append(f"ID: {uid} | @{uname or 'Sin_user'} | {nom} | Compras: {comp} | Registro: {fec}")
    enviar_archivo_txt(message.chat.id, "\n".join(lineas), "clientes_tienda.txt", f"👥 <b>{len(filas)}</b> clientes registrados")


# =====================================================================
# 🔘 CALLBACKS (botones)
# =====================================================================
@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    data = call.data or ""
    uid = call.from_user.id
    accion = data.split(":", 1)[0]

    if accion == "nada":
        return _responder_async(call)

    es_cantidad = data.startswith("cant:mas:") or data.startswith("cant:menos:")
    clave = (uid, data)

    if not es_cantidad:
        # Anti doble-clic: ignora toques repetidos mientras se procesa o recién procesado.
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


def _manejar_callback(call, data):
    user = call.from_user
    uid = user.id
    msg = call.message
    chat_id = msg.chat.id
    es_admin = uid == ADMIN_ID
    partes = data.split(":")
    accion = partes[0]
    registrar_usuario(user)

    # ---------- Navegación ----------
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
            editar(msg, TEXTO_CMDS_ADMIN, boton_volver_menu())
        return

    if accion == "perfil":
        _responder_async(call)
        uid_, uname, nombre, compras = obtener_datos_usuario(uid)
        ultimas = q("SELECT producto, credenciales FROM historial_compras WHERE user_id = %s ORDER BY id DESC LIMIT 5", (uid,))
        hist = ""
        if ultimas:
            hist = "\n\n📋 <b>Tus últimas compras:</b>\n" + "\n".join(f"• <b>{esc(p)}</b>: <code>{esc(cr)}</code>" for p, cr in ultimas)
        texto = (
            "👤 <b>TU CUENTA</b>\n\n"
            f"🏷️ Nombre: {esc(nombre)}\n"
            f"🔗 Username: <code>{esc(uname)}</code>\n"
            f"🆔 ID: <code>{uid_}</code>\n"
            f"🛍️ Compras totales: <code>{compras}</code>{hist}"
        )
        return editar(msg, texto, boton_volver_menu())

    if accion == "soporte":
        _responder_async(call)
        set_estado(uid, "soporte")
        m = InlineKeyboardMarkup()
        m.add(
            btn("💬 Soporte Directo", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}", style=AZUL),
            btn("🏠 Menú Principal", "inicio", style=AZUL),
        )
        return editar(msg, "🛠️ <b>CENTRO DE SOPORTE</b>\n\nEscribe tu consulta o reporte aquí en el chat y el administrador te atenderá:", m)

    # ---------- Responder a un cliente (admin) ----------
    if accion == "noti":
        _responder_async(call)
        if es_admin:
            try:
                target = int(partes[1])
            except (IndexError, ValueError):
                return
            set_estado(ADMIN_ID, "noti_user", target=target)
            bot.send_message(chat_id, f"📨 Escribe tu respuesta para <code>{target}</code>:", reply_markup=boton_cancelar())
        return

    # ---------- Selección de producto (cliente) ----------
    if accion == "sel":
        nombre = resolver_token(partes[1]) if len(partes) > 1 else None
        info = catalogo_completo().get(nombre) if nombre else None
        if not info:
            _responder(call, "Este producto ya no está disponible.", True)
            texto, m = vista_catalogo()
            return editar(msg, texto, m)

        precio = info["precio"]
        if info["correo"]:
            _responder_async(call)
            set_estado(uid, "compra", tipo="correo", producto=nombre)
            m = InlineKeyboardMarkup()
            m.add(btn("❌ Cancelar", "catalogo", style=ROJO))
            return editar(msg,
                          f"📧 <b>{esc(nombre.upper())}</b> (activación a correo)\n\n"
                          f"💵 Precio: <code>{precio:.2f} USDT</code>\n\n"
                          "👉 Envía ahora tu <b>correo electrónico</b> para continuar:", m)

        if info["stock"] <= 0:
            # 🔴 SIN STOCK → aviso inmediato + mensaje del bot
            _responder(call, f"🔴 Sin stock en {nombre}")
            texto, m = vista_sin_stock(nombre)
            return editar(msg, texto, m)

        _responder_async(call)
        set_estado(uid, "compra", tipo="stock", producto=nombre, cantidad=1, creado_ms=int(time.time() * 1000))
        texto, m = menu_cantidad(nombre, 1, precio, info["stock"])
        return editar(msg, texto, m)

    # ---------- Cantidad / pago (cliente) ----------
    if accion == "cant":
        sub = partes[1]
        nombre = resolver_token(partes[2]) if len(partes) > 2 else None
        st = estado.get(uid)
        if (not nombre or not st or st.get("flujo") != "compra" or st.get("tipo") != "stock"
                or st.get("producto") != nombre):
            return _responder(call, "La sesión expiró. Vuelve al catálogo.", True)

        if sub == "ok":
            _responder_async(call)
            stock = stock_en_vivo(nombre)
            cantidad = st.get("cantidad", 1)
            if stock <= 0:
                invalidar_cache()
                texto, m = vista_sin_stock(nombre)
                return editar(msg, texto, m)
            if stock < cantidad:
                invalidar_cache()
                st["cantidad"] = stock
                texto, m = menu_cantidad(nombre, stock, precio_en_vivo(nombre), stock)
                return editar(msg, "⚠️ <i>El stock cambió, ajustamos tu cantidad.</i>\n\n" + texto, m)
            precio_unit = precio_en_vivo(nombre)
            total = a_dec(precio_unit) * cantidad
            st["total"] = total
            st["creado_ms"] = int(time.time() * 1000)
            enviar_pago(chat_id, texto_instrucciones_pago(nombre, cantidad, total))
            return _borrar(chat_id, msg.message_id)

        info = catalogo_completo().get(nombre)
        stock = info["stock"] if info else 0
        cantidad = st.get("cantidad", 1)
        if sub == "mas":
            if cantidad >= stock:
                return _responder(call, f"Máximo disponible: {stock}")
            cantidad += 1
        elif sub == "menos":
            if cantidad <= 1:
                return _responder(call, "La cantidad mínima es 1")
            cantidad -= 1
        _responder_async(call)
        st["cantidad"] = cantidad
        texto, m = menu_cantidad(nombre, cantidad, info["precio"] if info else 0, stock)
        return editar(msg, texto, m)

    # ---------- Todo lo que sigue es solo para el admin ----------
    if not es_admin:
        return _responder(call)

    _responder_async(call)
    nombre = resolver_token(partes[1]) if len(partes) > 1 else None

    if accion in ("delplat", "delok", "modnom", "moddesc", "prec", "cargar", "lotecar") and not nombre:
        texto, m = vista_catalogo("⚠️ <i>Ese producto ya no existe.</i>\n\n")
        return editar(msg, texto, m)

    if accion == "delplat":
        m = InlineKeyboardMarkup(row_width=2)
        m.add(btn("✅ Sí, eliminar", f"delok:{tok(nombre)}", style=ROJO), btn("↩️ Cancelar", "inicio", style=AZUL))
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
        return editar(msg, f"📌 <b>CAMBIAR DESCRIPCIÓN DE:</b> <code>{esc(nombre)}</code>\n\n👉 Escribe la nueva descripción (Ej: <code>email:pass</code>, <code>link</code>):", boton_cancelar())

    if accion == "prec":
        set_estado(uid, "precio", producto=nombre)
        actual = catalogo_completo()[nombre]["precio"]
        return editar(msg, f"💵 <b>CAMBIAR PRECIO DE:</b> <code>{esc(nombre)}</code>\n\nActual: <code>{actual:.2f} USDT</code>\n\n👉 Escribe el nuevo precio (Ej: <code>3.50</code>):", boton_cancelar())

    if accion == "cargar":
        set_estado(uid, "carga", producto=nombre)
        return editar(msg, f"📥 <b>Cargando stock para:</b> <code>{esc(nombre)}</code>\n\nEnvía las cuentas o links, una por línea:", boton_cancelar())

    if accion == "lotecar":
        set_estado(uid, "carga", producto=nombre)
        return editar(msg, f"📦 <b>Carga masiva para:</b> <code>{esc(nombre)}</code>\n\nPega tu bloque completo, una cuenta/link por línea:", boton_cancelar())


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
    estado.pop(ADMIN_ID, None)
    q("UPDATE catalogo_db SET descripcion = %s WHERE producto = %s", (texto, st["producto"]), fetch=None)
    invalidar_cache()
    bot.reply_to(message, f"✅ <b>Descripción actualizada</b>\n\n📦 <code>{esc(st['producto'])}</code>\n📌 <code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
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
    bot.reply_to(message, f"✅ <b>Precio actualizado</b>\n\n📦 <code>{esc(st['producto'])}</code>\n💵 <code>{nuevo:.2f} USDT</code>", reply_markup=boton_volver_menu())
    return True


def flujo_agregar(message, st):
    texto = _texto_o_aviso(message)
    if texto is None:
        return True
    if st["paso"] == "nombre":
        st["nombre"] = texto
        st["paso"] = "precio"
        bot.reply_to(message, f"➕ Producto: <code>{esc(texto)}</code>\n\nPaso 2/3: Envía el <b>precio</b> en USDT (Ej: <code>3.50</code>):", reply_markup=boton_cancelar())
    elif st["paso"] == "precio":
        try:
            precio = round(float(texto.replace(",", ".")), 2)
            if precio < 0:
                raise ValueError()
        except ValueError:
            bot.reply_to(message, "❌ Precio inválido. Envía un número (Ej: <code>4.00</code>):", reply_markup=boton_cancelar())
            return True
        st["precio"] = precio
        st["paso"] = "desc"
        bot.reply_to(message, "Paso 3/3: Envía la <b>descripción o modalidad</b> (Ej: <code>email:pass</code>, <code>link</code>):", reply_markup=boton_cancelar())
    else:
        q("INSERT INTO catalogo_db (producto, precio, descripcion) VALUES (%s, %s, %s) "
          "ON CONFLICT (producto) DO UPDATE SET precio = EXCLUDED.precio, descripcion = EXCLUDED.descripcion",
          (st["nombre"], st["precio"], texto), fetch=None)
        estado.pop(ADMIN_ID, None)
        invalidar_cache()
        bot.reply_to(message, f"✅ <b>Plataforma agregada</b>\n\n📦 <code>{esc(st['nombre'])}</code>\n💵 <code>{st['precio']:.2f} USDT</code>\n📌 <code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
    return True


def _difundir(es_foto, file_id, caption, texto, aviso_chat, aviso_id):
    """Difusión en segundo plano: no bloquea al resto de los clientes."""
    todos = [r[0] for r in q("SELECT user_id FROM usuarios")]
    enviados = 0
    for destino in todos:
        try:
            if es_foto:
                bot.send_photo(destino, file_id, caption=f"📢 <b>AVISO OFICIAL</b>\n\n{esc(caption)}", reply_markup=boton_volver_menu())
            else:
                bot.send_message(destino, f"📢 <b>AVISO OFICIAL</b>\n\n{esc(texto)}", reply_markup=boton_volver_menu())
            enviados += 1
        except Exception:
            pass
        time.sleep(0.05)
    editar_o_enviar(aviso_chat, aviso_id, f"✅ Difusión completada (<code>{enviados}/{len(todos)}</code>).", boton_volver_menu())


def flujo_noti_global(message, st):
    estado.pop(ADMIN_ID, None)
    es_foto = message.content_type == "photo"
    aviso = bot.reply_to(message, "📢 Enviando difusión en segundo plano... puedes seguir usando el bot.")
    Thread(target=_difundir, daemon=True, args=(
        es_foto, message.photo[-1].file_id if es_foto else None, message.caption or "",
        message.text or "", message.chat.id, aviso.message_id)).start()
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


def flujo_carga(message, st):
    texto = _texto_o_aviso(message, "⚠️ Envía las cuentas/links como texto.")
    if texto is None:
        return True
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    if not lineas:
        bot.reply_to(message, "❌ El bloque está vacío.", reply_markup=boton_cancelar())
        return True
    estado.pop(ADMIN_ID, None)
    producto = st["producto"]
    with db() as conn:
        with conn.cursor() as c:
            execute_values(c, "INSERT INTO stock (producto, credenciales) VALUES %s",
                           [(producto, l) for l in lineas], page_size=500)
    invalidar_cache()
    bot.reply_to(message, f"✅ <b>¡Carga exitosa!</b>\nSe agregaron <code>{len(lineas)}</code> accesos a <b>{esc(producto)}</b>.", reply_markup=boton_volver_menu())
    return True


def flujo_soporte(message, st):
    estado.pop(message.from_user.id, None)
    uid, uname, nombre, _ = obtener_datos_usuario(message.from_user.id)
    header = f"🆘 <b>REPORTE DE SOPORTE</b>\n\n👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>"
    m = InlineKeyboardMarkup()
    m.add(btn("📨 Responder", f"noti:{uid}", style=VERDE))
    if message.content_type == "photo":
        bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"{header}\n\n{esc(message.caption or '')}", reply_markup=m)
    else:
        bot.send_message(ADMIN_ID, f"{header}\n\n{esc(message.text or '')}", reply_markup=m)
    bot.reply_to(message, "✅ Mensaje enviado a soporte. Te responderemos pronto.", reply_markup=boton_volver_menu())
    return True


# ---------- COMPRA: correo + verificación del pago ----------
def flujo_compra(message, st):
    user_id = message.from_user.id
    es_texto = message.content_type == "text"
    texto = (message.text or "").strip() if es_texto else ""

    # (a) Esperando el correo para productos "a correo"
    if st["tipo"] == "correo":
        if not es_texto:
            bot.reply_to(message, "⚠️ Envía tu correo como texto.")
            return True
        if "@" not in texto or "." not in texto.split("@")[-1] or " " in texto or len(texto) > 120:
            bot.reply_to(message, "⚠️ Correo inválido. Escribe un correo correcto:")
            return True
        producto = st["producto"]
        total = a_dec(precio_en_vivo(producto))
        set_estado(user_id, "compra", tipo="pago_correo", producto=producto, cantidad=1, correo=texto,
                   total=total, creado_ms=int(time.time() * 1000))
        enviar_pago(message.chat.id, texto_instrucciones_pago(producto, 1, total, correo=texto))
        return True

    # (b) Esperando el ID de orden de Binance
    if st["tipo"] not in ("stock", "pago_correo") or "total" not in st:
        bot.reply_to(message, "💡 Primero pulsa <b>Pagar</b> en tu pedido, o usa /buy para elegir un producto.", reply_markup=boton_volver_menu())
        return True
    if not es_texto:
        bot.reply_to(message, "⚠️ Envía el <b>ID de orden</b> como texto para validar tu pago.", reply_markup=boton_volver_menu())
        return True

    order_id = "".join(texto.split())
    if not order_id.isalnum() or len(order_id) < 8:
        bot.reply_to(message, "⚠️ ID de orden no válido. Copia el <b>ID de orden</b> exacto desde tu comprobante de Binance.", reply_markup=boton_volver_menu())
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
    tipo = st["tipo"]
    correo = st.get("correo")
    total = st["total"]

    if q("SELECT 1 FROM pagos_usados WHERE order_id = %s", (order_id,), "one"):
        bot.reply_to(message, "❌ <b>Este ID de orden ya fue utilizado.</b> No se puede reutilizar un comprobante.", reply_markup=boton_volver_menu())
        return

    espera = bot.reply_to(message, "⏳ <b>Verificando tu pago en Binance...</b>")
    ok, resultado = verificar_pago_binance(order_id, total, st.get("creado_ms"))
    if not ok:
        editar_o_enviar(chat_id, espera.message_id,
                        f"❌ <b>Pago no verificado</b>\n\n{esc(resultado)}\n\n🔁 Puedes pegar el ID de orden nuevamente.",
                        boton_volver_menu())
        return

    monto_pagado = resultado
    precio_total = float(total)
    resultado_tx = None
    entregadas = []

    with db() as conn:
        with conn.cursor() as c:
            # Reserva atómica del order_id (evita doble canje)
            c.execute("INSERT INTO pagos_usados (order_id, user_id, monto) VALUES (%s, %s, %s) "
                      "ON CONFLICT (order_id) DO NOTHING", (order_id, user_id, monto_pagado))
            if c.rowcount == 0:
                resultado_tx = "usado"
            elif tipo == "pago_correo":
                c.execute("INSERT INTO historial_compras (user_id, producto, credenciales, precio) VALUES (%s, %s, %s, %s)",
                          (user_id, producto, f"Activación a correo: {correo}", precio_total))
                c.execute("UPDATE usuarios SET compras = compras + 1 WHERE user_id = %s", (user_id,))
                resultado_tx = "ok_correo"
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
            "⚠️ <b>Pago verificado, pero el stock se agotó justo ahora.</b>\n\n"
            f"Contacta al administrador (@{esc(ADMIN_USERNAME.lstrip('@'))}) con tu Order ID <code>{order_id}</code> para reposición o reembolso.",
            boton_volver_menu())
        avisar_admin(f"⚠️ <b>PAGO SIN STOCK</b>\nUsuario <code>{user_id}</code> pagó <code>{monto_pagado:.2f} USDT</code> por <b>{esc(producto)}</b> x{cantidad}, pero no hay stock.\n🧾 <code>{order_id}</code>")
        return

    estado.pop(user_id, None)
    uid, uname, nombre, _ = obtener_datos_usuario(user_id)

    if resultado_tx == "ok_correo":
        editar_o_enviar(
            chat_id, espera.message_id,
            "✅ <b>¡PAGO VERIFICADO CON ÉXITO!</b>\n\n"
            f"📦 Producto: {esc(producto)}\n"
            f"📧 Correo: <code>{esc(correo)}</code>\n"
            f"💵 Pagado: <code>{monto_pagado:.2f} USDT</code>\n"
            f"🧾 Order ID: <code>{order_id}</code>\n\n"
            "⏳ Tu activación se procesará de inmediato.",
            boton_volver_menu())
        avisar_admin(
            "🔔 <b>NUEVA VENTA VERIFICADA (CORREO)</b>\n\n"
            f"👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>\n"
            f"📦 {esc(producto)}\n📧 <code>{esc(correo)}</code>\n"
            f"💵 <code>{monto_pagado:.2f} USDT</code>\n🧾 <code>{order_id}</code>")
        return

    # ok_stock
    invalidar_cache()
    bloque = "\n".join(f"• <code>{esc(x)}</code>" for x in entregadas)
    entregado = editar_o_enviar(
        chat_id, espera.message_id,
        "✨ <b>SA SHOP · ENTREGA CONFIRMADA</b>\n\n"
        f"📦 Producto: <code>{esc(producto)}</code> (x{cantidad})\n"
        f"💵 Pagado: <code>{monto_pagado:.2f} USDT</code>\n"
        f"🧾 Order ID: <code>{order_id}</code>\n\n"
        f"🔑 <b>Tus accesos:</b>\n{bloque}\n\n"
        "¡Gracias por tu compra en SA SHOP! ⚡",
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
    "noti_global": flujo_noti_global,
    "noti_user": flujo_noti_user,
    "carga": flujo_carga,
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
