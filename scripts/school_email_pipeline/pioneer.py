"""Pioneer (Fastino) hosted-inference path for the structured email parser.

Replaces the OpenAI-based parser when ENABLE_PIONEER=1. Issues a single
POST /inference call to https://api.pioneer.ai with a schema that asks
GLiNER2-Large for entities, classifications, and structured arrays in
one round-trip, then maps the response into StructuredParseResult.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from .entities import GLINER_LABELS
from .models import (
    ActionItem,
    Audience,
    CalendarItem,
    ExtractedEntity,
    PipelineEmail,
    StructuredParseResult,
)
from .pioneer_client import (
    PioneerAuthError,
    PioneerClient,
    PioneerError,
    PioneerRateLimited,
)
from .settings import PipelineSettings


logger = logging.getLogger(__name__)


class PioneerUnavailable(RuntimeError):
    pass


CLASSIFICATION_TASKS: list[dict[str, Any]] = [
    {
        "task": "email_type",
        "labels": [
            "announcement",
            "action_required",
            "calendar_event",
            "newsletter",
            "emergency",
            "fundraising",
            "sports",
            "lunch",
            "other",
        ],
    },
    {
        "task": "importance",
        "labels": ["ignore", "low", "medium", "high", "urgent"],
    },
    {
        "task": "audience",
        "labels": [
            "2nd grader",
            "5th grader",
            "both",
            "whole school",
            "unknown",
        ],
    },
    {
        "task": "parent_action_required",
        "labels": ["yes", "no"],
    },
]

STRUCTURE_DEFINITIONS: dict[str, Any] = {
    "action_items": {
        "fields": {
            "action": "string",
            "deadline": "string",
        }
    },
    "calendar_items": {
        "fields": {
            "title": "string",
            "date": "string",
            "time": "string",
            "location": "string",
        }
    },
}


def classify_with_pioneer(
    email: PipelineEmail,
    cleaned_body: str,
    entities: list[ExtractedEntity],
    settings: PipelineSettings,
) -> tuple[StructuredParseResult, str | None]:
    """Run a single Pioneer /inference call and return the parsed result.

    Returns (parse_result, inference_id). The inference_id is what the
    feedback API needs later when the user clicks Useful / Not useful.
    """
    if not settings.enable_pioneer:
        raise PioneerUnavailable("ENABLE_PIONEER is false")
    if not settings.pioneer_api_key:
        raise PioneerUnavailable("PIONEER_API_KEY is not configured")

    text = _build_input_text(email, cleaned_body)
    schema = _build_schema()
    client = PioneerClient(
        settings.pioneer_api_key,
        base_url=settings.pioneer_base_url,
        timeout_seconds=settings.pioneer_timeout_seconds,
    )

    try:
        response = client.infer(
            model_id=settings.pioneer_model_id,
            text=text,
            schema=schema,
            threshold=settings.pioneer_threshold,
        )
    except PioneerAuthError as exc:
        raise PioneerUnavailable(f"pioneer auth: {exc}") from exc
    except PioneerRateLimited as exc:
        raise PioneerUnavailable(f"pioneer rate limited: {exc}") from exc
    except PioneerError as exc:
        raise PioneerUnavailable(f"pioneer error: {exc}") from exc

    inference_id = _extract_inference_id(response)
    parsed = _map_response(response, email=email, gliner_entities=entities)
    return parsed, inference_id


def _build_input_text(email: PipelineEmail, cleaned_body: str) -> str:
    sender = email.sender or "(unknown)"
    subject = email.subject or "(no subject)"
    received = email.received_at or "(unknown)"
    body = cleaned_body or "(empty body)"
    return (
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Received: {received}\n"
        f"---\n"
        f"{body}"
    )


def _build_schema() -> dict[str, Any]:
    return {
        "entities": GLINER_LABELS,
        "classifications": CLASSIFICATION_TASKS,
        "structures": STRUCTURE_DEFINITIONS,
    }


def _extract_inference_id(response: dict[str, Any]) -> str | None:
    for key in ("inference_id", "id"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    metadata = response.get("metadata")
    if isinstance(metadata, dict):
        for key in ("inference_id", "id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _map_response(
    response: dict[str, Any],
    *,
    email: PipelineEmail,
    gliner_entities: list[ExtractedEntity],
) -> StructuredParseResult:
    classifications = _extract_classifications(response)
    structures = _extract_structures(response)
    pioneer_entities = _extract_response_entities(response)
    all_entities = pioneer_entities or gliner_entities

    audience_label, audience_conf = _pick_label(classifications.get("audience"))
    audience = _audience_from_label(audience_label, audience_conf, email.subject)

    email_type, email_type_conf = _pick_label(
        classifications.get("email_type"), default="other"
    )
    importance, importance_conf = _pick_label(
        classifications.get("importance"), default="low"
    )
    action_label, action_conf = _pick_label(
        classifications.get("parent_action_required"), default="no"
    )
    parent_action_required = action_label == "yes"

    action_items = _build_action_items(
        structures.get("action_items"), default_confidence=action_conf
    )
    calendar_items = _build_calendar_items(
        structures.get("calendar_items"), default_confidence=email_type_conf
    )

    if not action_items and parent_action_required and all_entities:
        action_items = _action_items_from_entities(all_entities)
    if not calendar_items and all_entities:
        calendar_items = _calendar_items_from_entities(all_entities)

    overall_confidence = _weighted_average(
        [audience_conf, email_type_conf, importance_conf, action_conf]
    )
    needs_human_review = overall_confidence < 0.6 or audience_conf < 0.5

    telegram_summary = _short(
        email.subject or (calendar_items[0].title if calendar_items else "School update"),
        260,
    )
    why_it_matters = _why_it_matters(
        importance, parent_action_required, audience.confidence
    )

    return StructuredParseResult(
        email_type=email_type,  # type: ignore[arg-type]
        audience=audience,
        importance=importance,  # type: ignore[arg-type]
        parent_action_required=parent_action_required,
        action_items=action_items,
        calendar_items=calendar_items,
        telegram_summary=telegram_summary,
        why_it_matters=why_it_matters,
        confidence=round(overall_confidence, 3),
        needs_human_review=needs_human_review,
    )


def _extract_classifications(
    response: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Normalize classification output across possible Pioneer shapes."""
    out: dict[str, list[dict[str, Any]]] = {}
    raw = response.get("classifications") or response.get("classification")
    if isinstance(raw, dict):
        for task, value in raw.items():
            out[str(task)] = _coerce_label_list(value)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            task = str(item.get("task") or item.get("name") or "")
            if not task:
                continue
            out[task] = _coerce_label_list(
                item.get("predictions")
                or item.get("results")
                or item.get("labels")
                or item.get("scores")
            )
    return out


