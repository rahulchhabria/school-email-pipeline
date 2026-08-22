from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from school_email_pipeline.cleanup import cleanup_email_body  # noqa: E402
from school_email_pipeline.entities import extract_entities  # noqa: E402
from school_email_pipeline.actions import _google_calendar_url, _send_calendar_email  # noqa: E402
from school_email_pipeline.ics import CalendarAttachment, build_calendar_attachment  # noqa: E402
from school_email_pipeline.models import (  # noqa: E402
    ExtractedEntity,
    FeedbackEvent,
    PipelineEmail,
    StructuredParseResult,
)
from school_email_pipeline.parser import _completion_id, _validate_json  # noqa: E402
from school_email_pipeline.pipeline import _is_google_calendar_notification  # noqa: E402
from school_email_pipeline.routing import RoutingPolicy, route_email  # noqa: E402
from school_email_pipeline.settings import PipelineSettings  # noqa: E402
from school_email_pipeline.storage import EmailStore  # noqa: E402
from school_email_pipeline.telegram import format_telegram_message  # noqa: E402
from email_webhook_server import normalize_email  # noqa: E402


def _settings(tmp_path: Path) -> PipelineSettings:
    return PipelineSettings(
        database_url=f"sqlite:///{tmp_path / 'emails.sqlite3'}",
        policy_config_path=tmp_path / "policy.json",
        openai_api_key="",
        openai_model="test",
        openai_timeout_seconds=1,
        openai_base_url="",
        pioneer_api_key="",
        pioneer_base_url="",
        pioneer_timeout_seconds=30.0,
        enable_ash=False,
        ash_base_url="",
        body_char_limit=12000,
        legacy_fallback_enabled=True,
        telegram_bot_token="",
        telegram_chat_id="",
        dry_run=True,
        ash_cwd=tmp_path,
        ash_model=None,
        senders_config_path=tmp_path / "senders.toml",
    )


def _policy() -> RoutingPolicy:
    return RoutingPolicy(
        immediate_importance={"high", "urgent"},
        deadline_within_days=7,
        high_calendar_confidence=0.8,
        parser_confidence_review_threshold=0.7,
        audience_confidence_review_threshold=0.7,
        daily_digest_email_types={"newsletter"},
        daily_digest_importance={"low"},
        relevant_audiences={"3rd grader", "6th grader", "both"},
        telegram_for_human_review=True,
    )


def _parsed(**overrides: object) -> StructuredParseResult:
    data = {
        "email_type": "action_required",
        "audience": {
            "applies_to_second_grader": True,
            "applies_to_fifth_grader": False,
            "applies_to_whole_school": False,
            "confidence": 0.9,
        },
        "importance": "medium",
        "parent_action_required": True,
        "action_items": [
            {
                "action": "Return the field trip form",
                "deadline": "2026-05-25",
                "applies_to": "3rd grader",
                "confidence": 0.9,
            }
        ],
        "calendar_items": [],
        "telegram_summary": "Field trip form is due soon.",
        "why_it_matters": "This affects the 3rd grader's field trip.",
        "confidence": 0.9,
        "needs_human_review": False,
    }
    data.update(overrides)
    return StructuredParseResult.model_validate(data)


def test_email_store_dedupes_message_id(tmp_path: Path) -> None:
    from school_email_pipeline.models import PipelineEmail

    store = EmailStore(f"sqlite:///{tmp_path / 'emails.sqlite3'}")
    email = PipelineEmail(message_id="<a@b>", sender="school", subject="hello")

    first_id, first_new = store.create_email_if_new(email)
    second_id, second_new = store.create_email_if_new(email)

    assert first_new is True
    assert second_new is False
    assert second_id == first_id


def test_cleanup_html_email_body() -> None:
    body = cleanup_email_body(
        "",
        "<html><body><h1>Trip</h1><p>Bring form&nbsp;Friday.</p><script>x()</script></body></html>",
        limit=200,
    )

    assert "Trip" in body
    assert "Bring form Friday." in body
    assert "script" not in body


def test_extract_entities_is_disabled(tmp_path: Path) -> None:
    """GLiNER extraction is permanently disabled in the OpenAI-only pipeline."""
    assert extract_entities("anything at all", _settings(tmp_path)) == []


