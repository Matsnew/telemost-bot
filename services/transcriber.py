import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from config import config

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=config.TRANSCRIPTION_WORKERS)


def _transcribe_sync(audio_path: str) -> str:
    # Import and load model inside the thread — keeps it off the main heap
    # and allows GC to reclaim memory after transcription
    from faster_whisper import WhisperModel
    logger.info("Loading Whisper model '%s' for transcription …", config.WHISPER_MODEL)
    model = WhisperModel(
        config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
    )
    segments, info = model.transcribe(audio_path, language=config.WHISPER_LANGUAGE)
    logger.info(
        "Transcription done: language '%s' (%.0f%%)",
        info.language,
        info.language_probability * 100,
    )
    result = " ".join(seg.text.strip() for seg in segments)
    del model  # free RAM immediately
    return result


async def transcribe_audio(audio_path: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _transcribe_sync, audio_path)
