"""Format a daily digest of recent email triage results."""

from __future__ import annotations

import json
from typing import Any


def format_digest(
    emails: list[dict[str, Any]],
    *,
    hours: int = 24,
) -> str:
    """Build a human-readable digest message for Telegram."""
    if not emails:
        return f"No school emails in the last {hours}h."

    delivered: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []

    for row in emails:
        status = row.get("processing_status") or "unknown"
        if status in ("delivered", "telegram_ready"):
            delivered.append(row)
        elif status in ("ignored", "sender_suppressed"):
            suppressed.append(row)
        elif status == "error":
            errors.append(row)
        else:
            other.append(row)

    lines: list[str] = [
        f"School Email Digest — Last {hours}h",
        f"Total: {len(emails)} | Delivered: {len(delivered)} | "
        f"Suppressed: {len(suppressed)} | Errors: {len(errors)}",
        "",
    ]

    if delivered:
        lines.append("--- Delivered ---")
        for row in delivered:
            subject = row.get("subject") or "(no subject)"
            sender = _short_sender(row.get("sender") or "")
            parsed = _parse_json(row.get("structured_parse_json"))
            email_type = parsed.get("email_type", "?")
            importance = parsed.get("importance", "?")
            audience = _audience_str(parsed.get("audience"))
            lines.append(f"  [{importance}] {subject}")
            lines.append(f"    From: {sender} | Type: {email_type} | For: {audience}")

    if suppressed:
        lines.append("")
        lines.append("--- Suppressed ---")
        by_reason: dict[str, list[str]] = {}
        for row in suppressed:
            reason = row.get("processing_status", "unknown")
            subject = row.get("subject") or "(no subject)"
            by_reason.setdefault(reason, []).append(subject)
        for reason, subjects in by_reason.items():
            lines.append(f"  {reason.replace('_', ' ').title()} ({len(subjects)}):")
            for s in subjects[:5]:
                lines.append(f"    - {s}")
            if len(subjects) > 5:
                lines.append(f"    ... and {len(subjects) - 5} more")

    if errors:
        lines.append("")
        lines.append("--- Errors ---")
        for row in errors[:5]:
            subject = row.get("subject") or "(no subject)"
            lines.append(f"  Error: {subject}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3995] + "\n..."
    return text


def _short_sender(sender: str) -> str:
    if "<" in sender:
        name = sender.split("<")[0].strip()
        if name:
            return name
    if "@" in sender:
        return sender.split("@")[0]
    return sender[:30]


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _audience_str(audience: Any) -> str:
    if not isinstance(audience, dict):
        return "?"
    parts = []
    if audience.get("applies_to_second_grader"):
        parts.append("2nd")
    if audience.get("applies_to_fifth_grader"):
        parts.append("5th")
    if audience.get("applies_to_whole_school"):
        parts.append("whole school")
    return "/".join(parts) if parts else "unknown"
