import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    MenuButtonWebApp, WebAppInfo
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from aiohttp import web

import anthropic
import db

# --- Config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", 8080))
TZ = ZoneInfo(TIMEZONE)

pending_tasks = {}

# --- Categories ---
CATEGORIES = {
    "work":      {"name": "Робота",     "emoji": "\U0001f4bc", "color": "#3B82F6"},
    "home":      {"name": "Побут",      "emoji": "\U0001f3e0", "color": "#10B981"},
    "hobby":     {"name": "Хоббі",     "emoji": "\U0001f3ae", "color": "#F59E0B"},
    "ai":        {"name": "AI",         "emoji": "\U0001f916", "color": "#8B5CF6"},
    "finance":   {"name": "Фінанси",   "emoji": "\U0001f4b0", "color": "#EF4444"},
    "health":    {"name": "Здоров'я",   "emoji": "\U0001f3cb\ufe0f", "color": "#EC4899"},
    "education": {"name": "Навчання",   "emoji": "\U0001f4da", "color": "#06B6D4"},
    "travel":    {"name": "Подорожі",   "emoji": "\u2708\ufe0f", "color": "#F97316"},
    "social":    {"name": "Соціальне",  "emoji": "\U0001f465", "color": "#14B8A6"},
    "personal":  {"name": "Особисте",   "emoji": "\U0001f4cb", "color": "#6366F1"},
}

REMINDER_PRESETS = {
    "event":   [1440, 120, 30],
    "meeting": [60, 15],
    "errand":  [30],
    "default": [30],
}

CAT_LIST_FOR_PROMPT = "\n".join(
    f'  "{k}" -- {v["emoji"]} {v["name"]}'
    for k, v in CATEGORIES.items()
)

# --- Init ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_now():
    return datetime.now(TZ)


def parse_message_with_ai(user_text, current_time, active_tasks):
    tasks_list = ""
    if active_tasks:
        tasks_list = "\n".join(
            f'  id={t["id"]}: "{t["title"]}" (deadline: {t["due_date"]}, cat: {t.get("category","personal")})'
            for t in active_tasks
        )
    else:
        tasks_list = "  (no active tasks)"

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=f"""You are a task manager AI. Current time: {current_time}. Timezone: {TIMEZONE}.

Active tasks:
{tasks_list}

Categories:
{CAT_LIST_FOR_PROMPT}

Respond ONLY with valid JSON, no markdown.

Possible intents:
1. "create" - create new task
2. "complete" - mark task done
3. "complete_all" - complete all
4. "delete" - delete task
5. "delete_all" - delete all
6. "list" - show tasks
7. "chat" - casual conversation, NOT task-related

Response formats:

For create:
{{"intent":"create","title":"...","due_date":"YYYY-MM-DD HH:MM","category":"work","time_specified":true,"task_type":"errand"}}
- time_specified: true if user EXPLICITLY mentioned time (o 14:00, cherez godynu, vvecheri, vrantsi, o 10)
- time_specified: false if NO specific time mentioned (only date or day of week, no hours)
- task_type: "event" (concert, show, party, match), "meeting" (work meeting, call, interview), "errand" (buy, do, pay, clean), "default" (other)

For complete/delete:
{{"intent":"complete","task_ids":[1,2]}}
{{"intent":"delete","task_ids":[3]}}

For complete_all/delete_all:
{{"intent":"complete_all"}}
{{"intent":"delete_all"}}

For list:
{{"intent":"list"}}

For chat:
{{"intent":"chat","response":"your reply in Ukrainian"}}

CRITICAL intent rules:
- If message describes an ACTION to do (buy, call, meet, do, pay, etc.) -> ALWAYS intent "create", even without date/time
- "vydaly", "vydalyty", "prybery" -> delete
- "zavershy", "hotovo", "zrobleno" -> complete
- "zavershy vsi", "vydalyty vsi" -> complete_all / delete_all
- "pokazhy zadachi", "moi zadachi" -> list
- intent "chat" -> ONLY for greetings, questions, conversations, NOT tasks

Category rules:
- Client meetings, reports, projects, deadlines -> "work"
- Shopping, cleaning, repairs -> "home"
- Games, sports, entertainment -> "hobby"
- Bots, AI, code, automation -> "ai"
- Payments, bills, investments -> "finance"
- Doctor, training, medicine -> "health"
- Courses, books, learning -> "education"
- Trips, visas, hotels -> "travel"
- Friends, birthday, gifts, parties -> "social"
- Documents, passport, personal -> "personal"
- If user specified category manually (e.g. "robota: zustrich") -> use specified
- If unclear -> "personal"

Date parsing rules:
- "zavtra" = next day
- "pislyazavtra" = +2 days
- "v ponedilok" = next Monday
- If time NOT specified -> set 09:00 as temp, but time_specified: false
- If date not specified -> today
- "cherez godynu" = now + 1 hour (time_specified: true)
- "vvecheri" = 19:00 (time_specified: true)
- "vrantsi" = 09:00 (time_specified: true)
- "vden" = 13:00 (time_specified: true)
- "v subotu" = next Saturday (time_specified: false)
- "na vykhidnykh" = next Saturday (time_specified: false)
- "zaraz" = now (time_specified: true)""",
        messages=[{"role": "user", "content": user_text}]
    )
    raw = response.content[0].text.strip()
    return json.loads(raw)


