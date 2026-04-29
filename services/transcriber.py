import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from faster_whisper import WhisperModel
from config import config

logger = logging.getLogger(__name__)

_model: WhisperModel | None = None
_executor = ThreadPoolExecutor(max_workers=config.TRANSCRIPTION_WORKERS)


def init_transcriber() -> None:
    """Load the Whisper model once at startup (blocking)."""
    global _model
    logger.info("Loading Whisper model '%s' …", config.WHISPER_MODEL)
    _model = WhisperModel(
        config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
    )
    logger.info("Whisper model loaded.")


def _transcribe_sync(audio_path: str) -> str:
    if _model is None:
        raise RuntimeError("Whisper model not initialized")
    segments, info = _model.transcribe(audio_path, language=config.WHISPER_LANGUAGE)
    logger.info(
        "Transcription done: detected language '%s' (%.0f%%)",
        info.language,
        info.language_probability * 100,
    )
    return " ".join(seg.text.strip() for seg in segments)


async def transcribe_audio(audio_path: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _transcribe_sync, audio_path)
