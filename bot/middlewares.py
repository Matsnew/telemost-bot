from typing import Any, Callable, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message
from config import config


class AllowedUsersMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in config.ALLOWED_USER_IDS:
            if isinstance(event, Message):
                await event.answer("❌ У вас нет доступа к этому боту.")
            return None
        return await handler(event, data)
