"""scripts/post_deploy_check.sh against a fake `docker`: db healthy, migrate exit 0, bot healthy, self-check."""
from __future__ import annotations

import pathlib
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

FAKE_DOCKER = r"""#!/usr/bin/env bash
# $FAKE_* variables decide what the pretend containers report. `sleep` is shimmed away by the test.
args="$*"
case "$args" in
  "compose ps -a -q db")      echo "cid-db" ;;
  "compose ps -a -q migrate") [ "${FAKE_MIGRATE_MISSING:-}" = "1" ] || echo "cid-migrate" ;;
  "compose ps -a -q bot")     echo "cid-bot" ;;
  "inspect -f {{.State.Status}} cid-db")  echo "${FAKE_DB_STATE:-running}" ;;
  "inspect -f {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} cid-db") echo "${FAKE_DB_HEALTH:-healthy}" ;;
  "inspect -f {{.State.Status}} cid-migrate")   echo "${FAKE_MIGRATE_STATE:-exited}" ;;
  "inspect -f {{.State.ExitCode}} cid-migrate") echo "${FAKE_MIGRATE_CODE:-0}" ;;
  "inspect -f {{.State.Status}} cid-bot")  echo "${FAKE_BOT_STATE:-running}" ;;
  "inspect -f {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} cid-bot") echo "${FAKE_BOT_HEALTH:-healthy}" ;;
  "compose exec -T bot /app/.venv/bin/python -m ytgen_bot selfcheck")
      echo "selfcheck ran" >> "$FAKE_LOG"; exit "${FAKE_SELFCHECK_CODE:-0}" ;;
  compose\ logs*) echo "fake log line" ;;
  *) echo "unexpected docker call: $args" >&2; exit 99 ;;
esac
"""


def run(tmp_path: pathlib.Path, **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "post_deploy_check.sh")],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(log), **env},
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )


def selfchecks(tmp_path: pathlib.Path) -> int:
    log = tmp_path / "calls.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_everything_healthy_runs_the_self_check_and_passes(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert selfchecks(tmp_path) == 1 and "migrations applied" in result.stdout


@pytest.mark.parametrize("env,needle", [
    ({"FAKE_DB_HEALTH": "unhealthy"}, "db container is not running+healthy"),
    ({"FAKE_DB_STATE": "restarting"}, "db container is not running+healthy"),
    ({"FAKE_MIGRATE_CODE": "1"}, "database migrations did not complete"),
    ({"FAKE_MIGRATE_STATE": "running"}, "database migrations did not complete"),
    ({"FAKE_MIGRATE_MISSING": "1"}, "migrate container does not exist"),
    ({"FAKE_BOT_HEALTH": "starting"}, "bot container is not running+healthy"),
    ({"FAKE_SELFCHECK_CODE": "1"}, "bot self-check failed"),
])
def test_each_failure_stops_the_deploy_with_a_named_reason(tmp_path, env, needle):
    result = run(tmp_path, **env)
    assert result.returncode != 0 and needle in result.stdout, result.stdout + result.stderr


def test_a_bad_migration_stops_before_the_bot_or_the_self_check_are_looked_at(tmp_path):
    result = run(tmp_path, FAKE_MIGRATE_CODE="3", FAKE_BOT_HEALTH="unhealthy")
    assert result.returncode != 0 and "migrations did not complete" in result.stdout
    assert "bot container" not in result.stdout and selfchecks(tmp_path) == 0


def test_the_script_never_calls_the_llm_provider():
    text = (ROOT / "scripts" / "post_deploy_check.sh").read_text()
    assert "kie" not in text.lower().replace("no live kie.ai call", "")
