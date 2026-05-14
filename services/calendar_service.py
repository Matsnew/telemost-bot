import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from config import config
from database import models

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
_TELEMOST_RE = re.compile(r"https?://telemost\.yandex\.ru/\S+")

# In-memory OAuth state store: state -> (user_id, expires_at)
_oauth_states: dict[str, tuple[int, float]] = {}


# ── OAuth ──────────────────────────────────────────────────────────────────

def _make_flow():
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(
        {
            "web": {
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [config.GOOGLE_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=config.GOOGLE_REDIRECT_URI,
    )


def get_auth_url(user_id: int) -> str:
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = (user_id, time.monotonic() + 600)  # 10 min TTL
    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    return auth_url


def _exchange_code_sync(code: str) -> tuple[str, str | None, datetime | None]:
    flow = _make_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    return creds.token, creds.refresh_token, creds.expiry


async def handle_oauth_callback(code: str, state: str) -> int | None:
    """Exchange code for tokens. Returns user_id on success, None on failure."""
    entry = _oauth_states.pop(state, None)
    if not entry:
        logger.warning("Unknown OAuth state: %s", state)
        return None
    user_id, expires_at = entry
    if time.monotonic() > expires_at:
        logger.warning("OAuth state expired for user %d", user_id)
        return None

    loop = asyncio.get_running_loop()
    access_token, refresh_token, expiry = await loop.run_in_executor(
        None, _exchange_code_sync, code
    )

    await models.save_google_token(user_id, access_token, refresh_token, expiry)
    # Create default calendar settings
    await models.save_calendar_settings(
        user_id,
        enabled=True,
        auto_join_all=False,
        join_minutes_before=config.CALENDAR_JOIN_BEFORE_MINUTES,
    )
    logger.info("Google Calendar connected for user %d", user_id)
    return user_id


# ── Calendar API ───────────────────────────────────────────────────────────

def _extract_telemost_url(event: dict) -> str | None:
    for field in [event.get("location", ""), event.get("description", "")]:
        if field:
            m = _TELEMOST_RE.search(field)
            if m:
                return m.group(0)
    for ep in event.get("conferenceData", {}).get("entryPoints", []):
        uri = ep.get("uri", "")
        if "telemost" in uri:
            return uri
    return None


def _fetch_events_sync(token_row: dict, days: int) -> list[dict[str, Any]]:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials(
        token=token_row["access_token"],
        refresh_token=token_row.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        expiry=token_row.get("token_expiry"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_row["_refreshed"] = {
            "access_token": creds.token,
            "expiry": creds.expiry,
        }

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days)

    # Get all calendars the user has access to (own + shared by colleagues)
    cal_list = service.calendarList().list(showHidden=False).execute()
    calendars = [
        c for c in cal_list.get("items", [])
        if not c.get("deleted", False)
        and c.get("accessRole") in ("owner", "writer", "reader")
    ]

    events: list[dict] = []
    seen_ids: set[str] = set()  # deduplicate events that appear in multiple calendars

    for cal in calendars:
        cal_id = cal["id"]
        is_primary = cal.get("primary", False)
        cal_name = "" if is_primary else cal.get("summary", cal_id)

        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            ).execute()
        except Exception as e:
            logger.warning("Failed to fetch events from calendar %s: %s", cal_id, e)
            continue

        for item in result.get("items", []):
            google_id = item["id"]
            if google_id in seen_ids:
                continue
            seen_ids.add(google_id)

            telemost_url = _extract_telemost_url(item)
            if not telemost_url:
                continue

            start_raw = item["start"].get("dateTime") or item["start"].get("date")
            try:
                start_dt = datetime.fromisoformat(start_raw)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            events.append({
                "google_id": google_id,
                "title": item.get("summary", "Без названия"),
                "start": start_dt,
                "url": telemost_url,
                "calendar_name": cal_name,  # empty string for own calendar
            })

    events.sort(key=lambda e: e["start"])
    return events


async def get_upcoming_events(user_id: int, days: int = 7) -> list[dict[str, Any]]:
    token_row = await models.get_google_token(user_id)
    if not token_row:
        return []

    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, _fetch_events_sync, token_row, days)

    # Save refreshed token if needed
    if token_row.get("_refreshed"):
        r = token_row["_refreshed"]
        await models.save_google_token(user_id, r["access_token"], None, r["expiry"])

    # Upsert events into DB
    for ev in events:
        await models.upsert_calendar_event(
            user_id, ev["google_id"], ev["title"], ev["start"], ev["url"],
            ev.get("calendar_name", ""),
        )

    return events
