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
        ts = m["created_at"].strftime("%d.%m")
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
        ts = m["created_at"].strftime("%d.%m %H:%M")
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

    # Проверяем что встреча принадлежит этому пользователю
    meeting = await models.get_meeting(meeting_id, user_id)
    if not meeting:
        await call.answer("Встреча не найдена", show_alert=True)
        return

    stopped = recorder.stop_recording(meeting_id)
    if stopped:
        await call.answer("⏹ Останавливаю запись…")
        await call.message.edit_text(
            f"⏹ Запись <code>{meeting_id[:8]}…</code> остановлена вручную."
        )
    else:
        await call.answer("Запись уже завершена или не найдена", show_alert=True)


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


@router.callback_query(F.data.startswith("meeting:"))
async def cb_meeting_detail(call: CallbackQuery) -> None:
    meeting_id = call.data.split(":", 1)[1]
    user_id = call.from_user.id

    meeting = await models.get_meeting(meeting_id, user_id)
    if not meeting:
        await call.answer("Встреча не найдена", show_alert=True)
        return

    ts = meeting["created_at"].strftime("%d.%m.%Y %H:%M")
    topic = meeting.get("topic") or "Без темы"
    tags = meeting.get("tags") or []
    participants = meeting.get("participants") or []
    summary = meeting.get("summary") or "Протокол ещё не готов."
    status = meeting.get("status", "—")

    tags_str = " ".join(f"#{t}" for t in tags) if tags else "—"
    participants_str = ", ".join(participants) if participants else "—"
    status_icon = {"done": "✅", "error": "❌"}.get(status, "⏳")

    text = (
        f"{status_icon} <b>{topic}</b>\n"
        f"📅 {ts}\n"
        f"🏷 {tags_str}\n"
        f"👥 {participants_str}\n\n"
        f"📄 <b>Протокол:</b>\n{summary}"
    )

    await call.answer()
    if len(text) <= 4000:
        await call.message.answer(text)
    else:
        await call.message.answer(text[:4000])
        await call.message.answer(text[4000:])


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
        logger.exception("Error in ask for user %d", user_id)
        await thinking.delete()
        await message.answer(f"❌ Ошибка: {exc}", reply_markup=main_keyboard())


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
