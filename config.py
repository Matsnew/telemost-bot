import os
from typing import List


def _parse_user_ids(raw: str) -> List[int]:
    return [int(uid.strip()) for uid in raw.split(",") if uid.strip()]


class Config:
    OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
    TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
    DATABASE_URL: str = os.environ["DATABASE_URL"]
    ENCRYPTION_KEY: str = os.environ["ENCRYPTION_KEY"]

    ALLOWED_USER_IDS: List[int] = _parse_user_ids(
        os.environ.get("ALLOWED_USER_IDS", "")
    )

    # Recording
    MAX_RECORDING_DURATION: int = 10800  # 3 hours
    DEFAULT_MAX_CONCURRENT: int = 2
    PARTICIPANT_POLL_INTERVAL: int = 30  # seconds
    ALONE_THRESHOLD: int = 2  # consecutive alone-polls before stopping

    # Whisper
    WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "medium")
    WHISPER_LANGUAGE: str = "ru"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    TRANSCRIPTION_WORKERS: int = 2

    # Claude
    OPENAI_MODEL: str = "gpt-4o"
    CONTEXT_MEETINGS_LIMIT: int = 3
    ASK_SUMMARIES_LIMIT: int = 50

    # Rate limiting
    ASK_RATE_LIMIT: int = 10  # requests per minute

    # System
    AUDIO_DIR: str = "/tmp"
    DISPLAY: str = ":99"


config = Config()
