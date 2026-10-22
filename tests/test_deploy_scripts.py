"""scripts/render_env.sh: a missing secret or empty required value fails the deploy loudly, by name."""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = "4242:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN"
KEY = "a" * 64


def render(tmp_path: pathlib.Path, env: dict[str, str], production: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "render_env.sh", tmp_path / "scripts" / "render_env.sh")
    (tmp_path / ".env.production").write_text(production)
    return subprocess.run(
        ["bash", "scripts/render_env.sh"], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True,
    )


GOOD_PROD = "WRAPPER_BASE_URL=https://api.example.test\nSUPPORT_CONTACT=@support\nLOG_LEVEL=INFO\n"
GOOD_ENV = {"BOT_TOKEN": TOKEN, "BOT_SERVICE_API_KEY": KEY}


def test_renders_both_layers(tmp_path):
    result = render(tmp_path, GOOD_ENV, GOOD_PROD)
    assert result.returncode == 0, result.stderr + result.stdout
    rendered = (tmp_path / ".env").read_text()
    assert "WRAPPER_BASE_URL=https://api.example.test" in rendered
    assert f"BOT_TOKEN={TOKEN}" in rendered and f"BOT_SERVICE_API_KEY={KEY}" in rendered
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0  # not readable by others


@pytest.mark.parametrize("missing", ["BOT_TOKEN", "BOT_SERVICE_API_KEY"])
def test_missing_secret_fails_naming_it(tmp_path, missing):
    env = {k: v for k, v in GOOD_ENV.items() if k != missing}
    result = render(tmp_path, env, GOOD_PROD)
    assert result.returncode != 0
    assert f"::error::{missing} is empty" in result.stdout
    assert not (tmp_path / ".env").exists()


def test_empty_secret_counts_as_missing(tmp_path):
    result = render(tmp_path, {**GOOD_ENV, "BOT_TOKEN": ""}, GOOD_PROD)
    assert result.returncode != 0 and "BOT_TOKEN" in result.stdout


def test_empty_support_contact_fails_naming_it(tmp_path):
    result = render(tmp_path, GOOD_ENV, "WRAPPER_BASE_URL=https://api.example.test\nSUPPORT_CONTACT=\n")
    assert result.returncode != 0
    assert "SUPPORT_CONTACT is missing or empty" in result.stdout


def test_the_committed_env_production_renders_a_deployable_env_with_dummy_secrets(tmp_path):
    """The real .env.production + dummy secrets -> an .env the bot's own config loader accepts."""
    import os
    import sys

    result = render(tmp_path, GOOD_ENV, (ROOT / ".env.production").read_text())
    assert result.returncode == 0, result.stdout + result.stderr
    env = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines() if "=" in line)
    assert set(env) >= {"BOT_TOKEN", "BOT_SERVICE_API_KEY", "WRAPPER_BASE_URL", "SUPPORT_CONTACT"}
    assert env["SUPPORT_CONTACT"] == "@togn15" and env["WRAPPER_BASE_URL"].startswith("https://")
    sys.path.insert(0, str(ROOT / "src"))
    from ytgen_bot.config import load_settings

    settings = load_settings({**os.environ, **env})
    assert settings.support_contact == "@togn15"


def test_the_committed_env_production_has_no_secret_names():
    keys = {line.split("=", 1)[0] for line in (ROOT / ".env.production").read_text().splitlines()
            if line and not line.startswith("#")}
    assert not keys & {"BOT_TOKEN", "BOT_SERVICE_API_KEY"}


def test_short_key_and_odd_token_are_refused_without_echoing_values(tmp_path):
    result = render(tmp_path, {"BOT_TOKEN": "nope", "BOT_SERVICE_API_KEY": "short"}, GOOD_PROD)
    assert result.returncode != 0
    assert "shorter than 32" in result.stdout and "does not look like a Telegram bot token" in result.stdout
    assert "nope" not in result.stdout


def test_compose_special_characters_in_plain_values_are_refused(tmp_path):
    result = render(tmp_path, GOOD_ENV, "WRAPPER_BASE_URL=https://a.test\nSUPPORT_CONTACT=$HOME\n")
    assert result.returncode != 0 and "SUPPORT_CONTACT" in result.stdout


def test_compose_file_publishes_no_ports_and_has_one_service():
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert list(compose["services"]) == ["bot"]
    assert "ports" not in compose["services"]["bot"]
    assert compose["services"]["bot"]["restart"] == "unless-stopped"


def test_workflow_deploys_only_on_dispatch_and_uses_the_rollback_pattern():
    import yaml

    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    wf = yaml.safe_load(text)
    deploy = wf["jobs"]["deploy"]
    assert deploy["if"] == "github.event_name == 'workflow_dispatch'"
    assert deploy["needs"] == "test" and deploy["runs-on"] == ["self-hosted", "ytgen-bot-vps"]
    test = wf["jobs"]["test"]
    assert test["runs-on"] == ["self-hosted", "ytgen-bot-vps"]
    assert "timeout-minutes" in test  # a hung test must not hold the only runner
    assert "concurrency" not in test  # only deploy queues; a test is never blocked by a queued deploy
    checkout = next(step for step in test["steps"] if str(step.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["path"] == ".ci-test-src"  # never touches the deploy workspace root
    assert all(step.get("working-directory") == ".ci-test-src" for step in test["steps"] if "run" in step)
    assert "always() && !cancelled() && (steps.deploy.outcome == 'failure' || steps.healthcheck.outcome == 'failure')" in text
    assert set(deploy["env"]) == {"BOT_TOKEN", "BOT_SERVICE_API_KEY"}
    assert wf[True]["push"]["branches"] == ["main"]  # `on:` parses as the boolean key True
