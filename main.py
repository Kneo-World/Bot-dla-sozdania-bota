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
    select_template = State()

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

    # Таблица шаблонов (глобальные, не привязаны к боту)
    await db.execute('''CREATE TABLE IF NOT EXISTS templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        scenes_json TEXT NOT NULL   -- JSON-структура сцен
    )''')

    await db.commit()

    # Заполняем шаблоны, если их нет
    await populate_templates(db)
    return db

async def populate_templates(db):
    # Проверяем, есть ли уже шаблоны
    async with db.execute("SELECT COUNT(*) FROM templates") as cursor:
        count = (await cursor.fetchone())[0]
    if count > 0:
        return

    templates = [
        {
            "name": "Приветствие",
            "description": "Простая сцена с приветствием и информацией о пользователе.",
            "scenes": [
                {
                    "scene_id": "start",
                    "name": "Старт",
                    "messages": [
                        "Привет, ##name_user##!",
                        "Твой ID: ##ID_user##",
                        "Твой username: ##user_user##"
                    ],
                    "buttons": []
                }
            ]
        },
        {
            "name": "Меню с кнопками",
            "description": "Сцена с главным меню и несколькими кнопками перехода.",
            "scenes": [
                {
                    "scene_id": "start",
                    "name": "Главное меню",
                    "messages": [
                        "Добро пожаловать в главное меню!"
                    ],
                    "buttons": [
                        {"text": "Профиль", "action": "goto:profile"},
                        {"text": "Магазин", "action": "goto:shop"},
                        {"text": "Помощь", "action": "goto:help"}
                    ]
                },
                {
                    "scene_id": "profile",
                    "name": "Профиль",
                    "messages": [
                        "Ваш профиль:",
                        "Имя: ##name_user##",
                        "ID: ##ID_user##"
                    ],
                    "buttons": [
                        {"text": "Назад", "action": "goto:start"}
                    ]
                },
                {
                    "scene_id": "shop",
                    "name": "Магазин",
                    "messages": [
                        "Добро пожаловать в магазин!",
                        "Здесь скоро появятся товары."
                    ],
                    "buttons": [
                        {"text": "Назад", "action": "goto:start"}
                    ]
                },
                {
                    "scene_id": "help",
                    "name": "Помощь",
                    "messages": [
                        "Раздел помощи. Обратитесь к администратору."
                    ],
                    "buttons": [
                        {"text": "Назад", "action": "goto:start"}
                    ]
                }
            ]
        },
        {
            "name": "Рейтинг (система рангов)",
            "description": "Демонстрация переменных и алиасов: ранг повышается/понижается.",
            "scenes": [
                {
                    "scene_id": "start",
                    "name": "Рейтинг",
                    "messages": [
                        "Привет, ##name_user##!",
                        "Твой текущий ранг: ##rank##",
                        "Звезд: ##stars##"
                    ],
                    "buttons": [
                        {"text": "➕ Повысить ранг", "action": "rank ++ 1"},
                        {"text": "➖ Понизить ранг", "action": "rank -- 1"},
                        {"text": "⭐ Получить звезду", "action": "stars ++ 1"},
                        {"text": "⭐ Потратить звезду", "action": "stars -- 1"}
                    ]
                }
            ]
        }
    ]

    for tpl in templates:
        await db.execute(
            "INSERT INTO templates (name, description, scenes_json) VALUES (?, ?, ?)",
            (tpl["name"], tpl["description"], json.dumps(tpl["scenes"], ensure_ascii=False))
        )
    await db.commit()

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

