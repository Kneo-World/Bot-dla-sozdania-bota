import os
import json
import logging
import asyncio
import random
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, FSInputFile
)
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import aiosqlite
import aiohttp
from aiohttp import web

# ========== ПОДКЛЮЧЕНИЕ ШАБЛОНА ==========
# Импортируем функции из готового шаблона (файл template_stars.py)
try:
    from template_stars import register_template_handlers, run_template_logic
    TEMPLATE_AVAILABLE = True
except ImportError:
    TEMPLATE_AVAILABLE = False
    logging.warning("Шаблон StarsForQuestion не найден. Шаблонные боты работать не будут.")

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в переменных окружения")

PORT = int(os.getenv('PORT', 10000))
ADMIN_ID = int(os.getenv('ADMIN_ID', '8364667153'))  # Основной админ @Nft_top3
SUPPORT_USERNAME = os.getenv('SUPPORT_USERNAME', '@Nft_top3')
WITHDRAWAL_CHANNEL = os.getenv('WITHDRAWAL_CHANNEL', '-1003891414947')
CHANNEL_ID = os.getenv('CHANNEL_ID', '-1003326584722')

# Экономика
DAILY_MIN, DAILY_MAX = 5, 10  # Кнетки за ежедневный бонус
REF_REWARD = 5                 # Бонус за реферала
ROYALTY_PERCENT = 20           # % автору шаблона при продаже

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
user_bots: Dict[str, Tuple[Bot, Dispatcher, asyncio.Task]] = {}  # токен -> (бот, диспетчер, задача)
WATERMARK_MESSAGE = "⚒️ Бот создан с помощью @KneoFreeBot"

# ========== ОСНОВНОЙ БОТ И ДИСПЕТЧЕР ==========
main_bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
main_dp = Dispatcher(storage=MemoryStorage())
main_router = Router()
main_dp.include_router(main_router)

# ========== СОСТОЯНИЯ FSM ==========
class ConstructorStates(StatesGroup):
    waiting_for_token = State()
    waiting_scene_name = State()
    waiting_scene_message = State()
    waiting_more_messages = State()
    waiting_button_type = State()
    waiting_button_text = State()
    waiting_button_url = State()
    waiting_button_target_scene = State()
    waiting_variable_name = State()
    waiting_variable_operation = State()
    waiting_variable_value = State()
    waiting_template_purchase = State()
    waiting_promo_code = State()
    waiting_broadcast = State()
    waiting_give_kn = State()
    waiting_moderate_template = State()

# ========== БАЗА ДАННЫХ ==========
async def init_db():
    async with aiosqlite.connect('kneo.db') as db:
        # Пользователи конструктора
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                kn_balance REAL DEFAULT 0,
                last_daily TIMESTAMP,
                ref_code TEXT UNIQUE,
                referred_by INTEGER,
                total_earned REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Транзакции
        await db.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Боты пользователей
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                bot_token TEXT UNIQUE,
                bot_username TEXT,
                is_active BOOLEAN DEFAULT 1,
                is_template BOOLEAN DEFAULT 0,
                template_author INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Сцены (для кастомных ботов)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                name TEXT,
                messages_json TEXT DEFAULT '[]',
                buttons_json TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(bot_id, name),
                FOREIGN KEY (bot_id) REFERENCES user_bots (id)
            )
        ''')
        
        # Переменные пользователей (для кастомных ботов)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_vars (
                bot_id INTEGER,
                user_id INTEGER,
                var_name TEXT,
                var_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(bot_id, user_id, var_name),
                FOREIGN KEY (bot_id) REFERENCES user_bots (id)
            )
        ''')
        
        # Алиасы переменных
        await db.execute('''
            CREATE TABLE IF NOT EXISTS var_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                var_name TEXT,
                alias_order INTEGER,
                alias_display TEXT,
                UNIQUE(bot_id, var_name, alias_order),
                UNIQUE(bot_id, var_name, alias_display),
                FOREIGN KEY (bot_id) REFERENCES user_bots (id)
            )
        ''')
        
        # Магазин шаблонов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER,
                name TEXT,
                description TEXT,
                price REAL,
                file_path TEXT,
                is_approved BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (author_id) REFERENCES users (user_id)
            )
        ''')
        
        # Покупки шаблонов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS template_purchases (
                user_id INTEGER,
                template_id INTEGER,
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (template_id) REFERENCES templates (id)
            )
        ''')
        
        # Промокоды
        await db.execute('''
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                amount REAL,
                uses_left INTEGER,
                created_by INTEGER
            )
        ''')
        
        # Использования промокодов
        await db.execute('''
            CREATE TABLE IF NOT EXISTS promo_uses (
                user_id INTEGER,
                code TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, code)
            )
        ''')
        
        await db.commit()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def get_user(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def create_user(user_id: int, username: str, first_name: str, ref_by: int = None):
    async with aiosqlite.connect('kneo.db') as db:
        ref_code = f"ref{user_id}"
        await db.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_name, ref_code, referred_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, ref_code, ref_by))
        await db.commit()

