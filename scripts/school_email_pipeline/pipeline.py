from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Awaitable

from .ash_hook import AshHookUnavailable, route_with_ash
from .cleanup import cleanup_email_body
from .models import (
    PipelineEmail,
    PipelineResult,
    RoutingDecision,
    StructuredParseResult,
    TelegramDelivery,
)
from .parser import parse_school_email
from .routing import load_policy, route_email
from .senders import check_sender, load_sender_policy
from .settings import PipelineSettings
from .storage import EmailStore
from .telegram import format_telegram_message, send_telegram_alert


logger = logging.getLogger(__name__)
LegacySummaryRunner = Callable[[PipelineEmail], str]
LegacySender = Callable[[str], Awaitable[int | None]]


async def process_school_email(
    email: PipelineEmail,
    settings: PipelineSettings,
    *,
    legacy_summary_runner: LegacySummaryRunner | None = None,
    legacy_sender: LegacySender | None = None,
) -> PipelineResult:
    store = EmailStore(settings.database_url)
    email_id, is_new = store.create_email_if_new(email)
    if not is_new:
        logger.info(
            "duplicate_email",
            extra={"message_id": email.message_id, "email_id": email_id},
        )
        return PipelineResult(
            status="duplicate", message_id=email.message_id, email_id=email_id
        )

    try:
        if _is_google_calendar_notification(email):
            store.update_stage(email_id, "google_calendar_suppressed")
            logger.info(
                "google_calendar_suppressed",
                extra={"message_id": email.message_id, "sender": email.sender},
            )
            return PipelineResult(
                status="google_calendar_suppressed",
                message_id=email.message_id,
                email_id=email_id,
            )

        logger.info("stage_cleanup_started", extra={"message_id": email.message_id})
        cleaned = cleanup_email_body(
            email.text_body,
            email.html_body,
            limit=settings.body_char_limit,
        )
        store.update_stage(email_id, "cleaned", cleaned_body=cleaned)

        sender_override = _check_sender_override(email.sender, settings)
        if sender_override == "always_suppress":
            store.update_stage(email_id, "sender_suppressed")
            logger.info(
                "sender_suppressed",
                extra={"message_id": email.message_id, "sender": email.sender},
            )
            return PipelineResult(
                status="sender_suppressed",
                message_id=email.message_id,
                email_id=email_id,
            )

        logger.info("stage_parse_started", extra={"message_id": email.message_id})
        parsed, inference_id = parse_school_email(email, cleaned, settings)
        if inference_id:
            store.set_pioneer_inference_id(email_id, inference_id)

        logger.info("stage_routing_started", extra={"message_id": email.message_id})
        routing = _route(parsed, settings, sender_override=sender_override)
        store.store_parse(email_id, parsed, routing)

        telegram = TelegramDelivery()
        if routing.send_telegram_now:
            text = format_telegram_message(email.subject, parsed, routing)
            try:
                message_id = await send_telegram_alert(
                    settings, text, email_id=email_id
                )
            except Exception as telegram_exc:  # noqa: BLE001
                store.append_error(email_id, f"telegram_failed: {telegram_exc}")
                store.update_stage(email_id, "telegram_failed")
                logger.warning(
                    "telegram_alert_failed_non_fatal",
                    extra={
                        "message_id": email.message_id,
                        "error": str(telegram_exc),
                    },
                )
                return PipelineResult(
                    status="telegram_failed",
                    message_id=email.message_id,
                    email_id=email_id,
                    routing=routing,
                    telegram=TelegramDelivery(sent=False, message_id=None, text=""),
                )
            store.set_telegram_message_id(email_id, message_id)
            telegram = TelegramDelivery(
                sent=not settings.dry_run,
                message_id=message_id,
                text=text if settings.dry_run else "",
            )
            logger.info("telegram_alert_ready", extra={"message_id": email.message_id})
        else:
            store.update_stage(email_id, routing.processing_status)
            logger.info(
                "telegram_suppressed_by_policy",
                extra={"message_id": email.message_id, "actions": routing.actions},
            )

        return PipelineResult(
            status="delivered" if telegram.sent else routing.processing_status,
            message_id=email.message_id,
            email_id=email_id,
            routing=routing,
            telegram=telegram,
        )
    except Exception as exc:  # noqa: BLE001
        store.append_error(email_id, str(exc))
        logger.exception(
            "school_email_pipeline_failed", extra={"message_id": email.message_id}
        )
        if _can_legacy_fallback(settings, legacy_summary_runner, legacy_sender, exc):
            return await _run_legacy_fallback(
                email,
                email_id,
                store,
                legacy_summary_runner,
                legacy_sender,
                settings,
            )
        raise


