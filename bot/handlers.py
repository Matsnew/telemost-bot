import asyncio
import logging
import re
from aiogram import Router, Bot
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import config
from database import models
from services import recorder
from services.analyzer import answer_question
from bot.rate_limiter import check_ask_rate_limit

logger = logging.getLogger(__name__)
router = Router()

TELEMOST_URL_RE = re.compile(r"https?://telemost\.yandex\.ru/\S+")

_HELP_TEXT = (
    "<b>Бот для записи встреч Яндекс Телемост</b>\n\n"
    "Просто отправь ссылку на встречу — бот войдёт, запишет и пришлёт протокол.\n\n"
    "<b>Команды:</b>\n"
    "/status — активные записи\n"
    "/history — последние 10 встреч\n"
    "/ask &lt;вопрос&gt; — поиск по базе встреч\n"
    "/start — эта справка"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await models.upsert_user(message.from_user.id)
    await message.answer(_HELP_TEXT)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user_id = message.from_user.id
    meetings = await models.get_active_meetings(user_id)

    if not meetings:
        await message.answer("📭 Активных записей нет.")
        return

    lines = ["🎬 <b>Активные записи:</b>"]
    for m in meetings:
        in_mem = str(m["id"]) in recorder.active_recordings
        icon = "🔴" if in_mem else "🟡"
        topic = m.get("topic") or "—"
        ts = m["created_at"].strftime("%d.%m %H:%M")
        lines.append(f"{icon} [{m['status']}] {topic} — {ts}")

    await message.answer("\n".join(lines))


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    user_id = message.from_user.id
    meetings = await models.get_user_history(user_id, limit=10)

    if not meetings:
        await message.answer("📭 Встреч пока нет.")
        return

    lines = ["📚 <b>Последние встречи:</b>"]
    for m in meetings:
        ts = m["created_at"].strftime("%d.%m.%Y %H:%M")
        topic = m.get("topic") or "Без темы"
        tags = m.get("tags") or []
        tags_str = " ".join(f"#{t}" for t in tags[:4]) if tags else ""
        status_icon = {"done": "✅", "error": "❌"}.get(m["status"], "⏳")
        lines.append(f"{status_icon} <b>{topic}</b> — {ts}")
        if tags_str:
            lines.append(f"   {tags_str}")

    await message.answer("\n".join(lines))


@router.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject) -> None:
    question = (command.args or "").strip()
    if not question:
        await message.answer("❓ Укажите вопрос: <code>/ask ваш вопрос</code>")
        return

    user_id = message.from_user.id

    if not check_ask_rate_limit(user_id):
        await message.answer(
            f"⏳ Слишком много запросов. Лимит — {config.ASK_RATE_LIMIT} запросов в минуту."
        )
        return

    thinking = await message.answer("🔍 Ищу по базе встреч…")
    try:
        answer = await answer_question(user_id, question)
        await thinking.delete()
        await message.answer(f"💬 {answer}")
    except Exception as exc:
        logger.exception("Error in /ask for user %d", user_id)
        await thinking.delete()
        await message.answer(f"❌ Ошибка: {exc}")


@router.message()
async def handle_message(message: Message, bot: Bot) -> None:
    if not message.text:
        return

    match = TELEMOST_URL_RE.search(message.text)
    if not match:
        return

    meeting_url = match.group(0)
    user_id = message.from_user.id

    await models.upsert_user(user_id)
    user = await models.get_user(user_id)
    max_concurrent = user.get("max_concurrent_recordings", config.DEFAULT_MAX_CONCURRENT)

    active_count = await models.get_active_recordings_count(user_id)
    if active_count >= max_concurrent:
        await message.answer(
            f"⚠️ У вас уже {active_count} активных записей (лимит {max_concurrent}).\n"
            "Дождитесь завершения перед запуском новой."
        )
        return

    meeting_id = await models.create_meeting(user_id, meeting_url)
    await message.answer(
        f"🎬 Запускаю запись…\n"
        f"ID встречи: <code>{meeting_id}</code>\n\n"
        "Пришлю протокол по окончании."
    )

    recorder.start_recording(meeting_id, user_id, meeting_url, bot)
