import os
import re
import time
import asyncio
import logging
import sqlite3
from typing import Optional

from telethon import TelegramClient, events, Button, functions, types, errors
from telethon.tl.types import InputReportReasonPersonalDetails
from telethon.tl.functions.contacts import BlockRequest
from telethon.tl.functions.channels import GetParticipantRequest

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "77251"  # Укажите токен
OWNER_IDS = {8640180536}
API_ID = 25874957
API_HASH = "c89ef6fd9ba5c8a479abb1f4d2de248d"

# Динамическое определение папки проекта
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "shakal_data.db")
IMAGE_PATH = os.path.join(BASE_DIR, "image.jpg")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ---
bot = TelegramClient(os.path.join(BASE_DIR, 'bot_session'), API_ID, API_HASH)

clients = {
    "sherlock": TelegramClient(os.path.join(BASE_DIR, 'sherlock'), API_ID, API_HASH),
    "osint": TelegramClient(os.path.join(BASE_DIR, 'osint'), API_ID, API_HASH),
    "sherlock3": TelegramClient(os.path.join(BASE_DIR, 'sherlock3'), API_ID, API_HASH),
    "depsearch": TelegramClient(os.path.join(BASE_DIR, 'depsearch'), API_ID, API_HASH)
}

# Кулдауны и блокировки
user_cooldowns = {}
last_global_report_time = 0
global_report_lock = asyncio.Lock()
active_auth_clients = {}

# --- СОСТОЯНИЯ (FSM) ---
user_states = {}

class ShakalStates:
    WaitingForSherlock = "WaitingForSherlock"
    WaitingForOtherBot = "WaitingForOtherBot"
    WaitingForOtherWord = "WaitingForOtherWord"
    WaitingForDepsearchBot = "WaitingForDepsearchBot"
    
    AdminBan = "AdminBan"
    AdminGiveID = "AdminGiveID"
    AdminGiveCount = "AdminGiveCount"
    AdminSetSherlockText = "AdminSetSherlockText"
    AdminSetOtherText = "AdminSetOtherText"
    AdminSetDepText = "AdminSetDepText"
    AdminBroadcast = "AdminBroadcast" # Новое состояние для рассылки
    
    AdminSessionPhone = "AdminSessionPhone"
    AdminSessionCode = "AdminSessionCode"
    AdminSession2FA = "AdminSession2FA"
    
    PromoRedeem = "PromoRedeem"

def set_state(user_id: int, state: str, data: dict = None):
    user_states[user_id] = {"state": state, "data": data or {}}

def get_state(user_id: int):
    return user_states.get(user_id, {"state": None, "data": {}})

def clear_state(user_id: int):
    user_states.pop(user_id, None)

def update_state_data(user_id: int, key: str, value):
    if user_id in user_states:
        user_states[user_id]["data"][key] = value

# --- СИНХРОННЫЕ ФУНКЦИИ БАЗЫ ДАННЫХ ---
def sync_init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            requests INTEGER DEFAULT 0,
            is_subscribed INTEGER DEFAULT 0,
            referrer_id INTEGER,
            is_banned INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            url TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY,
            count INTEGER
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("sherlock_text", "Сообщаю о том, что данный бот нарушает правила Telegram, предоставляя доступ к закрытым персональным данным. Подобная деятельность способствует краже личности и незаконному сбору информации о людях. Просьба заблокировать аккаунт бота за нарушение конфиденциальности"))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("other_text", "Добрый день. Данный бот занимается незаконным распространением конфиденциальной информации и нарушает закон о защите персональных данных. Прошу провести проверку и ограничить доступ к этому боту."))
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("depsearch_text", "Этот бот используется как инструмент для «пробива» граждан: он выдает полные паспортные данные, прописку и другие личные идентификаторы по запросу. Такая деятельность прямо запрещена правилами Telegram и нарушает тайну частной жизни. Прошу рассмотреть жалобу и заблокировать ресурс."))

    cursor.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1003766526712, "https://t.me/krectbII"))
    cursor.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)", (-1003111702928, "https://t.me/VoidAccs"))
    conn.commit()
    conn.close()

def sync_get_channels():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, url FROM channels")
    rows = cursor.fetchall()
    conn.close()
    return rows

