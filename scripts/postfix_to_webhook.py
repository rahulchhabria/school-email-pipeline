#!/usr/bin/env python3
"""Read a raw email from stdin and forward a normalized payload to the local receiver."""

from __future__ import annotations

import json
import os
import sys
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Any
from urllib import error, request

DEFAULT_WEBHOOK_URL = "http://127.0.0.1:8787/webhooks/email"
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 180.0


def _addresses(header_value: str | None) -> str:
    if not header_value:
        return ""
    values = []
    for name, addr in getaddresses([header_value]):
        if name and addr:
            values.append(f"{name} <{addr}>")
        elif addr:
            values.append(addr)
        elif name:
            values.append(name)
    return ", ".join(values)


def _extract_text(msg) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            try:
                content = part.get_content()
            except Exception:
                continue
            if not isinstance(content, str):
                continue
            if content_type == "text/plain":
                text_parts.append(content)
            elif content_type == "text/html":
                html_parts.append(content)
    else:
        try:
            content = msg.get_content()
        except Exception:
            content = ""
        if isinstance(content, str):
            if msg.get_content_type() == "text/html":
                html_parts.append(content)
            else:
                text_parts.append(content)

    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()


def _build_payload(raw_bytes: bytes) -> dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    text_body, html_body = _extract_text(msg)
    return {
        "message_id": (msg.get("Message-ID") or "").strip(),
        "from": _addresses(msg.get("From")),
        "to": _addresses(msg.get("To")),
        "subject": (msg.get("Subject") or "").strip(),
        "date": (msg.get("Date") or "").strip(),
        "text": text_body,
        "html": html_body,
    }


def main() -> int:
    webhook_url = os.environ.get("EMAIL_FORWARD_WEBHOOK_URL", DEFAULT_WEBHOOK_URL)
    webhook_secret = os.environ.get("EMAIL_FORWARD_WEBHOOK_SECRET", "").strip()
    webhook_timeout = float(
        os.environ.get("EMAIL_FORWARD_WEBHOOK_TIMEOUT_SECONDS")
        or DEFAULT_WEBHOOK_TIMEOUT_SECONDS
    )
    raw_bytes = sys.stdin.buffer.read()
    if not raw_bytes:
        print("No email content on stdin", file=sys.stderr)
        return 1

    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        headers["X-Email-Webhook-Secret"] = webhook_secret

    payload = _build_payload(raw_bytes)
    try:
        req = request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with request.urlopen(req, timeout=webhook_timeout) as response:
            if response.status >= 400:
                raise RuntimeError(f"Webhook returned HTTP {response.status}")
    except (OSError, error.URLError, error.HTTPError, RuntimeError) as exc:
        print(f"Webhook delivery failed: {exc}", file=sys.stderr)
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
