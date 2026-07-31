"""Deprecated stub.

GLiNER-based entity extraction has been removed from the live pipeline.
The OpenAI parser now does all classification and structure extraction
in a single round-trip and benefits from fine-tuning on the golden
evals. This module is kept only so older imports and the Pioneer
client/training scripts can still resolve `GLINER_LABELS` if needed.
"""

from __future__ import annotations

from .models import ExtractedEntity
from .settings import PipelineSettings


GLINER_LABELS: list[str] = [
    "school event",
    "date",
    "time",
    "deadline",
    "grade level",
    "teacher name",
    "location",
    "action required",
    "form",
    "payment",
    "volunteer opportunity",
    "student group",
    "calendar item",
    "pickup",
    "dropoff",
    "dress code",
    "field trip",
    "performance",
    "conference",
]


def extract_entities(body: str, settings: PipelineSettings) -> list[ExtractedEntity]:
    """No-op: GLiNER extraction is disabled in the OpenAI-only pipeline."""
    del body, settings
    return []
