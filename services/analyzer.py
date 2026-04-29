import json
import logging
from openai import AsyncOpenAI
from config import config
from database import models

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

_METADATA_SYSTEM = (
    "Ты — ассистент для анализа транскриптов встреч. "
    "Отвечай строго в формате JSON без пояснений."
)

_METADATA_PROMPT = """\
Извлеки метаданные из транскрипта встречи. Верни только JSON:
{{
  "tags": ["тег1", "тег2"],
  "topic": "краткая тема встречи (одно предложение)",
  "participants": ["имя1", "имя2"]
}}

Правила:
- tags: 2-5 ключевых тематических тегов на русском, нижний регистр
- participants: имена, упомянутые в тексте
- Если участники не упомянуты явно — пустой массив

Транскрипт:
{transcript}
"""

_PROTOCOL_SYSTEM = "Ты — ассистент для протоколирования встреч."

_PROTOCOL_PROMPT = """\
Контекст предыдущих встреч по этой теме:
{previous_summaries}

Текущая встреча:
{transcript}

Создай структурированный протокол:
1. Краткое резюме (3-5 предложений)
2. Ключевые решения
3. Action items с ответственными
4. Открытые вопросы
5. Связь с предыдущими встречами (если есть)
"""


async def _extract_metadata(transcript: str) -> tuple[list[str], str, list[str]]:
    response = await _client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=512,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _METADATA_SYSTEM},
            {"role": "user", "content": _METADATA_PROMPT.format(transcript=transcript[:8000])},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    return (
        data.get("tags", []),
        data.get("topic", "Без темы"),
        data.get("participants", []),
    )


async def _build_protocol(transcript: str, previous_meetings: list[dict]) -> str:
    if previous_meetings:
        prev_text = "\n\n---\n\n".join(
            f"Встреча от {m['created_at'].strftime('%d.%m.%Y')}"
            f" (тема: {m.get('topic', '—')}):\n{m['summary']}"
            for m in previous_meetings
        )
    else:
        prev_text = "Нет предыдущих встреч по этой теме."

    response = await _client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": _PROTOCOL_SYSTEM},
            {
                "role": "user",
                "content": _PROTOCOL_PROMPT.format(
                    previous_summaries=prev_text,
                    transcript=transcript[:12000],
                ),
            },
        ],
    )
    return response.choices[0].message.content.strip()


async def analyze_meeting(
    meeting_id: str, user_id: int, transcript: str
) -> tuple[str, list[str], str, list[str]]:
    """Returns (summary, tags, topic, participants)."""
    tags, topic, participants = await _extract_metadata(transcript)
    logger.info("Metadata extracted: topic=%r tags=%r", topic, tags)

    previous = await models.get_recent_meetings_by_tags(
        user_id, tags, exclude_id=meeting_id, limit=config.CONTEXT_MEETINGS_LIMIT
    )

    summary = await _build_protocol(transcript, previous)
    return summary, tags, topic, participants


async def answer_question(user_id: int, question: str) -> str:
    summaries = await models.get_all_summaries(user_id, limit=config.ASK_SUMMARIES_LIMIT)

    if not summaries:
        return "У вас ещё нет завершённых встреч в базе."

    base_text = "\n\n---\n\n".join(
        f"Встреча от {m['created_at'].strftime('%d.%m.%Y')}"
        f" (тема: {m.get('topic', '—')}):\n{m['summary']}"
        for m in summaries
    )

    prompt = (
        "У тебя есть база протоколов встреч пользователя.\n"
        "Ответь на вопрос опираясь только на эти данные.\n"
        "Если информации нет — так и скажи.\n\n"
        f"База встреч:\n{base_text}\n\n"
        f"Вопрос: {question}"
    )

    response = await _client.chat.completions.create(
        model=config.OPENAI_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()
