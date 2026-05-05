import asyncio
import logging
import re
from aiogram import Router, Bot, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

from config import config
from database import models
from services import recorder
from services.analyzer import answer_question
from bot.rate_limiter import check_ask_rate_limit
from utils.time import fmt_msk

logger = logging.getLogger(__name__)
router = Router()

TELEMOST_URL_RE = re.compile(r"https?://telemost\.yandex\.ru/\S+")


# ── FSM ───────────────────────────────────────────────────────────────────

class AskState(StatesGroup):
    waiting_question = State()


# ── Keyboards ─────────────────────────────────────────────────────────────

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Активные записи"), KeyboardButton(text="📚 История встреч")],
            [KeyboardButton(text="🔍 Задать вопрос"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def history_inline(meetings: list) -> InlineKeyboardMarkup:
    buttons = []
    for m in meetings:
        topic = (m.get("topic") or "Без темы")[:35]
        ts = fmt_msk(m["created_at"], "%d.%m")
        status_icon = {"done": "✅", "error": "❌"}.get(m["status"], "⏳")
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_icon} {ts} — {topic}",
                callback_data=f"meeting:{m['id']}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_HELP_TEXT = (
    "<b>Бот для записи встреч Яндекс Телемост</b>\n\n"
    "Просто отправь ссылку на встречу — бот войдёт, запишет и пришлёт протокол.\n\n"
    "<b>Кнопки меню:</b>\n"
    "🎬 <b>Активные записи</b> — что сейчас пишется\n"
    "📚 <b>История встреч</b> — последние 10 встреч\n"
    "🔍 <b>Задать вопрос</b> — поиск по базе встреч\n\n"
    "<b>Или отправь ссылку</b> telemost.yandex.ru — запись стартует сразу."
)


# ── Handlers ──────────────────────────────────────────────────────────────

@router.message(Command("start"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await models.upsert_user(message.from_user.id)
    await message.answer(_HELP_TEXT, reply_markup=main_keyboard())


def active_recordings_inline(meetings: list) -> InlineKeyboardMarkup:
    buttons = []
    for m in meetings:
        topic = (m.get("topic") or "Без темы")[:30]
        ts = fmt_msk(m["created_at"], "%d.%m %H:%M")
        status_icon = "🔴" if str(m["id"]) in recorder.active_recordings else "🟡"
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_icon} {ts} [{m['status']}] {topic}",
                callback_data=f"rec_info:{m['id']}",
            ),
            InlineKeyboardButton(
                text="⏹ Стоп",
                callback_data=f"rec_stop:{m['id']}",
            ),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("status"))
