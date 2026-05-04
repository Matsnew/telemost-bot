import asyncio
import logging
import os
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
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
    'button:has-text("Вступить")',
    'button:has-text("Подключиться")',
    'button:has-text("Продолжить")',
    'button:has-text("Join")',
    'button:has-text("Enter")',
    'button[type="submit"]',
    '[data-testid*="join"]',
    '[data-testid*="enter"]',
    '[class*="join"] button',
    '[class*="enter"] button',
    'form button',
]

_PARTICIPANT_SELECTORS = [
    '[class*="participant-item"]',
    '[class*="attendee"]',
    '[data-testid*="participant"]',
    '[class*="video-tile"]',
    '[class*="roster-item"]',
    '[class*="MemberList"] [class*="item"]',
    '[class*="members"] [class*="item"]',
]

_PARTICIPANT_NAME_SELECTORS = [
    '[class*="participant-item"] [class*="name"]',
    '[class*="participant-item"] [class*="title"]',
    '[class*="attendee"] [class*="name"]',
    '[class*="video-tile"] [class*="name"]',
    '[class*="video-tile"] [class*="label"]',
    '[class*="roster-item"] [class*="name"]',
    '[class*="MemberList"] [class*="name"]',
    '[class*="members"] [class*="name"]',
    '[data-testid*="participant-name"]',
    '[class*="participant"] [class*="name"]',
    '[class*="UserName"]',
    '[class*="userName"]',
    '[class*="user-name"]',
]

_MUTE_BUTTON_SELECTORS = [
    'button[aria-label*="микрофон" i]',
    'button[aria-label*="mute" i]',
    'button[title*="микрофон" i]',
    'button[title*="mute" i]',
    '[data-testid*="mute"]',
    '[data-testid*="mic"]',
]