def format_reminders_text(remind_minutes_list):
    parts = []
    for m in remind_minutes_list:
        if m >= 1440:
            parts.append(f"{m // 1440} день")
        elif m >= 60:
            parts.append(f"{m // 60} год")
        else:
            parts.append(f"{m} хв")
    return ", ".join(parts)


# --- Commands ---
@router.message(Command("start"))
async def cmd_start(message: Message):
    db.ensure_user(message.from_user.id)
    text = (
        "\U0001f44b Привіт! Я твій AI-менеджер задач.\n\n"
        "Просто напиши задачу:\n"
        "\u2022 \u00abЗателефонувати лікарю завтра о 10\u00bb\n"
        "\u2022 \u00abКупити молоко в п'ятницю\u00bb\n"
        "\u2022 \u00abробота: звіт до понеділка\u00bb\n\n"
        "Якщо не вкажеш час \u2014 я запитаю!\n\n"
        "Команди:\n"
        "/tasks \u2014 список задач\n"
        "/dashboard \u2014 дашборд\n"
        "/help \u2014 допомога"
    )
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="\U0001f4ca Відкрити дашборд", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    cats = "\n".join(f"  {v['emoji']} {v['name']}" for v in CATEGORIES.values())
    await message.answer(
        f"\U0001f4dd <b>Як мною користуватись:</b>\n\n"
        f"Пиши задачу текстом \u2014 я сам визначу категорію і дату.\n"
        f"Якщо не вкажеш час \u2014 запитаю кнопками.\n\n"
        f"<b>Розумні нагадування:</b>\n"
        f"  \U0001f3a4 Концерт/подія \u2192 за 1 день, 2 год, 30 хв\n"
        f"  \U0001f4bc Зустріч \u2192 за 1 год, 15 хв\n"
        f"  \U0001f6d2 Побутове \u2192 за 30 хв\n\n"
        f"<b>Категорії:</b>\n{cats}\n\n"
        f"<b>Команди:</b>\n"
        f"/tasks \u2014 активні задачі\n"
        f"/done \u2014 завершені\n"
        f"/dashboard \u2014 дашборд\n"
        f"/clear \u2014 видалити завершені",
        parse_mode=ParseMode.HTML
    )


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="\U0001f4ca Відкрити дашборд", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await message.answer("Натисни кнопку:", reply_markup=kb)
    else:
        await message.answer("\u26a0\ufe0f Дашборд не налаштований. Додай WEBAPP_URL.")


@router.message(Command("tasks"))
async def cmd_tasks(message: Message):
    tasks = db.get_active_tasks(message.from_user.id)
    if not tasks:
        await message.answer("\u2705 Задач немає. Напиши мені нову!")
        return
    text = "\U0001f4cb <b>Твої задачі:</b>\n\n"
    for t in tasks:
        cat = CATEGORIES.get(t.get("category","personal"), CATEGORIES["personal"])
        overdue = datetime.strptime(t["due_date"],"%Y-%m-%d %H:%M").replace(tzinfo=TZ) < get_now()
        s = "\U0001f534" if overdue else "\U0001f7e1"
        text += f"{s} {cat['emoji']} <b>{t['title']}</b>\n   \U0001f4c5 {t['due_date']}\n   /del_{t['id']}\n\n"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("done"))
