import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from config import config

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=config.TRANSCRIPTION_WORKERS)


@dataclass
class TranscriptSegment:
    start: float  # seconds from audio start
    end: float
    text: str


def _transcribe_sync(audio_path: str) -> list[TranscriptSegment]:
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
    result = [
        TranscriptSegment(seg.start, seg.end, seg.text.strip())
        for seg in segments
        if seg.text.strip()
    ]
    del model
    return result


def _find_speaker_at(timestamp: float, timeline: list[tuple[float, str]]) -> str:
    speaker = ""
    for t, name in timeline:
        if t <= timestamp:
            speaker = name
        else:
            break
    return speaker


def format_transcript(
    segments: list[TranscriptSegment],
    speaker_timeline: list[tuple[float, str]],
    recording_start_msk: datetime,
) -> str:
    lines = []
    for seg in segments:
        msk_time = recording_start_msk + timedelta(seconds=seg.start)
        time_str = msk_time.strftime("%H:%M:%S")
        speaker = _find_speaker_at(seg.start, speaker_timeline)
        prefix = f"[{time_str}] {speaker}:" if speaker else f"[{time_str}]"
        lines.append(f"{prefix} {seg.text}")
    return "\n".join(lines)


async def transcribe_audio(audio_path: str) -> list[TranscriptSegment]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _transcribe_sync, audio_path)
