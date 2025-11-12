# bot.py
import os
import aiosqlite
import dateparser
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx

# --- Настройки ---
DB_PATH = "tasks.db"
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# --- Инициализация ---
router = Router()
scheduler = AsyncIOScheduler()

# --- Инициализация БД ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                due_at TIMESTAMP,
                completed BOOLEAN DEFAULT FALSE
            )
        """)
        await db.commit()

# --- Парсинг даты ---
def parse_date(text: str) -> datetime | None:
    try:
        parsed = dateparser.parse(text, languages=['ru'])
        if parsed and parsed > datetime.now():
            return parsed
    except Exception:
        pass
    return None

# --- Умная корректировка через DeepSeek ---
async def suggest_tasks(text: str) -> list:
    if not DEEPSEEK_API_KEY:
        return [text]  # если API ключа нет — просто возвращаем как есть

    try:
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "Ты помогаешь пользователю уточнить задачу. Предложи 3 варианта, как можно уточнить или дополнить задачу. Ответь в формате JSON: [{'text': '...'}, {'text': '...'}, {'text': '...'}]"},
                {"role": "user", "content": text}
            ],
            "max_tokens": 150,
            "temperature": 0.7
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            content = response.json()['choices'][0]['message']['content'].strip()
            import json
            return json.loads(content)
    except Exception:
        return [text]

# --- Добавление задачи ---
@router.message(~Command("start", "today"))
async def add_task(message: Message):
    text = message.text.strip()
    if not text:
        await message.answer("❌ Не могу сохранить пустое сообщение.")
        return

    # Умная корректировка
    suggestions = await suggest_tasks(text)
    if len(suggestions) > 1:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=s["text"], callback_data=f"select_task:{s['text']}")]
            for s in suggestions
        ])
        await message.answer("Уточни задачу:", reply_markup=keyboard)
        return

    # Если один вариант — сохраняем
    corrected_text = suggestions[0]
    due_date = parse_date(corrected_text)
    await add_task_to_db(message.from_user.id, corrected_text, due_date)

    # Устанавливаем напоминание
    if due_date:
        scheduler.add_job(
            send_reminder,
            due_date,
            args=[message.from_user.id, corrected_text]
        )

    await message.answer(f"✅ Задача сохранена:\n\n{corrected_text}")

# --- Callback для выбора задачи ---
@router.callback_query(lambda c: c.data.startswith("select_task:"))
async def select_task_callback(callback_query):
    task_text = callback_query.data.split(":", 1)[1]
    user_id = callback_query.from_user.id

    due_date = parse_date(task_text)
    await add_task_to_db(user_id, task_text, due_date)

    # Устанавливаем напоминание
    if due_date:
        scheduler.add_job(
            send_reminder,
            due_date,
            args=[user_id, task_text]
        )

    await callback_query.message.edit_text(f"✅ Задача сохранена:\n\n{task_text}")
    await callback_query.answer()

# --- Добавление задачи в БД ---
async def add_task_to_db(user_id: int, text: str, due_date: datetime | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tasks (user_id, text, due_at) VALUES (?, ?, ?)", 
                          (user_id, text, due_date))
        await db.commit()

# --- Получение задач ---
async def get_tasks_from_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, text, due_at, completed FROM tasks WHERE user_id = ?", (user_id,)) as cursor:
            return [dict(row) async for row in cursor]

# --- Команда /today ---
@router.message(Command("today"))
async def show_today(message: Message):
    tasks = await get_tasks_from_db(message.from_user.id)
    if tasks:
        resp = "📝 Твои задачи:\n\n"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        for i, task in enumerate(tasks, 1):
            status = "✅" if task["completed"] else "☐"
            due = f" (до {task['due_at'].strftime('%d.%m %H:%M')})" if task['due_at'] else ""
            resp += f"{i}. {status} {task['text']}{due}\n"
            # Кнопка "Отметить"
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text="✅ Отметить", callback_data=f"complete:{task['id']}")
            ])
    else:
        resp = "Нет задач. Напиши что-нибудь!"
        keyboard = None

    await message.answer(resp, reply_markup=keyboard)

# --- Callback для отметки задачи ---
@router.callback_query(lambda c: c.data.startswith("complete:"))
async def complete_task_callback(callback_query):
    task_id = int(callback_query.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET completed = 1 WHERE id = ?", (task_id,))
        await db.commit()

    await callback_query.message.edit_text("✅ Задача отмечена как выполненная.")
    await callback_query.answer()

# --- Напоминание ---
async def send_reminder(user_id: int, task_text: str):
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(user_id, f"⏰ Напоминание: пора выполнить задачу:\n\n{task_text}")
    except Exception as e:
        print(f"Ошибка при отправке напоминания: {e}")
    finally:
        await bot.session.close()

# --- Команда /start ---
@router.message(Command("start"))
async def start_command(message: Message):
    await message.answer("Привет! Я твой планировщик. Напиши мне задачу или используй команды:\n/today — посмотреть задачи")

# --- Запуск ---
async def on_startup(bot: Bot):
    await init_db()
    scheduler.start()
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
