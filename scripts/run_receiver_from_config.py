#!/usr/bin/env python3
"""Launch the email receiver using Telegram settings from ~/.ash/config.toml."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


CONFIG_PATH = Path("/home/rahul/.ash/config.toml")
SKILL_DIR = Path("/home/rahul/.ash/workspace/skills/email-forward-summary")
RECEIVER_SCRIPT = Path(
    "/home/rahul/.ash/workspace/skills/email-forward-summary/scripts/email_webhook_server.py"
)
LOCAL_ENV_PATH = SKILL_DIR / ".env.local"
VENV_PYTHON = SKILL_DIR / ".venv" / "bin" / "python"
DEFAULT_PYTHON = "/home/linuxbrew/.linuxbrew/bin/python3"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def _load_local_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def main() -> int:
    config = _load_config()
    telegram = config.get("telegram", {})
    skill_telegram = config.get("skills", {}).get("sfday-telegram-alert", {})
    env_config = config.get("env", {})
    sandbox_env = config.get("sandbox", {}).get("env", {})

    bot_token = str(
        telegram.get("bot_token")
        or env_config.get("TELEGRAM_BOT_TOKEN")
        or sandbox_env.get("TELEGRAM_BOT_TOKEN")
        or skill_telegram.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    chat_id = str(
        skill_telegram.get("TELEGRAM_CHAT_ID")
        or env_config.get("TELEGRAM_CHAT_ID")
        or env_config.get("telegram_chat_id")
        or sandbox_env.get("TELEGRAM_CHAT_ID")
        or sandbox_env.get("telegram_chat_id")
        or ""
    ).strip()

    env = os.environ.copy()
    for key, value in _load_local_env(LOCAL_ENV_PATH).items():
        env.setdefault(key, value)
    if bot_token:
        env["TELEGRAM_BOT_TOKEN"] = bot_token
    if chat_id:
        env["TELEGRAM_CHAT_ID"] = chat_id
    env.setdefault("EMAIL_FORWARD_ASH_CWD", "/home/rahul/GitHub/ash")
    env.setdefault("EMAIL_FORWARD_ASH_MODEL", "default")
    env.setdefault(
        "EMAIL_FORWARD_STATE_PATH",
        "/home/rahul/.ash/workspace/skills/email-forward-summary/data/state.json",
    )
    env.setdefault("EMAIL_FORWARD_DRY_RUN", "0")
    env.setdefault("EMAIL_FORWARD_LEGACY_FALLBACK", "0")

    python_bin = str(VENV_PYTHON if VENV_PYTHON.exists() else DEFAULT_PYTHON)
    args = [
        python_bin,
        str(RECEIVER_SCRIPT),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8787",
    ]
    os.execvpe(python_bin, args, env)


if __name__ == "__main__":
    raise SystemExit(main())
