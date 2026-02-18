import asyncio
import logging
import os
import json
import re
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import aiosqlite
from aiohttp import web

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set")

PORT = int(os.getenv("PORT", 8000))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== БД ==========
DB_NAME = "bot_constructor.db"

# Глобальные переменные
user_bots: Dict[str, Tuple[Bot, Dispatcher, asyncio.Task]] = {}  # token -> (Bot, Dispatcher, Task)

# ========== FSM СОСТОЯНИЯ ==========
class ConstructorStates(StatesGroup):
    main_menu = State()
    waiting_for_token = State()
    select_bot = State()
    create_scene = State()
    edit_scene = State()
    add_message = State()
    add_button = State()
    edit_variables = State()
    delete_elements = State()
    create_variable = State()
    add_alias = State()

# ========== КЛАСС УПРАВЛЕНИЯ ПЕРЕМЕННЫМИ ==========
class VariableManager:
    def __init__(self, db, bot_id: int):
        self.db = db
        self.bot_id = bot_id
        self.aliases = {}

    async def load_aliases(self):
        async with self.db.execute(
            "SELECT alias, value FROM aliases WHERE bot_id = ?", (self.bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            self.aliases = {row[0]: int(row[1]) for row in rows}

    async def save_alias(self, alias: str, value: int):
        await self.db.execute(
            "INSERT OR REPLACE INTO aliases (bot_id, alias, value) VALUES (?, ?, ?)",
            (self.bot_id, alias, value)
        )
        await self.db.commit()
        self.aliases[alias] = value

    async def get_user_variable(self, user_id: int, key: str) -> Optional[str]:
        async with self.db.execute(
            "SELECT value FROM user_data WHERE bot_id = ? AND user_id = ? AND key = ?",
            (self.bot_id, user_id, key)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_user_variable(self, user_id: int, key: str, value: str):
        await self.db.execute(
            "INSERT OR REPLACE INTO user_data (bot_id, user_id, key, value) VALUES (?, ?, ?, ?)",
            (self.bot_id, user_id, key, value)
        )
        await self.db.commit()

    async def process_expression(self, user_id: int, expression: str) -> Tuple[bool, str]:
        try:
            expression = expression.strip()
            if "==" in expression:
                parts = expression.split("==", 1)
                if len(parts) == 2:
                    var_name = parts[0].strip()
                    value = parts[1].strip()
                    if value in self.aliases:
                        value = str(self.aliases[value])
                    await self.set_user_variable(user_id, var_name, value)
                    return True, f"✅ {var_name} = {value}"
            elif "++" in expression:
                parts = expression.split("++", 1)
                if len(parts) == 2:
                    var_name = parts[0].strip()
                    increment = parts[1].strip()
                    current = await self.get_user_variable(user_id, var_name)
                    if current in self.aliases:
                        cur_num = self.aliases[current]
                    else:
                        try:
                            cur_num = int(current) if current else 0
                        except:
                            cur_num = 0
                    try:
                        inc_num = int(increment)
                    except:
                        return False, f"❌ Некорректное число: {increment}"
                    new_num = cur_num + inc_num
                    new_value = str(new_num)
                    for alias, val in self.aliases.items():
                        if val == new_num:
                            new_value = alias
                            break
                    await self.set_user_variable(user_id, var_name, new_value)
                    return True, f"✅ {var_name} увеличен на {increment}. Новое значение: {new_value}"
            elif "--" in expression:
                parts = expression.split("--", 1)
                if len(parts) == 2:
                    var_name = parts[0].strip()
                    decrement = parts[1].strip()
                    current = await self.get_user_variable(user_id, var_name)
                    if current in self.aliases:
                        cur_num = self.aliases[current]
                    else:
                        try:
                            cur_num = int(current) if current else 0
                        except:
                            cur_num = 0
                    try:
                        dec_num = int(decrement)
                    except:
                        return False, f"❌ Некорректное число: {decrement}"
                    new_num = cur_num - dec_num
                    new_value = str(new_num)
                    for alias, val in self.aliases.items():
                        if val == new_num:
                            new_value = alias
                            break
                    await self.set_user_variable(user_id, var_name, new_value)
                    return True, f"✅ {var_name} уменьшен на {decrement}. Новое значение: {new_value}"
            return False, "❌ Некорректное выражение"
        except Exception as e:
            logger.error(f"Error processing expression: {e}")
            return False, f"❌ Ошибка: {str(e)}"

    def replace_placeholders(self, text: str, user_data: Dict) -> str:
        if not text:
            return text
        def replace(match):
            placeholder = match.group(1)
            if placeholder in user_data:
                return str(user_data[placeholder])
            return match.group(0)
        return re.sub(r'##(\w+)##', replace, text)

# ========== ИНИЦИАЛИЗАЦИЯ БД ==========
async def init_db():
    db = await aiosqlite.connect(DB_NAME)

    # Таблица ботов
    await db.execute('''CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        bot_username TEXT,
        is_active BOOLEAN DEFAULT 0,
        start_scene TEXT DEFAULT 'start',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Таблица сцен (привязаны к боту)
    await db.execute('''CREATE TABLE IF NOT EXISTS scenes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER NOT NULL,
        scene_id TEXT NOT NULL,
        name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(bot_id, scene_id),
        FOREIGN KEY (bot_id) REFERENCES bots(id) ON DELETE CASCADE
    )''')

    # Таблица сообщений
    await db.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id INTEGER NOT NULL,
        message_order INTEGER NOT NULL,
        text TEXT,
        media_type TEXT,
        media_id TEXT,
        FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
    )''')

    # Таблица кнопок
    await db.execute('''CREATE TABLE IF NOT EXISTS buttons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        button_order INTEGER NOT NULL,
        text TEXT NOT NULL,
        action TEXT NOT NULL,
        FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE,
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
    )''')

    # Таблица пользовательских переменных (для каждого бота)
    await db.execute('''CREATE TABLE IF NOT EXISTS user_data (
        bot_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (bot_id, user_id, key),
        FOREIGN KEY (bot_id) REFERENCES bots(id) ON DELETE CASCADE
    )''')

    # Таблица алиасов (для каждого бота)
    await db.execute('''CREATE TABLE IF NOT EXISTS aliases (
        bot_id INTEGER NOT NULL,
        alias TEXT NOT NULL,
        value INTEGER NOT NULL,
        PRIMARY KEY (bot_id, alias),
        FOREIGN KEY (bot_id) REFERENCES bots(id) ON DELETE CASCADE
    )''')

    await db.commit()
    return db

db = None

async def get_db():
    global db
    if db is None:
        db = await init_db()
    return db

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def check_bot_token(token: str) -> Tuple[bool, Optional[str]]:
    try:
        temp_bot = Bot(token=token)
        bot_info = await temp_bot.get_me()
        await temp_bot.session.close()
        return True, bot_info.username
    except Exception as e:
        logger.error(f"Ошибка проверки токена: {e}")
        return False, None

async def get_user_bots(user_id: int) -> List[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute(
        "SELECT * FROM bots WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_bot_by_id(bot_id: int) -> Optional[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None

async def get_bot_by_token(token: str) -> Optional[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute("SELECT * FROM bots WHERE token = ?", (token,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None

async def add_bot(user_id: int, token: str, bot_username: str) -> int:
    db_conn = await get_db()
    cursor = await db_conn.execute(
        "INSERT INTO bots (user_id, token, bot_username) VALUES (?, ?, ?)",
        (user_id, token, bot_username)
    )
    await db_conn.commit()
    return cursor.lastrowid

async def update_bot_active(bot_id: int, is_active: bool):
    db_conn = await get_db()
    await db_conn.execute(
        "UPDATE bots SET is_active = ? WHERE id = ?",
        (1 if is_active else 0, bot_id)
    )
    await db_conn.commit()

async def get_bot_scenes(bot_id: int) -> List[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute(
        "SELECT * FROM scenes WHERE bot_id = ? ORDER BY created_at", (bot_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_scene(bot_id: int, scene_id: str) -> Optional[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute(
        "SELECT * FROM scenes WHERE bot_id = ? AND scene_id = ?", (bot_id, scene_id)
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None

async def create_scene(bot_id: int, scene_id: str, name: str = None):
    db_conn = await get_db()
    if name is None:
        name = f"Сцена {scene_id}"
    await db_conn.execute(
        "INSERT INTO scenes (bot_id, scene_id, name) VALUES (?, ?, ?)",
        (bot_id, scene_id, name)
    )
    await db_conn.commit()

async def add_message(scene_id: int, text: str) -> int:
    db_conn = await get_db()
    # Получаем следующий order
    async with db_conn.execute(
        "SELECT COUNT(*) FROM messages WHERE scene_id = ?", (scene_id,)
    ) as cursor:
        count = (await cursor.fetchone())[0]
    cursor = await db_conn.execute(
        "INSERT INTO messages (scene_id, message_order, text, media_type) VALUES (?, ?, ?, ?)",
        (scene_id, count + 1, text, "text")
    )
    await db_conn.commit()
    return cursor.lastrowid

async def add_button(scene_id: int, message_id: int, text: str, action: str):
    db_conn = await get_db()
    async with db_conn.execute(
        "SELECT COUNT(*) FROM buttons WHERE message_id = ?", (message_id,)
    ) as cursor:
        count = (await cursor.fetchone())[0]
    await db_conn.execute(
        "INSERT INTO buttons (scene_id, message_id, button_order, text, action) VALUES (?, ?, ?, ?, ?)",
        (scene_id, message_id, count + 1, text, action)
    )
    await db_conn.commit()

async def delete_message(message_id: int):
    db_conn = await get_db()
    await db_conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    await db_conn.commit()

async def delete_button(button_id: int):
    db_conn = await get_db()
    await db_conn.execute("DELETE FROM buttons WHERE id = ?", (button_id,))
    await db_conn.commit()

# ========== ЗАПУСК/ОСТАНОВКА ПОЛЬЗОВАТЕЛЬСКИХ БОТОВ ==========
async def create_user_bot_handlers(bot_data: Dict):
    """Создание роутера для пользовательского бота"""
    router = Router()

    @router.message(Command("start"))
    async def user_bot_start(message: Message):
        # Вотермарка отдельным сообщением
        await message.answer("⚒️ Бот создан с помощью @KneoFreeBot")

        db_conn = await get_db()
        vm = VariableManager(db_conn, bot_data['id'])
        await vm.load_aliases()

        # Получаем переменные пользователя
        user_vars = {}
        async with db_conn.execute(
            "SELECT key, value FROM user_data WHERE bot_id = ? AND user_id = ?",
            (bot_data['id'], message.from_user.id)
        ) as cursor:
            rows = await cursor.fetchall()
            user_vars = {row[0]: row[1] for row in rows}

        # Добавляем системные переменные
        user_vars.setdefault("name_user", message.from_user.first_name)
        user_vars.setdefault("ID_user", str(message.from_user.id))
        user_vars.setdefault("user_user", message.from_user.username or "")

        # Получаем стартовую сцену
        scene = await get_scene(bot_data['id'], bot_data['start_scene'])
        if not scene:
            await message.answer("Сцена 'start' не найдена.")
            return

        # Получаем сообщения сцены
        async with db_conn.execute(
            "SELECT id, text FROM messages WHERE scene_id = ? ORDER BY message_order",
            (scene['id'],)
        ) as cursor:
            messages = await cursor.fetchall()

        for msg_id, msg_text in messages:
            processed = vm.replace_placeholders(msg_text, user_vars)

            # Получаем кнопки для этого сообщения
            async with db_conn.execute(
                "SELECT text, action FROM buttons WHERE message_id = ? ORDER BY button_order",
                (msg_id,)
            ) as cursor:
                buttons = await cursor.fetchall()

            keyboard = None
            if buttons:
                kb_buttons = []
                for btn_text, btn_action in buttons:
                    kb_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=btn_text)])
                keyboard = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

            await message.answer(processed, reply_markup=keyboard)

    @router.callback_query()
    async def user_bot_callback(callback: CallbackQuery):
        db_conn = await get_db()
        vm = VariableManager(db_conn, bot_data['id'])
        await vm.load_aliases()

        # Получаем действие по тексту кнопки
        async with db_conn.execute(
            "SELECT action FROM buttons WHERE text = ? AND message_id IN (SELECT id FROM messages WHERE scene_id IN (SELECT id FROM scenes WHERE bot_id = ?))",
            (callback.data, bot_data['id'])
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                await callback.answer("❌ Действие не найдено")
                return

        action = row[0]
        actions = action.split(';')
        for act in actions:
            act = act.strip()
            if act.startswith('goto:'):
                scene_id = act.replace('goto:', '').strip()
                # Здесь нужно реализовать показ сцены (можно вызвать функцию показа сцены)
                # Для простоты отправим сообщение о переходе
                await callback.message.answer(f"Переход на сцену {scene_id} (заглушка)")
            else:
                success, msg = await vm.process_expression(callback.from_user.id, act)
                if not success:
                    await callback.answer(msg, show_alert=True)
        await callback.answer()

    return router

async def start_user_bot(bot_data: Dict) -> bool:
    token = bot_data['token']
    if token in user_bots:
        return True

    try:
        user_bot = Bot(token=token)
        user_dp = Dispatcher(storage=MemoryStorage())
        router = await create_user_bot_handlers(bot_data)
        user_dp.include_router(router)

        task = asyncio.create_task(run_user_bot_polling(user_bot, user_dp, token))
        user_bots[token] = (user_bot, user_dp, task)
        logger.info(f"Запущен бот {bot_data['bot_username']}")
        return True
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
        return False

async def run_user_bot_polling(bot: Bot, dp: Dispatcher, token: str):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка поллинга бота {token[:10]}: {e}")
    finally:
        user_bots.pop(token, None)

async def stop_user_bot(token: str):
    if token in user_bots:
        bot, dp, task = user_bots[token]
        await dp.stop_polling()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        del user_bots[token]
        logger.info(f"Бот {token[:10]} остановлен")
        return True
    return False

async def start_all_user_bots():
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute("SELECT * FROM bots WHERE is_active = 1") as cursor:
        bots = await cursor.fetchall()
    for bot_data in bots:
        await start_user_bot(dict(bot_data))

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="🤖 Мои боты", callback_data="my_bots")],
        [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]])

def get_bot_management_keyboard(bot_id: int):
    keyboard = [
        [InlineKeyboardButton(text="📝 Создать сцену", callback_data=f"create_scene_{bot_id}")],
        [InlineKeyboardButton(text="✏️ Редактировать сцены", callback_data=f"edit_scenes_{bot_id}")],
        [InlineKeyboardButton(text="🔧 Мои переменные", callback_data=f"my_variables_{bot_id}")],
        [InlineKeyboardButton(text="➕ Создать переменную", callback_data=f"create_var_{bot_id}")],
        [InlineKeyboardButton(text="➕ Добавить алиас", callback_data=f"add_alias_{bot_id}")],
        [InlineKeyboardButton(text="▶️ Запустить бота", callback_data=f"start_bot_{bot_id}")],
        [InlineKeyboardButton(text="⏹ Остановить бота", callback_data=f"stop_bot_{bot_id}")],
        [InlineKeyboardButton(text="📊 Статус", callback_data=f"status_bot_{bot_id}")],
        [InlineKeyboardButton(text="↩️ Назад к ботам", callback_data="my_bots")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ========== ОСНОВНОЙ БОТ (КОНСТРУКТОР) ==========
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ========== ХЕНДЛЕРЫ ==========
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # Вотермарка
    await message.answer("⚒️ Бот создан с помощью @KneoFreeBot")

    # Проверяем, есть ли у пользователя боты
    bots = await get_user_bots(message.from_user.id)
    if not bots:
        await message.answer(
            "👋 Добро пожаловать в конструктор ботов!\n\n"
            "У вас пока нет ни одного бота. Отправьте токен бота, полученный от @BotFather, чтобы добавить его.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
            ])
        )
        await state.set_state(ConstructorStates.waiting_for_token)
    else:
        await message.answer(
            "Главное меню конструктора ботов:",
            reply_markup=get_main_keyboard()
        )

@router.message(ConstructorStates.waiting_for_token)
async def process_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if ":" not in token:
        await message.answer("❌ Неверный формат токена. Попробуйте ещё раз:")
        return

    wait_msg = await message.answer("🔍 Проверяю токен...")
    is_valid, username = await check_bot_token(token)
    if not is_valid:
        await wait_msg.edit_text("❌ Токен недействителен. Попробуйте ещё раз:")
        return

    # Сохраняем бота
    bot_id = await add_bot(message.from_user.id, token, username)
    await wait_msg.edit_text(
        f"✅ Бот @{username} успешно добавлен!\n"
        "Теперь вы можете управлять им через меню.",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "my_bots")
async def my_bots_callback(callback: CallbackQuery):
    bots = await get_user_bots(callback.from_user.id)
    if not bots:
        await callback.message.edit_text(
            "У вас нет добавленных ботов. Нажмите '➕ Добавить бота', чтобы добавить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")],
                [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
            ])
        )
        await callback.answer()
        return

    text = "🤖 Ваши боты:\n\n"
    keyboard = []
    for b in bots:
        status = "🟢 Активен" if b['is_active'] else "🔴 Остановлен"
        text += f"• @{b['bot_username']} ({status})\n"
        keyboard.append([InlineKeyboardButton(
            text=f"@{b['bot_username']}",
            callback_data=f"select_bot_{b['id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")])
    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data == "add_bot")
async def add_bot_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➕ Добавление нового бота\n\n"
        "Отправьте токен бота, полученный от @BotFather:",
        reply_markup=get_back_keyboard()
    )
    await state.set_state(ConstructorStates.waiting_for_token)
    await callback.answer()

@router.callback_query(F.data.startswith("select_bot_"))
async def select_bot_callback(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split("_")[2])
    bot_data = await get_bot_by_id(bot_id)
    if not bot_data:
        await callback.answer("Бот не найден")
        return

    await state.update_data(current_bot_id=bot_id)
    await callback.message.edit_text(
        f"Управление ботом @{bot_data['bot_username']}\n"
        "Выберите действие:",
        reply_markup=get_bot_management_keyboard(bot_id)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("create_scene_"))
async def create_scene_start(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split("_")[2])
    await state.update_data(current_bot_id=bot_id)
    await state.set_state(ConstructorStates.create_scene)
    await callback.message.edit_text(
        "📝 Создание новой сцены\n\n"
        "Введите ID сцены (латинские буквы, цифры, подчёркивание):\n"
        "Пример: start, menu, profile",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.create_scene)
async def create_scene_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("current_bot_id")
    scene_id = message.text.strip()

    if not re.match(r'^[a-zA-Z0-9_]+$', scene_id):
        await message.answer("❌ ID может содержать только латинские буквы, цифры и подчёркивание.")
        return

    db_conn = await get_db()
    async with db_conn.execute(
        "SELECT id FROM scenes WHERE bot_id = ? AND scene_id = ?", (bot_id, scene_id)
    ) as cursor:
        if await cursor.fetchone():
            await message.answer(f"❌ Сцена '{scene_id}' уже существует.")
            return

    await create_scene(bot_id, scene_id)
    await state.clear()
    await message.answer(
        f"✅ Сцена '{scene_id}' создана. Теперь добавьте в неё сообщения.",
        reply_markup=get_bot_management_keyboard(bot_id)
    )

@router.callback_query(F.data.startswith("edit_scenes_"))
async def edit_scenes_list(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split("_")[2])
    scenes = await get_bot_scenes(bot_id)

    if not scenes:
        await callback.message.edit_text(
            "У этого бота пока нет сцен. Создайте новую сцену.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Создать сцену", callback_data=f"create_scene_{bot_id}")],
                [InlineKeyboardButton(text="↩️ Назад", callback_data=f"select_bot_{bot_id}")]
            ])
        )
        await callback.answer()
        return

    text = "📋 Список сцен:\n\n"
    keyboard = []
    for s in scenes:
        text += f"• {s['name']} (ID: {s['scene_id']})\n"
        keyboard.append([InlineKeyboardButton(
            text=f"✏️ {s['scene_id']}",
            callback_data=f"edit_scene_{s['id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"select_bot_{bot_id}")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("edit_scene_"))
async def edit_scene_options(callback: CallbackQuery, state: FSMContext):
    scene_db_id = int(callback.data.split("_")[2])
    # Здесь можно показать опции для сцены (добавить сообщение, кнопку, удалить элементы)
    # Для краткости опустим детальную реализацию (она аналогична предыдущим ответам)
    await callback.answer("Редактирование сцены (в разработке)", show_alert=True)

# Аналогично для других кнопок (переменные, алиасы, запуск/остановка)

@router.callback_query(F.data.startswith("start_bot_"))
async def start_bot_callback(callback: CallbackQuery):
    bot_id = int(callback.data.split("_")[2])
    bot_data = await get_bot_by_id(bot_id)
    if not bot_data:
        await callback.answer("Бот не найден")
        return

    success = await start_user_bot(bot_data)
    if success:
        await update_bot_active(bot_id, True)
        await callback.answer("✅ Бот запущен")
        await callback.message.edit_text(
            f"Бот @{bot_data['bot_username']} запущен.",
            reply_markup=get_bot_management_keyboard(bot_id)
        )
    else:
        await callback.answer("❌ Не удалось запустить бота", show_alert=True)

@router.callback_query(F.data.startswith("stop_bot_"))
async def stop_bot_callback(callback: CallbackQuery):
    bot_id = int(callback.data.split("_")[2])
    bot_data = await get_bot_by_id(bot_id)
    if not bot_data:
        await callback.answer("Бот не найден")
        return

    success = await stop_user_bot(bot_data['token'])
    if success:
        await update_bot_active(bot_id, False)
        await callback.answer("✅ Бот остановлен")
        await callback.message.edit_text(
            f"Бот @{bot_data['bot_username']} остановлен.",
            reply_markup=get_bot_management_keyboard(bot_id)
        )
    else:
        await callback.answer("❌ Бот не был запущен", show_alert=True)

@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    help_text = """
📚 **ПОМОЩЬ ПО КОНСТРУКТОРУ БОТОВ**

**Боты**
• Вы можете добавить несколько ботов, каждый со своими сценами.
• Для добавления нажмите "➕ Добавить бота" и отправьте токен от @BotFather.
• Для управления ботом выберите его из списка.

**Сцены**
• Сцена — это набор сообщений и кнопок.
• Сообщения отправляются последовательно.
• Кнопки можно добавлять к любому сообщению.

**Переменные**
• Системные: `##name_user##`, `##ID_user##`, `##user_user##`.
• Свои переменные создаются через "➕ Создать переменную".
• Используйте в тексте: `##имя##`.

**Математика в кнопках**
• Присваивание: `переменная == значение`
• Сложение: `переменная ++ число`
• Вычитание: `переменная -- число`
• Комбинации: `действие1;действие2`

**Алиасы**
• Позволяют тексту соответствовать числу (например, Veteran = 2).
• Добавляются через "➕ Добавить алиас".

Подробнее — в разделах помощи по каждой функции.
"""
    keyboard = [[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]]
    await callback.message.edit_text(help_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Главное меню конструктора ботов:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

# ========== ВЕБ-СЕРВЕР ==========
async def web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Bot constructor is running"))
    app.router.add_get('/health', lambda request: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")
    await asyncio.Event().wait()

# ========== MAIN ==========
async def main():
    await get_db()
    await start_all_user_bots()  # Запускаем всех активных ботов при старте

    asyncio.create_task(web_server())
    await asyncio.sleep(1)

    logger.info("Constructor bot started polling")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
