#!/usr/bin/env python3
# /// script
# dependencies = [
#   "fastapi>=0.115.0",
#   "uvicorn[standard]>=0.32.0",
#   "httpx>=0.27.0",
#   "openai>=1.50.0",
#   "pydantic>=2.9.0",
#   "sentry-sdk[fastapi]>=2.60.0",
# ]
# ///
"""Receive forwarded-email webhooks, parse school emails, and relay Telegram alerts."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import sentry_sdk
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from school_email_pipeline.actions import dispatch_callback_action
from school_email_pipeline.digest import format_digest
from school_email_pipeline.models import PipelineEmail
from school_email_pipeline.parser import set_conversation_id as set_parser_conversation_id
from school_email_pipeline.pipeline import process_school_email
from school_email_pipeline.settings import load_pipeline_settings
from school_email_pipeline.storage import EmailStore
from school_email_pipeline.telegram import log_callback_feedback

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_STATE_PATH = SKILL_DIR / "data" / "state.json"
DEFAULT_BODY_CHAR_LIMIT = 12000
DEFAULT_ASH_CWD = Path("/home/rahul/GitHub/ash")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
HTML_BLOCK_BREAK_RE = re.compile(
    r"</?(?:br|div|p|tr|li|h[1-6])\b[^>]*>",
    re.IGNORECASE,
)
FORWARDED_MARKER_RE = re.compile(
    r"^\s*(?:[-]+\s*Forwarded message\s*[-]+|Begin forwarded message:)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
FORWARDED_HEADER_RE = re.compile(
    r"^\s*(from|to|cc|bcc|subject|date|sent):\s*(.+?)\s*$",
    re.IGNORECASE,
)
logger = logging.getLogger("email_forward_receiver")


class StateModel(BaseModel):
    """Persistent webhook receiver state."""

    model_config = ConfigDict(extra="ignore")

    seen_message_ids: list[str] = Field(default_factory=list)
    updated_at: str | None = None


class NormalizedEmail(BaseModel):
    """Provider-agnostic normalized email payload."""

    model_config = ConfigDict(extra="ignore")

    message_id: str
    from_: str = Field(alias="from")
    to: str = ""
    subject: str = ""
    date: str = ""
    text_body: str = ""
    html_body: str = ""
    provider: str = "generic"
    raw_payload: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the receiver."""

    webhook_secret: str
    telegram_bot_token: str
    telegram_chat_id: str
    ash_cwd: Path
    ash_model: str | None
    state_path: Path
    dry_run: bool
    body_char_limit: int


def load_settings() -> Settings:
    """Load runtime configuration from the environment."""
    return Settings(
        webhook_secret=os.environ.get("EMAIL_FORWARD_WEBHOOK_SECRET", "").strip(),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        ash_cwd=Path(os.environ.get("EMAIL_FORWARD_ASH_CWD", DEFAULT_ASH_CWD)),
        ash_model=os.environ.get("EMAIL_FORWARD_ASH_MODEL", "").strip() or None,
        state_path=Path(os.environ.get("EMAIL_FORWARD_STATE_PATH", DEFAULT_STATE_PATH)),
        dry_run=os.environ.get("EMAIL_FORWARD_DRY_RUN", "").strip()
        in {"1", "true", "yes"},
        body_char_limit=max(
            1000,
            _parse_int(
                os.environ.get("EMAIL_FORWARD_BODY_CHAR_LIMIT"),
                default=DEFAULT_BODY_CHAR_LIMIT,
            ),
        ),
    )


def _parse_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _load_state(path: Path) -> StateModel:
    if not path.exists():
        return StateModel()
    try:
        return StateModel.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return StateModel()


