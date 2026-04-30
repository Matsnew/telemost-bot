# Telemost Bot — контекст для Claude

Telegram-бот для автоматической записи встреч Яндекс Телемост с AI-анализом и базой знаний.

## Стек

- **Python 3.11**, asyncio — единый процесс (FastAPI + aiogram в одном event loop через `asyncio.gather`)
- **aiogram 3.x** — Telegram-бот с FSM, ReplyKeyboard и InlineKeyboard
- **Playwright** — headless Chromium (headless=False + Xvfb), входит на встречу как гость
- **PulseAudio** — виртуальный null-sink на каждую запись, захват через `parec | ffmpeg`
- **faster-whisper** (medium, cpu, int8) — транскрипция в ThreadPoolExecutor, lazy load + `del model` после
- **OpenAI gpt-4o** — тегирование, определение типа встречи, протокол
- **asyncpg + PostgreSQL** (Railway) — хранение встреч
- **Fernet** — шифрование `meeting_url` в БД
- **Docker + Railway** — деплой, healthcheck `/health`, `entrypoint.sh` запускает Xvfb и PulseAudio

## Структура

```
main.py                  — точка входа, asyncio.gather(bot, api)
config.py                — все настройки из env (OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, DATABASE_URL, ENCRYPTION_KEY, ALLOWED_USER_IDS, WHISPER_MODEL)
entrypoint.sh            — запуск Xvfb :99 и PulseAudio (auth-anonymous, unix-socket)
Dockerfile               — system deps + pip + pre-download whisper medium
railway.toml             — healthcheck /health, timeout 180s

bot/
  handlers.py            — все хендлеры aiogram: кнопки, FSM /ask, inline-callbacks
  middlewares.py         — AllowedUsersMiddleware (ALLOWED_USER_IDS)
  rate_limiter.py        — rate limit для /ask

services/
  recorder.py            — полный pipeline: PulseAudio sink → Playwright → join → запись → транскрипция → анализ → отправка
  transcriber.py         — faster-whisper в executor
  analyzer.py            — OpenAI: тегирование с учётом существующих тегов, тип встречи, протокол по типу

database/
  schema.sql             — CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD COLUMN IF NOT EXISTS (миграции)
  models.py              — все asyncpg-запросы
  connection.py          — пул соединений, init_db (применяет schema.sql)

utils/
  time.py                — MSK timezone, fmt_msk(), now_msk()
  encryption.py          — Fernet encrypt/decrypt
```

## Ключевые решения и почему

- **headless=False** — headless=True отключает аудио в Chromium, поэтому нужен Xvfb
- **PULSE_SERVER=unix:/tmp/pulse.sock** с `auth-anonymous` — чтобы работало от root без dbus
- **`pactl set-default-sink`** перед запуском Chromium — гарантирует что аудио встречи идёт в нужный sink
- **Whisper pre-download в Dockerfile** — модель medium (~1.5 ГБ) не успевает скачаться при старте контейнера
- **`meeting_belongs_to_user()`** без расшифровки URL — используется там где URL не нужен, чтобы не падать на decrypt
- **`get_meeting_raw()`** — для показа транскрипта/протокола без расшифровки URL
- **active_recordings: dict[str, asyncio.Task]** — in-memory реестр, теряется при рестарте
- **allowed_updates=["message", "callback_query"]** — обязательно, иначе inline-кнопки молча игнорируются

## Pipeline записи (recorder.py)

1. Создать PulseAudio null-sink → set-default-sink
2. Запустить Playwright Chromium (headless=False, DISPLAY=:99)
3. Зайти на встречу: заполнить имя "Protocaller", выключить mic/cam ДО нажатия кнопки входа
4. Отправить скриншот подтверждения
5. Запустить `parec | ffmpeg` → `/tmp/{meeting_id}.wav`
6. Ждать конца встречи: URL change / DOM-элемент / недоступность страницы
7. Транскрибировать (faster-whisper)
8. Анализировать (OpenAI): теги + тип встречи + протокол с учётом предыдущих встреч
9. Отправить протокол + кнопки "📝 Транскрипт" и "🎵 Аудио"
10. Аудиофайл НЕ удалять (для отладки и скачивания пользователем)

## Типы встреч и промпты (analyzer.py)

Встречи классифицируются: `sales / internal / planning / review / interview / partner / other`.
Структура протокола зависит от типа. Промпты: `_TAGGING_PROMPT` и `_PROTOCOL_PROMPT`.
Тегирование учитывает существующие теги пользователя из БД (`get_existing_tags()`).

## Railway Variables (обязательные)

```
TELEGRAM_BOT_TOKEN
OPENAI_API_KEY
DATABASE_URL         — полная строка postgresql://...
ENCRYPTION_KEY       — Fernet key (base64)
ALLOWED_USER_IDS     — через запятую без пробелов: 123456,789012
WHISPER_MODEL        — можно переопределить (tiny/small/medium), по умолчанию medium
```

## Известные нюансы

- При OOM (Out of Memory) — уменьшить WHISPER_MODEL до small через Railway Variables
- Если бот не отвечает — проверить ALLOWED_USER_IDS (запятая без пробелов)
- Аудио отправляется как document (не audio) — Telegram иначе отклоняет WAV
- Debug-скриншот страницы встречи отправляется автоматически при входе
