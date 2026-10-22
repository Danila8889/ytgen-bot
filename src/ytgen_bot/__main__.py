"""`python -m ytgen_bot` runs the bot; `python -m ytgen_bot selfcheck` runs the deploy check and exits."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from .app import SelfCheckError, run_bot, run_selfcheck
from .config import ConfigError, load_settings
from .logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "run"
    if command not in ("run", "selfcheck"):
        print(f"unknown command {command!r}; use: run | selfcheck", file=sys.stderr)
        return 2
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)  # names variables, never values
        return 2
    setup_logging(settings.log_level, settings.secrets)
    log = logging.getLogger("ytgen_bot")
    try:
        asyncio.run(run_selfcheck(settings) if command == "selfcheck" else run_bot(settings))
    except SelfCheckError as exc:
        log.error("self_check_failed", extra={"reason": str(exc)})
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
