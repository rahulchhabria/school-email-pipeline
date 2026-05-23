#!/usr/bin/env python3
"""Launch the email receiver using Telegram settings from ~/.ash/config.toml."""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path


CONFIG_PATH = Path("/home/rahul/.ash/config.toml")
RECEIVER_SCRIPT = Path(
    "/home/rahul/.ash/workspace/skills/email-forward-summary/scripts/email_webhook_server.py"
)
DEFAULT_UV_BIN = "/home/linuxbrew/.linuxbrew/bin/uv"


def _load_config() -> dict:
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def main() -> int:
    config = _load_config()
    telegram = config.get("skills", {}).get("sfday-telegram-alert", {})
    sandbox_env = config.get("sandbox", {}).get("env", {})

    bot_token = str(
        telegram.get("TELEGRAM_BOT_TOKEN")
        or sandbox_env.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    chat_id = str(
        telegram.get("TELEGRAM_CHAT_ID") or sandbox_env.get("TELEGRAM_CHAT_ID") or ""
    ).strip()

    env = os.environ.copy()
    if bot_token:
        env["TELEGRAM_BOT_TOKEN"] = bot_token
    if chat_id:
        env["TELEGRAM_CHAT_ID"] = chat_id
    env.setdefault("EMAIL_FORWARD_ASH_CWD", "/home/rahul/GitHub/ash-main")
    env.setdefault("EMAIL_FORWARD_ASH_MODEL", "default")
    env.setdefault(
        "EMAIL_FORWARD_STATE_PATH",
        "/home/rahul/.ash/workspace/skills/email-forward-summary/data/state.json",
    )
    env.setdefault("EMAIL_FORWARD_DRY_RUN", "0")

    uv_bin = shutil.which("uv") or DEFAULT_UV_BIN
    args = [
        uv_bin,
        "run",
        str(RECEIVER_SCRIPT),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8787",
    ]
    os.execvpe(uv_bin, args, env)


if __name__ == "__main__":
    raise SystemExit(main())
