import asyncpg
from config import config

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db() -> None:
    pool = await get_pool()
    with open("database/schema.sql") as f:
        schema = f.read()
    async with pool.acquire() as conn:
        await conn.execute(schema)


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
