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
from database.connection import get_pool
from services import recorder
from services.analyzer import answer_question
from services.calendar_service import get_auth_url, get_upcoming_events
from bot.rate_limiter import check_ask_rate_limit
from utils.time import fmt_msk

logger = logging.getLogger(__name__)
router = Router()

TELEMOST_URL_RE = re.compile(r"https?://telemost\.yandex\.ru/\S+")


# ── FSM ───────────────────────────────────────────────────────────────────

class AskState(StatesGroup):
    waiting_question = State()


class EditTagsState(StatesGroup):
    waiting_new_tag = State()


class RenameTagState(StatesGroup):
    waiting_new_name = State()


# ── Keyboards ─────────────────────────────────────────────────────────────

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Активные записи"), KeyboardButton(text="📚 История встреч")],
            [KeyboardButton(text="🔍 Задать вопрос"), KeyboardButton(text="ℹ️ Помощь")],
            [KeyboardButton(text="📅 Календарь")],
        ],
        resize_keyboard=True,
        persistent=True,
    )


# ── Calendar keyboards ─────────────────────────────────────────────────────

def calendar_menu_inline(connected: bool, auto_join: bool) -> InlineKeyboardMarkup:
    if not connected:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔗 Подключить Google Calendar", callback_data="cal:connect"),
        ]])
    mode_label = "✅ Все встречи автоматически" if auto_join else "✅ Только выбранные"
    toggle_label = "Переключить на ручной выбор" if auto_join else "Переключить на автоматический"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Режим: {mode_label}", callback_data="cal:noop")],
        [InlineKeyboardButton(text=toggle_label, callback_data="cal:toggle_mode")],
        [
            InlineKeyboardButton(text="📋 Встречи сегодня", callback_data="cal:today"),
            InlineKeyboardButton(text="📋 На неделю", callback_data="cal:week"),
        ],
        [InlineKeyboardButton(text="🔓 Отключить календарь", callback_data="cal:disconnect")],
    ])


