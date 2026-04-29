import asyncio
import logging
import os

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI

from config import config
from database.connection import close_db, init_db
from bot.handlers import router
from bot.middlewares import AllowedUsersMiddleware
from services.transcriber import init_transcriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── FastAPI (health / webhooks in the future) ──────────────────────────────
app = FastAPI(title="Telemost Bot", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── aiogram ────────────────────────────────────────────────────────────────
bot = Bot(
    token=config.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
dp.include_router(router)
dp.message.middleware(AllowedUsersMiddleware())


async def run_bot() -> None:
    logger.info("Starting aiogram polling")
    await dp.start_polling(bot, allowed_updates=["message"])


async def run_api() -> None:
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting uvicorn on :%d", port)
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    await server.serve()


async def main() -> None:
    logger.info("Initialising database …")
    await init_db()

    logger.info("Loading Whisper model …")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, init_transcriber)

    logger.info("Service ready.")
    try:
        await asyncio.gather(run_bot(), run_api())
    finally:
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
