"""Masking of personal data for logs and for echoing the user's own input back."""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Telegram bot tokens: <digits>:<35 url-safe chars>.
TOKEN_RE = re.compile(r"\b\d{3,}:[A-Za-z0-9_-]{30,}\b")


def mask_email(email: str) -> str:
    """`john@example.com` -> `j***@example.com` (the wrapper's own masking shape)."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def mask_code(code: str) -> str:
    """A one-time code is never logged, not even partly: only its length survives."""
    return f"<code len={len(code)}>"


def scrub_text(text: str) -> str:
    """Last line of defence for free text headed to a log: emails and bot tokens are replaced."""
    text = EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    return TOKEN_RE.sub("<bot-token>", text)