def test_normalize_email_uses_forwarded_text_date_when_payload_date_missing() -> None:
    email = normalize_email(
        "json",
        {
            "message_id": "<outer@local>",
            "from": "Rahul <rahul@example.com>",
            "to": "school@local",
            "subject": "Fwd: Field trip",
            "text": "\n".join(
                [
                    "FYI",
                    "",
                    "---------- Forwarded message ---------",
                    "From: School Office <office@school.edu>",
                    "Date: Fri, May 29, 2026 at 8:15 AM",
                    "Subject: Field trip",
                    "To: Parents <parents@school.edu>",
                    "",
                    "Please return the form.",
                ]
            ),
        },
        body_char_limit=2000,
    )

    assert email.date == "Fri, May 29, 2026 at 8:15 AM"


def test_normalize_email_uses_forwarded_html_sent_date_when_payload_date_missing() -> None:
    email = normalize_email(
        "json",
        {
            "message_id": "<outer-html@local>",
            "from": "Rahul <rahul@example.com>",
            "subject": "Fwd: Reminder",
            "html": """
                <div>Forwarding this</div>
                <div>Begin forwarded message:</div>
                <div>From: School Office &lt;office@school.edu&gt;</div>
                <div>Sent: Friday, May 29, 2026 8:15 AM</div>
                <div>Subject: Reminder</div>
                <p>Please return the form.</p>
            """,
        },
        body_char_limit=2000,
    )

    assert email.date == "Friday, May 29, 2026 8:15 AM"


def test_google_calendar_notification_detects_rewritten_sender_invite() -> None:
    email = PipelineEmail(
        message_id="<calendar-00b348b9-5fc3-4b55-9bfe-7fde3f42cd9e@google.com>",
        sender="Jocelyn Chhabria <jocelyn.kiyuna@gmail.com>",
        recipient="Rahul Chhabria <rahul.chhabria@gmail.com>",
        subject=(
            "Updated invitation: Coastal Camp - Mari Paid @ "
            "Mon Jul 20 - Fri Jul 24, 2026 (Rahul Chhabria)"
        ),
        text_body=(
            "Invitation from Google Calendar: https://calendar.google.com/calendar/\n"
            "You are receiving this email because you are subscribed to calendar "
            "notifications."
        ),
    )

    assert _is_google_calendar_notification(email) is True


def test_google_calendar_notification_detects_calendar_notification_sender() -> None:
    email = PipelineEmail(
        message_id="<some-message@google.com>",
        sender="Google Calendar <calendar-notification@google.com>",
        subject="Canceled event: Soccer",
    )

    assert _is_google_calendar_notification(email) is True


def test_google_calendar_notification_does_not_match_school_calendar_email() -> None:
    email = PipelineEmail(
        message_id="<school-calendar@sfday.org>",
        sender="School Office <office@sfday.org>",
        subject="Invitation: Closing Assembly",
        text_body="Please join the closing assembly at school.",
    )

    assert _is_google_calendar_notification(email) is False


def test_openai_json_validation_accepts_strict_schema() -> None:
    parsed = _validate_json(_parsed().model_dump_json())

    assert parsed.email_type == "action_required"
    assert parsed.audience.applies_to_second_grader is True


def test_completion_id_uses_openai_completion_id() -> None:
    class Completion:
        id = "chatcmpl-123"

    assert _completion_id(Completion()) == "chatcmpl-123"


def test_routing_policy_immediate_for_action_deadline() -> None:
    decision = route_email(
        _parsed(),
        _policy(),
        now=datetime(2026, 5, 23, tzinfo=UTC),
    )

    assert "send_immediate" in decision.actions
    assert decision.relevance == "3rd grader"


def test_routing_sends_uncertain_audience_to_human_review() -> None:
    parsed = _parsed(
        email_type="calendar_event",
        audience={
            "applies_to_second_grader": False,
            "applies_to_fifth_grader": False,
            "applies_to_whole_school": False,
            "confidence": 0.45,
        },
        parent_action_required=False,
        action_items=[],
        calendar_items=[
            {
                "title": "Field trip",
                "start": "08:45",
                "end": "14:30",
                "date_text": "Wednesday, April 29",
                "time_text": "8:45 AM - 2:30 PM",
                "location": "Tennessee Valley",
                "applies_to": "unknown",
                "confidence": 0.78,
            }
        ],
        confidence=0.72,
        needs_human_review=True,
    )

    decision = route_email(parsed, _policy())

    assert "ignore" not in decision.actions
    assert "needs_human_review" in decision.actions
    assert decision.send_telegram_now is True
    assert decision.processing_status == "telegram_ready"


