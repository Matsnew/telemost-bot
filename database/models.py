import uuid
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
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE meetings
            SET summary = $1, tags = $2, topic = $3, participants = $4
            WHERE id = $5
            """,
            summary,
            tags,
            topic,
            participants,
            meeting_id,
        )


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
