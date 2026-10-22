"""`python -m ytgen_bot` runs the bot; `selfcheck` is the deploy check; `migrate`, `funnel-report`, `ai-sample [--n N]`
and `ai-replay` are operator commands (see README "Commands")."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import cli
from .app import SelfCheckError, run_bot, run_selfcheck
from .config import ConfigError, load_settings
from .logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ytgen_bot", exit_on_error=False)
    cli.add_arguments(parser)
    try:
        parsed = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except (argparse.ArgumentError, SystemExit):
        print("use: run | selfcheck | migrate | funnel-report | ai-sample [--n N] | ai-replay", file=sys.stderr)
        return 2
    command = parsed.command
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)  # names variables, never values
        return 2
    setup_logging(settings.log_level, settings.secrets)
    log = logging.getLogger("ytgen_bot")
    try:
        if command == "migrate":
            return cli.run_migrate(settings)
        if command == "funnel-report":
            return cli.run_report(settings)
        if command == "ai-sample":
            return cli.run_ai_sample(settings, parsed.n)
        if command == "ai-replay":
            return cli.run_ai_replay(settings)
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