def test_routing_flags_action_without_deadline_for_review() -> None:
    parsed = _parsed(
        action_items=[
            {
                "action": "Return the field trip form",
                "deadline": None,
                "applies_to": "3rd grader",
                "confidence": 0.9,
            }
        ],
    )

    decision = route_email(parsed, _policy())

    assert "needs_human_review" in decision.actions


def test_telegram_formatting_is_short_and_parent_facing() -> None:
    routing = route_email(_parsed(), _policy(), now=datetime(2026, 5, 23, tzinfo=UTC))
    text = format_telegram_message("Field trip form", _parsed(), routing)

    assert text.startswith("Field trip form")
    assert "For: 3rd grader" in text
    assert "Action: Return the field trip form" in text
    assert "Due: 2026-05-25" in text
    assert "Time: none" not in text
    assert "Date:" not in text


def test_telegram_formatting_combines_calendar_date_and_time() -> None:
    parsed = _parsed(
        parent_action_required=False,
        action_items=[],
        calendar_items=[
            {
                "title": "Third Grade Open House",
                "start": None,
                "end": None,
                "date_text": "Monday, June 1st",
                "time_text": "2:15-3:00",
                "location": "Masonic Courtyard",
                "applies_to": "unknown",
                "confidence": 0.78,
            }
        ],
    )
    decision = route_email(parsed, _policy())

    text = format_telegram_message("Open House", parsed, decision)

    assert text.startswith("Third Grade Open House")
    assert "Event: Monday, June 1st, 2:15-3:00" in text
    assert "Place: Masonic Courtyard" in text
    assert "Action:" not in text


def test_telegram_format_ignores_legacy_entities_positional_arg() -> None:
    """Older callers may still pass an entities list; it must be ignored."""
    routing = route_email(_parsed(), _policy(), now=datetime(2026, 5, 23, tzinfo=UTC))
    text = format_telegram_message(
        "Field trip form",
        _parsed(),
        routing,
        [ExtractedEntity(label="date", text="ignored")],
    )
    assert text.startswith("Field trip form")


def test_build_calendar_attachment_for_iso_datetime() -> None:
    parsed = _parsed(
        parent_action_required=False,
        action_items=[],
        calendar_items=[
            {
                "title": "Third Grade Open House",
                "start": "2026-06-01T14:15:00",
                "end": "2026-06-01T15:00:00",
                "date_text": "Monday, June 1",
                "time_text": "2:15 PM - 3:00 PM",
                "location": "Masonic Courtyard",
                "applies_to": "3rd grader",
                "confidence": 0.9,
            }
        ],
    )
    email = PipelineEmail(
        message_id="<open-house@school>",
        sender="School Office <office@school.edu>",
        subject="Open House",
    )

    attachment = build_calendar_attachment(email, parsed)

    assert attachment is not None
    assert attachment.filename == "Third-Grade-Open-House.ics"
    assert "BEGIN:VCALENDAR" in attachment.content
    assert "BEGIN:VTIMEZONE" in attachment.content
    assert "METHOD:REQUEST" in attachment.content
    assert "ORGANIZER;CN=Ash School Email:mailto:ash@inbox.chhab.com" in attachment.content
    assert "ATTENDEE;CN=Rahul Chhabria;ROLE=REQ-PARTICIPANT" in attachment.content
    assert "X-WR-TIMEZONE:America/Los_Angeles" in attachment.content
    assert "SUMMARY:Third Grade Open House" in attachment.content
    assert "DTSTART;TZID=America/Los_Angeles:20260601T141500" in attachment.content
    assert "DTEND;TZID=America/Los_Angeles:20260601T150000" in attachment.content
    assert "LOCATION:Masonic Courtyard" in attachment.content



