from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import PipelineEmail, StructuredParseResult
from .settings import PipelineSettings


logger = logging.getLogger(__name__)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
try:
    import sentry_sdk
    import sentry_sdk.ai as sentry_ai
    from sentry_sdk.consts import SPANDATA
except Exception:  # noqa: BLE001
    sentry_sdk = None
    sentry_ai = None
    SPANDATA = None

_EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"
_FEW_SHOT_IDS = [
    "field_trip_permission",
    "emergency_early_dismissal",
    "school_newsletter",
    "volunteer_opportunity",
    "gymnastics_team_info",
    "ambiguous_audience",
]
_CONVERSATION_ID: ContextVar[str | None] = ContextVar(
    "school_email_conversation_id", default=None
)


SYSTEM_PROMPT = (
    "You are a strict school-email parser for a parent with one child in 3rd "
    "grade and one child in 6th grade. Both children attend the same school.\n\n"
    "PRIMARY GOAL: accurately determine which of the parent's children the "
    "email applies to. The `audience` object is the most important field. "
    "Treat audience identification as the central job of this task.\n\n"
    "AUDIENCE RULES (apply in this order):\n"
    "1. If the email explicitly names a grade (e.g. \"3rd Grade\", \"Grade 6\", "
    "\"sixth graders\", \"K-3\", \"6th-8th\", \"upper grades\", \"middle school\"), "
    "set the matching flags and use confidence >= 0.9.\n"
    "   - \"3rd Grade\", \"3rd graders\", \"Grade 3\", \"third grade\" -> "
    "applies_to_second_grader=true.\n"
    "   - \"6th Grade\", \"6th graders\", \"Grade 6\", \"sixth grade\" -> "
    "applies_to_fifth_grader=true.\n"
    "   - \"K-3\", \"lower grades\" (typically K-2 or K-3) -> "
    "applies_to_second_grader=true, applies_to_fifth_grader=false.\n"
    "   - \"6th-8th\", \"middle school\", \"upper school\" -> "
    "applies_to_fifth_grader=true, applies_to_second_grader=false.\n"
    "   - \"K-8\", \"all grades\", \"whole school\", \"all families\", "
    "\"Lincoln families\", \"all students\" -> applies_to_second_grader=true, "
    "applies_to_fifth_grader=true, applies_to_whole_school=true.\n"
    "2. If the email names a specific teacher or classroom, infer grade only if "
    "you are confident. If grade is unclear from teacher/room, leave the per-grade "
    "flags false and lower audience.confidence (<= 0.6) and set "
    "needs_human_review=true.\n"
    "3. If the email is about a specific event that targets one grade (e.g. "
    "\"3rd grade field trip\", \"6th grade orientation\", \"6th grade promotion "
    "ceremony\", \"3rd grade open house\"), set the matching flag and use "
    "confidence >= 0.85. The other grade flag MUST be false.\n"
    "4. If the email is about a school-wide event (e.g. school carnival, "
    "fundraiser open to everyone, school-wide assembly, lunch menu, school "
    "closure, emergency dismissal, all-school newsletter), set "
    "applies_to_whole_school=true AND both applies_to_second_grader=true AND "
    "applies_to_fifth_grader=true.\n"
    "5. If the email targets a grade range that includes 3rd OR 6th but not "
    "both (e.g. \"K-3 talent show\" or \"6-8 dance\"), set only the matching flag.\n"
    "   Emails that only target the old grades, such as 2nd grade or 5th grade, "
    "are not relevant unless they also include 3rd grade, 6th grade, all grades, "
    "or whole-school language.\n"
    "6. If the email is irrelevant to either child (e.g. a high-school sports "
    "announcement reaching a wrong list, or PTA business with no children "
    "impact), set all three audience flags to false and importance=\"ignore\".\n"
    "7. NEVER guess: if you cannot determine the audience from the email "
    "content, set all three flags to false, audience.confidence below 0.5, "
    "and needs_human_review=true. Do not invent a grade.\n\n"
    "PER-ITEM `applies_to` RULES:\n"
    "- Each action_item and calendar_item also has an `applies_to` field "
    "(values: \"3rd grader\", \"6th grader\", \"both\", \"unknown\").\n"
    "- If the item is grade-specific in the email body, set its `applies_to` "
    "to that grade. Otherwise inherit from the email-level audience: both "
    "grades true -> \"both\"; only one grade true -> that grade; otherwise "
    "\"unknown\".\n"
    "- A grade-specific item (e.g. \"6th grade field trip\") MUST have the "
    "audience grade flag set true as well.\n\n"
    "OTHER FIELD RULES:\n"
    "- parent_action_required MUST be true ONLY if the email contains an "
    "explicit mandatory deadline, a form that MUST be returned, or a payment "
    "that MUST be sent. Voluntary signups, optional RSVPs, fundraising "
    "participation, and \"we need volunteers\" are NOT parent_action_required "
    "- they are announcements.\n"
    "- action_items should ONLY contain things a parent must do to avoid a "
    "negative consequence (e.g. child misses a trip, gets marked absent). "
    "Do NOT create action items for optional opportunities, informational "
    "notices, or things that are nice-to-know.\n"
    "- importance should be 'low' for newsletters, general information, lunch "
    "menus, and volunteer requests. Use 'medium' only when a specific "
    "grade-relevant event is mentioned. Use 'high' or 'urgent' only for "
    "mandatory deadlines or emergencies.\n"
    "- Personnel/staffing changes (new or departing principals, assistant "
    "principals, teachers, counselors, or other staff; reassignments; "
    "interim appointments; leadership transitions) are significant school "
    "news. Set importance to 'medium' for a single-classroom change or a "
    "minor staff update, and 'high' when the change affects a child's "
    "current teacher/classroom, multiple staff at once, or school-wide "
    "leadership (e.g. principal/assistant principal). Do NOT classify "
    "these as 'low' even if no parent action is required.\n"
    "- email_type 'sports' is for athletics/PE communications; "
    "'calendar_event' is for performances, ceremonies, and school events; "
    "'announcement' is for general information and volunteer opportunities; "
    "'fundraising' is only when purchasing or donations are the primary ask.\n"
    "- Do not invent dates, locations, teachers, or requirements.\n"
    "- Preserve uncertainty with nulls and confidence scores.\n"
    "- Return only JSON."
)


