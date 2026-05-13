import uuid
from datetime import datetime
from typing import Any
from database.connection import get_pool
from utils.encryption import encrypt, decrypt


# ── Users ──────────────────────────────────────────────────────────────────

async def upsert_user(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id) VALUES ($1)
            ON CONFLICT (id) DO NOTHING
            """,
            user_id,
        )


async def get_user(user_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1", user_id
        )
    return dict(row) if row else None


# ── Meetings ───────────────────────────────────────────────────────────────

async def create_meeting(user_id: int, meeting_url: str) -> str:
    pool = await get_pool()
    meeting_id = str(uuid.uuid4())
    encrypted_url = encrypt(meeting_url)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO meetings (id, user_id, meeting_url, status)
            VALUES ($1, $2, $3, 'started')
            """,
            meeting_id,
            user_id,
            encrypted_url,
        )
    return meeting_id


async def get_meeting(meeting_id: str, user_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM meetings WHERE id = $1 AND user_id = $2",
            meeting_id,
            user_id,
        )
    if not row:
        return None
    result = dict(row)
    if result.get("meeting_url"):
        result["meeting_url"] = decrypt(result["meeting_url"])
    return result


async def meeting_belongs_to_user(meeting_id: str, user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM meetings WHERE id = $1 AND user_id = $2",
            meeting_id, user_id,
        )
    return row is not None


async def get_meeting_raw(meeting_id: str, user_id: int) -> dict | None:
    """Get meeting without decrypting URL — safe for display purposes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM meetings WHERE id = $1 AND user_id = $2",
            meeting_id, user_id,
        )
    return dict(row) if row else None


async def update_meeting_status(meeting_id: str, status: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE meetings SET status = $1 WHERE id = $2",
            status,
            meeting_id,
        )


async def save_transcript(meeting_id: str, transcript: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE meetings SET transcript = $1 WHERE id = $2",
            transcript,
            meeting_id,
        )


async def save_analysis(
    meeting_id: str,
    summary: str,
    tags: list[str],
    topic: str,
    participants: list[str],
    meeting_type: str = "other",
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE meetings
            SET summary = $1, tags = $2, topic = $3, participants = $4, meeting_type = $5
            WHERE id = $6
            """,
            summary,
            tags,
            topic,
            participants,
            meeting_type,
            meeting_id,
        )


async def set_calendar_title(meeting_id: str, title: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE meetings SET calendar_title = $1 WHERE id = $2",
            title, meeting_id,
        )


async def get_existing_tags(user_id: int, limit: int = 20) -> list[str]:
    """Return the user's most frequently used tags for prompt context."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT unnest(tags) AS tag, COUNT(*) AS cnt
            FROM meetings
            WHERE user_id = $1 AND tags IS NOT NULL
            GROUP BY tag
            ORDER BY cnt DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    return [row["tag"] for row in rows]


async def get_all_tags(user_id: int) -> list[dict]:
    """Return all distinct tags with usage count, ordered by frequency."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT unnest(tags) AS tag, COUNT(*) AS cnt
            FROM meetings
            WHERE user_id = $1 AND tags IS NOT NULL
            GROUP BY tag
            ORDER BY cnt DESC, tag
            """,
            user_id,
        )
    return [{"tag": row["tag"], "count": row["cnt"]} for row in rows]


async def delete_tag_everywhere(user_id: int, tag: str) -> int:
    """Remove tag from all meetings, return number of affected meetings."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE meetings SET tags = array_remove(tags, $1)
            WHERE user_id = $2 AND $1 = ANY(tags)
            """,
            tag, user_id,
        )
    return int(result.split()[-1])


async def rename_tag_everywhere(user_id: int, old_tag: str, new_tag: str) -> int:
    """Rename tag in all meetings, return number of affected meetings."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE meetings SET tags = array_replace(tags, $1, $2)
            WHERE user_id = $3 AND $1 = ANY(tags)
            """,
            old_tag, new_tag, user_id,
        )
    return int(result.split()[-1])


async def reset_stuck_meetings() -> int:
    """On startup: mark meetings stuck in active states as error (service was restarted)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE meetings
            SET status = 'error',
                error_message = 'Прервано из-за перезапуска сервиса'
            WHERE status IN ('started', 'recording', 'transcribing', 'analyzing')
            """,
        )
    return int(result.split()[-1])


async def save_error(meeting_id: str, error_message: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE meetings SET status = 'error', error_message = $1 WHERE id = $2",
            error_message[:2000],
            meeting_id,
        )


async def get_active_recordings_count(user_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM meetings
            WHERE user_id = $1 AND status IN ('started', 'recording', 'transcribing', 'analyzing')
            """,
            user_id,
        )
    return row["cnt"] if row else 0


