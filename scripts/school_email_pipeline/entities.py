from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from .models import ExtractedEntity
from .settings import PipelineSettings


logger = logging.getLogger(__name__)

GLINER_LABELS = [
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


@lru_cache(maxsize=1)
def _load_gliner_model() -> Any:
    from gliner import GLiNER  # type: ignore[import-not-found]

    return GLiNER.from_pretrained("urchade/gliner_medium-v2.1")


def extract_entities(body: str, settings: PipelineSettings) -> list[ExtractedEntity]:
    if not settings.enable_gliner:
        return []
    try:
        model = _load_gliner_model()
        raw_entities = model.predict_entities(body, GLINER_LABELS, threshold=0.35)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gliner_extraction_failed", extra={"error": str(exc)})
        return []

    entities: list[ExtractedEntity] = []
    for item in raw_entities or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        label = str(item.get("label") or "").strip()
        if not text or not label:
            continue
        entities.append(
            ExtractedEntity(
                label=label,
                text=text,
                start=_int_or_none(item.get("start")),
                end=_int_or_none(item.get("end")),
                confidence=_float_or_zero(item.get("score")),
            )
        )
    return entities


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