async def cmd_done(message: Message):
    tasks = db.get_done_tasks(message.from_user.id)
    if not tasks:
        await message.answer("Поки немає завершених задач.")
        return
    text = "\u2705 <b>Завершені:</b>\n\n"
    for t in tasks:
        cat = CATEGORIES.get(t.get("category","personal"), CATEGORIES["personal"])
        text += f"\u2022 {cat['emoji']} <s>{t['title']}</s> ({t['due_date']})\n"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    db.clear_done_tasks(message.from_user.id)
    await message.answer("\U0001f5d1 Завершені задачі видалено.")


@router.message(F.text.startswith("/del_"))
async def cmd_delete_task(message: Message):
    try:
        task_id = int(message.text.split("_")[1])
        task = db.get_task(task_id, message.from_user.id)
        if task:
            db.mark_done(task_id)
            remove_all_reminders(task_id)
            await message.answer(f"\u2705 \u00ab{task['title']}\u00bb \u2014 завершено!")
        else:
            await message.answer("Задачу не знайдено.")
    except (ValueError, IndexError):
        await message.answer("Невірний формат.")


# --- Callbacks ---
@router.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    task = db.get_task(task_id, callback.from_user.id)
    if task:
        db.mark_done(task_id)
        remove_all_reminders(task_id)
        await callback.message.edit_text(f"\u2705 \u00ab{task['title']}\u00bb \u2014 завершено!", parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    task = db.get_task(task_id, callback.from_user.id)
    if task:
        new_time = get_now() + timedelta(minutes=30)
        schedule_single_reminder(task_id, callback.from_user.id, task["title"], new_time, "snooze")
        await callback.message.edit_text(f"\u23f0 \u00ab{task['title']}\u00bb \u2014 нагадаю через 30 хв", parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith("time:"))
async def cb_time_select(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in pending_tasks:
        await callback.answer("Задача вже збережена")
        return

    data = callback.data
    if data == "time:custom":
        await callback.message.edit_text(
            "\u23f0 Напиши час, наприклад: <b>14:30</b> або <b>10:00</b>",
            parse_mode=ParseMode.HTML
        )
        await callback.answer()
        return

    parts = data.split(":")
    hour = int(parts[1])
    minute = int(parts[2]) if len(parts) > 2 else 0

    pending = pending_tasks.pop(user_id)
    due_date = f"{pending['date']} {hour:02d}:{minute:02d}"
    await save_and_confirm_task(user_id, pending["title"], due_date, pending["category"], pending["task_type"], pending["original_text"], callback)
    await callback.answer()


# --- Reminder system ---
async def send_reminder(task_id, user_id, title):
    task = db.get_task(task_id, user_id)
    if not task or task["is_done"]:
        return
    cat = CATEGORIES.get(task.get("category","personal"), CATEGORIES["personal"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\u2705 Готово", callback_data=f"done:{task_id}"),
        InlineKeyboardButton(text="\u23f0 +30 хв", callback_data=f"snooze:{task_id}"),
    ]])
    await bot.send_message(user_id,
        f"\U0001f514 <b>Нагадування!</b>\n\n{cat['emoji']} {title}\n\U0001f4c5 {task['due_date']}",
        parse_mode=ParseMode.HTML, reply_markup=kb)


def schedule_single_reminder(task_id, user_id, title, remind_at, suffix=""):
    job_id = f"reminder_{task_id}_{suffix}" if suffix else f"reminder_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if remind_at < get_now():
        asyncio.ensure_future(send_reminder(task_id, user_id, title))
        return
    scheduler.add_job(send_reminder, trigger=DateTrigger(run_date=remind_at),
        args=[task_id, user_id, title], id=job_id, replace_existing=True)
    logger.info(f"Scheduled {job_id} at {remind_at}")


def schedule_smart_reminders(task_id, user_id, title, due_dt, task_type):
    remind_minutes = REMINDER_PRESETS.get(task_type, REMINDER_PRESETS["default"])
    for i, minutes in enumerate(remind_minutes):
        remind_at = due_dt - timedelta(minutes=minutes)
        schedule_single_reminder(task_id, user_id, title, remind_at, str(i))


def remove_all_reminders(task_id):
    for suffix in ["", "0", "1", "2", "snooze"]:
        job_id = f"reminder_{task_id}_{suffix}" if suffix else f"reminder_{task_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)


# --- Save helper ---
async def save_and_confirm_task(user_id, title, due_date, category, task_type, original_text, msg_or_cb):
    if category not in CATEGORIES:
        category = "personal"
    cat = CATEGORIES[category]
    remind_minutes = REMINDER_PRESETS.get(task_type, REMINDER_PRESETS["default"])

    task_id = db.add_task(user_id=user_id, title=title, due_date=due_date,
        category=category, original_text=original_text, remind_before=remind_minutes[0])

    due_dt = datetime.strptime(due_date, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    schedule_smart_reminders(task_id, user_id, title, due_dt, task_type)

    remind_text = format_reminders_text(remind_minutes)
    confirm = (f"\u2705 <b>Задачу збережено!</b>\n\n"
        f"{cat['emoji']} {title}\n\U0001f4c5 {due_date}\n\U0001f3f7 {cat['name']}\n\U0001f514 Нагадаю за: {remind_text}")

    if isinstance(msg_or_cb, CallbackQuery):
        await msg_or_cb.message.edit_text(confirm, parse_mode=ParseMode.HTML)
    else:
        await msg_or_cb.answer(confirm, parse_mode=ParseMode.HTML)


# --- Main handler ---
@router.message(F.text)
async def handle_text(message: Message):
    db.ensure_user(message.from_user.id)
    user_text = message.text.strip()
    user_id = message.from_user.id

    if not user_text or user_text.startswith("/"):
        return

    # Check if user typing custom time for pending task
    if user_id in pending_tasks:
        import re
        time_match = re.match(r'^(\d{1,2})[:\.](\d{2})$', user_text.strip())
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                pending = pending_tasks.pop(user_id)
                due_date = f"{pending['date']} {hour:02d}:{minute:02d}"
                await save_and_confirm_task(user_id, pending["title"], due_date,
                    pending["category"], pending["task_type"], pending["original_text"], message)
                return

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        now = get_now().strftime("%Y-%m-%d %H:%M, %A")
        active_tasks = db.get_active_tasks(user_id)
        parsed = parse_message_with_ai(user_text, now, active_tasks)
        intent = parsed.get("intent", "create")

        if intent == "create":
            title = parsed.get("title")
        due_date = parsed.get("due_date")
        
        if not title or not due_date:
            await message.answer("❌ Я не зміг розпізнати задачу або дату. Напиши, будь ласка, повнісінько (наприклад: «Купити банани завтра о 12:30»).")
            return
            category = parsed.get("category", "personal")
            task_type = parsed.get("task_type", "default")
            time_specified = parsed.get("time_specified", True)

            if category not in CATEGORIES:
                category = "personal"

            if not time_specified:
                date_part = due_date.split(" ")[0]
                cat = CATEGORIES[category]
                pending_tasks[user_id] = {
                    "title": title, "date": date_part, "category": category,
                    "task_type": task_type, "original_text": user_text,
                }
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="\U0001f305 09:00", callback_data="time:9:0"),
                     InlineKeyboardButton(text="\u2600\ufe0f 12:00", callback_data="time:12:0")],
                    [InlineKeyboardButton(text="\U0001f307 15:00", callback_data="time:15:0"),
                     InlineKeyboardButton(text="\U0001f319 19:00", callback_data="time:19:0")],
                    [InlineKeyboardButton(text="\u270f\ufe0f Свій час", callback_data="time:custom")],
                ])
                await message.answer(
                    f"\U0001f4dd <b>{title}</b>\n\U0001f4c5 {date_part}\n\U0001f3f7 {cat['name']}\n\n\u23f0 На яку годину?",
                    parse_mode=ParseMode.HTML, reply_markup=kb)
                return

            await save_and_confirm_task(user_id, title, due_date, category, task_type, user_text, message)

        elif intent == "complete":
            task_ids = parsed.get("task_ids", [])
            completed = []
            for tid in task_ids:
                task = db.get_task(tid, user_id)
                if task and not task["is_done"]:
                    db.mark_done(tid)
                    remove_all_reminders(tid)
                    completed.append(task["title"])
            if completed:
                await message.answer(f"\u2705 Завершено: {', '.join(f'\u00ab{n}\u00bb' for n in completed)}")
            else:
                await message.answer("\U0001f914 Не знайшов таких задач.")

        elif intent == "complete_all":
            tasks = db.get_active_tasks(user_id)
            if tasks:
                for t in tasks:
                    db.mark_done(t["id"])
                    remove_all_reminders(t["id"])
                await message.answer(f"\u2705 Всі {len(tasks)} задач завершено!")
            else:
                await message.answer("\u2705 У тебе і так немає активних задач.")

        elif intent == "delete":
            task_ids = parsed.get("task_ids", [])
            deleted = []
            for tid in task_ids:
                task = db.get_task(tid, user_id)
                if task:
                    db.delete_task(tid, user_id)
                    remove_all_reminders(tid)
                    deleted.append(task["title"])
            if deleted:
                await message.answer(f"\U0001f5d1 Видалено: {', '.join(f'\u00ab{n}\u00bb' for n in deleted)}")
            else:
                await message.answer("\U0001f914 Не знайшов таких задач.")

        elif intent == "delete_all":
            tasks = db.get_active_tasks(user_id)
            if tasks:
                for t in tasks:
                    db.delete_task(t["id"], user_id)
                    remove_all_reminders(t["id"])
                await message.answer(f"\U0001f5d1 Видалено всі {len(tasks)} задач.")
            else:
                await message.answer("У тебе немає активних задач.")

        elif intent == "list":
            await cmd_tasks(message)

        elif intent == "chat":
            await message.answer(parsed.get("response", "Не зрозумів."))

    except json.JSONDecodeError:
        await message.answer("\U0001f914 Не зміг розпарсити. Спробуй: \u00abЗустріч завтра о 14:00\u00bb")
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.answer("\u274c Щось пішло не так. Спробуй ще раз.")


