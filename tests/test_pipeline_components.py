from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from school_email_pipeline.cleanup import cleanup_email_body  # noqa: E402
from school_email_pipeline.entities import extract_entities  # noqa: E402
from school_email_pipeline.models import (  # noqa: E402
    FeedbackEvent,
    StructuredParseResult,
)
from school_email_pipeline.parser import _validate_json  # noqa: E402
from school_email_pipeline.routing import RoutingPolicy, route_email  # noqa: E402
from school_email_pipeline.settings import PipelineSettings  # noqa: E402
from school_email_pipeline.storage import EmailStore  # noqa: E402
from school_email_pipeline.telegram import format_telegram_message  # noqa: E402


def _settings(tmp_path: Path, *, enable_gliner: bool = False) -> PipelineSettings:
    return PipelineSettings(
        database_url=f"sqlite:///{tmp_path / 'emails.sqlite3'}",
        policy_config_path=tmp_path / "policy.json",
        openai_api_key="",
        openai_model="test",
        openai_timeout_seconds=1,
        enable_gliner=enable_gliner,
        enable_pioneer=False,
        pioneer_api_key="",
        pioneer_model_id="gliner2-large",
        pioneer_base_url="https://api.pioneer.ai",
        pioneer_threshold=0.4,
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
                "applies_to": "2nd grader",
                "confidence": 0.9,
            }
        ],
        "calendar_items": [],
        "telegram_summary": "Field trip form is due soon.",
        "why_it_matters": "This affects the 2nd grader's field trip.",
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


def test_gliner_failure_falls_back(monkeypatch, tmp_path: Path) -> None:
    def fail() -> object:
        raise RuntimeError("missing model")

    monkeypatch.setattr("school_email_pipeline.entities._load_gliner_model", fail)

    assert (
        extract_entities("field trip Friday", _settings(tmp_path, enable_gliner=True))
        == []
    )


def test_openai_json_validation_accepts_strict_schema() -> None:
    parsed = _validate_json(_parsed().model_dump_json())

    assert parsed.email_type == "action_required"
    assert parsed.audience.applies_to_second_grader is True


def test_routing_policy_immediate_for_action_deadline() -> None:
    policy = RoutingPolicy(
        immediate_importance={"high", "urgent"},
        deadline_within_days=7,
        high_calendar_confidence=0.8,
        parser_confidence_review_threshold=0.7,
        audience_confidence_review_threshold=0.7,
        daily_digest_email_types={"newsletter"},
        daily_digest_importance={"low"},
        relevant_audiences={"2nd grader", "5th grader", "both"},
        telegram_for_human_review=True,
    )

    decision = route_email(
        _parsed(),
        [],
        policy,
        now=datetime(2026, 5, 23, tzinfo=UTC),
    )

    assert "send_immediate" in decision.actions
    assert decision.relevance == "2nd grader"


def test_telegram_formatting_is_short_and_parent_facing() -> None:
    routing = route_email(
        _parsed(),
        [],
        RoutingPolicy(
            immediate_importance={"high", "urgent"},
            deadline_within_days=7,
            high_calendar_confidence=0.8,
            parser_confidence_review_threshold=0.7,
            audience_confidence_review_threshold=0.7,
            daily_digest_email_types={"newsletter"},
            daily_digest_importance={"low"},
            relevant_audiences={"2nd grader", "5th grader", "both"},
            telegram_for_human_review=True,
        ),
        now=datetime(2026, 5, 23, tzinfo=UTC),
    )

    text = format_telegram_message("Field trip form", _parsed(), routing)

    assert "School: Field trip form" in text
    assert "Relevant to: 2nd grader" in text
    assert "Action: Return the field trip form" in text


def test_feedback_logging(tmp_path: Path) -> None:
    from school_email_pipeline.models import PipelineEmail

    store = EmailStore(f"sqlite:///{tmp_path / 'emails.sqlite3'}")
    email_id, _ = store.create_email_if_new(
        PipelineEmail(message_id="<feedback@school>", subject="test")
    )

    store.log_feedback(
        FeedbackEvent(
            email_id=email_id,
            telegram_message_id=123,
            feedback_type="useful",
            payload={"callback": "ok"},
        )
    )

    with store._connect() as conn:
        row = conn.execute(
            "SELECT feedback_json FROM emails WHERE id = ?", (email_id,)
        ).fetchone()

    feedback = json.loads(row["feedback_json"])
    assert feedback[0]["feedback_type"] == "useful"
