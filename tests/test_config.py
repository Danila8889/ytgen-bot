from __future__ import annotations

import pytest

from ytgen_bot.config import ConfigError, load_settings

GOOD = {
    "BOT_TOKEN": "4242:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN",
    "BOT_SERVICE_API_KEY": "k" * 40,
    "WRAPPER_BASE_URL": "https://api.example.test/",
    "SUPPORT_CONTACT": "@support",
}


def test_valid_config():
    s = load_settings(GOOD)
    assert s.wrapper_base_url == "https://api.example.test"  # trailing slash stripped
    assert s.support_contact == "@support" and s.log_level == "INFO"


def test_secrets_are_not_in_repr():
    assert GOOD["BOT_TOKEN"] not in repr(load_settings(GOOD))
    assert GOOD["BOT_SERVICE_API_KEY"] not in repr(load_settings(GOOD))


@pytest.mark.parametrize("name", ["BOT_TOKEN", "BOT_SERVICE_API_KEY", "WRAPPER_BASE_URL", "SUPPORT_CONTACT"])
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
