from __future__ import annotations

from typing import Any

import httpx

from .models import FeedbackEvent, RoutingDecision, StructuredParseResult
from .settings import PipelineSettings
from .storage import EmailStore


BUTTONS = [
    ("Useful", "useful"),
    ("Not useful", "not_useful"),
    ("Wrong kid", "wrong_kid"),
    ("Too noisy", "too_noisy"),
    ("Add calendar", "add_calendar"),
    ("Show original", "show_original"),
]


def format_telegram_message(
    subject: str,
    parsed: StructuredParseResult,
    routing: RoutingDecision,
) -> str:
    action = _action_text(parsed)
    when = _when_text(parsed)
    return "\n".join(
        [
            f"School: {_short(parsed.calendar_items[0].title if parsed.calendar_items else subject, 80)}",
            f"Relevant to: {routing.relevance}",
            f"Action: {action}",
            f"When: {when}",
            f"Summary: {_short(parsed.telegram_summary, 260)}",
            f"Why it matters: {_short(parsed.why_it_matters, 180)}",
        ]
    )


async def send_telegram_alert(
    settings: PipelineSettings,
    text: str,
    *,
    email_id: int,
) -> int | None:
    if settings.dry_run:
        return None
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "reply_markup": _reply_markup(email_id),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        message = data.get("result", {})
        if isinstance(message, dict) and isinstance(message.get("message_id"), int):
            return int(message["message_id"])
    return None


def log_callback_feedback(update: dict[str, Any], store: EmailStore) -> bool:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return False
    data = str(callback.get("data") or "")
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "fb":
        return False
    try:
        email_id = int(parts[1])
    except ValueError:
        return False
    message = (
        callback.get("message") if isinstance(callback.get("message"), dict) else {}
    )
    telegram_message_id = message.get("message_id")
    store.log_feedback(
        FeedbackEvent(
            email_id=email_id,
            telegram_message_id=telegram_message_id
            if isinstance(telegram_message_id, int)
            else None,
            feedback_type=parts[2],
            payload=callback,
        )
    )
    return True


def _reply_markup(email_id: int) -> dict[str, Any]:
    rows = []
    for start in range(0, len(BUTTONS), 2):
        rows.append(
            [
                {"text": text, "callback_data": f"fb:{email_id}:{value}"}
                for text, value in BUTTONS[start : start + 2]
            ]
        )
    return {"inline_keyboard": rows}


def _action_text(parsed: StructuredParseResult) -> str:
    if not parsed.parent_action_required or not parsed.action_items:
        return "none"
    return "; ".join(_short(item.action, 100) for item in parsed.action_items[:3])


def _when_text(parsed: StructuredParseResult) -> str:
    for item in parsed.action_items:
        if item.deadline:
            return item.deadline
    for item in parsed.calendar_items:
        when = " ".join(part for part in (item.start, item.time_text) if part).strip()
        if when:
            return when
        if item.date_text:
            return item.date_text
    return "none"


def _short(value: str, limit: int) -> str:
    value = " ".join((value or "").split()).strip()
    if not value:
        return "none"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
