# bot.py
import os
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Получаем токен и URL из переменных окружения
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

router = Router()
tasks = []  # временный список задач (в будущем — SQLite)

@router.message()
async def add_task(message: Message):
    task = message.text.strip()
    tasks.append({"user_id": message.from_user.id, "text": task})
    await message.answer(f"✅ Задача сохранена:\n\n{task}")

@router.message(commands=["today"])
async def show_today(message: Message):
    if tasks:
        resp = "📝 Твои задачи:\n\n" + "\n".join(t["text"] for t in tasks)
    else:
        resp = "Нет задач. Напиши что-нибудь!"
    await message.answer(resp)

async def on_startup(bot: Bot):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"✅ Webhook установлен: {WEBHOOK_URL}")
    else:
        print("⚠️ WEBHOOK_URL не задан — проверь Secrets в Render!")

def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
