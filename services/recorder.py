import asyncio
import logging
import os
from aiogram import Bot
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from config import config
from database import models
from services.transcriber import transcribe_audio
from services.analyzer import analyze_meeting

logger = logging.getLogger(__name__)

# Global task registry: { meeting_id: asyncio.Task }
active_recordings: dict[str, asyncio.Task] = {}

# ── Playwright selectors (Telemost UI) ────────────────────────────────────

_NAME_INPUT_SELECTORS = [
    'input[placeholder*="имя" i]',
    'input[placeholder*="name" i]',
    '[data-testid*="name"] input',
    '[class*="name"] input',
    'input[type="text"]',
]

_JOIN_BUTTON_SELECTORS = [
    'button:has-text("Войти")',
    'button:has-text("Присоединиться")',
    'button:has-text("Join")',
    '[data-testid*="join"]',
    '[class*="join"] button',
]

_PARTICIPANT_SELECTORS = [
    '[class*="participant-item"]',
    '[class*="attendee"]',
    '[data-testid*="participant"]',
    '[class*="video-tile"]',
    '[class*="roster-item"]',
]

_MEETING_ENDED_SELECTORS = [
    ':text("встреча завершена")',
    ':text("meeting ended")',
    ':text("звонок завершён")',
    '[class*="meeting-ended"]',
    '[class*="call-ended"]',
]

# ── PulseAudio helpers ────────────────────────────────────────────────────


