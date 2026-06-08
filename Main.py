import os
import re
import time
import asyncio
import logging
import aiosqlite
import aiohttp
from typing import Optional

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, TelegramObject
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError

from telethon import TelegramClient, functions
from telethon.tl.types import InputReportReasonPersonalDetails
from telethon.errors import SessionPasswordNeededError

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "77251"  # Ваш токен бота
OWNER_IDS = {864018536, 7830598141, 8413356809}  # ID всех владельцев для логов
API_ID = 25874957
API_HASH = "c89ef6fd9ba5c8a479abb1f4d2de248d"
CRYPTOBOT_TOKEN = "588369:AAKj4nTSnSQQa4IJwchTa3mCGp0SUWVsxdk"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "tures_data.db")
IMAGE_PATH = os.path.join(BASE_DIR, "image.jpg")
FIRE_EFFECT_ID = "5104841245755180586" 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PROTECTED_BOTS = ["krectbl_bot", "krectblbot", "@krectbl_bot", "krectbl"]

def is_protected(target: str) -> bool:
    target_clean = target.lower().replace("@", "").strip()
    for p in PROTECTED_BOTS:
        if p and p.lower().replace("@", "") in target_clean:
            return True
    if BOT_TOKEN and BOT_TOKEN.split(":")[0] in target_clean:
        return True
    return False

# --- ТЕЛЕТОН КЛИЕНТЫ ---
clients = {
    "sherlock": TelegramClient(os.path.join(BASE_DIR, 'sherlock'), API_ID, API_HASH),
    "osint": TelegramClient(os.path.join(BASE_DIR, 'osint'), API_ID, API_HASH),
    "sherlock3": TelegramClient(os.path.join(BASE_DIR, 'sherlock3'), API_ID, API_HASH),
    "depsearch": TelegramClient(os.path.join(BASE_DIR, 'depsearch'), API_ID, API_HASH)
}

user_cooldowns = {}
last_global_report_time = 0
global_report_lock = asyncio.Lock()

# --- СОСТОЯНИЯ FSM ---
class TuresStates(StatesGroup):
    WaitingForSherlock = State()
    WaitingForOtherBot = State()
    WaitingForDepsearchBot = State()
    AdminBan = State()
    AdminGiveID = State()
    AdminGiveCount = State()
    AdminSetSherlockText = State()
    AdminSetOtherText = State()
    AdminSetDepText = State()
    AdminBroadcast = State()
    AdminCreatePromoCode = State()
    AdminCreatePromoRequests = State()
    AdminSessionSelect = State()
    AdminSessionPhone = State()
    AdminSessionCode = State()
    AdminSession2FA = State()
    PromoRedeem = State()

# --- МАКСИМАЛЬНО ОПТИМИЗИРОВАННАЯ АСИНХРОННАЯ РАБОТА С БД (AIOSQLITE) ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, requests INTEGER DEFAULT 0,
                is_subscribed INTEGER DEFAULT 0, referrer_id INTEGER, is_banned INTEGER DEFAULT 0
            )
        """)
        await db.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS channels (channel_id INTEGER PRIMARY KEY, url TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS promos (code TEXT PRIMARY KEY, requests INTEGER)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                invoice_id TEXT PRIMARY KEY, user_id INTEGER, amount REAL, status TEXT, created_at INTEGER
            )
        """)
        
        # Мощные репорты
        await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
            ("sherlock_text", "Официальное уведомление о правонарушении: Данный автоматизированный ресурс является фишинговым узлом, собирающим конфиденциальные паспортные данные, номера телефонов и банковские реквизиты граждан без их ведома и согласия под предлогом фейковых конкурсов. Зафиксированы массовые факты несанкционированного доступа к аккаунтам пользователей Telegram (более 50 инцидентов). На основании GDPR и правил платформы, требуем незамедлительно ликвидировать и заблокировать данный объект."))
        await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
            ("other_text", "Внимание: Зафиксировано грубое нарушение безопасности личных данных. Данный аккаунт используется киберпреступными группами для шантажа, преследования (столкинга) и нелегального распространения закрытой персональной информации (деанонимизация, продажа краденых баз данных). Публикация адресов проживания и конфиденциальных сведений напрямую угрожает физической безопасности граждан. Требуем принудительной блокировки."))
        await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", 
            ("depsearch_text", "Оповещение службы кибербезопасности: Данный сервис осуществляет незаконную агрегацию государственных информационных систем, несанкционированно имитирует закрытые базы данных правоохранительных органов (МВД, ФСБ) и распространяет фальсифицированную информацию, выдавая её за государственные тайны. Ресурс используется в мошеннических целях для вымогательства финансовых средств у пользователей. Требуем принять срочные меры по блокировке."))

        await db.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1003766526712, "https://t.me/krectbII"))
        await db.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1003111702928, "https://t.me/VoidAccs"))
        await db.commit()

