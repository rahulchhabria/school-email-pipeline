from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .models import RouteAction, RoutingDecision, StructuredParseResult


@dataclass(frozen=True)
class RoutingPolicy:
    immediate_importance: set[str]
    deadline_within_days: int
    high_calendar_confidence: float
    parser_confidence_review_threshold: float
    audience_confidence_review_threshold: float
    daily_digest_email_types: set[str]
    daily_digest_importance: set[str]
    relevant_audiences: set[str]
    telegram_for_human_review: bool


def load_policy(path: Path) -> RoutingPolicy:
    data = json.loads(path.read_text()) if path.exists() else {}
    return RoutingPolicy(
        immediate_importance=set(data.get("immediate_importance", ["high", "urgent"])),
        deadline_within_days=int(data.get("deadline_within_days", 7)),
        high_calendar_confidence=float(data.get("high_calendar_confidence", 0.8)),
        parser_confidence_review_threshold=float(
            data.get("parser_confidence_review_threshold", 0.7)
        ),
        audience_confidence_review_threshold=float(
            data.get("audience_confidence_review_threshold", 0.7)
        ),
        daily_digest_email_types=set(
            data.get("daily_digest_email_types", ["newsletter"])
        ),
        daily_digest_importance=set(data.get("daily_digest_importance", ["low"])),
        relevant_audiences=set(
            data.get("relevant_audiences", ["3rd grader", "6th grader", "both"])
        ),
        telegram_for_human_review=bool(data.get("telegram_for_human_review", True)),
    )


def route_email(
    parsed: StructuredParseResult,
    policy: RoutingPolicy,
    *,
    now: datetime | None = None,
) -> RoutingDecision:
    now = now or datetime.now(UTC)
    actions: list[RouteAction] = []
    reasons: list[str] = []
    relevance = _relevance(parsed)
    relevant = (
        relevance in policy.relevant_audiences
        or parsed.audience.applies_to_whole_school
    )

    if parsed.confidence < policy.parser_confidence_review_threshold:
        actions.append("needs_human_review")
        reasons.append("parser confidence below threshold")
    if parsed.audience.confidence < policy.audience_confidence_review_threshold:
        actions.append("needs_human_review")
        reasons.append("audience confidence below threshold")
    if parsed.needs_human_review:
        actions.append("needs_human_review")
        reasons.append("parser requested human review")

    if parsed.importance == "ignore" or (
        not relevant and "needs_human_review" not in actions
    ):
        actions.append("ignore")
        reasons.append(
            "importance is ignore"
            if parsed.importance == "ignore"
            else "not relevant to configured grades"
        )
        return RoutingDecision(
            actions=_unique(actions),
            reasons=reasons,
            relevance=relevance,
            processing_status="ignored",
        )
    if _important_date_unresolved(parsed):
        actions.append("needs_human_review")
        reasons.append("important date exists but was not normalized")
    if _ambiguous_action(parsed):
        actions.append("needs_human_review")
        reasons.append("action required is ambiguous")

    if parsed.importance in policy.immediate_importance:
        actions.append("send_immediate")
        reasons.append("high or urgent importance")
    if parsed.parent_action_required:
        actions.append("send_immediate")
        reasons.append("parent action required")
    if _deadline_within(parsed, policy.deadline_within_days, now=now):
        actions.append("send_immediate")
        reasons.append(f"deadline within {policy.deadline_within_days} days")
    if any(
        item.confidence >= policy.high_calendar_confidence
        for item in parsed.calendar_items
    ):
        actions.append("send_immediate")
        actions.append("calendar_candidate")
        reasons.append("high-confidence calendar item")
    elif parsed.calendar_items:
        actions.append("calendar_candidate")
        reasons.append("calendar item candidate")

    if (
        parsed.email_type in policy.daily_digest_email_types
        or parsed.importance in policy.daily_digest_importance
    ) and not parsed.parent_action_required:
        actions.append("add_to_daily_digest")
        reasons.append("low-priority digest candidate")

    if not actions:
        actions.append("add_to_daily_digest")
        reasons.append("default non-urgent relevant school email")

    send_now = "send_immediate" in actions or (
        policy.telegram_for_human_review and "needs_human_review" in actions
    )
    status = "telegram_ready" if send_now else "queued_digest"
    return RoutingDecision(
        actions=_unique(actions),
        reasons=reasons,
        relevance=relevance,
        send_telegram_now=send_now,
        processing_status=status,
    )


def _relevance(parsed: StructuredParseResult) -> str:
    second = parsed.audience.applies_to_second_grader
    fifth = parsed.audience.applies_to_fifth_grader
    if second and fifth:
        return "both"
    if second:
        return "3rd grader"
    if fifth:
        return "6th grader"
    if parsed.audience.applies_to_whole_school:
        return "both"
    return "unknown"


def _deadline_within(
    parsed: StructuredParseResult, days: int, *, now: datetime
) -> bool:
    today = now.date()
    for item in parsed.action_items:
        deadline = _parse_date(item.deadline)
        if deadline and today <= deadline <= date.fromordinal(today.toordinal() + days):
            return True
    return False


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _important_date_unresolved(parsed: StructuredParseResult) -> bool:
    if not (parsed.parent_action_required or parsed.calendar_items):
        return False
    has_action_deadline = any(item.deadline for item in parsed.action_items)
    has_calendar_date = any(
        (item.start or item.date_text) for item in parsed.calendar_items
    )
    if parsed.parent_action_required and not has_action_deadline:
        return True
    if parsed.calendar_items and not has_calendar_date:
        return True
    return False


def _ambiguous_action(parsed: StructuredParseResult) -> bool:
    if not parsed.parent_action_required:
        return False
    if not parsed.action_items:
        return True
    return any(
        item.confidence < 0.5 or not item.action.strip() for item in parsed.action_items
    )


def _unique(actions: list[RouteAction]) -> list[RouteAction]:
    seen: set[str] = set()
    unique: list[RouteAction] = []
    for action in actions:
        if action in seen:
            continue
        seen.add(action)
        unique.append(action)
    return unique
