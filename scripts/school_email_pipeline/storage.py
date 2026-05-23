from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .models import FeedbackEvent, PipelineEmail, RoutingDecision, StructuredParseResult


def database_path(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(unquote(database_url.removeprefix("sqlite://")))
    if database_url.startswith("sqlite://"):
        return Path(unquote(database_url.removeprefix("sqlite://")))
    return Path(database_url)


class EmailStore:
    def __init__(self, database_url: str) -> None:
        self.path = database_path(database_url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    sender TEXT,
                    recipient TEXT,
                    subject TEXT,
                    received_at TEXT,
                    raw_email_json TEXT NOT NULL,
                    cleaned_body TEXT,
                    extracted_entities_json TEXT,
                    structured_parse_json TEXT,
                    routing_decision_json TEXT,
                    telegram_message_id INTEGER,
                    feedback_json TEXT,
                    processing_status TEXT NOT NULL,
                    error_logs_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id INTEGER,
                    telegram_message_id INTEGER,
                    feedback_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(email_id) REFERENCES emails(id)
                )
                """
            )

    def create_email_if_new(self, email: PipelineEmail) -> tuple[int, bool]:
        now = _utc_now()
        raw = _json(email.model_dump())
        dedupe_key = email.message_id
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO emails (
                        message_id, dedupe_key, sender, recipient, subject,
                        received_at, raw_email_json, processing_status,
                        error_logs_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email.message_id,
                        dedupe_key,
                        email.sender,
                        email.recipient,
                        email.subject,
                        email.received_at,
                        raw,
                        "received",
                        "[]",
                        now,
                        now,
                    ),
                )
                return int(cursor.lastrowid), True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT id FROM emails WHERE message_id = ? OR dedupe_key = ?",
                    (email.message_id, dedupe_key),
                ).fetchone()
                if row is None:
                    raise
                return int(row["id"]), False

    def update_stage(self, email_id: int, status: str, **fields: Any) -> None:
        allowed = {
            "cleaned_body",
            "extracted_entities_json",
            "structured_parse_json",
            "routing_decision_json",
            "telegram_message_id",
            "feedback_json",
            "processing_status",
            "error_logs_json",
        }
        updates = {"processing_status": status, **fields, "updated_at": _utc_now()}
        columns = [key for key in updates if key in allowed or key == "updated_at"]
        assignments = ", ".join(f"{key} = ?" for key in columns)
        values = [updates[key] for key in columns]
        values.append(email_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE emails SET {assignments} WHERE id = ?", values)

    def store_parse(
        self,
        email_id: int,
        parsed: StructuredParseResult,
        routing: RoutingDecision,
    ) -> None:
        self.update_stage(
            email_id,
            routing.processing_status,
            structured_parse_json=parsed.model_dump_json(),
            routing_decision_json=routing.model_dump_json(),
        )

    def set_telegram_message_id(self, email_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        self.update_stage(email_id, "delivered", telegram_message_id=message_id)

    def append_error(self, email_id: int, message: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT error_logs_json FROM emails WHERE id = ?", (email_id,)
            ).fetchone()
            errors = json.loads(row["error_logs_json"] or "[]") if row else []
            errors.append({"at": _utc_now(), "message": message})
            conn.execute(
                """
                UPDATE emails
                SET error_logs_json = ?, processing_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json(errors), "error", _utc_now(), email_id),
            )

    def log_feedback(self, event: FeedbackEvent) -> None:
        created_at = event.created_at.isoformat() if event.created_at else _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback (
                    email_id, telegram_message_id, feedback_type,
                    payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.email_id,
                    event.telegram_message_id,
                    event.feedback_type,
                    _json(event.payload),
                    created_at,
                ),
            )
            if event.email_id is not None:
                rows = conn.execute(
                    "SELECT feedback_type, payload_json, created_at FROM feedback WHERE email_id = ?",
                    (event.email_id,),
                ).fetchall()
                feedback = [
                    {
                        "feedback_type": row["feedback_type"],
                        "payload": json.loads(row["payload_json"]),
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
                conn.execute(
                    "UPDATE emails SET feedback_json = ?, updated_at = ? WHERE id = ?",
                    (_json(feedback), _utc_now(), event.email_id),
                )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
