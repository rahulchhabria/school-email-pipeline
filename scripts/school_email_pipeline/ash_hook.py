from __future__ import annotations

from .models import RoutingDecision, StructuredParseResult
from .settings import PipelineSettings


class AshHookUnavailable(RuntimeError):
    pass


def route_with_ash(
    parsed: StructuredParseResult,
    settings: PipelineSettings,
) -> RoutingDecision:
    del parsed
    if not settings.enable_ash:
        raise AshHookUnavailable("ENABLE_ASH is false")
    raise AshHookUnavailable("Ash orchestration hook is not implemented yet")