def sync_add_channel(channel_id: int, url: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO channels (channel_id, url) VALUES (?, ?)", (channel_id, url))
    conn.commit()
    conn.close()

def sync_get_all_users():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

def sync_get_config(key: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""

def sync_update_config(key: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def sync_is_banned(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row[0]) if row else False

def sync_start_user(user_id: int, username: str, ref_id: Optional[int]) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_subscribed FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)", (user_id, username, ref_id))
        conn.commit()
        is_sub = 0
    else:
        is_sub = row[0]
    conn.close()
    return is_sub

def sync_activate_sub(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_subscribed, referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    referrer_id = None
    remains = 0
    activated = False

    if row and row[0] == 0:
        cursor.execute("UPDATE users SET is_subscribed = 1 WHERE user_id = ?", (user_id,))
        referrer_id = row[1]
        activated = True

        if referrer_id:
            cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND is_subscribed = 1", (referrer_id,))
            sub_ref_count = cursor.fetchone()[0]
            remains = 3 - (sub_ref_count % 3)
            if remains == 3:
                cursor.execute("UPDATE users SET requests = requests + 3 WHERE user_id = ?", (referrer_id,))
    conn.commit()
    conn.close()
    return activated, referrer_id, remains

def sync_get_profile(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, requests FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row if row else ("Пользователь", 0)

def sync_get_requests(user_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT requests FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def sync_decrement_requests(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET requests = requests - 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def sync_ban_user(u_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (user_id, is_banned) VALUES (?, 1)", (u_id,))
    conn.commit()
    conn.close()

def sync_give_requests(u_id: int, count: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET requests = requests + ? WHERE user_id = ?", (count, u_id))
    conn.commit()
    conn.close()

def sync_count_referrals(user_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def sync_is_subscribed(user_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_subscribed FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def sync_create_promo(code: str, count: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO promos (code, count) VALUES (?, ?)", (code, count))
    conn.commit()
    conn.close()

def sync_get_promo(code: str) -> Optional[int]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT count FROM promos WHERE code = ?", (code,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def sync_delete_promo(code: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM promos WHERE code = ?", (code,))
    conn.commit()
    conn.close()

# --- АСИНХРОННЫЕ ОБЕРТКИ ---
async def is_banned(user_id: int) -> bool: return await asyncio.to_thread(sync_is_banned, user_id)
async def get_config(key: str) -> str: return await asyncio.to_thread(sync_get_config, key)
async def update_config(key: str, value: str): await asyncio.to_thread(sync_update_config, key, value)
async def get_referrals(user_id: int) -> int: return await asyncio.to_thread(sync_count_referrals, user_id)
async def is_subscribed(user_id: int) -> int: return await asyncio.to_thread(sync_is_subscribed, user_id)
async def create_promo(code: str, cnt: int): await asyncio.to_thread(sync_create_promo, code, cnt)
async def get_promo(code: str) -> Optional[int]: return await asyncio.to_thread(sync_get_promo, code)
async def delete_promo(code: str): await asyncio.to_thread(sync_delete_promo, code)
async def add_channel(channel_id: int, url: str): await asyncio.to_thread(sync_add_channel, channel_id, url)
async def get_all_users(): return await asyncio.to_thread(sync_get_all_users)

async def restart_global_client(sess_key: str):
    if sess_key not in clients:
        return
    clients[sess_key] = TelegramClient(os.path.join(BASE_DIR, sess_key), API_ID, API_HASH)
    await clients[sess_key].connect()

def is_owner(uid: int) -> bool:
    return uid in OWNER_IDS

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return [
        [Button.inline("Шąкализирơвать", b"menu_shakal")], 
        [Button.inline("Профиль", b"menu_profile")]        
    ]

def get_profile_keyboard():
    return [
        [Button.inline("Вернуться обратно", b"profile_back")],
        [Button.inline("Промокод", b"profile_promo")]
    ]

async def build_subscription_keyboard():
    channels = await asyncio.to_thread(sync_get_channels)
    buttons = []
    for idx, (ch_id, url) in enumerate(channels, start=1):
        buttons.append([Button.url(f"Подписаться на канал #{idx}", url)])
    buttons.append([Button.inline("Проверить подписку ✅", b"check_subscription")])
    return buttons

async def send_shakal_photo(chat_id: int, caption: str, buttons=None):
    if os.path.exists(IMAGE_PATH):
        return await bot.send_file(chat_id, file=IMAGE_PATH, caption=caption, buttons=buttons, parse_mode="HTML")
    return await bot.send_message(chat_id, caption, buttons=buttons, parse_mode="HTML")

# --- КОМАНДЫ ДЛЯ ОВНЕРОВ ---
@bot.on(events.NewMessage(pattern=r'^/add(?:\s+(.*))?$'))
async def cmd_add_channel(event):
    if not is_owner(event.sender_id): return
    if await is_banned(event.sender_id): return
    args = event.pattern_match.group(1)
    
    if not args:
        await event.respond("❌ Неверный формат! Используйте:\n`/add <айди_канала> <ссылка>`\n\nПример:\n`/add -1003441944576 https://t.me/+P-pynIFyi9gwYjE1`", parse_mode="md")
        return

    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')', args)
    parts = [p.strip('"\'') for p in parts]

    if len(parts) < 2:
        await event.respond("❌ Ошибка! Необходимо указать и ID, и ссылку.")
        return

    ch_id_str, ch_url = parts[0], parts[1]
    if not ch_id_str.replace('-', '').isdigit():
        await event.respond("❌ ID канала должен быть числовым!")
        return

    ch_id = int(ch_id_str)
    await add_channel(ch_id, ch_url)
    await event.respond(f"✅ Канал успешно добавлен в список обязательных подписок!\n\n<b>ID:</b> <code>{ch_id}</code>\n<b>Ссылка:</b> {ch_url}", parse_mode="html")

@bot.on(events.NewMessage(pattern=r'^/adder(?:\s+(.*))?$'))
async def cmd_adder(event):
    if not is_owner(event.sender_id): return
    if await is_banned(event.sender_id): return
    
    text_to_send = event.pattern_match.group(1)
    if not text_to_send and not event.message.media:
        await event.respond("❌ Напишите текст рассылки после команды! Пример:\n`/adder Всем привет!`", parse_mode="md")
        return

    users = await asyncio.to_thread(sync_get_all_users)
    status_msg = await event.respond(f"⏳ Запуск рассылки... Всего пользователей: <code>{len(users)}</code>", parse_mode="html")

    kb = [[Button.inline("🔘 Ознакомлен", b"read_broadcast")]]
    success_cnt = 0
    
    for u_id in users:
        try:
            if event.message.media:
                await bot.send_file(u_id, event.message.media, caption=text_to_send or "", buttons=kb, parse_mode="html")
            else:
                await bot.send_message(u_id, text_to_send, buttons=kb, parse_mode="html")
            success_cnt += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await status_msg.edit(f"📢 <b>Рассылка завершена!</b>\n\nУспешно отправлено: <code>{success_cnt}</code> из <code>{len(users)}</code> пользователям.", parse_mode="html")

@bot.on(events.NewMessage(pattern=r'^/promo(?:\s+(.*))?$'))
async def cmd_create_promo(event):
    if not is_owner(event.sender_id): return
    if await is_banned(event.sender_id): return
    
    args = event.pattern_match.group(1)
    if not args:
        await event.respond('Использование: /promo "название" "кол-во"')
        return
        
    parts = re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')', args)
    parts = [p.strip('"\'') for p in parts]
    if len(parts) < 2 or not parts[1].isdigit():
        await event.respond('Неверный формат. Пример: /promo SPRING2026 5')
        return
        
    code = parts[0]
    cnt = int(parts[1])
    await create_promo(code, cnt)
    await event.respond(f"✅ Промокод {code} создан на {cnt} запросов.")

@bot.on(events.NewMessage(pattern=r'^/admin$'))
async def cmd_admin(event):
    if not is_owner(event.sender_id): return
    if await is_banned(event.sender_id): return
    
    kb = [
        [Button.inline("1. Бан пользователя", b"admin_ban")],
        [Button.inline("2. Выдать запросы", b"admin_give")],
        [Button.inline("3. Изменить текст Шерлока", b"admin_set_sherlock")],
        [Button.inline("4. Изменить текст Других ботов", b"admin_set_other")],
        [Button.inline("5. Изменить текст Depsearch", b"admin_set_dep")],
        [Button.inline("🔄 Заменить сессию", b"admin_change_session")],
        [Button.inline("📢 Рассылка", b"admin_broadcast")]
    ]
    await event.respond("👑 <b>Панель управления Владельца:</b>", parse_mode="html", buttons=kb)

# --- СТАРТ И ОСНОВНОЕ МЕНЮ ---
@bot.on(events.NewMessage(pattern=r'^/start(?:\s+(.*))?$'))
async def cmd_start(event):
    if await is_banned(event.sender_id): return
    
    user_id = event.sender_id
    sender = await event.get_sender()
    username = sender.username or "без юзера"

    ref_id = None
    args = event.pattern_match.group(1)
    if args and args.isdigit():
        potential_ref = int(args)
        if potential_ref != user_id:
            ref_id = potential_ref

    is_sub = await asyncio.to_thread(sync_start_user, user_id, username, ref_id)

    if is_sub == 1:
        await send_shakal_photo(user_id, "<b>Главное меню:</b>", buttons=get_main_keyboard())
    else:
        kb = await build_subscription_keyboard()
        await send_shakal_photo(user_id, "Чтобы пользоваться ботом, подпишись на каналы ниже.", buttons=kb)

# --- FSM ХЭНДЛЕРЫ ---
@bot.on(events.NewMessage)
async def fsm_handler(event):
    user_id = event.sender_id
    if await is_banned(user_id): return
    
    if event.text.startswith('/'): return # Игнорируем команды
    
    state_info = get_state(user_id)
    state = state_info["state"]
    data = state_info["data"]
    
    if not state:
        return

    text = event.text.strip()
    sender = await event.get_sender()
    username = sender.username or "Unknown"

    if state == ShakalStates.PromoRedeem:
        code = text
        cnt = await get_promo(code)
        clear_state(user_id)
        if cnt is None:
            await event.respond("❌ Промокод не найден или уже использован.")
            return
        await asyncio.to_thread(sync_give_requests, user_id, cnt)
        await delete_promo(code)
        await event.respond(f"✅ Промокод принят. Вам зачислено +{cnt} запрос(ов).")

    elif state == ShakalStates.AdminBroadcast:
        clear_state(user_id)
        users = await asyncio.to_thread(sync_get_all_users)
        status_msg = await event.respond(f"⏳ Запуск рассылки... Всего пользователей: <code>{len(users)}</code>", parse_mode="html")

        kb = [[Button.inline("🔘 Ознакомлен", b"read_broadcast")]]
        success_cnt = 0
        
        media = event.message.media
        text_to_send = event.text

        for u_id in users:
            try:
                if media:
                    await bot.send_file(u_id, media, caption=text_to_send, buttons=kb, parse_mode="html")
                else:
                    await bot.send_message(u_id, text_to_send, buttons=kb, parse_mode="html")
                success_cnt += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass

        await status_msg.edit(f"📢 <b>Рассылка завершена!</b>\n\nУспешно отправлено: <code>{success_cnt}</code> из <code>{len(users)}</code> пользователям.", parse_mode="html")

    elif state == ShakalStates.AdminBan:
        clear_state(user_id)
        if not text.isdigit():
            await event.respond("❌ ID должен быть числом.")
            return
        u_id = int(text)
        await asyncio.to_thread(sync_ban_user, u_id)
        await event.respond(f"⛔️ Пользователь <code>{u_id}</code> забанен навсегда.", parse_mode="html")

    elif state == ShakalStates.AdminGiveID:
        if not text.isdigit():
            await event.respond("❌ ID должен быть числом.")
            clear_state(user_id)
            return
        update_state_data(user_id, 'target_user', int(text))
        set_state(user_id, ShakalStates.AdminGiveCount, get_state(user_id)['data'])
        await event.respond("Введите количество запросов:")

    elif state == ShakalStates.AdminGiveCount:
        clear_state(user_id)
        if not text.isdigit():
            await event.respond("❌ Количество должно быть числом.")
            return
        u_id = data['target_user']
        count = int(text)
        await asyncio.to_thread(sync_give_requests, u_id, count)
        await event.respond(f"💎 Пользователю <code>{u_id}</code> успешно зачислено +{count} запросов.", parse_mode="html")

    elif state == ShakalStates.AdminSetSherlockText:
        await update_config("sherlock_text", text)
        clear_state(user_id)
        await event.respond("✅ Шаблон текста для Шерлока изменен.")

    elif state == ShakalStates.AdminSetOtherText:
        await update_config("other_text", text)
        clear_state(user_id)
        await event.respond("✅ Шаблон текста для Других OSINT ботов изменен.")

    elif state == ShakalStates.AdminSetDepText:
        await update_config("depsearch_text", text)
        clear_state(user_id)
        await event.respond("✅ Шаблон текста для Depsearch изменен.")

    elif state == ShakalStates.WaitingForSherlock:
        target = text
        if not target.lower().endswith("bot"):
            await event.respond("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'. Попробуйте еще раз:")
            return
        clear_state(user_id)
        await process_sherlock(event, user_id, username, target)

    elif state == ShakalStates.WaitingForOtherBot:
        target = text
        if not target.lower().endswith("bot"):
            await event.respond("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'. Попробуйте еще раз:")
            return
        update_state_data(user_id, 'target_bot', target)
        set_state(user_id, ShakalStates.WaitingForOtherWord, get_state(user_id)['data'])
        await event.respond("Введите с какого слова начинается главное сообщение бота (для поиска):")

    elif state == ShakalStates.WaitingForOtherWord:
        word_trigger = text
        target = data['target_bot']
        clear_state(user_id)
        await process_other_bot_logic(event, user_id, username, target, word_trigger)

    elif state == ShakalStates.WaitingForDepsearchBot:
        target = text
        if not target.lower().endswith("bot"):
            await event.respond("❌ Ошибка! Юзернейм должен заканчиваться на 'bot'. Попробуйте еще раз:")
            return
        clear_state(user_id)
        refs = await get_referrals(user_id)
        if refs < 10:
            await event.respond("❌ Для использования Depsearch требуется минимум 10 рефералов.")
            return
        await process_depsearch_logic(event, user_id, username, target)

    # Замена сессий FSM
    elif state == ShakalStates.AdminSessionPhone:
        phone = text.replace(" ", "")
        update_state_data(user_id, 'phone', phone)
        sess_num = data['sess_num']
        sess_path = os.path.join(BASE_DIR, sess_num)

        temp_client_obj = clients.get(sess_num)
        if temp_client_obj:
            try: await temp_client_obj.disconnect()
            except: pass

        for ext in ['', '.journal', '.session']:
            path_to_rm = sess_path + ext
            if os.path.exists(path_to_rm):
                try: os.remove(path_to_rm)
                except: pass

        temp_client = TelegramClient(sess_path, API_ID, API_HASH)
        await temp_client.connect()

        try:
            send_code_res = await temp_client.send_code_request(phone)
            update_state_data(user_id, 'phone_code_hash', send_code_res.phone_code_hash)
            active_auth_clients[user_id] = temp_client
            set_state(user_id, ShakalStates.AdminSessionCode, get_state(user_id)['data'])
            await event.respond("Код отправлен. Пожалуйста, введите код, полученный от Telegram:")
        except Exception as e:
            await event.respond(f"❌ Ошибка отправки кода: {e}\nНачните авторизацию заново через /admin")
            try: await temp_client.disconnect()
            except: pass
            clear_state(user_id)

    elif state == ShakalStates.AdminSessionCode:
        code = text
        phone = data['phone']
        phone_code_hash = data['phone_code_hash']
        sess_num = data['sess_num']

        temp_client = active_auth_clients.get(user_id)
        if not temp_client:
            await event.respond("❌ Ошибка: Временная сессия потеряна. Начните заново через /admin")
            clear_state(user_id)
            return

        try:
            await temp_client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            await event.respond(f"✅ Сессия {sess_num}.session успешно создана и авторизована!")
            await restart_global_client(sess_num)
            active_auth_clients.pop(user_id, None)
            clear_state(user_id)
        except errors.SessionPasswordNeededError:
            set_state(user_id, ShakalStates.AdminSession2FA, data)
            await event.respond("🔒 На аккаунте включен облачный пароль (2FA). Пожалуйста, введите ваш пароль:")
        except Exception as e:
            await event.respond(f"❌ Ошибка авторизации: {e}\nНачните заново через /admin")
            try: await temp_client.disconnect()
            except: pass
            active_auth_clients.pop(user_id, None)
            clear_state(user_id)

    elif state == ShakalStates.AdminSession2FA:
        password = text
        sess_num = data['sess_num']
        temp_client = active_auth_clients.get(user_id)
        
        if not temp_client:
            await event.respond("❌ Ошибка: Временная сессия потеряна. Начните заново через /admin")
            clear_state(user_id)
            return

        try:
            await temp_client.sign_in(password=password)
            await event.respond(f"✅ Сессия {sess_num}.session успешно создана и авторизована с учетом 2FA!")
            await restart_global_client(sess_num)
            active_auth_clients.pop(user_id, None)
            clear_state(user_id)
        except Exception as e:
            await event.respond(f"❌ Неверный пароль или критическая ошибка: {e}\nНачните заново через /admin")
            try: await temp_client.disconnect()
            except: pass
            active_auth_clients.pop(user_id, None)
            clear_state(user_id)

# --- CALLBACK ХЭНДЛЕРЫ ---
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    if await is_banned(user_id): return
    
    data = event.data.decode('utf-8')
    sender = await event.get_sender()
    username = sender.username or "без юзера"

    if data == "read_broadcast":
        try: await event.delete()
        except: await event.answer("Не удалось удалить.", alert=True)
        return

    if data == "check_subscription":
        if await asyncio.to_thread(sync_is_subscribed, user_id) == 1:
            activated, referrer_id, remains = await asyncio.to_thread(sync_activate_sub, user_id)
            try: await event.delete()
            except: pass
            await send_shakal_photo(user_id, "<b>Успешно! Доступ открыт.</b>", buttons=get_main_keyboard())
            return

        channels = await asyncio.to_thread(sync_get_channels)
        for ch_id, url in channels:
            try:
                participant = await bot(GetParticipantRequest(channel=ch_id, participant=user_id))
            except errors.UserNotParticipantError:
                await event.answer("❌ Ты подписался не на все каналы!", alert=True)
                return
            except Exception:
                await event.answer("❌ Ошибка проверки подписки. Бот должен быть администратором канала.", alert=True)
                return

        activated, referrer_id, remains = await asyncio.to_thread(sync_activate_sub, user_id)
        if activated and referrer_id:
            if remains == 3:
                try: await bot.send_message(referrer_id, "🎉 Вы успешно пригласили 3 друзей и получили 3 запроса!")
                except: pass
            else:
                try: await bot.send_message(referrer_id, f"🔔 Новый реферал! Осталось до 3 пробных запросов: {remains}")
                except: pass

            try:
                owner_log = f"🌟 <b>Новый реферал!</b>\nУ кого: {referrer_id}\nОсталось до бонуса: {remains}\nНовый: @{username} (<code>{user_id}</code>)"
                for o in OWNER_IDS:
                    try: await bot.send_message(o, owner_log, parse_mode="html")
                    except: pass
            except: pass

        try: await event.delete()
        except: pass
        await send_shakal_photo(user_id, "<b>Успешно! Доступ открыт.</b>", buttons=get_main_keyboard())

    elif data == "menu_profile":
        me = await bot.get_me()
        bot_username = me.username
        username_db, req_count = await asyncio.to_thread(sync_get_profile, user_id)
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        
        profile_text = (
            f"<blockquote>┌\n├  Пользователь: @{username_db} | {user_id}\n├  Запpơсы: {req_count}\n└\n\n"
            f"┌\n├ Как получать запpơсы? \n├ Приглашайте друзей по ссылке:\n├ <code>{ref_link}</code> \n"
            f"├ За каждых 3 приглашенных друзей вы получите 3 запpơса!\n└</blockquote>"
        )
        await send_shakal_photo(user_id, profile_text, buttons=get_profile_keyboard())

    elif data == "profile_back":
        await send_shakal_photo(user_id, "<b>Главное меню:</b>", buttons=get_main_keyboard())

    elif data == "profile_promo":
        await event.respond("Введите название промокода для активации:")
        set_state(user_id, ShakalStates.PromoRedeem)

    elif data == "menu_shakal":
        now = time.time()
        if user_id in user_cooldowns and now < user_cooldowns[user_id]:
            remains = int(user_cooldowns[user_id] - now)
            await event.answer(f"❌ КД! Вы сможете отправить новый запpơс через {remains // 60} мин {remains % 60} сек.", alert=True)
            return

        requests = await asyncio.to_thread(sync_get_requests, user_id)
        if requests <= 0:
            await event.answer("❌ У вас 0 доступных запpơcов! Пригласите друзей.", alert=True)
            return

        kb_buttons = [
            [Button.inline("Шерлơк", b"shakal_sherlock")],
            [Button.inline("Другой ơсинт бот", b"shakal_other")]
        ]

        refs = await get_referrals(user_id)
        if refs >= 10:
            kb_buttons.append([Button.inline("Depsearch", b"shakal_depsearch")])
        else:
            kb_buttons.append([Button.inline(f"Depsearch (требуется 10 реф., у вас {refs})", b"shakal_depsearch_locked")])

        await send_shakal_photo(user_id, "<b>Выберите категорию цели для уничтожения:</b>", buttons=kb_buttons)

    elif data == "shakal_depsearch_locked":
        await event.answer("❌ Для использования Deрseaṛch требуется 10 приглашенных рефералов.", alert=True)

    elif data == "shakal_sherlock":
        await event.respond("Введите юзернейм бота (например, @sherlock_bot):")
        set_state(user_id, ShakalStates.WaitingForSherlock)

    elif data == "shakal_other":
        await event.respond("Введите юзернейм бота (например, @osint_bot):")
        set_state(user_id, ShakalStates.WaitingForOtherBot)

    elif data == "shakal_depsearch":
        await event.respond("Укажите ссылку/юзернейм бота Depsearch (например, @DepsearchBot):")
        set_state(user_id, ShakalStates.WaitingForDepsearchBot)

    elif data.startswith("admin_"):
        if data == "admin_broadcast":
            await event.respond("Отправьте сообщение для рассылки всем пользователям (к тексту можно прикрепить фото/видео):")
            set_state(user_id, ShakalStates.AdminBroadcast)
        elif data == "admin_ban":
            await event.respond("Введите ID пользователя для вечной блокировки в боте:")
            set_state(user_id, ShakalStates.AdminBan)
        elif data == "admin_give":
            await event.respond("Введите ID пользователя, которому выдать запросы:")
            set_state(user_id, ShakalStates.AdminGiveID)
        elif data == "admin_set_sherlock":
            current_txt = await get_config('sherlock_text')
            await event.respond(f"Текущий текст:\n<i>{current_txt}</i>\n\nВведите новый default текст для Шерлока:", parse_mode="html")
            set_state(user_id, ShakalStates.AdminSetSherlockText)
        elif data == "admin_set_other":
            current_txt = await get_config('other_text')
            await event.respond(f"Текущий текст:\n<i>{current_txt}</i>\n\nВведите новый default текст для Других ботов:", parse_mode="html")
            set_state(user_id, ShakalStates.AdminSetOtherText)
        elif data == "admin_set_dep":
            current_txt = await get_config('depsearch_text')
            await event.respond(f"Текущий текст:\n<i>{current_txt}</i>\n\nВведите новый default текст для Depsearch:", parse_mode="html")
            set_state(user_id, ShakalStates.AdminSetDepText)
        elif data == "admin_change_session":
            kb = [
                [Button.inline("sherlock.session", b"change_sess_sherlock")],
                [Button.inline("osint.session", b"change_sess_osint")],
                [Button.inline("sherlock3.session", b"change_sess_sherlock3")],
                [Button.inline("depsearch.session", b"change_sess_depsearch")]
            ]
            await event.respond("Выберите сессию которую нужно заменить:", buttons=kb)

    elif data.startswith("change_sess_"):
        sess_key = data.replace("change_sess_", "")
        set_state(user_id, ShakalStates.AdminSessionPhone, {"sess_num": sess_key})
        await event.respond("Введите номер телефона для этой сессии (в международном формате, например +79991234567):")

# --- ЛОГИКА АТАКИ ---
async def process_sherlock(event, user_id, username, target):
    global last_global_report_time
    async with global_report_lock:
        now = time.time()
        if now - last_global_report_time < 300:
            wait_time = 300 - (now - last_global_report_time)
            wait_msg = await event.respond(f"⏳ Очередь занята. Ваш запpơс добавлен в очередь и начнется автоматически через {int(wait_time)} сек...")
            await asyncio.sleep(wait_time)
            try: await wait_msg.delete()
            except: pass

        user_cooldowns[user_id] = time.time() + 15 * 60
        await asyncio.to_thread(sync_decrement_requests, user_id)

        status_msg = await event.respond("🚀 [1/3] Отправка.")
        comment_text = await get_config("sherlock_text")
        success_cnt, fail_cnt = 0, 0
        clients_to_use = [clients["sherlock"], clients["sherlock3"]]

        try:
            for cl in clients_to_use:
                try:
                    await cl.send_message(target, "/start")
                    await asyncio.sleep(1.0)
                except: pass
            await asyncio.sleep(1.5)

            await status_msg.edit("🚀 [2/3] Отправка на профиль. ")
            for cl in clients_to_use:
                for _ in range(4):
                    try:
                        await cl(functions.account.ReportPeerRequest(
                            peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        success_cnt += 1
                        await asyncio.sleep(1.2)
                    except: fail_cnt += 1

            await status_msg.edit("🔍 [3/3] Поиск целевого сообщения. ..")
            msg_reported = False
            try:
                messages = await clients["sherlock"].get_messages(target, limit=50)
                target_msg_id = None
                third_msg_id = None
                bot_count = 0

                for msg in messages:
                    if not msg.out and msg.message:
                        bot_count += 1
                        if str(msg.message).startswith("ℹ️ Примеры") or str(msg.message).startswith("«Scalp»"):
                            target_msg_id = msg.id
                            break
                        if bot_count == 3:
                            third_msg_id = msg.id

                if not target_msg_id and third_msg_id:
                    target_msg_id = third_msg_id

                if target_msg_id:
                    for cl in clients_to_use:
                        for _ in range(4):
                            try:
                                await cl(functions.messages.ReportRequest(
                                    peer=target, id=[target_msg_id], reason=InputReportReasonPersonalDetails(), message=comment_text
                                ))
                                success_cnt += 1
                                await asyncio.sleep(1.2)
                            except: fail_cnt += 1
                    msg_reported = True
            except: pass

            if not msg_reported:
                await status_msg.edit("⚠️ Сообщение не найдено. Досылаем финальные жąлơбы на прơфиль...")
                for cl in clients_to_use:
                    for _ in range(4):
                        try:
                            await cl(functions.account.ReportPeerRequest(
                                peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                            ))
                            success_cnt += 1
                            await asyncio.sleep(1.2)
                        except: fail_cnt += 1

            for cl in clients_to_use:
                try: await cl(BlockRequest(id=target))
                except: pass

            try: await status_msg.delete()
            except: pass

            await send_shakal_photo(user_id, f"✅ <b>Шакализатор успешно отправлен на {target}! Бот шакализирован аккаунтами.</b>", buttons=get_main_keyboard())

            try:
                log_txt = f"⚡️ <b>Успешно отправлено на:</b> {target}\n👤 <b>Юзер:</b> @{username}\n📝 <b>Тип:</b> Шерлок (sherlock + sherlock3)\n✅ {success_cnt} | ❌ {fail_cnt}"
                for o in OWNER_IDS:
                    try: await bot.send_message(o, log_txt, parse_mode="html")
                    except: pass
            except: pass

        except Exception as e:
            await event.respond(f"❌ Ошибка выполнения: {e}")

        last_global_report_time = time.time()

async def process_other_bot_logic(event, user_id, username, target, word_trigger):
    global last_global_report_time
    async with global_report_lock:
        now = time.time()
        if now - last_global_report_time < 300:
            wait_time = 300 - (now - last_global_report_time)
            wait_msg = await event.respond(f"⏳ Очередь занята. Ваш запрос добавлен в очередь и начнется автоматически через {int(wait_time)} сек...")
            await asyncio.sleep(wait_time)
            try: await wait_msg.delete()
            except: pass

        user_cooldowns[user_id] = time.time() + 15 * 60
        await asyncio.to_thread(sync_decrement_requests, user_id)

        status_msg = await event.respond("🚀 [1/3] Отправка команды...")
        comment_text = await get_config("other_text")
        success_cnt, fail_cnt = 0, 0

        try:
            try:
                await clients["osint"].send_message(target, "/start")
                await asyncio.sleep(2.5)
            except: pass

            await status_msg.edit("🚀 [2/3] Отправка на профиль...")
            for _ in range(4):
                try:
                    await clients["osint"](functions.account.ReportPeerRequest(
                        peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                    ))
                    success_cnt += 1
                    await asyncio.sleep(1.2)
                except: fail_cnt += 1

            await status_msg.edit(f"🔍 [3/3] Поиск сообщения, начинающегося на слово: '{word_trigger}'...")
            msg_reported = False
            try:
                messages = await clients["osint"].get_messages(target, limit=50)
                target_msg = None
                third_msg = None
                bot_count = 0

                for msg in messages:
                    if not msg.out and msg.message:
                        bot_count += 1
                        if msg.message.lower().startswith(word_trigger.lower()):
                            target_msg = msg
                            break
                        if bot_count == 3:
                            third_msg = msg

                if not target_msg and third_msg:
                    target_msg = third_msg

                if target_msg:
                    for _ in range(4):
                        try:
                            await clients["osint"](functions.messages.ReportRequest(
                                peer=target, id=[target_msg.id], reason=InputReportReasonPersonalDetails(), message=comment_text
                            ))
                            success_cnt += 1
                            await asyncio.sleep(1.2)
                        except: fail_cnt += 1
                    msg_reported = True
            except: pass

            if not msg_reported:
                await status_msg.edit("⚠️ Сообщение не найдено. Досылаем финальные...")
                for _ in range(4):
                    try:
                        await clients["osint"](functions.account.ReportPeerRequest(
                            peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        success_cnt += 1
                        await asyncio.sleep(1.2)
                    except: fail_cnt += 1

            try: await status_msg.delete()
            except: pass

            await send_shakal_photo(user_id, f"✅ <b>Шакализатор успешно отправлен на {target}!</b>", buttons=get_main_keyboard())

            try:
                log_txt = f"⚡️ <b>Успешно отправлено на:</b> {target}\n👤 <b>Юзер:</b> @{username}\n📝 <b>Тип:</b> Other OSINT\n✅ {success_cnt} | ❌ {fail_cnt}"
                for o in OWNER_IDS:
                    try: await bot.send_message(o, log_txt, parse_mode="html")
                    except: pass
            except: pass

        except Exception as e:
            await event.respond(f"❌ Произошла критическая ошибка сессии osint: {e}")

        last_global_report_time = time.time()

async def process_depsearch_logic(event, user_id, username, target):
    global last_global_report_time
    async with global_report_lock:
        now = time.time()
        if now - last_global_report_time < 300:
            wait_time = 300 - (now - last_global_report_time)
            wait_msg = await event.respond(f"⏳ Очередь занята. Ваш запрос начнётся через {int(wait_time)} сек...")
            await asyncio.sleep(wait_time)
            try: await wait_msg.delete()
            except: pass

        user_cooldowns[user_id] = time.time() + 15 * 60
        await asyncio.to_thread(sync_decrement_requests, user_id)

        status_msg = await event.respond("🚀 [1/3] Отправка команды...")
        comment_text = await get_config("depsearch_text")
        success_cnt, fail_cnt = 0, 0

        try:
            try:
                await clients["depsearch"].send_message(target, "/start")
                await asyncio.sleep(1.0)
            except: pass

            await status_msg.edit("🔍 Поиск кнопки...")
            clicked = False
            try:
                messages = await clients["depsearch"].get_messages(target, limit=20)
                for msg in messages:
                    if msg.buttons:
                        rows = msg.buttons if isinstance(msg.buttons[0], list) else [msg.buttons]
                        for row in rows:
                            for btn in row:
                                data = getattr(btn, 'data', None)
                                try:
                                    if data and (data == b"search" or (isinstance(data, bytes) and b"search" in data) or (isinstance(data, str) and "search" in data)):
                                        await clients["depsearch"](functions.messages.GetBotCallbackAnswerRequest(
                                            peer=target, msg_id=msg.id, data=data
                                        ))
                                        clicked = True
                                        break
                                except: pass
                            if clicked: break
                    if clicked: break
            except: pass

            await asyncio.sleep(0.8)
            await status_msg.edit("🔍 Поиск большого результата...")
            target_msg = None
            try:
                messages = await clients["depsearch"].get_messages(target, limit=50)
                bot_count = 0
                third_msg = None
                for msg in messages:
                    if not msg.out and msg.message:
                        bot_count += 1
                        if len(str(msg.message)) > 100:
                            target_msg = msg
                            break
                        if bot_count == 3:
                            third_msg = msg
                if not target_msg and third_msg:
                    target_msg = third_msg

                if target_msg:
                    await status_msg.edit("🚀 Отправка шакализатора на сообщение (message reports)...")
                    for _ in range(4):
                        try:
                            await clients["depsearch"](functions.messages.ReportRequest(
                                peer=target, id=[target_msg.id], reason=InputReportReasonPersonalDetails(), message=comment_text
                            ))
                            success_cnt += 1
                            await asyncio.sleep(0.5)
                        except: fail_cnt += 1

                await status_msg.edit("🚀 Отправка шакализатора на профиль (peer reports)...")
                for _ in range(4):
                    try:
                        await clients["depsearch"](functions.account.ReportPeerRequest(
                            peer=target, reason=InputReportReasonPersonalDetails(), message=comment_text
                        ))
                        success_cnt += 1
                        await asyncio.sleep(0.5)
                    except: fail_cnt += 1
            except: pass

            try: await status_msg.delete()
            except: pass

            await send_shakal_photo(user_id, f"✅ <b>Depsearch: шакализатор успешно отправлены на {target}!</b>", buttons=get_main_keyboard())

            try:
                log_txt = f"⚡️ <b>Depsearch отправлено на:</b> {target}\n👤 <b>Юзер:</b> @{username}\n📝 <b>Тип:</b> Depsearch\n✅ {success_cnt} | ❌ {fail_cnt}"
                for o in OWNER_IDS:
                    try: await bot.send_message(o, log_txt, parse_mode="html")
                    except: pass
            except: pass

        except Exception as e:
            await event.respond(f"❌ Произошла критическая ошибка сессии depsearch: {e}")

        last_global_report_time = time.time()

# --- ЗАПУСК ---
async def main():
    await asyncio.to_thread(sync_init_db)
    logging.info("Подключение сессий Telethon (bot, sherlock, osint, sherlock3, depsearch)...")

    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logging.info(f"Бот авторизован: @{me.username} ({me.id})")

    for key, cl in clients.items():
        try:
            await cl.connect()
            logging.info(f"Сессия {key} успешно подключена!")
        except Exception as e:
            logging.error(f"❌ Ошибка Сессии {key} (возможно мертва): {e}. Бот продолжит работу, замените сессию через /admin.")

    logging.info("Все сессии обработаны. Бот слушает обновления...")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
