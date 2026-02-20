import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import anthropic
import db

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Telegram bot token
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # Claude API key
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")  # Часовий пояс
TZ = ZoneInfo(TIMEZONE)

# ─── Init ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_now() -> datetime:
    """Get current time in user's timezone."""
    return datetime.now(TZ)

# ─── AI: Parse task from natural language ─────────────────────────
def parse_message_with_ai(user_text: str, current_time: str, active_tasks: list) -> dict:
    """Use Claude to understand user intent and extract details."""
    
    tasks_list = ""
    if active_tasks:
        tasks_list = "\n".join(
            f"  id={t['id']}: \"{t['title']}\" (дедлайн: {t['due_date']})"
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

Визнач намір користувача та відповідай ТІЛЬКИ валідним JSON без markdown.

Можливі intent:
1. "create" — створити нову задачу
2. "complete" — завершити задачу (відмітити як виконану)
3. "complete_all" — завершити всі задачі
4. "delete" — видалити задачу
5. "delete_all" — видалити всі задачі
6. "list" — показати задачі
7. "chat" — звичайне спілкування, не пов'язане з задачами

Формати відповідей:

Для create:
{{"intent": "create", "title": "...", "due_date": "YYYY-MM-DD HH:MM", "remind_before": 30}}

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

Правила парсингу дат:
- "завтра" = наступний день
- "післязавтра" = +2 дні
- "в понеділок" = найближчий понеділок
- Якщо час не вказано — став 09:00
- Якщо дата не вказана — став сьогодні
- "через годину" = поточний час + 1 година
- "ввечері" = 19:00, "вранці" = 09:00, "вдень" = 13:00

Правила визначення наміру:
- "видали", "видалити", "прибери" → delete
- "завершити", "готово", "зроблено", "виконано" → complete
- "завершити всі", "видалити всі" → complete_all / delete_all
- "покажи задачі", "мої задачі", "що маю зробити" → list
- Якщо повідомлення схоже на задачу (щось зробити до певного часу) → create""",
        messages=[{"role": "user", "content": user_text}]
    )
    
    raw = response.content[0].text.strip()
    return json.loads(raw)


# ─── Commands ─────────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message):
    db.ensure_user(message.from_user.id)
    await message.answer(
        "👋 Привіт! Я твій AI-менеджер задач.\n\n"
        "Просто напиши мені задачу звичайною мовою, наприклад:\n"
        "• «Зателефонувати лікарю завтра о 10»\n"
        "• «Купити молоко в п'ятницю»\n"
        "• «Зустріч з Олегом 25 січня о 15:00»\n\n"
        "Команди:\n"
        "/tasks — список активних задач\n"
        "/done — завершені задачі\n"
        "/help — допомога"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📝 <b>Як мною користуватись:</b>\n\n"
        "Просто пиши задачу текстом — я сам зрозумію що і коли.\n\n"
        "<b>Приклади:</b>\n"
        "• «Здати звіт в понеділок»\n"
        "• «Через 2 години подзвонити Марії»\n"
        "• «Купити подарунок 14 лютого о 12:00»\n\n"
        "<b>Команди:</b>\n"
        "/tasks — активні задачі\n"
        "/done — завершені задачі\n"
        "/clear — видалити всі завершені",
        parse_mode=ParseMode.HTML
    )


@router.message(Command("tasks"))
async def cmd_tasks(message: Message):
    tasks = db.get_active_tasks(message.from_user.id)
    if not tasks:
        await message.answer("✅ Задач немає. Напиши мені нову!")
        return

    text = "📋 <b>Твої задачі:</b>\n\n"
    for t in tasks:
        status = "🔴" if datetime.strptime(t["due_date"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ) < get_now() else "🟡"
        text += (
            f"{status} <b>{t['title']}</b>\n"
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
        text += f"• <s>{t['title']}</s> ({t['due_date']})\n"
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    db.clear_done_tasks(message.from_user.id)
    await message.answer("🗑 Завершені задачі видалено.")


# ─── Handle delete commands like /del_5 ──────────────────────────
@router.message(F.text.startswith("/del_"))
async def cmd_delete_task(message: Message):
    try:
        task_id = int(message.text.split("_")[1])
        task = db.get_task(task_id, message.from_user.id)
        if task:
            db.mark_done(task_id)
            # Remove scheduled reminder
            job_id = f"reminder_{task_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            await message.answer(f"✅ «{task['title']}» — завершено!")
        else:
            await message.answer("Задачу не знайдено.")
    except (ValueError, IndexError):
        await message.answer("Невірний формат команди.")


# ─── Callback: mark done from reminder ───────────────────────────
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
        # Відкласти на 30 хвилин
        new_time = get_now() + timedelta(minutes=30)
        schedule_reminder(task_id, callback.from_user.id, task["title"], new_time)
        await callback.message.edit_text(
            f"⏰ «{task['title']}» — нагадаю через 30 хв",
            parse_mode=ParseMode.HTML
        )
    await callback.answer()


# ─── Reminder sender ─────────────────────────────────────────────
async def send_reminder(task_id: int, user_id: int, title: str):
    """Send a reminder message with action buttons."""
    task = db.get_task(task_id, user_id)
    if not task or task["is_done"]:
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{task_id}"),
            InlineKeyboardButton(text="⏰ +30 хв", callback_data=f"snooze:{task_id}"),
        ]
    ])

    await bot.send_message(
        user_id,
        f"🔔 <b>Нагадування!</b>\n\n"
        f"📝 {title}\n"
        f"📅 {task['due_date']}",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


def schedule_reminder(task_id: int, user_id: int, title: str, remind_at: datetime):
    """Schedule a reminder at specific time."""
    job_id = f"reminder_{task_id}"
    
    # Remove existing job if any
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    # Don't schedule in the past
    if remind_at < get_now():
        # If reminder time is in the past, send immediately
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


# ─── Main message handler (AI parsing) ───────────────────────────
@router.message(F.text)
async def handle_text(message: Message):
    db.ensure_user(message.from_user.id)
    user_text = message.text.strip()

    if not user_text or user_text.startswith("/"):
        return

    # Show "typing" while AI processes
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        now = get_now().strftime("%Y-%m-%d %H:%M, %A")
        active_tasks = db.get_active_tasks(message.from_user.id)
        parsed = parse_message_with_ai(user_text, now, active_tasks)
        intent = parsed.get("intent", "create")

        # ── CREATE ─────────────────────────────────────
        if intent == "create":
            title = parsed["title"]
            due_date = parsed["due_date"]
            remind_before = parsed.get("remind_before", 30)

            task_id = db.add_task(
                user_id=message.from_user.id,
                title=title,
                due_date=due_date,
                original_text=user_text,
                remind_before=remind_before
            )

            due_dt = datetime.strptime(due_date, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            remind_at = due_dt - timedelta(minutes=remind_before)
            schedule_reminder(task_id, message.from_user.id, title, remind_at)

            await message.answer(
                f"✅ <b>Задачу збережено!</b>\n\n"
                f"📝 {title}\n"
                f"📅 {due_date}\n"
                f"🔔 Нагадаю за {remind_before} хв до дедлайну",
                parse_mode=ParseMode.HTML
            )

        # ── COMPLETE ───────────────────────────────────
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

        # ── COMPLETE ALL ───────────────────────────────
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

        # ── DELETE ─────────────────────────────────────
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

        # ── DELETE ALL ─────────────────────────────────
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

        # ── LIST ───────────────────────────────────────
        elif intent == "list":
            await cmd_tasks(message)

        # ── CHAT ───────────────────────────────────────
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


# ─── Startup: reschedule existing reminders ───────────────────────
async def reschedule_all():
    """On startup, reschedule all active task reminders."""
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


# ─── Main ─────────────────────────────────────────────────────────
async def main():
    db.init()
    dp.include_router(router)
    scheduler.start()
    await reschedule_all()
    logger.info("Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