def _is_google_calendar_notification(email: PipelineEmail) -> bool:
    """Suppress Google Calendar notification emails that arrive via Gmail forwarding."""
    sender = email.sender.lower()
    subject = email.subject.lower()
    message_id = email.message_id.lower()
    body = f"{email.text_body}\n{email.html_body}".lower()

    if "calendar-notification@google.com" in sender:
        return True

    if message_id.startswith("<calendar-") and message_id.endswith("@google.com>"):
        return True

    if (
        "invitation from google calendar" in body
        and "you are receiving this email because you are subscribed to calendar" in body
    ):
        return True

    return (
        subject.startswith(("invitation:", "updated invitation:", "canceled event:"))
        and "google calendar" in body
        and "calendar.google.com" in body
    )


def _check_sender_override(sender: str, settings: PipelineSettings) -> str | None:
    policy = load_sender_policy(settings.senders_config_path)
    return check_sender(sender, policy)


def _route(
    parsed: StructuredParseResult,
    settings: PipelineSettings,
    *,
    sender_override: str | None = None,
) -> RoutingDecision:
    if sender_override == "digest_only":
        return RoutingDecision(
            actions=["add_to_daily_digest"],
            reasons=["sender policy: digest_only"],
            relevance="both",
            send_telegram_now=False,
            processing_status="queued_digest",
        )
    if sender_override == "high_priority":
        parsed.importance = "high"  # type: ignore[assignment]
    if settings.enable_ash:
        try:
            return route_with_ash(parsed, settings)
        except AshHookUnavailable:
            logger.info("ash_hook_unavailable_falling_back_to_local_policy")
    return route_email(parsed, load_policy(settings.policy_config_path))


def _can_legacy_fallback(
    settings: PipelineSettings,
    legacy_summary_runner: LegacySummaryRunner | None,
    legacy_sender: LegacySender | None,
    exc: Exception,
) -> bool:
    del exc
    return (
        settings.legacy_fallback_enabled
        and legacy_summary_runner is not None
        and legacy_sender is not None
    )


async def _run_legacy_fallback(
    email: PipelineEmail,
    email_id: int,
    store: EmailStore,
    legacy_summary_runner: LegacySummaryRunner | None,
    legacy_sender: LegacySender | None,
    settings: PipelineSettings,
) -> PipelineResult:
    if legacy_summary_runner is None or legacy_sender is None:
        raise RuntimeError("legacy fallback requested without legacy handlers")
    logger.warning(
        "legacy_summary_fallback_started", extra={"message_id": email.message_id}
    )
    summary = legacy_summary_runner(email).strip()
    if not summary or summary == "[NO_REPLY]":
        store.update_stage(email_id, "no_reply")
        return PipelineResult(
            status="no_reply", message_id=email.message_id, email_id=email_id
        )
    telegram_message_id = await legacy_sender(summary)
    store.set_telegram_message_id(email_id, telegram_message_id)
    return PipelineResult(
        status="dry_run" if settings.dry_run else "legacy_delivered",
        message_id=email.message_id,
        email_id=email_id,
        telegram=TelegramDelivery(
            sent=not settings.dry_run,
            message_id=telegram_message_id,
            text=summary if settings.dry_run else "",
        ),
    )
