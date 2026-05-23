from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_DB_PATH = SKILL_DIR / "data" / "school_email_pipeline.sqlite3"
DEFAULT_POLICY_PATH = SKILL_DIR / "policy.default.json"
DEFAULT_ASH_CWD = Path("/home/rahul/GitHub/ash-main")


def env_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, *, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class PipelineSettings:
    database_url: str
    policy_config_path: Path
    openai_api_key: str
    openai_model: str
    openai_timeout_seconds: float
    enable_gliner: bool
    enable_pioneer: bool
    pioneer_api_key: str
    pioneer_model_id: str
    pioneer_base_url: str
    pioneer_threshold: float
    pioneer_timeout_seconds: float
    enable_ash: bool
    ash_base_url: str
    body_char_limit: int
    legacy_fallback_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    dry_run: bool
    ash_cwd: Path
    ash_model: str | None
    senders_config_path: Path


def load_pipeline_settings() -> PipelineSettings:
    return PipelineSettings(
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}"),
        policy_config_path=Path(
            os.environ.get("SCHOOL_EMAIL_POLICY_PATH", DEFAULT_POLICY_PATH)
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip(),
        openai_timeout_seconds=float(
            os.environ.get("OPENAI_TIMEOUT_SECONDS", "60").strip() or "60"
        ),
        enable_gliner=env_bool("ENABLE_GLINER"),
        enable_pioneer=env_bool("ENABLE_PIONEER"),
        pioneer_api_key=os.environ.get("PIONEER_API_KEY", "").strip(),
        pioneer_model_id=os.environ.get("PIONEER_MODEL_ID", "gliner2-large").strip()
        or "gliner2-large",
        pioneer_base_url=os.environ.get(
            "PIONEER_BASE_URL", "https://api.pioneer.ai"
        ).strip()
        or "https://api.pioneer.ai",
        pioneer_threshold=float(
            os.environ.get("PIONEER_THRESHOLD", "0.4").strip() or "0.4"
        ),
        pioneer_timeout_seconds=float(
            os.environ.get("PIONEER_TIMEOUT_SECONDS", "30").strip() or "30"
        ),
        enable_ash=env_bool("ENABLE_ASH"),
        ash_base_url=os.environ.get("ASH_BASE_URL", "").strip(),
        body_char_limit=max(
            1000, env_int("EMAIL_FORWARD_BODY_CHAR_LIMIT", default=12000)
        ),
        legacy_fallback_enabled=env_bool("EMAIL_FORWARD_LEGACY_FALLBACK", default=True),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        dry_run=env_bool("EMAIL_FORWARD_DRY_RUN"),
        ash_cwd=Path(os.environ.get("EMAIL_FORWARD_ASH_CWD", DEFAULT_ASH_CWD)),
        ash_model=os.environ.get("EMAIL_FORWARD_ASH_MODEL", "").strip() or None,
        senders_config_path=Path(
            os.environ.get("SENDERS_CONFIG_PATH", SKILL_DIR / "senders.toml")
        ),
    )
