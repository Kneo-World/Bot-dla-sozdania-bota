import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

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

# ========== ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА ==========
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ========== СОСТОЯНИЯ FSM ==========
class BotConstructorStates(StatesGroup):
    waiting_for_token = State()
    waiting_for_greeting = State()
    waiting_for_buttons = State()
    waiting_button_text = State()
    waiting_button_url = State()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
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
                greeting_text TEXT DEFAULT 'Привет! Я ваш бот.',
                buttons_json TEXT DEFAULT '[]',
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
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
            INSERT INTO user_bots (user_id, bot_token, bot_username)
            VALUES (?, ?, ?)
        ''', (user_id, token, bot_username))
        
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

async def update_greeting(user_id: int, greeting: str):
    """Обновление приветственного сообщения"""
    async with aiosqlite.connect('database.db') as db:
        await db.execute('''
            UPDATE user_bots SET greeting_text = ? WHERE user_id = ?
        ''', (greeting, user_id))
        await db.commit()

async def update_buttons(user_id: int, buttons: List[Dict]):
    """Обновление кнопок бота"""
    buttons_json = json.dumps(buttons, ensure_ascii=False)
    async with aiosqlite.connect('database.db') as db:
        await db.execute('''
            UPDATE user_bots SET buttons_json = ? WHERE user_id = ?
        ''', (buttons_json, user_id))
        await db.commit()

async def check_bot_token(token: str) -> tuple[bool, Optional[str]]:
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
                InlineKeyboardButton(
                    text="✏️ Изменить приветствие",
                    callback_data="edit_greeting"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔘 Настроить кнопки",
                    callback_data="edit_buttons"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статус бота",
                    callback_data="bot_status"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Сменить токен",
                    callback_data="change_token"
                )
            ]
        ]
    )

def get_cancel_keyboard():
    """Клавиатура с кнопкой отмены"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel"
                )
            ]
        ]
    )

def get_buttons_management_keyboard():
    """Клавиатура управления кнопками"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить кнопку",
                    callback_data="add_button"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Очистить все кнопки",
                    callback_data="clear_buttons"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Назад",
                    callback_data="back_to_menu"
                )
            ]
        ]
    )

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    await save_user(user_id)
    
    user_bot = await get_user_bot(user_id)
    
    if user_bot:
        # Если бот уже настроен, показываем меню
        await message.answer(
            "👋 Добро пожаловать в конструктор ботов!\n\n"
            "Выберите действие:",
            reply_markup=get_main_keyboard()
        )
    else:
        # Если бот не настроен, просим ввести токен
        await message.answer(
            "🤖 <b>Добро пожаловать в конструктор Telegram-ботов!</b>\n\n"
            "Для начала работы вам необходимо предоставить токен вашего бота.\n\n"
            "<i>Отправьте токен бота, полученный от @BotFather:</i>"
        )
        # Устанавливаем состояние ожидания токена
        from aiogram.fsm.context import FSMContext
        state = FSMContext(storage=dp.storage, key=chat=message.chat.id, user=user_id)
        await state.set_state(BotConstructorStates.waiting_for_token)

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help"""
    help_text = (
        "📚 <b>Справка по конструктору ботов</b>\n\n"
        "Этот бот позволяет вам настроить вашего Telegram-бота:\n\n"
        "1. <b>Добавьте токен</b> - токен, полученный от @BotFather\n"
        "2. <b>Настройте приветствие</b> - сообщение, которое будет отправляться при команде /start\n"
        "3. <b>Добавьте кнопки</b> - inline-кнопки с ссылками\n\n"
        "Ваш бот останется активным 24/7 благодаря веб-серверу!\n\n"
        "Для начала работы используйте /start"
    )
    await message.answer(help_text)

# ========== ОБРАБОТЧИКИ СОСТОЯНИЙ ==========
@router.message(BotConstructorStates.waiting_for_token)
async def process_token(message: Message, state: FSMContext):
    """Обработка ввода токена бота"""
    token = message.text.strip()
    
    # Проверяем формат токена (примерная проверка)
    if not token.startswith("") or ":" not in token:
        await message.answer(
            "❌ <b>Неверный формат токена!</b>\n\n"
            "Токен должен выглядеть примерно так:\n"
            "<code>1234567890:ABCDefGhIJKlmNoPQRsTUVwxyZ</code>\n\n"
            "Попробуйте еще раз:"
        )
        return
    
    # Показываем ожидание
    wait_msg = await message.answer("🔍 Проверяю токен...")
    
    # Проверяем валидность токена
    is_valid, bot_username = await check_bot_token(token)
    
    if is_valid and bot_username:
        # Сохраняем токен
        user_id = message.from_user.id
        await save_bot_token(user_id, token, bot_username)
        
        # Сбрасываем состояние
        await state.clear()
        
        # Обновляем сообщение
        await wait_msg.edit_text(
            f"✅ <b>Токен успешно проверен!</b>\n\n"
            f"Бот: @{bot_username}\n"
            f"Теперь вы можете настроить вашего бота.",
            reply_markup=get_main_keyboard()
        )
    else:
        await wait_msg.edit_text(
            "❌ <b>Недействительный токен!</b>\n\n"
            "Проверьте правильность токена и попробуйте еще раз:\n"
            "<i>Отправьте токен бота:</i>"
        )

@router.message(BotConstructorStates.waiting_for_greeting)
async def process_greeting(message: Message, state: FSMContext):
    """Обработка ввода приветственного сообщения"""
    greeting_text = message.text
    
    user_id = message.from_user.id
    await update_greeting(user_id, greeting_text)
    
    await state.clear()
    
    await message.answer(
        "✅ <b>Приветственное сообщение обновлено!</b>\n\n"
        f"Новое сообщение:\n<code>{greeting_text}</code>",
        reply_markup=get_main_keyboard()
    )

@router.message(BotConstructorStates.waiting_button_text)
async def process_button_text(message: Message, state: FSMContext):
    """Обработка ввода текста кнопки"""
    button_text = message.text
    
    # Сохраняем текст во временные данные
    await state.update_data(button_text=button_text)
    
    # Запрашиваем URL
    await state.set_state(BotConstructorStates.waiting_button_url)
    await message.answer(
        f"📝 Текст кнопки: <b>{button_text}</b>\n\n"
        "Теперь отправьте URL для этой кнопки:\n"
        "<i>Пример: https://example.com</i>",
        reply_markup=get_cancel_keyboard()
    )

@router.message(BotConstructorStates.waiting_button_url)
async def process_button_url(message: Message, state: FSMContext):
    """Обработка ввода URL кнопки"""
    url = message.text
    
    # Проверяем, что URL выглядит корректно
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer(
            "❌ <b>Некорректный URL!</b>\n\n"
            "URL должен начинаться с http:// или https://\n"
            "Попробуйте еще раз:"
        )
        return
    
    # Получаем сохраненный текст кнопки
    data = await state.get_data()
    button_text = data.get('button_text')
    
    # Получаем текущие кнопки пользователя
    user_id = message.from_user.id
    user_bot = await get_user_bot(user_id)
    
    if user_bot:
        buttons = json.loads(user_bot.get('buttons_json', '[]'))
        
        # Добавляем новую кнопку
        buttons.append({
            "text": button_text,
            "url": url
        })
        
        # Сохраняем в БД
        await update_buttons(user_id, buttons)
        
        await state.clear()
        
        # Показываем результат
        buttons_list = "\n".join([f"• {b['text']} - {b['url']}" for b in buttons])
        
        await message.answer(
            "✅ <b>Кнопка успешно добавлена!</b>\n\n"
            f"Текущие кнопки:\n{buttons_list}\n\n"
            "Вы можете добавить еще кнопки или вернуться в меню.",
            reply_markup=get_buttons_management_keyboard()
        )
    else:
        await state.clear()
        await message.answer(
            "❌ Произошла ошибка. Пожалуйста, начните с /start",
            reply_markup=get_main_keyboard()
        )

# ========== ОБРАБОТЧИКИ CALLBACK-ЗАПРОСОВ ==========
@router.callback_query(F.data == "edit_greeting")
async def edit_greeting_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик нажатия кнопки изменения приветствия"""
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.answer("Сначала настройте бота!")
        return
    
    current_greeting = user_bot.get('greeting_text', 'Привет! Я ваш бот.')
    
    await callback.message.edit_text(
        f"✏️ <b>Изменение приветственного сообщения</b>\n\n"
        f"Текущее сообщение:\n<code>{current_greeting}</code>\n\n"
        "Отправьте новое приветственное сообщение:",
        reply_markup=get_cancel_keyboard()
    )
    
    await state.set_state(BotConstructorStates.waiting_for_greeting)
    await callback.answer()

@router.callback_query(F.data == "edit_buttons")
async def edit_buttons_callback(callback: CallbackQuery):
    """Обработчик нажатия кнопки редактирования кнопок"""
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.answer("Сначала настройте бота!")
        return
    
    buttons = json.loads(user_bot.get('buttons_json', '[]'))
    
    if buttons:
        buttons_list = "\n".join([f"• {b['text']} - {b['url']}" for b in buttons])
        text = f"🔘 <b>Текущие кнопки:</b>\n\n{buttons_list}"
    else:
        text = "📭 <b>Кнопки не настроены</b>\n\nДобавьте ваши первые кнопки!"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_buttons_management_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "bot_status")
async def bot_status_callback(callback: CallbackQuery):
    """Обработчик нажатия кнопки статуса бота"""
    user_bot = await get_user_bot(callback.from_user.id)
    
    if not user_bot:
        await callback.answer("Сначала настройте бота!")
        return
    
    # Проверяем статус бота
    token = user_bot['bot_token']
    is_valid, bot_username = await check_bot_token(token)
    
    if is_valid:
        status = "🟢 <b>Активен</b>"
        status_details = f"Бот @{bot_username} работает нормально"
    else:
        status = "🔴 <b>Неактивен</b>"
        status_details = "Токен недействителен. Проверьте токен бота."
    
    created_at = datetime.fromisoformat(user_bot['created_at'])
    
    await callback.message.edit_text(
        f"📊 <b>Статус вашего бота</b>\n\n"
        f"{status}\n"
        f"{status_details}\n\n"
        f"<b>Информация:</b>\n"
        f"• Токен сохранен: {created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"• Приветствие: {len(user_bot['greeting_text'])} символов\n"
        f"• Кнопок: {len(json.loads(user_bot['buttons_json']))}\n\n"
        f"<i>Статус проверен: {datetime.now().strftime('%d.%m.%Y %H:%M')}</i>",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "change_token")
async def change_token_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик нажатия кнопки смены токена"""
    await callback.message.edit_text(
        "🔄 <b>Смена токена бота</b>\n\n"
        "Отправьте новый токен бота:\n"
        "<i>Пример: 1234567890:ABCDefGhIJKlmNoPQRsTUVwxyZ</i>",
        reply_markup=get_cancel_keyboard()
    )
    
    await state.set_state(BotConstructorStates.waiting_for_token)
    await callback.answer()

@router.callback_query(F.data == "add_button")
async def add_button_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик добавления новой кнопки"""
    await callback.message.edit_text(
        "➕ <b>Добавление новой кнопки</b>\n\n"
        "Отправьте текст для кнопки:\n"
        "<i>Пример: Мой сайт</i>",
        reply_markup=get_cancel_keyboard()
    )
    
    await state.set_state(BotConstructorStates.waiting_button_text)
    await callback.answer()

@router.callback_query(F.data == "clear_buttons")
async def clear_buttons_callback(callback: CallbackQuery):
    """Обработчик очистки всех кнопок"""
    user_id = callback.from_user.id
    
    # Очищаем кнопки
    await update_buttons(user_id, [])
    
    await callback.message.edit_text(
        "✅ <b>Все кнопки удалены!</b>\n\n"
        "Теперь вы можете добавить новые кнопки.",
        reply_markup=get_buttons_management_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: CallbackQuery):
    """Обработчик возврата в главное меню"""
    user_bot = await get_user_bot(callback.from_user.id)
    
    if user_bot:
        await callback.message.edit_text(
            "👋 <b>Главное меню управления ботом</b>\n\n"
            "Выберите действие:",
            reply_markup=get_main_keyboard()
        )
    else:
        await callback.message.edit_text(
            "🤖 <b>Добро пожаловать в конструктор Telegram-ботов!</b>\n\n"
            "Для начала работы вам необходимо предоставить токен вашего бота.\n\n"
            "<i>Отправьте токен бота, полученный от @BotFather:</i>"
        )
    await callback.answer()

@router.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик отмены действия"""
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
        await state.set_state(BotConstructorStates.waiting_for_token)
    
    await callback.answer()

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
async def web_server():
    """Запуск веб-сервера для поддержания активности на Render"""
    app = web.Application()
    
    # Простой эндпоинт для проверки здоровья
    async def health_check(request):
        return web.Response(text="Bot constructor is running!")
    
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
    logger.info("Запуск конструктора ботов...")
    
    # Инициализация базы данных
    await init_db()
    
    # Запуск веб-сервера в фоне
    import asyncio
    asyncio.create_task(web_server())
    
    # Запуск бота
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