async def get_scene_by_db_id(scene_db_id: int) -> Optional[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_db_id,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None

async def get_scene_by_scene_id(bot_id: int, scene_id: str) -> Optional[Dict]:
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

async def add_message(scene_db_id: int, text: str) -> int:
    db_conn = await get_db()
    async with db_conn.execute(
        "SELECT COUNT(*) FROM messages WHERE scene_id = ?", (scene_db_id,)
    ) as cursor:
        count = (await cursor.fetchone())[0]
    cursor = await db_conn.execute(
        "INSERT INTO messages (scene_id, message_order, text, media_type) VALUES (?, ?, ?, ?)",
        (scene_db_id, count + 1, text, "text")
    )
    await db_conn.commit()
    return cursor.lastrowid

async def add_button(scene_db_id: int, message_id: int, text: str, action: str):
    db_conn = await get_db()
    async with db_conn.execute(
        "SELECT COUNT(*) FROM buttons WHERE message_id = ?", (message_id,)
    ) as cursor:
        count = (await cursor.fetchone())[0]
    await db_conn.execute(
        "INSERT INTO buttons (scene_id, message_id, button_order, text, action) VALUES (?, ?, ?, ?, ?)",
        (scene_db_id, message_id, count + 1, text, action)
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

async def get_messages(scene_db_id: int) -> List[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute(
        "SELECT * FROM messages WHERE scene_id = ? ORDER BY message_order", (scene_db_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_buttons(message_id: int) -> List[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute(
        "SELECT * FROM buttons WHERE message_id = ? ORDER BY button_order", (message_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_templates() -> List[Dict]:
    db_conn = await get_db()
    db_conn.row_factory = aiosqlite.Row
    async with db_conn.execute("SELECT * FROM templates") as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def apply_template(bot_id: int, template_id: int):
    db_conn = await get_db()
    async with db_conn.execute("SELECT scenes_json FROM templates WHERE id = ?", (template_id,)) as cursor:
        row = await cursor.fetchone()
        if not row:
            return
    scenes = json.loads(row[0])
    for scene_data in scenes:
        scene_id = scene_data["scene_id"]
        name = scene_data.get("name", scene_id)
        # Создаём сцену
        await db_conn.execute(
            "INSERT INTO scenes (bot_id, scene_id, name) VALUES (?, ?, ?)",
            (bot_id, scene_id, name)
        )
        # Получаем id сцены
        async with db_conn.execute(
            "SELECT id FROM scenes WHERE bot_id = ? AND scene_id = ?", (bot_id, scene_id)
        ) as cur:
            scene_db_id = (await cur.fetchone())[0]

        # Добавляем сообщения
        for msg_text in scene_data.get("messages", []):
            await add_message(scene_db_id, msg_text)

        # Добавляем кнопки (предполагаем, что кнопки привязаны к последнему сообщению)
        # Для простоты добавляем все кнопки к первому сообщению? Лучше - к последнему.
        # В шаблоне может быть несколько сообщений. Для каждого сообщения могут быть свои кнопки.
        # В нашей структуре шаблона кнопки не привязаны к конкретному сообщению, поэтому добавим их к первому.
        # Это упрощение, но для демо сойдёт.
        if scene_data.get("buttons"):
            # Получаем первое сообщение сцены
            async with db_conn.execute(
                "SELECT id FROM messages WHERE scene_id = ? ORDER BY message_order LIMIT 1", (scene_db_id,)
            ) as cur:
                first_msg = await cur.fetchone()
                if first_msg:
                    for btn in scene_data["buttons"]:
                        await add_button(scene_db_id, first_msg[0], btn["text"], btn["action"])
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
        scene = await get_scene_by_scene_id(bot_data['id'], bot_data['start_scene'])
        if not scene:
            await message.answer("Сцена 'start' не найдена.")
            return

        # Получаем сообщения сцены
        messages = await get_messages(scene['id'])

        for msg in messages:
            processed = vm.replace_placeholders(msg['text'], user_vars)

            # Получаем кнопки для этого сообщения
            buttons = await get_buttons(msg['id'])

            keyboard = None
            if buttons:
                kb_buttons = []
                for btn in buttons:
                    kb_buttons.append([InlineKeyboardButton(text=btn['text'], callback_data=f"btn_{btn['id']}")])
                keyboard = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

            await message.answer(processed, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("btn_"))
    async def user_bot_callback(callback: CallbackQuery):
        btn_id = int(callback.data.split("_")[1])
        db_conn = await get_db()
        async with db_conn.execute("SELECT action FROM buttons WHERE id = ?", (btn_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await callback.answer("❌ Действие не найдено")
                return
        action = row[0]

        db_conn = await get_db()
        vm = VariableManager(db_conn, bot_data['id'])
        await vm.load_aliases()

        actions = action.split(';')
        for act in actions:
            act = act.strip()
            if act.startswith('goto:'):
                scene_id = act.replace('goto:', '').strip()
                # Показываем сцену
                scene = await get_scene_by_scene_id(bot_data['id'], scene_id)
                if not scene:
                    await callback.message.answer(f"❌ Сцена '{scene_id}' не найдена")
                    continue

                # Получаем переменные пользователя
                user_vars = {}
                async with db_conn.execute(
                    "SELECT key, value FROM user_data WHERE bot_id = ? AND user_id = ?",
                    (bot_data['id'], callback.from_user.id)
                ) as cursor:
                    rows = await cursor.fetchall()
                    user_vars = {row[0]: row[1] for row in rows}
                user_vars.setdefault("name_user", callback.from_user.first_name)
                user_vars.setdefault("ID_user", str(callback.from_user.id))
                user_vars.setdefault("user_user", callback.from_user.username or "")

                messages = await get_messages(scene['id'])
                for msg in messages:
                    processed = vm.replace_placeholders(msg['text'], user_vars)
                    btns = await get_buttons(msg['id'])
                    keyboard = None
                    if btns:
                        kb_buttons = []
                        for b in btns:
                            kb_buttons.append([InlineKeyboardButton(text=b['text'], callback_data=f"btn_{b['id']}")])
                        keyboard = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
                    await callback.message.answer(processed, reply_markup=keyboard)
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
        [InlineKeyboardButton(text="📂 Шаблоны сцен", callback_data=f"templates_{bot_id}")],
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

def get_scene_management_keyboard(scene_db_id: int, bot_id: int):
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить сообщение", callback_data=f"add_msg_{scene_db_id}")],
        [InlineKeyboardButton(text="🔘 Добавить кнопку", callback_data=f"add_btn_choose_msg_{scene_db_id}")],
        [InlineKeyboardButton(text="👁 Просмотреть сцену", callback_data=f"view_scene_{scene_db_id}")],
        [InlineKeyboardButton(text="🗑 Удалить элементы", callback_data=f"del_elements_{scene_db_id}")],
        [InlineKeyboardButton(text="↩️ Назад к сценам", callback_data=f"edit_scenes_{bot_id}")]
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

# ----- Шаблоны -----
@router.callback_query(F.data.startswith("templates_"))
async def templates_list(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split("_")[1])
    templates = await get_templates()
    if not templates:
        await callback.answer("Нет доступных шаблонов", show_alert=True)
        return

    text = "📂 Выберите шаблон для применения:\n\n"
    keyboard = []
    for t in templates:
        text += f"• {t['name']}: {t['description']}\n"
        keyboard.append([InlineKeyboardButton(
            text=t['name'],
            callback_data=f"apply_template_{bot_id}_{t['id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"select_bot_{bot_id}")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("apply_template_"))
async def apply_template_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    bot_id = int(parts[2])
    template_id = int(parts[3])
    await apply_template(bot_id, template_id)
    await callback.answer("✅ Шаблон применён!", show_alert=True)
    # Возвращаемся к управлению ботом
    bot_data = await get_bot_by_id(bot_id)
    await callback.message.edit_text(
        f"Управление ботом @{bot_data['bot_username']}\n"
        "Шаблон успешно добавлен.",
        reply_markup=get_bot_management_keyboard(bot_id)
    )

# ----- Создание сцены -----
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

# ----- Редактирование сцен (список) -----
@router.callback_query(F.data.startswith("edit_scenes_"))
async def edit_scenes_list(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split("_")[2])
    scenes = await get_bot_scenes(bot_id)

    if not scenes:
        await callback.message.edit_text(
            "У этого бота пока нет сцен. Создайте новую сцену или примените шаблон.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Создать сцену", callback_data=f"create_scene_{bot_id}")],
                [InlineKeyboardButton(text="📂 Шаблоны", callback_data=f"templates_{bot_id}")],
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

# ----- Управление конкретной сценой -----
@router.callback_query(F.data.startswith("edit_scene_"))
async def edit_scene_options(callback: CallbackQuery, state: FSMContext):
    scene_db_id = int(callback.data.split("_")[2])
    scene = await get_scene_by_db_id(scene_db_id)
    if not scene:
        await callback.answer("Сцена не найдена")
        return
    await state.update_data(current_scene_id=scene_db_id, current_bot_id=scene['bot_id'])
    await callback.message.edit_text(
        f"Редактирование сцены: {scene['name']} (ID: {scene['scene_id']})",
        reply_markup=get_scene_management_keyboard(scene_db_id, scene['bot_id'])
    )
    await callback.answer()

# ----- Добавление сообщения -----
@router.callback_query(F.data.startswith("add_msg_"))
async def add_msg_start(callback: CallbackQuery, state: FSMContext):
    scene_db_id = int(callback.data.split("_")[2])
    await state.update_data(current_scene_id=scene_db_id)
    await state.set_state(ConstructorStates.add_message)
    await callback.message.edit_text(
        "➕ Добавление сообщения\n\n"
        "Введите текст сообщения (можно использовать ##переменные##):",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.add_message)
async def add_msg_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    scene_db_id = data.get("current_scene_id")
    text = message.text

    msg_id = await add_message(scene_db_id, text)
    await state.clear()
    scene = await get_scene_by_db_id(scene_db_id)
    await message.answer(
        f"✅ Сообщение добавлено.",
        reply_markup=get_scene_management_keyboard(scene_db_id, scene['bot_id'])
    )

# ----- Добавление кнопки (выбор сообщения) -----
@router.callback_query(F.data.startswith("add_btn_choose_msg_"))
async def add_btn_choose_msg(callback: CallbackQuery, state: FSMContext):
    scene_db_id = int(callback.data.split("_")[3])
    messages = await get_messages(scene_db_id)
    if not messages:
        await callback.answer("❌ Сначала добавьте сообщение", show_alert=True)
        return

    await state.update_data(current_scene_id=scene_db_id)
    text = "Выберите сообщение, к которому добавить кнопку:\n\n"
    keyboard = []
    for msg in messages:
        preview = msg['text'][:30] + "..." if len(msg['text']) > 30 else msg['text']
        keyboard.append([InlineKeyboardButton(
            text=f"📝 {preview}",
            callback_data=f"add_btn_to_msg_{msg['id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"edit_scene_{scene_db_id}")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("add_btn_to_msg_"))
async def add_btn_start(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[3])
    await state.update_data(current_message_id=msg_id)
    await state.set_state(ConstructorStates.add_button)
    await callback.message.edit_text(
        "➕ Добавление кнопки\n\n"
        "Введите данные в формате: Текст кнопки | Действие\n\n"
        "Примеры действий:\n"
        "goto:start\n"
        "stars == 10\n"
        "stars ++ 5\n"
        "rank -- 1\n"
        "Можно комбинировать через ; (например: stars ++ 5;goto:menu)",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.add_button)
async def add_btn_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    msg_id = data.get("current_message_id")
    scene_db_id = data.get("current_scene_id")

    if "|" not in message.text:
        await message.answer("❌ Используйте формат: Текст | Действие")
        return

    btn_text, btn_action = message.text.split("|", 1)
    btn_text = btn_text.strip()
    btn_action = btn_action.strip()

    await add_button(scene_db_id, msg_id, btn_text, btn_action)
    await state.clear()
    scene = await get_scene_by_db_id(scene_db_id)
    await message.answer(
        f"✅ Кнопка добавлена.",
        reply_markup=get_scene_management_keyboard(scene_db_id, scene['bot_id'])
    )

# ----- Просмотр сцены с подстановкой -----
@router.callback_query(F.data.startswith("view_scene_"))
async def view_scene_callback(callback: CallbackQuery):
    scene_db_id = int(callback.data.split("_")[2])
    scene = await get_scene_by_db_id(scene_db_id)
    if not scene:
        await callback.answer("Сцена не найдена")
        return

    db_conn = await get_db()
    vm = VariableManager(db_conn, scene['bot_id'])
    await vm.load_aliases()

    # Получаем переменные пользователя (для примера используем текущего пользователя)
    user_vars = {}
    async with db_conn.execute(
        "SELECT key, value FROM user_data WHERE bot_id = ? AND user_id = ?",
        (scene['bot_id'], callback.from_user.id)
    ) as cursor:
        rows = await cursor.fetchall()
        user_vars = {row[0]: row[1] for row in rows}
    user_vars.setdefault("name_user", callback.from_user.first_name)
    user_vars.setdefault("ID_user", str(callback.from_user.id))
    user_vars.setdefault("user_user", callback.from_user.username or "")

    messages = await get_messages(scene_db_id)
    if not messages:
        await callback.message.edit_text(
            "Сцена не содержит сообщений.",
            reply_markup=get_scene_management_keyboard(scene_db_id, scene['bot_id'])
        )
        await callback.answer()
        return

    text = f"👁 Просмотр сцены: {scene['name']} (ID: {scene['scene_id']})\n\n"
    for msg in messages:
        processed = vm.replace_placeholders(msg['text'], user_vars)
        text += f"📝 Сообщение {msg['message_order']}:\n{processed}\n\n"
        buttons = await get_buttons(msg['id'])
        if buttons:
            text += "Кнопки:\n"
            for btn in buttons:
                text += f"• {btn['text']} → {btn['action']}\n"
            text += "\n"

    await callback.message.edit_text(
        text,
        reply_markup=get_scene_management_keyboard(scene_db_id, scene['bot_id'])
    )
    await callback.answer()

# ----- Удаление элементов -----
@router.callback_query(F.data.startswith("del_elements_"))
async def del_elements_start(callback: CallbackQuery, state: FSMContext):
    scene_db_id = int(callback.data.split("_")[2])
    scene = await get_scene_by_db_id(scene_db_id)
    if not scene:
        await callback.answer("Сцена не найдена")
        return

    messages = await get_messages(scene_db_id)
    if not messages:
        await callback.answer("Сцена пуста, нечего удалять", show_alert=True)
        return

    await state.update_data(current_scene_id=scene_db_id)
    text = "🗑 Выберите элемент для удаления:\n\n"
    keyboard = []

    for msg in messages:
        preview = msg['text'][:20] + "..." if len(msg['text']) > 20 else msg['text']
        keyboard.append([InlineKeyboardButton(
            text=f"🗑 Сообщение {msg['message_order']}: {preview}",
            callback_data=f"del_msg_{msg['id']}"
        )])
        # Кнопки этого сообщения
        btns = await get_buttons(msg['id'])
        for btn in btns:
            keyboard.append([InlineKeyboardButton(
                text=f"  🗑 Кнопка: {btn['text']}",
                callback_data=f"del_btn_{btn['id']}"
            )])

    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"edit_scene_{scene_db_id}")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("del_msg_"))
async def del_msg_callback(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[2])
    await delete_message(msg_id)
    await callback.answer("✅ Сообщение удалено", show_alert=True)
    # Возвращаемся к списку удаления
    data = await state.get_data()
    scene_db_id = data.get("current_scene_id")
    await del_elements_start(callback, state)

@router.callback_query(F.data.startswith("del_btn_"))
async def del_btn_callback(callback: CallbackQuery, state: FSMContext):
    btn_id = int(callback.data.split("_")[2])
    await delete_button(btn_id)
    await callback.answer("✅ Кнопка удалена", show_alert=True)
    data = await state.get_data()
    scene_db_id = data.get("current_scene_id")
    await del_elements_start(callback, state)

# ----- Переменные и алиасы -----
@router.callback_query(F.data.startswith("my_variables_"))
async def my_variables_callback(callback: CallbackQuery):
    bot_id = int(callback.data.split("_")[2])
    db_conn = await get_db()
    vm = VariableManager(db_conn, bot_id)
    await vm.load_aliases()

    # Получаем переменные текущего пользователя для этого бота
    async with db_conn.execute(
        "SELECT key, value FROM user_data WHERE bot_id = ? AND user_id = ?",
        (bot_id, callback.from_user.id)
    ) as cursor:
        rows = await cursor.fetchall()
        user_vars = {row[0]: row[1] for row in rows}

    text = "🔧 Ваши переменные:\n\n"
    if user_vars:
        for k, v in user_vars.items():
            text += f"##{k}## = {v}\n"
    else:
        text += "У вас пока нет переменных.\n"

    if vm.aliases:
        text += "\nАлиасы:\n"
        for alias, val in vm.aliases.items():
            text += f"{alias} = {val}\n"

    keyboard = [
        [InlineKeyboardButton(text="➕ Создать переменную", callback_data=f"create_var_{bot_id}")],
        [InlineKeyboardButton(text="➕ Добавить алиас", callback_data=f"add_alias_{bot_id}")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"select_bot_{bot_id}")]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@router.callback_query(F.data.startswith("create_var_"))
async def create_var_start(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split("_")[2])
    await state.update_data(current_bot_id=bot_id)
    await state.set_state(ConstructorStates.create_variable)
    await callback.message.edit_text(
        "➕ Создание переменной\n\n"
        "Введите выражение в формате: имя == значение\n"
        "Например: stars == 10  или  rank == Veteran",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.create_variable)
async def create_var_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("current_bot_id")
    expr = message.text

    db_conn = await get_db()
    vm = VariableManager(db_conn, bot_id)
    await vm.load_aliases()

    success, result = await vm.process_expression(message.from_user.id, expr)
    if success:
        await message.answer(
            result,
            reply_markup=get_bot_management_keyboard(bot_id)
        )
    else:
        await message.answer(
            result,
            reply_markup=get_back_keyboard()
        )
    await state.clear()

@router.callback_query(F.data.startswith("add_alias_"))
async def add_alias_start(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split("_")[2])
    await state.update_data(current_bot_id=bot_id)
    await state.set_state(ConstructorStates.add_alias)
    await callback.message.edit_text(
        "➕ Добавление алиаса\n\n"
        "Введите в формате: алиас == число\n"
        "Например: Veteran == 2",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.add_alias)
async def add_alias_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("current_bot_id")
    expr = message.text

    if "==" not in expr:
        await message.answer("❌ Используйте формат: алиас == число")
        return

    alias, val_str = expr.split("==", 1)
    alias = alias.strip()
    try:
        value = int(val_str.strip())
    except:
        await message.answer("❌ Число должно быть целым")
        return

    db_conn = await get_db()
    vm = VariableManager(db_conn, bot_id)
    await vm.save_alias(alias, value)

    await message.answer(
        f"✅ Алиас '{alias}' = {value} сохранён.",
        reply_markup=get_bot_management_keyboard(bot_id)
    )
    await state.clear()

# ----- Запуск/остановка бота -----
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

@router.callback_query(F.data.startswith("status_bot_"))
async def status_bot_callback(callback: CallbackQuery):
    bot_id = int(callback.data.split("_")[2])
    bot_data = await get_bot_by_id(bot_id)
    if not bot_data:
        await callback.answer("Бот не найден")
        return

    is_running = bot_data['token'] in user_bots
    scenes = await get_bot_scenes(bot_id)
    text = f"📊 Статус бота @{bot_data['bot_username']}\n\n"
    text += f"• Статус: {'🟢 Запущен' if is_running else '🔴 Остановлен'}\n"
    text += f"• Сцен: {len(scenes)}\n"
    if scenes:
        text += "\nСцены:\n"
        for s in scenes:
            msgs = await get_messages(s['id'])
            btns = 0
            for m in msgs:
                btns += len(await get_buttons(m['id']))
            text += f"• {s['scene_id']} ({len(msgs)} сообщ., {btns} кнопок)\n"

    keyboard = [[InlineKeyboardButton(text="↩️ Назад", callback_data=f"select_bot_{bot_id}")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

# ----- Помощь -----
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

**Шаблоны**
• Готовые наборы сцен для быстрого старта.
• Выберите "Шаблоны сцен" в меню бота.

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

**Редактирование**
• В сцене можно добавлять/удалять сообщения и кнопки.
• Используйте кнопки управления сценой.
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
    await start_all_user_bots()

    asyncio.create_task(web_server())
    await asyncio.sleep(1)

    logger.info("Constructor bot started polling")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
