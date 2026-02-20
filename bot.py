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
import speech_recognition as sr
from pydub import AudioSegment

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")  # Public Railway URL
PORT = int(os.getenv("PORT", 8080))
TZ = ZoneInfo(TIMEZONE)
# Тимчасове сховище для задач без дати
pending_tasks = {}

# ─── Categories ───────────────────────────────────────────────────
CATEGORIES = {
    "work":      {"name": "Робота",     "emoji": "💼", "color": "#3B82F6"},
    "home":      {"name": "Побут",      "emoji": "🏠", "color": "#10B981"},
    "hobby":     {"name": "Хоббі",     "emoji": "🎮", "color": "#F59E0B"},
    "ai":        {"name": "AI",         "emoji": "🤖", "color": "#8B5CF6"},
    "finance":   {"name": "Фінанси",   "emoji": "💰", "color": "#EF4444"},
    "health":    {"name": "Здоров'я",   "emoji": "🏋️", "color": "#EC4899"},
    "education": {"name": "Навчання",   "emoji": "📚", "color": "#06B6D4"},
    "travel":    {"name": "Подорожі",   "emoji": "✈️", "color": "#F97316"},
    "social":    {"name": "Соціальне",  "emoji": "👥", "color": "#14B8A6"},
    "personal":  {"name": "Особисте",   "emoji": "📋", "color": "#6366F1"},
}

CAT_LIST_FOR_PROMPT = "\n".join(
    f'  "{k}" — {v["emoji"]} {v["name"]}'
    for k, v in CATEGORIES.items()
)

# ─── Init ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
recognizer = sr.Recognizer()


def get_now() -> datetime:
    return datetime.now(TZ)


# ─── AI: Parse message ───────────────────────────────────────────
def parse_message_with_ai(user_text: str, current_time: str, active_tasks: list) -> dict:
    tasks_list = ""
    if active_tasks:
        tasks_list = "\n".join(
            f'  id={t["id"]}: "{t["title"]}" (дедлайн: {t["due_date"]}, категорія: {t.get("category", "personal")})'
            for t in active_tasks
        )
    else:
        tasks_list = "  (немає активних задач)"

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=f"""Ти — AI-менеджер задач. Поточний час: {current_time}. Часовий пояс: {TIMEZONE}.

Активні задачі користувача:
{tasks_list}

Доступні категорії:
{CAT_LIST_FOR_PROMPT}

Визнач намір користувача та відповідай ТІЛЬКИ валідним JSON без markdown.

Можливі intent:
1. "create" — створити нову задачу
2. "complete" — завершити задачу
3. "complete_all" — завершити всі задачі
4. "delete" — видалити задачу
5. "delete_all" — видалити всі задачі
6. "list" — показати задачі
7. "chat" — звичайне спілкування

Формати відповідей:

Для create:
{{"intent": "create", "title": "...", "due_date": "YYYY-MM-DD HH:MM", "category": "work", "remind_before": 30}}

Для complete/delete:
{{"intent": "complete", "task_ids": [1, 2]}}
{{"intent": "delete", "task_ids": [3]}}

Для complete_all/delete_all:
{{"intent": "complete_all"}}
{{"intent": "delete_all"}}

Для list:
{{"intent": "list"}}

Для chat:
{{"intent": "chat", "response": "твоя відповідь"}}

Правила визначення категорії:
- Зустрічі з клієнтами, звіти, проекти, дедлайни → "work"
- Покупки, прибирання, ремонт, комунальні → "home"
- Ігри, спорт, розваги, фільми, музика → "hobby"
- Боти, нейронки, промпти, код, автоматизація → "ai"
- Оплата, рахунки, борги, інвестиції → "finance"
- Лікар, тренування, ліки, дієта → "health"
- Курси, книги, навчання, сертифікати → "education"
- Поїздки, візи, готелі, квитки → "travel"
- Друзі, день народження, подарунки, вечірки → "social"
- Документи, паспорт, особисті справи → "personal"
- Якщо користувач вказав категорію вручну (напр. "робота: зустріч") — використай вказану
- Якщо не зрозуміло — став "personal"

Правила парсингу дат:
- "завтра" = наступний день
- "післязавтра" = +2 дні
- "в понеділок" = найближчий понеділок
- "Якщо користувач не вказав конкретний час або дату, НЕ вигадуй їх. У такому разі повертай intent: 'chat' та у полі 'response' запитай: 'Записав задачу, але на коли саме поставити нагадування? Напиши дату і час.'".
- Якщо дата не вказана — став сьогодні
- "через годину" = поточний час + 1 година
- "ввечері" = 19:00, "вранці" = 09:00, "вдень" = 13:00

Правила визначення наміру:
- "видали", "видалити", "прибери" → delete
- "завершити", "готово", "зроблено", "виконано" → complete
- "завершити всі", "видалити всі" → complete_all / delete_all
- "покажи задачі", "мої задачі", "що маю зробити" → list
- Якщо повідомлення схоже на задачу → create""",
        messages=[{"role": "user", "content": user_text}]
    )

    raw = response.content[0].text.strip()
    return json.loads(raw)


