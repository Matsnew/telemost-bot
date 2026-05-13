import asyncio
import logging
import os

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config import config
from database.connection import close_db, init_db
from bot.handlers import router
from bot.middlewares import AllowedUsersMiddleware
from services.scheduler import run_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Telemost Bot", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/oauth/google/callback")
async def oauth_google_callback(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    if error or not code:
        return HTMLResponse("<h2>❌ Авторизация отменена.</h2><p>Можешь закрыть эту страницу.</p>")

    from services.calendar_service import handle_oauth_callback
    user_id = await handle_oauth_callback(code, state)
    if not user_id:
        return HTMLResponse("<h2>❌ Ошибка авторизации.</h2><p>Попробуй ещё раз через бота.</p>")

    # Notify user in Telegram
    try:
        await bot.send_message(
            user_id,
            "✅ <b>Google Calendar подключён!</b>\n\n"
            "Нажми кнопку 📅 Календарь чтобы настроить режим и посмотреть встречи.",
        )
    except Exception:
        pass

    return HTMLResponse(
        "<h2>✅ Google Calendar успешно подключён!</h2>"
        "<p>Можешь закрыть эту страницу и вернуться в Telegram.</p>"
    )


bot = Bot(
    token=config.TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)
dp.message.middleware(AllowedUsersMiddleware())


async def run_bot() -> None:
    logger.info("Starting aiogram polling")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


async def run_api() -> None:
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting uvicorn on :%d", port)
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    await server.serve()


async def main() -> None:
    logger.info("Initialising database …")
    await init_db()
    logger.info("Service ready.")
    try:
        await asyncio.gather(run_bot(), run_api(), run_scheduler(bot))
    finally:
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
