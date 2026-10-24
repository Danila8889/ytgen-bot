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
        capture_output=True, text=True, check=False,
    )


GOOD_PROD = (
    "WRAPPER_BASE_URL=https://api.example.test\nSUPPORT_CONTACT=@support\nLOG_LEVEL=INFO\n"
    "POSTGRES_DB=ytgen_bot\nPOSTGRES_USER=ytgen_bot\nKIE_API_URL=https://api.kie.example\n"
    "LLM_MODEL=gemini-3-8-flash-openai\nRCLONE_REMOTE=remote:bucket/ytgen-bot\n"
)
DB_PASSWORD = "d" * 32
GOOD_ENV = {
    "BOT_TOKEN": TOKEN, "BOT_SERVICE_API_KEY": KEY, "BOT_DB_PASSWORD": DB_PASSWORD,
    "KIE_API_KEY": "kie-test-key-value", "MANAGER_CHAT_ID": "-1001234567890",
}
SECRET_NAMES = tuple(GOOD_ENV)


def test_renders_both_layers(tmp_path):
    result = render(tmp_path, GOOD_ENV, GOOD_PROD)
    assert result.returncode == 0, result.stderr + result.stdout
    rendered = (tmp_path / ".env").read_text()
    assert "WRAPPER_BASE_URL=https://api.example.test" in rendered
    assert f"BOT_TOKEN={TOKEN}" in rendered and f"BOT_SERVICE_API_KEY={KEY}" in rendered
    for name, value in GOOD_ENV.items():
        assert f"{name}={value}" in rendered
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0  # not readable by others


@pytest.mark.parametrize("missing", SECRET_NAMES)
def test_missing_secret_fails_naming_it(tmp_path, missing):
    env = {k: v for k, v in GOOD_ENV.items() if k != missing}
    result = render(tmp_path, env, GOOD_PROD)
    assert result.returncode != 0
    assert f"::error::{missing} is empty" in result.stdout
    assert not (tmp_path / ".env").exists()
    assert all(value not in result.stdout for value in GOOD_ENV.values())  # names, never values


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
    assert set(env) >= {*SECRET_NAMES, "WRAPPER_BASE_URL", "SUPPORT_CONTACT", "POSTGRES_DB", "POSTGRES_USER",
                        "KIE_API_URL", "LLM_MODEL", "RCLONE_REMOTE"}
    assert env["SUPPORT_CONTACT"] == "@togn15" and env["WRAPPER_BASE_URL"].startswith("https://")
    sys.path.insert(0, str(ROOT / "src"))
    from ytgen_bot.config import load_settings

    settings = load_settings({**os.environ, **env})
    assert settings.support_contact == "@togn15"


def test_the_committed_env_production_has_no_secret_names():
    keys = {line.split("=", 1)[0] for line in (ROOT / ".env.production").read_text().splitlines()
            if line and not line.startswith("#")}
    assert not keys & set(SECRET_NAMES)


def test_short_key_and_odd_token_are_refused_without_echoing_values(tmp_path):
    result = render(tmp_path, {"BOT_TOKEN": "nope", "BOT_SERVICE_API_KEY": "short"}, GOOD_PROD)
    assert result.returncode != 0
    assert "shorter than 32" in result.stdout and "does not look like a Telegram bot token" in result.stdout
    assert "nope" not in result.stdout


def test_compose_special_characters_in_plain_values_are_refused(tmp_path):
    result = render(tmp_path, GOOD_ENV, "WRAPPER_BASE_URL=https://a.test\nSUPPORT_CONTACT=$HOME\n")
    assert result.returncode != 0 and "SUPPORT_CONTACT" in result.stdout


