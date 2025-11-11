# bot.py
import os
import aiosqlite
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

DB_PATH = "tasks.db"
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

router = Router()

# Инициализация БД
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

# Добавление задачи (только если это НЕ команда /start или /today)
@router.message(~Command("start", "today"))
async def add_task(message: Message):
    if message.text.strip():
        await add_task_to_db(message.from_user.id, message.text.strip())
        await message.answer(f"✅ Задача сохранена:\n\n{message.text}")
    else:
        await message.answer("❌ Не могу сохранить пустое сообщение.")

# Добавление задачи в БД
async def add_task_to_db(user_id: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tasks (user_id, text) VALUES (?, ?)", (user_id, text))
        await db.commit()

# Получение задач
async def get_tasks_from_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT text FROM tasks WHERE user_id = ?", (user_id,)) as cursor:
            return [row[0] async for row in cursor]

# Команда /today
@router.message(Command("today"))
async def show_today(message: Message):
    tasks = await get_tasks_from_db(message.from_user.id)
    if tasks:
        resp = "📝 Твои задачи:\n\n" + "\n".join(tasks)
    else:
        resp = "Нет задач. Напиши что-нибудь!"
    await message.answer(resp)

# Команда /start
@router.message(Command("start"))
async def start_command(message: Message):
    await message.answer("Привет! Я твой планировщик. Напиши мне задачу или используй команды:\n/today — посмотреть задачи")

async def on_startup(bot: Bot):
    await init_db()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"✅ Webhook установлен: {WEBHOOK_URL}")
    else:
        print("⚠️ WEBHOOK_URL не задан!")

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