async def add_kn(user_id: int, amount: float, description: str = ''):
    async with aiosqlite.connect('kneo.db') as db:
        await db.execute('UPDATE users SET kn_balance = kn_balance + ? WHERE user_id = ?', (amount, user_id))
        await db.execute('''
            INSERT INTO transactions (user_id, amount, type, description)
            VALUES (?, ?, ?, ?)
        ''', (user_id, amount, 'credit' if amount > 0 else 'debit', description))
        await db.commit()

async def deduct_kn(user_id: int, amount: float, description: str):
    await add_kn(user_id, -amount, description)

async def get_user_bot(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM user_bots WHERE user_id = ? ORDER BY id DESC LIMIT 1
        ''', (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def save_bot_token(user_id: int, token: str, bot_username: str, is_template: bool = False, author_id: int = None):
    async with aiosqlite.connect('kneo.db') as db:
        await db.execute('''
            INSERT INTO user_bots (user_id, bot_token, bot_username, is_template, template_author)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, token, bot_username, 1 if is_template else 0, author_id))
        await db.commit()

async def check_bot_token(token: str) -> Tuple[bool, Optional[str]]:
    try:
        temp_bot = Bot(token=token)
        me = await temp_bot.get_me()
        await temp_bot.session.close()
        return True, me.username
    except:
        return False, None

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ПЕРЕМЕННЫМИ В КАСТОМНЫХ БОТАХ ==========
async def get_user_var(bot_id: int, user_id: int, var_name: str) -> str:
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('''
            SELECT var_value FROM user_vars WHERE bot_id = ? AND user_id = ? AND var_name = ?
        ''', (bot_id, user_id, var_name))
        row = await cur.fetchone()
        if row:
            return row['var_value']
        else:
            await db.execute('''
                INSERT INTO user_vars (bot_id, user_id, var_name, var_value)
                VALUES (?, ?, ?, ?)
            ''', (bot_id, user_id, var_name, '0'))
            await db.commit()
            return '0'

async def set_user_var(bot_id: int, user_id: int, var_name: str, value: str):
    async with aiosqlite.connect('kneo.db') as db:
        await db.execute('''
            INSERT OR REPLACE INTO user_vars (bot_id, user_id, var_name, var_value)
            VALUES (?, ?, ?, ?)
        ''', (bot_id, user_id, var_name, value))
        await db.commit()

async def modify_user_var(bot_id: int, user_id: int, var_name: str, op: str, operand: str) -> str:
    # op: '==', '++', '--'
    current = await get_user_var(bot_id, user_id, var_name)
    try:
        current_num = float(current)
        operand_num = float(operand)
        if op == '==':
            new_val = operand
        elif op == '++':
            new_val = str(current_num + operand_num)
        elif op == '--':
            new_val = str(current_num - operand_num)
        else:
            new_val = current
    except:
        # Текстовые значения – поддерживаем только присваивание
        if op == '==':
            new_val = operand
        else:
            new_val = current
    await set_user_var(bot_id, user_id, var_name, new_val)
    return new_val

async def get_scene(bot_id: int, scene_name: str) -> Optional[Dict]:
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('''
            SELECT * FROM scenes WHERE bot_id = ? AND name = ?
        ''', (bot_id, scene_name))
        row = await cur.fetchone()
        return dict(row) if row else None