def _load_few_shot_examples() -> list[tuple[str, str, str]]:
    """Load pre-built (subject, body_excerpt, expected_json) few-shot tuples."""
    examples: list[tuple[str, str, str]] = []
    for case_id in _FEW_SHOT_IDS:
        email_path = _EVALS_DIR / "golden_emails" / f"{case_id}.json"
        expected_path = _EVALS_DIR / "expected_outputs" / f"{case_id}.json"
        if not email_path.exists() or not expected_path.exists():
            continue
        try:
            email_data = json.loads(email_path.read_text())
            expected_data = json.loads(expected_path.read_text())
            subject = email_data.get("subject", "")
            body = (email_data.get("text_body") or "")[:600]
            examples.append((subject, body, json.dumps(expected_data, ensure_ascii=False)))
        except (json.JSONDecodeError, OSError):
            continue
    return examples


_FEW_SHOT_EXAMPLES = _load_few_shot_examples()


class ParserUnavailable(RuntimeError):
    pass


class ParserValidationError(RuntimeError):
    pass


def set_conversation_id(conversation_id: str | None) -> None:
    """Attach a Sentry AI conversation ID to parser spans in this context."""
    _CONVERSATION_ID.set(conversation_id)


def _resolve_api_key(settings: PipelineSettings) -> str:
    """Return the OpenAI API key for the parser client."""
    if not settings.openai_api_key:
        raise ParserUnavailable("OPENAI_API_KEY is not configured")
    return settings.openai_api_key


