# -*- coding: utf-8 -*-
"""
⚡ SA SHOP — Tienda automática 24/7 para Telegram
💎 Verificación de pagos con Binance Pay (historial de transacciones recibidas)

Variables de entorno requeridas (Railway -> Variables):
  TELEGRAM_TOKEN, ADMIN_ID, ADMIN_USERNAME, BINANCE_PAY_ID,
  BINANCE_API_KEY, BINANCE_SECRET_KEY, DATABASE_URL
Opcionales:
  BINANCE_BASE_URL (por defecto https://api.binance.com)
  QR_URL (imagen del QR de pago)
"""

import io
import os
import time
import hmac
import html
import hashlib
import re
from decimal import Decimal, InvalidOperation
from threading import Thread
from urllib.parse import urlencode

import psycopg2
import requests
import telebot
from flask import Flask
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BotCommandScopeChat,
)

# =====================================================================
# 🔐 CONFIGURACIÓN (desde variables de entorno)
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "uservipxd")
BINANCE_PAY_ID = os.environ.get("BINANCE_PAY_ID", "884195829")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://api.binance.com")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
QR_URL = os.environ.get("QR_URL", "https://i.ibb.co/68Hn6Wf/image-2e9128.png")
TITULAR = os.environ.get("TITULAR", "SA SHOP")

# Tolerancia de monto (USDT) y ventana de búsqueda en el historial (días)
TOLERANCIA_MONTO = 0.00
DIAS_HISTORIAL = 3

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
def home():
    return "⚡ SA SHOP Bot activo 24/7"


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()


# =====================================================================
# ⚙️ BOT Y ESTADOS EN MEMORIA
# =====================================================================
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

compras_pendientes = {}
estado_carga_admin = {}
estado_carga_lote = {}
estado_precio_admin = {}
estado_mod_nombre = {}
estado_mod_desc = {}
estado_agregar_plat = {}
esperando_ticket_soporte = set()
estado_notificacion_admin = {}
esperando_noti_global = set()


def esc(x):
    """Escapa texto para HTML de Telegram."""
    return html.escape(str(x), quote=False)