def _coerce_label_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for entry in value:
            if isinstance(entry, dict):
                label = str(entry.get("label") or entry.get("name") or "").strip()
                score = entry.get("score")
                if score is None:
                    score = entry.get("confidence")
                try:
                    score_f = float(score) if score is not None else 0.0
                except (TypeError, ValueError):
                    score_f = 0.0
                if label:
                    result.append({"label": label, "score": score_f})
            elif isinstance(entry, str):
                result.append({"label": entry, "score": 0.0})
        return result
    if isinstance(value, dict):
        return [
            {
                "label": str(label),
                "score": float(score) if isinstance(score, (int, float)) else 0.0,
            }
            for label, score in value.items()
        ]
    if isinstance(value, str):
        return [{"label": value, "score": 0.0}]
    return []


def _pick_label(
    candidates: list[dict[str, Any]] | None,
    *,
    default: str = "unknown",
) -> tuple[str, float]:
    if not candidates:
        return default, 0.0
    best = max(candidates, key=lambda item: float(item.get("score") or 0.0))
    label = str(best.get("label") or default).strip() or default
    try:
        score = float(best.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return label, max(0.0, min(1.0, score))


def _extract_structures(response: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    raw = response.get("structures") or response.get("structured")
    if isinstance(raw, dict):
        for name, value in raw.items():
            if isinstance(value, list):
                out[str(name)] = [item for item in value if isinstance(item, dict)]
            elif isinstance(value, dict):
                out[str(name)] = [value]
    return out


def _extract_response_entities(response: dict[str, Any]) -> list[ExtractedEntity]:
    raw = response.get("entities")
    if not isinstance(raw, list):
        return []
    out: list[ExtractedEntity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        label = str(item.get("label") or item.get("type") or "").strip()
        if not text or not label:
            continue
        try:
            confidence = float(item.get("score") or item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        out.append(
            ExtractedEntity(
                label=label,
                text=text,
                start=item.get("start") if isinstance(item.get("start"), int) else None,
                end=item.get("end") if isinstance(item.get("end"), int) else None,
                confidence=max(0.0, min(1.0, confidence)),
            )
        )
    return out


def _audience_from_label(
    label: str, confidence: float, subject: str
) -> Audience:
    second = "2nd" in label or "second" in label or label == "both"
    fifth = "5th" in label or "fifth" in label or label == "both"
    whole = "whole school" in label or label == "all"
    subject_lower = (subject or "").lower()
    if not second and "2nd" in subject_lower:
        second = True
    if not fifth and "5th" in subject_lower:
        fifth = True
    return Audience(
        applies_to_second_grader=second,
        applies_to_fifth_grader=fifth,
        applies_to_whole_school=whole,
        confidence=confidence,
    )


def _build_action_items(
    raw: list[dict[str, Any]] | None, *, default_confidence: float
) -> list[ActionItem]:
    if not raw:
        return []
    out: list[ActionItem] = []
    for entry in raw:
        action = str(entry.get("action") or entry.get("text") or "").strip()
        if not action:
            continue
        deadline = _normalize_deadline(entry.get("deadline"))
        applies_to = _normalize_applies_to(entry.get("applies_to"))
        confidence = _coerce_confidence(entry.get("confidence"), default_confidence)
        out.append(
            ActionItem(
                action=action,
                deadline=deadline,
                applies_to=applies_to,
                confidence=confidence,
            )
        )
    return out


def _build_calendar_items(
    raw: list[dict[str, Any]] | None, *, default_confidence: float
) -> list[CalendarItem]:
    if not raw:
        return []
    out: list[CalendarItem] = []
    for entry in raw:
        title = str(entry.get("title") or entry.get("event") or "").strip()
        date_text = str(entry.get("date") or entry.get("date_text") or "").strip()
        time_text = str(entry.get("time") or entry.get("time_text") or "").strip()
        if not title and not date_text:
            continue
        location_value = entry.get("location")
        location = (
            str(location_value).strip() or None
            if location_value is not None
            else None
        )
        start = _combine_iso(date_text, time_text)
        applies_to = _normalize_applies_to(entry.get("applies_to"))
        confidence = _coerce_confidence(entry.get("confidence"), default_confidence)
        out.append(
            CalendarItem(
                title=title or "School event",
                start=start,
                end=None,
                date_text=date_text,
                time_text=time_text,
                location=location,
                applies_to=applies_to,
                confidence=confidence,
            )
        )
    return out


def _action_items_from_entities(entities: list[ExtractedEntity]) -> list[ActionItem]:
    actions: list[str] = []
    deadline: str | None = None
    for entity in entities:
        if entity.label in {"action required", "form", "payment"}:
            actions.append(entity.text)
        if entity.label == "deadline" and not deadline:
            deadline = _normalize_deadline(entity.text)
    if not actions:
        return []
    return [
        ActionItem(
            action=actions[0],
            deadline=deadline,
            applies_to="unknown",
            confidence=0.4,
        )
    ]


def _calendar_items_from_entities(
    entities: list[ExtractedEntity],
) -> list[CalendarItem]:
    title = ""
    date_text = ""
    time_text = ""
    location: str | None = None
    for entity in entities:
        if entity.label in {"school event", "field trip", "performance", "conference"}:
            title = title or entity.text
        elif entity.label == "date":
            date_text = date_text or entity.text
        elif entity.label == "time":
            time_text = time_text or entity.text
        elif entity.label == "location":
            location = location or entity.text
    if not title and not date_text:
        return []
    return [
        CalendarItem(
            title=title or "School event",
            start=_combine_iso(date_text, time_text),
            end=None,
            date_text=date_text,
            time_text=time_text,
            location=location,
            applies_to="unknown",
            confidence=0.3,
        )
    ]


_DATE_PATTERNS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d",
    "%b %d",
]
_TIME_PATTERNS = ["%I:%M %p", "%I%p", "%H:%M"]


def _normalize_deadline(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse_loose_date(text)
    if parsed is None:
        return None
    return parsed.isoformat()


def _combine_iso(date_text: str, time_text: str) -> str | None:
    parsed_date = _parse_loose_date(date_text) if date_text else None
    if parsed_date is None:
        return None
    parsed_time = _parse_loose_time(time_text) if time_text else None
    if parsed_time is None:
        return parsed_date.isoformat()
    combined = datetime.combine(parsed_date, parsed_time)
    return combined.isoformat()


def _parse_loose_date(text: str) -> date | None:
    cleaned = re.sub(r"\s+", " ", text.replace(",", "")).strip()
    if not cleaned:
        return None
    for pattern in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
        if parsed.year == 1900:
            parsed = parsed.replace(year=datetime.now().year)
        return parsed
    return None


def _parse_loose_time(text: str) -> Any | None:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return None
    for pattern in _TIME_PATTERNS:
        try:
            return datetime.strptime(cleaned.upper(), pattern).time()
        except ValueError:
            continue
    return None


def _normalize_applies_to(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    candidate = value.strip().lower()
    if candidate in {"2nd grader", "5th grader", "both", "unknown"}:
        return candidate
    if "2nd" in candidate or "second" in candidate:
        return "2nd grader"
    if "5th" in candidate or "fifth" in candidate:
        return "5th grader"
    if "both" in candidate or "all" in candidate:
        return "both"
    return "unknown"


def _coerce_confidence(value: Any, default: float) -> float:
    try:
        if value is None:
            return max(0.0, min(1.0, default))
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return max(0.0, min(1.0, default))


def _weighted_average(values: list[float]) -> float:
    cleaned = [v for v in values if isinstance(v, float)]
    if not cleaned:
        return 0.0
    return sum(cleaned) / len(cleaned)


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def _why_it_matters(
    importance: str,
    parent_action_required: bool,
    audience_confidence: float,
) -> str:
    parts: list[str] = []
    if importance in {"high", "urgent"}:
        parts.append(f"Importance: {importance}")
    if parent_action_required:
        parts.append("Parent action required")
    if audience_confidence < 0.5:
        parts.append("Audience uncertain")
    if not parts:
        parts.append("Routine school update")
    return "; ".join(parts)
