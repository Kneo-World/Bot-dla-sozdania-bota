import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv

load_dotenv()
API_TOKEN = os.getenv('BOT_TOKEN')

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Состояния для редактора
class BotEditor(StatesGroup):
    waiting_for_text = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🛠 **Конструктор ботов**\n\nНажми кнопку ниже, чтобы создать новое сообщение с кнопкой.",
        reply_markup=InlineKeyboardBuilder().button(
            text="Создать пост", callback_data="create_post"
        ).as_markup()
    )

# Начало создания поста
@dp.callback_query(F.data == "create_post")
async def start_creation(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите текст вашего будущего сообщения:")
    await state.set_state(BotEditor.waiting_for_text)
    await callback.answer()

# Получаем текст сообщения
@dp.message(BotEditor.waiting_for_text)
async def get_text(message: Message, state: FSMContext):
    await state.update_data(post_text=message.text)
    await message.answer("Отлично! Теперь введите текст для инлайн-кнопки:")
    await state.set_state(BotEditor.waiting_for_button_text)

# Получаем текст кнопки
@dp.message(BotEditor.waiting_for_button_text)
async def get_btn_text(message: Message, state: FSMContext):
    await state.update_data(btn_text=message.text)
    await message.answer("И последним шагом — пришлите ссылку (URL) для этой кнопки:")
    await state.set_state(BotEditor.waiting_for_button_url)

# Финальный результат
@dp.message(BotEditor.waiting_for_button_url)
async def get_btn_url(message: Message, state: FSMContext):
    if not message.text.startswith("http"):
        await message.answer("Ошибка! Ссылка должна начинаться с http:// или https://")
        return

    user_data = await state.get_data()
    
    # Сборка клавиатуры
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=user_data['btn_text'], 
        url=message.text)
    )

    await message.answer("✅ Ваш пост готов:")
    await message.answer(
        text=user_data['post_text'],
        reply_markup=builder.as_markup()
    )
    await state.clear()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