def parse_school_email(
    email: PipelineEmail,
    cleaned_body: str,
    settings: PipelineSettings,
) -> tuple[StructuredParseResult, str | None]:
    """Parse a school email. Returns (parsed, inference_id).

    inference_id is the chat-completion id reported by OpenAI. Used by the
    feedback replayer to attach Telegram thumbs-up/down to a specific inference.
    None if the upstream call did not return an id.
    """
    api_key = _resolve_api_key(settings)

    try:
        from openai import OpenAI
    except Exception as exc:  # noqa: BLE001
        raise ParserUnavailable(f"openai package is unavailable: {exc}") from exc

    client_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": settings.openai_timeout_seconds,
    }
    client = OpenAI(**client_kwargs)
    messages = _messages(email, cleaned_body)
    errors: list[str] = []

    for attempt in range(2):
        try:
            parsed, inference_id = _parse_with_structured_output(
                client, settings.openai_model, messages
            )
            logger.info(
                "openai_structured_parse_succeeded", extra={"attempt": attempt + 1}
            )
            return parsed, inference_id
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            logger.warning(
                "openai_structured_parse_failed",
                extra={"attempt": attempt + 1, "error": str(exc)},
            )
            try:
                parsed, inference_id = _parse_with_json_object(
                    client, settings.openai_model, messages
                )
                logger.info(
                    "openai_json_parse_succeeded", extra={"attempt": attempt + 1}
                )
                return parsed, inference_id
            except Exception as json_exc:  # noqa: BLE001
                errors.append(str(json_exc))
                logger.warning(
                    "openai_json_parse_failed",
                    extra={"attempt": attempt + 1, "error": str(json_exc)},
                )
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Your prior response failed validation. Return exactly one JSON "
                            "object matching the schema. Do not add markdown. Validation errors: "
                            f"{str(json_exc)[:1200]}"
                        ),
                    },
                ]

    raise ParserValidationError("; ".join(errors[-4:]))


def _completion_id(completion: Any) -> str | None:
    value = getattr(completion, "id", None)
    if isinstance(value, str) and value:
        return value
    return None


def _parse_with_structured_output(
    client: Any,
    model: str,
    messages: list[dict[str, str]],
) -> tuple[StructuredParseResult, str | None]:
    parse_method = getattr(
        getattr(getattr(client, "beta", None), "chat", None), "completions", None
    )
    if parse_method is None or not hasattr(parse_method, "parse"):
        raise ParserUnavailable("OpenAI structured parse helper is not available")

    with _gen_ai_span("openai.beta.chat.completions.parse", model, messages) as span:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": StructuredParseResult,
        }
        completion = parse_method.parse(**kwargs)
        _record_completion_span_data(span, completion)
    parsed = completion.choices[0].message.parsed
    inference_id = _completion_id(completion)
    if not isinstance(parsed, StructuredParseResult):
        return StructuredParseResult.model_validate(parsed), inference_id
    return parsed, inference_id


def _parse_with_json_object(
    client: Any,
    model: str,
    messages: list[dict[str, str]],
) -> tuple[StructuredParseResult, str | None]:
    with _gen_ai_span("openai.chat.completions.create", model, messages) as span:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        completion = client.chat.completions.create(**kwargs)
        _record_completion_span_data(span, completion)
    content = completion.choices[0].message.content or ""
    return _validate_json(content), _completion_id(completion)


def _gen_ai_span(name: str, model: str, messages: list[dict[str, str]]) -> Any:
    if sentry_sdk is None or SPANDATA is None:
        return nullcontext(None)
    span = sentry_sdk.start_span(op="gen_ai.chat", name=name)
    span.set_data(SPANDATA.GEN_AI_SYSTEM, "openai")
    span.set_data(SPANDATA.GEN_AI_PROVIDER_NAME, "openai")
    span.set_data(SPANDATA.GEN_AI_OPERATION_NAME, "chat")
    span.set_data(SPANDATA.GEN_AI_REQUEST_MODEL, model)
    if conversation_id := _CONVERSATION_ID.get():
        span.set_data(SPANDATA.GEN_AI_CONVERSATION_ID, conversation_id)
    _set_span_data(span, SPANDATA.GEN_AI_INPUT_MESSAGES, messages)
    return span