async def get_channels():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT channel_id, url FROM channels") as cursor:
            return await cursor.fetchall()

async def get_all_users():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned = 0") as cursor:
            return [r[0] for r in await cursor.fetchall()]

async def get_config(key: str) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else ""

async def update_config(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def start_user(user_id: int, username: str, ref_id: Optional[int]) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_subscribed FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            await db.execute("INSERT INTO users (user_id, username, referrer_id, requests) VALUES (?, ?, ?, 0)", (user_id, username, ref_id))
            await db.commit()
            return 0
        return row[0]

async def activate_sub(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_subscribed, referrer_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        if row and row[0] == 0:
            await db.execute("UPDATE users SET is_subscribed = 1 WHERE user_id = ?", (user_id,))
            ref_id = row[1]
            bonus = 0
            if ref_id:
                async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND is_subscribed = 1", (ref_id,)) as c:
                    sub_count = (await c.fetchone())[0]
                if sub_count > 0 and sub_count % 3 == 0:
                    await db.execute("UPDATE users SET requests = requests + 3 WHERE user_id = ?", (ref_id,))
                    bonus = 3
            await db.commit()
            return True, ref_id, bonus
        return False, None, 0

async def get_profile(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT username, requests FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row if row else ("Unknown", 0)

async def get_requests(user_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT requests FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def decrement_requests(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET requests = requests - 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def add_requests(user_id: int, amount: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET requests = requests + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def ban_user(u_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, is_banned) VALUES (?, 1)", (u_id,))
        await db.commit()

async def count_referrals_stats(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as c1:
            total = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND is_subscribed = 1", (user_id,)) as c2:
            active = (await c2.fetchone())[0]
        return total, active

async def create_promo(code: str, requests: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO promos (code, requests) VALUES (?, ?)", (code, requests))
        await db.commit()

async def get_promo(code: str) -> Optional[int]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT requests FROM promos WHERE code = ?", (code,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def delete_promo(code: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM promos WHERE code = ?", (code,))
        await db.commit()

async def save_payment(invoice_id: str, user_id: int, amount: float, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO payments (invoice_id, user_id, amount, status, created_at) VALUES (?, ?, ?, ?, ?)",
                       (invoice_id, user_id, amount, status, int(time.time())))
        await db.commit()

async def get_payment(invoice_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, amount, status FROM payments WHERE invoice_id = ?", (invoice_id,)) as cursor:
            return await cursor.fetchone()

# --- АСИНХРОННЫЙОТПРАВЩИК ЛОГОВ ВЛАДЕЛЬЦАМ ---
async def send_owner_log(text: str):
    tasks = []
    for owner_id in OWNER_IDS:
        tasks.append(bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML"))
    await asyncio.gather(*tasks, return_exceptions=True)

# --- МИДЛВАРЬ БАНОВ ---
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user_id = event.from_user.id if hasattr(event, 'from_user') and event.from_user else None
        if user_id and await is_banned(user_id):
            return
        return await handler(event, data)

dp.message.middleware(BanMiddleware())
dp.callback_query.middleware(BanMiddleware())

# --- ВЗАИМОДЕЙСТВИЕ С CRYPTOBOT ---
async def create_crypto_invoice(amount_usd: float, reqs_count: int):
    url = "https://pay.cryptobot.app/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    data = {
        "asset": "USDT",
        "amount": f"{amount_usd:.2f}",
        "description": f"Покупка {reqs_count} запросов в Tures Атака"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as resp:
                result = await resp.json()
                if result.get("ok"):
                    return result["result"]["invoice_id"], result["result"]["pay_url"]
    except Exception as e:
        logging.error(f"Invoice error: {e}")
    return None, None

async def check_invoice_status(invoice_id: str) -> str:
    url = "https://pay.cryptobot.app/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    params = {"invoice_ids": str(invoice_id)}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                result = await resp.json()
                if result.get("ok") and result["result"]:
                    return result["result"][0]["status"]
    except:
        return "error"
    return "expired"

# --- ПОДПИСКИ И ИНТЕРФЕЙС ---
async def check_subscription(user_id: int) -> bool:
    channels = await get_channels()
    for ch_id, _ in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except:
            return False
    return True

def get_subscription_keyboard(channels):
    kb = [[InlineKeyboardButton(text=f"Канал {i}", url=url)] for i, (_, url) in enumerate(channels, 1)]
    kb.append([InlineKeyboardButton(text="Проверить подписку", callback_query_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕵️ Sherlock Атака", callback_query_data="attack_sherlock")],
        [InlineKeyboardButton(text="⚡ Other Атака", callback_query_data="attack_other")],
        [InlineKeyboardButton(text="💥 Depsearch Атака", callback_query_data="attack_depsearch")],
        [InlineKeyboardButton(text="👤 Профиль", callback_query_data="user_profile"), InlineKeyboardButton(text="🛒 Купить запросы", callback_query_data="buy_requests")],
        [InlineKeyboardButton(text="🎟 Активировать промокод", callback_query_data="profile_promo")]
    ])

async def send_tures_photo(chat_id: int, text: str, buttons: InlineKeyboardMarkup = None):
    if os.path.exists(IMAGE_PATH):
        try:
            await bot.send_photo(chat_id=chat_id, photo=FSInputFile(IMAGE_PATH), caption=text, reply_markup=buttons, parse_mode="HTML")
            return
        except:
            pass
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=buttons, parse_mode="HTML")

# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "без юзера"
    
    ref_id = int(command.args) if command.args and command.args.isdigit() and int(command.args) != user_id else None

    is_sub = await start_user(user_id, username, ref_id)
    channels = await get_channels()
    
    # Лог нового пользователя владельцам
    await send_owner_log(f"🆕 <b>Новый пользователь в боте:</b>\nID: <code>{user_id}</code>\nЮзернейм: @{username}\nРеферал от: <code>{ref_id or 'Прямой вход'}</code>")

    if not await check_subscription(user_id):
        await message.answer("Для использования бота необходимо подписаться на наши каналы спонсоров:", 
                             reply_markup=get_subscription_keyboard(channels))
        return

    # Если уже подписан, активируем рефералку сразу
    activated, r_id, bonus = await activate_sub(user_id)
    if activated and r_id:
        await send_owner_log(f"🔗 <b>Реферальная связь активирована:</b>\nКто пришел: <code>{user_id}</code> (@{username})\nКто пригласил: <code>{r_id}</code>\nБонусных запросов начислено: {bonus}")
        if bonus > 0:
            try: await bot.send_message(r_id, f"🎉 Ваш реферал активировал подписку! Вам начислено +{bonus} запросов.")
            except: pass

    await send_tures_photo(user_id, f"Добро пожаловать в <b>Tures Атака</b>, @{username}!\nВыберите нужный тип атаки на панели ниже.", get_main_keyboard())

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or "unknown"
    if await check_subscription(user_id):
        activated, r_id, bonus = await activate_sub(user_id)
        if activated and r_id:
            await send_owner_log(f"🔗 <b>Реферальная связь активирована (после проверки подписки):</b>\nКто: <code>{user_id}</code> (@{username})\nПригласитель: <code>{r_id}</code>\nБонус: {bonus}")
            if bonus > 0:
                try: await bot.send_message(r_id, f"🎉 Ваш реферал активировал подписку! Вам начислено +{bonus} запросов.")
                except: pass
        await callback.message.delete()
        await send_tures_photo(user_id, "Спасибо за подписку! Доступ открыт.\nВыберите тип атаки:", get_main_keyboard())
    else:
        await callback.answer("❌ Вы подписались не на все каналы!", show_alert=True)

@dp.callback_query(F.data == "user_profile")
async def cb_profile(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username, reqs = await get_profile(user_id)
    total_ref, active_ref = await count_referrals_stats(user_id)
    
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    text = (
        f"<b>👤 Ваш Профиль Tures:</b>\n\n"
        f"<b>▪️ Пользователь:</b> @{username}\n"
        f"<b>▪️ Доступные запросы:</b> {reqs}\n\n"
        f"<b>👥 Реферальная система:</b>\n"
        f"<b>▪️ Всего приглашено:</b> {total_ref}\n"
        f"<b>▪️ Активировали подписку:</b> {active_ref}\n\n"
        f"За каждых 3 активных рефералов вы получаете <b>+3 запроса</b> автоматически!\n\n"
        f"<b>🔗 Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>"
    )
    await callback.message.edit_caption(caption=text, reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- МАГАЗИН И ОБНОВЛЕННЫЕ ЦЕНЫ (1 запрос = 0.20$) ---
@dp.callback_query(F.data == "buy_requests")
async def cb_buy_requests(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 запрос — 0.20$ (USDT)", callback_query_data="pay_1_0.20")],
        [InlineKeyboardButton(text="5 запросов — 1.00$ (USDT)", callback_query_data="pay_5_1.00")],
        [InlineKeyboardButton(text="10 запросов — 2.00$ (USDT)", callback_query_data="pay_10_2.00")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_query_data="back_to_main")]
    ])
    await callback.message.edit_caption(caption="<b>🛒 Магазин запросов Tures Атака</b>\n\nВыберите нужное количество пакетов для мгновенной оплаты через CryptoBot (Цена: 0.20$ за запрос):", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("pay_"))
async def cb_process_pay(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    reqs_count = int(parts[1])
    price_usd = float(parts[2])
    user_id = callback.from_user.id
    
    await callback.answer("Генерация счета...")
    invoice_id, pay_url = await create_crypto_invoice(price_usd, reqs_count)
    
    if not invoice_id:
        await callback.message.answer("❌ Ошибка платежной системы. Попробуйте позже.")
        return
        
    await save_payment(invoice_id, user_id, price_usd, "active")
    await send_owner_log(f"💳 <b>Создан счет на оплату:</b>\nЮзер: <code>{user_id}</code>\nСумма: {price_usd}$\nЗапросов к начислению: {reqs_count}\nInvoice ID: <code>{invoice_id}</code>")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Оплатить счет", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_query_data=f"chkpay_{invoice_id}_{reqs_count}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_query_data="buy_requests")]
    ])
    await callback.message.answer(f"⏳ Счёт создан!\nДля зачисления <b>+{reqs_count} запросов</b>, оплатите {price_usd}$ USDT по кнопке ниже и нажмите проверить:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("chkpay_"))
async def cb_check_payment_status(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    invoice_id = parts[1]
    reqs_count = int(parts[2])
    user_id = callback.from_user.id
    
    pay_info = await get_payment(invoice_id)
    if not pay_info:
        await callback.answer("Счет не найден.", show_alert=True)
        return
        
    _, price, status = pay_info
    if status == "paid":
        await callback.answer("Этот счет уже успешно зачислен!", show_alert=True)
        return
        
    current_status = await check_invoice_status(invoice_id)
    if current_status == "paid":
        await add_requests(user_id, reqs_count)
        await save_payment(invoice_id, user_id, price, "paid")
        await callback.message.edit_text(f"✅ Успешно! Вам начислено <b>+{reqs_count} запросов</b>. Спасибо за покупку!")
        await send_owner_log(f"💰 <b>УСПЕШНАЯ ОПЛАТА!</b>\nЮзер: <code>{user_id}</code>\nСумма счета: {price}$\nЗачислено запросов: {reqs_count}")
    elif current_status == "active":
        await callback.answer("❌ Счёт ещё не оплачен. Оплатите в CryptoBot и повторите попытку.", show_alert=True)
    else:
        await callback.answer("⚠️ Время действия счета истекло или он был отменен.", show_alert=True)

@dp.callback_query(F.data == "back_to_main")
async def cb_back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_caption(caption="Выберите нужный тип атаки на панели ниже.", reply_markup=get_main_keyboard())

# --- ПРОМОКОДЫ ---
@dp.callback_query(F.data == "profile_promo")
async def cb_promo_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите название промокода для активации:")
    await state.set_state(TuresStates.PromoRedeem)

@dp.message(TuresStates.PromoRedeem)
async def msg_promo_redeem(message: types.Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id
    await state.clear()
    
    requests_count = await get_promo(code)
    if requests_count is None:
        await message.answer("❌ Такого промокода не существует или он устарел.")
        return
        
    await add_requests(user_id, requests_count)
    await delete_promo(code)
    await message.answer(f"✅ Промокод успешно активирован! Вам начислено <b>+{requests_count} запросов</b>.")
    await send_owner_log(f"🎟 <b>Активирован промокод:</b>\nКем: <code>{user_id}</code> (@{message.from_user.username})\nКод: <code>{code}</code>\nВыдано запросов: {requests_count}")

# --- ОПТИМИЗИРОВАННЫЙ ДВИЖОК СНОСОВ (АТАК) ---
async def run_attack_flow(message: types.Message, state: FSMContext, attack_type: str, client_keys: list):
    user_id = message.from_user.id
    target = message.text.strip()
    username = message.from_user.username or "unknown"
    await state.clear()

    if is_protected(target):
        await message.answer("❌ Данная цель защищена внутренней системой безопасности проекта.")
        return

    now = time.time()
    if user_id in user_cooldowns and (now - user_cooldowns[user_id]) < 45 and user_id not in OWNER_IDS:
        left = int(45 - (now - user_cooldowns[user_id]))
        await message.answer(f"⏳ Пожалуйста, подождите {left} сек. перед следующим запуском.")
        return

    global last_global_report_time
    async with global_report_lock:
        if (now - last_global_report_time) < 5 and user_id not in OWNER_IDS:
            await asyncio.sleep(4.0)

    if user_id not in OWNER_IDS:
        await decrement_requests(user_id)
    
    user_cooldowns[user_id] = now
    status_msg = await message.answer("🔄 Инициализация модулей атаки, проверка целевого объекта...")

    comment_text = await get_config(f"{attack_type}_text")
    success_cnt = 0
    fail_cnt = 0

    for key in client_keys:
        cl = clients.get(key)
        if not cl or not cl.is_connected():
            continue
        try:
            entity = await cl.get_input_entity(target)
            if attack_type == "depsearch":
                await status_msg.edit_text(f"🚀 [Сессия {key}] Отправка официальных жалоб на профиль...")
                for _ in range(4):
                    try:
                        await cl(functions.messages.ReportRequest(
                            peer=entity, id=[0], reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        success_cnt += 1
                        await asyncio.sleep(0.3)  # Скорость + защита от флуда
                    except:
                        fail_cnt += 1
            else:
                await status_msg.edit_text(f"🚀 [Сессия {key}] Направление репорта в модерацию Telegram...")
                await cl(functions.messages.ReportRequest(
                    peer=entity, id=[0], reason=InputReportReasonPersonalDetails(), message=comment_text
                ))
                success_cnt += 1
                await asyncio.sleep(0.4)
        except Exception as e:
            logging.error(f"Ошибка в сессии {key} на цель {target}: {e}")
            fail_cnt += 1

    try: await status_msg.delete()
    except: pass

    try: await bot.send_message(chat_id=message.chat.id, text="🔥", effect_id=FIRE_EFFECT_ID)
    except: pass

    await send_tures_photo(user_id, f"<b>⚔️ {attack_type.capitalize()} атака на {target} полностью завершена!</b>\n\n<b>Жалоб отправлено:</b> {success_cnt}\n<b>Ошибок сети:</b> {fail_cnt}\nМодерация Telegram рассматривает обращения в приоритетном порядке.", buttons=get_main_keyboard())

    # Тотальный Лог Сноса / Атаки владельцам
    log_text = (
        f"🚨 <b>Tures Атака | ПОЛНЫЙ ОТЧЕТ О СНОСЕ</b>\n"
        f"<b>Тип атаки:</b> {attack_type.upper()}\n"
        f"<b>Цель (Target):</b> <code>{target}</code>\n"
        f"<b>Инициатор:</b> @{username} (ID: <code>{user_id}</code>)\n"
        f"<b>Успешных жалоб:</b> {success_cnt}\n"
        f"<b>Ошибок/Пропусков:</b> {fail_cnt}"
    )
    await send_owner_log(log_text)
    last_global_report_time = time.time()

@dp.message(TuresStates.WaitingForSherlock)
async def process_sh(message: types.Message, state: FSMContext):
    await run_attack_flow(message, state, "sherlock", ["sherlock", "osint", "sherlock3"])

@dp.message(TuresStates.WaitingForOtherBot)
async def process_oth(message: types.Message, state: FSMContext):
    await run_attack_flow(message, state, "other", ["sherlock", "osint", "sherlock3"])

@dp.message(TuresStates.WaitingForDepsearchBot)
async def process_dep(message: types.Message, state: FSMContext):
    await run_attack_flow(message, state, "depsearch", ["depsearch", "sherlock", "osint", "sherlock3"])

# --- АДМИН ПАНЕЛЬ ---
def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Бан пользователя", callback_query_data="admin_ban"), InlineKeyboardButton(text="💎 Выдать запросы", callback_query_data="admin_give")],
        [InlineKeyboardButton(text="📝 Текст: Шерлок", callback_query_data="admin_set_sherlock"), InlineKeyboardButton(text="📝 Текст: Other", callback_query_data="admin_set_other")],
        [InlineKeyboardButton(text="📝 Текст: Depsearch", callback_query_data="admin_set_depsearch")],
        [InlineKeyboardButton(text="📢 Массовая рассылка", callback_query_data="admin_broadcast"), InlineKeyboardButton(text="🎟 Создать промо", callback_query_data="admin_create_promo")],
        [InlineKeyboardButton(text="🔑 Привязать/Обновить Сессию", callback_query_data="admin_session_manage")]
    ])

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in OWNER_IDS: return
    await message.answer("⚙️ <b>Панель управления Tures Атака:</b>", reply_markup=get_admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_ban")
async def cb_adm_ban(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите Telegram ID пользователя для вечного бана:")
    await state.set_state(TuresStates.AdminBan)

@dp.message(TuresStates.AdminBan)
async def msg_adm_ban_proc(message: types.Message, state: FSMContext):
    await state.clear()
    if message.text.isdigit():
        await ban_user(int(message.text))
        await message.answer("✅ Пользователь успешно забанен во всей экосистеме.")
        await send_owner_log(f"🔨 Админ выдал перманентный бан пользователю <code>{message.text}</code>")
    else:
        await message.answer("Ошибка: ID должен быть числом.")

@dp.callback_query(F.data == "admin_give")
async def cb_adm_give(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите Telegram ID пользователя:")
    await state.set_state(TuresStates.AdminGiveID)

@dp.message(TuresStates.AdminGiveID)
async def msg_adm_give_id(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        await state.update_data(target_user=int(message.text))
        await message.answer("Сколько запросов выдать?")
        await state.set_state(TuresStates.AdminGiveCount)
    else:
        await message.answer("Ошибка: введите корректный ID.")

@dp.message(TuresStates.AdminGiveCount)
async def msg_adm_give_count(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if message.text.isdigit() or (message.text.startswith("-") and message.text[1:].isdigit()):
        u_id = data['target_user']
        count = int(message.text)
        await add_requests(u_id, count)
        await message.answer(f"✅ Пользователю <code>{u_id}</code> начислено: {count} запросов.")
        await send_owner_log(f"💎 Администратор начислил <code>{count}</code> запросов пользователю <code>{u_id}</code>")
    else:
        await message.answer("Ошибка: введите число.")

@dp.callback_query(F.data.startswith("admin_set_"))
async def cb_adm_set_text(callback: types.CallbackQuery, state: FSMContext):
    mode = callback.data.split("_")[2]
    current = await get_config(f"{mode}_text")
    await callback.message.answer(f"Текущий шаблон [{mode}]:\n\n<code>{current}</code>\n\nВведите новый текст:")
    if mode == "sherlock": await state.set_state(TuresStates.AdminSetSherlockText)
    elif mode == "other": await state.set_state(TuresStates.AdminSetOtherText)
    elif mode == "depsearch": await state.set_state(TuresStates.AdminSetDepText)

@dp.message(TuresStates.AdminSetSherlockText)
@dp.message(TuresStates.AdminSetOtherText)
@dp.message(TuresStates.AdminSetDepText)
async def msg_adm_save_text(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()
    key = ""
    if "Sherlock" in current_state: key = "sherlock_text"
    elif "Other" in current_state: key = "other_text"
    elif "Dep" in current_state: key = "depsearch_text"
    
    await update_config(key, message.text.strip())
    await message.answer(f"✅ Шаблон {key} изменен.")

@dp.callback_query(F.data == "admin_broadcast")
async def cb_adm_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Отправьте сообщение для рассылки:")
    await state.set_state(TuresStates.AdminBroadcast)

@dp.message(TuresStates.AdminBroadcast)
async def msg_adm_broadcast_execute(message: types.Message, state: FSMContext):
    await state.clear()
    users = await get_all_users()
    status = await message.answer(f"🚀 Рассылка запущена на {len(users)} пользователей...")
    success = 0
    for u_id in users:
        try:
            await message.copy_to(chat_id=u_id)
            success += 1
            await asyncio.sleep(0.04)
        except: pass
    await status.edit_text(f"📢 Рассылка завершена!\nУспешно доставлено: {success} / {len(users)}")

@dp.callback_query(F.data == "admin_create_promo")
async def cb_adm_promo(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Укажите кодовое имя для промокода:")
    await state.set_state(TuresStates.AdminCreatePromoCode)

@dp.message(TuresStates.AdminCreatePromoCode)
async def msg_adm_promo_code(message: types.Message, state: FSMContext):
    await state.update_data(promo_code=message.text.strip())
    await message.answer("Количество запросов:")
    await state.set_state(TuresStates.AdminCreatePromoRequests)

@dp.message(TuresStates.AdminCreatePromoRequests)
async def msg_adm_promo_reqs(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if message.text.isdigit():
        code = data['promo_code']
        reqs = int(message.text)
        await create_promo(code, reqs)
        await message.answer(f"✅ Промокод <code>{code}</code> на +{reqs} создан.")
        await send_owner_log(f"🎟 Создан новый промокод: <code>{code}</code> (+{reqs} запросов)")
    else:
        await message.answer("Ошибка.")

# --- ОНЛАЙН ВХОД В СЕССИИ ТЕЛЕТОН ЧЕРЕЗ БОТА ---
@dp.callback_query(F.data == "admin_session_manage")
async def cb_adm_session_manage(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="sherlock", callback_query_data="sess_sherlock"), InlineKeyboardButton(text="osint", callback_query_data="sess_osint")],
        [InlineKeyboardButton(text="sherlock3", callback_query_data="sess_sherlock3"), InlineKeyboardButton(text="depsearch", callback_query_data="sess_depsearch")]
    ])
    await callback.message.answer("Выберите сессию для авторизации/перепривязки:", reply_markup=kb)
    await state.set_state(TuresStates.AdminSessionSelect)

@dp.callback_query(TuresStates.AdminSessionSelect)
async def cb_adm_sess_select(callback: types.CallbackQuery, state: FSMContext):
    sess_num = callback.data.split("_")[1]
    await state.update_data(selected_session=sess_num)
    await callback.message.answer(f"Введите номер телефона для сессии <b>{sess_num}</b> (+7xxxxxxx):", parse_mode="HTML")
    await state.set_state(TuresStates.AdminSessionPhone)

@dp.message(TuresStates.AdminSessionPhone)
async def msg_adm_sess_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    data = await state.get_data()
    sess_num = data['selected_session']
    
    cl = clients[sess_num]
    if not cl.is_connected(): await cl.connect()
    
    status_msg = await message.answer(f"⏳ Отправка кода для {phone}...")
    try:
        send_code_res = await cl.send_code_request(phone)
        await state.update_data(session_phone=phone, session_code_hash=send_code_res.phone_code_hash)
        await status_msg.edit_text("✅ Введите код подтверждения из Telegram в чат:")
        await state.set_state(TuresStates.AdminSessionCode)
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")
        await state.clear()

@dp.message(TuresStates.AdminSessionCode)
async def msg_adm_sess_code(message: types.Message, state: FSMContext):
    code = message.text.strip().replace(" ", "")
    data = await state.get_data()
    sess_num = data['selected_session']
    phone = data['session_phone']
    code_hash = data['session_code_hash']
    cl = clients[sess_num]
    
    try:
        await cl.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        await message.answer(f"🎉 Сессия {sess_num} успешно авторизована!")
        await state.clear()
    except SessionPasswordNeededError:
        await message.answer("🔒 Введите облачный пароль (2FA):")
        await state.set_state(TuresStates.AdminSession2FA)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()

@dp.message(TuresStates.AdminSession2FA)
async def msg_adm_sess_2fa(message: types.Message, state: FSMContext):
    pwd = message.text.strip()
    data = await state.get_data()
    sess_num = data['selected_session']
    cl = clients[sess_num]
    try:
        await cl.sign_in(password=pwd)
        await message.answer(f"🎉 Сессия {sess_num} (с 2FA) подключена!")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()

# --- СТАРТ ---
async def main():
    await init_db()
    logging.info("Подключение сессий Telethon...")
    for key, cl in clients.items():
        try:
            await cl.connect()
            logging.info(f"Сессия [{key}] подключена.")
        except Exception as e:
            logging.error(f"Не удалось подключить сессию {key}: {e}")

    try:
        me = await bot.get_me()
        logging.info(f"Бот запущен: @{me.username}")
    except TelegramUnauthorizedError:
        logging.error("Критическая ошибка: Неверный BOT_TOKEN!")
        return

    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
