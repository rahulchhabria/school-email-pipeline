"""Fine-tune the school-email OpenAI parser on the golden eval set.

This script exists for one reason: improve the model's accuracy at deciding
which of the parent's two children (3rd grader vs 6th grader) a given email
applies to. It uses ONLY the 12 golden eval pairs in
``evals/golden_emails/`` and ``evals/expected_outputs/`` as training data.

Pipeline:
  1. Build a Chat fine-tuning JSONL from the golden pairs using the live
     production system prompt and user-message template (so what the model
     learns matches what it sees at inference time).
  2. Upload the JSONL via the OpenAI Files API.
  3. Create a fine-tuning job against the configured base model
     (``OPENAI_MODEL``, default ``gpt-4.1-mini``).
  4. Poll until the job is ``succeeded`` or ``failed``.
  5. Print the fine-tuned model id; the operator drops it into
     ``OPENAI_MODEL`` in ``.env.local``.

Usage::

    python -m school_email_pipeline.openai_finetune export \\
        --out data/openai_finetune_train.jsonl

    python -m school_email_pipeline.openai_finetune train \\
        --jsonl data/openai_finetune_train.jsonl \\
        --base-model gpt-4.1-mini
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from .models import PipelineEmail, StructuredParseResult
from .parser import SYSTEM_PROMPT, render_user_message
from .settings import SKILL_DIR


logger = logging.getLogger(__name__)

DEFAULT_EVALS_DIR = SKILL_DIR / "evals"
DEFAULT_JSONL_PATH = SKILL_DIR / "data" / "openai_finetune_train.jsonl"


def _iter_golden_pairs(evals_dir: Path) -> Iterable[tuple[str, dict[str, Any], dict[str, Any]]]:
    emails_dir = evals_dir / "golden_emails"
    expected_dir = evals_dir / "expected_outputs"
    if not emails_dir.is_dir() or not expected_dir.is_dir():
        raise SystemExit(
            f"evals dir missing golden_emails/ or expected_outputs/: {evals_dir}"
        )
    for email_path in sorted(emails_dir.glob("*.json")):
        case_id = email_path.stem
        expected_path = expected_dir / f"{case_id}.json"
        if not expected_path.exists():
            logger.warning("skip_no_expected", extra={"case_id": case_id})
            continue
        try:
            email = json.loads(email_path.read_text())
            expected = json.loads(expected_path.read_text())
        except json.JSONDecodeError as exc:
            logger.warning(
                "skip_json_error", extra={"case_id": case_id, "error": str(exc)}
            )
            continue
        yield case_id, email, expected


def _to_pipeline_email(raw: dict[str, Any]) -> PipelineEmail:
    return PipelineEmail(
        message_id=raw.get("message_id", f"finetune-{id(raw)}"),
        sender=raw.get("from", ""),
        recipient=raw.get("to", ""),
        subject=raw.get("subject", ""),
        received_at=raw.get("date", ""),
        text_body=raw.get("text_body", ""),
        html_body=raw.get("html_body", ""),
        provider="finetune",
        raw_payload=raw,
    )


def _build_training_row(email: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    pipeline_email = _to_pipeline_email(email)
    cleaned_body = (email.get("text_body") or "").strip() or (
        email.get("html_body") or ""
    )
    validated = StructuredParseResult.model_validate(expected)
    assistant_content = validated.model_dump_json()
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": render_user_message(pipeline_email, cleaned_body),
            },
            {"role": "assistant", "content": assistant_content},
        ],
    }


def export_jsonl(evals_dir: Path, out_path: Path) -> int:
    """Write the OpenAI Chat fine-tuning JSONL. Returns row count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for case_id, email, expected in _iter_golden_pairs(evals_dir):
            try:
                row = _build_training_row(email, expected)
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                logger.warning(
                    "skip_build_error", extra={"case_id": case_id, "error": str(exc)}
                )
                continue
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")
            written += 1
    logger.info(
        "openai_finetune_exported",
        extra={"rows": written, "skipped": skipped, "path": str(out_path)},
    )
    return written


def _openai_client() -> Any:
    try:
        from openai import OpenAI
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "openai package is not installed. Add openai>=1.50.0 to the script "
            f"dependencies. ({exc})"
        ) from exc
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set")
    return OpenAI(api_key=api_key)