# ─── Commands ─────────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message):
    db.ensure_user(message.from_user.id)

    text = (
        "👋 Привіт! Я твій AI-менеджер задач.\n\n"
        "Просто напиши задачу звичайною мовою:\n"
        "• «Зателефонувати лікарю завтра о 10»\n"
        "• «Купити молоко в п'ятницю»\n"
        "• «робота: звіт до понеділка»\n\n"
        "Команди:\n"
        "/tasks — список задач\n"
        "/dashboard — відкрити дашборд\n"
        "/help — допомога"
    )

    if WEBAPP_URL:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📊 Відкрити дашборд",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ]])
        await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    cats = "\n".join(f"  {v['emoji']} {v['name']}" for v in CATEGORIES.values())
    await message.answer(
        f"📝 <b>Як мною користуватись:</b>\n\n"
        f"Пиши задачу текстом — я сам визначу категорію і дату.\n\n"
        f"<b>Можна вказати категорію вручну:</b>\n"
        f"• «робота: зустріч з клієнтом завтра»\n"
        f"• «хоббі: падл-теніс в суботу о 18»\n\n"
        f"<b>Категорії:</b>\n{cats}\n\n"
        f"<b>Команди:</b>\n"
        f"/tasks — активні задачі\n"
        f"/done — завершені задачі\n"
        f"/dashboard — дашборд\n"
        f"/clear — видалити завершені",
        parse_mode=ParseMode.HTML
    )


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    if WEBAPP_URL:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📊 Відкрити дашборд",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        ]])
        await message.answer("Натисни кнопку щоб відкрити дашборд:", reply_markup=keyboard)
    else:
        await message.answer("⚠️ Дашборд ще не налаштований. Потрібно додати WEBAPP_URL.")


@router.message(Command("tasks"))
async def cmd_tasks(message: Message):
    tasks = db.get_active_tasks(message.from_user.id)
    if not tasks:
        await message.answer("✅ Задач немає. Напиши мені нову!")
        return

    text = "📋 <b>Твої задачі:</b>\n\n"
    for t in tasks:
        cat = CATEGORIES.get(t.get("category", "personal"), CATEGORIES["personal"])
        is_overdue = datetime.strptime(t["due_date"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ) < get_now()
        status = "🔴" if is_overdue else "🟡"
        text += (
            f"{status} {cat['emoji']} <b>{t['title']}</b>\n"
            f"   📅 {t['due_date']}\n"
            f"   /del_{t['id']}\n\n"
        )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("done"))
async def cmd_done(message: Message):
    tasks = db.get_done_tasks(message.from_user.id)
    if not tasks:
        await message.answer("Поки немає завершених задач.")
        return

    text = "✅ <b>Завершені:</b>\n\n"
    for t in tasks:
        cat = CATEGORIES.get(t.get("category", "personal"), CATEGORIES["personal"])
        text += f"• {cat['emoji']} <s>{t['title']}</s> ({t['due_date']})\n"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    db.clear_done_tasks(message.from_user.id)
    await message.answer("🗑 Завершені задачі видалено.")


    # ─── NEW: Voice Handler ──────────────────────────────────────────
@router.message(F.voice)
async def handle_voice(message: Message):
    status_msg = await message.answer("🎤 Обробляю голосове повідомлення...")
    ogg_file = f"voice_{message.from_user.id}.ogg"
    wav_file = f"voice_{message.from_user.id}.wav"

    try:
        # Завантаження файлу
        file_info = await bot.get_file(message.voice.file_id)
        await bot.download_file(file_info.file_path, ogg_file)

        # Конвертація в WAV
        audio = AudioSegment.from_ogg(ogg_file)
        audio.export(wav_file, format="wav")

        # Розпізнавання (українська мова)
        with sr.AudioFile(wav_file) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="uk-UA")

        await status_msg.edit_text(f"🗣 <b>Розпізнано:</b> {text}", parse_mode=ParseMode.HTML)
        
        # Передаємо текст в основний обробник
        await handle_text(message, custom_text=text)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await status_msg.edit_text("⚠️ Не вдалося розпізнати голос. Напишіть задачу текстом.")
    finally:
        for f in [ogg_file, wav_file]:
            if os.path.exists(f): os.remove(f)