async def _create_pulse_sink(sink_name: str) -> int | None:
    """Load a null-sink and return its module index."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "load-module", "module-null-sink", f"sink_name={sink_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            module_index = int(stdout.strip())
            logger.info("PulseAudio sink '%s' created (module %d)", sink_name, module_index)
            return module_index
    except Exception:
        logger.exception("Failed to create PulseAudio sink '%s'", sink_name)
    return None


async def _remove_pulse_sink(module_index: int) -> None:
    try:
        await asyncio.create_subprocess_exec(
            "pactl", "unload-module", str(module_index)
        )
        logger.info("PulseAudio module %d unloaded", module_index)
    except Exception:
        logger.exception("Failed to unload PulseAudio module %d", module_index)


async def _start_audio_capture(audio_path: str, sink_name: str) -> asyncio.subprocess.Process:
    cmd = (
        f"parec --device={sink_name}.monitor --format=s16le --rate=16000 --channels=1 | "
        f"ffmpeg -y -f s16le -ar 16000 -ac 1 -i pipe:0 {audio_path}"
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    logger.info("Audio capture started → %s", audio_path)
    return proc


# ── Playwright meeting join ───────────────────────────────────────────────


async def _find_element(page, selectors: list[str], timeout_ms: int = 3000):
    for selector in selectors:
        try:
            el = page.locator(selector).first
            await el.wait_for(state="visible", timeout=timeout_ms)
            return el
        except Exception:
            continue
    return None


async def _join_meeting(page, meeting_url: str) -> None:
    logger.info("Navigating to meeting URL")
    await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60_000)

    name_input = await _find_element(page, _NAME_INPUT_SELECTORS, timeout_ms=30_000)
    if name_input is None:
        raise RuntimeError("Не найдено поле ввода имени на странице встречи")

    await name_input.fill("Протоколист")
    logger.info("Name entered")

    join_btn = await _find_element(page, _JOIN_BUTTON_SELECTORS, timeout_ms=5_000)
    if join_btn is None:
        raise RuntimeError("Не найдена кнопка входа")

    await join_btn.click()
    logger.info("Join button clicked")

    # Wait for meeting room to initialise
    await asyncio.sleep(5)
    logger.info("In meeting room")


async def _count_participants(page) -> int:
    # Check if meeting ended
    for sel in _MEETING_ENDED_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return 0
        except Exception:
            pass

    for sel in _PARTICIPANT_SELECTORS:
        try:
            count = await page.locator(sel).count()
            if count > 0:
                return count
        except Exception:
            pass
    return -1  # Unknown — assume still going


async def _wait_for_meeting_end(page) -> None:
    """Poll participant count every 30 s; return when alone twice in a row."""
    consecutive_alone = 0
    while True:
        await asyncio.sleep(config.PARTICIPANT_POLL_INTERVAL)
        count = await _count_participants(page)
        logger.debug("Participant count: %s", count)
        if count == 0:
            logger.info("Meeting ended (zero participants)")
            return
        if count == 1:
            consecutive_alone += 1
            logger.info("Alone in meeting (%d/%d)", consecutive_alone, config.ALONE_THRESHOLD)
            if consecutive_alone >= config.ALONE_THRESHOLD:
                return
        else:
            consecutive_alone = 0


# ── Error handling ────────────────────────────────────────────────────────


async def _handle_error(
    meeting_id: str, user_id: int, bot: Bot, error: str
) -> None:
    logger.error("Meeting %s error: %s", meeting_id, error)
    try:
        await models.save_error(meeting_id, error)
    except Exception:
        pass
    try:
        await bot.send_message(user_id, f"❌ Ошибка записи:\n<code>{error[:500]}</code>")
    except Exception:
        pass


# ── Main pipeline ─────────────────────────────────────────────────────────


async def _recording_pipeline(
    meeting_id: str, user_id: int, meeting_url: str, bot: Bot
) -> None:
    audio_path = os.path.join(config.AUDIO_DIR, f"{meeting_id}.wav")
    sink_name = f"sink_{meeting_id.replace('-', '')[:16]}"
    module_index: int | None = None
    audio_proc: asyncio.subprocess.Process | None = None
    browser = None
    context = None

    try:
        module_index = await _create_pulse_sink(sink_name)

        await models.update_meeting_status(meeting_id, "recording")
        await bot.send_message(user_id, "✅ Вхожу на встречу и начинаю запись…")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                ],
                env={
                    **os.environ,
                    "DISPLAY": config.DISPLAY,
                    "PULSE_SINK": sink_name,
                },
            )
            context = await browser.new_context(
                permissions=["microphone", "camera"],
                ignore_https_errors=True,
            )
            page = await context.new_page()

            await _join_meeting(page, meeting_url)

            audio_proc = await _start_audio_capture(audio_path, sink_name)

            await asyncio.wait_for(
                _wait_for_meeting_end(page),
                timeout=config.MAX_RECORDING_DURATION,
            )

        # Stop audio capture
        if audio_proc and audio_proc.returncode is None:
            audio_proc.terminate()
            await audio_proc.wait()

        # ── Transcription ──────────────────────────────────────────────
        await models.update_meeting_status(meeting_id, "transcribing")
        await bot.send_message(user_id, "🎙 Транскрибирую запись…")

        transcript = await transcribe_audio(audio_path)

        if os.path.exists(audio_path):
            os.remove(audio_path)
            logger.info("Audio file deleted: %s", audio_path)

        await models.save_transcript(meeting_id, transcript)

        # ── Analysis ───────────────────────────────────────────────────
        await models.update_meeting_status(meeting_id, "analyzing")
        await bot.send_message(user_id, "🤖 Анализирую встречу…")

        summary, tags, topic, participants = await analyze_meeting(
            meeting_id, user_id, transcript
        )
        await models.save_analysis(meeting_id, summary, tags, topic, participants)
        await models.update_meeting_status(meeting_id, "done")

        # ── Send result ────────────────────────────────────────────────
        tags_str = ", ".join(f"#{t}" for t in tags) if tags else "—"
        participants_str = ", ".join(participants) if participants else "—"

        header = (
            f"✅ <b>Встреча записана</b>\n\n"
            f"📋 <b>Тема:</b> {topic}\n"
            f"🏷 <b>Теги:</b> {tags_str}\n"
            f"👥 <b>Участники:</b> {participants_str}\n\n"
            f"📄 <b>Протокол:</b>\n"
        )

        # Telegram limit ~4096; split if needed
        full_msg = header + summary
        if len(full_msg) <= 4000:
            await bot.send_message(user_id, full_msg)
        else:
            await bot.send_message(user_id, header)
            for chunk_start in range(0, len(summary), 4000):
                await bot.send_message(user_id, summary[chunk_start:chunk_start + 4000])

    except asyncio.TimeoutError:
        await _handle_error(
            meeting_id, user_id, bot, "Превышен лимит записи (3 часа)"
        )
    except PlaywrightTimeout:
        await _handle_error(
            meeting_id, user_id, bot, "Таймаут браузера при входе на встречу"
        )
    except Exception as exc:
        logger.exception("Pipeline error for meeting %s", meeting_id)
        await _handle_error(meeting_id, user_id, bot, str(exc))
    finally:
        if audio_proc and audio_proc.returncode is None:
            audio_proc.terminate()
        if os.path.exists(audio_path):
            os.remove(audio_path)
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if module_index is not None:
            await _remove_pulse_sink(module_index)


def start_recording(
    meeting_id: str, user_id: int, meeting_url: str, bot: Bot
) -> asyncio.Task:
    task = asyncio.create_task(
        _recording_pipeline(meeting_id, user_id, meeting_url, bot),
        name=f"recording-{meeting_id}",
    )
    active_recordings[meeting_id] = task
    task.add_done_callback(lambda _: active_recordings.pop(meeting_id, None))
    return task