# --- Reschedule on startup ---
async def reschedule_all():
    tasks = db.get_all_active_tasks()
    current = get_now()
    for t in tasks:
        due_dt = datetime.strptime(t["due_date"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        remind_at = due_dt - timedelta(minutes=t["remind_before"])
        if remind_at > current:
            schedule_single_reminder(t["id"], t["user_id"], t["title"], remind_at, "0")
        elif due_dt > current:
            schedule_single_reminder(t["id"], t["user_id"], t["title"], current + timedelta(seconds=10), "0")
    logger.info(f"Rescheduled {len(tasks)} active tasks")


# --- Web API ---
async def handle_api_tasks(request):
    user_id = request.query.get("user_id")
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    tasks = db.get_all_tasks_for_user(int(user_id))
    return web.json_response({"tasks": tasks, "categories": CATEGORIES})

async def handle_api_complete(request):
    task_id = int(request.match_info["id"])
    user_id = int(request.query.get("user_id", 0))
    task = db.get_task(task_id, user_id)
    if task:
        if task["is_done"]:
            db.mark_undone(task_id)
        else:
            db.mark_done(task_id)
            remove_all_reminders(task_id)
    return web.json_response({"ok": True})

async def handle_api_delete(request):
    task_id = int(request.match_info["id"])
    user_id = int(request.query.get("user_id", 0))
    db.delete_task(task_id, user_id)
    remove_all_reminders(task_id)
    return web.json_response({"ok": True})

async def handle_dashboard(request):
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    return web.FileResponse(html_path)


# --- Main ---
async def main():
    db.init()
    dp.include_router(router)
    scheduler.start()
    await reschedule_all()

    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api/tasks", handle_api_tasks)
    app.router.add_post("/api/tasks/{id}/complete", handle_api_complete)
    app.router.add_delete("/api/tasks/{id}", handle_api_delete)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")
    logger.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