# ─── Handle /del_N ────────────────────────────────────────────────
@router.message(F.text.startswith("/del_"))
async def cmd_delete_task(message: Message):
    try:
        task_id = int(message.text.split("_")[1])
        task = db.get_task(task_id, message.from_user.id)
        if task:
            db.mark_done(task_id)
            job_id = f"reminder_{task_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            await message.answer(f"✅ «{task['title']}» — завершено!")
        else:
            await message.answer("Задачу не знайдено.")
    except (ValueError, IndexError):
        await message.answer("Невірний формат команди.")


# ─── Callbacks ────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    task = db.get_task(task_id, callback.from_user.id)
    if task:
        db.mark_done(task_id)
        await callback.message.edit_text(
            f"✅ «{task['title']}» — завершено!",
            parse_mode=ParseMode.HTML
        )
    await callback.answer()


@router.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(callback: CallbackQuery):
    task_id = int(callback.data.split(":")[1])
    task = db.get_task(task_id, callback.from_user.id)
    if task:
        new_time = get_now() + timedelta(minutes=30)
        schedule_reminder(task_id, callback.from_user.id, task["title"], new_time)
        await callback.message.edit_text(
            f"⏰ «{task['title']}» — нагадаю через 30 хв",
            parse_mode=ParseMode.HTML
        )
    await callback.answer()


# ─── Reminder ─────────────────────────────────────────────────────
async def send_reminder(task_id: int, user_id: int, title: str):
    task = db.get_task(task_id, user_id)
    if not task or task["is_done"]:
        return

    cat = CATEGORIES.get(task.get("category", "personal"), CATEGORIES["personal"])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{task_id}"),
        InlineKeyboardButton(text="⏰ +30 хв", callback_data=f"snooze:{task_id}"),
    ]])

    await bot.send_message(
        user_id,
        f"🔔 <b>Нагадування!</b>\n\n"
        f"{cat['emoji']} {title}\n"
        f"📅 {task['due_date']}",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


def schedule_reminder(task_id: int, user_id: int, title: str, remind_at: datetime):
    job_id = f"reminder_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if remind_at < get_now():
        asyncio.ensure_future(send_reminder(task_id, user_id, title))
        return

    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=remind_at),
        args=[task_id, user_id, title],
        id=job_id,
        replace_existing=True
    )
    logger.info(f"Scheduled reminder for task {task_id} at {remind_at}")