def _upload_file(client: Any, path: Path) -> str:
    with path.open("rb") as fh:
        uploaded = client.files.create(file=fh, purpose="fine-tune")
    file_id = getattr(uploaded, "id", None)
    if not file_id:
        raise SystemExit(f"upload returned no file id: {uploaded}")
    logger.info("openai_file_uploaded", extra={"file_id": file_id})
    return str(file_id)


def _create_job(client: Any, file_id: str, base_model: str, suffix: str | None) -> str:
    kwargs: dict[str, Any] = {"training_file": file_id, "model": base_model}
    if suffix:
        kwargs["suffix"] = suffix
    job = client.fine_tuning.jobs.create(**kwargs)
    job_id = getattr(job, "id", None)
    if not job_id:
        raise SystemExit(f"create job returned no id: {job}")
    logger.info(
        "openai_finetune_job_created",
        extra={"job_id": job_id, "base_model": base_model},
    )
    return str(job_id)


def _poll_job(client: Any, job_id: str, *, poll_seconds: float, timeout_seconds: int) -> Any:
    deadline = time.time() + timeout_seconds
    last = None
    while time.time() < deadline:
        job = client.fine_tuning.jobs.retrieve(job_id)
        last = job
        status = getattr(job, "status", "unknown")
        logger.info("openai_finetune_job_status", extra={"status": status})
        if status in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(poll_seconds)
    raise SystemExit(f"timed out polling fine-tuning job {job_id}; last: {last}")


def cmd_export(args: argparse.Namespace) -> int:
    rows = export_jsonl(args.evals_dir, args.out)
    if rows == 0:
        print(f"no rows written to {args.out}", file=sys.stderr)
        return 1
    print(f"wrote {rows} rows to {args.out}")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    jsonl_path = args.jsonl
    if not jsonl_path.exists() or args.export:
        export_jsonl(args.evals_dir, jsonl_path)
    if not jsonl_path.exists():
        raise SystemExit(f"training JSONL missing: {jsonl_path}")

    client = _openai_client()
    file_id = _upload_file(client, jsonl_path)
    job_id = _create_job(
        client,
        file_id,
        base_model=args.base_model,
        suffix=args.suffix,
    )
    if args.no_wait:
        print(json.dumps({"job_id": job_id, "training_file": file_id}, indent=2))
        return 0
    job = _poll_job(
        client,
        job_id,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    status = getattr(job, "status", "unknown")
    fine_tuned_model = getattr(job, "fine_tuned_model", None)
    print(
        json.dumps(
            {
                "job_id": job_id,
                "status": status,
                "fine_tuned_model": fine_tuned_model,
                "training_file": file_id,
            },
            indent=2,
        )
    )
    if status == "succeeded" and fine_tuned_model:
        print(
            f"\nSet in .env.local:\n    OPENAI_MODEL={fine_tuned_model}\n",
            file=sys.stderr,
        )
        return 0
    return 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser(
        "export", help="Write the fine-tuning JSONL but do not upload"
    )
    export.add_argument("--evals-dir", type=Path, default=DEFAULT_EVALS_DIR)
    export.add_argument("--out", type=Path, default=DEFAULT_JSONL_PATH)
    export.set_defaults(func=cmd_export)

    train = subparsers.add_parser(
        "train", help="Export (if needed), upload, create job, and poll to completion"
    )
    train.add_argument("--evals-dir", type=Path, default=DEFAULT_EVALS_DIR)
    train.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL_PATH)
    train.add_argument(
        "--base-model",
        default=os.environ.get("OPENAI_FINETUNE_BASE_MODEL", "gpt-4.1-mini"),
        help="Base model id (must support fine-tuning).",
    )
    train.add_argument("--suffix", default="school-email")
    train.add_argument("--poll-seconds", type=float, default=30.0)
    train.add_argument("--timeout-seconds", type=int, default=3600)
    train.add_argument("--no-wait", action="store_true")
    train.add_argument(
        "--export",
        action="store_true",
        help="Re-export the JSONL before uploading even if one already exists.",
    )
    train.set_defaults(func=cmd_train)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
