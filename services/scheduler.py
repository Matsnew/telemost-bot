import asyncio
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot

from config import config
from database import models
from services import recorder
from services.calendar_service import get_upcoming_events

logger = logging.getLogger(__name__)


async def _check_and_join(bot: Bot) -> None:
    users = await models.get_calendar_enabled_users()
    now = datetime.now(timezone.utc)

    for user in users:
        user_id = user["user_id"]
        join_before = user.get("join_minutes_before", config.CALENDAR_JOIN_BEFORE_MINUTES)
        auto_join_all = user.get("auto_join_all", False)

        try:
            events = await get_upcoming_events(user_id, days=1)
        except Exception:
            logger.exception("Failed to fetch calendar events for user %d", user_id)
            continue

        for event in events:
            start: datetime = event["start"]
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)

            minutes_until = (start - now).total_seconds() / 60
            if not (0 <= minutes_until <= join_before):
                continue

            google_id = event["google_id"]
            meeting_url = event["url"]

            if await models.is_calendar_event_joined(user_id, google_id):
                continue

            should_join = auto_join_all or await models.is_calendar_event_selected(user_id, google_id)
            if not should_join:
                continue

            await models.mark_calendar_event_joined(user_id, google_id)
            meeting_id = await models.create_meeting(user_id, meeting_url)
            await models.set_calendar_title(meeting_id, event["title"])
            recorder.start_recording(meeting_id, user_id, meeting_url, bot)

            await bot.send_message(
                user_id,
                f"📅 <b>Подключаюсь к встрече из календаря</b>\n"
                f"📋 {event['title']}\n"
                f"🕐 Начало: {start.astimezone().strftime('%H:%M')}\n"
                f"ID: <code>{meeting_id}</code>",
            )
            logger.info("Auto-joined calendar event %s for user %d", google_id, user_id)


async def run_scheduler(bot: Bot) -> None:
    logger.info("Calendar scheduler started")
    while True:
        try:
            await _check_and_join(bot)
        except Exception:
            logger.exception("Scheduler error")
        await asyncio.sleep(60)
