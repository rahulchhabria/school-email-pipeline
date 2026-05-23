from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any

import httpx

from .settings import PipelineSettings
from .storage import EmailStore


logger = logging.getLogger(__name__)

ACK_LABELS: dict[str, str] = {
    "useful": "Marked useful",
    "not_useful": "Marked not useful",
    "wrong_kid": "Recorded: wrong kid",
    "too_noisy": "Recorded: too noisy",
    "show_original": "Sending original",
    "add_calendar": "Adding to calendar",
}

FEEDBACK_VERDICTS: dict[str, str] = {
    "useful": "positive",
    "not_useful": "negative",
    "wrong_kid": "negative",
    "too_noisy": "negative",
}


def _bot_url(settings: PipelineSettings, method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


async def _send_message(
    settings: PipelineSettings,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
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
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(_bot_url(settings, "sendMessage"), json=payload)
        response.raise_for_status()
        data = response.json()
        result = data.get("result") if isinstance(data, dict) else None
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


def _build_calendar_prompt(parsed_json: str, subject: str, sender: str) -> str:
    return "\n".join(
        [
            "Use the google skill to create a Google Calendar event from this forwarded school email.",
            "Use account alias 'default' and calendar 'primary' unless the email clearly implies otherwise.",
            "Return a one-line confirmation including event title, start, and a link if available.",
            "If start time is missing, infer a reasonable all-day date and note that in the confirmation.",
            "If no calendar item can be inferred, reply exactly 'NO_CALENDAR_ITEM'.",
            "",
            f"Email subject: {subject}",
            f"Email sender: {sender}",
            "",
            "Structured parse JSON (use calendar_items first, fall back to action_items deadlines):",
            parsed_json,
        ]
    )


def _run_ash_chat(prompt: str, settings: PipelineSettings) -> tuple[int, str, str]:
    uv_bin = shutil.which("uv") or "/home/linuxbrew/.linuxbrew/bin/uv"
    cmd = [uv_bin, "run", "ash", "chat", "--no-streaming"]
    if settings.ash_model:
        cmd.extend(["--model", settings.ash_model])
    cmd.append(prompt)
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(
        cmd,
        cwd=settings.ash_cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return completed.returncode, completed.stdout, completed.stderr


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text: str) -> str:
    cleaned = _ANSI_RE.sub("", text or "").strip()
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


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
    parsed_json = row.get("structured_parse_json") or "{}"
    subject = row.get("subject") or ""
    sender = row.get("sender") or ""

    await _send_message(
        settings,
        f"Working on calendar event for email #{email_id}\u2026",
        reply_to_message_id=reply_to,
    )
    prompt = _build_calendar_prompt(parsed_json, subject, sender)
    try:
        rc, stdout, stderr = _run_ash_chat(prompt, settings)
    except subprocess.TimeoutExpired:
        await _send_message(settings, "Calendar creation timed out.", reply_to_message_id=reply_to)
        return "timeout"
    output = _clean(stdout) or _clean(stderr) or "(no output)"
    if rc != 0:
        await _send_message(
            settings,
            f"Calendar creation failed:\n{_short(output, 1500)}",
            reply_to_message_id=reply_to,
        )
        return "failed"
    if "NO_CALENDAR_ITEM" in output:
        await _send_message(
            settings,
            "No calendar item could be inferred from this email.",
            reply_to_message_id=reply_to,
        )
        return "no_item"
    await _send_message(settings, _short(output, 1500), reply_to_message_id=reply_to)
    return "created"


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
            verdict = FEEDBACK_VERDICTS.get(feedback_type)
            if verdict and email_id is not None:
                parsed_snapshot = store.get_email_parsed_result(email_id)
                from .models import FeedbackEvent as _FBEv
                store.log_feedback(
                    _FBEv(
                        email_id=email_id,
                        telegram_message_id=telegram_message_id,
                        feedback_type=feedback_type,
                        payload={"ack": label},
                    ),
                    verdict=verdict,
                    parsed_snapshot=parsed_snapshot,
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
