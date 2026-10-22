from __future__ import annotations

import pytest

from ytgen_bot.config import ConfigError, load_settings

GOOD = {
    "BOT_TOKEN": "4242:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN",
    "BOT_SERVICE_API_KEY": "k" * 40,
    "WRAPPER_BASE_URL": "https://api.example.test/",
    "SUPPORT_CONTACT": "@support",
    "POSTGRES_DB": "ytgen_bot",
    "POSTGRES_USER": "ytgen_bot",
    "BOT_DB_PASSWORD": "p" * 24,
    "KIE_API_KEY": "kie-secret-key-value",
    "KIE_API_URL": "https://api.kie.example",
    "LLM_MODEL": "gemini-3-8-flash-openai",
    "MANAGER_CHAT_ID": "-1001234567890",
}


def test_valid_config():
    s = load_settings(GOOD)
    assert s.wrapper_base_url == "https://api.example.test"  # trailing slash stripped
    assert s.support_contact == "@support" and s.log_level == "INFO"


def test_secrets_are_not_in_repr():
    assert GOOD["BOT_TOKEN"] not in repr(load_settings(GOOD))
    assert GOOD["BOT_SERVICE_API_KEY"] not in repr(load_settings(GOOD))


@pytest.mark.parametrize("name", [
    "BOT_TOKEN", "BOT_SERVICE_API_KEY", "WRAPPER_BASE_URL", "SUPPORT_CONTACT", "POSTGRES_DB", "POSTGRES_USER",
    "BOT_DB_PASSWORD", "KIE_API_KEY", "KIE_API_URL", "LLM_MODEL", "MANAGER_CHAT_ID",
])
def test_a_missing_variable_is_named(name):
    env = {k: v for k, v in GOOD.items() if k != name}
    with pytest.raises(ConfigError, match=name):
        load_settings(env)


def test_blank_values_count_as_missing():
    with pytest.raises(ConfigError, match="SUPPORT_CONTACT"):
        load_settings({**GOOD, "SUPPORT_CONTACT": "   "})


def test_all_problems_are_reported_at_once_without_values():
    with pytest.raises(ConfigError) as exc:
        load_settings({"BOT_SERVICE_API_KEY": "short-secret-value"})
    message = str(exc.value)
    assert "BOT_TOKEN" in message and "WRAPPER_BASE_URL" in message and "SUPPORT_CONTACT" in message
    assert "short-secret-value" not in message


def test_short_service_key_is_refused():
    with pytest.raises(ConfigError, match="BOT_SERVICE_API_KEY is shorter"):
        load_settings({**GOOD, "BOT_SERVICE_API_KEY": "k" * 31})


@pytest.mark.parametrize(
    "url",
    ["http://api.example.test", "ftp://x", "https://", "https://u:p@api.example.test", "https://a.test/?q=1", "nonsense"],
)
def test_unsafe_or_malformed_wrapper_url_is_refused(url):
    with pytest.raises(ConfigError, match="WRAPPER_BASE_URL"):
        load_settings({**GOOD, "WRAPPER_BASE_URL": url})


@pytest.mark.parametrize("url", ["http://127.0.0.1:8090", "http://localhost:8090", "https://api.example.test"])
def test_plain_http_only_for_loopback(url):
    assert load_settings({**GOOD, "WRAPPER_BASE_URL": url})


def test_bad_token_shape_is_refused_without_echoing_it():
    with pytest.raises(ConfigError) as exc:
        load_settings({**GOOD, "BOT_TOKEN": "not-a-token"})
    assert "not-a-token" not in str(exc.value)


def test_support_contact_must_be_one_line():
    with pytest.raises(ConfigError, match="SUPPORT_CONTACT"):
        load_settings({**GOOD, "SUPPORT_CONTACT": "a\nb"})


def test_new_settings_defaults_and_overrides():
    s = load_settings(GOOD)
    assert (s.db_host, s.db_port, s.db_name, s.db_user) == ("db", 5432, "ytgen_bot", "ytgen_bot")
    assert s.manager_chat_id == -1001234567890 and s.llm_model == "gemini-3-8-flash-openai"
    assert (s.ai_user_hourly_limit, s.ai_daily_ceiling) == (10, 500)
    tuned = load_settings({**GOOD, "AI_USER_HOURLY_LIMIT": "3", "AI_DAILY_CEILING": "50", "POSTGRES_HOST": "pg.internal",
                           "LLM_TEMPERATURE": "0.1"})
    assert (tuned.ai_user_hourly_limit, tuned.ai_daily_ceiling, tuned.db_host, tuned.llm_temperature) == (3, 50, "pg.internal", 0.1)


def test_new_secrets_are_not_in_repr_and_are_redacted_by_the_log_filter():
    s = load_settings(GOOD)
    shown = repr(s)
    for secret in (GOOD["BOT_DB_PASSWORD"], GOOD["KIE_API_KEY"], GOOD["MANAGER_CHAT_ID"]):
        assert secret not in shown
        assert secret in s.secrets


@pytest.mark.parametrize("name,value", [
    ("MANAGER_CHAT_ID", "@manager"), ("MANAGER_CHAT_ID", "12"), ("KIE_API_URL", "http://api.kie.example"),
    ("KIE_API_URL", "https://u:p@api.kie.example"), ("LLM_MODEL", "bad model/with space"),
    ("POSTGRES_DB", "db; drop"), ("POSTGRES_USER", "u u"), ("POSTGRES_HOST", "a b"), ("POSTGRES_PORT", "70000"),
    ("AI_USER_HOURLY_LIMIT", "0"), ("AI_DAILY_CEILING", "abc"), ("LLM_TEMPERATURE", "5"), ("LLM_TIMEOUT_S", "0"),
])
def test_invalid_new_values_name_the_variable_not_the_value(name, value):
    with pytest.raises(ConfigError, match=name) as exc:
        load_settings({**GOOD, name: value})
    assert value not in str(exc.value) or len(value) < 4