def _save_state(path: Path, state: StateModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump()
    payload["updated_at"] = _utc_now()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _strip_html(html: str) -> str:
    text = TAG_RE.sub(" ", html)
    return WHITESPACE_RE.sub(" ", unescape(text)).strip()


def _strip_html_for_header_scan(html: str) -> str:
    text = HTML_BLOCK_BREAK_RE.sub("\n", html)
    text = TAG_RE.sub(" ", text)
    lines = [
        WHITESPACE_RE.sub(" ", unescape(line)).strip()
        for line in text.splitlines()
    ]
    return "\n".join(line for line in lines if line)


def _compact(text: str, *, limit: int) -> str:
    collapsed = WHITESPACE_RE.sub(" ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _coalesce_text(text_body: str, html_body: str, *, limit: int) -> str:
    if text_body.strip():
        return _compact(text_body, limit=limit)
    if html_body.strip():
        return _compact(_strip_html(html_body), limit=limit)
    return ""


def _extract_forwarded_date(*bodies: str) -> str:
    for body in bodies:
        if not body.strip():
            continue
        lines = body.splitlines()
        marker_starts = [
            body[: match.end()].count("\n")
            for match in FORWARDED_MARKER_RE.finditer(body)
        ]
        starts = marker_starts or [
            index
            for index, line in enumerate(lines[:40])
            if FORWARDED_HEADER_RE.match(line)
        ]
        for start in starts:
            if forwarded_date := _extract_forwarded_date_from_lines(lines, start):
                return forwarded_date
    return ""


def _extract_forwarded_date_from_lines(lines: list[str], start: int) -> str:
    seen_headers: set[str] = set()
    forwarded_date = ""
    for line in lines[start : start + 30]:
        match = FORWARDED_HEADER_RE.match(line)
        if not match:
            if seen_headers and not line.strip():
                continue
            if seen_headers:
                break
            continue

        header = match.group(1).lower()
        value = match.group(2).strip()
        seen_headers.add(header)
        if header in {"date", "sent"}:
            forwarded_date = value
            continue
        if forwarded_date and seen_headers & {"from", "subject", "to"}:
            return forwarded_date

    if forwarded_date and seen_headers & {"from", "subject", "to"}:
        return forwarded_date
    return ""


def _header_secret(request: Request) -> str:
    return request.headers.get("x-email-webhook-secret", "").strip()


async def _read_request_payload(request: Request) -> tuple[str, dict[str, Any]]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        if isinstance(payload, dict):
            return "json", payload
        raise HTTPException(status_code=400, detail="JSON payload must be an object")

    body = await request.body()
    if "application/x-www-form-urlencoded" in content_type:
        parsed = parse_qs(body.decode(errors="replace"), keep_blank_values=False)
        return "form", {key: values[-1] for key, values in parsed.items() if values}

    if "multipart/form-data" in content_type:
        raise HTTPException(
            status_code=415,
            detail="multipart/form-data is not supported by this local build; configure your provider to send JSON or URL-encoded payloads",
        )

    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")
    raise HTTPException(
        status_code=415, detail=f"Unsupported content type: {content_type}"
    )


def _pick(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_email(
    payload_type: str,
    payload: dict[str, Any],
    *,
    body_char_limit: int,
) -> NormalizedEmail:
    """Normalize common webhook payloads into a single email model."""
    del payload_type
    provider = "generic"
    if "Message-Id" in payload or "body-plain" in payload:
        provider = "mailgun"
    elif "MessageID" in payload or "TextBody" in payload:
        provider = "postmark"

    message_id = _pick(
        payload,
        "message_id",
        "Message-Id",
        "MessageID",
        "message-id",
        "headers.message-id",
    )
    if not message_id:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        message_id = f"generated:{digest}"

    text_body = _pick(
        payload, "text_body", "text", "body-plain", "TextBody", "stripped-text"
    )
    html_body = _pick(
        payload, "html_body", "html", "body-html", "HtmlBody", "stripped-html"
    )
    received_date = _pick(payload, "date", "Date", "timestamp")
    if not received_date:
        received_date = _extract_forwarded_date(
            text_body,
            _strip_html_for_header_scan(html_body) if html_body else "",
        )

    normalized = NormalizedEmail.model_validate(
        {
            "message_id": message_id,
            "from": _pick(payload, "from", "sender", "From", "FromFull", "from_email"),
            "to": _pick(payload, "to", "recipient", "To", "to_email"),
            "subject": _pick(payload, "subject", "Subject"),
            "date": received_date,
            "text_body": _coalesce_text(text_body, "", limit=body_char_limit),
            "html_body": _compact(html_body, limit=body_char_limit)
            if html_body
            else "",
            "provider": provider,
            "raw_payload": payload,
        }
    )
    if not normalized.text_body and normalized.html_body:
        normalized = normalized.model_copy(
            update={
                "text_body": _coalesce_text(
                    "", normalized.html_body, limit=body_char_limit
                )
            }
        )
    return normalized


def _build_skill_prompt(email: NormalizedEmail) -> str:
    body = email.text_body or "(empty body)"
    return "\n".join(
        [
            "Use the sfday-telegram-alert skill in direct email mode.",
            "Do not poll Gmail for this request.",
            "Summarize the forwarded email below for Telegram using the sfday family-oriented summary style.",
            "Return ONLY the final Telegram-ready message.",
            "",
            "Normalized email payload:",
            f"- message_id: {email.message_id}",
            f"- from: {email.from_ or '(unknown)'}",
            f"- to: {email.to or '(unknown)'}",
            f"- subject: {email.subject or '(no subject)'}",
            f"- date: {email.date or '(unknown)'}",
            "",
            "Body:",
            body,
        ]
    )


def _clean_ash_output(text: str) -> str:
    cleaned = ANSI_RE.sub("", text).strip()
    if not cleaned:
        return ""
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def run_ash_summary(email: NormalizedEmail, settings: Settings) -> str:
    """Invoke Ash CLI and return the skill result."""
    prompt = _build_skill_prompt(email)
    cmd = ["uv", "run", "ash", "chat", "--no-streaming"]
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
    )
    stdout = _clean_ash_output(completed.stdout)
    stderr = _clean_ash_output(completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or "ash chat failed without output")
    return stdout


async def send_telegram(settings: Settings, text: str) -> int | None:
    """Send a message to Telegram."""
    if settings.dry_run:
        return None
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        result = data.get("result", {})
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
    return None


def _to_pipeline_email(email: NormalizedEmail) -> PipelineEmail:
    return PipelineEmail(
        message_id=email.message_id,
        sender=email.from_,
        recipient=email.to,
        subject=email.subject,
        received_at=email.date,
        text_body=email.text_body,
        html_body=email.html_body,
        provider=email.provider,
        raw_payload=email.raw_payload,
    )


def _from_pipeline_email(email: PipelineEmail) -> NormalizedEmail:
    return NormalizedEmail.model_validate(
        {
            "message_id": email.message_id,
            "from": email.sender,
            "to": email.recipient,
            "subject": email.subject,
            "date": email.received_at,
            "text_body": email.text_body,
            "html_body": email.html_body,
            "provider": email.provider,
            "raw_payload": email.raw_payload,
        }
    )


async def process_email(email: NormalizedEmail, settings: Settings) -> dict[str, Any]:
    """Deduplicate, parse, route, and optionally deliver a Telegram alert."""
    conversation_id = _email_conversation_id(email)
    logger.info(
        "processing_email",
        extra={
            "message_id": email.message_id,
            "sentry.conversation_id": conversation_id,
            "from": email.from_,
            "subject": email.subject,
            "provider": email.provider,
        },
    )
    _set_sentry_conversation_id(conversation_id)
    set_parser_conversation_id(conversation_id)
    sentry_sdk.set_tag("email.message_id", email.message_id)
    sentry_sdk.set_tag("email.sender", email.from_)
    sentry_sdk.set_tag("sentry.conversation_id", conversation_id)
    sentry_sdk.set_context("email", {
        "message_id": email.message_id,
        "conversation_id": conversation_id,
        "sender": email.from_,
        "subject": email.subject,
    })
    pipeline_settings = load_pipeline_settings()
    pipeline_email = _to_pipeline_email(email)

    result = await process_school_email(
        pipeline_email,
        pipeline_settings,
        legacy_summary_runner=lambda item: run_ash_summary(
            _from_pipeline_email(item), settings
        ),
        legacy_sender=lambda text: send_telegram(settings, text),
    )
    return result.model_dump()


def _email_conversation_id(email: NormalizedEmail) -> str:
    """Build a stable non-PII conversation ID for one inbound email."""
    raw_id = email.message_id or f"{email.from_}|{email.subject}|{email.date}"
    digest = hashlib.sha256(raw_id.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"school-email-{digest}"


def _set_sentry_conversation_id(conversation_id: str) -> None:
    try:
        sentry_ai = importlib.import_module("sentry_sdk.ai")
        sentry_ai.set_conversation_id(conversation_id)
    except Exception:
        logger.debug("sentry_conversation_id_skipped", exc_info=True)


def create_app(settings: Settings) -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="Email Forward Receiver", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        pipeline_settings = load_pipeline_settings()
        return {
            "ok": True,
            "dry_run": settings.dry_run,
            "ash_cwd": str(settings.ash_cwd),
            "state_path": str(settings.state_path),
            "database_url": pipeline_settings.database_url,
            "openai_model": pipeline_settings.openai_model,
            "enable_ash": pipeline_settings.enable_ash,
        }

    @app.post("/webhooks/email")
    async def webhooks_email(request: Request) -> dict[str, Any]:
        expected = settings.webhook_secret
        if expected and _header_secret(request) != expected:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

        payload_type, payload = await _read_request_payload(request)
        email = normalize_email(
            payload_type,
            payload,
            body_char_limit=settings.body_char_limit,
        )
        if not email.from_ and not email.subject and not email.text_body:
            raise HTTPException(
                status_code=400, detail="Could not normalize email payload"
            )

        try:
            return await process_email(email, settings)
        except Exception as exc:  # noqa: BLE001
            logger.exception("webhook_processing_failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/webhooks/telegram/callback")
    async def telegram_callback(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Telegram update must be an object"
            )
        pipeline_settings = load_pipeline_settings()
        store = EmailStore(pipeline_settings.database_url)
        handled = log_callback_feedback(payload, store)
        action_result: dict[str, Any] = {}
        if handled:
            callback = payload.get("callback_query") or {}
            data = str(callback.get("data") or "")
            parts = data.split(":", 2)
            email_id_int: int | None = None
            feedback_type = ""
            if len(parts) == 3 and parts[0] == "fb":
                try:
                    email_id_int = int(parts[1])
                except ValueError:
                    email_id_int = None
                feedback_type = parts[2]
            message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
            tg_message_id = message.get("message_id") if isinstance(message, dict) else None
            if not isinstance(tg_message_id, int):
                tg_message_id = None
            if feedback_type and email_id_int is not None:
                action_result = await dispatch_callback_action(
                    feedback_type,
                    email_id_int,
                    tg_message_id,
                    pipeline_settings,
                    store,
                )
        return {"ok": True, "handled": handled, "action": action_result}

    @app.get("/digest")
    async def digest(hours: int = 24) -> dict[str, Any]:
        pipeline_settings = load_pipeline_settings()
        store = EmailStore(pipeline_settings.database_url)
        emails = store.get_recent_emails(hours=hours)
        text = format_digest(emails, hours=hours)

        if not settings.dry_run and settings.telegram_bot_token and settings.telegram_chat_id:
            try:
                await send_telegram(settings, text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("digest_send_failed", extra={"error": str(exc)})

        return {"ok": True, "hours": hours, "count": len(emails), "text": text}

    @app.post("/webhooks/telegram/command")
    async def telegram_command(request: Request) -> dict[str, Any]:
        """Handle /digest and other commands forwarded by ash."""
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Command payload must be an object")
        message = payload.get("message") or {}
        text = str(message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id") if isinstance(message.get("chat"), dict) else None

        if text.startswith("/digest"):
            parts = text.split()
            hours = 24
            if len(parts) > 1:
                try:
                    hours = int(parts[1])
                except ValueError:
                    hours = 24
            pipeline_settings = load_pipeline_settings()
            store = EmailStore(pipeline_settings.database_url)
            emails = store.get_recent_emails(hours=hours)
            digest_text = format_digest(emails, hours=hours)
            if not settings.dry_run and settings.telegram_bot_token and settings.telegram_chat_id:
                await send_telegram(settings, digest_text)
            return {"ok": True, "command": "digest", "hours": hours}

        return {"ok": True, "command": "unknown", "text": text}

    return app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI server")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    sample_parser = subparsers.add_parser(
        "print-sample", help="Print a sample JSON payload for testing"
    )
    sample_parser.add_argument(
        "--message-id",
        default="<sample-1@example.com>",
        help="Sample message id",
    )

    return parser.parse_args()


def _print_sample(message_id: str) -> None:
    payload = {
        "message_id": message_id,
        "from": "Alice Example <alice@example.com>",
        "to": "school-bot@example.com",
        "subject": "Reminder: field trip waiver due Friday",
        "date": "2026-03-22T10:00:00Z",
        "text": "Please sign and return the field trip waiver by Friday at 3pm.",
    }
    print(json.dumps(payload, indent=2))


def _init_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=dsn,
            integrations=[FastApiIntegration()],
            traces_sample_rate=float(
                os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")
            ),
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=os.environ.get("SENTRY_RELEASE", "school-email-pipeline@0.1.0"),
            stream_gen_ai_spans=os.environ.get(
                "SENTRY_STREAM_GEN_AI_SPANS", "1"
            ).lower()
            not in {"0", "false", "no", "off"},
            send_default_pii=os.environ.get("SENTRY_SEND_DEFAULT_PII", "1").lower()
            not in {"0", "false", "no", "off"},
            enable_logs=True,
        )
        logger.info("sentry_initialized")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentry_init_failed", extra={"error": str(exc)})


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("EMAIL_FORWARD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _init_sentry()
    args = _parse_args()
    settings = load_settings()

    if args.command == "print-sample":
        _print_sample(args.message_id)
        return 0

    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
