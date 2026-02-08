import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message

# Настройка логов
logging.basicConfig(level=logging.INFO)

# ТВОЙ ТОКЕН (получи у @BotFather)
API_TOKEN = 'ВАШ_ТОКЕН_ЗДЕСЬ'

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "привет! Я помогу тебе запустить своего бота.\n"
        "Просто пришли мне API токен от @BotFather, и я проверю его."
    )

@dp.message()
async def handle_token(message: Message):
    potential_token = message.text
    
    # Простая проверка на структуру токена
    if ":" not in potential_token or len(potential_token) < 30:
        await message.answer("Это не похоже на валидный токен. Попробуй еще раз.")
        return

    await message.answer(f"Проверяю токен: `{potential_token}`...", parse_mode="Markdown")
    
    try:
        # Пытаемся создать временный экземпляр бота для проверки
        temp_bot = Bot(token=potential_token)
        user_bot = await temp_bot.get_me()
        await message.answer(f"✅ Успех! Бот @{user_bot.username} активен.")
        await temp_bot.session.close()
    except Exception as e:
        await message.answer(f"❌ Ошибка: токен не подходит.")

async def main():
    # Запуск поллинга
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

