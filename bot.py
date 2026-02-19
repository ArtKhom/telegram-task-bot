import os
import json
import logging
import asyncio
from datetime import datetime, timedelta

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

# ─── Init ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── AI: Parse task from natural language ─────────────────────────
def parse_task_with_ai(user_text: str, current_time: str) -> dict:
    """Use Claude to extract task details from natural text."""
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=f"""Ти — парсер задач. Поточний час: {current_time}. Часовий пояс: {TIMEZONE}.

Користувач пише тобі задачу звичайною мовою. Ти маєш витягнути:
1. title — короткий заголовок задачі
2. due_date — дата та час у форматі "YYYY-MM-DD HH:MM" (24h формат)
3. remind_before — за скільки хвилин до дедлайну нагадати (за замовчуванням 30)

Правила:
- "завтра" = наступний день
- "післязавтра" = +2 дні
- "в понеділок" = найближчий понеділок (якщо сьогодні понеділок — наступний)
- Якщо час не вказано — став 09:00
- Якщо дата не вказана — став сьогодні
- "через годину" = поточний час + 1 година
- "ввечері" = 19:00, "вранці" = 09:00, "вдень" = 13:00

Відповідай ТІЛЬКИ валідним JSON без markdown:
{{"title": "...", "due_date": "YYYY-MM-DD HH:MM", "remind_before": 30}}""",
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
        status = "🔴" if datetime.strptime(t["due_date"], "%Y-%m-%d %H:%M") < datetime.now() else "🟡"
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
        new_time = datetime.now() + timedelta(minutes=30)
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
    if remind_at < datetime.now():
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
        now = datetime.now().strftime("%Y-%m-%d %H:%M, %A")
        parsed = parse_task_with_ai(user_text, now)

        title = parsed["title"]
        due_date = parsed["due_date"]
        remind_before = parsed.get("remind_before", 30)

        # Save to DB
        task_id = db.add_task(
            user_id=message.from_user.id,
            title=title,
            due_date=due_date,
            original_text=user_text,
            remind_before=remind_before
        )

        # Schedule reminder
        due_dt = datetime.strptime(due_date, "%Y-%m-%d %H:%M")
        remind_at = due_dt - timedelta(minutes=remind_before)
        schedule_reminder(task_id, message.from_user.id, title, remind_at)

        await message.answer(
            f"✅ <b>Задачу збережено!</b>\n\n"
            f"📝 {title}\n"
            f"📅 {due_date}\n"
            f"🔔 Нагадаю за {remind_before} хв до дедлайну",
            parse_mode=ParseMode.HTML
        )

    except json.JSONDecodeError:
        await message.answer(
            "🤔 Не зміг розпарсити задачу. Спробуй написати чіткіше, "
            "наприклад: «Зустріч з клієнтом завтра о 14:00»"
        )
    except Exception as e:
        logger.error(f"Error processing task: {e}")
        await message.answer("❌ Щось пішло не так. Спробуй ще раз.")


# ─── Startup: reschedule existing reminders ───────────────────────
async def reschedule_all():
    """On startup, reschedule all active task reminders."""
    tasks = db.get_all_active_tasks()
    now = datetime.now()
    for t in tasks:
        due_dt = datetime.strptime(t["due_date"], "%Y-%m-%d %H:%M")
        remind_at = due_dt - timedelta(minutes=t["remind_before"])
        if remind_at > now:
            schedule_reminder(t["id"], t["user_id"], t["title"], remind_at)
        elif due_dt > now:
            # Reminder time passed but deadline hasn't — remind now
            schedule_reminder(t["id"], t["user_id"], t["title"], now + timedelta(seconds=10))
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
