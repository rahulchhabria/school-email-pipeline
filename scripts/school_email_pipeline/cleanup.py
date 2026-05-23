from __future__ import annotations

import re
from html import unescape


SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr)\b[^>]*>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
BLANK_LINES_RE = re.compile(r"\n{3,}")
FORWARDED_HEADER_RE = re.compile(
    r"^-{2,}\s*Forwarded message\s*-{2,}$", re.IGNORECASE | re.MULTILINE
)


def html_to_text(html: str) -> str:
    text = SCRIPT_STYLE_RE.sub(" ", html)
    text = BREAK_RE.sub("\n", text)
    text = TAG_RE.sub(" ", text)
    return normalize_text(unescape(text))


def normalize_text(text: str) -> str:
    lines = []
    for raw_line in (
        unescape(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ):
        line = WHITESPACE_RE.sub(" ", raw_line).strip()
        if line.startswith(">"):
            continue
        lines.append(line)
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n\s*\n", "\n\n", normalized).strip()
    return BLANK_LINES_RE.sub("\n\n", normalized)


def cleanup_email_body(text_body: str, html_body: str, *, limit: int) -> str:
    body = normalize_text(text_body) if text_body.strip() else html_to_text(html_body)
    forwarded_match = FORWARDED_HEADER_RE.search(body)
    if forwarded_match and forwarded_match.start() > 200:
        body = body[: forwarded_match.start()].strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"
