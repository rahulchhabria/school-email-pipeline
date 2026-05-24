#!/usr/bin/env python3
# /// script
# dependencies = [
#   "pydantic>=2.9.0",
#   "openai>=1.50.0",
# ]
# ///
"""Evaluation harness for the school email pipeline.

Runs each golden email through cleanup → entities → parse → routing,
compares against expected outputs, and prints a metrics scorecard.

Usage:
    uv run evals/run_evals.py              # all goldens, uses OpenAI parser
    uv run evals/run_evals.py --dry-run    # skip parser, only test routing
    uv run evals/run_evals.py --case field_trip_permission
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Ensure pipeline modules are importable.
SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from school_email_pipeline.cleanup import cleanup_email_body  # noqa: E402
from school_email_pipeline.entities import extract_entities  # noqa: E402
from school_email_pipeline.models import PipelineEmail, StructuredParseResult  # noqa: E402
from school_email_pipeline.parser import parse_school_email, ParserUnavailable  # noqa: E402
from school_email_pipeline.routing import load_policy, route_email  # noqa: E402
from school_email_pipeline.settings import PipelineSettings  # noqa: E402

ROOT = Path(__file__).resolve().parent
GOLDEN_DIR = ROOT / "golden_emails"
EXPECTED_DIR = ROOT / "expected_outputs"


def load_goldens(case: str | None = None) -> list[tuple[str, dict, dict]]:
    """Load (case_id, email_payload, expected_output) tuples."""
    pattern = f"{case}.json" if case else "*.json"
    cases: list[tuple[str, dict, dict]] = []
    for path in sorted(GOLDEN_DIR.glob(pattern)):
        case_id = path.stem
        expected_path = EXPECTED_DIR / path.name
        if not expected_path.exists():
            print(f"SKIP {case_id}: no expected output file", file=sys.stderr)
            continue
        email_data = json.loads(path.read_text())
        expected_data = json.loads(expected_path.read_text())
        cases.append((case_id, email_data, expected_data))
    return cases


def _to_pipeline_email(raw: dict[str, Any]) -> PipelineEmail:
    return PipelineEmail(
        message_id=raw.get("message_id", f"eval-{id(raw)}"),
        sender=raw.get("from", ""),
        recipient=raw.get("to", ""),
        subject=raw.get("subject", ""),
        received_at=raw.get("date", ""),
        text_body=raw.get("text_body", ""),
        html_body=raw.get("html_body", ""),
        provider="eval",
        raw_payload=raw,
    )


def run_case(
    case_id: str,
    email_payload: dict[str, Any],
    expected: dict[str, Any],
    settings: PipelineSettings,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one golden through the pipeline and return comparison results."""
    email = _to_pipeline_email(email_payload)
    cleaned = cleanup_email_body(
        email.text_body, email.html_body, limit=settings.body_char_limit
    )
    entities = extract_entities(cleaned, settings)

    if dry_run:
        parsed = StructuredParseResult.model_validate(expected)
    else:
        try:
            parsed = parse_school_email(email, cleaned, entities, settings)
        except ParserUnavailable as exc:
            return {
                "case_id": case_id,
                "status": "parser_unavailable",
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            return {"case_id": case_id, "status": "parse_error", "error": str(exc)}

    policy = load_policy(settings.policy_config_path)
    routing = route_email(parsed, entities, policy)

    comparison = compare(parsed, routing, expected)
    comparison["case_id"] = case_id
    comparison["status"] = "ok"
    comparison["parsed"] = parsed.model_dump()
    comparison["routing"] = routing.model_dump()
    return comparison


def compare(
    parsed: StructuredParseResult,
    routing: Any,
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Compare parsed output against expected and return per-field results."""
    exp = StructuredParseResult.model_validate(expected)

    email_type_match = parsed.email_type == exp.email_type
    importance_match = parsed.importance == exp.importance
    parent_action_match = parsed.parent_action_required == exp.parent_action_required

    audience_exact = (
        parsed.audience.applies_to_second_grader == exp.audience.applies_to_second_grader
        and parsed.audience.applies_to_fifth_grader == exp.audience.applies_to_fifth_grader
        and parsed.audience.applies_to_whole_school == exp.audience.applies_to_whole_school
    )

    audience_relevant = _audience_relevant_match(parsed, exp)

    action_recall = _action_item_recall(parsed, exp)
    action_precision = _action_item_precision(parsed, exp)

    calendar_recall = _calendar_item_recall(parsed, exp)
    calendar_precision = _calendar_item_precision(parsed, exp)

    noise_suppressed = _noise_suppressed_correct(parsed, routing, exp)

    return {
        "email_type_match": email_type_match,
        "importance_match": importance_match,
        "parent_action_match": parent_action_match,
        "audience_exact": audience_exact,
        "audience_relevant": audience_relevant,
        "action_recall": action_recall,
        "action_precision": action_precision,
        "calendar_recall": calendar_recall,
        "calendar_precision": calendar_precision,
        "noise_suppressed": noise_suppressed,
    }


def _audience_relevant_match(parsed: StructuredParseResult, exp: StructuredParseResult) -> bool:
    """At least one correct audience flag is true, and no wrong-only flags."""
    p = parsed.audience
    e = exp.audience
    if e.applies_to_second_grader and p.applies_to_second_grader:
        return True
    if e.applies_to_fifth_grader and p.applies_to_fifth_grader:
        return True
    if e.applies_to_whole_school and p.applies_to_whole_school:
        return True
    if not any([e.applies_to_second_grader, e.applies_to_fifth_grader, e.applies_to_whole_school]):
        return not any([p.applies_to_second_grader, p.applies_to_fifth_grader, p.applies_to_whole_school])
    return False


def _action_item_recall(parsed: StructuredParseResult, exp: StructuredParseResult) -> float:
    if not exp.action_items:
        return 1.0
    if not parsed.action_items:
        return 0.0
    matched = 0
    for exp_item in exp.action_items:
        for act_item in parsed.action_items:
            if _action_similar(act_item.action, exp_item.action):
                matched += 1
                break
    return matched / len(exp.action_items)


def _action_item_precision(parsed: StructuredParseResult, exp: StructuredParseResult) -> float:
    if not parsed.action_items:
        return 1.0
    if not exp.action_items:
        return 0.0
    matched = 0
    for act_item in parsed.action_items:
        for exp_item in exp.action_items:
            if _action_similar(act_item.action, exp_item.action):
                matched += 1
                break
    return matched / len(parsed.action_items)


def _action_similar(a: str, b: str) -> bool:
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()
    if a_lower == b_lower:
        return True
    a_words = set(a_lower.split())
    b_words = set(b_lower.split())
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / min(len(a_words), len(b_words))
    return overlap >= 0.5


def _calendar_item_recall(parsed: StructuredParseResult, exp: StructuredParseResult) -> float:
    if not exp.calendar_items:
        return 1.0
    if not parsed.calendar_items:
        return 0.0
    matched = 0
    for exp_item in exp.calendar_items:
        for cal_item in parsed.calendar_items:
            if _calendar_similar(cal_item, exp_item):
                matched += 1
                break
    return matched / len(exp.calendar_items)


def _calendar_item_precision(parsed: StructuredParseResult, exp: StructuredParseResult) -> float:
    if not parsed.calendar_items:
        return 1.0
    if not exp.calendar_items:
        return 0.0
    matched = 0
    for cal_item in parsed.calendar_items:
        for exp_item in exp.calendar_items:
            if _calendar_similar(cal_item, exp_item):
                matched += 1
                break
    return matched / len(parsed.calendar_items)


def _calendar_similar(a: Any, b: Any) -> bool:
    if _action_similar(a.title, b.title):
        return True
    if a.date_text and b.date_text and a.date_text == b.date_text:
        return True
    return False


def _noise_suppressed_correct(
    parsed: StructuredParseResult, routing: Any, exp: StructuredParseResult
) -> bool:
    """If expected importance is 'ignore', was it actually suppressed?"""
    if exp.importance != "ignore":
        return True
    return "ignore" in routing.actions or not routing.send_telegram_now


def compute_aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate metrics across all cases."""
    ok_results = [r for r in results if r.get("status") == "ok"]
    n = len(ok_results)
    if n == 0:
        return {"total_cases": len(results), "successful": 0}

    metrics = {
        "total_cases": len(results),
        "successful": n,
        "parse_errors": len(results) - n,
        "email_type_accuracy": sum(r["email_type_match"] for r in ok_results) / n,
        "importance_accuracy": sum(r["importance_match"] for r in ok_results) / n,
        "parent_action_accuracy": sum(r["parent_action_match"] for r in ok_results) / n,
        "audience_exact_accuracy": sum(r["audience_exact"] for r in ok_results) / n,
        "audience_relevant_accuracy": sum(r["audience_relevant"] for r in ok_results) / n,
        "action_item_recall": sum(r["action_recall"] for r in ok_results) / n,
        "action_item_precision": sum(r["action_precision"] for r in ok_results) / n,
        "calendar_item_recall": sum(r["calendar_recall"] for r in ok_results) / n,
        "calendar_item_precision": sum(r["calendar_precision"] for r in ok_results) / n,
        "noise_suppressed_correct": sum(r["noise_suppressed"] for r in ok_results) / n,
    }

    noise_cases = [r for r in ok_results if r["noise_suppressed"] is not True]
    metrics["noise_false_negatives"] = len(noise_cases)
    return metrics


def print_scorecard(metrics: dict[str, Any]) -> None:
    """Print a one-line-per-metric scorecard."""
    print("\n=== Pipeline Eval Scorecard ===\n")
    if metrics["successful"] == 0:
        print("No successful cases to score.")
        return

    print(f"Cases: {metrics['successful']}/{metrics['total_cases']} ok, {metrics['parse_errors']} errors\n")

    rows = [
        ("email_type_accuracy", "Email Type Accuracy"),
        ("importance_accuracy", "Importance Accuracy"),
        ("parent_action_accuracy", "Parent Action Accuracy"),
        ("audience_exact_accuracy", "Audience Exact Accuracy"),
        ("audience_relevant_accuracy", "Audience Relevant Accuracy"),
        ("action_item_recall", "Action Item Recall"),
        ("action_item_precision", "Action Item Precision"),
        ("calendar_item_recall", "Calendar Item Recall"),
        ("calendar_item_precision", "Calendar Item Precision"),
        ("noise_suppressed_correct", "Noise Suppressed Correctly"),
    ]
    for key, label in rows:
        val = metrics.get(key, 0.0)
        bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
        print(f"  {label:30s} {val:5.1%}  {bar}")

    if metrics.get("noise_false_negatives", 0) > 0:
        print(f"\n  Noise false negatives: {metrics['noise_false_negatives']}")

    avg = sum(metrics.get(k, 0.0) for k, _ in rows) / len(rows)
    grade = "A" if avg >= 0.9 else "B" if avg >= 0.8 else "C" if avg >= 0.7 else "D" if avg >= 0.6 else "F"
    print(f"\n  Overall: {avg:.1%}  Grade: {grade}")


def print_case_detail(result: dict[str, Any]) -> None:
    """Print per-case comparison detail."""
    case_id = result["case_id"]
    status = result.get("status", "?")
    if status != "ok":
        print(f"\n--- {case_id}: {status} ---")
        print(f"  Error: {result.get('error', 'unknown')}")
        return

    print(f"\n--- {case_id} ---")
    checks = [
        ("email_type_match", "Email Type"),
        ("importance_match", "Importance"),
        ("parent_action_match", "Parent Action"),
        ("audience_exact", "Audience Exact"),
        ("audience_relevant", "Audience Relevant"),
        ("action_recall", "Action Recall"),
        ("action_precision", "Action Precision"),
        ("calendar_recall", "Calendar Recall"),
        ("calendar_precision", "Calendar Precision"),
        ("noise_suppressed", "Noise Suppressed"),
    ]
    for key, label in checks:
        val = result.get(key)
        icon = "✓" if val else "✗"
        print(f"  {icon} {label}: {val}")

    parsed = result.get("parsed", {})
    expected_data = None
    expected_path = EXPECTED_DIR / f"{case_id}.json"
    if expected_path.exists():
        expected_data = json.loads(expected_path.read_text())

    if not result.get("email_type_match") and expected_data:
        print(f"    Got: {parsed.get('email_type')}  Expected: {expected_data.get('email_type')}")
    if not result.get("importance_match") and expected_data:
        print(f"    Got: {parsed.get('importance')}  Expected: {expected_data.get('importance')}")


def load_eval_settings(*, dry_run: bool = False) -> PipelineSettings:
    """Load pipeline settings from .env.local for eval runs."""
    from dotenv import load_dotenv

    env_path = Path(__file__).resolve().parents[1] / ".env.local"
    if env_path.exists():
        load_dotenv(env_path)

    return PipelineSettings(
        database_url="sqlite:///eval/tmp.sqlite3",
        policy_config_path=Path(__file__).resolve().parents[1] / "policy.default.json",
        openai_api_key="" if dry_run else os.environ.get("OPENAI_API_KEY", "").strip(),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip(),
        openai_timeout_seconds=60.0,
        enable_gliner=False,
        enable_pioneer=False,
        pioneer_api_key="",
        pioneer_model_id="gliner2-large",
        pioneer_base_url="https://api.pioneer.ai",
        pioneer_threshold=0.4,
        pioneer_timeout_seconds=30.0,
        enable_ash=False,
        ash_base_url="",
        body_char_limit=12000,
        legacy_fallback_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
        dry_run=True,
        ash_cwd=Path("/tmp"),
        ash_model=None,
        senders_config_path=Path(__file__).resolve().parents[1] / "senders.toml",
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run eval harness")
    parser.add_argument("--case", default=None, help="Run a single case by ID")
    parser.add_argument("--dry-run", action="store_true", help="Skip parser, test routing only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-case details")
    args = parser.parse_args()

    goldens = load_goldens(args.case)
    if not goldens:
        print("No golden cases found.", file=sys.stderr)
        return 1

    settings = load_eval_settings(dry_run=args.dry_run)
    results: list[dict[str, Any]] = []

    for case_id, email_payload, expected in goldens:
        print(f"Running {case_id}...", end=" ", flush=True)
        t0 = time.time()
        result = run_case(case_id, email_payload, expected, settings, dry_run=args.dry_run)
        elapsed = time.time() - t0
        status = result.get("status", "?")
        ok_marker = "ok" if status == "ok" else status
        print(f"{ok_marker} ({elapsed:.1f}s)")
        results.append(result)

    metrics = compute_aggregate(results)
    print_scorecard(metrics)

    if args.verbose:
        for result in results:
            print_case_detail(result)

    if metrics.get("parse_errors", 0) > 0 and not args.dry_run:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
