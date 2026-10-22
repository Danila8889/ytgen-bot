"""Structured logging without secrets or PII: masking helpers, the redaction filter, the JSON shape."""
from __future__ import annotations

import io
import json
import logging

from ytgen_bot.logging_setup import JsonFormatter, RedactingFilter, setup_logging
from ytgen_bot.masking import mask_code, mask_email, scrub_text

TOKEN = "4242:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN"
KEY = "k" * 40


def test_mask_email():
    assert mask_email("john.smith@example.com") == "j***@example.com"
    assert mask_email("a@b.co") == "a***@b.co"
    assert mask_email("garbage") == "***"


def test_mask_code_keeps_nothing_but_the_length():
    masked = mask_code("123456")
    assert "123456" not in masked and "6" in masked


def test_scrub_text_masks_emails_and_tokens_inside_free_text():
    text = scrub_text(f"failed for john.smith@example.com using {TOKEN}")
    assert "john.smith" not in text and "j***@example.com" in text
    assert TOKEN not in text and "<bot-token>" in text


def _logger(stream: io.StringIO, secrets=(TOKEN, KEY)) -> logging.Logger:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(secrets))
    logger = logging.getLogger(f"test.{id(stream)}")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


def test_json_line_shape_and_extras():
    stream = io.StringIO()
    _logger(stream).info("checkout_created", extra={"tg_user": 7, "payment_id": "p1"})
    line = json.loads(stream.getvalue())
    assert line["msg"] == "checkout_created" and line["tg_user"] == 7 and line["level"] == "INFO"


def test_secrets_never_survive_the_filter_even_in_tracebacks_and_extras():
    stream = io.StringIO()
    log = _logger(stream)
    try:
        raise RuntimeError(f"boom while calling with {KEY} and {TOKEN}")
    except RuntimeError:
        log.exception("failed %s", TOKEN, extra={"detail": f"key={KEY}"})
    out = stream.getvalue()
    assert TOKEN not in out and KEY not in out
    assert "<redacted>" in out


def test_emails_in_messages_are_masked_by_the_filter():
    stream = io.StringIO()
    _logger(stream).warning("user typed john.smith@example.com")
    assert "john.smith" not in stream.getvalue() and "j***@example.com" in stream.getvalue()


def test_setup_logging_installs_one_json_handler_and_quiets_httpx(capsys):
    setup_logging("INFO", [TOKEN])
    logging.getLogger("ytgen_bot").info("hello", extra={"tg_user": 1})
    logging.getLogger("httpx").info("GET https://example.test/secret-path")
    out = capsys.readouterr().out
    assert '"msg": "hello"' in out and "secret-path" not in out
    assert len(logging.getLogger().handlers) == 1
