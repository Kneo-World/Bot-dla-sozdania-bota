import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiohttp import web
from dotenv import load_dotenv

# Настройки
load_dotenv()
API_TOKEN = os.getenv('BOT_TOKEN')
PORT = int(os.environ.get("PORT", 8080)) # Порт для Render

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

# --- ЛОГИКА КОНСТРУКТОРА ---
class BotEditor(StatesGroup):
    waiting_for_text = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✨ Создать пост с кнопкой", callback_data="create_post"))
    
    await message.answer(
        "👋 Привет! Я конструктор ботов.\n\n"
        "Я помогу тебе создать сообщение с красивой инлайн-кнопкой.",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "create_post")
async def start_creation(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("1️⃣ Введите текст сообщения:")
    await state.set_state(BotEditor.waiting_for_text)
    await callback.answer()

@dp.message(BotEditor.waiting_for_text)
async def get_text(message: Message, state: FSMContext):
    await state.update_data(post_text=message.text)
    await message.answer("2️⃣ Теперь введите текст, который будет на кнопке:")
    await state.set_state(BotEditor.waiting_for_button_text)

@dp.message(BotEditor.waiting_for_button_text)
async def get_btn_text(message: Message, state: FSMContext):
    await state.update_data(btn_text=message.text)
    await message.answer("3️⃣ Пришлите ссылку (URL) для кнопки (например, https://google.com):")
    await state.set_state(BotEditor.waiting_for_button_url)

@dp.message(BotEditor.waiting_for_button_url)
async def get_btn_url(message: Message, state: FSMContext):
    if not message.text.startswith("http"):
        await message.answer("⚠️ Ошибка! Ссылка должна начинаться с http:// или https://")
        return

    data = await state.get_data()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=data['btn_text'], url=message.text))

    await message.answer("✅ Готово! Вот ваш результат:")
    await message.answer(text=data['post_text'], reply_markup=builder.as_markup())
    await state.clear()

# --- ЗАПУСК ---
async def main():
    # Запускаем сервер и бота одновременно
    await asyncio.gather(
        start_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
