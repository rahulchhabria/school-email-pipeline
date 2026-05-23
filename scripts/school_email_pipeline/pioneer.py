from __future__ import annotations

from .models import ExtractedEntity, PipelineEmail, StructuredParseResult
from .settings import PipelineSettings


class PioneerUnavailable(RuntimeError):
    pass


def classify_with_pioneer(
    email: PipelineEmail,
    cleaned_body: str,
    entities: list[ExtractedEntity],
    settings: PipelineSettings,
) -> StructuredParseResult:
    del email, cleaned_body, entities
    if not settings.enable_pioneer:
        raise PioneerUnavailable("ENABLE_PIONEER is false")
    raise PioneerUnavailable("Pioneer classifier hook is not implemented yet")