# ========== КЛАВИАТУРЫ ==========
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="🎁 Бонус", callback_data="daily_bonus")],
        [InlineKeyboardButton(text="🛠️ Мои боты", callback_data="my_bots"),
         InlineKeyboardButton(text="🏪 Магазин шаблонов", callback_data="template_shop")],
        [InlineKeyboardButton(text="👥 Рефералы", callback_data="ref"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
        [InlineKeyboardButton(text="👑 Админ панель", callback_data="admin_panel")]  # будет скрыто для не-админов
    ])

def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

# ========== ОБРАБОТЧИКИ ОСНОВНОГО БОТА ==========
@main_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or ''
    first_name = message.from_user.first_name or ''
    
    # Реферальная система
    ref_by = None
    if len(message.text.split()) > 1:
        arg = message.text.split()[1]
        if arg.startswith('ref'):
            try:
                ref_by = int(arg[3:])
            except:
                pass
    
    await create_user(user_id, username, first_name, ref_by)
    if ref_by and ref_by != user_id:
        await add_kn(ref_by, REF_REWARD, f"Реферал {user_id}")
    
    await message.answer(
        f"👋 Добро пожаловать в <b>Kneo Bots | Создай бота бесплатно</b>!\n\n"
        f"Твой баланс: {await get_kn_balance(user_id)} Кнеток",
        reply_markup=main_keyboard()
    )

@main_router.callback_query(F.data == "profile")
async def profile_callback(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    await call.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Имя: {user['first_name']}\n"
        f"Баланс: {user['kn_balance']:.2f} Кнеток\n"
        f"Рефералов: {await count_refs(user['user_id'])}\n"
        f"Ссылка: https://t.me/{(await main_bot.get_me()).username}?start={user['ref_code']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
        ])
    )

@main_router.callback_query(F.data == "daily_bonus")
async def daily_bonus_callback(call: CallbackQuery):
    user_id = call.from_user.id
    now = datetime.now()
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT last_daily FROM users WHERE user_id = ?', (user_id,))
        row = await cur.fetchone()
        if row and row['last_daily']:
            last = datetime.fromisoformat(row['last_daily'])
            if (now - last).days < 1:
                await call.answer("Ты уже получал бонус сегодня! Приходи завтра.", show_alert=True)
                return
        bonus = random.randint(DAILY_MIN, DAILY_MAX)
        await db.execute('UPDATE users SET kn_balance = kn_balance + ?, last_daily = ? WHERE user_id = ?',
                         (bonus, now.isoformat(), user_id))
        await db.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
                         (user_id, bonus, 'credit', 'Ежедневный бонус'))
        await db.commit()
    await call.answer(f"🎁 +{bonus} Кнеток получено!", show_alert=True)
    await profile_callback(call)

@main_router.callback_query(F.data == "ref")
async def ref_callback(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    ref_link = f"https://t.me/{(await main_bot.get_me()).username}?start={user['ref_code']}"
    await call.message.edit_text(
        f"👥 <b>Реферальная система</b>\n\n"
        f"За каждого приглашенного друга, который запустит бота, ты получаешь {REF_REWARD} Кнеток.\n\n"
        f"Твоя ссылка:\n<code>{ref_link}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
        ])
    )

@main_router.callback_query(F.data == "help")
async def help_callback(call: CallbackQuery):
    help_text = (
        "📚 <b>Помощь по Kneo Bots</b>\n\n"
        "• <b>Профиль</b> – твой баланс и реферальная ссылка.\n"
        "• <b>Мои боты</b> – управление созданными ботами, добавление токена, настройка сцен.\n"
        "• <b>Магазин шаблонов</b> – покупка готовых ботов за Кнетки. Автор получает 20% роялти.\n"
        "• <b>Пополнение баланса</b> – свяжись с @Nft_top3 для покупки Кнеток.\n"
        "• <b>Ежедневный бонус</b> – получай 5-10 Кнеток каждый день.\n"
        "• <b>Промокоды</b> – активируй в разделе 'Профиль'.\n\n"
        "Поддержка: @Nft_top3"
    )
    await call.message.edit_text(help_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ]))

