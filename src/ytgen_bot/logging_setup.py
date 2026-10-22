"""Structured (JSON-lines) logging with a redaction filter.

Rules: only the Telegram user id identifies a person in a log line; an email is masked, a code is never
written, and a configured secret (bot token, service key) that somehow reaches a message is replaced.
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable

from .masking import scrub_text

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def _clean(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return scrub_text(text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._clean(record.getMessage())
        record.args = None
        if record.exc_info:
            # Render now so the traceback text can be cleaned as well (it can echo URLs and values).
            record.exc_text = self._clean(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, str):
                record.__dict__[key] = self._clean(value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        elif record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO; nothing in them is needed here.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
