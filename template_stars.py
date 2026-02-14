import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ========== КОНФИГУРАЦИЯ (может быть переопределена при регистрации) ==========
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003326584722")
WITHDRAWAL_CHANNEL_ID = os.getenv("WITHDRAWAL_CHANNEL", "-1003891414947")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@Nft_top3")
PORT = int(os.environ.get("PORT", 10000))

REF_REWARD = 5.0
VIEW_REWARD = 0.3
DAILY_MIN, DAILY_MAX = 1, 3
LUCK_MIN, LUCK_MAX = 0, 5
LUCK_COOLDOWN = 6 * 60 * 60
WITHDRAWAL_OPTIONS = [15, 25, 50, 100]

GIFTS_PRICES = {
    "🧸 Мишка": 45, "❤️ Сердце": 45,
    "🎁 Подарок": 75, "🌹 Роза": 75,
    "🍰 Тортик": 150, "💐 Букет": 150, "🚀 Ракета": 150, "🍾 Шампанское": 150,
    "🏆 Кубок": 300, "💍 Колечко": 300, "💎 Алмаз": 300
}

SPECIAL_ITEMS = {
    "Ramen": {"price": 250, "limit": 25, "full_name": "🍜 Ramen"},
    "Candle": {"price": 199, "limit": 30, "full_name": "🕯 B-Day Candle"},
    "Calendar": {"price": 320, "limit": 18, "full_name": "🗓 Desk Calendar"}
}

ITEMS_PER_PAGE = 5

# ========== КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ШАБЛОНА ==========
class TemplateDatabase:
    def __init__(self, bot_id: int):
        self.db_path = f"stars_template_{bot_id}.db"
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            conn.execute("DROP TABLE IF EXISTS marketplace")
            conn.execute("""CREATE TABLE IF NOT EXISTS marketplace (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER,
                item_name TEXT,
                price REAL
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                stars REAL DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                last_daily TIMESTAMP,
                last_luck TIMESTAMP,
                ref_code TEXT UNIQUE,
                ref_boost REAL DEFAULT 1.0,
                is_active INTEGER DEFAULT 0,
                total_earned REAL DEFAULT 0,
                referred_by INTEGER
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER,
                item_name TEXT,
                quantity INTEGER DEFAULT 1,
                PRIMARY KEY(user_id, item_name)
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS lottery (
                id INTEGER PRIMARY KEY,
                pool REAL DEFAULT 0,
                participants TEXT DEFAULT ''
            )""")
            conn.execute("INSERT OR IGNORE INTO lottery (id, pool, participants) VALUES (1, 0, '')")

            conn.execute("""CREATE TABLE IF NOT EXISTS lottery_history (
                user_id INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS task_claims (
                user_id INTEGER,
                task_id TEXT,
                PRIMARY KEY(user_id, task_id)
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS promo (
                code TEXT PRIMARY KEY,
                reward_type TEXT,
                reward_value TEXT,
                uses INTEGER
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS promo_history (
                user_id INTEGER,
                code TEXT,
                PRIMARY KEY(user_id, code)
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS daily_bonus (
                user_id INTEGER PRIMARY KEY,
                last_date TEXT,
                streak INTEGER DEFAULT 0
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS active_duels (
                creator_id INTEGER PRIMARY KEY,
                amount REAL
            )""")
            conn.commit()

    def get_user(self, user_id: int):
        with self.get_connection() as conn:
            return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def create_user(self, user_id, username, first_name):
        with self.get_connection() as conn:
            ref_code = f"ref{user_id}"
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, ref_code) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, ref_code)
            )
            conn.commit()

    def add_stars(self, user_id, amount):
        with self.get_connection() as conn:
            if amount > 0:
                user = self.get_user(user_id)
                boost = user['ref_boost'] if user and 'ref_boost' in user.keys() else 1.0
                amount = float(amount) * boost
                conn.execute("UPDATE users SET stars = stars + ? WHERE user_id = ?", (amount, user_id))
                conn.commit()
            else:
                conn.execute("UPDATE users SET stars = stars + ? WHERE user_id = ?", (amount, user_id))
                conn.commit()

    # Добавим остальные методы по мере необходимости, но пока оставим так.

# ========== СОСТОЯНИЯ FSM ==========
class AdminStates(StatesGroup):
    waiting_fake_name = State()
    waiting_give_data = State()
    waiting_broadcast_msg = State()
    waiting_channel_post = State()
    waiting_promo_data = State()

class PromoStates(StatesGroup):
    waiting_for_code = State()

class P2PSaleStates(StatesGroup):
    waiting_for_price = State()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def mask_name(name):
    if not name:
        return "User****"
    name = name.replace("@", "")
    return name[:3] + "****" if len(name) > 3 else name + "****"

def generate_fake_id():
    return "".join([str(random.randint(0, 9)) for _ in range(10)])

def generate_fake_user():
    prefixes = ["Kripto", "Star", "Rich", "Trader", "Money", "Lucky", "Alex", "Dmitry", "Zevs"]
    suffixes = ["_top", "777", "X", "_pro", "King", "Off", "Master"]
    return random.choice(prefixes) + random.choice(suffixes)

