import json
import logging
from openai import AsyncOpenAI
from config import config
from database import models

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

# ── Prompts ────────────────────────────────────────────────────────────────

_TAGGING_SYSTEM = (
    "Ты — система тегирования рабочих встреч. "
    "Отвечай строго в формате JSON без пояснений."
)

_TAGGING_PROMPT = """\
Ты — система тегирования рабочих встреч. Твоя задача — проставить теги,
которые обеспечат связность базы знаний через все встречи.

СУЩЕСТВУЮЩИЕ ТЕГИ В БАЗЕ (использовать в приоритете если подходят):
{existing_tags}

ТРАНСКРИПТ ВСТРЕЧИ:
{transcript}

ПРАВИЛА:
1. Сначала проверь существующие теги — если встреча касается той же темы,
   компании, проекта или человека что уже есть в базе, используй ТОЧНО
   тот же тег (регистр, написание — всё идентично)
2. Новый тег создавай только если тема действительно новая и её нет в базе
3. Теги — короткие существительные или словосочетания (1-3 слова)
4. Названия компаний, продуктов, проектов — всегда тегировать
5. Имена ключевых участников — тегировать если упоминаются регулярно
6. Количество тегов: 3-7 на встречу

ФОРМАТ ОТВЕТА — только JSON, никакого текста вокруг:
{{
  "tags": ["тег1", "тег2", "тег3"],
  "topic": "одно предложение о чём встреча",
  "participants": ["имя1", "имя2"],
  "meeting_type": "тип встречи"
}}

Типы встреч (выбрать один):
- sales — переговоры с клиентом, презентация, коммерческое предложение
- internal — внутренняя рабочая встреча команды
- planning — планирование, стратегия, roadmap
- review — ретроспектива, разбор результатов
- interview — собеседование, HR
- partner — встреча с партнёром, интеграция, совместный проект
- other — не подходит ни одно из выше
"""

_PROTOCOL_SYSTEM = "Ты — ассистент для протоколирования рабочих встреч."

_PROTOCOL_PROMPT = """\
Ты — ассистент для протоколирования рабочих встреч.

ТИП ВСТРЕЧИ: {meeting_type}
ТЕГИ: {tags}
УЧАСТНИКИ: {participants}

КОНТЕКСТ ПРЕДЫДУЩИХ ВСТРЕЧ ПО ЭТОЙ ТЕМЕ:
{previous_summaries}
(если пусто — предыдущих встреч по теме не было)

ТРАНСКРИПТ:
{transcript}

Составь протокол встречи. Структура зависит от типа:

ЕСЛИ sales:
## Краткое резюме
## Клиент и его задача
## Что предложили / показали
## Возражения и вопросы клиента
## Договорённости и следующий шаг
## Ретроспектива (что изменилось по сравнению с предыдущими встречами по теме)

ЕСЛИ internal:
## Краткое резюме
## Обсуждаемые вопросы
## Принятые решения
## Action items (формат: [Ответственный] — [задача] — [срок если упоминался])
## Открытые вопросы

ЕСЛИ planning:
## Краткое резюме
## Цели и контекст
## Рассмотренные варианты
## Принятые решения и обоснование
## План действий
## Риски и зависимости
## Ретроспектива (как это соотносится с предыдущими планами по теме)

ЕСЛИ review:
## Краткое резюме
## Что оценивали
## Результаты и выводы
## Что сработало / что нет
## Action items
## Ретроспектива (динамика по сравнению с предыдущими встречами)

ЕСЛИ partner:
## Краткое резюме
## Партнёр и контекст сотрудничества
## Обсуждаемые возможности
## Договорённости
## Следующие шаги с обеих сторон
## Ретроспектива

ЕСЛИ interview:
## Краткое резюме
## Кандидат / собеседуемый
## Ключевые моменты
## Оценка
## Решение или следующий шаг

ЕСЛИ other:
## Краткое резюме
## Ключевые темы
## Решения и договорённости
## Action items

ВАЖНО:
- Раздел "Ретроспектива" заполнять только если есть реальная связь
  с предыдущими встречами. Если связи нет — раздел не включать.
- Action items писать конкретно: кто, что, когда.
- Если имена участников неизвестны — писать "не установлен".
- Отвечать только текстом протокола, без вводных фраз.
"""

_ASK_PROMPT = """\
У тебя есть база протоколов встреч пользователя.
Ответь на вопрос опираясь только на эти данные.
Если информации нет — так и скажи.

База встреч:
{base_text}

Вопрос: {question}
"""

# ── Internal helpers ───────────────────────────────────────────────────────


async def _extract_metadata(
    transcript: str, existing_tags: list[str]
) -> tuple[list[str], str, list[str], str]:
    tags_str = ", ".join(existing_tags) if existing_tags else "тегов пока нет"
    response = await _client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=512,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _TAGGING_SYSTEM},
            {
                "role": "user",
                "content": _TAGGING_PROMPT.format(
                    existing_tags=tags_str,
                    transcript=transcript[:8000],
                ),
            },
        ],
    )
    data = json.loads(response.choices[0].message.content)
    return (
        data.get("tags", []),
        data.get("topic", "Без темы"),
        data.get("participants", []),
        data.get("meeting_type", "other"),
    )


async def _build_protocol(
    transcript: str,
    meeting_type: str,
    tags: list[str],
    participants: list[str],
    previous_meetings: list[dict],
) -> str:
    if previous_meetings:
        prev_text = "\n\n---\n\n".join(
            f"Встреча от {m['created_at'].strftime('%d.%m.%Y')}"
            f" (тема: {m.get('topic', '—')}):\n{m['summary']}"
            for m in previous_meetings
        )
    else:
        prev_text = ""

    response = await _client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _PROTOCOL_SYSTEM},
            {
                "role": "user",
                "content": _PROTOCOL_PROMPT.format(
                    meeting_type=meeting_type,
                    tags=", ".join(tags) if tags else "—",
                    participants=", ".join(participants) if participants else "не установлены",
                    previous_summaries=prev_text or "Предыдущих встреч по теме не было.",
                    transcript=transcript[:12000],
                ),
            },
        ],
    )
    return response.choices[0].message.content.strip()


# ── Public API ─────────────────────────────────────────────────────────────


async def analyze_meeting(
    meeting_id: str, user_id: int, transcript: str
) -> tuple[str, list[str], str, list[str], str]:
    """Returns (summary, tags, topic, participants, meeting_type)."""
    existing_tags = await models.get_existing_tags(user_id)
    tags, topic, participants, meeting_type = await _extract_metadata(transcript, existing_tags)
    logger.info("Metadata: topic=%r type=%r tags=%r", topic, meeting_type, tags)

    previous = await models.get_recent_meetings_by_tags(
        user_id, tags, exclude_id=meeting_id, limit=config.CONTEXT_MEETINGS_LIMIT
    )

    summary = await _build_protocol(transcript, meeting_type, tags, participants, previous)
    return summary, tags, topic, participants, meeting_type


async def answer_question(user_id: int, question: str) -> str:
    summaries = await models.get_all_summaries(user_id, limit=config.ASK_SUMMARIES_LIMIT)

    if not summaries:
        return "У вас ещё нет завершённых встреч в базе."

    base_text = "\n\n---\n\n".join(
        f"Встреча от {m['created_at'].strftime('%d.%m.%Y')}"
        f" (тема: {m.get('topic', '—')}):\n{m['summary']}"
        for m in summaries
    )

    response = await _client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": _ASK_PROMPT.format(base_text=base_text, question=question)}],
    )
    return response.choices[0].message.content.strip()
