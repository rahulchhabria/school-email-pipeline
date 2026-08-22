from __future__ import annotations

from typing import Any

import httpx

from .models import (
    FeedbackEvent,
    RoutingDecision,
    StructuredParseResult,
)
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

FEEDBACK_VERDICTS: dict[str, str] = {
    "useful": "positive",
    "not_useful": "negative",
    "wrong_kid": "negative",
    "too_noisy": "negative",
}


def format_telegram_message(
    subject: str,
    parsed: StructuredParseResult,
    routing: RoutingDecision,
    entities: object | None = None,
) -> str:
    del entities  # legacy positional argument; entities are no longer extracted
    lines = [_headline(subject, parsed), "", f"For: {routing.relevance}"]

    if action := _action_text(parsed):
        lines.append(f"Action: {action}")
    if due := _due_text(parsed):
        lines.append(f"Due: {due}")
    if event := _event_text(parsed):
        lines.append(f"Event: {event}")
    if place := _place_text(parsed):
        lines.append(f"Place: {place}")
    if _calendar_ready(parsed):
        lines.append("Calendar: tap Add calendar")

    lines.extend(["", f"Why: {_short(parsed.why_it_matters, 180)}"])
    if summary := _short(parsed.telegram_summary, 260):
        lines.append(summary)
    if "needs_human_review" in routing.actions or parsed.needs_human_review:
        lines.append("Check: audience/date may need review")

    return "\n".join(line for line in lines if line != "")


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
    feedback_type = parts[2]
    verdict = FEEDBACK_VERDICTS.get(feedback_type)
    parsed_snapshot = store.get_email_parsed_result(email_id) if verdict else None
    store.log_feedback(
        FeedbackEvent(
            email_id=email_id,
            telegram_message_id=telegram_message_id
            if isinstance(telegram_message_id, int)
            else None,
            feedback_type=feedback_type,
            payload=callback,
        ),
        verdict=verdict,
        parsed_snapshot=parsed_snapshot,
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


def _headline(subject: str, parsed: StructuredParseResult) -> str:
    if parsed.calendar_items:
        title = parsed.calendar_items[0].title
    else:
        title = subject or parsed.telegram_summary or "School update"
    return _short(title, 80) or "School update"


def _action_text(parsed: StructuredParseResult) -> str:
    if not parsed.parent_action_required or not parsed.action_items:
        return ""
    return "; ".join(
        action for item in parsed.action_items[:3] if (action := _short(item.action, 100))
    )


def _due_text(parsed: StructuredParseResult) -> str:
    for item in parsed.action_items:
        if item.deadline:
            return _short(item.deadline, 80)
    return ""


def _event_text(parsed: StructuredParseResult) -> str:
    for item in parsed.calendar_items:
        date_text = _short(item.date_text or "", 80)
        time_text = _short(item.time_text or "", 80)
        if not date_text and item.start:
            date_text, start_time = _split_start(item.start)
            time_text = time_text or start_time
        if date_text and time_text:
            return f"{date_text}, {time_text}"
        if date_text:
            return date_text
        if time_text:
            return time_text
    return ""


def _place_text(parsed: StructuredParseResult) -> str:
    for item in parsed.calendar_items:
        if item.location:
            return _short(item.location, 120)
    return ""


def _calendar_ready(parsed: StructuredParseResult) -> bool:
    return any(
        bool(item.start and item.confidence >= 0.7 and item.start[:10].count("-") == 2)
        for item in parsed.calendar_items
    )


def _split_start(value: str) -> tuple[str, str]:
    start = _short(value, 80)
    if "T" not in start:
        return start, ""
    date_part, time_part = start.split("T", 1)
    return date_part, time_part


def _short(value: str, limit: int) -> str:
    value = " ".join((value or "").split()).strip()
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
