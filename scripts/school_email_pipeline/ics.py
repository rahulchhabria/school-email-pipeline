from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import CalendarItem, PipelineEmail, StructuredParseResult


ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


@dataclass(frozen=True)
class CalendarAttachment:
    filename: str
    content: str
    caption: str


def build_calendar_attachment(
    email: PipelineEmail,
    parsed: StructuredParseResult,
) -> CalendarAttachment | None:
    item = _best_calendar_item(parsed.calendar_items)
    if item is None:
        return None

    start = (item.start or "").strip()
    if ISO_DATE_RE.match(start):
        dtstart = f"DTSTART;VALUE=DATE:{start.replace('-', '')}"
        end = (item.end or "").strip()
        if ISO_DATE_RE.match(end):
            end_date = datetime.fromisoformat(end).date()
        else:
            end_date = datetime.fromisoformat(start).date() + timedelta(days=1)
        dtend = f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}"
    elif ISO_DATETIME_RE.match(start):
        start_dt = datetime.fromisoformat(start)
        end = (item.end or "").strip()
        if ISO_DATETIME_RE.match(end):
            end_dt = datetime.fromisoformat(end)
        else:
            end_dt = start_dt + timedelta(hours=1)
        dtstart = f"DTSTART:{_format_local_datetime(start_dt)}"
        dtend = f"DTEND:{_format_local_datetime(end_dt)}"
    else:
        return None

    title = item.title or email.subject or "School event"
    uid_source = f"{email.message_id}|{title}|{start}"
    uid = hashlib.sha256(uid_source.encode("utf-8")).hexdigest()[:24]
    description = "\n".join(
        part
        for part in [
            parsed.telegram_summary,
            parsed.why_it_matters,
            f"From: {email.sender}" if email.sender else "",
            f"Subject: {email.subject}" if email.subject else "",
        ]
        if part
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ash School Email Pipeline//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}@ash-school-email",
        f"DTSTAMP:{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        dtstart,
        dtend,
        f"SUMMARY:{_escape_text(title)}",
    ]
    if item.location:
        lines.append(f"LOCATION:{_escape_text(item.location)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape_text(description)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR"])

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:48]
    filename = f"{safe_name or 'school-event'}.ics"
    return CalendarAttachment(
        filename=filename,
        content="\r\n".join(_fold_line(line) for line in lines) + "\r\n",
        caption="Calendar file attached. Open it on your phone to add it to Google Calendar.",
    )


def _best_calendar_item(items: list[CalendarItem]) -> CalendarItem | None:
    dated = [item for item in items if item.start and item.confidence >= 0.7]
    if not dated:
        return None
    return max(dated, key=lambda item: item.confidence)


def _format_local_datetime(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_line(line: str) -> str:
    if len(line) <= 75:
        return line
    chunks = [line[:75]]
    rest = line[75:]
    while rest:
        chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(chunks)