def events_inline(
    events: list[dict],
    selected_ids: set[str],
    show_select: bool,
) -> InlineKeyboardMarkup:
    buttons = []
    for ev in events:
        gid = ev["google_id"]
        start = ev["start"]
        time_str = start.strftime("%d.%m %H:%M") if hasattr(start, "strftime") else str(start)
        title = (ev.get("title") or "Без названия")[:28]
        cal_name = ev.get("calendar_name") or ""
        owner = f" [{cal_name[:15]}]" if cal_name else ""
        label = f"{'✅' if gid in selected_ids else '⬜'} {time_str} — {title}{owner}"
        row = [InlineKeyboardButton(text=label, callback_data=f"cal:ev:{gid}")]
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def all_tags_inline(tags: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for item in tags:
        tag = item["tag"]
        cnt = item["count"]
        buttons.append([InlineKeyboardButton(
            text=f"🏷 #{tag}  ×{cnt}",
            callback_data=f"tag_manage:{tag}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def tag_actions_inline(tag: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"tag_rename:{tag}"),
            InlineKeyboardButton(text="🗑 Удалить везде", callback_data=f"tag_delete:{tag}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="tags_list")],
    ])


def tags_edit_inline(meeting_id: str, tags: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for tag in tags:
        buttons.append([InlineKeyboardButton(
            text=f"❌ #{tag}",
            callback_data=f"tags_rm:{meeting_id}:{tag}",
        )])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить тег", callback_data=f"tags_add:{meeting_id}"),
        InlineKeyboardButton(text="✅ Готово", callback_data=f"tags_done:{meeting_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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
    buttons.append([
        InlineKeyboardButton(text="🏷 Изменить теги", callback_data=f"edit_tags:{meeting_id}")
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
    audio_path = f"{config.AUDIO_DIR}/{meeting_id}.wav"
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

    audio_path = f"{config.AUDIO_DIR}/{meeting_id}.wav"
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


# ── Calendar handlers ──────────────────────────────────────────────────────

@router.message(F.text == "📅 Календарь")
async def cmd_calendar(message: Message) -> None:
    user_id = message.from_user.id
    token = await models.get_google_token(user_id)
    if not token:
        await message.answer(
            "📅 <b>Google Calendar</b>\n\nКалендарь не подключён.",
            reply_markup=calendar_menu_inline(connected=False, auto_join=False),
        )
        return
    settings = await models.get_calendar_settings(user_id)
    await message.answer(
        "📅 <b>Google Calendar подключён</b>\n\nВыбери действие:",
        reply_markup=calendar_menu_inline(connected=True, auto_join=settings["auto_join_all"]),
    )


@router.callback_query(F.data == "cal:connect")
async def cb_cal_connect(call: CallbackQuery) -> None:
    if not config.GOOGLE_CLIENT_ID:
        await call.answer("Google Calendar не настроен на сервере.", show_alert=True)
        return
    url = get_auth_url(call.from_user.id)
    await call.answer()
    await call.message.answer(
        f"🔗 Перейди по ссылке для авторизации Google Calendar:\n\n{url}\n\n"
        "После авторизации вернись сюда — бот пришлёт подтверждение."
    )


@router.callback_query(F.data == "cal:disconnect")
async def cb_cal_disconnect(call: CallbackQuery) -> None:
    await models.delete_google_token(call.from_user.id)
    await call.answer("Календарь отключён.")
    await call.message.edit_text(
        "📅 <b>Google Calendar отключён.</b>",
        reply_markup=calendar_menu_inline(connected=False, auto_join=False),
    )


@router.callback_query(F.data == "cal:toggle_mode")
async def cb_cal_toggle_mode(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    settings = await models.get_calendar_settings(user_id)
    new_mode = not settings["auto_join_all"]
    await models.save_calendar_settings(
        user_id,
        enabled=settings["enabled"],
        auto_join_all=new_mode,
        join_minutes_before=settings["join_minutes_before"],
    )
    mode_text = "все встречи автоматически ✅" if new_mode else "только выбранные вручную ✅"
    await call.answer(f"Режим изменён: {mode_text}")
    await call.message.edit_reply_markup(
        reply_markup=calendar_menu_inline(connected=True, auto_join=new_mode)
    )


@router.callback_query(F.data == "cal:noop")
async def cb_cal_noop(call: CallbackQuery) -> None:
    await call.answer()


async def _show_events(call: CallbackQuery, days: int) -> None:
    user_id = call.from_user.id
    await call.answer("Загружаю встречи…")
    try:
        events = await get_upcoming_events(user_id, days=days)
    except Exception as exc:
        await call.message.answer(f"❌ Ошибка загрузки календаря: {exc}")
        return

    if not events:
        period = "сегодня" if days == 1 else f"на {days} дней"
        await call.message.answer(f"📋 Встреч с Телемостом {period} нет.")
        return

    settings = await models.get_calendar_settings(user_id)
    auto_join = settings["auto_join_all"]

    # Get selected event IDs from DB
    from datetime import timezone as _tz
    from datetime import datetime as _dt
    now = _dt.now(_tz.utc)
    db_events = await models.get_calendar_events(
        user_id,
        date_from=now,
        date_to=now + __import__("datetime").timedelta(days=days),
    )
    selected_ids = {e["google_id"] for e in db_events if e["selected"]}

    # Merge calendar_name from DB into the events list for display
    cal_name_by_gid = {e["google_id"]: e.get("calendar_name", "") for e in db_events}
    for ev in events:
        if not ev.get("calendar_name"):
            ev["calendar_name"] = cal_name_by_gid.get(ev["google_id"], "")

    period_label = "сегодня" if days == 1 else f"на {days} дней"
    mode_hint = (
        "Режим: <b>автоматически</b> — подключусь ко всем."
        if auto_join
        else "Режим: <b>ручной</b> — нажми на встречу чтобы выбрать/убрать ✅."
    )
    # Show note if any events are from colleagues' calendars
    colleagues = {ev["calendar_name"] for ev in events if ev.get("calendar_name")}
    col_hint = f"\n👥 Включая календари: {', '.join(sorted(colleagues))}" if colleagues else ""
    await call.message.answer(
        f"📋 <b>Встречи {period_label}:</b>\n{mode_hint}{col_hint}",
        reply_markup=events_inline(events, selected_ids, show_select=not auto_join),
    )


@router.callback_query(F.data == "cal:today")
async def cb_cal_today(call: CallbackQuery) -> None:
    await _show_events(call, days=1)


@router.callback_query(F.data == "cal:week")
async def cb_cal_week(call: CallbackQuery) -> None:
    await _show_events(call, days=7)


@router.callback_query(F.data.startswith("cal:ev:"))
async def cb_cal_event_toggle(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    google_id = call.data[len("cal:ev:"):]

    settings = await models.get_calendar_settings(user_id)
    if settings["auto_join_all"]:
        await call.answer("В автоматическом режиме все встречи подключаются сами.", show_alert=True)
        return

    currently = await models.is_calendar_event_selected(user_id, google_id)
    new_val = not currently
    await models.set_calendar_event_selected(user_id, google_id, new_val)
    status = "добавлена ✅" if new_val else "убрана ❌"
    await call.answer(f"Встреча {status}")

    # Refresh the message keyboard
    from datetime import timezone as _tz, datetime as _dt, timedelta as _td
    now = _dt.now(_tz.utc)
    db_events = await models.get_calendar_events(user_id, date_from=now, date_to=now + _td(days=7))
    selected_ids = {e["google_id"] for e in db_events if e["selected"]}
    all_events = await get_upcoming_events(user_id, days=7)
    await call.message.edit_reply_markup(
        reply_markup=events_inline(all_events, selected_ids, show_select=True)
    )


def _tags_edit_text(tags: list[str]) -> str:
    tags_str = " ".join(f"#{t}" for t in tags) if tags else "—"
    return (
        f"🏷 <b>Редактирование тегов</b>\n\n"
        f"Текущие теги: {tags_str}\n\n"
        "Нажми ❌ рядом с тегом чтобы убрать его, или ➕ чтобы добавить новый."
    )


@router.callback_query(F.data.startswith("edit_tags:"))
async def cb_edit_tags(call: CallbackQuery) -> None:
    meeting_id = call.data.split(":", 1)[1]
    if not await models.meeting_belongs_to_user(meeting_id, call.from_user.id):
        await call.answer("Встреча не найдена", show_alert=True)
        return
    meeting = await models.get_meeting_raw(meeting_id, call.from_user.id)
    tags = list(meeting.get("tags") or [])
    await call.answer()
    await call.message.answer(
        _tags_edit_text(tags),
        reply_markup=tags_edit_inline(meeting_id, tags),
    )


@router.callback_query(F.data.startswith("tags_rm:"))
async def cb_tags_remove(call: CallbackQuery) -> None:
    _, meeting_id, tag = call.data.split(":", 2)
    if not await models.meeting_belongs_to_user(meeting_id, call.from_user.id):
        await call.answer("Встреча не найдена", show_alert=True)
        return
    meeting = await models.get_meeting_raw(meeting_id, call.from_user.id)
    tags = list(meeting.get("tags") or [])
    if tag in tags:
        tags.remove(tag)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE meetings SET tags = $1 WHERE id = $2", tags, meeting_id)
    await call.answer(f"Тег #{tag} удалён")
    await call.message.edit_text(
        _tags_edit_text(tags),
        reply_markup=tags_edit_inline(meeting_id, tags),
    )


@router.callback_query(F.data.startswith("tags_add:"))
async def cb_tags_add(call: CallbackQuery, state: FSMContext) -> None:
    meeting_id = call.data.split(":", 1)[1]
    if not await models.meeting_belongs_to_user(meeting_id, call.from_user.id):
        await call.answer("Встреча не найдена", show_alert=True)
        return
    await state.set_state(EditTagsState.waiting_new_tag)
    await state.update_data(meeting_id=meeting_id)
    await call.answer()
    await call.message.answer(
        "➕ Отправь название нового тега:\n"
        "Например: <code>Selectel</code> или <code>проект Альфа</code>",
        reply_markup=cancel_keyboard(),
    )


@router.message(EditTagsState.waiting_new_tag)
async def cmd_tags_add_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    meeting_id = data.get("meeting_id")
    await state.clear()

    raw = (message.text or "").strip().lstrip("#")
    if not raw:
        await message.answer("Тег не добавлен.", reply_markup=main_keyboard())
        return

    meeting = await models.get_meeting_raw(meeting_id, message.from_user.id)
    tags = list(meeting.get("tags") or [])
    if raw not in tags:
        tags.append(raw)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE meetings SET tags = $1 WHERE id = $2", tags, meeting_id)

    await message.answer(
        _tags_edit_text(tags),
        reply_markup=tags_edit_inline(meeting_id, tags),
    )


@router.callback_query(F.data.startswith("tags_done:"))
async def cb_tags_done(call: CallbackQuery) -> None:
    meeting_id = call.data.split(":", 1)[1]
    meeting = await models.get_meeting_raw(meeting_id, call.from_user.id)
    tags = list(meeting.get("tags") or [])
    tags_str = " ".join(f"#{t}" for t in tags) if tags else "—"
    await call.answer("Сохранено ✅")
    await call.message.edit_text(f"✅ Теги сохранены: {tags_str}")


@router.message(Command("tags"))
async def cmd_tags(message: Message) -> None:
    user_id = message.from_user.id
    tags = await models.get_all_tags(user_id)
    if not tags:
        await message.answer("🏷 Тегов пока нет — они появятся после первой встречи.")
        return
    await message.answer(
        "🏷 <b>Все теги</b>\n"
        "Цифра рядом — сколько встреч. "
        "Нажми на тег чтобы переименовать или удалить из всех встреч:",
        reply_markup=all_tags_inline(tags),
    )


@router.callback_query(F.data == "tags_list")
async def cb_tags_list(call: CallbackQuery) -> None:
    tags = await models.get_all_tags(call.from_user.id)
    await call.answer()
    if not tags:
        await call.message.edit_text("🏷 Тегов пока нет.")
        return
    await call.message.edit_text(
        "🏷 <b>Все теги</b>\n"
        "Цифра рядом — сколько встреч. "
        "Нажми на тег чтобы переименовать или удалить из всех встреч:",
        reply_markup=all_tags_inline(tags),
    )


@router.callback_query(F.data.startswith("tag_manage:"))
async def cb_tag_manage(call: CallbackQuery) -> None:
    tag = call.data[len("tag_manage:"):]
    await call.answer()
    await call.message.edit_text(
        f"🏷 Тег <b>#{tag}</b>\nВыбери действие:",
        reply_markup=tag_actions_inline(tag),
    )


@router.callback_query(F.data.startswith("tag_delete:"))
async def cb_tag_delete(call: CallbackQuery) -> None:
    tag = call.data[len("tag_delete:"):]
    count = await models.delete_tag_everywhere(call.from_user.id, tag)
    await call.answer(f"#{tag} удалён из {count} встреч", show_alert=True)
    tags = await models.get_all_tags(call.from_user.id)
    if not tags:
        await call.message.edit_text("🏷 Тегов больше нет.")
        return
    await call.message.edit_text(
        "🏷 <b>Все теги</b>\nНажми на тег чтобы переименовать или удалить из всех встреч:",
        reply_markup=all_tags_inline(tags),
    )


@router.callback_query(F.data.startswith("tag_rename:"))
async def cb_tag_rename(call: CallbackQuery, state: FSMContext) -> None:
    tag = call.data[len("tag_rename:"):]
    await state.set_state(RenameTagState.waiting_new_name)
    await state.update_data(old_tag=tag)
    await call.answer()
    await call.message.answer(
        f"✏️ Введи новое название для тега <b>#{tag}</b>:",
        reply_markup=cancel_keyboard(),
    )


@router.message(RenameTagState.waiting_new_name)
async def cmd_tag_rename_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    old_tag = data.get("old_tag")
    await state.clear()

    new_tag = (message.text or "").strip().lstrip("#")
    if not new_tag:
        await message.answer("Переименование отменено.", reply_markup=main_keyboard())
        return

    count = await models.rename_tag_everywhere(message.from_user.id, old_tag, new_tag)
    await message.answer(
        f"✅ <b>#{old_tag}</b> → <b>#{new_tag}</b> в {count} встречах.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("reprocess"))
async def cmd_reprocess(message: Message, bot: Bot) -> None:
    """Временная команда: /reprocess <meeting_id> — повторно обработать запись."""
    import os as _os
    from services.transcriber import transcribe_audio, format_transcript
    from services.analyzer import analyze_meeting
    from utils.time import now_msk

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /reprocess <meeting_id>")
        return

    meeting_id = args[1].strip()
    user_id = message.from_user.id

    if not await models.meeting_belongs_to_user(meeting_id, user_id):
        await message.answer("❌ Встреча не найдена.")
        return

    audio_path = _os.path.join("/audio", f"{meeting_id}.wav")
    if not _os.path.exists(audio_path):
        # fallback to /tmp
        audio_path = f"{config.AUDIO_DIR}/{meeting_id}.wav"
    if not _os.path.exists(audio_path):
        await message.answer(f"❌ Аудиофайл не найден:\n<code>{audio_path}</code>")
        return

    await message.answer("🎙 Транскрибирую запись…")
    try:
        segments = await transcribe_audio(audio_path)
        transcript = format_transcript(segments, [], now_msk())
        await models.save_transcript(meeting_id, transcript)

        await message.answer("🤖 Анализирую встречу…")
        summary, tags, topic, participants, meeting_type = await analyze_meeting(
            meeting_id, user_id, transcript, []
        )
        await models.save_analysis(meeting_id, summary, tags, topic, participants, meeting_type)
        await models.update_meeting_status(meeting_id, "done")

        tags_str = ", ".join(f"#{t}" for t in tags) if tags else "—"
        participants_str = ", ".join(participants) if participants else "—"
        type_icons = {
            "sales": "🤝", "internal": "🏠", "planning": "📅",
            "review": "🔍", "interview": "👤", "partner": "🤝", "other": "📌",
        }
        header = (
            f"✅ <b>Встреча обработана</b>\n\n"
            f"📋 <b>Тема:</b> {topic}\n"
            f"{type_icons.get(meeting_type, '📌')} <b>Тип:</b> {meeting_type}\n"
            f"🏷 <b>Теги:</b> {tags_str}\n"
            f"👥 <b>Участники:</b> {participants_str}\n\n"
            f"📄 <b>Протокол:</b>\n"
        )
        full_msg = header + summary
        if len(full_msg) <= 4000:
            await message.answer(full_msg)
        else:
            await message.answer(header)
            for chunk_start in range(0, len(summary), 4000):
                await message.answer(summary[chunk_start:chunk_start + 4000])

        row = [InlineKeyboardButton(text="📝 Транскрипт", callback_data=f"transcript:{meeting_id}")]
        if _os.path.exists(audio_path):
            row.append(InlineKeyboardButton(text="🎵 Аудио", callback_data=f"audio:{meeting_id}"))
        await message.answer(
            "⬆️ Нажми чтобы получить транскрипт или аудио:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[row]),
        )
    except Exception as exc:
        logger.exception("Reprocess error for %s", meeting_id)
        await message.answer(f"❌ Ошибка: <code>{str(exc)[:500]}</code>")


@router.message(Command("storage"))
async def cmd_storage(message: Message) -> None:
    """Показать содержимое /audio — список WAV-файлов с размерами и датами."""
    import os as _os
    from datetime import datetime

    audio_dir = config.AUDIO_DIR
    try:
        files = [f for f in _os.listdir(audio_dir) if f.endswith(".wav")]
    except FileNotFoundError:
        await message.answer(f"❌ Директория <code>{audio_dir}</code> не найдена.")
        return

    if not files:
        await message.answer(f"📂 <code>{audio_dir}</code> пуста — аудиофайлов нет.")
        return

    # Собираем статистику
    file_infos = []
    total_bytes = 0
    for fname in sorted(files):
        fpath = _os.path.join(audio_dir, fname)
        try:
            stat = _os.stat(fpath)
            size = stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime)
            total_bytes += size
            file_infos.append((fname, size, mtime))
        except OSError:
            continue

    # Сортируем по дате (новые первые)
    file_infos.sort(key=lambda x: x[2], reverse=True)

    def fmt_size(b: int) -> str:
        if b >= 1024 ** 3:
            return f"{b / 1024 ** 3:.1f} ГБ"
        if b >= 1024 ** 2:
            return f"{b / 1024 ** 2:.1f} МБ"
        return f"{b / 1024:.0f} КБ"

    lines = [
        f"💾 <b>Хранилище {audio_dir}</b>",
        f"📁 Файлов: {len(file_infos)} | 📊 Итого: {fmt_size(total_bytes)}",
        "",
    ]
    for fname, size, mtime in file_infos:
        short = fname[:8] + "…" + fname[-8:] if len(fname) > 20 else fname
        lines.append(f"<code>{short}</code>  {fmt_size(size)}  {mtime.strftime('%d.%m %H:%M')}")

    await message.answer("\n".join(lines), parse_mode="HTML")


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
