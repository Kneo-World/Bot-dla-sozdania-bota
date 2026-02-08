import os
import logging
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

# Загружаем переменные из окружения
load_dotenv()
# BOT_TOKEN — это название переменной, которую мы создадим в Render
API_TOKEN = os.getenv('BOT_TOKEN')

logging.basicConfig(level=logging.INFO)

if not API_TOKEN:
    exit("Ошибка: Переменная BOT_TOKEN не найдена!")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Бот запущен и работает через секретные переменные!")

@dp.message()
async def check_token(message: Message):
    # Логика проверки токенов пользователей
    if ":" in message.text:
        await message.answer("Проверяю токен...")
    else:
        await message.answer("Пришли корректный токен.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