async def get_active_meetings(user_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, topic, created_at FROM meetings
            WHERE user_id = $1 AND status IN ('started', 'recording', 'transcribing', 'analyzing')
            ORDER BY created_at DESC
            """,
            user_id,
        )
    return [dict(r) for r in rows]


async def get_user_history(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, topic, tags, created_at FROM meetings
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    return [dict(r) for r in rows]


async def get_recent_meetings_by_tags(
    user_id: int, tags: list[str], exclude_id: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Find recent meetings with overlapping tags for context."""
    if not tags:
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT summary, topic, created_at FROM meetings
            WHERE user_id = $1
              AND id != $2
              AND status = 'done'
              AND tags && $3
            ORDER BY created_at DESC
            LIMIT $4
            """,
            user_id,
            exclude_id,
            tags,
            limit,
        )
    return [dict(r) for r in rows]


# ── Google Calendar ────────────────────────────────────────────────────────

async def save_google_token(
    user_id: int,
    access_token: str,
    refresh_token: str | None,
    token_expiry: datetime | None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO google_tokens (user_id, access_token, refresh_token, token_expiry, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_id) DO UPDATE
              SET access_token = EXCLUDED.access_token,
                  refresh_token = COALESCE(EXCLUDED.refresh_token, google_tokens.refresh_token),
                  token_expiry = EXCLUDED.token_expiry,
                  updated_at = NOW()
            """,
            user_id,
            encrypt(access_token),
            encrypt(refresh_token) if refresh_token else None,
            token_expiry,
        )


async def get_google_token(user_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM google_tokens WHERE user_id = $1", user_id
        )
    if not row:
        return None
    result = dict(row)
    result["access_token"] = decrypt(result["access_token"])
    if result.get("refresh_token"):
        result["refresh_token"] = decrypt(result["refresh_token"])
    return result


async def delete_google_token(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM google_tokens WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM calendar_settings WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM calendar_events WHERE user_id = $1", user_id)


async def get_calendar_settings(user_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM calendar_settings WHERE user_id = $1", user_id
        )
    if not row:
        return {"user_id": user_id, "enabled": True, "auto_join_all": False, "join_minutes_before": 1}
    return dict(row)


async def save_calendar_settings(
    user_id: int,
    enabled: bool,
    auto_join_all: bool,
    join_minutes_before: int,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO calendar_settings (user_id, enabled, auto_join_all, join_minutes_before)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
              SET enabled = EXCLUDED.enabled,
                  auto_join_all = EXCLUDED.auto_join_all,
                  join_minutes_before = EXCLUDED.join_minutes_before
            """,
            user_id, enabled, auto_join_all, join_minutes_before,
        )


async def get_calendar_enabled_users() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT cs.user_id, cs.auto_join_all, cs.join_minutes_before
            FROM calendar_settings cs
            JOIN google_tokens gt ON gt.user_id = cs.user_id
            WHERE cs.enabled = TRUE
            """
        )
    return [dict(r) for r in rows]


async def upsert_calendar_event(
    user_id: int,
    google_id: str,
    title: str,
    start_time: datetime,
    meeting_url: str,
) -> None:
    pool = await get_pool()
    event_date = start_time.date()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO calendar_events
              (user_id, google_id, title, start_time, meeting_url, event_date)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (user_id, google_id) DO UPDATE
              SET title = EXCLUDED.title,
                  start_time = EXCLUDED.start_time,
                  meeting_url = EXCLUDED.meeting_url,
                  event_date = EXCLUDED.event_date
            """,
            user_id, google_id, title, start_time, meeting_url, event_date,
        )


async def get_calendar_events(
    user_id: int, date_from: datetime, date_to: datetime
) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM calendar_events
            WHERE user_id = $1 AND start_time >= $2 AND start_time < $3
            ORDER BY start_time
            """,
            user_id, date_from, date_to,
        )
    return [dict(r) for r in rows]


async def set_calendar_event_selected(
    user_id: int, google_id: str, selected: bool
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE calendar_events SET selected = $1 WHERE user_id = $2 AND google_id = $3",
            selected, user_id, google_id,
        )


async def is_calendar_event_joined(user_id: int, google_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT joined FROM calendar_events WHERE user_id = $1 AND google_id = $2",
            user_id, google_id,
        )
    return bool(row and row["joined"])


async def mark_calendar_event_joined(user_id: int, google_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE calendar_events SET joined = TRUE WHERE user_id = $1 AND google_id = $2",
            user_id, google_id,
        )


async def is_calendar_event_selected(user_id: int, google_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT selected FROM calendar_events WHERE user_id = $1 AND google_id = $2",
            user_id, google_id,
        )
    return bool(row and row["selected"])


async def get_all_summaries(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Load summaries for /ask — only current user, no transcripts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT topic, summary, tags, participants, created_at FROM meetings
            WHERE user_id = $1 AND status = 'done' AND summary IS NOT NULL
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    return [dict(r) for r in rows]
