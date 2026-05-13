import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from config import config

_PAUSE_THRESHOLD = 5.0  # seconds of silence between segments to start a new block

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=config.TRANSCRIPTION_WORKERS)
# Whisper is heavy (~1.5 GB RAM). Run only one transcription at a time to avoid OOM.
_transcribe_semaphore = asyncio.Semaphore(1)


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


def _fmt_elapsed(seconds: float) -> str:
    """Format seconds as [MM:SS] or [HH:MM:SS] relative to recording start."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def format_transcript(
    segments: list[TranscriptSegment],
    speaker_timeline: list[tuple[float, str]],
    recording_start_msk: datetime,  # kept for API compatibility, no longer used for timestamps
) -> str:
    if not segments:
        return ""

    # Build blocks: merge consecutive segments from same speaker with no big pause
    blocks: list[dict] = []
    current: dict | None = None

    for seg in segments:
        speaker = _find_speaker_at(seg.start, speaker_timeline)
        gap = (seg.start - current["last_end"]) if current else 0.0
        same_speaker = current is not None and speaker == current["speaker"]

        if current is None or not same_speaker or gap > _PAUSE_THRESHOLD:
            if current is not None:
                blocks.append(current)
            current = {"start": seg.start, "speaker": speaker, "texts": [seg.text], "last_end": seg.end}
        else:
            current["texts"].append(seg.text)
            current["last_end"] = seg.end

    if current is not None:
        blocks.append(current)

    # Format blocks
    lines = []
    for block in blocks:
        time_str = _fmt_elapsed(block["start"])
        speaker = block["speaker"]
        text = " ".join(block["texts"])
        prefix = f"{time_str} {speaker}:" if speaker else time_str
        lines.append(f"{prefix} {text}")

    return "\n\n".join(lines)


async def transcribe_audio(audio_path: str) -> list[TranscriptSegment]:
    async with _transcribe_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, _transcribe_sync, audio_path)
