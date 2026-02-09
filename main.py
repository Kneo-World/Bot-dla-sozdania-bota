import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set, Any

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    PhotoSize, Video
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType

import aiosqlite
import aiohttp
from aiohttp import web

# ========== НАСТРОЙКА ЛОГГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в переменных окружения")

PORT = int(os.getenv('PORT', 10000))

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
user_bots: Dict[str, Tuple[Bot, Dispatcher, asyncio.Task]] = {}  # token -> (Bot, Dispatcher, Task)
WATERMARK = "⚒️ Бот создан с помощью @KneoFreeBot\n\n"

# ========== ОСНОВНОЙ БОТ И ДИСПЕТЧЕР ==========
main_bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
main_dp = Dispatcher(storage=MemoryStorage())
main_router = Router()
main_dp.include_router(main_router)

# ========== СОСТОЯНИЯ FSM ==========
class BotConstructorStates(StatesGroup):
    waiting_for_token = State()
    waiting_scene_name = State()
    waiting_content_type = State()
    waiting_scene_text = State()
    waiting_scene_photo = State()
    waiting_scene_video = State()
    waiting_scene_caption = State()
    waiting_button_type = State()
    waiting_button_text = State()
    waiting_button_url = State()
    waiting_button_target_scene = State()
    editing_scene = State()
    waiting_edit_content = State()
    waiting_edit_caption = State()

# ========== БАЗА ДАННЫХ ==========
async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect('database.db') as db:
        # Таблица для пользователей конструктора
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица для ботов пользователей
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                bot_token TEXT UNIQUE,
                bot_username TEXT,
                is_active BOOLEAN DEFAULT 1,
                start_scene TEXT DEFAULT 'start',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица для сцен с поддержкой медиа
        await db.execute('''
            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                name TEXT,
                content_type TEXT DEFAULT 'text',  -- text, photo, video
                file_id TEXT,  -- file_id для медиа
                caption TEXT,  -- подпись для медиа
                buttons_json TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(bot_id, name),
                FOREIGN KEY (bot_id) REFERENCES user_bots (id)
            )
        ''')
        
        await db.commit()
        logger.info("База данных инициализирована")

async def save_user(user_id: int):
    """Сохранение пользователя в БД"""
    async with aiosqlite.connect('database.db') as db:
        await db.execute(
            'INSERT OR IGNORE INTO users (user_id) VALUES (?)',
            (user_id,)
        )
        await db.commit()

async def save_bot_token(user_id: int, token: str, bot_username: str):
    """Сохранение токена бота пользователя"""
    async with aiosqlite.connect('database.db') as db:
        # Удаляем старый токен, если есть
        await db.execute(
            'DELETE FROM user_bots WHERE user_id = ?',
            (user_id,)
        )
        
        # Сохраняем новый
        await db.execute('''
            INSERT INTO user_bots (user_id, bot_token, bot_username, start_scene)
            VALUES (?, ?, ?, ?)
        ''', (user_id, token, bot_username, 'start'))
        
        await db.commit()

