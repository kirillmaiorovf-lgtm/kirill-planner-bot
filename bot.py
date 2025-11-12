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

# --- Глобальные переменные ---
USER_CONTEXT = {}

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
                completed BOOLEAN DEFAULT FALSE,
                category TEXT DEFAULT 'личное'
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
async def suggest_tasks(user_id: int, text: str) -> list:
    # Получаем контекст пользователя
    context = USER_CONTEXT.get(user_id, {"name": "Пользователь", "location": "Москва", "preferences": "любит готовить"})
    
    if not DEEPSEEK_API_KEY:
        return [{"text": text, "due": "", "solution": "Не удалось сгенерировать решение", "category": "личное"}]

    try:
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": f"Ты — умный помощник по планированию задач. Твоя задача: анализировать ввод пользователя и предлагать 3 варианта уточнения. В каждом варианте укажи: 1. Полный текст задачи 2. Предлагаемое время (если не указано) 3. Решение: как выполнить задачу 4. Категория (работа, личное, важное). Ответь в формате JSON: [{'text': '...', 'due': '...', 'solution': '...', 'category': '...'}, {'text': '...', 'due': '...', 'solution': '...', 'category': '...'}, {'text': '...', 'due': '...', 'solution': '...', 'category': '...'}]"},
                {"role": "user", "content": f"Пользователь: {context['name']}\nМестоположение: {context['location']}\nПредпочтения: {context['preferences']}\nЗадача: {text}"}
            ],
            "max_tokens": 500,
            "temperature": 0.7
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            content = response.json()['choices'][0]['message']['content'].strip()
            import json
            return json.loads(content)
    except Exception:
        return [{"text": text, "due": "", "solution": "Не удалось сгенерировать решение", "category": "личное"}]

# --- Добавление задачи ---
@router.message(~Command("start", "today"))
async def add_task(message: Message):
    text = message.text.strip()
    if not text:
        await message.answer("❌ Не могу сохранить пустое сообщение.")
        return

    # Умная корректировка
    suggestions = await suggest_tasks(message.from_user.id, text)
    if len(suggestions) > 1 and 'solution' in suggestions[0]:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        for i, s in enumerate(suggestions, 1):
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=f"{i}. {s['text']}", callback_data=f"select_task:{s['text']}:{s['due']}:{s['solution']}:{s['category']}")
            ])
        await message.answer("Уточни задачу:", reply_markup=keyboard)
        return

    # Если один вариант — сохраняем
    corrected_text = suggestions[0]['text']
    due_date = parse_date(corrected_text) or parse_date(suggestions[0]['due'])
    category = suggestions[0].get('category', 'личное')
    await add_task_to_db(message.from_user.id, corrected_text, due_date, category)

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
    data = callback_query.data.split(":", 1)[1]
    parts = data.split(":", 3)
    task_text = parts[0]
    due_date_str = parts[1]
    solution = parts[2]
    category = parts[3]
    user_id = callback_query.from_user.id

    due_date = parse_date(due_date_str) if due_date_str else None
    await add_task_to_db(user_id, task_text, due_date, category)

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
async def add_task_to_db(user_id: int, text: str, due_date: datetime | None, category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tasks (user_id, text, due_at, category) VALUES (?, ?, ?, ?)", 
                          (user_id, text, due_date, category))
        await db.commit()

# --- Получение задач ---
async def get_tasks_from_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, text, due_at, completed, category FROM tasks WHERE user_id = ?", (user_id,)) as cursor:
            tasks = []
            async for row in cursor:
                tasks.append({
                    "id": row[0],
                    "text": row[1],
                    "due_at": row[2],
                    "completed": row[3],
                    "category": row[4]
                })
            return tasks

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
            category_emoji = "💼" if task["category"] == "работа" else "❤️" if task["category"] == "важное" else "🏡"
            resp += f"{i}. {status} {category_emoji} {task['text']}{due}\n"
            # Кнопки "Отметить", "Отложить", "Удалить", "Изменить"
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=f"{i}. ✅ Отметить", callback_data=f"complete:{task['id']}"),
                InlineKeyboardButton(text=f"⏸️ Отложить", callback_data=f"postpone:{task['id']}"),
                InlineKeyboardButton(text=f"🗑️ Удалить", callback_data=f"delete:{task['id']}"),
                InlineKeyboardButton(text=f"✏️ Изменить", callback_data=f"edit:{task['id']}")
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

# --- Callback для откладывания задачи ---
@router.callback_query(lambda c: c.data.startswith("postpone:"))
async def postpone_task_callback(callback_query):
    task_id = int(callback_query.data.split(":", 1)[1])
    # Откладываем на 1 день
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET due_at = datetime(due_at, '+1 day') WHERE id = ?", (task_id,))
        await db.commit()

    await callback_query.message.edit_text("⏸️ Задача отложена на 1 день.")
    await callback_query.answer()

# --- Callback для удаления задачи ---
@router.callback_query(lambda c: c.data.startswith("delete:"))
async def delete_task_callback(callback_query):
    task_id = int(callback_query.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()

    await callback_query.message.edit_text("🗑️ Задача удалена.")
    await callback_query.answer()

# --- Callback для изменения задачи ---
@router.callback_query(lambda c: c.data.startswith("edit:"))
async def edit_task_callback(callback_query):
    task_id = int(callback_query.data.split(":", 1)[1])
    # Пока просто показываем текущую задачу
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT text FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                await callback_query.message.edit_text(f"✏️ Редактируешь задачу:\n\n{row[0]}")
            else:
                await callback_query.message.edit_text("❌ Задача не найдена.")

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
    # Сохраняем контекст пользователя
    USER_CONTEXT[message.from_user.id] = {
        "name": message.from_user.first_name or "Пользователь",
        "location": "Москва",
        "preferences": "любит готовить"
    }
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