def test_compose_publishes_no_ports_anywhere_and_orders_db_migrate_bot():
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert list(compose["services"]) == ["db", "migrate", "bot"]
    for name, service in compose["services"].items():
        assert "ports" not in service, f"{name} must not publish a host port (the VPS host ports are all taken)"
        assert "network_mode" not in service  # compose-internal network only
    db, migrate, bot = (compose["services"][n] for n in ("db", "migrate", "bot"))
    assert db["image"].startswith("postgres:16") and "healthcheck" in db
    assert db["volumes"] == ["ytgen_bot_pgdata:/var/lib/postgresql/data"] and "ytgen_bot_pgdata" in compose["volumes"]
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy" and migrate["restart"] == "no"
    assert "migrate" in migrate["command"]
    assert bot["depends_on"]["db"]["condition"] == "service_healthy"
    assert bot["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert bot["restart"] == "unless-stopped"
    # the db password comes from the BOT_DB_PASSWORD secret, and a missing one stops compose by name
    assert "BOT_DB_PASSWORD" in db["environment"]["POSTGRES_PASSWORD"] and ":?" in db["environment"]["POSTGRES_PASSWORD"]


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
    # setup-uv must not create/activate a venv at the workspace root (breaks the 2nd run, pollutes the deploy checkout)
    setup = next(step for step in test["steps"] if str(step.get("uses", "")).startswith("astral-sh/setup-uv"))
    assert "python-version" not in setup["with"] and setup["with"]["activate-environment"] is False
    assert test["env"]["UV_PROJECT_ENVIRONMENT"].endswith("/.ci-test-src/.venv")
    assert test["env"]["UV_PYTHON"] == "3.12"
    assert any(step.get("run") == "uv run ruff check" for step in test["steps"])
    assert "always() && !cancelled() && (steps.deploy.outcome == 'failure' || steps.healthcheck.outcome == 'failure')" in text
    assert set(deploy["env"]) == set(SECRET_NAMES)
    assert wf[True]["push"]["branches"] == ["main"]  # `on:` parses as the boolean key True


@pytest.mark.parametrize("bad", ["short", "has space-and-more-characters!", "x" * 15])
def test_weak_or_odd_db_password_is_refused_without_echoing_it(tmp_path, bad):
    result = render(tmp_path, {**GOOD_ENV, "BOT_DB_PASSWORD": bad}, GOOD_PROD)
    assert result.returncode != 0 and "BOT_DB_PASSWORD" in result.stdout
    assert bad not in result.stdout


@pytest.mark.parametrize("bad", ["@manager", "12", "12 34", "-"])
def test_non_numeric_manager_chat_id_is_refused(tmp_path, bad):
    result = render(tmp_path, {**GOOD_ENV, "MANAGER_CHAT_ID": bad}, GOOD_PROD)
    assert result.returncode != 0 and "MANAGER_CHAT_ID" in result.stdout


def test_kie_key_with_compose_special_characters_is_refused(tmp_path):
    result = render(tmp_path, {**GOOD_ENV, "KIE_API_KEY": "abc$def"}, GOOD_PROD)
    assert result.returncode != 0 and "KIE_API_KEY" in result.stdout and "abc$def" not in result.stdout


@pytest.mark.parametrize("missing", ["POSTGRES_DB", "POSTGRES_USER", "KIE_API_URL", "LLM_MODEL", "RCLONE_REMOTE"])
def test_missing_plain_db_ai_or_backup_value_fails_naming_it(tmp_path, missing):
    prod = "\n".join(line for line in GOOD_PROD.splitlines() if not line.startswith(missing + "="))
    result = render(tmp_path, GOOD_ENV, prod)
    assert result.returncode != 0 and f"{missing} is missing or empty" in result.stdout


def test_nothing_is_written_when_any_new_secret_is_missing(tmp_path):
    env = {k: v for k, v in GOOD_ENV.items() if k != "KIE_API_KEY"}
    result = render(tmp_path, env, GOOD_PROD)
    assert result.returncode != 0 and not (tmp_path / ".env").exists()
    assert "gh secret set KIE_API_KEY" in result.stdout


def test_deploy_and_rollback_archive_container_logs_before_recreating_them():
    """Docker deletes a container's log when compose removes it: the deploy ships
    the running containers' logs between build and `up`, the rollback before its
    checkout (core's ship_container_logs.sh, ytgen-infra hosts.md "Log archive")."""
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = wf["jobs"]["deploy"]["steps"]
    deploy = next(s for s in steps if s.get("id") == "deploy")
    run = deploy["run"]
    assert deploy["env"]["LOG_SHIPPER"].endswith("/YouTubeGen/scripts/ship_container_logs.sh")
    assert run.index("docker compose build") < run.index('"$LOG_SHIPPER" --name ytgen-bot') < run.index("docker compose up -d")
    assert "--build" not in run.split("docker compose up -d", 1)[1].splitlines()[0]
    rollback = next(s for s in steps if str(s.get("name", "")).startswith("Roll back"))["run"]
    assert rollback.index("ship_container_logs.sh --name ytgen-bot") < rollback.index("git checkout")