# ---------- Управление ботами ----------
@main_router.callback_query(F.data == "my_bots")
async def my_bots_callback(call: CallbackQuery):
    user_id = call.from_user.id
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM user_bots WHERE user_id = ? ORDER BY created_at DESC
        ''', (user_id,))
        bots = await cursor.fetchall()
    if not bots:
        text = "У тебя пока нет ботов. Добавь нового бота, отправив его токен (получи у @BotFather)."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
        ])
    else:
        text = "Твои боты:\n\n"
        kb_buttons = []
        for bot in bots:
            status = "🟢 активен" if bot['is_active'] and bot['bot_token'] in user_bots else "🔴 остановлен"
            text += f"• @{bot['bot_username']} – {status}\n"
            kb_buttons.append([InlineKeyboardButton(text=f"⚙️ @{bot['bot_username']}", callback_data=f"edit_bot_{bot['id']}")])
        kb_buttons.append([InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")])
        kb_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await call.message.edit_text(text, reply_markup=kb)

@main_router.callback_query(F.data == "add_bot")
async def add_bot_callback(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "🤖 Отправь токен своего бота (получи у @BotFather).\n"
        "Пример: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
        reply_markup=cancel_keyboard()
    )
    await state.set_state(ConstructorStates.waiting_for_token)

@main_router.message(ConstructorStates.waiting_for_token)
async def process_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if ':' not in token:
        await message.answer("❌ Неверный формат токена. Попробуй снова.")
        return
    wait = await message.answer("🔍 Проверяю токен...")
    ok, username = await check_bot_token(token)
    if ok and username:
        user_id = message.from_user.id
        await save_bot_token(user_id, token, username)
        await wait.edit_text(f"✅ Бот @{username} добавлен! Теперь ты можешь настроить его сцены.")
        await state.clear()
    else:
        await wait.edit_text("❌ Недействительный токен. Проверь и попробуй ещё раз.")

# ---------- Магазин шаблонов ----------
@main_router.callback_query(F.data == "template_shop")
async def template_shop_callback(call: CallbackQuery):
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM templates WHERE is_approved = 1 ORDER BY price
        ''')
        templates = await cursor.fetchall()
    if not templates:
        text = "🏪 В магазине пока нет одобренных шаблонов."
    else:
        text = "🏪 <b>Магазин шаблонов</b>\n\nВыбери шаблон для покупки:\n"
    kb = InlineKeyboardBuilder()
    for tmpl in templates:
        kb.row(InlineKeyboardButton(
            text=f"{tmpl['name']} — {tmpl['price']} KN",
            callback_data=f"buy_template_{tmpl['id']}"
        ))
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_main"))
    await call.message.edit_text(text, reply_markup=kb.as_markup())

@main_router.callback_query(F.data.startswith("buy_template_"))
async def buy_template_callback(call: CallbackQuery, state: FSMContext):
    template_id = int(call.data.split('_')[2])
    user_id = call.from_user.id
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        tmpl = await (await db.execute('SELECT * FROM templates WHERE id = ?', (template_id,))).fetchone()
        if not tmpl:
            await call.answer("Шаблон не найден", show_alert=True)
            return
        user = await get_user(user_id)
        if user['kn_balance'] < tmpl['price']:
            await call.answer("❌ Недостаточно Кнеток! Пополни баланс.", show_alert=True)
            return
        # Проверяем, не покупал ли уже
        already = await db.execute('SELECT 1 FROM template_purchases WHERE user_id = ? AND template_id = ?',
                                   (user_id, template_id))
        if await already.fetchone():
            await call.answer("Ты уже покупал этот шаблон", show_alert=True)
            return
        # Списание
        await db.execute('UPDATE users SET kn_balance = kn_balance - ? WHERE user_id = ?',
                         (tmpl['price'], user_id))
        # Роялти автору
        royalty = tmpl['price'] * ROYALTY_PERCENT / 100
        await db.execute('UPDATE users SET kn_balance = kn_balance + ? WHERE user_id = ?',
                         (royalty, tmpl['author_id']))
        # Запись покупки
        await db.execute('INSERT INTO template_purchases (user_id, template_id) VALUES (?, ?)',
                         (user_id, template_id))
        # Создание бота из шаблона (копирование токена не нужно, шаблон — это код, а не токен)
        # Здесь мы просто отмечаем, что у пользователя теперь есть право запустить шаблон
        # В реальности нужно сохранить файл шаблона и при создании бота использовать его код
        # Упрощённо: создаём запись в user_bots с флагом is_template=1 и template_author
        # Токен пользователь введёт позже, но бот будет использовать логику шаблона
        await db.commit()
    await call.answer("✅ Покупка совершена! Теперь ты можешь создать бота на основе этого шаблона.", show_alert=True)
    await my_bots_callback(call)