_CAMERA_OFF_SELECTORS = [
    'button[aria-label*="камер" i]',
    'button[aria-label*="camera" i]',
    'button[aria-label*="видео" i]',
    'button[title*="камер" i]',
    '[data-testid*="camera"]',
    '[data-testid*="video"]',
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
    """Load a null-sink, set it as default, and return its module index."""
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
            # Set as default so Chromium routes audio here
            await asyncio.create_subprocess_exec(
                "pactl", "set-default-sink", sink_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
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


async def _join_meeting(page, meeting_url: str, bot=None, user_id: int = 0) -> None:
    logger.info("Navigating to meeting URL")
    await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(3)

    # Send debug screenshot so we can see what Telemost shows
    if bot and user_id:
        try:
            screenshot = await page.screenshot(full_page=True)
            await bot.send_photo(
                user_id,
                photo=BufferedInputFile(screenshot, filename="debug.png"),
                caption=f"🔍 Отладка: страница встречи\nTitle: {await page.title()}\nURL: {page.url}",
            )
        except Exception as e:
            logger.warning("Failed to send debug screenshot: %s", e)

    name_input = await _find_element(page, _NAME_INPUT_SELECTORS, timeout_ms=10_000)
    if name_input is None:
        raise RuntimeError(
            f"Не найдено поле ввода имени. Title: {await page.title()} URL: {page.url}"
        )

    await name_input.fill("Protocaller")
    logger.info("Name entered")
    await asyncio.sleep(1)

    # Отключить микрофон и камеру на экране предпросмотра (ДО входа)
    for selectors, name in [(_MUTE_BUTTON_SELECTORS, "mic"), (_CAMERA_OFF_SELECTORS, "camera")]:
        btn = await _find_element(page, selectors, timeout_ms=2000)
        if btn:
            try:
                await btn.click()
                logger.info("Clicked %s off on pre-join screen", name)
                await asyncio.sleep(0.5)
            except Exception:
                pass

    join_btn = await _find_element(page, _JOIN_BUTTON_SELECTORS, timeout_ms=10_000)
    if join_btn is None:
        if bot and user_id:
            try:
                shot = await page.screenshot(full_page=True)
                await bot.send_photo(user_id, photo=BufferedInputFile(shot, filename="debug.png"), caption="⚠️ Не нашёл кнопку входа")
            except Exception:
                pass
        raise RuntimeError("Не найдена кнопка входа")

    await join_btn.click()
    logger.info("Join button clicked")

    # Wait for meeting room to initialise
    await asyncio.sleep(8)
    logger.info("In meeting room")


async def _get_participant_names(page) -> list[str]:
    """Extract participant display names from Telemost UI."""
    names: set[str] = set()
    for selector in _PARTICIPANT_NAME_SELECTORS:
        try:
            elements = await page.locator(selector).all()
            for el in elements:
                text = (await el.inner_text()).strip()
                if text and text != "Protocaller" and 1 < len(text) < 100:
                    names.add(text)
        except Exception:
            continue
    return list(names)


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


async def _wait_for_meeting_end(page, participants: set[str]) -> None:
    """Poll every 30 s for reliable meeting-end signals, collecting participant names along the way."""
    initial_url = page.url

    while True:
        await asyncio.sleep(config.PARTICIPANT_POLL_INTERVAL)

        # Collect participant names on each poll
        new_names = await _get_participant_names(page)
        if new_names:
            participants.update(new_names)
            logger.info("Participants so far: %s", participants)

        # 1. URL изменился — Телемост перенаправил после завершения
        if page.url != initial_url:
            logger.info("Meeting ended: URL changed → %s", page.url)
            return

        # 2. Появился экран завершения встречи
        for sel in _MEETING_ENDED_SELECTORS:
            try:
                if await page.locator(sel).count() > 0:
                    logger.info("Meeting ended: found end-screen element")
                    return
            except Exception:
                pass

        # 3. Страница недоступна / упала
        try:
            await page.title()
        except Exception:
            logger.info("Meeting ended: page is no longer accessible")
            return


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
            chromium_path = (
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
                or "/usr/bin/chromium"
            )
            browser = await p.chromium.launch(
                executable_path=chromium_path,
                headless=False,
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
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1280,720",
                ],
                env={
                    **os.environ,
                    "DISPLAY": config.DISPLAY,
                    "PULSE_SERVER": f"unix:/tmp/pulse.sock",
                    "PULSE_SINK": sink_name,
                },
            )
            context = await browser.new_context(
                permissions=["microphone", "camera"],
                ignore_https_errors=True,
            )
            page = await context.new_page()

            await _join_meeting(page, meeting_url, bot=bot, user_id=user_id)

            # ── Подтверждение входа со скриншотом ─────────────────────
            from utils.time import now_msk
            joined_at = now_msk().strftime("%d.%m.%Y %H:%M:%S МСК")
            try:
                screenshot = await page.screenshot(full_page=False)
                await bot.send_photo(
                    user_id,
                    photo=BufferedInputFile(screenshot, filename="joined.png"),
                    caption=(
                        f"🟢 <b>Запись началась</b>\n"
                        f"🕐 Время входа: {joined_at}\n"
                        f"🔗 {meeting_url}"
                    ),
                )
            except Exception as e:
                logger.warning("Failed to send join screenshot: %s", e)

            # Collect initial participant names after join settles
            await asyncio.sleep(5)
            scraped_participants: set[str] = set(await _get_participant_names(page))
            logger.info("Initial participants: %s", scraped_participants)

            audio_proc = await _start_audio_capture(audio_path, sink_name)

            await asyncio.wait_for(
                _wait_for_meeting_end(page, scraped_participants),
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
        # Аудиофайл НЕ удаляем — оставляем для отладки
        logger.info("Audio file kept at: %s", audio_path)

        await models.save_transcript(meeting_id, transcript)

        # ── Analysis ───────────────────────────────────────────────────
        await models.update_meeting_status(meeting_id, "analyzing")
        await bot.send_message(user_id, "🤖 Анализирую встречу…")

        summary, tags, topic, participants, meeting_type = await analyze_meeting(
            meeting_id, user_id, transcript, list(scraped_participants)
        )
        await models.save_analysis(meeting_id, summary, tags, topic, participants, meeting_type)
        await models.update_meeting_status(meeting_id, "done")

        # ── Send result ────────────────────────────────────────────────
        tags_str = ", ".join(f"#{t}" for t in tags) if tags else "—"
        participants_str = ", ".join(participants) if participants else "—"
        type_icons = {
            "sales": "🤝", "internal": "🏠", "planning": "📅",
            "review": "🔍", "interview": "👤", "partner": "🤝", "other": "📌",
        }
        type_icon = type_icons.get(meeting_type, "📌")

        header = (
            f"✅ <b>Встреча записана</b>\n\n"
            f"📋 <b>Тема:</b> {topic}\n"
            f"{type_icon} <b>Тип:</b> {meeting_type}\n"
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

        # ── Action buttons ─────────────────────────────────────────────
        has_audio = os.path.exists(audio_path)
        row = [InlineKeyboardButton(text="📝 Транскрипт", callback_data=f"transcript:{meeting_id}")]
        if has_audio:
            row.append(InlineKeyboardButton(text="🎵 Аудио", callback_data=f"audio:{meeting_id}"))
        await bot.send_message(
            user_id,
            "⬆️ Нажми чтобы получить транскрипт или аудио:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[row]),
        )

    except asyncio.CancelledError:
        logger.info("Recording %s cancelled by user", meeting_id)
        try:
            await models.update_meeting_status(meeting_id, "cancelled")
            await bot.send_message(user_id, "⏹ Запись остановлена вручную.")
        except Exception:
            pass
        raise
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
        # Аудио не удаляем (оставляем для отладки)
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


def stop_recording(meeting_id: str) -> bool:
    """Cancel an active recording task. Returns True if task was found and cancelled."""
    task = active_recordings.get(meeting_id)
    if task and not task.done():
        task.cancel()
        return True
    return False