# =====================================================================
# 🗄️ BASE DE DATOS
# =====================================================================
def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db()
    c = conn.cursor()
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
    c.execute("SELECT COUNT(*) FROM catalogo_db")
    if c.fetchone()[0] == 0:
        for prod, (precio, desc) in CATALOGO_INICIAL.items():
            c.execute(
                "INSERT INTO catalogo_db (producto, precio, descripcion) VALUES (%s, %s, %s) "
                "ON CONFLICT (producto) DO NOTHING",
                (prod, precio, desc),
            )
    c.execute("""CREATE TABLE IF NOT EXISTS entregas_pendientes (
        order_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, producto TEXT NOT NULL,
        credenciales TEXT NOT NULL, creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    c.close()
    conn.close()


init_db()


def obtener_catalogo():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT producto, precio, descripcion FROM catalogo_db ORDER BY producto")
    filas = c.fetchall()
    c.close()
    conn.close()
    return {p: {"precio": pr, "desc": d or ""} for p, pr, d in filas}


def obtener_precio(producto):
    cat = obtener_catalogo()
    return cat[producto]["precio"] if producto in cat else 0.0


def desc_es_correo(desc, producto=""):
    """Solo Canva usa activación a correo; los demás requieren stock."""
    return "canva" in (producto or "").casefold()


def es_producto_a_correo(producto):
    cat = obtener_catalogo()
    return producto in cat and desc_es_correo(cat[producto]["desc"], producto)


def registrar_o_actualizar_usuario(user):
    conn = get_db()
    c = conn.cursor()
    uname = user.username.lower() if user.username else ""
    nombre = user.first_name or "Usuario"
    c.execute(
        "INSERT INTO usuarios (user_id, username, nombre, compras) VALUES (%s, %s, %s, 0) "
        "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, nombre = EXCLUDED.nombre",
        (user.id, uname, nombre),
    )
    conn.commit()
    c.close()
    conn.close()


def obtener_datos_usuario(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, username, nombre, compras FROM usuarios WHERE user_id = %s", (user_id,))
    fila = c.fetchone()
    c.close()
    conn.close()
    if not fila:
        return user_id, "Sin @username", "Cliente", 0
    return fila[0], (f"@{fila[1]}" if fila[1] else "Sin @username"), (fila[2] or "Cliente"), (fila[3] or 0)


def buscar_id_por_id_o_username(identificador):
    limpio = identificador.strip().lstrip("@").lower()
    conn = get_db()
    c = conn.cursor()
    if limpio.isdigit():
        c.execute("SELECT user_id FROM usuarios WHERE user_id = %s", (int(limpio),))
    else:
        c.execute("SELECT user_id FROM usuarios WHERE LOWER(username) = %s", (limpio,))
    fila = c.fetchone()
    c.close()
    conn.close()
    if fila:
        return fila[0]
    return int(limpio) if limpio.isdigit() else None


def contar_stock(producto):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM stock WHERE producto = %s AND vendido = 0", (producto,))
    cantidad = c.fetchone()[0]
    c.close()
    conn.close()
    return cantidad


def buscar_producto_nombre(texto_busqueda):
    t = texto_busqueda.strip().lower()
    for prod in obtener_catalogo().keys():
        if prod.lower() == t or t in prod.lower():
            return prod
    return None


def limpiar_estados_usuario(user_id):
    compras_pendientes.pop(user_id, None)
    estado_carga_admin.pop(user_id, None)
    estado_carga_lote.pop(user_id, None)
    estado_precio_admin.pop(user_id, None)
    estado_mod_nombre.pop(user_id, None)
    estado_mod_desc.pop(user_id, None)
    estado_agregar_plat.pop(user_id, None)
    esperando_ticket_soporte.discard(user_id)
    estado_notificacion_admin.pop(user_id, None)
    esperando_noti_global.discard(user_id)


# =====================================================================
# 🧰 UTILIDADES DE TELEGRAM
# =====================================================================
def configurar_menu_comandos():
    try:
        publicos = [
            BotCommand("menu", "🏠 Menú Principal"),
            BotCommand("buy", "🛒 Catálogo de Compras"),
            BotCommand("stock", "📊 Consultar Stock"),
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
        ]
        bot.set_my_commands(admin, scope=BotCommandScopeChat(ADMIN_ID))
    except Exception as e:
        print(f"⚠️ No se pudo configurar el menú de comandos: {e}")


configurar_menu_comandos()


def boton_volver_menu():
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("🏠 Menú Principal", callback_data="inicio"))
    return m


def boton_cancelar():
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("❌ Cancelar", callback_data="inicio"))
    return m


def safe_edit(chat_id, msg_id, texto, markup=None):
    """Edita un mensaje; si no se puede (p. ej. era una foto), borra y reenvía."""
    try:
        bot.edit_message_text(texto, chat_id, msg_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" in str(e):
            return
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        bot.send_message(chat_id, texto, reply_markup=markup)


def enviar_archivo_txt(chat_id, contenido, nombre_archivo, caption=""):
    archivo = io.BytesIO(contenido.encode("utf-8"))
    archivo.name = nombre_archivo
    bot.send_document(chat_id, archivo, caption=caption, reply_markup=boton_volver_menu())


# =====================================================================
# 💎 VERIFICACIÓN DE PAGOS EN BINANCE
# =====================================================================
# IMPORTANTE: El endpoint "order/query" de Binance Pay solo sirve para órdenes
# creadas por un comercio (Merchant API). Un pago normal por Binance Pay /
# transferencia (el "ID de orden" que muestra la app) se consulta en el
# historial de transacciones de TU cuenta:  GET /sapi/v1/pay/transactions
# La API Key debe ser una API normal de Binance con permiso "Enable Reading".
# =====================================================================
def _binance_get(path, params):
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 30000
    query = urlencode(params)
    firma = hmac.new(
        BINANCE_SECRET_KEY.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    url = f"{BINANCE_BASE_URL}{path}?{query}&signature={firma}"
    r = requests.get(url, headers={"X-MBX-APIKEY": BINANCE_API_KEY}, timeout=20)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:300]}
    return r.status_code, data


def _extraer_monto_usdt(tx):
    """Devuelve el monto en USDT de una transacción (positivo = recibido)."""
    try:
        if str(tx.get("currency", "")).upper() == "USDT":
            return float(tx.get("amount", 0))
        for f in tx.get("fundsDetail", []) or []:
            if str(f.get("currency", "")).upper() == "USDT":
                return float(f.get("amount", 0))
    except (TypeError, ValueError):
        pass
    return None


def _coincide_id(tx, order_id):
    for key in ("transactionId", "orderId", "merchantTradeNo", "prepayId"):
        val = tx.get(key)
        if val is not None and str(val).strip() == order_id:
            return True
    return False


def verificar_pago_binance(order_id, monto_esperado, creado_ms=None):
    """Comprueba una transferencia en el historial autenticado.

    Advertencia: esto NO equivale a una orden Merchant ni garantiza
    que el remitente corresponda al cliente. Para producción comercial
    debe sustituirse por una confirmación autorizada de Binance Pay Merchant.
    """
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
                print(f"Binance status={status} code={data.get('code') if isinstance(data,dict) else 'unknown'}")
                return False, "No se pudo consultar el historial de pagos."
            lista = data.get("data") or []
            if not isinstance(lista, list):
                return False, "Respuesta inesperada de Binance."
            for tx in lista:
                if isinstance(tx, dict) and _coincide_id(tx, order_id):
                    encontrado = tx
                    break
            if encontrado or len(lista) < 100:
                break
            tiempos = [int(t.get("transactionTime", fin)) for t in lista if isinstance(t,dict)]
            if not tiempos:
                break
            fin = min(tiempos) - 1
            time.sleep(0.5)
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"Binance connection: {type(exc).__name__}")
        return False, "Error temporal consultando Binance."

    if not encontrado:
        return False, "No se encontró ese ID en el historial. Comprueba el comprobante."
    try:
        moneda = str(encontrado.get("currency", "")).upper()
        if moneda != "USDT":
            return False, "La transacción no corresponde a USDT."
        monto = Decimal(str(encontrado.get("amount", "")))
        if not monto.is_finite() or monto <= 0:
            return False, "La operación no es un pago recibido."
        if monto != esperado:
            return False, f"El importe debe ser exactamente {esperado:.2f} USDT."
        # Si Binance expone un estado, rechazar los estados no definitivos.
        estado = str(encontrado.get("status", encontrado.get("transactionStatus", ""))).upper()
        if estado and estado not in ("SUCCESS", "SUCCESSFUL", "COMPLETED", "COMPLETE", "PAID"):
            return False, "La operación aún no figura como completada."
        # Si se conoce la fecha del pedido, no admitir pagos anteriores.
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
    registrar_o_actualizar_usuario(user)
    uid, uname, nombre, _ = obtener_datos_usuario(user.id)
    rango = "👑 ADMINISTRADOR" if uid == ADMIN_ID else "👤 Cliente"
    texto = (
        "⚡ <b>SA SHOP — TIENDA AUTOMÁTICA 24/7</b> ⚡\n\n"
        f"┏ 👤 <b>Usuario:</b> {esc(nombre)} (<code>{esc(uname)}</code>)\n"
        f"┣ 🎖️ <b>Rol:</b> {rango}\n"
        f"┗ 🆔 <b>ID:</b> <code>{uid}</code>\n\n"
        "👇 Selecciona una opción:"
    )
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        InlineKeyboardButton("🛒 Ver Catálogo y Comprar", callback_data="catalogo"),
        InlineKeyboardButton("👤 Mi Perfil e Historial", callback_data="perfil"),
        InlineKeyboardButton("🛠️ Soporte y Ayuda", callback_data="soporte"),
    )
    if uid == ADMIN_ID:
        m.add(InlineKeyboardButton("👑 Panel de Administración", callback_data="panel_admin"))
    return texto, m


def vista_catalogo():
    m = InlineKeyboardMarkup(row_width=1)
    for prod, info in obtener_catalogo().items():
        precio, desc = info["precio"], info["desc"]
        if desc_es_correo(desc, prod):
            txt = f"🟢 {prod} — {precio:.2f} USDT | ✉️ {desc}"
        else:
            disp = contar_stock(prod)
            txt = (f"🟢 {prod} — {precio:.2f} USDT ({disp} disp.)" if disp > 0
                   else f"🔴 {prod} — {precio:.2f} USDT (Agotado)")
        m.add(InlineKeyboardButton(txt, callback_data=f"sel_{prod}"))
    m.add(InlineKeyboardButton("🏠 Menú Principal", callback_data="inicio"))
    texto = ("🛒 <b>CATÁLOGO DE PRODUCTOS</b>\n\n"
             "🟢 Disponible  |  🔴 Agotado\n"
             "👇 Elige el producto que deseas adquirir:")
    return texto, m


def menu_cantidad(producto, cantidad, precio_unit, stock_disp):
    total = cantidad * precio_unit
    texto = (
        "📦 <b>SELECCIONA LA CANTIDAD</b>\n\n"
        f"• Producto: <code>{esc(producto)}</code>\n"
        f"• Stock disponible: <code>{stock_disp}</code>\n"
        f"• Precio unitario: <code>{precio_unit:.2f} USDT</code>\n\n"
        f"🔢 Cantidad: <b>{cantidad}</b>\n"
        f"💵 <b>Total a pagar:</b> <code>{total:.2f} USDT</code>"
    )
    m = InlineKeyboardMarkup(row_width=3)
    m.add(
        InlineKeyboardButton("➖ Quitar", callback_data=f"cant_menos_{producto}"),
        InlineKeyboardButton(f"🔢 {cantidad}", callback_data="nada"),
        InlineKeyboardButton("➕ Agregar", callback_data=f"cant_mas_{producto}"),
    )
    m.add(InlineKeyboardButton(f"💳 Pagar {total:.2f} USDT con Binance Pay", callback_data=f"cant_ok_{producto}"))
    m.add(InlineKeyboardButton("⬅️ Volver al Catálogo", callback_data="catalogo"))
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
        f"• <b>TITULAR:</b> {TITULAR}\n\n"
        "📸 Escanea el QR o transfiere al ID indicado.\n\n"
        "👇 Luego pega aquí el <b>ID de orden</b> de tu pago para validarlo y recibir tu entrega automática."
    )


def enviar_pago(chat_id, texto):
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("❌ Cancelar", callback_data="catalogo"))
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
    "• /mis_entregas ➔ Recuperar entrega pendiente"
)


# =====================================================================
# 💬 COMANDOS PÚBLICOS
# =====================================================================
@bot.message_handler(commands=["start", "menu", "menú", "inicio"])
def cmd_start(message):
    limpiar_estados_usuario(message.from_user.id)
    texto, m = vista_menu_principal(message.from_user)
    bot.send_message(message.chat.id, texto, reply_markup=m)


@bot.message_handler(commands=["buy", "catalogo", "catálogo"])
def cmd_buy(message):
    registrar_o_actualizar_usuario(message.from_user)
    limpiar_estados_usuario(message.from_user.id)
    texto, m = vista_catalogo()
    bot.send_message(message.chat.id, texto, reply_markup=m)


@bot.message_handler(commands=["stock"])
def cmd_stock(message):
    registrar_o_actualizar_usuario(message.from_user)
    partes = message.text.split(maxsplit=1)
    es_admin = message.from_user.id == ADMIN_ID
    catalogo = obtener_catalogo()

    if len(partes) == 1:
        lineas = ["📊 <b>ESTADO DEL INVENTARIO</b>\n"]
        for prod, info in catalogo.items():
            if desc_es_correo(info["desc"], prod):
                lineas.append(f"🟢 <b>{esc(prod)}</b> ➔ <code>{info['precio']:.2f} USDT</code> | ✉️ {esc(info['desc'])}")
            else:
                cant = contar_stock(prod)
                color = "🟢" if cant > 0 else "🔴"
                lineas.append(f"{color} <b>{esc(prod)}</b> ➔ <code>{cant}</code> disp. (<code>{info['precio']:.2f} USDT</code>) | 📌 {esc(info['desc'])}")
        bot.reply_to(message, "\n".join(lineas), reply_markup=boton_volver_menu())

        if es_admin:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT id, producto, credenciales FROM stock WHERE vendido = 0 ORDER BY producto, id")
            filas = c.fetchall()
            c.close()
            conn.close()
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
    if desc_es_correo(info["desc"], prod):
        bot.reply_to(message, f"📦 <b>{esc(prod)}</b>\n💵 Precio: <code>{info['precio']:.2f} USDT</code>\n📌 {esc(info['desc'])}", reply_markup=boton_volver_menu())
        return

    cant = contar_stock(prod)
    color = "🟢" if cant > 0 else "🔴"
    bot.reply_to(
        message,
        f"📦 <b>Inventario:</b> {esc(prod)}\n💵 <b>Precio:</b> <code>{info['precio']:.2f} USDT</code>\n"
        f"📌 <b>Tipo:</b> {esc(info['desc'])}\n{color} <b>Disponibles:</b> <code>{cant}</code>",
        reply_markup=boton_volver_menu(),
    )
    if es_admin and cant > 0:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, credenciales FROM stock WHERE producto = %s AND vendido = 0 ORDER BY id", (prod,))
        filas = c.fetchall()
        c.close()
        conn.close()
        contenido = f"=== STOCK DE {prod.upper()} ({cant}) ===\n\n" + "\n".join(f"ID: {f[0]} | {f[1]}" for f in filas)
        enviar_archivo_txt(message.chat.id, contenido, f"stock_{prod.lower().replace(' ', '_')}.txt", f"🔐 <b>Credenciales de {esc(prod)}</b>")


# =====================================================================
# 👑 COMANDOS ADMIN
# =====================================================================
def solo_admin(message):
    return message.from_user.id == ADMIN_ID


@bot.message_handler(commands=["cmds", "admin", "ayuda"])
def cmd_cmds(message):
    if not solo_admin(message):
        return
    bot.reply_to(message, TEXTO_CMDS_ADMIN, reply_markup=boton_volver_menu())

@bot.message_handler(commands=["testip"])
def cmd_testip(message):
    if not solo_admin(message):
        return

    try:
        respuesta = requests.get(
            "https://api.ipify.org?format=json",
            timeout=10
        )
        respuesta.raise_for_status()
        ip = respuesta.json()["ip"]

        bot.reply_to(
            message,
            f"🌐 IP pública de Railway:\n<code>{esc(ip)}</code>"
        )

    except Exception as e:
        bot.reply_to(
            message,
            f"❌ Error: {esc(str(e))}"
        )

@bot.message_handler(commands=["testbinance"])
def cmd_testbinance(message):
    if not solo_admin(message):
        return

    msg = bot.reply_to(
        message,
        "🧪 Probando conexión con Binance..."
    )

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
    bot.edit_message_text(texto, message.chat.id, msg.message_id, reply_markup=boton_volver_menu())


@bot.message_handler(commands=["agregar_plataforma"])
def cmd_agregar_plataforma(message):
    if not solo_admin(message):
        return
    limpiar_estados_usuario(ADMIN_ID)
    estado_agregar_plat[ADMIN_ID] = {"paso": "nombre"}
    bot.reply_to(message, "➕ <b>CREAR NUEVA PLATAFORMA</b>\n\nPaso 1/3: Envía el <b>nombre</b> (Ej: <code>Paramount+</code>):", reply_markup=boton_cancelar())


def _menu_catalogo_admin(message, prefijo, icono, titulo, con_precio=False, con_desc=False):
    limpiar_estados_usuario(ADMIN_ID)
    m = InlineKeyboardMarkup(row_width=1)
    for prod, info in obtener_catalogo().items():
        extra = f" ({info['precio']:.2f} USDT)" if con_precio else (f" ({info['desc']})" if con_desc else "")
        m.add(InlineKeyboardButton(f"{icono} {prod}{extra}", callback_data=f"{prefijo}_{prod}"))
    m.add(InlineKeyboardButton("🏠 Menú Principal", callback_data="inicio"))
    bot.reply_to(message, titulo, reply_markup=m)


@bot.message_handler(commands=["quitar_plataforma"])
def cmd_quitar(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "delplat", "🗑️", "🗑️ <b>ELIMINAR PLATAFORMA</b>\n\nSelecciona el producto a retirar:")


@bot.message_handler(commands=["modificar_nombre"])
def cmd_mod_nombre(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "modnom", "✏️", "✏️ <b>MODIFICAR NOMBRE</b>\n\nSelecciona el producto:")


@bot.message_handler(commands=["modificar_precio"])
def cmd_mod_precio(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "prec", "💵", "💵 <b>MODIFICAR PRECIO</b>\n\nSelecciona el producto:", con_precio=True)


@bot.message_handler(commands=["modificar_descripcion"])
def cmd_mod_desc(message):
    if solo_admin(message):
        _menu_catalogo_admin(message, "moddesc", "📌", "📌 <b>MODIFICAR DESCRIPCIÓN</b>\n\nSelecciona el producto:", con_desc=True)


def _menu_carga(message, prefijo, titulo):
    limpiar_estados_usuario(ADMIN_ID)
    m = InlineKeyboardMarkup(row_width=2)
    botones = []
    for prod, info in obtener_catalogo().items():
        if desc_es_correo(info["desc"], prod):
            continue
        cant = contar_stock(prod)
        botones.append(InlineKeyboardButton(f"{'🟢' if cant > 0 else '🔴'} {prod} ({cant})", callback_data=f"{prefijo}_{prod}"))
    if botones:
        m.add(*botones)
    m.add(InlineKeyboardButton("🏠 Menú Principal", callback_data="inicio"))
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
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, producto, credenciales, precio, fecha FROM historial_compras WHERE user_id = %s ORDER BY id DESC", (uid,))
    compras = c.fetchall()
    c.close()
    conn.close()
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
    limpiar_estados_usuario(ADMIN_ID)
    esperando_noti_global.add(ADMIN_ID)
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
    estado_notificacion_admin[ADMIN_ID] = target
    bot.reply_to(message, f"📨 Escribe el mensaje o imagen para <code>{target}</code>:", reply_markup=boton_cancelar())


@bot.message_handler(commands=["borrar"])
def cmd_borrar(message):
    if not solo_admin(message):
        return
    partes = message.text.split()
    if len(partes) != 2 or not partes[1].isdigit():
        bot.reply_to(message, "⚠️ Uso: <code>/borrar [ID_DE_CUENTA]</code>")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM stock WHERE id = %s AND vendido = 0", (int(partes[1]),))
    n = c.rowcount
    conn.commit()
    c.close()
    conn.close()
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
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM stock WHERE producto = %s AND vendido = 0", (prod,))
    n = c.rowcount
    conn.commit()
    c.close()
    conn.close()
    bot.reply_to(message, f"🗑️ Se eliminaron <code>{n}</code> cuentas no vendidas de <b>{esc(prod)}</b>.", reply_markup=boton_volver_menu())


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not solo_admin(message):
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM usuarios")
    usuarios = c.fetchone()[0]
    c.execute("SELECT COUNT(*), COALESCE(SUM(precio), 0) FROM historial_compras")
    ventas, ingresos = c.fetchone()
    c.execute("SELECT COUNT(*), COALESCE(SUM(monto), 0) FROM pagos_usados")
    n_pagos, total_pagos = c.fetchone()
    c.execute("SELECT COUNT(*) FROM stock WHERE vendido = 0")
    stock = c.fetchone()[0]
    c.close()
    conn.close()
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
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, username, nombre, compras, fecha_registro FROM usuarios ORDER BY fecha_registro DESC")
    filas = c.fetchall()
    c.close()
    conn.close()
    lineas = ["=== BASE DE DATOS DE CLIENTES ===", ""]
    for uid, uname, nom, comp, fec in filas:
        lineas.append(f"ID: {uid} | @{uname or 'Sin_user'} | {nom} | Compras: {comp} | Registro: {fec}")
    enviar_archivo_txt(message.chat.id, "\n".join(lineas), "clientes_tienda.txt", f"👥 <b>{len(filas)}</b> clientes registrados")


# =====================================================================
# 🔘 CALLBACKS
# =====================================================================
@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    user = call.from_user
    user_id = user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data
    registrar_o_actualizar_usuario(user)

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    es_admin = user_id == ADMIN_ID

    if data == "nada":
        return

    if data == "inicio":
        limpiar_estados_usuario(user_id)
        texto, m = vista_menu_principal(user)
        safe_edit(chat_id, msg_id, texto, m)

    elif data == "panel_admin":
        if es_admin:
            safe_edit(chat_id, msg_id, TEXTO_CMDS_ADMIN, boton_volver_menu())

    elif data == "catalogo":
        limpiar_estados_usuario(user_id)
        texto, m = vista_catalogo()
        safe_edit(chat_id, msg_id, texto, m)

    elif data.startswith("delplat_"):
        if not es_admin:
            return
        producto = data.split("_", 1)[1]
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM catalogo_db WHERE producto = %s", (producto,))
        conn.commit()
        c.close()
        conn.close()
        texto, m = vista_catalogo()
        safe_edit(chat_id, msg_id, f"🗑️ Plataforma <code>{esc(producto)}</code> eliminada.\n\n" + texto, m)

    elif data.startswith("modnom_"):
        if not es_admin:
            return
        producto = data.split("_", 1)[1]
        estado_mod_nombre[user_id] = producto
        safe_edit(chat_id, msg_id, f"✏️ <b>CAMBIAR NOMBRE DE:</b> <code>{esc(producto)}</code>\n\n👉 Escribe el nuevo nombre:", boton_cancelar())

    elif data.startswith("moddesc_"):
        if not es_admin:
            return
        producto = data.split("_", 1)[1]
        estado_mod_desc[user_id] = producto
        safe_edit(chat_id, msg_id, f"📌 <b>CAMBIAR DESCRIPCIÓN DE:</b> <code>{esc(producto)}</code>\n\n👉 Escribe la nueva descripción (Ej: <code>email:pass</code>, <code>link</code>, <code>activación a correo</code>):", boton_cancelar())

    elif data.startswith("prec_"):
        if not es_admin:
            return
        producto = data.split("_", 1)[1]
        estado_precio_admin[user_id] = producto
        safe_edit(chat_id, msg_id, f"💵 <b>CAMBIAR PRECIO DE:</b> <code>{esc(producto)}</code>\n\nActual: <code>{obtener_precio(producto):.2f} USDT</code>\n\n👉 Escribe el nuevo precio (Ej: <code>3.50</code>):", boton_cancelar())

    elif data.startswith("cargar_"):
        if not es_admin:
            return
        producto = data.split("_", 1)[1]
        estado_carga_admin[user_id] = producto
        safe_edit(chat_id, msg_id, f"📥 <b>Cargando stock para:</b> <code>{esc(producto)}</code>\n\nEnvía las cuentas o links, una por línea:", boton_cancelar())

    elif data.startswith("lotecar_"):
        if not es_admin:
            return
        producto = data.split("_", 1)[1]
        estado_carga_lote[user_id] = producto
        safe_edit(chat_id, msg_id, f"📦 <b>Carga masiva para:</b> <code>{esc(producto)}</code>\n\nPega tu bloque completo, una cuenta/link por línea:", boton_cancelar())

    elif data.startswith("noti_"):
        if not es_admin:
            return
        target = int(data.split("_", 1)[1])
        estado_notificacion_admin[ADMIN_ID] = target
        bot.send_message(chat_id, f"📨 Escribe tu respuesta para <code>{target}</code>:", reply_markup=boton_cancelar())

    elif data.startswith("sel_"):
        producto = data.split("_", 1)[1]
        cat = obtener_catalogo()
        if producto not in cat:
            bot.answer_callback_query(call.id, "Producto no disponible.", show_alert=True)
            return
        precio = cat[producto]["precio"]

        if desc_es_correo(cat[producto]["desc"], producto):
            compras_pendientes[user_id] = {"producto": producto, "tipo": "correo"}
            m = InlineKeyboardMarkup()
            m.add(InlineKeyboardButton("❌ Cancelar", callback_data="catalogo"))
            safe_edit(chat_id, msg_id,
                      f"📧 <b>{esc(producto.upper())}</b> (activación a correo)\n\n"
                      f"💵 Precio: <code>{precio:.2f} USDT</code>\n\n"
                      "👉 Envía ahora tu <b>correo electrónico</b> para continuar:", m)
            return

        stock = contar_stock(producto)
        if stock <= 0:
            bot.answer_callback_query(call.id, f"🔴 {producto} está agotado.", show_alert=True)
            return
        compras_pendientes[user_id] = {"producto": producto, "cantidad": 1, "precio_unit": precio, "tipo": "stock", "creado_ms": int(time.time() * 1000)}
        texto, m = menu_cantidad(producto, 1, precio, stock)
        safe_edit(chat_id, msg_id, texto, m)

    elif data.startswith("cant_"):
        _, accion, producto = data.split("_", 2)
        pend = compras_pendientes.get(user_id)
        if not pend or pend.get("producto") != producto:
            bot.answer_callback_query(call.id, "La sesión expiró. Vuelve al catálogo.", show_alert=True)
            return

        stock = contar_stock(producto)
        cantidad = pend.get("cantidad", 1)
        precio_unit = obtener_precio(producto)
        pend["precio_unit"] = precio_unit

        if accion == "mas" and cantidad < stock:
            cantidad += 1
        elif accion == "menos" and cantidad > 1:
            cantidad -= 1
        elif accion == "ok":
            if stock < cantidad:
                bot.answer_callback_query(call.id, "El stock cambió. Ajusta la cantidad.", show_alert=True)
                return
            total = cantidad * precio_unit
            enviar_pago(chat_id, texto_instrucciones_pago(producto, cantidad, total))
            return

        pend["cantidad"] = cantidad
        texto, m = menu_cantidad(producto, cantidad, precio_unit, stock)
        safe_edit(chat_id, msg_id, texto, m)

    elif data == "perfil":
        uid, uname, nombre, compras = obtener_datos_usuario(user_id)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT producto, credenciales FROM historial_compras WHERE user_id = %s ORDER BY id DESC LIMIT 5", (user_id,))
        ultimas = c.fetchall()
        c.close()
        conn.close()
        hist = ""
        if ultimas:
            hist = "\n\n📋 <b>Tus últimas compras:</b>\n" + "\n".join(f"• <b>{esc(p)}</b>: <code>{esc(cr)}</code>" for p, cr in ultimas)
        texto = (
            "👤 <b>TU CUENTA</b>\n\n"
            f"🏷️ Nombre: {esc(nombre)}\n"
            f"🔗 Username: <code>{esc(uname)}</code>\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"🛍️ Compras totales: <code>{compras}</code>{hist}"
        )
        safe_edit(chat_id, msg_id, texto, boton_volver_menu())

    elif data == "soporte":
        limpiar_estados_usuario(user_id)
        esperando_ticket_soporte.add(user_id)
        m = InlineKeyboardMarkup()
        m.add(
            InlineKeyboardButton("💬 Soporte Directo", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}"),
            InlineKeyboardButton("🏠 Menú Principal", callback_data="inicio"),
        )
        safe_edit(chat_id, msg_id, "🛠️ <b>CENTRO DE SOPORTE</b>\n\nEscribe tu consulta o reporte aquí en el chat y el administrador te atenderá:", m)


# =====================================================================
# ⌨️ ENTRADAS DE TEXTO / FOTO
# =====================================================================
@bot.message_handler(commands=["mis_entregas"])
def cmd_mis_entregas(message):
    """Reenvía entregas que quedaron pendientes tras un error de Telegram."""
    uid = message.from_user.id
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as c:
                c.execute("SELECT order_id, producto, credenciales FROM entregas_pendientes WHERE user_id = %s ORDER BY creado LIMIT 10", (uid,))
                pendientes = c.fetchall()
        if not pendientes:
            bot.reply_to(message, "📦 No tienes entregas pendientes. Consulta /menu para ver tu historial.")
            return
        for oid, prod, cred in pendientes:
            bot.send_message(message.chat.id, f"📦 <b>Entrega recuperada</b>\nProducto: {esc(prod)}\nAccesos: <code>{esc(cred)}</code>")
            with conn:
                with conn.cursor() as c:
                    c.execute("DELETE FROM entregas_pendientes WHERE order_id=%s AND user_id=%s", (oid,uid))
    finally:
        conn.close()


@bot.message_handler(content_types=["text", "photo"])
def manejar_entradas(message):
    user = message.from_user
    user_id = user.id
    registrar_o_actualizar_usuario(user)
    texto = (message.text or "").strip()
    es_texto = message.content_type == "text"
    es_admin = user_id == ADMIN_ID

    # ---------- ADMIN: flujos ----------
    if es_admin and user_id in estado_mod_nombre and es_texto:
        antiguo = estado_mod_nombre.pop(user_id)
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE catalogo_db SET producto = %s WHERE producto = %s", (texto, antiguo))
            c.execute("UPDATE stock SET producto = %s WHERE producto = %s", (texto, antiguo))
            conn.commit()
            c.close()
            conn.close()
            bot.reply_to(message, f"✅ <b>Nombre actualizado</b>\n\n<code>{esc(antiguo)}</code> ➔ <code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
        except Exception as e:
            bot.reply_to(message, f"❌ No se pudo renombrar: <code>{esc(e)}</code>", reply_markup=boton_volver_menu())
        return

    if es_admin and user_id in estado_mod_desc and es_texto:
        producto = estado_mod_desc.pop(user_id)
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE catalogo_db SET descripcion = %s WHERE producto = %s", (texto, producto))
        conn.commit()
        c.close()
        conn.close()
        bot.reply_to(message, f"✅ <b>Descripción actualizada</b>\n\n📦 <code>{esc(producto)}</code>\n📌 <code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
        return

    if es_admin and user_id in estado_agregar_plat and es_texto:
        est = estado_agregar_plat[user_id]
        if est["paso"] == "nombre":
            est["nombre"] = texto
            est["paso"] = "precio"
            bot.reply_to(message, f"➕ Producto: <code>{esc(texto)}</code>\n\nPaso 2/3: Envía el <b>precio</b> en USDT (Ej: <code>3.50</code>):", reply_markup=boton_cancelar())
        elif est["paso"] == "precio":
            try:
                precio = float(texto.replace(",", "."))
                if precio < 0:
                    raise ValueError()
                est["precio"] = precio
                est["paso"] = "desc"
                bot.reply_to(message, "Paso 3/3: Envía la <b>descripción o modalidad</b> (Ej: <code>email:pass</code>, <code>link</code> o <code>activación a correo</code>):", reply_markup=boton_cancelar())
            except ValueError:
                bot.reply_to(message, "❌ Precio inválido. Envía un número (Ej: <code>4.00</code>):", reply_markup=boton_cancelar())
        elif est["paso"] == "desc":
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "INSERT INTO catalogo_db (producto, precio, descripcion) VALUES (%s, %s, %s) "
                "ON CONFLICT (producto) DO UPDATE SET precio = EXCLUDED.precio, descripcion = EXCLUDED.descripcion",
                (est["nombre"], est["precio"], texto),
            )
            conn.commit()
            c.close()
            conn.close()
            estado_agregar_plat.pop(user_id, None)
            bot.reply_to(message, f"✅ <b>Plataforma agregada</b>\n\n📦 <code>{esc(est['nombre'])}</code>\n💵 <code>{est['precio']:.2f} USDT</code>\n📌 <code>{esc(texto)}</code>", reply_markup=boton_volver_menu())
        return

    if es_admin and user_id in estado_precio_admin and es_texto:
        producto = estado_precio_admin.pop(user_id)
        try:
            nuevo = float(texto.replace(",", "."))
            if nuevo < 0:
                raise ValueError()
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE catalogo_db SET precio = %s WHERE producto = %s", (nuevo, producto))
            conn.commit()
            c.close()
            conn.close()
            bot.reply_to(message, f"✅ <b>Precio actualizado</b>\n\n📦 <code>{esc(producto)}</code>\n💵 <code>{nuevo:.2f} USDT</code>", reply_markup=boton_volver_menu())
        except ValueError:
            bot.reply_to(message, "❌ Precio inválido. Usa /modificar_precio e inténtalo de nuevo.", reply_markup=boton_volver_menu())
        return

    if es_admin and ADMIN_ID in esperando_noti_global:
        esperando_noti_global.discard(ADMIN_ID)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM usuarios")
        todos = [r[0] for r in c.fetchall()]
        c.close()
        conn.close()
        espera = bot.reply_to(message, f"📢 Enviando difusión a <code>{len(todos)}</code> usuarios...")
        enviados = 0
        for uid in todos:
            try:
                if message.content_type == "photo":
                    bot.send_photo(uid, message.photo[-1].file_id, caption=f"📢 <b>AVISO OFICIAL</b>\n\n{esc(message.caption or '')}", reply_markup=boton_volver_menu())
                else:
                    bot.send_message(uid, f"📢 <b>AVISO OFICIAL</b>\n\n{esc(texto)}", reply_markup=boton_volver_menu())
                enviados += 1
            except Exception:
                pass
            time.sleep(0.05)
        bot.edit_message_text(f"✅ Difusión completada (<code>{enviados}/{len(todos)}</code>).", message.chat.id, espera.message_id, reply_markup=boton_volver_menu())
        return

    if es_admin and ADMIN_ID in estado_notificacion_admin:
        target = estado_notificacion_admin.pop(ADMIN_ID)
        try:
            if message.content_type == "photo":
                bot.send_photo(target, message.photo[-1].file_id, caption=f"🔔 <b>MENSAJE DEL ADMIN</b>\n\n{esc(message.caption or '')}", reply_markup=boton_volver_menu())
            else:
                bot.send_message(target, f"🔔 <b>MENSAJE DEL ADMIN</b>\n\n{esc(texto)}", reply_markup=boton_volver_menu())
            bot.reply_to(message, f"✅ Mensaje enviado a <code>{target}</code>.", reply_markup=boton_volver_menu())
        except Exception as e:
            bot.reply_to(message, f"❌ Error: <code>{esc(e)}</code>", reply_markup=boton_volver_menu())
        return

    if es_admin and (user_id in estado_carga_admin or user_id in estado_carga_lote):
        producto = estado_carga_admin.pop(user_id, None) or estado_carga_lote.pop(user_id, None)
        if not es_texto:
            bot.reply_to(message, "⚠️ Envía las cuentas/links como texto.", reply_markup=boton_volver_menu())
            return
        lineas = [l.strip() for l in texto.split("\n") if l.strip()]
        if not lineas:
            bot.reply_to(message, "❌ El bloque está vacío.", reply_markup=boton_volver_menu())
            return
        conn = get_db()
        c = conn.cursor()
        c.executemany("INSERT INTO stock (producto, credenciales) VALUES (%s, %s)", [(producto, l) for l in lineas])
        conn.commit()
        c.close()
        conn.close()
        bot.reply_to(message, f"✅ <b>¡Carga exitosa!</b>\nSe agregaron <code>{len(lineas)}</code> accesos a <b>{esc(producto)}</b>.", reply_markup=boton_volver_menu())
        return

    # ---------- CLIENTE: soporte ----------
    if user_id in esperando_ticket_soporte:
        esperando_ticket_soporte.discard(user_id)
        uid, uname, nombre, _ = obtener_datos_usuario(user_id)
        header = f"🆘 <b>REPORTE DE SOPORTE</b>\n\n👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>"
        m = InlineKeyboardMarkup()
        m.add(InlineKeyboardButton("📨 Responder", callback_data=f"noti_{uid}"))
        if message.content_type == "photo":
            bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"{header}\n\n{esc(message.caption or '')}", reply_markup=m)
        else:
            bot.send_message(ADMIN_ID, f"{header}\n\n{esc(texto)}", reply_markup=m)
        bot.reply_to(message, "✅ Mensaje enviado a soporte. Te responderemos pronto.", reply_markup=boton_volver_menu())
        return

    # ---------- CLIENTE: correo para productos a correo ----------
    pend = compras_pendientes.get(user_id)
    if pend and pend.get("tipo") == "correo":
        if not es_texto:
            bot.reply_to(message, "⚠️ Envía tu correo como texto.")
            return
        if "@" not in texto or "." not in texto.split("@")[-1] or " " in texto:
            bot.reply_to(message, "⚠️ Correo inválido. Escribe un correo correcto:")
            return
        producto = pend["producto"]
        precio = obtener_precio(producto)
        compras_pendientes[user_id] = {"producto": producto, "cantidad": 1, "precio_unit": precio, "correo": texto, "tipo": "pago_correo", "creado_ms": int(time.time() * 1000)}
        enviar_pago(message.chat.id, texto_instrucciones_pago(producto, 1, precio, correo=texto))
        return

    # ---------- CLIENTE: verificación del pago ----------
    if pend and pend.get("tipo") in ("stock", "pago_correo"):
        if not es_texto:
            bot.reply_to(message, "⚠️ Envía el <b>ID de orden</b> como texto para validar tu pago.", reply_markup=boton_volver_menu())
            return

        order_id = "".join(texto.split())  # quita espacios/saltos
        if not order_id.isalnum() or len(order_id) < 8:
            bot.reply_to(message, "⚠️ ID de orden no válido. Copia el <b>ID de orden</b> exacto desde tu comprobante de Binance.", reply_markup=boton_volver_menu())
            return

        producto = pend["producto"]
        cantidad = pend.get("cantidad", 1)
        total_esperado = cantidad * obtener_precio(producto)
        tipo = pend["tipo"]
        correo = pend.get("correo")

        # ¿Ya fue usado?
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT 1 FROM pagos_usados WHERE order_id = %s", (order_id,))
        if c.fetchone():
            c.close()
            conn.close()
            bot.reply_to(message, "❌ <b>Este ID de orden ya fue utilizado.</b> No se puede reutilizar un comprobante.", reply_markup=boton_volver_menu())
            return
        c.close()
        conn.close()

        espera = bot.reply_to(message, "⏳ <b>Verificando tu pago en Binance...</b>")
        ok, resultado = verificar_pago_binance(order_id, total_esperado, pend.get("creado_ms"))

        if not ok:
            bot.edit_message_text(f"❌ <b>Pago no verificado</b>\n\n{esc(resultado)}", message.chat.id, espera.message_id, reply_markup=boton_volver_menu())
            return

        monto_pagado = resultado
        conn = get_db()
        c = conn.cursor()
        try:
            # Reservar el order_id de forma atómica (evita doble canje)
            c.execute("INSERT INTO pagos_usados (order_id, user_id, monto) VALUES (%s, %s, %s) ON CONFLICT (order_id) DO NOTHING",
                      (order_id, user_id, monto_pagado))
            if c.rowcount == 0:
                conn.rollback()
                bot.edit_message_text("❌ <b>Este ID de orden ya fue utilizado.</b>", message.chat.id, espera.message_id, reply_markup=boton_volver_menu())
                return

            # ----- Producto a correo -----
            if tipo == "pago_correo":
                c.execute("INSERT INTO historial_compras (user_id, producto, credenciales, precio) VALUES (%s, %s, %s, %s)",
                          (user_id, producto, f"Activación a correo: {correo}", total_esperado))
                c.execute("UPDATE usuarios SET compras = compras + 1 WHERE user_id = %s", (user_id,))
                conn.commit()
                compras_pendientes.pop(user_id, None)

                bot.edit_message_text(
                    "✅ <b>¡PAGO VERIFICADO CON ÉXITO!</b>\n\n"
                    f"📦 Producto: {esc(producto)}\n"
                    f"📧 Correo: <code>{esc(correo)}</code>\n"
                    f"💵 Pagado: <code>{monto_pagado:.2f} USDT</code>\n"
                    f"🧾 Order ID: <code>{order_id}</code>\n\n"
                    "⏳ Tu activación se procesará de inmediato.",
                    message.chat.id, espera.message_id, reply_markup=boton_volver_menu(),
                )
                uid, uname, nombre, _ = obtener_datos_usuario(user_id)
                bot.send_message(
                    ADMIN_ID,
                    "🔔 <b>NUEVA VENTA VERIFICADA (CORREO)</b>\n\n"
                    f"👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n🆔 <code>{uid}</code>\n"
                    f"📦 {esc(producto)}\n📧 <code>{esc(correo)}</code>\n"
                    f"💵 <code>{monto_pagado:.2f} USDT</code>\n🧾 <code>{order_id}</code>",
                )
                return

            # ----- Producto de stock -----
            c.execute("SELECT id, credenciales FROM stock WHERE producto = %s AND vendido = 0 ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
                      (producto, cantidad))
            filas = c.fetchall()
            if len(filas) < cantidad:
                conn.rollback()  # el pago NO queda consumido
                bot.edit_message_text(
                    "⚠️ <b>Pago verificado, pero el stock se agotó justo ahora.</b>\n\n"
                    f"Contacta al administrador (@{esc(ADMIN_USERNAME.lstrip('@'))}) con tu Order ID <code>{order_id}</code> para reposición o reembolso.",
                    message.chat.id, espera.message_id, reply_markup=boton_volver_menu(),
                )
                bot.send_message(ADMIN_ID, f"⚠️ <b>PAGO SIN STOCK</b>\nUsuario <code>{user_id}</code> pagó <code>{monto_pagado:.2f} USDT</code> por <b>{esc(producto)}</b> x{cantidad}, pero no hay stock.\n🧾 <code>{order_id}</code>")
                return

            entregadas = []
            for sid, cred in filas:
                c.execute("UPDATE stock SET vendido = 1 WHERE id = %s", (sid,))
                entregadas.append(cred)
            c.execute("UPDATE usuarios SET compras = compras + 1 WHERE user_id = %s", (user_id,))
            c.execute("INSERT INTO historial_compras (user_id, producto, credenciales, precio) VALUES (%s, %s, %s, %s)",
                      (user_id, producto, " | ".join(entregadas), total_esperado))
            c.execute("INSERT INTO entregas_pendientes (order_id, user_id, producto, credenciales) VALUES (%s, %s, %s, %s) ON CONFLICT (order_id) DO NOTHING", (order_id, user_id, producto, " | ".join(entregadas)))
            conn.commit()
            compras_pendientes.pop(user_id, None)

            bloque = "\n".join(f"• <code>{esc(x)}</code>" for x in entregadas)
            bot.edit_message_text(
                "✨ <b>SA SHOP · ENTREGA CONFIRMADA</b>\n\n"
                f"📦 Producto: <code>{esc(producto)}</code> (x{cantidad})\n"
                f"💵 Pagado: <code>{monto_pagado:.2f} USDT</code>\n"
                f"🧾 Order ID: <code>{order_id}</code>\n\n"
                f"🔑 <b>Tus accesos:</b>\n{bloque}\n\n"
                "¡Gracias por tu compra en SA SHOP! ⚡",
                message.chat.id, espera.message_id, reply_markup=boton_volver_menu(),
            )
            c2 = get_db()
            try:
                with c2:
                    with c2.cursor() as cur2:
                        cur2.execute("DELETE FROM entregas_pendientes WHERE order_id = %s", (order_id,))
            finally:
                c2.close()
            uid, uname, nombre, _ = obtener_datos_usuario(user_id)
            bot.send_message(
                ADMIN_ID,
                "💰 <b>VENTA AUTOMÁTICA VERIFICADA</b>\n\n"
                f"👤 {esc(nombre)} (<code>{esc(uname)}</code>)\n"
                f"📦 {esc(producto)} (x{cantidad})\n"
                f"💵 <code>{monto_pagado:.2f} USDT</code>\n🧾 <code>{order_id}</code>",
            )
        except Exception as e:
            conn.rollback()
            print(f"❌ Error procesando compra: {e}")
            bot.edit_message_text("❌ Ocurrió un error procesando tu compra. No se consumió tu pago; inténtalo de nuevo o contacta soporte.", message.chat.id, espera.message_id, reply_markup=boton_volver_menu())
        finally:
            c.close()
            conn.close()
        return

    # ---------- Mensajes sueltos ----------
    if es_texto and texto.isdigit() and len(texto) >= 15:
        bot.reply_to(message, "⚠️ No tienes ninguna compra en proceso. Elige un producto en el catálogo primero.", reply_markup=boton_volver_menu())
        return

    bot.reply_to(message, "💡 Usa /menu para ir al inicio.", reply_markup=boton_volver_menu())



# =====================================================================
# 🚀 ARRANQUE
# =====================================================================
if __name__ == "__main__":
    keep_alive()
    print("🚀 SA SHOP Bot iniciado — verificación de pagos vía historial de Binance Pay")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"⚠️ Error en polling: {e}")
            time.sleep(5)