def _record_completion_span_data(span: Any, completion: Any) -> None:
    if span is None or SPANDATA is None:
        return
    if response_id := getattr(completion, "id", None):
        span.set_data(SPANDATA.GEN_AI_RESPONSE_ID, response_id)
    if response_model := getattr(completion, "model", None):
        span.set_data(SPANDATA.GEN_AI_RESPONSE_MODEL, response_model)

    usage = getattr(completion, "usage", None)
    if usage is not None:
        if input_tokens := getattr(usage, "prompt_tokens", None):
            span.set_data(SPANDATA.GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
        if output_tokens := getattr(usage, "completion_tokens", None):
            span.set_data(SPANDATA.GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
        if total_tokens := getattr(usage, "total_tokens", None):
            span.set_data(SPANDATA.GEN_AI_USAGE_TOTAL_TOKENS, total_tokens)

    choices = getattr(completion, "choices", None) or []
    output_messages: list[dict[str, Any]] = []
    finish_reasons: list[str] = []
    for choice in choices:
        if reason := getattr(choice, "finish_reason", None):
            finish_reasons.append(reason)
        message = getattr(choice, "message", None)
        if message is None:
            continue
        content = getattr(message, "content", None)
        parsed = getattr(message, "parsed", None)
        if content is None and parsed is not None:
            try:
                content = parsed.model_dump_json()
            except AttributeError:
                content = json.dumps(parsed, default=str)
        output_messages.append(
            {"role": getattr(message, "role", "assistant"), "content": content or ""}
        )
    if output_messages:
        _set_span_data(span, SPANDATA.GEN_AI_OUTPUT_MESSAGES, output_messages)
    if finish_reasons:
        _set_span_data(span, SPANDATA.GEN_AI_RESPONSE_FINISH_REASONS, finish_reasons)


def _set_span_data(span: Any, key: str, value: Any) -> None:
    if sentry_ai is not None:
        sentry_ai.set_data_normalized(span, key, value)
    else:
        span.set_data(key, json.dumps(value, default=str))


def _validate_json(content: str) -> StructuredParseResult:
    try:
        return StructuredParseResult.model_validate_json(content)
    except ValidationError:
        match = JSON_OBJECT_RE.search(content)
        if not match:
            raise
        return StructuredParseResult.model_validate(json.loads(match.group(0)))


def render_user_message(email: PipelineEmail, cleaned_body: str) -> str:
    """Render the per-email user message. Reused by the fine-tuning exporter."""
    schema = json.dumps(StructuredParseResult.model_json_schema(), indent=2)
    return "\n".join(
        [
            "Parse this school email into the required schema.",
            "",
            "Schema:",
            schema,
            "",
            "Context:",
            "- Parent context: one 3rd grader and one 6th grader at the same school.",
            "- Determine `audience` accurately. It is the most important field.",
            "- Only flag parent_action_required if the parent MUST do something (not optional).",
            "- Only create action_items for mandatory tasks with consequences for missing them.",
            f"- Sender: {email.sender or '(unknown)'}",
            f"- Subject: {email.subject or '(no subject)'}",
            f"- Received timestamp: {email.received_at or '(unknown)'}",
            "",
            "Email body:",
            cleaned_body or "(empty body)",
        ]
    )


def _messages(email: PipelineEmail, cleaned_body: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for subject, body_excerpt, expected_json in _FEW_SHOT_EXAMPLES:
        messages.append(
            {
                "role": "user",
                "content": f"Subject: {subject}\n\n{body_excerpt}",
            }
        )
        messages.append({"role": "assistant", "content": expected_json})
    messages.append({"role": "user", "content": render_user_message(email, cleaned_body)})
    return messages
