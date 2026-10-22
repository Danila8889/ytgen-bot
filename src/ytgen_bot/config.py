"""Environment configuration, validated at startup (fail fast, name the variable, never its value)."""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from urllib.parse import urlparse

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TOKEN_RE = re.compile(r"^\d{3,}:[A-Za-z0-9_-]{20,}$")
# The wrapper treats a bot key shorter than this as "feature off" (every call 503), so a shorter
# value here can never work; refuse it at startup instead of failing on the first call.
MIN_SERVICE_KEY_LENGTH = 32
_MAX_SUPPORT_CONTACT = 100


class ConfigError(RuntimeError):
    """Raised at startup; the message names variables, never values."""


@dataclasses.dataclass(frozen=True)
class Settings:
    bot_token: str = dataclasses.field(repr=False)
    service_key: str = dataclasses.field(repr=False)
    wrapper_base_url: str
    support_contact: str
    log_level: str = "INFO"
    heartbeat_file: str = "/tmp/ytgen-bot.heartbeat"

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.bot_token, self.service_key)


def _required(env: Mapping[str, str], name: str, problems: list[str]) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        problems.append(f"{name} is missing or empty")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    problems: list[str] = []

    bot_token = _required(env, "BOT_TOKEN", problems)
    if bot_token and _TOKEN_RE.fullmatch(bot_token) is None:
        problems.append("BOT_TOKEN does not look like a Telegram bot token")

    service_key = _required(env, "BOT_SERVICE_API_KEY", problems)
    if service_key and len(service_key) < MIN_SERVICE_KEY_LENGTH:
        problems.append(
            f"BOT_SERVICE_API_KEY is shorter than {MIN_SERVICE_KEY_LENGTH} characters "
            "(the wrapper would treat the bot API as disabled)"
        )

    base_url = _required(env, "WRAPPER_BASE_URL", problems).rstrip("/")
    if base_url:
        try:
            parsed = urlparse(base_url)
            host = parsed.hostname or ""
            parsed.port  # noqa: B018 - raises ValueError on a malformed port
        except ValueError:
            problems.append("WRAPPER_BASE_URL is not a valid URL")
        else:
            if parsed.scheme not in ("http", "https") or not host:
                problems.append("WRAPPER_BASE_URL must be an http(s) URL with a host")
            elif parsed.scheme == "http" and host not in _LOOPBACK_HOSTS:
                problems.append(
                    "WRAPPER_BASE_URL must be https (plain http is accepted for loopback only: "
                    "the service key travels in a header)"
                )
            elif parsed.username or parsed.password or parsed.query or parsed.fragment:
                problems.append("WRAPPER_BASE_URL must not carry credentials, a query or a fragment")

    support = _required(env, "SUPPORT_CONTACT", problems)
    if support and (len(support) > _MAX_SUPPORT_CONTACT or "\n" in support or "\r" in support):
        problems.append(f"SUPPORT_CONTACT must be one line of at most {_MAX_SUPPORT_CONTACT} characters")

    log_level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        problems.append("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR")

    if problems:
        raise ConfigError("Invalid configuration: " + "; ".join(problems))

    heartbeat = (env.get("HEARTBEAT_FILE") or "/tmp/ytgen-bot.heartbeat").strip()
    return Settings(
        bot_token=bot_token,
        service_key=service_key,
        wrapper_base_url=base_url,
        support_contact=support,
        log_level=log_level,
        heartbeat_file=heartbeat,
    )