# ---------- Админ панель ----------
def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💎 Начислить Кнетки", callback_data="admin_give_kn")],
        [InlineKeyboardButton(text="📦 Модерация шаблонов", callback_data="admin_moderate")],
        [InlineKeyboardButton(text="🎫 Создать промокод", callback_data="admin_create_promo")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])

@main_router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.message.edit_text("👑 <b>Админ панель</b>", reply_markup=admin_keyboard())

# Рассылка
@main_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConstructorStates.waiting_broadcast)
    await call.message.edit_text(
        "Отправь сообщение для рассылки (текст, фото, видео).",
        reply_markup=cancel_keyboard()
    )

@main_router.message(ConstructorStates.waiting_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    # Получаем всех пользователей
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT user_id FROM users')
        users = await cursor.fetchall()
    success = 0
    for u in users:
        try:
            await main_bot.copy_message(
                chat_id=u['user_id'],
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            success += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Рассылка завершена. Отправлено {success} пользователям.")
    await state.clear()

# Начисление Кнеток
@main_router.callback_query(F.data == "admin_give_kn")
async def admin_give_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConstructorStates.waiting_give_kn)
    await call.message.edit_text(
        "Введи ID пользователя и сумму через пробел.\nПример: 123456789 50",
        reply_markup=cancel_keyboard()
    )

@main_router.message(ConstructorStates.waiting_give_kn)
async def admin_give_process(message: Message, state: FSMContext):
    try:
        uid, amount = message.text.split()
        uid = int(uid)
        amount = float(amount)
        await add_kn(uid, amount, f"Начислено админом {message.from_user.id}")
        await message.answer(f"✅ Пользователю {uid} начислено {amount} Кнеток.")
    except:
        await message.answer("❌ Ошибка формата. Попробуй снова.")
    await state.clear()

# Модерация шаблонов
@main_router.callback_query(F.data == "admin_moderate")
async def admin_moderate_list(call: CallbackQuery):
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM templates WHERE is_approved = 0')
        pending = await cursor.fetchall()
    if not pending:
        await call.message.edit_text("Нет шаблонов на модерацию.", reply_markup=admin_keyboard())
        return
    text = "Шаблоны на проверку:\n"
    kb = InlineKeyboardBuilder()
    for t in pending:
        kb.row(InlineKeyboardButton(
            text=f"{t['name']} от {t['author_id']}",
            callback_data=f"mod_template_{t['id']}"
        ))
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel"))
    await call.message.edit_text(text, reply_markup=kb.as_markup())

@main_router.callback_query(F.data.startswith("mod_template_"))
async def admin_moderate_detail(call: CallbackQuery, state: FSMContext):
    tid = int(call.data.split('_')[2])
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        tmpl = await (await db.execute('SELECT * FROM templates WHERE id = ?', (tid,))).fetchone()
    if not tmpl:
        await call.answer("Шаблон не найден")
        return
    await call.message.edit_text(
        f"Шаблон: {tmpl['name']}\n"
        f"Автор: {tmpl['author_id']}\n"
        f"Цена: {tmpl['price']} KN\n"
        f"Описание: {tmpl['description']}\n\n"
        "Подтвердить публикацию?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data=f"approve_template_{tid}")],
            [InlineKeyboardButton(text="❌ Нет", callback_data=f"reject_template_{tid}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_moderate")]
        ])
    )

