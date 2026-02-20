import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Нові імпорти для голосу
import speech_recognition as sr
from pydub import AudioSegment

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

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", 8080))
TZ = ZoneInfo(TIMEZONE)

# ... (CATEGORIES та інші константи залишаються без змін) ...
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

CAT_LIST_FOR_PROMPT = "\n".join(f'  "{k}" — {v["emoji"]} {v["name"]}' for k, v in CATEGORIES.items())

# ─── Init ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
# Ініціалізація розпізнавача
recognizer = sr.Recognizer()

def get_now() -> datetime:
    return datetime.now(TZ)

# ... (AI Logic: parse_message_with_ai залишається без змін) ...
def parse_message_with_ai(user_text: str, current_time: str, active_tasks: list) -> dict:
    tasks_list = "\n".join([f'id={t["id"]}: "{t["title"]}"' for t in active_tasks]) if active_tasks else "(немає)"
    response = claude.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=500,
        system=f"Ти — AI-менеджер задач. Категорії:\n{CAT_LIST_FOR_PROMPT}\nJSON ONLY.",
        messages=[{"role": "user", "content": user_text}]
    )
    return json.loads(response.content[0].text.strip())

# ─── NEW: Voice Handler ──────────────────────────────────────────
@router.message(F.voice)
async def handle_voice(message: Message):
    status_msg = await message.answer("🎤 Обробляю голосове повідомлення...")
    ogg_file = f"voice_{message.from_user.id}.ogg"
    wav_file = f"voice_{message.from_user.id}.wav"

    try:
        file_info = await bot.get_file(message.voice.file_id)
        await bot.download_file(file_info.file_path, ogg_file)

        # Конвертація в WAV для SpeechRecognition
        audio = AudioSegment.from_ogg(ogg_file)
        audio.export(wav_file, format="wav")

        with sr.AudioFile(wav_file) as source:
            audio_data = recognizer.record(source)
            # Використовуємо Google Free API для української мови
            text = recognizer.recognize_google(audio_data, language="uk-UA")

        await status_msg.edit_text(f"🗣 <b>Розпізнано:</b> {text}", parse_mode=ParseMode.HTML)
        # Передаємо розпізнаний текст у вашу основну функцію handle_text
        await handle_text(message, custom_text=text)

    except sr.UnknownValueError:
        await status_msg.edit_text("❌ Не зміг розібрати слова. Спробуйте ще раз чіткіше.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await status_msg.edit_text("⚠️ Не вдалося обробити звук. Спробуйте написати текстом.")
    finally:
        for f in [ogg_file, wav_file]:
            if os.path.exists(f): os.remove(f)

# ─── Modified: handle_text (додано параметр custom_text) ─────────
@router.message(F.text)
async def handle_text(message: Message, custom_text: str = None):
    db.ensure_user(message.from_user.id)
    
    # ПРІОРИТЕТ: якщо прийшов текст із голосового, беремо його. Якщо ні — message.text.
    user_text = custom_text if custom_text else message.text
    
    if not user_text or user_text.startswith("/"):
        return

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        now = get_now().strftime("%Y-%m-%d %H:%M, %A")
        active_tasks = db.get_active_tasks(message.from_user.id)
        parsed = parse_message_with_ai(user_text, now, active_tasks)
        
        # ... (ДАЛІ ВЕСЬ ВАШ ОРИГІНАЛЬНИЙ КОД ОБРОБКИ JSON ВІД AI БЕЗ ЗМІН) ...
        # (intent == "create", "complete", "delete", "list", "chat" і т.д.)
        intent = parsed.get("intent", "create")
        if intent == "create":
            title = parsed["title"]
            due_date = parsed["due_date"]
            category = parsed.get("category", "personal")
            remind_before = parsed.get("remind_before", 30)
            if category not in CATEGORIES: category = "personal"
            cat = CATEGORIES[category]

            task_id = db.add_task(message.from_user.id, title, due_date, category, user_text, remind_before)
            due_dt = datetime.strptime(due_date, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            schedule_reminder(task_id, message.from_user.id, title, due_dt - timedelta(minutes=remind_before))

            await message.answer(
                f"✅ <b>Задача збережена!</b>\n\n{cat['emoji']} {title}\n📅 {due_date}\n🏷 {cat['name']}",
                parse_mode=ParseMode.HTML
            )
        # (Решта логіки intent копіюється з вашого оригінального файлу)
        elif intent == "list": await cmd_tasks(message)
        elif intent == "chat": await message.answer(parsed.get("response", "..."))

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.answer("❌ Щось пішло не так.")

# ... (Всі ваші інші функції: cmd_start, cmd_tasks, send_reminder, handle_dashboard і т.д. залишаються як були) ...