# ─── Main message handler ────────────────────────────────────────
@router.message(F.text)
async def handle_text(message: Message, custom_text: str = None):
    db.ensure_user(message.from_user.id)
    user_text = custom_text if custom_text else message.text.strip()

    if not user_text or user_text.startswith("/"):
        return

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        now = get_now().strftime("%Y-%m-%d %H:%M, %A")
        active_tasks = db.get_active_tasks(message.from_user.id)
        parsed = parse_message_with_ai(user_text, now, active_tasks)
        intent = parsed.get("intent", "create")

        if intent == "create":
            title = parsed["title"]
            due_date = parsed["due_date"]
            category = parsed.get("category", "personal")
            remind_before = parsed.get("remind_before", 30)

            if category not in CATEGORIES:
                category = "personal"

            cat = CATEGORIES[category]

            task_id = db.add_task(
                user_id=message.from_user.id,
                title=title,
                due_date=due_date,
                category=category,
                original_text=user_text,
                remind_before=remind_before
            )

            due_dt = datetime.strptime(due_date, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            remind_at = due_dt - timedelta(minutes=remind_before)
            schedule_reminder(task_id, message.from_user.id, title, remind_at)

            await message.answer(
                f"✅ <b>Задачу збережено!</b>\n\n"
                f"{cat['emoji']} {title}\n"
                f"📅 {due_date}\n"
                f"🏷 {cat['name']}\n"
                f"🔔 Нагадаю за {remind_before} хв до дедлайну",
                parse_mode=ParseMode.HTML
            )

        elif intent == "complete":
            task_ids = parsed.get("task_ids", [])
            completed = []
            for tid in task_ids:
                task = db.get_task(tid, message.from_user.id)
                if task and not task["is_done"]:
                    db.mark_done(tid)
                    job_id = f"reminder_{tid}"
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id)
                    completed.append(task["title"])
            if completed:
                names = ", ".join(f"«{n}»" for n in completed)
                await message.answer(f"✅ Завершено: {names}")
            else:
                await message.answer("🤔 Не знайшов таких задач.")

        elif intent == "complete_all":
            tasks = db.get_active_tasks(message.from_user.id)
            if tasks:
                for t in tasks:
                    db.mark_done(t["id"])
                    job_id = f"reminder_{t['id']}"
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id)
                await message.answer(f"✅ Всі {len(tasks)} задач завершено!")
            else:
                await message.answer("✅ У тебе і так немає активних задач.")

        elif intent == "delete":
            task_ids = parsed.get("task_ids", [])
            deleted = []
            for tid in task_ids:
                task = db.get_task(tid, message.from_user.id)
                if task:
                    db.delete_task(tid, message.from_user.id)
                    job_id = f"reminder_{tid}"
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id)
                    deleted.append(task["title"])
            if deleted:
                names = ", ".join(f"«{n}»" for n in deleted)
                await message.answer(f"🗑 Видалено: {names}")
            else:
                await message.answer("🤔 Не знайшов таких задач.")

        elif intent == "delete_all":
            tasks = db.get_active_tasks(message.from_user.id)
            if tasks:
                for t in tasks:
                    db.delete_task(t["id"], message.from_user.id)
                    job_id = f"reminder_{t['id']}"
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id)
                await message.answer(f"🗑 Видалено всі {len(tasks)} задач.")
            else:
                await message.answer("У тебе немає активних задач.")

        elif intent == "list":
            await cmd_tasks(message)

        elif intent == "chat":
            response_text = parsed.get("response", "Не зрозумів, спробуй ще раз.")
            await message.answer(response_text)

    except json.JSONDecodeError:
        await message.answer(
            "🤔 Не зміг розпарсити. Спробуй написати чіткіше, "
            "наприклад: «Зустріч з клієнтом завтра о 14:00»"
        )
    except Exception as e:
        logger.error(f"Error processing task: {e}")
        await message.answer("❌ Щось пішло не так. Спробуй ще раз.")


# ─── Startup: reschedule ──────────────────────────────────────────
async def reschedule_all():
    tasks = db.get_all_active_tasks()
    current = get_now()
    for t in tasks:
        due_dt = datetime.strptime(t["due_date"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        remind_at = due_dt - timedelta(minutes=t["remind_before"])
        if remind_at > current:
            schedule_reminder(t["id"], t["user_id"], t["title"], remind_at)
        elif due_dt > current:
            schedule_reminder(t["id"], t["user_id"], t["title"], current + timedelta(seconds=10))
    logger.info(f"Rescheduled {len(tasks)} active tasks")


# ═══════════════════════════════════════════════════════════════════
# WEB APP (Dashboard API + HTML)
# ═══════════════════════════════════════════════════════════════════

async def handle_api_tasks(request):
    """GET /api/tasks?user_id=123"""
    user_id = request.query.get("user_id")
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    tasks = db.get_all_tasks_for_user(int(user_id))
    return web.json_response({"tasks": tasks, "categories": CATEGORIES})


async def handle_api_complete(request):
    """POST /api/tasks/{id}/complete?user_id=123"""
    task_id = int(request.match_info["id"])
    user_id = int(request.query.get("user_id", 0))
    task = db.get_task(task_id, user_id)
    if task:
        if task["is_done"]:
            db.mark_undone(task_id)
        else:
            db.mark_done(task_id)
            job_id = f"reminder_{task_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
    return web.json_response({"ok": True})


async def handle_api_delete(request):
    """DELETE /api/tasks/{id}?user_id=123"""
    task_id = int(request.match_info["id"])
    user_id = int(request.query.get("user_id", 0))
    db.delete_task(task_id, user_id)
    job_id = f"reminder_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    return web.json_response({"ok": True})


async def handle_dashboard(request):
    """GET / — serve dashboard HTML"""
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    return web.FileResponse(html_path)


# ─── Main ─────────────────────────────────────────────────────────
async def main():
    db.init()
    dp.include_router(router)
    scheduler.start()
    await reschedule_all()

    # Setup web server
    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api/tasks", handle_api_tasks)
    app.router.add_post("/api/tasks/{id}/complete", handle_api_complete)
    app.router.add_delete("/api/tasks/{id}", handle_api_delete)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

    logger.info("Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