@main_router.callback_query(F.data.startswith("approve_template_"))
async def admin_approve_template(call: CallbackQuery):
    tid = int(call.data.split('_')[2])
    async with aiosqlite.connect('kneo.db') as db:
        await db.execute('UPDATE templates SET is_approved = 1 WHERE id = ?', (tid,))
        await db.commit()
    await call.answer("Шаблон опубликован в магазине.", show_alert=True)
    await admin_moderate_list(call)

@main_router.callback_query(F.data.startswith("reject_template_"))
async def admin_reject_template(call: CallbackQuery):
    tid = int(call.data.split('_')[2])
    async with aiosqlite.connect('kneo.db') as db:
        await db.execute('DELETE FROM templates WHERE id = ?', (tid,))
        await db.commit()
    await call.answer("Шаблон удалён.", show_alert=True)
    await admin_moderate_list(call)

# Промокоды
@main_router.callback_query(F.data == "admin_create_promo")
async def admin_create_promo(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConstructorStates.waiting_promo_code)
    await call.message.edit_text(
        "Введи данные промокода в формате:\n"
        "<code>КОД СУММА КОЛИЧЕСТВО_ИСПОЛЬЗОВАНИЙ</code>\n"
        "Пример: BONUS10 10 5",
        reply_markup=cancel_keyboard()
    )

@main_router.message(ConstructorStates.waiting_promo_code)
async def admin_create_promo_process(message: Message, state: FSMContext):
    try:
        code, amount, uses = message.text.split()
        amount = float(amount)
        uses = int(uses)
        async with aiosqlite.connect('kneo.db') as db:
            await db.execute('''
                INSERT INTO promos (code, amount, uses_left, created_by)
                VALUES (?, ?, ?, ?)
            ''', (code, amount, uses, message.from_user.id))
            await db.commit()
        await message.answer(f"✅ Промокод {code} создан!")
    except:
        await message.answer("❌ Ошибка формата.")
    await state.clear()

# ---------- Общие кнопки ----------
@main_router.callback_query(F.data == "back_main")
async def back_main_callback(call: CallbackQuery):
    await call.message.edit_text(
        "👋 Главное меню",
        reply_markup=main_keyboard()
    )

@main_router.callback_query(F.data == "cancel")
async def cancel_callback(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await back_main_callback(call)

# ========== ЗАПУСК ПОЛЬЗОВАТЕЛЬСКИХ БОТОВ ==========
async def run_user_bot(token: str):
    """Запускает бота пользователя. Если бот помечен как шаблонный, использует логику шаблона."""
    bot_data = await get_bot_by_token(token)
    if not bot_data:
        return
    if bot_data['is_template'] and TEMPLATE_AVAILABLE:
        # Используем обработчики из шаблона
        bot_instance = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())
        # Регистрируем хендлеры шаблона, передавая bot_data (автор, etc.)
        await register_template_handlers(dp, bot_data)
        task = asyncio.create_task(dp.start_polling(bot_instance))
        user_bots[token] = (bot_instance, dp, task)
    else:
        # Кастомный бот с простыми сценами (заглушка)
        # Здесь должен быть код для запуска бота с обычными сценами (из таблицы scenes)
        # Пока просто пропускаем
        pass

async def start_all_user_bots():
    async with aiosqlite.connect('kneo.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM user_bots WHERE is_active = 1')
        bots = await cursor.fetchall()
    for b in bots:
        if b['bot_token'] not in user_bots:
            await run_user_bot(b['bot_token'])

async def stop_user_bot(token: str):
    if token in user_bots:
        bot, dp, task = user_bots[token]
        await dp.stop_polling()
        task.cancel()
        del user_bots[token]

# ========== ВЕБ-СЕРВЕР ==========
async def health_check(request):
    return web.Response(text=f"Kneo Bots active. Running bots: {len(user_bots)}")

async def web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    logging.info(f"Web server started on port {PORT}")
    await site.start()

# ========== MAIN ==========
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    asyncio.create_task(web_server())
    await start_all_user_bots()
    await main_bot.delete_webhook(drop_pending_updates=True)
    await main_dp.start_polling(main_bot)

if __name__ == "__main__":
    asyncio.run(main())
