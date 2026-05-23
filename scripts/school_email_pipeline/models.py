from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_EMAIL_TYPE_FALLBACK: dict[str, str] = {
    "action": "action_required",
    "action item": "action_required",
    "calendar": "calendar_event",
    "event": "calendar_event",
    "news": "newsletter",
    "fundraiser": "fundraising",
    "sport": "sports",
    "urgent notice": "emergency",
    "alert": "emergency",
}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

EmailType = Literal[
    "announcement",
    "action_required",
    "calendar_event",
    "newsletter",
    "emergency",
    "fundraising",
    "sports",
    "lunch",
    "other",
]
Importance = Literal["ignore", "low", "medium", "high", "urgent"]
AppliesTo = Literal["2nd grader", "5th grader", "both", "unknown"]
RouteAction = Literal[
    "send_immediate",
    "add_to_daily_digest",
    "ignore",
    "needs_human_review",
    "calendar_candidate",
]


class PipelineEmail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: str
    sender: str = ""
    recipient: str = ""
    subject: str = ""
    received_at: str = ""
    text_body: str = ""
    html_body: str = ""
    provider: str = "generic"
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ExtractedEntity(BaseModel):
    label: str
    text: str
    start: int | None = None
    end: int | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Audience(BaseModel):
    applies_to_second_grader: bool
    applies_to_fifth_grader: bool
    applies_to_whole_school: bool
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return 0.0
        return v


class ActionItem(BaseModel):
    action: str
    deadline: str | None = None
    applies_to: AppliesTo
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("action", mode="after")
    @classmethod
    def _strip_action(cls, v: str) -> str:
        return v.strip()

    @field_validator("deadline", mode="after")
    @classmethod
    def _normalize_deadline(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if _ISO_DATETIME_RE.match(v):
            return v[:10]
        if _ISO_DATE_RE.match(v):
            return v[:10]
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return 0.0
        return v


class CalendarItem(BaseModel):
    title: str
    start: str | None = None
    end: str | None = None
    date_text: str
    time_text: str
    location: str | None = None
    applies_to: AppliesTo
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("title", mode="after")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return v.strip() or "School event"

    @field_validator("start", mode="after")
    @classmethod
    def _normalize_start(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if _ISO_DATETIME_RE.match(v):
            return v
        if _ISO_DATE_RE.match(v):
            return v
        return v

    @field_validator("end", mode="after")
    @classmethod
    def _normalize_end(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if _ISO_DATETIME_RE.match(v):
            return v
        if _ISO_DATE_RE.match(v):
            return v
        return v

    @field_validator("location", mode="after")
    @classmethod
    def _strip_location(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return 0.0
        return v


class StructuredParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_type: EmailType
    audience: Audience
    importance: Importance
    parent_action_required: bool
    action_items: list[ActionItem]
    calendar_items: list[CalendarItem]
    telegram_summary: str
    why_it_matters: str
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool

    @field_validator("email_type", mode="before")
    @classmethod
    def _coerce_email_type(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return "other"
        normalized = v.strip().lower()
        if normalized in {"announcement", "action_required", "calendar_event",
                          "newsletter", "emergency", "fundraising", "sports",
                          "lunch", "other"}:
            return normalized
        return _EMAIL_TYPE_FALLBACK.get(normalized, "other")

    @field_validator("importance", mode="before")
    @classmethod
    def _coerce_importance(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return "low"
        normalized = v.strip().lower()
        if normalized in {"ignore", "low", "medium", "high", "urgent"}:
            return normalized
        if normalized in {"critical", "asap"}:
            return "urgent"
        if normalized in {"important", "significant"}:
            return "high"
        return "low"

    @field_validator("action_items", mode="after")
    @classmethod
    def _filter_empty_actions(cls, v: list[ActionItem]) -> list[ActionItem]:
        return [item for item in v if item.action]

    @field_validator("telegram_summary", mode="after")
    @classmethod
    def _strip_summary(cls, v: str) -> str:
        return v.strip() or "School update"

    @field_validator("why_it_matters", mode="after")
    @classmethod
    def _strip_why(cls, v: str) -> str:
        return v.strip() or "Routine school update"

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return 0.0
        return v


class RoutingDecision(BaseModel):
    actions: list[RouteAction]
    reasons: list[str] = Field(default_factory=list)
    relevance: AppliesTo
    send_telegram_now: bool = False
    processing_status: str = "processed"


class TelegramDelivery(BaseModel):
    sent: bool = False
    message_id: int | None = None
    text: str = ""


class PipelineResult(BaseModel):
    status: str
    message_id: str
    email_id: int | None = None
    routing: RoutingDecision | None = None
    telegram: TelegramDelivery | None = None


class FeedbackEvent(BaseModel):
    email_id: int | None = None
    telegram_message_id: int | None = None
    feedback_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
