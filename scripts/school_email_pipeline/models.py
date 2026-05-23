from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class ActionItem(BaseModel):
    action: str
    deadline: str | None = None
    applies_to: AppliesTo
    confidence: float = Field(ge=0.0, le=1.0)


class CalendarItem(BaseModel):
    title: str
    start: str | None = None
    end: str | None = None
    date_text: str
    time_text: str
    location: str | None = None
    applies_to: AppliesTo
    confidence: float = Field(ge=0.0, le=1.0)


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
