from __future__ import annotations

import json
import logging
import os
import re
import shutil
import smtplib
import ssl
import subprocess
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .ics import CalendarAttachment, build_calendar_attachment
from .models import PipelineEmail, StructuredParseResult
from .settings import PipelineSettings
from .storage import EmailStore


logger = logging.getLogger(__name__)

ACK_LABELS: dict[str, str] = {
    "useful": "Marked useful",
    "not_useful": "Marked not useful",
    "wrong_kid": "Recorded: wrong kid",
    "too_noisy": "Recorded: too noisy",
    "show_original": "Sending original",
    "add_calendar": "Creating calendar invite",
}


def _bot_url(settings: PipelineSettings, method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


async def _send_message(
    settings: PipelineSettings,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> int | None:
    if settings.dry_run or not settings.telegram_bot_token or not settings.telegram_chat_id:
        return None
    payload: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(_bot_url(settings, "sendMessage"), json=payload)
        response.raise_for_status()
        data = response.json()
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
    return None



def _google_calendar_url(email: PipelineEmail, parsed: StructuredParseResult) -> str | None:
    item = max(
        (item for item in parsed.calendar_items if item.start and item.confidence >= 0.7),
        key=lambda item: item.confidence,
        default=None,
    )
    if item is None:
        return None

    start = (item.start or "").strip()
    end = (item.end or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        start_date = datetime.fromisoformat(start).date()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", end):
            end_date = datetime.fromisoformat(end).date()
        else:
            end_date = start_date + timedelta(days=1)
        dates = f"{start_date:%Y%m%d}/{end_date:%Y%m%d}"
    elif re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", start):
        start_dt = datetime.fromisoformat(start)
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", end):
            end_dt = datetime.fromisoformat(end)
        else:
            end_dt = start_dt + timedelta(hours=1)
        dates = f"{start_dt:%Y%m%dT%H%M%S}/{end_dt:%Y%m%dT%H%M%S}"
    else:
        return None

    details = "\n".join(
        part
        for part in [
            parsed.telegram_summary,
            parsed.why_it_matters,
            f"From: {email.sender}" if email.sender else "",
            f"Subject: {email.subject}" if email.subject else "",
        ]
        if part
    )
    params = {
        "action": "TEMPLATE",
        "text": item.title or email.subject or "School event",
        "dates": dates,
        "details": details,
        "ctz": "America/Los_Angeles",
    }
    if item.location:
        params["location"] = item.location
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _google_calendar_reply_markup(url: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "Open Google Calendar", "url": url}]]}

def _calendar_email_recipient() -> str:
    return (
        os.environ.get("EMAIL_FORWARD_CALENDAR_EMAIL")
        or os.environ.get("CALENDAR_INVITE_EMAIL")
        or "rahul.chhabria@gmail.com"
    ).strip()


def _calendar_email_sender() -> str:
    return (
        os.environ.get("EMAIL_FORWARD_SMTP_FROM")
        or os.environ.get("EMAIL_FORWARD_EMAIL_FROM")
        or "Ash School Email <ash@inbox.chhab.com>"
    ).strip()


def _calendar_smtp_port() -> int:
    raw = (os.environ.get("EMAIL_FORWARD_SMTP_PORT") or "587").strip()
    try:
        return int(raw)
    except ValueError:
        return 587


def _build_calendar_email(attachment: CalendarAttachment, *, subject: str) -> EmailMessage | None:
    recipient = _calendar_email_recipient()
    if not recipient:
        return None

    msg = EmailMessage()
    msg["From"] = _calendar_email_sender()
    msg["To"] = recipient
    msg["Subject"] = f"Calendar invite: {subject}"
    msg.set_content(
        "Calendar invite attached. Open this email on iOS/macOS and use the calendar option to add it.\n"
    )
    msg.add_attachment(
        attachment.content.encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename=attachment.filename,
        params={"method": "REQUEST", "charset": "utf-8"},
    )
    return msg


