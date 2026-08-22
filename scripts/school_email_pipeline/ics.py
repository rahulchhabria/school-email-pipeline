from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import CalendarItem, PipelineEmail, StructuredParseResult


ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
DEFAULT_TZID = "America/Los_Angeles"


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
        dtstart = f"DTSTART;TZID={DEFAULT_TZID}:{_format_local_datetime(start_dt)}"
        dtend = f"DTEND;TZID={DEFAULT_TZID}:{_format_local_datetime(end_dt)}"
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
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ash School Email Pipeline//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        f"X-WR-CALNAME:{_escape_text(title)}",
        f"X-WR-TIMEZONE:{DEFAULT_TZID}",
    ]
    if "TZID=" in dtstart or "TZID=" in dtend:
        lines.extend(_vtimezone_lines(DEFAULT_TZID))
    lines.extend(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}@ash-school-email",
            f"DTSTAMP:{stamp}",
            f"CREATED:{stamp}",
            f"LAST-MODIFIED:{stamp}",
            "SEQUENCE:0",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "ORGANIZER;CN=Ash School Email:mailto:ash@inbox.chhab.com",
            "ATTENDEE;CN=Rahul Chhabria;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:rahul.chhabria@gmail.com",
            dtstart,
            dtend,
            f"SUMMARY:{_escape_text(title)}",
        ]
    )
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
        caption="Calendar invite attached. Open it from email to add it to your calendar.",
    )


def _vtimezone_lines(tzid: str) -> list[str]:
    if tzid != "America/Los_Angeles":
        return ["BEGIN:VTIMEZONE", f"TZID:{tzid}", "END:VTIMEZONE"]
    return [
        "BEGIN:VTIMEZONE",
        "TZID:America/Los_Angeles",
        "X-LIC-LOCATION:America/Los_Angeles",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:-0800",
        "TZOFFSETTO:-0700",
        "TZNAME:PDT",
        "DTSTART:19700308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:-0700",
        "TZOFFSETTO:-0800",
        "TZNAME:PST",
        "DTSTART:19701101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]


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