async def get_user_bot(user_id: int) -> Optional[Dict]:
    """Получение информации о боте пользователя"""
    async with aiosqlite.connect('database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM user_bots WHERE user_id = ?
        ''', (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def get_bot_by_token(token: str) -> Optional[Dict]:
    """Получение настроек бота по токену"""
    async with aiosqlite.connect('database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM user_bots WHERE bot_token = ?
        ''', (token,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def get_bot_scenes(bot_id: int) -> List[Dict]:
    """Получение всех сцен бота"""
    async with aiosqlite.connect('database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM scenes WHERE bot_id = ? ORDER BY created_at
        ''', (bot_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_scene(bot_id: int, scene_name: str) -> Optional[Dict]:
    """Получение конкретной сцены"""
    async with aiosqlite.connect('database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM scenes WHERE bot_id = ? AND name = ?
        ''', (bot_id, scene_name))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def save_scene(bot_id: int, scene_name: str, content_type: str, file_id: str = None, caption: str = None):
    """Сохранение сцены"""
    async with aiosqlite.connect('database.db') as db:
        await db.execute('''
            INSERT OR REPLACE INTO scenes (bot_id, name, content_type, file_id, caption)
            VALUES (?, ?, ?, ?, ?)
        ''', (bot_id, scene_name, content_type, file_id, caption))
        await db.commit()

async def update_scene_content(bot_id: int, scene_name: str, content_type: str = None, file_id: str = None, caption: str = None):
    """Обновление контента сцены"""
    async with aiosqlite.connect('database.db') as db:
        if content_type:
            await db.execute('''
                UPDATE scenes SET content_type = ? WHERE bot_id = ? AND name = ?
            ''', (content_type, bot_id, scene_name))
        if file_id:
            await db.execute('''
                UPDATE scenes SET file_id = ? WHERE bot_id = ? AND name = ?
            ''', (file_id, bot_id, scene_name))
        if caption is not None:
            await db.execute('''
                UPDATE scenes SET caption = ? WHERE bot_id = ? AND name = ?
            ''', (caption, bot_id, scene_name))
        await db.commit()

async def update_scene_buttons(bot_id: int, scene_name: str, buttons: List[Dict]):
    """Обновление кнопок сцены"""
    buttons_json = json.dumps(buttons, ensure_ascii=False)
    async with aiosqlite.connect('database.db') as db:
        await db.execute('''
            UPDATE scenes SET buttons_json = ? WHERE bot_id = ? AND name = ?
        ''', (buttons_json, bot_id, scene_name))
        await db.commit()

async def delete_scene(bot_id: int, scene_name: str):
    """Удаление сцены"""
    async with aiosqlite.connect('database.db') as db:
        await db.execute('''
            DELETE FROM scenes WHERE bot_id = ? AND name = ?
        ''', (bot_id, scene_name))
        await db.commit()

async def set_bot_active_status(bot_id: int, is_active: bool):
    """Установка статуса активности бота"""
    async with aiosqlite.connect('database.db') as db:
        await db.execute('''
            UPDATE user_bots SET is_active = ? WHERE id = ?
        ''', (1 if is_active else 0, bot_id))
        await db.commit()

async def get_all_active_bots() -> List[Dict]:
    """Получение всех активных ботов из БД"""
    async with aiosqlite.connect('database.db') as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT * FROM user_bots WHERE is_active = 1
        ''')
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def check_bot_token(token: str) -> Tuple[bool, Optional[str]]:
    """Проверка валидности токена бота"""
    try:
        temp_bot = Bot(token=token)
        bot_info = await temp_bot.get_me()
        await temp_bot.session.close()
        return True, bot_info.username
    except Exception as e:
        logger.error(f"Ошибка проверки токена: {e}")
        return False, None

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    """Главное меню управления ботом"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🆕 Создать сцену", callback_data="create_scene"),
                InlineKeyboardButton(text="📋 Мои сцены", callback_data="my_scenes")
            ],
            [
                InlineKeyboardButton(text="▶️ Запустить бота", callback_data="start_bot"),
                InlineKeyboardButton(text="⏹ Остановить бота", callback_data="stop_bot")
            ],
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="bot_status"),
                InlineKeyboardButton(text="🔄 Сменить токен", callback_data="change_token")
            ]
        ]
    )

def get_cancel_keyboard():
    """Клавиатура с кнопкой отмены"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
            ]
        ]
    )

def get_content_type_keyboard():
    """Клавиатура выбора типа контента"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Текст", callback_data="content_text")
            ],
            [
                InlineKeyboardButton(text="🖼️ Фото", callback_data="content_photo")
            ],
            [
                InlineKeyboardButton(text="🎥 Видео", callback_data="content_video")
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
            ]
        ]
    )

def get_scene_management_keyboard(scene_name: str = None):
    """Клавиатура управления сценой"""
    buttons = [
        [
            InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="add_button_to_scene")
        ],
        [
            InlineKeyboardButton(text="✏️ Редактировать контент", callback_data=f"edit_scene_{scene_name}" if scene_name else "edit_scene")
        ],
        [
            InlineKeyboardButton(text="✅ Завершить сцену", callback_data="finish_scene")
        ],
        [
            InlineKeyboardButton(text="❌ Удалить сцену", callback_data="delete_scene")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад к сценам", callback_data="back_to_scenes")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_button_type_keyboard():
    """Клавиатура выбора типа кнопки"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔗 Ссылка (URL)", callback_data="button_type_url")
            ],
            [
                InlineKeyboardButton(text="🔄 Переход на сцену", callback_data="button_type_scene")
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
            ]
        ]
    )

def get_scenes_list_keyboard(scenes: List[Dict], current_page: int = 0, scenes_per_page: int = 5):
    """Клавиатура со списком сцен"""
    start_idx = current_page * scenes_per_page
    end_idx = start_idx + scenes_per_page
    
    buttons = []
    for scene in scenes[start_idx:end_idx]:
        icon = "📄"
        if scene['content_type'] == 'photo':
            icon = "🖼️"
        elif scene['content_type'] == 'video':
            icon = "🎥"
        
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {scene['name']}",
                callback_data=f"scene_{scene['name']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_{current_page-1}")
        )
    if end_idx < len(scenes):
        nav_buttons.append(
            InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_{current_page+1}")
        )
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([
        InlineKeyboardButton(text="🆕 Создать сцену", callback_data="create_scene"),
        InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def add_watermark(text: str) -> str:
    """Добавление вотермарки к тексту"""
    if text:
        return WATERMARK + text
    return WATERMARK.strip()

def get_content_type_icon(content_type: str) -> str:
    """Получение иконки для типа контента"""
    icons = {
        'text': '📝',
        'photo': '🖼️',
        'video': '🎥'
    }
    return icons.get(content_type, '📄')

# ========== УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ОТОБРАЖЕНИЯ СЦЕН ==========
async def render_scene(bot: Bot, chat_id: int, scene: Dict, message_id: int = None) -> Optional[int]:
    """
    Универсальная функция отображения сцены
    Возвращает message_id отправленного сообщения
    """
    try:
        caption = add_watermark(scene['caption']) if scene['caption'] else WATERMARK.strip()
        buttons = json.loads(scene['buttons_json']) if scene['buttons_json'] else []
        
        # Создаем клавиатуру с кнопками
        if buttons:
            keyboard_buttons = []
            for btn in buttons:
                if btn['type'] == 'url':
                    keyboard_buttons.append([
                        InlineKeyboardButton(text=btn['text'], url=btn['url'])
                    ])
                elif btn['type'] == 'scene':
                    keyboard_buttons.append([
                        InlineKeyboardButton(
                            text=btn['text'], 
                            callback_data=f"scene_{btn['target_scene']}"
                        )
                    ])
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        else:
            keyboard = None
        
        # Отправляем контент в зависимости от типа
        if scene['content_type'] == 'text':
            if message_id:
                # Редактируем существующее сообщение
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=caption,
                    reply_markup=keyboard
                )
                return message_id
            else:
                # Отправляем новое сообщение
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    reply_markup=keyboard
                )
                return msg.message_id
                
        elif scene['content_type'] == 'photo' and scene['file_id']:
            if message_id:
                # Редактируем существующее сообщение с фото
                try:
                    await bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=message_id,
                        media=InputMediaPhoto(
                            media=scene['file_id'],
                            caption=caption
                        ),
                        reply_markup=keyboard
                    )
                    return message_id
                except:
                    # Если не удалось редактировать медиа, удаляем старое и отправляем новое
                    await bot.delete_message(chat_id, message_id)
                    msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=scene['file_id'],
                        caption=caption,
                        reply_markup=keyboard
                    )
                    return msg.message_id
            else:
                # Отправляем новое фото
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=scene['file_id'],
                    caption=caption,
                    reply_markup=keyboard
                )
                return msg.message_id
                
        elif scene['content_type'] == 'video' and scene['file_id']:
            if message_id:
                # Редактируем существующее сообщение с видео
                try:
                    await bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=message_id,
                        media=InputMediaVideo(
                            media=scene['file_id'],
                            caption=caption
                        ),
                        reply_markup=keyboard
                    )
                    return message_id
                except:
                    # Если не удалось редактировать медиа, удаляем старое и отправляем новое
                    await bot.delete_message(chat_id, message_id)
                    msg = await bot.send_video(
                        chat_id=chat_id,
                        video=scene['file_id'],
                        caption=caption,
                        reply_markup=keyboard
                    )
                    return msg.message_id
            else:
                # Отправляем новое видео
                msg = await bot.send_video(
                    chat_id=chat_id,
                    video=scene['file_id'],
                    caption=caption,
                    reply_markup=keyboard
                )
                return msg.message_id
        else:
            # Ошибка загрузки сцены
            if message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=add_watermark("Ошибка загрузки сцены")
                )
                return message_id
            else:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=add_watermark("Ошибка загрузки сцены")
                )
                return msg.message_id
                
    except Exception as e:
        logger.error(f"Ошибка отображения сцены: {e}")
        return None

# ========== ОБРАБОТЧИКИ ОСНОВНОГО БОТА ==========
@main_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start для основного бота"""
    user_id = message.from_user.id
    await save_user(user_id)
    
    user_bot = await get_user_bot(user_id)
    
    if user_bot:
        await message.answer(
            "👋 Добро пожаловать в конструктор ботов с медиа-сценами!\n\n"
            "Выберите действие:",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "🤖 <b>Добро пожаловать в конструктор Telegram-ботов с медиа-сценами!</b>\n\n"
            "Для начала работы вам необходимо предоставить токен вашего бота.\n\n"
            "<i>Отправьте токен бота, полученный от @BotFather:</i>"
        )
        await state.set_state(BotConstructorStates.waiting_for_token)

@main_router.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help"""
    help_text = (
        "📚 <b>Конструктор ботов с медиа-сценами</b>\n\n"
        "Создавайте ботов с поддержкой фото и видео!\n\n"
        "<b>Основные функции:</b>\n"
        "1. 🆕 <b>Создание сцен</b> с различным контентом:\n"
        "   • 📝 Текстовые сообщения\n"
        "   • 🖼️ Фотографии\n"
        "   • 🎥 Видео\n"
        "2. 🔘 <b>Интерактивные кнопки</b>:\n"
        "   • 🔗 Ссылки на внешние ресурсы\n"
        "   • 🔄 Переходы между сценами\n"
        "3. ▶️ <b>Управление ботом</b> - запуск и остановка\n"
        "4. ⚒️ <b>Автоматическая вотермарка</b>\n"
        "5. ✏️ <b>Редактирование сцен</b>\n\n"
        "Для начала работы используйте /start"
    )
    await message.answer(help_text)

@main_router.message(BotConstructorStates.waiting_for_token)
async def process_token(message: Message, state: FSMContext):
    """Обработка ввода токена бота"""
    token = message.text.strip()
    
    if not token.startswith("") or ":" not in token:
        await message.answer(
            "❌ <b>Неверный формат токена!</b>\n\n"
            "Попробуйте еще раз:"
        )
        return
    
    wait_msg = await message.answer("🔍 Проверяю токен...")
    
    is_valid, bot_username = await check_bot_token(token)
    
    if is_valid and bot_username:
        user_id = message.from_user.id
        await save_bot_token(user_id, token, bot_username)
        
        # Создаем стартовую сцену
        user_bot = await get_user_bot(user_id)
        if user_bot:
            await save_scene(user_bot['id'], 'start', 'text', caption='Добро пожаловать!')
        
        await state.clear()
        
        await wait_msg.edit_text(
            f"✅ <b>Токен успешно проверен!</b>\n\n"
            f"Бот: @{bot_username}\n\n"
            f"Создана стартовая текстовая сцена 'start'. Вы можете ее отредактировать.",
            reply_markup=get_main_keyboard()
        )
    else:
        await wait_msg.edit_text(
            "❌ <b>Недействительный токен!</b>\n\n"
            "Попробуйте еще раз:"
        )

@main_router.callback_query(F.data == "create_scene")
async def create_scene_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик создания новой сцены"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.message.answer("Сначала настройте бота!")
        return
    
    await callback.message.edit_text(
        "🆕 <b>Создание новой сцены</b>\n\n"
        "Введите уникальное имя для сцены (на английском, без пробелов):\n"
        "<i>Пример: main_menu, catalog, about</i>",
        reply_markup=get_cancel_keyboard()
    )
    
    await state.set_state(BotConstructorStates.waiting_scene_name)
    await state.update_data(bot_id=user_bot['id'])

@main_router.message(BotConstructorStates.waiting_scene_name)
async def process_scene_name(message: Message, state: FSMContext):
    """Обработка ввода имени сцены"""
    scene_name = message.text.strip().lower()
    
    if not scene_name.isalnum() or ' ' in scene_name:
        await message.answer(
            "❌ <b>Некорректное имя сцены!</b>\n\n"
            "Имя должно содержать только английские буквы и цифры, без пробелов.\n"
            "Попробуйте еще раз:"
        )
        return
    
    data = await state.get_data()
    bot_id = data['bot_id']
    
    # Проверяем, существует ли уже сцена с таким именем
    existing_scene = await get_scene(bot_id, scene_name)
    if existing_scene:
        await message.answer(
            "❌ <b>Сцена с таким именем уже существует!</b>\n\n"
            "Пожалуйста, выберите другое имя:"
        )
        return
    
    await state.update_data(scene_name=scene_name)
    await state.set_state(BotConstructorStates.waiting_content_type)
    
    await message.answer(
        f"✅ Имя сцены: <b>{scene_name}</b>\n\n"
        "Что отправить в этой сцене?",
        reply_markup=get_content_type_keyboard()
    )

@main_router.callback_query(F.data.in_(["content_text", "content_photo", "content_video"]))
async def content_type_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора типа контента"""
    await callback.answer()
    
    content_type = callback.data.replace("content_", "")
    
    await state.update_data(content_type=content_type)
    
    if content_type == "text":
        await state.set_state(BotConstructorStates.waiting_scene_text)
        await callback.message.edit_text(
            "📝 <b>Текстовая сцена</b>\n\n"
            "Введите текст сообщения:\n"
            "<i>Вотермарка будет добавлена автоматически</i>",
            reply_markup=get_cancel_keyboard()
        )
    elif content_type == "photo":
        await state.set_state(BotConstructorStates.waiting_scene_photo)
        await callback.message.edit_text(
            "🖼️ <b>Фото-сцена</b>\n\n"
            "Отправьте фото (как файл, не как сжатое изображение):",
            reply_markup=get_cancel_keyboard()
        )
    elif content_type == "video":
        await state.set_state(BotConstructorStates.waiting_scene_video)
        await callback.message.edit_text(
            "🎥 <b>Видео-сцена</b>\n\n"
            "Отправьте видео (файлом):",
            reply_markup=get_cancel_keyboard()
        )

@main_router.message(BotConstructorStates.waiting_scene_text)
async def process_scene_text(message: Message, state: FSMContext):
    """Обработка ввода текста сцены"""
    scene_text = message.text
    
    data = await state.get_data()
    bot_id = data['bot_id']
    scene_name = data['scene_name']
    
    # Сохраняем сцену с вотермаркой
    await save_scene(bot_id, scene_name, 'text', caption=scene_text)
    
    await state.update_data(scene_text=scene_text)
    
    await message.answer(
        f"✅ <b>Текстовая сцена '{scene_name}' создана!</b>\n\n"
        f"Текст сцены (с вотермаркой):\n"
        f"{add_watermark(scene_text)}\n\n"
        "Теперь вы можете добавить кнопки к этой сцене:",
        reply_markup=get_scene_management_keyboard(scene_name)
    )
    
    # Очищаем состояние
    await state.set_state(None)

@main_router.message(BotConstructorStates.waiting_scene_photo)
async def process_scene_photo(message: Message, state: FSMContext):
    """Обработка загрузки фото"""
    if not message.photo:
        await message.answer(
            "❌ <b>Пожалуйста, отправьте фото!</b>\n\n"
            "Отправьте фото (как файл, не как сжатое изображение):",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    # Берем фото с максимальным разрешением
    photo = message.photo[-1]
    file_id = photo.file_id
    
    data = await state.get_data()
    await state.update_data(file_id=file_id)
    await state.set_state(BotConstructorStates.waiting_scene_caption)
    
    await message.answer(
        "✅ <b>Фото получено!</b>\n\n"
        "Теперь введите подпись для фото:\n"
        "<i>Вотермарка будет добавлена автоматически</i>",
        reply_markup=get_cancel_keyboard()
    )

@main_router.message(BotConstructorStates.waiting_scene_video)
async def process_scene_video(message: Message, state: FSMContext):
    """Обработка загрузки видео"""
    if not message.video:
        await message.answer(
            "❌ <b>Пожалуйста, отправьте видео!</b>\n\n"
            "Отправьте видео (файлом):",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    video = message.video
    file_id = video.file_id
    
    data = await state.get_data()
    await state.update_data(file_id=file_id)
    await state.set_state(BotConstructorStates.waiting_scene_caption)
    
    await message.answer(
        "✅ <b>Видео получено!</b>\n\n"
        "Теперь введите подпись для видео:\n"
        "<i>Вотермарка будет добавлена автоматически</i>",
        reply_markup=get_cancel_keyboard()
    )

@main_router.message(BotConstructorStates.waiting_scene_caption)
async def process_scene_caption(message: Message, state: FSMContext):
    """Обработка ввода подписи для медиа"""
    caption = message.text
    
    data = await state.get_data()
    bot_id = data['bot_id']
    scene_name = data['scene_name']
    content_type = data['content_type']
    file_id = data.get('file_id')
    
    # Сохраняем сцену
    await save_scene(bot_id, scene_name, content_type, file_id=file_id, caption=caption)
    
    await message.answer(
        f"✅ <b>Медиа-сцена '{scene_name}' создана!</b>\n\n"
        f"Тип: {content_type}\n"
        f"Подпись (с вотермаркой):\n{add_watermark(caption)}\n\n"
        "Теперь вы можете добавить кнопки к этой сцене:",
        reply_markup=get_scene_management_keyboard(scene_name)
    )
    
    # Очищаем состояние
    await state.set_state(None)

@main_router.callback_query(F.data == "add_button_to_scene")
async def add_button_to_scene_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик добавления кнопки к сцене"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.message.answer("Сначала настройте бота!")
        return
    
    # Получаем сцены пользователя
    scenes = await get_bot_scenes(user_bot['id'])
    
    if not scenes:
        await callback.message.edit_text(
            "📭 <b>У вас еще нет сцен</b>\n\n"
            "Сначала создайте сцену!"
        )
        return
    
    # Создаем клавиатуру выбора сцены для добавления кнопки
    scene_buttons = []
    for scene in scenes:
        icon = get_content_type_icon(scene['content_type'])
        scene_buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {scene['name']}",
                callback_data=f"select_scene_{scene['name']}"
            )
        ])
    
    scene_buttons.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ])
    
    await callback.message.edit_text(
        "🔘 <b>Добавление кнопки</b>\n\n"
        "Выберите сцену, к которой хотите добавить кнопку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=scene_buttons)
    )

@main_router.callback_query(F.data.startswith("select_scene_"))
async def select_scene_for_button_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора сцены для добавления кнопки"""
    await callback.answer()
    
    scene_name = callback.data.replace("select_scene_", "")
    
    user_bot = await get_user_bot(callback.from_user.id)
    if not user_bot:
        return
    
    await state.update_data(selected_scene=scene_name, bot_id=user_bot['id'])
    
    await callback.message.edit_text(
        f"🔘 <b>Добавление кнопки к сцене '{scene_name}'</b>\n\n"
        "Выберите тип кнопки:",
        reply_markup=get_button_type_keyboard()
    )

@main_router.callback_query(F.data == "button_type_url")
async def button_type_url_callback(callback: CallbackQuery, state: FSMContext):
    """Выбор типа кнопки - URL"""
    await callback.answer()
    
    await state.update_data(button_type="url")
    await state.set_state(BotConstructorStates.waiting_button_text)
    
    await callback.message.edit_text(
        "🔗 <b>Создание кнопки-ссылки</b>\n\n"
        "Введите текст для кнопки:",
        reply_markup=get_cancel_keyboard()
    )

@main_router.callback_query(F.data == "button_type_scene")
async def button_type_scene_callback(callback: CallbackQuery, state: FSMContext):
    """Выбор типа кнопки - переход на сцену"""
    await callback.answer()
    
    data = await state.get_data()
    bot_id = data['bot_id']
    
    # Получаем все сцены для выбора целевой
    scenes = await get_bot_scenes(bot_id)
    
    scene_buttons = []
    for scene in scenes:
        icon = get_content_type_icon(scene['content_type'])
        scene_buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {scene['name']}",
                callback_data=f"target_scene_{scene['name']}"
            )
        ])
    
    scene_buttons.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ])
    
    await state.update_data(button_type="scene")
    
    await callback.message.edit_text(
        "🔄 <b>Создание кнопки перехода на сцену</b>\n\n"
        "Введите текст для кнопки:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(BotConstructorStates.waiting_button_text)

@main_router.message(BotConstructorStates.waiting_button_text)
async def process_button_text(message: Message, state: FSMContext):
    """Обработка ввода текста кнопки"""
    button_text = message.text
    
    data = await state.get_data()
    button_type = data.get('button_type')
    
    await state.update_data(button_text=button_text)
    
    if button_type == "url":
        await state.set_state(BotConstructorStates.waiting_button_url)
        await message.answer(
            f"📝 Текст кнопки: <b>{button_text}</b>\n\n"
            "Теперь введите URL для кнопки:\n"
            "<i>Пример: https://example.com</i>",
            reply_markup=get_cancel_keyboard()
        )
    elif button_type == "scene":
        # Получаем список сцен для выбора
        bot_id = data['bot_id']
        scenes = await get_bot_scenes(bot_id)
        
        scene_buttons = []
        for scene in scenes:
            icon = get_content_type_icon(scene['content_type'])
            scene_buttons.append([
                InlineKeyboardButton(
                    text=f"{icon} {scene['name']}",
                    callback_data=f"target_scene_{scene['name']}"
                )
            ])
        
        scene_buttons.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
        ])
        
        await message.answer(
            f"📝 Текст кнопки: <b>{button_text}</b>\n\n"
            "Выберите сцену, на которую будет вести кнопка:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=scene_buttons)
        )

@main_router.message(BotConstructorStates.waiting_button_url)
async def process_button_url(message: Message, state: FSMContext):
    """Обработка ввода URL кнопки"""
    url = message.text
    
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer(
            "❌ <b>Некорректный URL!</b>\n\n"
            "URL должен начинаться с http:// или https://\n"
            "Попробуйте еще раз:"
        )
        return
    
    data = await state.get_data()
    await save_button_to_scene(data, url=url)
    await state.clear()
    
    await message.answer(
        "✅ <b>Кнопка-ссылка успешно добавлена!</b>\n\n"
        f"Текст: {data['button_text']}\n"
        f"URL: {url}\n\n"
        "Вы можете добавить еще кнопки или завершить сцену.",
        reply_markup=get_scene_management_keyboard(data.get('selected_scene'))
    )

@main_router.callback_query(F.data.startswith("target_scene_"))
async def target_scene_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора целевой сцены"""
    await callback.answer()
    
    target_scene = callback.data.replace("target_scene_", "")
    
    data = await state.get_data()
    await save_button_to_scene(data, target_scene=target_scene)
    await state.clear()
    
    await callback.message.edit_text(
        "✅ <b>Кнопка перехода успешно добавлена!</b>\n\n"
        f"Текст: {data['button_text']}\n"
        f"Целевая сцена: {target_scene}\n\n"
        "Вы можете добавить еще кнопки или завершить сцену.",
        reply_markup=get_scene_management_keyboard(data.get('selected_scene'))
    )

async def save_button_to_scene(data: Dict, url: str = None, target_scene: str = None):
    """Сохранение кнопки в сцену"""
    bot_id = data['bot_id']
    scene_name = data['selected_scene']
    button_text = data['button_text']
    button_type = data['button_type']
    
    # Получаем текущие кнопки сцены
    scene = await get_scene(bot_id, scene_name)
    buttons = json.loads(scene['buttons_json']) if scene['buttons_json'] else []
    
    # Создаем новую кнопку
    button_data = {"text": button_text, "type": button_type}
    if button_type == "url" and url:
        button_data["url"] = url
    elif button_type == "scene" and target_scene:
        button_data["target_scene"] = target_scene
    
    buttons.append(button_data)
    
    # Сохраняем обновленные кнопки
    await update_scene_buttons(bot_id, scene_name, buttons)

@main_router.callback_query(F.data == "my_scenes")
async def my_scenes_callback(callback: CallbackQuery):
    """Обработчик просмотра сцен"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.message.answer("Сначала настройте бота!")
        return
    
    scenes = await get_bot_scenes(user_bot['id'])
    
    if not scenes:
        await callback.message.edit_text(
            "📭 <b>У вас еще нет сцен</b>\n\n"
            "Создайте свою первую сцену!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🆕 Создать сцену", callback_data="create_scene")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")]
            ])
        )
    else:
        await callback.message.edit_text(
            f"📋 <b>Ваши сцены ({len(scenes)})</b>\n\n"
            "Выберите сцену для просмотра или редактирования:",
            reply_markup=get_scenes_list_keyboard(scenes)
        )

@main_router.callback_query(F.data.startswith("scene_"))
async def scene_detail_callback(callback: CallbackQuery):
    """Обработчик просмотра деталей сцены"""
    await callback.answer()
    
    scene_name = callback.data.replace("scene_", "")
    
    user_bot = await get_user_bot(callback.from_user.id)
    if not user_bot:
        return
    
    scene = await get_scene(user_bot['id'], scene_name)
    
    if not scene:
        await callback.answer("Сцена не найдена!")
        return
    
    buttons = json.loads(scene['buttons_json']) if scene['buttons_json'] else []
    icon = get_content_type_icon(scene['content_type'])
    
    scene_info = f"{icon} <b>Сцена: {scene['name']}</b>\n\n"
    
    if scene['content_type'] == 'text':
        scene_info += f"<b>Тип:</b> Текст\n"
        scene_info += f"<b>Текст (с вотермаркой):</b>\n{add_watermark(scene['caption'])}\n\n"
    else:
        scene_info += f"<b>Тип:</b> {'Фото' if scene['content_type'] == 'photo' else 'Видео'}\n"
        scene_info += f"<b>Подпись (с вотермаркой):</b>\n{add_watermark(scene['caption'])}\n\n"
    
    if buttons:
        scene_info += "<b>Кнопки:</b>\n"
        for i, btn in enumerate(buttons, 1):
            if btn['type'] == 'url':
                scene_info += f"{i}. {btn['text']} → {btn['url']}\n"
            else:
                scene_info += f"{i}. {btn['text']} → сцена: {btn['target_scene']}\n"
    else:
        scene_info += "<i>Кнопок пока нет</i>\n"
    
    await callback.message.edit_text(
        scene_info,
        reply_markup=get_scene_management_keyboard(scene_name)
    )

@main_router.callback_query(F.data.startswith("edit_scene_"))
async def edit_scene_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик редактирования сцены"""
    await callback.answer()
    
    scene_name = callback.data.replace("edit_scene_", "")
    
    user_bot = await get_user_bot(callback.from_user.id)
    if not user_bot:
        return
    
    scene = await get_scene(user_bot['id'], scene_name)
    
    if not scene:
        await callback.answer("Сцена не найдена!")
        return
    
    # Сохраняем данные о редактируемой сцене
    await state.update_data(
        bot_id=user_bot['id'],
        scene_name=scene_name,
        current_content_type=scene['content_type']
    )
    
    await state.set_state(BotConstructorStates.waiting_edit_content)
    
    await callback.message.edit_text(
        f"✏️ <b>Редактирование сцены '{scene_name}'</b>\n\n"
        "Что вы хотите изменить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Изменить текст/подпись", callback_data="edit_caption")
            ],
            [
                InlineKeyboardButton(text="🖼️/🎥 Изменить медиа", callback_data="edit_media")
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")
            ]
        ])
    )

@main_router.callback_query(F.data == "edit_caption")
async def edit_caption_callback(callback: CallbackQuery, state: FSMContext):
    """Редактирование подписи/текста"""
    await callback.answer()
    
    data = await state.get_data()
    
    await state.set_state(BotConstructorStates.waiting_edit_caption)
    
    if data.get('current_content_type') == 'text':
        await callback.message.edit_text(
            "📝 <b>Редактирование текста</b>\n\n"
            "Введите новый текст сцены:",
            reply_markup=get_cancel_keyboard()
        )
    else:
        await callback.message.edit_text(
            "📝 <b>Редактирование подписи</b>\n\n"
            "Введите новую подпись для медиа:",
            reply_markup=get_cancel_keyboard()
        )

@main_router.message(BotConstructorStates.waiting_edit_caption)
async def process_edit_caption(message: Message, state: FSMContext):
    """Обработка нового текста/подписи"""
    new_caption = message.text
    
    data = await state.get_data()
    bot_id = data['bot_id']
    scene_name = data['scene_name']
    
    # Обновляем подпись в базе данных
    await update_scene_content(bot_id, scene_name, caption=new_caption)
    
    await state.clear()
    
    await message.answer(
        "✅ <b>Текст/подпись успешно обновлены!</b>\n\n"
        f"Новый текст (с вотермаркой):\n{add_watermark(new_caption)}",
        reply_markup=get_scene_management_keyboard(scene_name)
    )

@main_router.callback_query(F.data == "edit_media")
async def edit_media_callback(callback: CallbackQuery, state: FSMContext):
    """Редактирование медиафайла"""
    await callback.answer()
    
    data = await state.get_data()
    content_type = data.get('current_content_type')
    
    if content_type == 'text':
        await callback.message.edit_text(
            "❌ <b>Невозможно изменить медиа для текстовой сцены!</b>\n\n"
            "Вы можете изменить только текст.",
            reply_markup=get_scene_management_keyboard(data.get('scene_name'))
        )
        return
    
    await state.set_state(BotConstructorStates.waiting_content_type)
    
    await callback.message.edit_text(
        "🔄 <b>Изменение медиафайла</b>\n\n"
        "Выберите новый тип контента:",
        reply_markup=get_content_type_keyboard()
    )

@main_router.callback_query(F.data == "cancel_edit")
async def cancel_edit_callback(callback: CallbackQuery, state: FSMContext):
    """Отмена редактирования"""
    await callback.answer()
    
    await state.clear()
    
    user_bot = await get_user_bot(callback.from_user.id)
    if not user_bot:
        return
    
    scenes = await get_bot_scenes(user_bot['id'])
    
    await callback.message.edit_text(
        f"📋 <b>Ваши сцены ({len(scenes)})</b>\n\n"
        "Выберите сцену для просмотра или редактирования:",
        reply_markup=get_scenes_list_keyboard(scenes)
    )

@main_router.callback_query(F.data == "finish_scene")
async def finish_scene_callback(callback: CallbackQuery):
    """Завершение редактирования сцены"""
    await callback.answer()
    
    await callback.message.edit_text(
        "✅ <b>Сцена сохранена!</b>\n\n"
        "Возвращаемся к списку сцен.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои сцены", callback_data="my_scenes")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")]
        ])
    )

@main_router.callback_query(F.data == "delete_scene")
async def delete_scene_callback(callback: CallbackQuery):
    """Удаление сцены"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    if not user_bot:
        return
    
    # Получаем текущую сцену из сообщения (первая строка после "Сцена: ")
    message_text = callback.message.text
    scene_line = message_text.split('\n')[0]
    scene_name = scene_line.split(': ')[1].replace('</b>', '')
    
    await delete_scene(user_bot['id'], scene_name)
    
    await callback.message.edit_text(
        f"✅ <b>Сцена '{scene_name}' удалена!</b>\n\n"
        "Возвращаемся к списку сцен.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои сцены", callback_data="my_scenes")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")]
        ])
    )

@main_router.callback_query(F.data == "start_bot")
async def start_bot_callback(callback: CallbackQuery):
    """Запуск пользовательского бота"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.message.answer("Сначала настройте бота!")
        return
    
    token = user_bot['bot_token']
    
    if token in user_bots:
        await callback.message.edit_text("Бот уже запущен!")
        return
    
    # Запускаем бота
    success = await start_user_bot(token)
    
    if success:
        await set_bot_active_status(user_bot['id'], True)
        
        await callback.message.edit_text(
            "✅ <b>Бот успешно запущен!</b>\n\n"
            f"Теперь ваш бот @{user_bot['bot_username']} отвечает на сообщения.\n"
            f"Используйте команду /start в вашем боте для проверки.",
            reply_markup=get_main_keyboard()
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Не удалось запустить бота!</b>\n\n"
            "Проверьте токен бота и попробуйте снова.",
            reply_markup=get_main_keyboard()
        )

@main_router.callback_query(F.data == "stop_bot")
async def stop_bot_callback(callback: CallbackQuery):
    """Остановка пользовательского бота"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.message.answer("Сначала настройте бота!")
        return
    
    token = user_bot['bot_token']
    
    if token not in user_bots:
        await callback.message.edit_text("Бот не запущен!")
        return
    
    # Останавливаем бота
    await stop_user_bot(token)
    await set_bot_active_status(user_bot['id'], False)
    
    await callback.message.edit_text(
        "⏹ <b>Бот остановлен!</b>\n\n"
        f"Ваш бот @{user_bot['bot_username']} больше не отвечает на сообщения.\n"
        f"Для возобновления работы нажмите 'Запустить бота'.",
        reply_markup=get_main_keyboard()
    )

@main_router.callback_query(F.data == "bot_status")
async def bot_status_callback(callback: CallbackQuery):
    """Обработчик статуса бота"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.message.answer("Сначала настройте бота!")
        return
    
    token = user_bot['bot_token']
    is_running = token in user_bots
    
    scenes = await get_bot_scenes(user_bot['id'])
    text_count = len([s for s in scenes if s['content_type'] == 'text'])
    photo_count = len([s for s in scenes if s['content_type'] == 'photo'])
    video_count = len([s for s in scenes if s['content_type'] == 'video'])
    
    status_text = f"📊 <b>Статус бота @{user_bot['bot_username']}</b>\n\n"
    status_text += f"• Статус: {'🟢 Запущен' if is_running else '🔴 Остановлен'}\n"
    status_text += f"• Всего сцен: {len(scenes)}\n"
    status_text += f"  - 📝 Текстовых: {text_count}\n"
    status_text += f"  - 🖼️ Фото: {photo_count}\n"
    status_text += f"  - 🎥 Видео: {video_count}\n"
    status_text += f"• Создан: {datetime.fromisoformat(user_bot['created_at']).strftime('%d.%m.%Y')}\n\n"
    
    if scenes:
        status_text += "<b>Последние сцены:</b>\n"
        for scene in scenes[:3]:
            icon = get_content_type_icon(scene['content_type'])
            buttons = json.loads(scene['buttons_json']) if scene['buttons_json'] else []
            status_text += f"• {icon} {scene['name']} ({len(buttons)} кнопок)\n"
        
        if len(scenes) > 3:
            status_text += f"... и еще {len(scenes) - 3} сцен\n"
    
    await callback.message.edit_text(
        status_text,
        reply_markup=get_main_keyboard()
    )

@main_router.callback_query(F.data == "change_token")
async def change_token_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик смены токена"""
    await callback.answer()
    
    await callback.message.edit_text(
        "🔄 <b>Смена токена бота</b>\n\n"
        "Отправьте новый токен бота:\n"
        "<i>Пример: 1234567890:ABCDefGhIJKlmNoPQRsTUVwxyZ</i>",
        reply_markup=get_cancel_keyboard()
    )
    
    await state.set_state(BotConstructorStates.waiting_for_token)

@main_router.callback_query(F.data.startswith("page_"))
async def page_callback(callback: CallbackQuery):
    """Обработчик переключения страниц"""
    await callback.answer()
    
    page = int(callback.data.replace("page_", ""))
    
    user_bot = await get_user_bot(callback.from_user.id)
    if not user_bot:
        return
    
    scenes = await get_bot_scenes(user_bot['id'])
    
    await callback.message.edit_text(
        f"📋 <b>Ваши сцены ({len(scenes)})</b>\n\n"
        "Выберите сцену для просмотра или редактирования:",
        reply_markup=get_scenes_list_keyboard(scenes, page)
    )

@main_router.callback_query(F.data == "back_to_scenes")
async def back_to_scenes_callback(callback: CallbackQuery):
    """Возврат к списку сцен"""
    await callback.answer()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        return
    
    scenes = await get_bot_scenes(user_bot['id'])
    
    await callback.message.edit_text(
        f"📋 <b>Ваши сцены ({len(scenes)})</b>\n\n"
        "Выберите сцену для просмотра или редактирования:",
        reply_markup=get_scenes_list_keyboard(scenes)
    )

@main_router.callback_query(F.data == "back_to_main")
async def back_to_main_callback(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.answer()
    
    await callback.message.edit_text(
        "👋 <b>Главное меню управления ботом</b>\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard()
    )

@main_router.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик отмены действия"""
    await callback.answer()
    
    await state.clear()
    
    user_bot = await get_user_bot(callback.from_user.id)
    
    if user_bot:
        await callback.message.edit_text(
            "❌ Действие отменено.\n\n"
            "Возврат в главное меню:",
            reply_markup=get_main_keyboard()
        )
    else:
        await callback.message.edit_text(
            "❌ Действие отменено.\n\n"
            "Отправьте токен бота для начала работы:"
        )

# ========== ПОЛЬЗОВАТЕЛЬСКИЕ БОТЫ ==========
async def create_user_bot_handlers(token: str, bot_data: Dict):
    """Создание обработчиков для пользовательского бота"""
    router = Router()
    
    # Хранилище для message_id последней отправленной сцены
    user_last_messages = {}
    
    @router.message(CommandStart())
    async def user_bot_start(message: Message):
        """Обработчик команды /start для пользовательского бота"""
        start_scene_name = bot_data.get('start_scene', 'start')
        scene = await get_scene(bot_data['id'], start_scene_name)
        
        if not scene:
            await message.answer(add_watermark("Добро пожаловать! Сцена 'start' не настроена."))
            return
        
        # Отправляем сцену
        message_id = await render_scene(message.bot, message.chat.id, scene)
        
        # Сохраняем message_id для этого пользователя
        user_last_messages[message.chat.id] = message_id
    
    @router.callback_query(F.data.startswith("scene_"))
    async def user_bot_scene_callback(callback: CallbackQuery):
        """Обработчик перехода между сценами"""
        # ВАЖНО: Отвечаем сразу, чтобы убрать индикатор загрузки
        await callback.answer()
        
        scene_name = callback.data.replace("scene_", "")
        scene = await get_scene(bot_data['id'], scene_name)
        
        if not scene:
            await callback.message.answer("Сцена не найдена!")
            return
        
        # Получаем последний message_id для этого чата
        last_message_id = user_last_messages.get(callback.message.chat.id)
        
        # Отправляем/редактируем сцену
        new_message_id = await render_scene(
            callback.bot, 
            callback.message.chat.id, 
            scene,
            message_id=last_message_id
        )
        
        # Обновляем message_id
        if new_message_id:
            user_last_messages[callback.message.chat.id] = new_message_id
    
    @router.message()
    async def user_bot_echo(message: Message):
        """Эхо-обработчик для пользовательского бота"""
        await message.answer(add_watermark("Используйте /start для начала работы"))
    
    return router

async def start_user_bot(token: str) -> bool:
    """Запуск пользовательского бота"""
    try:
        # Проверяем, не запущен ли уже бот
        if token in user_bots:
            return True
        
        # Получаем данные бота из БД
        bot_data = await get_bot_by_token(token)
        if not bot_data:
            logger.error(f"Данные бота не найдены для токена: {token[:10]}...")
            return False
        
        # Проверяем валидность токена
        is_valid, bot_username = await check_bot_token(token)
        if not is_valid:
            logger.error(f"Невалидный токен: {token[:10]}...")
            return False
        
        # Создаем экземпляр бота
        user_bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        
        # Создаем диспетчер
        user_dp = Dispatcher(storage=MemoryStorage())
        
        # Создаем и добавляем обработчики
        user_router = await create_user_bot_handlers(token, bot_data)
        user_dp.include_router(user_router)
        
        # Запускаем поллинг в отдельной задаче
        task = asyncio.create_task(run_user_bot_polling(user_bot, user_dp, token))
        
        # Сохраняем в глобальный словарь
        user_bots[token] = (user_bot, user_dp, task)
        
        logger.info(f"Запущен пользовательский бот: @{bot_username}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка запуска пользовательского бота: {e}")
        return False

async def run_user_bot_polling(bot: Bot, dp: Dispatcher, token: str):
    """Запуск поллинга для пользовательского бота"""
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка поллинга для бота {token[:10]}...: {e}")
    finally:
        # Удаляем бота из активных при остановке
        if token in user_bots:
            del user_bots[token]

async def stop_user_bot(token: str):
    """Остановка пользовательского бота"""
    if token in user_bots:
        user_bot, user_dp, task = user_bots[token]
        
        # Останавливаем поллинг
        await user_dp.stop_polling()
        
        # Отменяем задачу
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        
        # Удаляем из словаря
        del user_bots[token]
        
        logger.info(f"Остановлен пользовательский бот с токеном {token[:10]}...")
        return True
    return False

async def start_all_user_bots():
    """Запуск всех пользовательских ботов из БД"""
    bots = await get_all_active_bots()
    
    tasks = []
    for bot_data in bots:
        token = bot_data.get('bot_token')
        if token:
            tasks.append(start_user_bot(token))
    
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successful = sum(1 for r in results if r is True)
        logger.info(f"Запущено {successful}/{len(tasks)} пользовательских ботов")
    else:
        logger.info("Нет пользовательских ботов для запуска")

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
async def health_check(request):
    """Проверка здоровья сервиса"""
    return web.Response(text=f"Bot constructor is running! Active bots: {len(user_bots)}")

async def web_server():
    """Запуск веб-сервера для поддержания активности на Render"""
    app = web.Application()
    
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    
    logger.info(f"Web server started on port {PORT}")
    await site.start()

# ========== ОСНОВНАЯ ФУНКЦИЯ ==========
async def main():
    """Основная функция запуска бота"""
    logger.info("Запуск конструктора ботов с медиа-сценами...")
    
    # Инициализация базы данных
    await init_db()
    
    # Запуск веб-сервера в фоне
    web_server_task = asyncio.create_task(web_server())
    
    # Запуск всех активных ботов из БД
    await start_all_user_bots()
    
    try:
        # Запуск основного бота (конструктора)
        await main_bot.delete_webhook(drop_pending_updates=True)
        await main_dp.start_polling(main_bot)
    except Exception as e:
        logger.error(f"Ошибка при запуске основного бота: {e}")
    finally:
        # Остановка всех пользовательских ботов
        for token in list(user_bots.keys()):
            await stop_user_bot(token)
        
        # Отмена задачи веб-сервера
        web_server_task.cancel()
        try:
            await web_server_task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    asyncio.run(main())
