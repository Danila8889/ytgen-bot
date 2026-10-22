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
_MANAGER_CHAT_RE = re.compile(r"^-?\d{5,20}$")
_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class ConfigError(RuntimeError):
    """Raised at startup; the message names variables, never values."""


@dataclasses.dataclass(frozen=True)
class Settings:
    bot_token: str = dataclasses.field(repr=False)
    service_key: str = dataclasses.field(repr=False)
    wrapper_base_url: str
    support_contact: str
    # --- funnel database (own Postgres; the wrapper stays the system of record for accounts/billing) ---
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = dataclasses.field(repr=False)
    # --- AI layer (kie.ai chat completions, single provider, no failover: an interim MVP choice) ---
    kie_api_key: str = dataclasses.field(repr=False)
    kie_api_url: str
    llm_model: str
    # --- manager hand-off ---
    manager_chat_id: int = dataclasses.field(repr=False)
    log_level: str = "INFO"
    heartbeat_file: str = "/tmp/ytgen-bot.heartbeat"
    ai_user_hourly_limit: int = 10
    ai_daily_ceiling: int = 500
    llm_timeout_s: float = 25.0
    llm_max_tokens: int = 600
    llm_temperature: float = 0.2
    ai_breaker_failures: int = 5
    ai_breaker_cooldown_min: int = 10
    tariff_cache_ttl_s: int = 300

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.bot_token, self.service_key, self.kie_api_key, self.db_password, str(self.manager_chat_id))


def _required(env: Mapping[str, str], name: str, problems: list[str]) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        problems.append(f"{name} is missing or empty")
    return value


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int, problems: list[str]) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name} must be an integer")
        return default
    if not lo <= value <= hi:
        problems.append(f"{name} must be between {lo} and {hi}")
        return default
    return value


def _float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float, problems: list[str]) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        problems.append(f"{name} must be a number")
        return default
    if not lo <= value <= hi:
        problems.append(f"{name} must be between {lo} and {hi}")
        return default
    return value


def _https_url(value: str, name: str, problems: list[str]) -> str:
    value = value.rstrip("/")
    if not value:
        return value
    try:
        parsed = urlparse(value)
        parsed.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        problems.append(f"{name} is not a valid URL")
        return value
    if parsed.scheme != "https" or not parsed.hostname:
        problems.append(f"{name} must be an https URL with a host (a secret travels in a header)")
    elif parsed.username or parsed.password or parsed.query or parsed.fragment:
        problems.append(f"{name} must not carry credentials, a query or a fragment")
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

    db_host = (env.get("POSTGRES_HOST") or "db").strip()
    if _HOST_RE.fullmatch(db_host) is None:
        problems.append("POSTGRES_HOST is not a valid host name")
    db_port = _int(env, "POSTGRES_PORT", 5432, 1, 65535, problems)
    db_name = _required(env, "POSTGRES_DB", problems)
    if db_name and _DB_NAME_RE.fullmatch(db_name) is None:
        problems.append("POSTGRES_DB must be a plain identifier ([A-Za-z_][A-Za-z0-9_]*)")
    db_user = _required(env, "POSTGRES_USER", problems)
    if db_user and _DB_NAME_RE.fullmatch(db_user) is None:
        problems.append("POSTGRES_USER must be a plain identifier ([A-Za-z_][A-Za-z0-9_]*)")
    db_password = _required(env, "BOT_DB_PASSWORD", problems)

    kie_key = _required(env, "KIE_API_KEY", problems)
    kie_url = _https_url(_required(env, "KIE_API_URL", problems), "KIE_API_URL", problems)
    model = _required(env, "LLM_MODEL", problems)
    if model and _MODEL_RE.fullmatch(model) is None:
        problems.append("LLM_MODEL must match [A-Za-z0-9._:-]{1,64}")

    manager_raw = _required(env, "MANAGER_CHAT_ID", problems)
    manager_chat_id = 0
    if manager_raw:
        if _MANAGER_CHAT_RE.fullmatch(manager_raw) is None:
            problems.append("MANAGER_CHAT_ID must be a numeric Telegram chat id")
        else:
            manager_chat_id = int(manager_raw)

    ai_user_hourly = _int(env, "AI_USER_HOURLY_LIMIT", 10, 1, 1000, problems)
    ai_daily = _int(env, "AI_DAILY_CEILING", 500, 1, 1_000_000, problems)
    llm_timeout = _float(env, "LLM_TIMEOUT_S", 25.0, 1.0, 120.0, problems)
    llm_max_tokens = _int(env, "LLM_MAX_TOKENS", 600, 50, 4000, problems)
    llm_temperature = _float(env, "LLM_TEMPERATURE", 0.2, 0.0, 1.0, problems)
    breaker_failures = _int(env, "AI_BREAKER_FAILURES", 5, 1, 100, problems)
    breaker_cooldown = _int(env, "AI_BREAKER_COOLDOWN_MIN", 10, 1, 1440, problems)
    tariff_ttl = _int(env, "TARIFF_CACHE_TTL_S", 300, 5, 86400, problems)

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
        db_host=db_host,
        db_port=db_port,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        kie_api_key=kie_key,
        kie_api_url=kie_url,
        llm_model=model,
        manager_chat_id=manager_chat_id,
        log_level=log_level,
        heartbeat_file=heartbeat,
        ai_user_hourly_limit=ai_user_hourly,
        ai_daily_ceiling=ai_daily,
        llm_timeout_s=llm_timeout,
        llm_max_tokens=llm_max_tokens,
        llm_temperature=llm_temperature,
        ai_breaker_failures=breaker_failures,
        ai_breaker_cooldown_min=breaker_cooldown,
        tariff_cache_ttl_s=tariff_ttl,
    )