def _send_calendar_email_via_smtp(msg: EmailMessage) -> bool:
    host = (os.environ.get("EMAIL_FORWARD_SMTP_HOST") or "").strip()
    if not host:
        return False

    port = _calendar_smtp_port()
    username = (os.environ.get("EMAIL_FORWARD_SMTP_USER") or "").strip()
    password = os.environ.get("EMAIL_FORWARD_SMTP_PASSWORD") or ""
    use_ssl = (os.environ.get("EMAIL_FORWARD_SMTP_SSL") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    use_starttls = (
        os.environ.get("EMAIL_FORWARD_SMTP_STARTTLS") or "true"
    ).strip().lower() not in {"0", "false", "no"}

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
                if username or password:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                if use_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if username or password:
                    smtp.login(username, password)
                smtp.send_message(msg)
    except OSError as exc:
        logger.warning("calendar_email_smtp_failed", extra={"error": str(exc)[:500]})
        return False
    except smtplib.SMTPException as exc:
        logger.warning("calendar_email_smtp_failed", extra={"error": str(exc)[:500]})
        return False
    return True


def _send_calendar_email_via_sendmail(msg: EmailMessage) -> bool:
    sendmail = shutil.which("sendmail") or "/usr/sbin/sendmail"
    if not Path(sendmail).exists():
        return False

    completed = subprocess.run(
        [sendmail, "-t"],
        input=msg.as_bytes(),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        logger.warning(
            "calendar_email_sendmail_failed",
            extra={
                "returncode": completed.returncode,
                "stderr": completed.stderr.decode(errors="replace")[:500],
            },
        )
        return False
    return True


def _send_calendar_email(attachment: CalendarAttachment, *, subject: str) -> bool:
    msg = _build_calendar_email(attachment, subject=subject)
    if msg is None:
        return False
    return _send_calendar_email_via_smtp(msg) or _send_calendar_email_via_sendmail(msg)


async def _send_document(
    settings: PipelineSettings,
    attachment: CalendarAttachment,
    *,
    reply_to_message_id: int | None = None,
) -> int | None:
    if settings.dry_run or not settings.telegram_bot_token or not settings.telegram_chat_id:
        return None
    data: dict[str, Any] = {
        "chat_id": settings.telegram_chat_id,
        "caption": attachment.caption,
    }
    if reply_to_message_id:
        data["reply_to_message_id"] = str(reply_to_message_id)
        data["allow_sending_without_reply"] = "true"
    files = {
        "document": (
            attachment.filename,
            attachment.content.encode("utf-8"),
            "text/calendar; charset=utf-8",
        )
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            _bot_url(settings, "sendDocument"), data=data, files=files
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
    return None


def _load_email_row(store: EmailStore, email_id: int) -> dict[str, Any] | None:
    with store._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT raw_email_json, structured_parse_json, subject, sender FROM emails WHERE id = ?",
            (email_id,),
        ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _short(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "\u2026"


async def _action_show_original(
    settings: PipelineSettings,
    store: EmailStore,
    email_id: int,
    reply_to: int | None,
) -> str:
    row = _load_email_row(store, email_id)
    if row is None:
        await _send_message(settings, f"Email #{email_id} not found.", reply_to_message_id=reply_to)
        return "missing"
    try:
        raw = json.loads(row.get("raw_email_json") or "{}")
    except json.JSONDecodeError:
        raw = {}
    body = (raw.get("text_body") or "").strip()
    if not body:
        body = (raw.get("html_body") or "").strip()
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
    if not body:
        body = "(empty body)"
    header = f"From: {raw.get('sender') or row.get('sender') or '(unknown)'}\nSubject: {row.get('subject') or '(no subject)'}\n---\n"
    chunk_limit = 3500
    payload = header + body
    sent_any = False
    while payload:
        chunk = payload[:chunk_limit]
        payload = payload[chunk_limit:]
        await _send_message(settings, chunk, reply_to_message_id=reply_to if not sent_any else None)
        sent_any = True
    return "sent"



async def _action_add_calendar(
    settings: PipelineSettings,
    store: EmailStore,
    email_id: int,
    reply_to: int | None,
) -> str:
    row = _load_email_row(store, email_id)
    if row is None:
        await _send_message(settings, f"Email #{email_id} not found.", reply_to_message_id=reply_to)
        return "missing"
    try:
        raw = json.loads(row.get("raw_email_json") or "{}")
        parsed = StructuredParseResult.model_validate_json(
            row.get("structured_parse_json") or "{}"
        )
        email = PipelineEmail.model_validate(
            {
                **raw,
                "subject": row.get("subject") or raw.get("subject") or "",
                "sender": row.get("sender") or raw.get("sender") or "",
            }
        )
    except (json.JSONDecodeError, ValueError) as exc:
        await _send_message(
            settings,
            f"Could not create a calendar invite from email #{email_id}: {_short(str(exc), 200)}",
            reply_to_message_id=reply_to,
        )
        return "invalid"

    calendar_url = _google_calendar_url(email, parsed)
    if calendar_url is None:
        await _send_message(
            settings,
            "No calendar link could be created from this email. I need a calendar item with a normalized date/time first.",
            reply_to_message_id=reply_to,
        )
        return "no_item"

    await _send_message(
        settings,
        "Open Google Calendar to review and save this event.",
        reply_to_message_id=reply_to,
        reply_markup=_google_calendar_reply_markup(calendar_url),
    )
    return "google_link_sent"


async def dispatch_callback_action(
    feedback_type: str,
    email_id: int | None,
    telegram_message_id: int | None,
    settings: PipelineSettings,
    store: EmailStore,
) -> dict[str, Any]:
    """Run side effects after feedback is logged. Returns a status dict."""
    result: dict[str, Any] = {"feedback_type": feedback_type, "action": "noop"}
    if email_id is None:
        return result
    try:
        if feedback_type == "show_original":
            result["action"] = await _action_show_original(
                settings, store, email_id, telegram_message_id
            )
        elif feedback_type == "add_calendar":
            result["action"] = await _action_add_calendar(
                settings, store, email_id, telegram_message_id
            )
        elif feedback_type in {"useful", "not_useful", "wrong_kid", "too_noisy"}:
            label = ACK_LABELS.get(feedback_type, feedback_type)
            await _send_message(
                settings,
                f"{label} for email #{email_id}.",
                reply_to_message_id=telegram_message_id,
            )
            result["action"] = "ack"
        else:
            logger.info(
                "callback_action_unknown",
                extra={"feedback_type": feedback_type, "email_id": email_id},
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("callback_action_failed")
        result["action"] = "error"
        result["error"] = str(exc)
        try:
            await _send_message(
                settings,
                f"Action {feedback_type} failed: {_short(str(exc), 200)}",
                reply_to_message_id=telegram_message_id,
            )
        except Exception:
            logger.exception("callback_error_notify_failed")
    return result