@router.message(F.text == "🎬 Активные записи")
async def cmd_status(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    meetings = await models.get_active_meetings(user_id)

    if not meetings:
        await message.answer("📭 Активных записей нет.", reply_markup=main_keyboard())
        return

    await message.answer(
        "🎬 <b>Активные записи:</b>",
        reply_markup=active_recordings_inline(meetings),
    )


@router.callback_query(F.data.startswith("rec_stop:"))
async def cb_stop_recording(call: CallbackQuery) -> None:
    meeting_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id

    try:
        if not await models.meeting_belongs_to_user(meeting_id, user_id):
            await call.answer("Встреча не найдена", show_alert=True)
            return

        recorder.stop_recording(meeting_id)
        await models.update_meeting_status(meeting_id, "cancelled")
        await call.answer("⏹ Запись остановлена")
        await call.message.edit_text(
            f"⏹ Запись остановлена вручную.\n<code>{meeting_id[:8]}…</code>"
        )
    except Exception as exc:
        logger.exception("Error stopping recording %s", meeting_id)
        await call.answer(f"Ошибка: {exc}", show_alert=True)


@router.callback_query(F.data.startswith("rec_info:"))
async def cb_rec_info(call: CallbackQuery) -> None:
    await call.answer("Запись идёт…")


@router.message(Command("history"))
@router.message(F.text == "📚 История встреч")
async def cmd_history(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    meetings = await models.get_user_history(user_id, limit=10)

    if not meetings:
        await message.answer("📭 Встреч пока нет.", reply_markup=main_keyboard())
        return

    await message.answer(
        "📚 <b>Последние встречи</b>\nНажми на встречу чтобы увидеть протокол:",
        reply_markup=history_inline(meetings),
    )


def meeting_detail_inline(meeting_id: str, has_transcript: bool, has_audio: bool) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text="📄 Протокол", callback_data=f"summary:{meeting_id}")]
    if has_transcript:
        row1.append(InlineKeyboardButton(text="📝 Транскрипт", callback_data=f"transcript:{meeting_id}"))
    buttons = [row1]
    if has_audio:
        buttons.append([
            InlineKeyboardButton(text="🎵 Скачать аудио", callback_data=f"audio:{meeting_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("meeting:"))
async def cb_meeting_detail(call: CallbackQuery) -> None:
    meeting_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id

    try:
        meeting = await models.get_meeting(meeting_id, user_id)
    except Exception as exc:
        logger.exception("get_meeting failed: %s", exc)
        await call.answer(f"Ошибка: {exc}", show_alert=True)
        return

    if not meeting:
        await call.answer("Встреча не найдена", show_alert=True)
        return

    ts = fmt_msk(meeting["created_at"], "%d.%m.%Y %H:%M")
    topic = meeting.get("topic") or "Без темы"
    tags = meeting.get("tags") or []
    participants = meeting.get("participants") or []
    status = meeting.get("status", "—")

    tags_str = " ".join(f"#{t}" for t in tags) if tags else "—"
    participants_str = ", ".join(participants) if participants else "—"
    status_icon = {"done": "✅", "error": "❌"}.get(status, "⏳")

    text = (
        f"{status_icon} <b>{topic}</b>\n"
        f"📅 {ts} МСК\n"
        f"🏷 {tags_str}\n"
        f"👥 {participants_str}"
    )

    import os as _os
    audio_path = f"/tmp/{meeting_id}.wav"
    has_audio = _os.path.exists(audio_path)

    await call.answer()
    await call.message.answer(
        text,
        reply_markup=meeting_detail_inline(meeting_id, bool(meeting.get("transcript")), has_audio)
    )


@router.callback_query(F.data.startswith("audio:"))
async def cb_meeting_audio(call: CallbackQuery) -> None:
    import os as _os
    import asyncio as _asyncio
    from aiogram.types import FSInputFile
    meeting_id = call.data.split(":", 1)[1]

    if not await models.meeting_belongs_to_user(meeting_id, call.from_user.id):
        await call.answer("Встреча не найдена", show_alert=True)
        return

    audio_path = f"/tmp/{meeting_id}.wav"
    if not _os.path.exists(audio_path):
        await call.answer("Аудиофайл не найден (удалён или ещё не записан)", show_alert=True)
        return

    mp3_path = f"/tmp/{meeting_id}.mp3"
    if not _os.path.exists(mp3_path):
        await call.answer("Конвертирую аудио, подождите…")
        proc = await _asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", audio_path,
            "-codec:a", "libmp3lame", "-qscale:a", "5",
            mp3_path,
            stdout=_asyncio.subprocess.DEVNULL,
            stderr=_asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode != 0 or not _os.path.exists(mp3_path):
            await call.message.answer("❌ Не удалось конвертировать аудио в MP3.")
            return
    else:
        await call.answer("Отправляю аудио…")

    size_mb = _os.path.getsize(mp3_path) / 1024 / 1024
    try:
        await call.message.answer_document(
            FSInputFile(mp3_path, filename=f"meeting_{meeting_id[:8]}.mp3"),
            caption=f"🎵 Аудио встречи · {size_mb:.1f} МБ",
        )
    except Exception as exc:
        await call.message.answer(f"❌ Не удалось отправить аудио: {exc}")


@router.callback_query(F.data.startswith("summary:"))
async def cb_meeting_summary(call: CallbackQuery) -> None:
    meeting_id = call.data.split(":", 1)[1]
    try:
        meeting = await models.get_meeting_raw(meeting_id, call.from_user.id)
    except Exception as exc:
        await call.answer(f"Ошибка: {exc}", show_alert=True)
        return
    if not meeting:
        await call.answer("Встреча не найдена", show_alert=True)
        return
    summary = meeting.get("summary") or "Протокол ещё не готов."
    await call.answer()
    for i in range(0, len(summary), 4000):
        await call.message.answer(f"📄 <b>Протокол:</b>\n{summary[i:i+4000]}")


@router.callback_query(F.data.startswith("transcript:"))
async def cb_meeting_transcript(call: CallbackQuery) -> None:
    meeting_id = call.data.split(":", 1)[1]
    try:
        meeting = await models.get_meeting_raw(meeting_id, call.from_user.id)
    except Exception as exc:
        await call.answer(f"Ошибка: {exc}", show_alert=True)
        return
    if not meeting:
        await call.answer("Встреча не найдена", show_alert=True)
        return
    transcript = meeting.get("transcript") or "Транскрипт недоступен."
    await call.answer()
    for i in range(0, len(transcript), 4000):
        await call.message.answer(f"📝 <b>Транскрипт:</b>\n{transcript[i:i+4000]}")


@router.message(Command("ask"))
@router.message(F.text == "🔍 Задать вопрос")
async def cmd_ask_prompt(message: Message, state: FSMContext) -> None:
    await state.set_state(AskState.waiting_question)
    await message.answer(
        "❓ Напишите вопрос по базе ваших встреч:",
        reply_markup=cancel_keyboard(),
    )


@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_keyboard())


@router.message(AskState.waiting_question)
async def cmd_ask_answer(message: Message, state: FSMContext) -> None:
    await state.clear()
    question = (message.text or "").strip()
    if not question:
        await message.answer("Вопрос не может быть пустым.", reply_markup=main_keyboard())
        return

    user_id = message.from_user.id
    if not check_ask_rate_limit(user_id):
        await message.answer(
            f"⏳ Слишком много запросов. Лимит — {config.ASK_RATE_LIMIT} в минуту.",
            reply_markup=main_keyboard(),
        )
        return

    thinking = await message.answer("🔍 Ищу по базе встреч…")
    try:
        answer = await answer_question(user_id, question)
        await thinking.delete()
        await message.answer(f"💬 {answer}", reply_markup=main_keyboard())
    except Exception as exc:
        logger.exception("Error in ask for user %d: %s", user_id, exc)
        try:
            await thinking.delete()
        except Exception:
            pass
        await message.answer(
            f"❌ Ошибка при обращении к OpenAI:\n<code>{str(exc)[:300]}</code>",
            reply_markup=main_keyboard()
        )


@router.message()
async def handle_message(message: Message, bot: Bot, state: FSMContext) -> None:
    if not message.text:
        return

    match = TELEMOST_URL_RE.search(message.text)
    if not match:
        return

    await state.clear()
    meeting_url = match.group(0)
    user_id = message.from_user.id

    await models.upsert_user(user_id)
    user = await models.get_user(user_id)
    max_concurrent = user.get("max_concurrent_recordings", config.DEFAULT_MAX_CONCURRENT)

    active_count = await models.get_active_recordings_count(user_id)
    if active_count >= max_concurrent:
        await message.answer(
            f"⚠️ У вас уже {active_count} активных записей (лимит {max_concurrent}).\n"
            "Дождитесь завершения перед запуском новой.",
            reply_markup=main_keyboard(),
        )
        return

    meeting_id = await models.create_meeting(user_id, meeting_url)
    await message.answer(
        f"🎬 Запускаю запись…\n"
        f"ID встречи: <code>{meeting_id}</code>\n\n"
        "Пришлю протокол по окончании.",
        reply_markup=main_keyboard(),
    )

    recorder.start_recording(meeting_id, user_id, meeting_url, bot)
