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

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set")

PORT = int(os.getenv("PORT", 8000))

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# База данных
DB_NAME = "bot_constructor.db"

# FSM состояния
class ConstructorStates(StatesGroup):
    main_menu = State()
    create_scene = State()
    edit_scene = State()
    add_message = State()
    add_button = State()
    edit_variables = State()
    delete_elements = State()
    create_variable = State()

# Класс для работы с переменными
class VariableManager:
    def __init__(self, db):
        self.db = db
        self.aliases = {}  # Псевдонимы: {"Veteran": 2, "Rang 1": 1}
    
    async def load_aliases(self):
        """Загрузка алиасов из БД"""
        async with self.db.execute("SELECT alias, value FROM aliases") as cursor:
            rows = await cursor.fetchall()
            self.aliases = {row[0]: int(row[1]) for row in rows}
    
    async def save_alias(self, alias: str, value: int):
        """Сохранение алиаса в БД"""
        await self.db.execute(
            "INSERT OR REPLACE INTO aliases (alias, value) VALUES (?, ?)",
            (alias, value)
        )
        await self.db.commit()
        self.aliases[alias] = value
    
    async def get_user_variable(self, user_id: int, key: str) -> Optional[str]:
        """Получение переменной пользователя"""
        async with self.db.execute(
            "SELECT value FROM user_data WHERE user_id = ? AND key = ?",
            (user_id, key)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
    
    async def set_user_variable(self, user_id: int, key: str, value: str):
        """Установка переменной пользователя"""
        await self.db.execute(
            "INSERT OR REPLACE INTO user_data (user_id, key, value) VALUES (?, ?, ?)",
            (user_id, key, value)
        )
        await self.db.commit()
    
    async def process_expression(self, user_id: int, expression: str) -> Tuple[bool, str]:
        """Обработка математических выражений"""
        try:
            expression = expression.strip()
            
            # Присваивание
            if "==" in expression:
                parts = expression.split("==", 1)
                if len(parts) == 2:
                    var_name = parts[0].strip()
                    value = parts[1].strip()
                    
                    # Проверяем алиас
                    if value in self.aliases:
                        value = str(self.aliases[value])
                    
                    await self.set_user_variable(user_id, var_name, value)
                    return True, f"✅ {var_name} = {value}"
            
            # Сложение
            elif "++" in expression:
                parts = expression.split("++", 1)
                if len(parts) == 2:
                    var_name = parts[0].strip()
                    increment = parts[1].strip()
                    
                    current = await self.get_user_variable(user_id, var_name)
                    
                    # Преобразуем текущее значение в число
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
                    
                    # Ищем алиас для нового числа
                    new_value = str(new_num)
                    for alias, val in self.aliases.items():
                        if val == new_num:
                            new_value = alias
                            break
                    
                    await self.set_user_variable(user_id, var_name, new_value)
                    return True, f"✅ {var_name} увеличен на {increment}. Новое значение: {new_value}"
            
            # Вычитание
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
        """Замена плейсхолдеров в тексте"""
        if not text:
            return text
        
        def replace(match):
            placeholder = match.group(1)
            if placeholder in user_data:
                return str(user_data[placeholder])
            return match.group(0)
        
        return re.sub(r'##(\w+)##', replace, text)

async def init_db():
    """Инициализация базы данных"""
    db = await aiosqlite.connect(DB_NAME)
    
    await db.execute('''CREATE TABLE IF NOT EXISTS scenes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id TEXT UNIQUE,
        name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    await db.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id TEXT,
        message_order INTEGER,
        text TEXT,
        media_type TEXT,
        media_id TEXT,
        FOREIGN KEY (scene_id) REFERENCES scenes(scene_id) ON DELETE CASCADE
    )''')
    
    await db.execute('''CREATE TABLE IF NOT EXISTS buttons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id TEXT,
        message_id INTEGER,
        button_order INTEGER,
        text TEXT,
        action TEXT,
        FOREIGN KEY (scene_id) REFERENCES scenes(scene_id) ON DELETE CASCADE,
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
    )''')
    
    await db.execute('''CREATE TABLE IF NOT EXISTS user_data (
        user_id INTEGER,
        key TEXT,
        value TEXT,
        PRIMARY KEY (user_id, key)
    )''')
    
    await db.execute('''CREATE TABLE IF NOT EXISTS aliases (
        alias TEXT PRIMARY KEY,
        value INTEGER
    )''')
    
    await db.commit()
    
    # Инициализация менеджера переменных
    variable_manager = VariableManager(db)
    await variable_manager.load_aliases()
    
    return db, variable_manager

# Глобальные переменные для БД
db = None
variable_manager = None

async def get_db():
    global db, variable_manager
    if db is None:
        db, variable_manager = await init_db()
    return db, variable_manager

# Клавиатуры
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="📝 Создать сцену", callback_data="create_scene")],
        [InlineKeyboardButton(text="✏️ Редактировать сцену", callback_data="edit_scene")],
        [InlineKeyboardButton(text="🔧 Мои переменные", callback_data="my_variables")],
        [InlineKeyboardButton(text="➕ Создать переменную", callback_data="create_variable")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_back_keyboard():
    keyboard = [[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# Хендлеры
@router.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start с вотермаркой"""
    await message.answer(
        "⚒️ Бот создан с помощью @KneoFreeBot\n\n"
        "Добро пожаловать в конструктор ботов! "
        "Используйте кнопки ниже для управления ботом.",
        reply_markup=get_main_keyboard()
    )
    
    # Сохраняем базовые переменные
    _, vm = await get_db()
    await vm.set_user_variable(message.from_user.id, "name_user", message.from_user.first_name)
    await vm.set_user_variable(message.from_user.id, "ID_user", str(message.from_user.id))
    await vm.set_user_variable(message.from_user.id, "user_user", message.from_user.username or "")

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await callback.message.edit_text(
        "Главное меню конструктора ботов:",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "create_scene")
async def create_scene_start(callback: CallbackQuery, state: FSMContext):
    """Начало создания сцены"""
    await state.set_state(ConstructorStates.create_scene)
    await callback.message.edit_text(
        "📝 Создание новой сцены\n\n"
        "Введите ID сцены (латинскими буквами, без пробелов):\n"
        "Пример: start, menu, profile",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.create_scene)
async def create_scene_finish(message: Message, state: FSMContext):
    """Завершение создания сцены"""
    scene_id = message.text.strip()
    
    # Проверка ID
    if not re.match(r'^[a-zA-Z0-9_]+$', scene_id):
        await message.answer(
            "❌ ID сцены может содержать только латинские буквы, цифры и подчеркивания.",
            reply_markup=get_back_keyboard()
        )
        return
    
    db_conn, _ = await get_db()
    
    # Проверка существования сцены
    async with db_conn.execute("SELECT scene_id FROM scenes WHERE scene_id = ?", (scene_id,)) as cursor:
        if await cursor.fetchone():
            await message.answer(
                f"❌ Сцена с ID '{scene_id}' уже существует.",
                reply_markup=get_back_keyboard()
            )
            return
    
    # Создание сцены
    await db_conn.execute(
        "INSERT INTO scenes (scene_id, name) VALUES (?, ?)",
        (scene_id, f"Сцена {scene_id}")
    )
    await db_conn.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Сцена '{scene_id}' успешно создана!\n"
        "Теперь вы можете добавить сообщения и кнопки к ней.",
        reply_markup=get_main_keyboard()
    )

@router.callback_query(F.data == "edit_scene")
async def edit_scene_select(callback: CallbackQuery, state: FSMContext):
    """Выбор сцены для редактирования"""
    db_conn, _ = await get_db()
    
    async with db_conn.execute("SELECT scene_id, name FROM scenes") as cursor:
        scenes = await cursor.fetchall()
    
    if not scenes:
        await callback.message.edit_text(
            "❌ Нет созданных сцен. Сначала создайте сцену.",
            reply_markup=get_back_keyboard()
        )
        await callback.answer()
        return
    
    keyboard = []
    for scene_id, name in scenes:
        keyboard.append([InlineKeyboardButton(
            text=f"📄 {name} ({scene_id})", 
            callback_data=f"edit_scene_{scene_id}"
        )])
    
    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")])
    
    await callback.message.edit_text(
        "✏️ Выберите сцену для редактирования:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("edit_scene_"))
async def edit_scene_options(callback: CallbackQuery, state: FSMContext):
    """Опции редактирования сцены"""
    scene_id = callback.data.replace("edit_scene_", "")
    await state.update_data(edit_scene_id=scene_id)
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Добавить сообщение", callback_data=f"add_msg_{scene_id}")],
        [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data=f"add_btn_{scene_id}")],
        [InlineKeyboardButton(text="🗑 Удалить элементы", callback_data=f"del_elems_{scene_id}")],
        [InlineKeyboardButton(text="👁 Просмотреть сцену", callback_data=f"view_scene_{scene_id}")],
        [InlineKeyboardButton(text="↩️ Назад к списку", callback_data="edit_scene")]
    ]
    
    await callback.message.edit_text(
        f"✏️ Редактирование сцены: {scene_id}\n\n"
        "Выберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("add_msg_"))
async def add_message_start(callback: CallbackQuery, state: FSMContext):
    """Добавление сообщения к сцене"""
    scene_id = callback.data.replace("add_msg_", "")
    await state.set_state(ConstructorStates.add_message)
    await state.update_data(scene_id=scene_id)
    
    await callback.message.edit_text(
        f"➕ Добавление сообщения к сцене: {scene_id}\n\n"
        "Введите текст сообщения (можно использовать ##переменные##):\n\n"
        "Доступные переменные:\n"
        "##name_user## - имя пользователя\n"
        "##ID_user## - ID пользователя\n"
        "##user_user## - юзернейм\n"
        "##любая_ваша_переменная## - ваши переменные",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.add_message)
async def add_message_finish(message: Message, state: FSMContext):
    """Сохранение сообщения в сцену"""
    data = await state.get_data()
    scene_id = data.get("scene_id")
    text = message.text
    
    db_conn, _ = await get_db()
    
    # Определяем порядковый номер сообщения
    async with db_conn.execute(
        "SELECT COUNT(*) FROM messages WHERE scene_id = ?",
        (scene_id,)
    ) as cursor:
        count = (await cursor.fetchone())[0]
    
    # Сохранение сообщения
    await db_conn.execute(
        "INSERT INTO messages (scene_id, message_order, text, media_type) VALUES (?, ?, ?, ?)",
        (scene_id, count + 1, text, "text")
    )
    await db_conn.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Сообщение добавлено в сцену '{scene_id}'!\n"
        "Порядковый номер: " + str(count + 1),
        reply_markup=get_main_keyboard()
    )

@router.callback_query(F.data.startswith("add_btn_"))
async def add_button_start(callback: CallbackQuery, state: FSMContext):
    """Добавление кнопки к сцене"""
    scene_id = callback.data.replace("add_btn_", "")
    await state.set_state(ConstructorStates.add_button)
    await state.update_data(scene_id=scene_id)
    
    # Получаем список сообщений в сцене
    db_conn, _ = await get_db()
    
    async with db_conn.execute(
        "SELECT id, text FROM messages WHERE scene_id = ? ORDER BY message_order",
        (scene_id,)
    ) as cursor:
        messages = await cursor.fetchall()
    
    if not messages:
        await callback.answer("❌ В сцене нет сообщений для добавления кнопок!", show_alert=True)
        return
    
    keyboard = []
    for msg_id, msg_text in messages:
        preview = msg_text[:30] + "..." if len(msg_text) > 30 else msg_text
        keyboard.append([InlineKeyboardButton(
            text=f"📝 {preview}", 
            callback_data=f"select_msg_{msg_id}"
        )])
    
    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"edit_scene_{scene_id}")])
    
    await callback.message.edit_text(
        f"➕ Добавление кнопки к сцене: {scene_id}\n\n"
        "Выберите сообщение, к которому добавить кнопку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("select_msg_"))
async def select_message_for_button(callback: CallbackQuery, state: FSMContext):
    """Выбор сообщения для кнопки"""
    msg_id = int(callback.data.replace("select_msg_", ""))
    await state.update_data(message_id=msg_id)
    
    await callback.message.edit_text(
        "✏️ Введите данные для кнопки в формате:\n\n"
        "Текст кнопки | Действие\n\n"
        "Примеры действий:\n"
        "• goto:start - переход на сцену 'start'\n"
        "• stars == 10 - установить переменную 'stars' в 10\n"
        "• rank == Veteran - установить переменную 'rank' в 'Veteran'\n"
        "• stars ++ 5 - увеличить 'stars' на 5\n"
        "• rank -- 1 - уменьшить 'rank' на 1\n\n"
        "Можно комбинировать несколько действий через ;\n"
        "Пример: stars ++ 5;goto:menu",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.add_button)
async def add_button_finish(message: Message, state: FSMContext):
    """Сохранение кнопки"""
    data = await state.get_data()
    scene_id = data.get("scene_id")
    msg_id = data.get("message_id")
    
    if "|" not in message.text:
        await message.answer("❌ Используйте формат: 'Текст кнопки | Действие'")
        return
    
    button_text, button_action = message.text.split("|", 1)
    button_text = button_text.strip()
    button_action = button_action.strip()
    
    db_conn, _ = await get_db()
    
    # Определяем порядковый номер кнопки
    async with db_conn.execute(
        "SELECT COUNT(*) FROM buttons WHERE message_id = ?",
        (msg_id,)
    ) as cursor:
        count = (await cursor.fetchone())[0]
    
    # Сохранение кнопки
    await db_conn.execute(
        "INSERT INTO buttons (scene_id, message_id, button_order, text, action) VALUES (?, ?, ?, ?, ?)",
        (scene_id, msg_id, count + 1, button_text, button_action)
    )
    await db_conn.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Кнопка '{button_text}' добавлена!\n"
        f"Действие: {button_action}",
        reply_markup=get_main_keyboard()
    )

@router.callback_query(F.data.startswith("del_elems_"))
async def delete_elements_start(callback: CallbackQuery, state: FSMContext):
    """Удаление элементов сцены"""
    scene_id = callback.data.replace("del_elems_", "")
    await state.set_state(ConstructorStates.delete_elements)
    await state.update_data(scene_id=scene_id)
    
    db_conn, _ = await get_db()
    
    # Получаем сообщения сцены
    async with db_conn.execute(
        """SELECT m.id, m.message_order, m.text, 
                  COUNT(b.id) as button_count 
           FROM messages m 
           LEFT JOIN buttons b ON m.id = b.message_id 
           WHERE m.scene_id = ? 
           GROUP BY m.id 
           ORDER BY m.message_order""",
        (scene_id,)
    ) as cursor:
        messages = await cursor.fetchall()
    
    if not messages:
        await callback.message.edit_text(
            f"❌ В сцене '{scene_id}' нет элементов для удаления.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад", callback_data=f"edit_scene_{scene_id}")]
            ])
        )
        await callback.answer()
        return
    
    keyboard = []
    for msg_id, msg_order, msg_text, btn_count in messages:
        preview = msg_text[:20] + "..." if len(msg_text) > 20 else msg_text
        keyboard.append([InlineKeyboardButton(
            text=f"🗑 Сообщение {msg_order}: {preview} ({btn_count} кнопок)", 
            callback_data=f"del_msg_{msg_id}"
        )])
    
    # Получаем все кнопки отдельно
    async with db_conn.execute(
        """SELECT b.id, b.button_order, b.text, m.message_order 
           FROM buttons b 
           JOIN messages m ON b.message_id = m.id 
           WHERE b.scene_id = ? 
           ORDER BY m.message_order, b.button_order""",
        (scene_id,)
    ) as cursor:
        buttons = await cursor.fetchall()
    
    for btn_id, btn_order, btn_text, msg_order in buttons:
        keyboard.append([InlineKeyboardButton(
            text=f"🗑 Кнопка {btn_order} (на сообщ. {msg_order}): {btn_text}", 
            callback_data=f"del_btn_{btn_id}"
        )])
    
    keyboard.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"edit_scene_{scene_id}")])
    
    await callback.message.edit_text(
        f"🗑 Удаление элементов сцены: {scene_id}\n\n"
        "Выберите элемент для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("del_msg_"))
async def delete_message(callback: CallbackQuery, state: FSMContext):
    """Удаление сообщения"""
    msg_id = int(callback.data.replace("del_msg_", ""))
    
    db_conn, _ = await get_db()
    
    # Удаляем сообщение (кнопки удалятся каскадно)
    await db_conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    await db_conn.commit()
    
    await callback.answer("✅ Сообщение и все его кнопки удалены!", show_alert=True)
    
    # Обновляем список элементов
    data = await state.get_data()
    scene_id = data.get("scene_id")
    
    # Возвращаемся к списку элементов
    await delete_elements_start(callback, state)

@router.callback_query(F.data.startswith("del_btn_"))
async def delete_button(callback: CallbackQuery, state: FSMContext):
    """Удаление кнопки"""
    btn_id = int(callback.data.replace("del_btn_", ""))
    
    db_conn, _ = await get_db()
    
    # Удаляем кнопку
    await db_conn.execute("DELETE FROM buttons WHERE id = ?", (btn_id,))
    await db_conn.commit()
    
    await callback.answer("✅ Кнопка удалена!", show_alert=True)
    
    # Обновляем список элементов
    data = await state.get_data()
    scene_id = data.get("scene_id")
    
    # Возвращаемся к списку элементов
    await delete_elements_start(callback, state)

@router.callback_query(F.data.startswith("view_scene_"))
async def view_scene(callback: CallbackQuery):
    """Просмотр сцены"""
    scene_id = callback.data.replace("view_scene_", "")
    user_id = callback.from_user.id
    
    db_conn, vm = await get_db()
    
    # Получаем переменные пользователя
    user_data = {}
    async with db_conn.execute(
        "SELECT key, value FROM user_data WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        user_data = {row[0]: row[1] for row in rows}
    
    # Добавляем системные переменные, если их нет
    if "name_user" not in user_data:
        user_data["name_user"] = callback.from_user.first_name
    if "ID_user" not in user_data:
        user_data["ID_user"] = str(callback.from_user.id)
    if "user_user" not in user_data:
        user_data["user_user"] = callback.from_user.username or ""
    
    # Получаем сообщения сцены
    async with db_conn.execute(
        "SELECT id, text FROM messages WHERE scene_id = ? ORDER BY message_order",
        (scene_id,)
    ) as cursor:
        messages = await cursor.fetchall()
    
    if not messages:
        await callback.message.edit_text(
            f"❌ Сцена '{scene_id}' не содержит сообщений.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад", callback_data=f"edit_scene_{scene_id}")]
            ])
        )
        await callback.answer()
        return
    
    # Формируем сообщение с просмотром
    view_text = f"👁 Просмотр сцены: {scene_id}\n\n"
    
    for idx, (msg_id, msg_text) in enumerate(messages, 1):
        # Заменяем плейсхолдеры
        processed_text = vm.replace_placeholders(msg_text, user_data)
        view_text += f"📝 Сообщение {idx}:\n{processed_text}\n\n"
        
        # Получаем кнопки для этого сообщения
        async with db_conn.execute(
            "SELECT text, action FROM buttons WHERE message_id = ? ORDER BY button_order",
            (msg_id,)
        ) as cursor:
            buttons = await cursor.fetchall()
        
        if buttons:
            view_text += "Кнопки:\n"
            for btn_text, btn_action in buttons:
                view_text += f"• {btn_text} → {btn_action}\n"
            view_text += "\n"
    
    # Добавляем клавиатуру для перехода к редактированию
    keyboard = [
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_scene_{scene_id}")],
        [InlineKeyboardButton(text="↩️ Главное меню", callback_data="back_to_main")]
    ]
    
    await callback.message.edit_text(
        view_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data == "my_variables")
async def show_my_variables(callback: CallbackQuery):
    """Показать переменные пользователя"""
    user_id = callback.from_user.id
    
    db_conn, vm = await get_db()
    
    # Получаем переменные пользователя
    async with db_conn.execute(
        "SELECT key, value FROM user_data WHERE user_id = ? ORDER BY key",
        (user_id,)
    ) as cursor:
        variables = await cursor.fetchall()
    
    # Получаем системные переменные
    user_data = {}
    async with db_conn.execute(
        "SELECT key, value FROM user_data WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        user_data = {row[0]: row[1] for row in rows}
    
    # Добавляем базовые переменные, если их нет
    if "name_user" not in user_data:
        user_data["name_user"] = callback.from_user.first_name
    if "ID_user" not in user_data:
        user_data["ID_user"] = str(callback.from_user.id)
    if "user_user" not in user_data:
        user_data["user_user"] = callback.from_user.username or ""
    
    text = "🔧 Мои переменные:\n\n"
    
    # Системные переменные
    text += "Системные переменные:\n"
    text += f"##name_user## = {user_data.get('name_user')}\n"
    text += f"##ID_user## = {user_data.get('ID_user')}\n"
    text += f"##user_user## = {user_data.get('user_user')}\n\n"
    
    # Пользовательские переменные
    if variables:
        text += "Пользовательские переменные:\n"
        for key, value in variables:
            text += f"##{key}## = {value}\n"
    else:
        text += "❌ У вас нет пользовательских переменных.\n"
    
    # Алиасы
    if vm.aliases:
        text += "\nАлиасы (псевдонимы):\n"
        for alias, val in vm.aliases.items():
            text += f"{alias} = {val}\n"
    
    keyboard = [
        [InlineKeyboardButton(text="➕ Создать переменную", callback_data="create_variable")],
        [InlineKeyboardButton(text="➕ Добавить алиас", callback_data="add_alias")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
    ]
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data == "create_variable")
async def create_variable_start(callback: CallbackQuery, state: FSMContext):
    """Создание переменной"""
    await state.set_state(ConstructorStates.create_variable)
    
    await callback.message.edit_text(
        "➕ Создание переменной\n\n"
        "Введите данные в формате:\n"
        "ИмяПеременной == Значение\n\n"
        "Примеры:\n"
        "stars == 10\n"
        "rank == Veteran\n"
        "coins == 1000",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.create_variable)
async def create_variable_finish(message: Message, state: FSMContext):
    """Сохранение переменной"""
    user_id = message.from_user.id
    expression = message.text
    
    db_conn, vm = await get_db()
    
    success, result = await vm.process_expression(user_id, expression)
    
    if success:
        await message.answer(
            result + "\n\n"
            "Теперь вы можете использовать эту переменную в тексте как ##имя_переменной##",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            result + "\n\n"
            "Попробуйте еще раз.",
            reply_markup=get_back_keyboard()
        )
    
    await state.clear()

@router.callback_query(F.data == "add_alias")
async def add_alias_start(callback: CallbackQuery, state: FSMContext):
    """Добавление алиаса"""
    await state.set_state(ConstructorStates.edit_variables)
    
    await callback.message.edit_text(
        "➕ Добавление алиаса (псевдонима)\n\n"
        "Введите данные в формате:\n"
        "Алиас == ЧисловоеЗначение\n\n"
        "Примеры:\n"
        "Veteran == 3\n"
        "Rang 1 == 1\n"
        "Новичок == 0\n\n"
        "После этого можно использовать алиасы в операциях:\n"
        "rank == Veteran\n"
        "rank -- 1  (получится Rang 1)",
        reply_markup=get_back_keyboard()
    )
    await callback.answer()

@router.message(ConstructorStates.edit_variables)
async def add_alias_finish(message: Message, state: FSMContext):
    """Сохранение алиаса"""
    expression = message.text
    
    if "==" not in expression:
        await message.answer("❌ Используйте формат: Алиас == Число")
        return
    
    parts = expression.split("==", 1)
    alias = parts[0].strip()
    value_str = parts[1].strip()
    
    try:
        value = int(value_str)
    except ValueError:
        await message.answer("❌ Значение должно быть целым числом")
        return
    
    db_conn, vm = await get_db()
    await vm.save_alias(alias, value)
    
    await message.answer(
        f"✅ Алиас '{alias}' = {value} успешно сохранен!\n\n"
        "Теперь вы можете использовать его в операциях с переменными.",
        reply_markup=get_main_keyboard()
    )
    
    await state.clear()

@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    """Показать помощь"""
    help_text = """
📚 **ПОМОЩЬ ПО КОНСТРУКТОРУ БОТОВ**

🎭 **СЦЕНЫ:**
• *Создание сцены:* Используйте кнопку "Создать сцену", введите уникальный ID
• *Несколько сообщений:* В одной сцене может быть несколько сообщений - они будут отправляться последовательно
• *Удаление:* В режиме редактирования сцены используйте "Удалить элементы" для удаления сообщений и кнопок

👤 **ПЕРЕМЕННЫЕ ПОЛЬЗОВАТЕЛЯ:**
Доступны автоматически:
• `##name_user##` - имя пользователя
• `##ID_user##` - ID пользователя в Telegram
• `##user_user##` - юзернейм (@username)

➕ **СВОИ ПЕРЕМЕННЫЕ:**
• Создавайте через "Создать переменную"
• Формат: `имя_переменной == значение`
• Пример: `coins == 100` или `rank == Новичок`
• Используйте в тексте как `##coins##` или `##rank##`

🔢 **МАТЕМАТИКА В КНОПКАХ:**
В действии кнопки можно использовать:
• *Присваивание:* `[Переменная] == [Значение]`
  Пример: `stars == 10` или `rank == Veteran`
• *Сложение:* `[Переменная] ++ [Число]`
  Пример: `stars ++ 5`
• *Вычитание:* `[Переменная] -- [Число]`
  Пример: `stars -- 2`
• *Комбинации:* `stars ++ 5;goto:menu`

🔄 **УМНЫЕ АЛИАСЫ:**
• Алиас - это текстовое представление числа
• Пример: Veteran=2, Rang 1=1
• При операции `rank == Veteran` переменная сохранит "Veteran"
• При операции `rank -- 1` получится "Rang 1"
• Добавляйте алиасы через "Мои переменные" → "Добавить алиас"

🎯 **ПРИМЕР СИСТЕМЫ РАНГОВ:**
1. Создайте алиасы: 
    Новичок == 0
    Rang 1 == 1
    Veteran == 2
    Elite == 3
2. Создайте переменную:
    rank == Новичок
3. В кнопке для повышения:
    Действие: rank ++ 1
    При нажатии: Новичок → Rang 1 → Veteran → Elite
4. В кнопке для понижения:
    Действие: rank -- 1
    При нажатии: Elite → Veteran → Rang 1 → Новичок

📝 **ПРИМЕР ТЕКСТА С ПЕРЕМЕННЫМИ:**
    Привет, ##name_user##!
    Твой ранг: ##rank##
    Баланс: ##coins## монет
    ID: ##ID_user##

🛠 **ТЕХНИЧЕСКАЯ ИНФОРМАЦИЯ:**
• Медиа (фото/видео) временно недоступны
• Все данные хранятся в SQLite базе
• Для работы на Render требуется веб-сервер
"""
    
    keyboard = [[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]]
    
    await callback.message.edit_text(
        help_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query()
async def handle_button_click(callback: CallbackQuery):
    """Обработка нажатий на кнопки в сценах"""
    user_id = callback.from_user.id
    button_data = callback.data
    
    db_conn, vm = await get_db()
    
    # Получаем действие кнопки
    async with db_conn.execute(
        "SELECT action FROM buttons WHERE text = ? OR id = ? LIMIT 1",
        (button_data, button_data)
    ) as cursor:
        result = await cursor.fetchone()
    
    if not result:
        # Пробуем найти кнопку по тексту (если callback содержит текст кнопки)
        async with db_conn.execute(
            "SELECT action FROM buttons WHERE text = ? LIMIT 1",
            (button_data,)
        ) as cursor:
            result = await cursor.fetchone()
    
    if not result:
        await callback.answer("❌ Действие не найдено")
        return
    
    action = result[0]
    
    # Обрабатываем действие
    actions = action.split(';')
    
    for act in actions:
        act = act.strip()
        
        # Проверяем на переход
        if act.startswith('goto:'):
            scene_id = act.replace('goto:', '').strip()
            await show_scene(user_id, scene_id, callback.message)
        
        # Проверяем на выражение
        else:
            success, result_msg = await vm.process_expression(user_id, act)
            if not success:
                await callback.answer(f"❌ Ошибка: {result_msg}")
    
    await callback.answer("✅ Действие выполнено")

async def show_scene(user_id: int, scene_id: str, message_obj: Message = None):
    """Показать сцену пользователю"""
    db_conn, vm = await get_db()
    
    # Получаем переменные пользователя
    user_data = {}
    async with db_conn.execute(
        "SELECT key, value FROM user_data WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        user_data = {row[0]: row[1] for row in rows}
    
    # Добавляем системные переменные
    user_data.setdefault("name_user", "Пользователь")
    user_data.setdefault("ID_user", str(user_id))
    user_data.setdefault("user_user", "")
    
    # Получаем сообщения сцены
    async with db_conn.execute(
        "SELECT id, text FROM messages WHERE scene_id = ? ORDER BY message_order",
        (scene_id,)
    ) as cursor:
        messages = await cursor.fetchall()
    
    if not messages:
        if message_obj:
            await message_obj.answer(f"❌ Сцена '{scene_id}' не найдена")
        return
    
    # Отправляем каждое сообщение
    for msg_id, msg_text in messages:
        processed_text = vm.replace_placeholders(msg_text, user_data)
        
        # Получаем кнопки для этого сообщения
        async with db_conn.execute(
            "SELECT text, action FROM buttons WHERE message_id = ? ORDER BY button_order",
            (msg_id,)
        ) as cursor:
            buttons = await cursor.fetchall()
        
        # Создаем клавиатуру
        keyboard = []
        for btn_text, btn_action in buttons:
            callback_data = btn_text  # Используем текст кнопки как callback
            keyboard.append([InlineKeyboardButton(
                text=btn_text,
                callback_data=callback_data
            )])
        
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None
        
        if message_obj:
            await message_obj.answer(processed_text, reply_markup=reply_markup)

# Веб-сервер для Render
async def web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Bot is running"))
    app.router.add_get('/health', lambda request: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")
    # Keep the server running
    await asyncio.Event().wait()

async def main():
    """Основная функция"""
    # Инициализация БД
    await get_db()
    
    # Запуск веб-сервера в фоне
    asyncio.create_task(web_server())
    # Даём время серверу запуститься
    await asyncio.sleep(1)
    
    # Запуск бота
    logger.info("Bot started polling")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