# ========== ФУНКЦИЯ РЕГИСТРАЦИИ ШАБЛОНА ==========
async def register_template_handlers(dp: Dispatcher, bot: Bot, admin_ids: List[int]):
    router = Router()

    # Создаём экземпляр базы данных для этого бота
    # В реальном проекте bot_id нужно передать, но здесь нет bot_id, можно использовать id бота или хэш токена
    # Упрощённо: используем фиксированное имя файла
    db = TemplateDatabase(bot_id=hash(bot.token) % 10000)

    # ------------------------------------------------------------------
    # ХЕНДЛЕРЫ (все используют bot, db, admin_ids через замыкание)
    # ------------------------------------------------------------------

    # --- СТАРТ ---
    @router.message(CommandStart())
    async def cmd_start(message: Message):
        # Вотермарка
        await message.answer("⚒️ Бот создан с помощью @KneoFreeBot")

        args = message.text.split()
        if len(args) > 1 and args[1].startswith("duel"):
            creator_id = int(args[1].replace("duel", ""))
            if creator_id != message.from_user.id:
                kb = InlineKeyboardBuilder().row(
                    InlineKeyboardButton(text="🤝 Принять вызов (5.0 ⭐)", callback_data=f"accept_duel_{creator_id}"),
                    InlineKeyboardButton(text="❌ Отказ", callback_data="menu")
                )
                await message.answer(f"⚔️ Игрок ID:{creator_id} вызывает тебя на дуэль!", reply_markup=kb.as_markup())
                return

        uid = message.from_user.id
        if not db.get_user(uid):
            db.create_user(uid, message.from_user.username, message.from_user.first_name)
            if " " in message.text:
                ref_part = message.text.split()[1]
                if ref_part.startswith("ref"):
                    ref_id = int(ref_part.replace("ref", ""))
                    if ref_id != uid:
                        with db.get_connection() as conn:
                            conn.execute("UPDATE users SET referrals = referrals + 1 WHERE user_id = ?", (ref_id,))
                            conn.commit()
                        try:
                            await bot.send_message(ref_id, "👥 У вас новый реферал! Вы получите 5 ⭐, когда он заработает свои первые 1.0 ⭐.")
                        except:
                            pass

        text = (
            f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
            "💎 <b>StarsForQuestion</b> — это место, где твоя активность превращается в Звезды.\n\n"
            "🎯 Выполняй задания, крути удачу и забирай подарки!"
        )
        await message.answer(text, reply_markup=get_main_kb(uid))

    # --- ФУНКЦИЯ ДОБАВЛЕНИЯ ЗВЁЗД (используется внутри) ---
    def add_stars_secure(user_id, amount, is_task=False):
        db.add_stars(user_id, amount)
        if amount > 0:
            with db.get_connection() as conn:
                conn.execute("UPDATE users SET total_earned = total_earned + ? WHERE user_id = ?", (amount, user_id))
                user = db.get_user(user_id)
                if user['total_earned'] >= 1.0 and user['is_active'] == 0:
                    conn.execute("UPDATE users SET is_active = 1 WHERE user_id = ?", (user_id,))
                    conn.commit()

    # --- ЕЖЕДНЕВНЫЙ БОНУС ---
    @router.callback_query(F.data == "daily_bonus")
    async def cb_daily_bonus(call: CallbackQuery):
        await call.answer()
        uid = call.from_user.id
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        with db.get_connection() as conn:
            data = conn.execute("SELECT last_date, streak FROM daily_bonus WHERE user_id = ?", (uid,)).fetchone()

            if data:
                last_date = datetime.strptime(data['last_date'], "%Y-%m-%d")
                delta = (now.date() - last_date.date()).days
                if delta == 0:
                    await call.answer("❌ Бонус уже получен! Приходи завтра.", show_alert=True)
                    return
                elif delta == 1:
                    new_streak = min(data['streak'] + 1, 7)
                else:
                    new_streak = 1
                conn.execute("UPDATE daily_bonus SET last_date = ?, streak = ? WHERE user_id = ?", (today_str, new_streak, uid))
            else:
                new_streak = 1
                conn.execute("INSERT INTO daily_bonus (user_id, last_date, streak) VALUES (?, ?, ?)", (uid, today_str, new_streak))
            conn.commit()

        reward = round(0.1 * new_streak, 2)
        db.add_stars(uid, reward)
        await call.answer(f"✅ День {new_streak}! Получено: {reward} ⭐", show_alert=True)

    # --- ДУЭЛИ ---
    @router.callback_query(F.data == "duel_menu")
    async def cb_duel_menu(call: CallbackQuery):
        await call.answer()
        uid = call.from_user.id
        bot_username = (await bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=duel{uid}"

        text = (
            "⚔️ <b>ДУЭЛЬНЫЙ КЛУБ</b>\n━━━━━━━━━━━━━━\n"
            "Ставка: <b>5.0 ⭐</b>\n"
            "Победитель получает: <b>9.0 ⭐</b>\n\n"
            "Отправь ссылку другу, чтобы вызвать его на бой:"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📨 Скинуть ссылку другу", switch_inline_query=link))
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))

        await call.message.edit_text(f"{text}\n<code>{link}</code>", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("accept_duel_"))
    async def cb_accept_duel(call: CallbackQuery):
        await call.answer()
        opponent_id = call.from_user.id
        creator_id = int(call.data.split("_")[2])

        if opponent_id == creator_id:
            await call.answer("❌ Нельзя играть с самим собой!", show_alert=True)
            return

        user = db.get_user(opponent_id)
        if user['stars'] < 5.0:
            await call.answer("❌ Недостаточно ⭐ для ставки!", show_alert=True)
            return

        db.add_stars(opponent_id, -5.0)

        msg = await call.message.answer("🎲 Бросаем кости...")
        dice = await msg.answer_dice("🎲")
        await asyncio.sleep(3.5)

        winner_id = creator_id if dice.dice.value <= 3 else opponent_id
        db.add_stars(winner_id, 9.0)

        await call.message.answer(
            f"🎰 Выпало <b>{dice.dice.value}</b>!\n"
            f"👑 Победитель: <a href='tg://user?id={winner_id}'>Игрок</a>\n"
            f"Зачислено: <b>9.0 ⭐</b>"
        )

    # --- ЛОТЕРЕЯ ---
    @router.callback_query(F.data == "lottery")
    async def cb_lottery(call: CallbackQuery):
        await call.answer()
        with db.get_connection() as conn:
            data = conn.execute("SELECT pool, participants FROM lottery WHERE id = 1").fetchone()

        count = len(data['participants'].split(',')) if data['participants'] else 0
        text = (
            "🎟 <b>ЗВЕЗДНАЯ ЛОТЕРЕЯ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💰 Текущий банк: <b>{data['pool']:.2f} ⭐</b>\n"
            f"👥 Участников: <b>{count}</b>\n"
            f"🎫 Цена билета: <b>2.0 ⭐</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Победитель забирает 80% банка. Розыгрыш происходит автоматически!</i>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💎 Купить билет", callback_data="buy_ticket"))
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
        await call.message.edit_text(text, reply_markup=kb.as_markup())

    @router.callback_query(F.data == "buy_ticket")
    async def cb_buy_ticket(call: CallbackQuery):
        await call.answer()
        uid = call.from_user.id
        user = db.get_user(uid)
        if user['stars'] < 2:
            await call.answer("❌ Недостаточно звезд (нужно 2.0)", show_alert=True)
            return

        db.add_stars(uid, -2)
        with db.get_connection() as conn:
            conn.execute("UPDATE lottery SET pool = pool + 2, participants = participants || ? WHERE id = 1", (f"{uid},",))
            conn.commit()

        await call.message.answer(
            f"🎟 <b>Билет №{random.randint(1000, 9999)} успешно куплен!</b>\n\n"
            "Твой шанс на победу вырос! Следи за каналом выплат."
        )
        await cb_lottery(call)

    @router.callback_query(F.data == "menu")
    async def cb_menu(call: CallbackQuery):
        await call.answer()
        await call.message.edit_text("⭐ <b>Главное меню</b>", reply_markup=get_main_kb(call.from_user.id))

    @router.callback_query(F.data == "profile")
    async def cb_profile(call: CallbackQuery):
        await call.answer()
        u = db.get_user(call.from_user.id)
        await call.message.edit_text(
            f"👤 <b>Профиль</b>\n\n"
            f"🆔 ID: <code>{u['user_id']}</code>\n"
            f"⭐ Баланс: <b>{u['stars']:.2f} ⭐</b>\n"
            f"👥 Рефералов: {u['referrals']}",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu")).as_markup()
        )

    @router.callback_query(F.data == "referrals")
    async def cb_referrals(call: CallbackQuery):
        await call.answer()
        u = db.get_user(call.from_user.id)
        bot_username = (await bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={u['ref_code']}"
        await call.message.edit_text(
            f"👥 <b>Рефералы</b>\n\nЗа друга: <b>{REF_REWARD} ⭐</b>\n\n🔗 Ссылка:\n<code>{ref_link}</code>",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu")).as_markup()
        )

    @router.callback_query(F.data == "daily")
    async def cb_daily(call: CallbackQuery):
        await call.answer()
        u = db.get_user(call.from_user.id)
        now = datetime.now()
        if u['last_daily'] and (now - datetime.fromisoformat(u['last_daily'])).days < 1:
            await call.answer("⏳ Только раз в день!", show_alert=True)
            return
        rew = random.randint(DAILY_MIN, DAILY_MAX)
        db.add_stars(call.from_user.id, rew)
        with db.get_connection() as conn:
            conn.execute("UPDATE users SET last_daily = ? WHERE user_id = ?", (now.isoformat(), call.from_user.id))
            conn.commit()
        await call.answer(f"🎁 +{rew} ⭐", show_alert=True)
        await call.message.edit_text("⭐ <b>Главное меню</b>", reply_markup=get_main_kb(call.from_user.id))

    @router.callback_query(F.data == "luck")
    async def cb_luck(call: CallbackQuery):
        await call.answer()
        u = db.get_user(call.from_user.id)
        now = datetime.now()
        if u['last_luck'] and (now - datetime.fromisoformat(u['last_luck'])).total_seconds() < LUCK_COOLDOWN:
            await call.answer("⏳ Кулдаун 6 часов!", show_alert=True)
            return
        win = random.randint(LUCK_MIN, LUCK_MAX)
        db.add_stars(call.from_user.id, win)
        with db.get_connection() as conn:
            conn.execute("UPDATE users SET last_luck = ? WHERE user_id = ?", (now.isoformat(), call.from_user.id))
            conn.commit()
        await call.answer(f"🎰 +{win} ⭐", show_alert=True)
        await call.message.edit_text("⭐ <b>Главное меню</b>", reply_markup=get_main_kb(call.from_user.id))

    @router.callback_query(F.data == "tasks")
    async def cb_tasks(call: CallbackQuery):
        await call.answer()
        uid = call.from_user.id
        with db.get_connection() as conn:
            active_refs = conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE referred_by = ? AND total_earned >= 1.0",
                (uid,)
            ).fetchone()['cnt']
            tickets_bought = conn.execute(
                "SELECT COUNT(*) as cnt FROM lottery_history WHERE user_id = ?",
                (uid,)
            ).fetchone()['cnt']

        kb = InlineKeyboardBuilder()
        status1 = "✅ Готово" if active_refs >= 3 else f"⏳ {active_refs}/3"
        kb.row(InlineKeyboardButton(text=f"📈 Стахановец: {status1}", callback_data="claim_task_1"))
        status2 = "✅ Готово" if tickets_bought >= 5 else f"⏳ {tickets_bought}/5"
        kb.row(InlineKeyboardButton(text=f"🎰 Ловец удачи: {status2}", callback_data="claim_task_2"))
        kb.row(InlineKeyboardButton(text="📸 Отправить видео-отзыв (100 ⭐)", url="https://t.me/Nft_top3"))
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))

        text = (
            "🎯 <b>ЗАДАНИЯ И КВЕСТЫ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💰 Забирай награды за активность!\n"
            "Награды начисляются моментально."
        )
        await call.message.edit_text(text, reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("claim_task_"))
    async def claim_task(call: CallbackQuery):
        await call.answer()
        task_num = call.data.split("_")[2]
        uid = call.from_user.id

        with db.get_connection() as conn:
            check = conn.execute(
                "SELECT 1 FROM task_claims WHERE user_id = ? AND task_id = ?",
                (uid, task_num)
            ).fetchone()
            if check:
                await call.answer("❌ Вы уже получили награду за этот квест!", show_alert=True)
                return

            if task_num == "1":
                count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM users WHERE referred_by = ? AND total_earned >= 1.0",
                    (uid,)
                ).fetchone()['cnt']
                if count < 3:
                    await call.answer("❌ Нужно 3 активных реферала!", show_alert=True)
                    return
                reward = 15.0
            elif task_num == "2":
                count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM lottery_history WHERE user_id = ?",
                    (uid,)
                ).fetchone()['cnt']
                if count < 5:
                    await call.answer("❌ Нужно купить еще билетов!", show_alert=True)
                    return
                reward = 3.0
            else:
                return

            conn.execute("INSERT INTO task_claims (user_id, task_id) VALUES (?, ?)", (uid, task_num))
            conn.commit()
            db.add_stars(uid, reward)

        await call.answer(f"✅ Начислено {reward} ⭐!", show_alert=True)
        await cb_tasks(call)

    @router.callback_query(F.data == "top")
    async def cb_top(call: CallbackQuery):
        await call.answer()
        with db.get_connection() as conn:
            rows = conn.execute("SELECT first_name, stars FROM users ORDER BY stars DESC LIMIT 10").fetchall()

        text = "🏆 <b>ТОП-10 МАГНАТОВ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for i, row in enumerate(rows, 1):
            name = row['first_name'][:3] + "***"
            text += f"{i}. {name} — <b>{row['stars']:.1f} ⭐</b>\n"

        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
        await call.message.edit_text(text, reply_markup=kb.as_markup())

    @router.callback_query(F.data == "help")
    async def cb_help(call: CallbackQuery):
        await call.answer()
        await call.message.edit_text(
            f"🆘 <b>ПОМОЩЬ</b>\n\nПоддержка: {SUPPORT_USERNAME}",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu")).as_markup()
        )

    # --- ВЫВОД СРЕДСТВ ---
    @router.callback_query(F.data == "withdraw")
    async def cb_withdraw_select(call: CallbackQuery):
        await call.answer()
        u = db.get_user(call.from_user.id)
        if u['stars'] < 15:
            await call.answer("❌ Минимум 15 ⭐", show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        for opt in WITHDRAWAL_OPTIONS:
            if u['stars'] >= opt:
                kb.row(InlineKeyboardButton(text=f"💎 {opt} ⭐", callback_data=f"wd_run_{opt}"))
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
        await call.message.edit_text("Выберите сумму:", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("wd_run_"))
    async def cb_wd_execute(call: CallbackQuery):
        await call.answer()
        amt = float(call.data.split("_")[2])
        uid = call.from_user.id
        if db.get_user(uid)['stars'] >= amt:
            db.add_stars(uid, -amt)
            name = mask_name(call.from_user.username or call.from_user.first_name)
            await bot.send_message(
                WITHDRAWAL_CHANNEL_ID,
                f"📥 <b>НОВАЯ ЗАЯВКА</b>\n\n👤 Юзер: @{name}\n🆔 ID: <code>{uid}</code>\n💎 Сумма: <b>{amt} ⭐</b>",
                reply_markup=get_admin_decision_kb(uid, amt)
            )
            await call.message.edit_text("✅ Заявка отправлена!", reply_markup=get_main_kb(uid))
        else:
            await call.answer("Ошибка баланса!")

    # --- АДМИН ПАНЕЛЬ ---
    @router.callback_query(F.data == "admin_panel")
    async def cb_admin_panel(call: CallbackQuery):
        await call.answer()
        if call.from_user.id not in admin_ids:
            return
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="📢 Рассылка", callback_data="a_broadcast"),
            InlineKeyboardButton(text="🎁 Создать Промо", callback_data="a_create_promo")
        )
        kb.row(
            InlineKeyboardButton(text="📢 Пост в КАНАЛ", callback_data="a_post_chan"),
            InlineKeyboardButton(text="🎭 Фейк Заявка", callback_data="a_fake_gen")
        )
        kb.row(
            InlineKeyboardButton(text="💎 Выдать ⭐", callback_data="a_give_stars"),
            InlineKeyboardButton(text="⛔ Стоп Лотерея 🎰", callback_data="a_run_lottery")
        )
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
        await call.message.edit_text("👑 <b>АДМИН-МЕНЮ</b>", reply_markup=kb.as_markup())

    @router.callback_query(F.data == "a_run_lottery")
    async def adm_run_lottery(call: CallbackQuery):
        await call.answer()
        if call.from_user.id not in admin_ids:
            return

        with db.get_connection() as conn:
            data = conn.execute("SELECT pool, participants FROM lottery WHERE id = 1").fetchone()
            if not data or not data['participants']:
                await call.answer("❌ Нет участников!", show_alert=True)
                return

            participants = [p for p in data['participants'].split(',') if p]
            winner_id = int(random.choice(participants))
            win_amount = data['pool'] * 0.8

            conn.execute("UPDATE lottery SET pool = 0, participants = '' WHERE id = 1")
            conn.commit()

        db.add_stars(winner_id, win_amount)

        await bot.send_message(winner_id, f"🥳 <b>ПОЗДРАВЛЯЕМ!</b>\nВы выиграли в лотерее: <b>{win_amount:.2f} ⭐</b>")
        await call.message.answer(f"✅ Лотерея завершена! Победитель: {winner_id}, Сумма: {win_amount}")

    @router.callback_query(F.data == "a_broadcast")
    async def adm_broadcast_start(call: CallbackQuery, state: FSMContext):
        await call.answer()
        if call.from_user.id not in admin_ids:
            return
        await state.set_state(AdminStates.waiting_broadcast_msg)
        await call.message.edit_text(
            "📢 <b>РАССЫЛКА ПОЛЬЗОВАТЕЛЯМ</b>\n\n"
            "Отправьте сообщение (текст, фото, видео), которое хотите разослать всем.",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")).as_markup()
        )

    @router.message(AdminStates.waiting_broadcast_msg)
    async def adm_broadcast_confirm(message: types.Message, state: FSMContext):
        await state.update_data(broadcast_msg_id=message.message_id, broadcast_chat_id=message.chat.id)
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🚀 НАЧАТЬ", callback_data="confirm_broadcast_send"))
        kb.row(InlineKeyboardButton(text="❌ ОТМЕНА", callback_data="admin_panel"))
        await message.answer(
            "👆 <b>Это превью сообщения.</b>\nНачать рассылку для всех пользователей?",
            reply_markup=kb.as_markup()
        )

    @router.callback_query(F.data == "confirm_broadcast_send")
    async def adm_broadcast_run(call: CallbackQuery, state: FSMContext):
        await call.answer()
        data = await state.get_data()
        msg_id = data.get("broadcast_msg_id")
        from_chat = data.get("broadcast_chat_id")
        await state.clear()

        try:
            with db.get_connection() as conn:
                rows = conn.execute("SELECT user_id FROM users").fetchall()
                users_list = [row['user_id'] for row in rows]
        except Exception as e:
            await call.message.answer(f"❌ Ошибка базы данных: {e}")
            return

        if not users_list:
            await call.message.answer("❌ В базе данных еще нет пользователей для рассылки.")
            return

        count = 0
        err = 0
        await call.message.edit_text(f"⏳ Рассылка запущена для {len(users_list)} чел...")

        for user_id in users_list:
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=from_chat,
                    message_id=msg_id
                )
                count += 1
                await asyncio.sleep(0.05)
            except Exception:
                err += 1

        await call.message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📊 Успешно: {count}\n"
            f"🚫 Ошибок (бан бота): {err}"
        )

    @router.callback_query(F.data == "a_give_stars")
    async def adm_give_stars_start(call: CallbackQuery, state: FSMContext):
        await call.answer()
        if call.from_user.id not in admin_ids:
            return
        await state.set_state(AdminStates.waiting_give_data)
        await call.message.edit_text(
            "💎 <b>ВЫДАЧА ЗВЕЗД</b>\n\n"
            "Введите ID пользователя и количество звезд через пробел.\n"
            "Пример: <code>8364667153 100</code>",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")).as_markup()
        )

    @router.message(AdminStates.waiting_give_data)
    async def adm_give_stars_process(message: Message, state: FSMContext):
        if message.from_user.id not in admin_ids:
            return

        try:
            parts = message.text.split()
            if len(parts) != 2:
                await message.answer("❌ Ошибка! Введите два числа через пробел: ID и Сумму.")
                return

            target_id = int(parts[0])
            amount = float(parts[1])

            user = db.get_user(target_id)
            if not user:
                await message.answer(f"❌ Пользователь с ID <code>{target_id}</code> не найден в базе бота!")
                return

            db.add_stars(target_id, amount)

            await message.answer(
                f"✅ <b>УСПЕШНО!</b>\n\n"
                f"Пользователю: <b>{user['first_name']}</b> (<code>{target_id}</code>)\n"
                f"Начислено: <b>{amount} ⭐</b>",
                reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В админку", callback_data="admin_panel")).as_markup()
            )

            try:
                await bot.send_message(target_id, f"🎁 Администратор начислил вам <b>{amount} ⭐</b>!")
            except:
                pass

            await state.clear()

        except ValueError:
            await message.answer("❌ Ошибка! Используйте только цифры. Пример: <code>12345678 50</code>")
        except Exception as e:
            await message.answer(f"❌ Произошла ошибка: {e}")
            await state.clear()

    @router.callback_query(F.data == "a_create_promo")
    async def adm_promo_start(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.set_state(AdminStates.waiting_promo_data)
        await call.message.answer(
            "Введите данные промокода через пробел:\n"
            "<code>КОД ТИП ЗНАЧЕНИЕ КОЛ_ВО</code>\n\n"
            "Примеры:\n"
            "<code>GIFT1 stars 100 10</code> (100 звезд)\n"
            "<code>ROZA gift 🌹_Роза 5</code> (5 роз)"
        )

    @router.message(AdminStates.waiting_promo_data)
    async def adm_promo_save(message: Message, state: FSMContext):
        try:
            code, r_type, val, uses = message.text.split()
            with db.get_connection() as conn:
                conn.execute("INSERT INTO promo VALUES (?, ?, ?, ?)", (code, r_type, val, int(uses)))
                conn.commit()
            await message.answer(f"✅ Промокод <code>{code}</code> создан на {uses} использований!")
            await state.clear()
        except Exception as e:
            await message.answer("❌ Ошибка! Формат: <code>КОД ТИП ЗНАЧЕНИЕ КОЛ_ВО</code>")

    @router.callback_query(F.data == "a_fake_gen")
    async def adm_fake(call: CallbackQuery):
        await call.answer()
        if call.from_user.id not in admin_ids:
            return

        items = list(GIFTS_PRICES.keys())
        fake_item = random.choice(items)

        fake_names = ["Dmitry_ST", "Sasha_Official", "Rich_Boy", "CryptoKing", "Masha_Stars", "Legenda_77"]
        name = random.choice(fake_names)
        fid = random.randint(1000000000, 9999999999)

        text = (
            f"🎁 <b>ЗАЯВКА НА ВЫВОД </b>\n\n"
            f"👤 Юзер: @{name}\n"
            f"🆔 ID: <code>{fid}</code>\n"
            f"📦 Предмет: <b>{fake_item}</b>"
        )

        await bot.send_message(
            WITHDRAWAL_CHANNEL_ID,
            text,
            reply_markup=get_admin_decision_kb(0, "GIFT")
        )
        await call.answer("✅ Реалистичный фейк отправлен!")

    @router.message(AdminStates.waiting_channel_post)
    async def adm_post_end(message: Message, state: FSMContext):
        pid = f"v_{random.randint(100, 999)}"
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="💰 Забрать 0.3 ⭐", callback_data=f"claim_{pid}"))
        await bot.send_message(CHANNEL_ID, message.text, reply_markup=kb.as_markup())
        await message.answer("✅ Опубликовано!")
        await state.clear()

    @router.callback_query(F.data.startswith("claim_"))
    async def cb_claim(call: CallbackQuery):
        await call.answer()
        pid, uid = call.data.split("_")[1], call.from_user.id
        if not db.get_user(uid):
            await call.answer("❌ Запусти бота!", show_alert=True)
            return
        try:
            with db.get_connection() as conn:
                conn.execute("INSERT INTO post_claims (user_id, post_id) VALUES (?, ?)", (uid, pid))
                conn.commit()
            db.add_stars(uid, VIEW_REWARD)
            await call.answer(f"✅ +{VIEW_REWARD} ⭐", show_alert=True)
        except:
            await call.answer("❌ Уже забрал!", show_alert=True)

    @router.callback_query(F.data.startswith("adm_chat_"))
    async def cb_adm_chat(call: CallbackQuery):
        await call.answer()
        if call.from_user.id not in admin_ids:
            return
        uid = call.data.split("_")[2]
        if uid == "0":
            await call.answer("❌ Это фейк!", show_alert=True)
            return
        await call.message.answer(f"🔗 Связь с юзером: tg://user?id={uid}")
        await call.answer()

    @router.callback_query(F.data.startswith("adm_app_") | F.data.startswith("adm_rej_"))
    async def cb_adm_action(call: CallbackQuery):
        await call.answer()
        if call.from_user.id not in admin_ids:
            await call.answer("❌ Вы не являетесь администратором!", show_alert=True)
            return

        data_parts = call.data.split("_")
        action = data_parts[1]
        target_uid = int(data_parts[2])
        value = data_parts[3]

        if target_uid == 0:
            status_fake = "✅ ОДОБРЕНО (ФЕЙК)" if action == "app" else "❌ ОТКЛОНЕНО (ФЕЙК)"
            await call.message.edit_text(f"{call.message.text}\n\n<b>Итог: {status_fake}</b>")
            await call.answer("Это был фейк-вывод")
            return

        try:
            if action == "app":
                reward_text = "подарка" if value == "GIFT" else f"{value} ⭐"
                await bot.send_message(target_uid, f"🎉 <b>Ваша заявка на вывод {reward_text} одобрена!</b>")
                status_text = "✅ ПРИНЯТО"
            else:
                if value == "GIFT":
                    await bot.send_message(target_uid, "❌ <b>Заявка на вывод подарка отклонена.</b>\nСвяжитесь с поддержкой.")
                else:
                    db.add_stars(target_uid, float(value))
                    await bot.send_message(target_uid, f"❌ <b>Выплата {value} ⭐ отклонена.</b>\nЗвезды возвращены на ваш баланс.")
                status_text = "❌ ОТКЛОНЕНО"

            await call.message.edit_text(
                f"{call.message.text}\n\n<b>Итог: {status_text}</b> (Админ: @{call.from_user.username or call.from_user.id})"
            )
            await call.answer("Готово!")

        except Exception as e:
            logging.error(f"Ошибка в админ-действии: {e}")
            await call.answer("❌ Ошибка (возможно, юзер заблокировал бота)", show_alert=True)

    # --- МАГАЗИН ---
    @router.callback_query(F.data == "shop")
    async def cb_shop_menu(call: CallbackQuery):
        await call.answer()
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💎 ЭКСКЛЮЗИВНЫЕ ТОВАРЫ", callback_data="special_shop"))
        kb.row(InlineKeyboardButton(text="⚡ Буст рефералов +0.1 (50 ⭐)", callback_data="buy_boost_01"))
        for item, price in GIFTS_PRICES.items():
            kb.add(InlineKeyboardButton(text=f"{item} {price}⭐", callback_data=f"buy_g_{item}"))
        kb.adjust(1, 1, 2)
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))

        await call.message.edit_text(
            "✨ <b>МАГАЗИН</b>\n\n"
            "Обычные подарки доступны всегда, а в <b>Эксклюзивном отделе</b> товары ограничены по количеству!",
            reply_markup=kb.as_markup()
        )

    @router.callback_query(F.data == "buy_boost_01")
    async def buy_boost(call: CallbackQuery):
        await call.answer()
        uid = call.from_user.id
        user = db.get_user(uid)
        if user['stars'] < 50:
            await call.answer("❌ Нужно 50 ⭐", show_alert=True)
            return

        db.add_stars(uid, -50)
        with db.get_connection() as conn:
            conn.execute("UPDATE users SET ref_boost = ref_boost + 0.1 WHERE user_id = ?", (uid,))
            conn.commit()
        await call.answer("🚀 Буст успешно куплен! Теперь ты получаешь больше.", show_alert=True)

    @router.callback_query(F.data.startswith("buy_g_"))
    async def process_gift_buy(call: CallbackQuery):
        await call.answer()
        item_name = call.data.replace("buy_g_", "")
        price = GIFTS_PRICES.get(item_name)
        uid = call.from_user.id
        user = db.get_user(uid)

        if user['stars'] < price:
            await call.answer(f"❌ Недостаточно звезд! Нужно {price} ⭐", show_alert=True)
            return

        db.add_stars(uid, -price)
        with db.get_connection() as conn:
            existing = conn.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (uid, item_name)).fetchone()
            if existing:
                conn.execute("UPDATE inventory SET quantity = quantity + 1 WHERE user_id = ? AND item_name = ?", (uid, item_name))
            else:
                conn.execute("INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1)", (uid, item_name))
            conn.commit()

        await call.answer(f"✅ Вы купили {item_name}!", show_alert=True)

    @router.callback_query(F.data.startswith("inventory"))
    async def cb_inventory_logic(call: CallbackQuery):
        await call.answer()
        if "_" in call.data:
            page = int(call.data.split("_")[1])
        else:
            page = 0

        uid = call.from_user.id
        with db.get_connection() as conn:
            items = conn.execute("SELECT item_name, quantity FROM inventory WHERE user_id = ?", (uid,)).fetchall()

        if not items:
            kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
            await call.message.edit_text("🎒 <b>Твой инвентарь пуст.</b>\nКупи что-нибудь в магазине!", reply_markup=kb.as_markup())
            return

        total_pages = (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        start_idx = page * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        current_items = items[start_idx:end_idx]

        text = f"🎒 <b>ТВОЙ ИНВЕНТАРЬ</b> (Стр. {page+1}/{total_pages})\n\nНажми на предмет, чтобы вывести его:"
        kb = InlineKeyboardBuilder()
        for it in current_items:
            kb.row(InlineKeyboardButton(text=f"{it['item_name']} ({it['quantity']} шт.)", callback_data=f"pre_out_{it['item_name']}"))

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"inventory_{page-1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"inventory_{page+1}"))
        if nav_row:
            kb.row(*nav_row)
        kb.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="menu"))

        await call.message.edit_text(text, reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("pre_out_"))
    async def cb_pre_out(call: CallbackQuery):
        await call.answer()
        item = call.data.replace("pre_out_", "")
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🎁 Получить как подарок", callback_data=f"confirm_out_{item}"))
        if any(info['full_name'] in item for info in SPECIAL_ITEMS.values()):
            kb.row(InlineKeyboardButton(text="💰 Выставить на P2P Маркет", callback_data=f"sell_p2p_{item}"))
        kb.row(InlineKeyboardButton(text="❌ Отмена", callback_data="inventory_0"))
        await call.message.edit_text(f"Вы выбрали: <b>{item}</b>\nЧто хотите сделать?", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("confirm_out_"))
    async def cb_final_out(call: CallbackQuery):
        await call.answer()
        item = call.data.replace("confirm_out_", "")
        uid = call.from_user.id
        username = call.from_user.username or "User"

        with db.get_connection() as conn:
            res = conn.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (uid, item)).fetchone()
            if not res or res['quantity'] <= 0:
                await call.answer("❌ Предмет не найден!", show_alert=True)
                return

            if res['quantity'] > 1:
                conn.execute("UPDATE inventory SET quantity = quantity - 1 WHERE user_id = ? AND item_name = ?", (uid, item))
            else:
                conn.execute("DELETE FROM inventory WHERE user_id = ? AND item_name = ?", (uid, item))
            conn.commit()

        await bot.send_message(
            WITHDRAWAL_CHANNEL_ID,
            f"🎁 <b>ЗАЯВКА НА ВЫВОД </b>\n\n👤 Юзер: @{username}\n🆔 ID: <code>{uid}</code>\n📦 Предмет: <b>{item}</b>",
            reply_markup=get_admin_decision_kb(uid, "GIFT")
        )

        await call.message.edit_text(
            f"✅ Заявка на вывод <b>{item}</b> отправлена!\nОжидайте сообщения от администратора.",
            reply_markup=get_main_kb(uid)
        )

    @router.callback_query(F.data == "use_promo")
    async def promo_start(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.set_state(PromoStates.waiting_for_code)
        await call.message.answer("⌨️ Введите промокод:")

    @router.message(PromoStates.waiting_for_code)
    async def promo_process(message: Message, state: FSMContext):
        code = message.text.strip()
        uid = message.from_user.id

        with db.get_connection() as conn:
            already_used = conn.execute(
                "SELECT 1 FROM promo_history WHERE user_id = ? AND code = ?",
                (uid, code)
            ).fetchone()
            if already_used:
                await state.clear()
                await message.answer("❌ Вы уже активировали этот промокод!")
                return

            p = conn.execute("SELECT * FROM promo WHERE code = ? AND uses > 0", (code,)).fetchone()

            if p:
                conn.execute("UPDATE promo SET uses = uses - 1 WHERE code = ?", (code,))
                conn.execute("INSERT INTO promo_history (user_id, code) VALUES (?, ?)", (uid, code))
                conn.commit()

                if p['reward_type'] == 'stars':
                    db.add_stars(uid, float(p['reward_value']))
                    await message.answer(f"✅ Активировано! +{p['reward_value']} ⭐")
                else:
                    item = p['reward_value']
                    existing = conn.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (uid, item)).fetchone()
                    if existing:
                        conn.execute("UPDATE inventory SET quantity = quantity + 1 WHERE user_id = ? AND item_name = ?", (uid, item))
                    else:
                        conn.execute("INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1)", (uid, item))
                    conn.commit()
                    await message.answer(f"✅ Активировано! Получен предмет: {item}")
            else:
                await message.answer("❌ Код неверный, либо закончились его активации.")

        await state.clear()

    @router.callback_query(F.data == "special_shop")
    async def cb_special_shop(call: CallbackQuery):
        await call.answer()
        kb = InlineKeyboardBuilder()
        with db.get_connection() as conn:
            for key, info in SPECIAL_ITEMS.items():
                res = conn.execute("SELECT SUM(quantity) FROM inventory WHERE item_name = ?", (info['full_name'],)).fetchone()
                sold = res[0] if res and res[0] else 0
                left = info['limit'] - sold
                if left > 0:
                    text = f"{info['full_name']} — {info['price']} ⭐ (Осталось: {left})"
                    callback = f"buy_t_{key}"
                else:
                    text = f"{info['full_name']} — 🚫 РАСПРОДАНО"
                    callback = "sold_out"
                kb.row(InlineKeyboardButton(text=text, callback_data=callback))

        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
        await call.message.edit_text(
            "🛒 <b>ЭКСКЛЮЗИВНЫЕ ТОВАРЫ</b>\n\n"
            "<i>Когда лимит исчерпан, товар можно купить только у игроков на P2P Рынке!</i>",
            reply_markup=kb.as_markup()
        )

    @router.callback_query(F.data == "sold_out")
    async def cb_sold_out(call: CallbackQuery):
        await call.answer("❌ Этот товар закончился в магазине! Ищите его на P2P рынке.", show_alert=True)

    @router.callback_query(F.data.startswith("buy_t_"))
    async def buy_special_item(call: CallbackQuery):
        await call.answer()
        item_key = call.data.split("_")[2]
        full_name = {
            "Ramen": "🍜 Ramen",
            "Candle": "🕯 B-Day Candle",
            "Calendar": "🗓 Desk Calendar"
        }[item_key]
        price = SPECIAL_ITEMS[item_key]["price"]
        uid = call.from_user.id

        user = db.get_user(uid)
        if user['stars'] < price:
            await call.answer("❌ Недостаточно звезд!", show_alert=True)
            return

        db.add_stars(uid, -price)
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1",
                (uid, full_name)
            )
            conn.commit()

        await call.answer(f"✅ {full_name} куплен!", show_alert=True)

    @router.callback_query(F.data == "p2p_market")
    async def cb_p2p_market(call: CallbackQuery):
        await call.answer()
        kb = InlineKeyboardBuilder()
        with db.get_connection() as conn:
            items = conn.execute("SELECT id, seller_id, item_name, price FROM marketplace").fetchall()

        text = "🏪 <b>P2P МАРКЕТ</b>\n\nЗдесь можно перекупить эксклюзивы у игроков.\n"
        if not items:
            text += "\n<i>Лотов пока нет.</i>"

        for it in items:
            kb.row(InlineKeyboardButton(text=f"🛒 {it['item_name']} | {it['price']} ⭐", callback_data=f"buy_p2p_{it['id']}"))

        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu"))
        await call.message.edit_text(text, reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("sell_p2p_"))
    async def cb_sell_item_start(call: CallbackQuery, state: FSMContext):
        await call.answer()
        item_name = call.data.replace("sell_p2p_", "")
        await state.update_data(sell_item=item_name)
        await state.set_state(P2PSaleStates.waiting_for_price)
        await call.message.answer(f"💰 Введите цену в ⭐, за которую хотите продать <b>{item_name}</b>:")

    @router.message(P2PSaleStates.waiting_for_price)
    async def process_p2p_sale_price(message: Message, state: FSMContext):
        data = await state.get_data()
        item_name = data.get("sell_item")
        uid = message.from_user.id

        if not message.text.isdigit():
            await message.answer("❌ Введите цену числом!")
            return

        price = int(message.text)
        if price <= 0:
            await message.answer("❌ Цена должна быть больше 0!")
            return

        with db.get_connection() as conn:
            res = conn.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (uid, item_name)).fetchone()
            if not res or res['quantity'] <= 0:
                await state.clear()
                await message.answer("❌ У вас нет этого предмета!")
                return

            if res['quantity'] > 1:
                conn.execute("UPDATE inventory SET quantity = quantity - 1 WHERE user_id = ? AND item_name = ?", (uid, item_name))
            else:
                conn.execute("DELETE FROM inventory WHERE user_id = ? AND item_name = ?", (uid, item_name))

            conn.execute("INSERT INTO marketplace (seller_id, item_name, price) VALUES (?, ?, ?)", (uid, item_name, price))
            conn.commit()

        await message.answer(f"✅ Предмет <b>{item_name}</b> выставлен на P2P Маркет за {price} ⭐")
        await state.clear()

    @router.callback_query(F.data.startswith("buy_p2p_"))
    async def cb_buy_p2p(call: CallbackQuery):
        await call.answer()
        order_id = int(call.data.split("_")[2])
        buyer_id = call.from_user.id

        with db.get_connection() as conn:
            order = conn.execute("SELECT * FROM marketplace WHERE id = ?", (order_id,)).fetchone()
            if not order:
                await call.answer("❌ Товар уже продан!", show_alert=True)
                return
            if order['seller_id'] == buyer_id:
                await call.answer("❌ Свой товар купить нельзя!", show_alert=True)
                return

            buyer = db.get_user(buyer_id)
            if buyer['stars'] < order['price']:
                await call.answer("❌ Недостаточно ⭐", show_alert=True)
                return

            db.add_stars(buyer_id, -order['price'])
            db.add_stars(order['seller_id'], order['price'] * 0.9)

            conn.execute(
                "INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + 1",
                (buyer_id, order['item_name'])
            )
            conn.execute("DELETE FROM marketplace WHERE id = ?", (order_id,))
            conn.commit()

        await call.answer(f"✅ Успешно купили {order['item_name']}!", show_alert=True)
        await cb_p2p_market(call)

    # --- Функции клавиатур (используются в хендлерах) ---
    def get_main_kb(uid):
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="🎯 Квесты", callback_data="tasks"),
            InlineKeyboardButton(text="⚔️ Дуель", callback_data="duel_menu"),
            InlineKeyboardButton(text="👥 Друзья", callback_data="referrals")
        )
        builder.row(
            InlineKeyboardButton(text="🎰 Удача", callback_data="luck"),
            InlineKeyboardButton(text="📆 Ежедневно", callback_data="daily"),
            InlineKeyboardButton(text="🎟 Лотерея", callback_data="lottery")
        )
        builder.row(
            InlineKeyboardButton(text="🛒 Магазин", callback_data="shop"),
            InlineKeyboardButton(text="🏪 P2P Маркет", callback_data="p2p_market"),
            InlineKeyboardButton(text="🎒 Инвентарь", callback_data="inventory")
        )
        builder.row(
            InlineKeyboardButton(text="🏆 ТОП", callback_data="top"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
            InlineKeyboardButton(text="🎁 Промокод", callback_data="use_promo")
        )
        if uid in admin_ids:
            builder.row(InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin_panel"))
        return builder.as_markup()

    def get_admin_decision_kb(uid, amount):
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="✅ Принять", callback_data=f"adm_app_{uid}_{amount}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_rej_{uid}_{amount}")
        )
        builder.row(InlineKeyboardButton(text="✉️ Написать в ЛС", callback_data=f"adm_chat_{uid}"))
        return builder.as_markup()

    # Регистрируем роутер в диспетчер
    dp.include_router(router)
