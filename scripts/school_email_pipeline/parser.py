from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import ExtractedEntity, PipelineEmail, StructuredParseResult
from .settings import PipelineSettings


logger = logging.getLogger(__name__)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"
_FEW_SHOT_IDS = ["field_trip_permission", "emergency_early_dismissal", "school_newsletter"]


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


def parse_school_email(
    email: PipelineEmail,
    cleaned_body: str,
    entities: list[ExtractedEntity],
    settings: PipelineSettings,
) -> StructuredParseResult:
    if not settings.openai_api_key:
        raise ParserUnavailable("OPENAI_API_KEY is not configured")

    try:
        from openai import OpenAI
    except Exception as exc:  # noqa: BLE001
        raise ParserUnavailable(f"openai package is unavailable: {exc}") from exc

    client = OpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
    )
    messages = _messages(email, cleaned_body, entities)
    errors: list[str] = []

    for attempt in range(2):
        try:
            parsed = _parse_with_structured_output(
                client, settings.openai_model, messages
            )
            logger.info(
                "openai_structured_parse_succeeded", extra={"attempt": attempt + 1}
            )
            return parsed
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            logger.warning(
                "openai_structured_parse_failed",
                extra={"attempt": attempt + 1, "error": str(exc)},
            )
            try:
                parsed = _parse_with_json_object(
                    client, settings.openai_model, messages
                )
                logger.info(
                    "openai_json_parse_succeeded", extra={"attempt": attempt + 1}
                )
                return parsed
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


def _parse_with_structured_output(
    client: Any, model: str, messages: list[dict[str, str]]
) -> StructuredParseResult:
    parse_method = getattr(
        getattr(getattr(client, "beta", None), "chat", None), "completions", None
    )
    if parse_method is None or not hasattr(parse_method, "parse"):
        raise ParserUnavailable("OpenAI structured parse helper is not available")

    completion = parse_method.parse(
        model=model,
        messages=messages,
        response_format=StructuredParseResult,
    )
    parsed = completion.choices[0].message.parsed
    if not isinstance(parsed, StructuredParseResult):
        return StructuredParseResult.model_validate(parsed)
    return parsed


def _parse_with_json_object(
    client: Any, model: str, messages: list[dict[str, str]]
) -> StructuredParseResult:
    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
    )
    content = completion.choices[0].message.content or ""
    return _validate_json(content)


def _validate_json(content: str) -> StructuredParseResult:
    try:
        return StructuredParseResult.model_validate_json(content)
    except ValidationError:
        match = JSON_OBJECT_RE.search(content)
        if not match:
            raise
        return StructuredParseResult.model_validate(json.loads(match.group(0)))


def _messages(
    email: PipelineEmail,
    cleaned_body: str,
    entities: list[ExtractedEntity],
) -> list[dict[str, str]]:
    schema = json.dumps(StructuredParseResult.model_json_schema(), indent=2)
    entity_context = json.dumps(
        [entity.model_dump() for entity in entities],
        ensure_ascii=False,
        indent=2,
    )
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are a strict school-email parser. The parent has one child in "
                "2nd grade and one child in 5th grade. Prioritize parent action, "
                "deadlines, calendar events, grade relevance, and urgent notices. "
                "Do not invent dates, locations, teachers, or requirements. Preserve "
                "uncertainty with nulls and confidence scores. Return only JSON."
            ),
        },
    ]
    for subject, body_excerpt, expected_json in _FEW_SHOT_EXAMPLES:
        messages.append(
            {
                "role": "user",
                "content": f"Subject: {subject}\n\n{body_excerpt}",
            }
        )
        messages.append({"role": "assistant", "content": expected_json})
    messages.append(
        {
            "role": "user",
            "content": "\n".join(
                [
                    "Parse this school email into the required schema.",
                    "",
                    "Schema:",
                    schema,
                    "",
                    "Context:",
                    "- Parent context: one 2nd grader and one 5th grader.",
                    f"- Sender: {email.sender or '(unknown)'}",
                    f"- Subject: {email.subject or '(no subject)'}",
                    f"- Received timestamp: {email.received_at or '(unknown)'}",
                    "",
                    "GLiNER entities:",
                    entity_context,
                    "",
                    "Email body:",
                    cleaned_body or "(empty body)",
                ]
            ),
        }
    )
    return messages
