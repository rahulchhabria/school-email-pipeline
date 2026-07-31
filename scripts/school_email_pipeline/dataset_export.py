"""Convert evals/golden_emails + expected_outputs into Pioneer JSONL.

The output file is a decoder-format dataset (Pioneer ``dataset_type="decoder"``):

    {"messages": [
        {"role": "system",    "content": "<system prompt>"},
        {"role": "user",      "content": "<email rendering>"},
        {"role": "assistant", "content": "<StructuredParseResult JSON>"}
    ]}

One JSONL row per (golden_email, expected_output) pair.

Usage
-----

    python -m school_email_pipeline.dataset_export \
        --evals-dir /home/rahul/.ash/workspace/skills/email-forward-summary/evals \
        --out /tmp/school_emails.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import StructuredParseResult


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "You are a strict school-email parser for a parent with one child in 3rd "
    "grade and one child in 6th grade.\n\n"
    "Return EXACTLY one JSON object matching the StructuredParseResult schema, "
    "no prose, no markdown.\n"
    "- parent_action_required MUST be true only when the email contains a "
    "mandatory deadline, a form that MUST be returned, or a payment that MUST "
    "be sent.\n"
    "- action_items capture only mandatory tasks whose omission causes a "
    "negative consequence.\n"
    "- importance: 'low' for newsletters/info; 'medium' for grade-relevant "
    "events; 'high'/'urgent' only for mandatory deadlines or emergencies.\n"
    "- Never invent dates, locations, teachers, or requirements.\n"
    "- Preserve uncertainty with null values and per-field confidence scores."
)


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    email: dict[str, Any]
    expected: dict[str, Any]


def _iter_eval_cases(evals_dir: Path) -> Iterable[EvalCase]:
    emails_dir = evals_dir / "golden_emails"
    expected_dir = evals_dir / "expected_outputs"
    for email_path in sorted(emails_dir.glob("*.json")):
        case_id = email_path.stem
        expected_path = expected_dir / f"{case_id}.json"
        if not expected_path.exists():
            logger.warning(
                "missing_expected_output", extra={"eval_case_id": case_id}
            )
            continue
        try:
            email = json.loads(email_path.read_text())
            expected = json.loads(expected_path.read_text())
        except json.JSONDecodeError as exc:
            logger.warning(
                "eval_case_unparseable",
                extra={"eval_case_id": case_id, "error": str(exc)},
            )
            continue
        yield EvalCase(case_id=case_id, email=email, expected=expected)


def _render_user_message(email: dict[str, Any]) -> str:
    body = (email.get("text_body") or "").strip()
    return "\n".join(
        [
            f"From: {email.get('from') or '(unknown)'}",
            f"Subject: {email.get('subject') or '(no subject)'}",
            f"Received: {email.get('date') or '(unknown)'}",
            "---",
            body or "(empty body)",
        ]
    )


def _canonicalize_expected(expected: dict[str, Any]) -> str:
    parsed = StructuredParseResult.model_validate(expected)
    return parsed.model_dump_json()


def _build_row(case: EvalCase) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _render_user_message(case.email)},
            {"role": "assistant", "content": _canonicalize_expected(case.expected)},
        ],
        "metadata": {"case_id": case.case_id},
    }


def export_jsonl(evals_dir: Path, out_path: Path) -> int:
    """Write a Pioneer decoder JSONL file. Returns number of rows written."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for case in _iter_eval_cases(evals_dir):
            try:
                row = _build_row(case)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "case_skipped",
                    extra={"eval_case_id": case.case_id, "error": str(exc)},
                )
                skipped += 1
                continue
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")
            written += 1
    logger.info(
        "dataset_exported",
        extra={"rows": written, "skipped": skipped, "path": str(out_path)},
    )
    return written


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evals-dir",
        type=Path,
        required=True,
        help="Path to the evals/ directory containing golden_emails/ and expected_outputs/",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Destination JSONL path",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows = export_jsonl(args.evals_dir, args.out)
    print(f"wrote {rows} rows to {args.out}")
    return 0 if rows > 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