def test_google_calendar_url_for_iso_datetime() -> None:
    parsed = _parsed(
        calendar_items=[
            {
                "title": "Third Grade Open House",
                "start": "2026-06-01T14:15:00",
                "end": "2026-06-01T15:00:00",
                "date_text": "Monday, June 1",
                "time_text": "2:15 PM - 3:00 PM",
                "location": "Masonic Courtyard",
                "applies_to": "3rd grader",
                "confidence": 0.9,
            }
        ],
    )
    email = PipelineEmail(
        message_id="<open-house@school>",
        sender="School Office <office@school.edu>",
        subject="Open House",
    )

    url = _google_calendar_url(email, parsed)

    assert url is not None
    assert url.startswith("https://calendar.google.com/calendar/render?")
    assert "action=TEMPLATE" in url
    assert "text=Third+Grade+Open+House" in url
    assert "dates=20260601T141500%2F20260601T150000" in url
    assert "location=Masonic+Courtyard" in url
    assert "ctz=America%2FLos_Angeles" in url

def test_build_calendar_attachment_skips_unresolved_dates() -> None:
    parsed = _parsed(
        parent_action_required=False,
        action_items=[],
        calendar_items=[
            {
                "title": "Open House",
                "start": None,
                "end": None,
                "date_text": "next Monday",
                "time_text": "after lunch",
                "location": None,
                "applies_to": "unknown",
                "confidence": 0.9,
            }
        ],
    )
    email = PipelineEmail(message_id="<open-house@school>", subject="Open House")

    assert build_calendar_attachment(email, parsed) is None


def test_feedback_logging(tmp_path: Path) -> None:
    from school_email_pipeline.models import PipelineEmail

    store = EmailStore(f"sqlite:///{tmp_path / 'emails.sqlite3'}")
    email_id, _ = store.create_email_if_new(
        PipelineEmail(message_id="<feedback@school>", subject="test")
    )

    feedback_id = store.log_feedback(
        FeedbackEvent(
            email_id=email_id,
            telegram_message_id=123,
            feedback_type="useful",
            payload={"callback": "ok"},
        )
    )
    assert isinstance(feedback_id, int) and feedback_id > 0

    with store._connect() as conn:
        row = conn.execute(
            "SELECT feedback_json FROM emails WHERE id = ?", (email_id,)
        ).fetchone()

    feedback = json.loads(row["feedback_json"])
    assert feedback[0]["feedback_type"] == "useful"


def test_openai_finetune_export_emits_canonical_jsonl(tmp_path: Path) -> None:
    from school_email_pipeline.openai_finetune import export_jsonl

    evals_dir = ROOT / "evals"
    out_path = tmp_path / "openai_finetune.jsonl"
    rows = export_jsonl(evals_dir, out_path)

    assert rows > 0
    with out_path.open() as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    assert len(records) == rows
    sample = records[0]
    assert {m["role"] for m in sample["messages"]} == {"system", "user", "assistant"}
    assistant_payload = json.loads(sample["messages"][-1]["content"])
    assert "email_type" in assistant_payload
    assert "audience" in assistant_payload
    assert {"applies_to_second_grader", "applies_to_fifth_grader"}.issubset(
        assistant_payload["audience"]
    )


def test_send_calendar_email_prefers_configured_smtp(monkeypatch) -> None:
    sent: list[object] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            assert host == "smtp.example.com"
            assert port == 587
            assert timeout == 20

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def starttls(self, *, context: object) -> None:
            sent.append("starttls")

        def login(self, username: str, password: str) -> None:
            sent.append((username, password))

        def send_message(self, msg: object) -> None:
            sent.append(msg)

    monkeypatch.setenv("EMAIL_FORWARD_CALENDAR_EMAIL", "rahul.chhabria@gmail.com")
    monkeypatch.setenv("EMAIL_FORWARD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FORWARD_SMTP_USER", "user@example.com")
    monkeypatch.setenv("EMAIL_FORWARD_SMTP_PASSWORD", "secret")
    monkeypatch.setattr("school_email_pipeline.actions.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("school_email_pipeline.actions._send_calendar_email_via_sendmail", lambda msg: False)

    attachment = CalendarAttachment(
        filename="event.ics",
        content="BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n",
        caption="Calendar invite attached.",
    )

    assert _send_calendar_email(attachment, subject="School Event") is True
    assert "starttls" in sent
    assert ("user@example.com", "secret") in sent
    assert any(getattr(item, "get", lambda key: None)("To") == "rahul.chhabria@gmail.com" for item in sent)
