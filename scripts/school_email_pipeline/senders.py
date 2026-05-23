"""Per-sender allow/deny rules loaded from senders.toml.

Applied before the routing policy engine to short-circuit known senders
without needing the parser to classify them.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SenderRule:
    pattern: str
    action: str


@dataclass
class SenderPolicy:
    allow: list[SenderRule] = field(default_factory=list)
    deny: list[SenderRule] = field(default_factory=list)


def load_sender_policy(path: Path) -> SenderPolicy:
    if not path.exists():
        return SenderPolicy()
    try:
        import tomllib
    except ImportError:
        try:
            from tomli import tomllib  # type: ignore[no-redef]
        except ImportError:
            logger.warning("senders_toml_no_toml_parser", extra={"path": str(path)})
            return SenderPolicy()

    try:
        data: dict[str, Any] = tomllib.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        logger.warning("senders_toml_parse_error", extra={"error": str(exc)})
        return SenderPolicy()

    allow = [
        SenderRule(pattern=pat, action=act)
        for pat, act in data.get("allow", {}).items()
    ]
    deny = [
        SenderRule(pattern=pat, action=act)
        for pat, act in data.get("deny", {}).items()
    ]
    return SenderPolicy(allow=allow, deny=deny)


def check_sender(
    sender: str,
    policy: SenderPolicy,
) -> str | None:
    """Return an override action string or None (no override).

    Priority: deny > allow > None.
    Action strings:
      - "always_suppress": skip the email entirely (importance = ignore)
      - "digest_only": route to daily digest regardless of parser
      - "high_priority": treat as high importance regardless of parser
    """
    sender_lower = (sender or "").lower().strip()

    for rule in policy.deny:
        if fnmatch.fnmatch(sender_lower, rule.pattern.lower()):
            return rule.action

    for rule in policy.allow:
        if fnmatch.fnmatch(sender_lower, rule.pattern.lower()):
            return rule.action

    return None
