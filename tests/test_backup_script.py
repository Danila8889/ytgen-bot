"""scripts/backup_db.sh against fake `docker` and `rclone`: names, env parsing, upload, prune, failures."""
from __future__ import annotations

import pathlib
import re
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The pattern ytgen-infra's backup watchdog applies to remote object names (scripts/backup_watchdog.sh `ok_name`).
WATCHDOG_NAME = r"^{db}_([0-9]+_[0-9]+_)?[A-Za-z0-9]+\.dump$"

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
[ "${FAKE_DUMP_FAIL:-}" = "1" ] && { echo "pg_dump: error" >&2; exit 1; }
[ "${FAKE_DUMP_EMPTY:-}" = "1" ] && exit 0
printf 'PGDMP fake dump of the funnel database'
"""
FAKE_RCLONE = r"""#!/usr/bin/env bash
echo "rclone $*" >> "$FAKE_LOG"
[ "${FAKE_RCLONE_FAIL:-}" = "1" ] && { echo "rclone: 403 Forbidden" >&2; exit 1; }
if [ "$1" = "copy" ]; then cp "$2" "$FAKE_UPLOADS/"; fi
"""


def run(tmp_path: pathlib.Path, env_text: str | None, **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("rclone", FAKE_RCLONE)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "uploads").mkdir()
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "backup_db.sh"
    script.write_text((ROOT / "scripts" / "backup_db.sh").read_text())
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text)
    return subprocess.run(
        ["bash", str(script)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(tmp_path / "calls.log"),
             "FAKE_UPLOADS": str(tmp_path / "uploads"), "TMPDIR": str(tmp_path), **env},
        capture_output=True, text=True, check=False, cwd=tmp_path,
    )


GOOD = (
    "BOT_TOKEN=4242:SECRETTOKENSECRETTOKENSECRETTOKEN\nBOT_DB_PASSWORD=supersecretpassword123\n"
    "POSTGRES_DB=ytgen_bot\nPOSTGRES_USER=ytgen_bot\nRCLONE_REMOTE=beget:bucket/ytgen-bot\nLOG_LEVEL=INFO\n"
)


def calls(tmp_path: pathlib.Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def test_dumps_inside_the_db_container_uploads_and_prunes_this_databases_dumps_only(tmp_path):
    result = run(tmp_path, GOOD)
    assert result.returncode == 0, result.stdout + result.stderr
    log = calls(tmp_path)
    assert log[0] == "docker compose exec -T db pg_dump -U ytgen_bot -Fc ytgen_bot"
    copy = next(line for line in log if line.startswith("rclone copy"))
    assert copy.endswith("beget:bucket/ytgen-bot/ --s3-no-check-bucket")
    prune = next(line for line in log if line.startswith("rclone delete"))
    assert "beget:bucket/ytgen-bot/" in prune and "--min-age 14d" in prune and "--include ytgen_bot_*.dump" in prune
    (uploaded,) = list((tmp_path / "uploads").iterdir())
    assert re.fullmatch(r"ytgen_bot_\d{8}_\d{6}_[0-9a-f]{8}\.dump", uploaded.name)
    assert re.fullmatch(WATCHDOG_NAME.format(db="ytgen_bot"), uploaded.name)  # the existing watchdog recognises it
    assert uploaded.read_bytes().startswith(b"PGDMP")


def test_reads_the_db_name_and_remote_from_the_env_file_without_sourcing_it(tmp_path):
    env = GOOD.replace("POSTGRES_DB=ytgen_bot", "POSTGRES_DB=other_db") + "WEIRD=$(touch pwned)\n"
    result = run(tmp_path, env)
    assert result.returncode == 0
    assert not (tmp_path / "pwned").exists()  # parsed, never evaluated
    assert (next((tmp_path / "uploads").iterdir())).name.startswith("other_db_")


def test_retention_days_can_be_overridden_in_the_env_file(tmp_path):
    run(tmp_path, GOOD + "BACKUP_RETENTION_DAYS=30\n")
    assert any("--min-age 30d" in line for line in calls(tmp_path))


@pytest.mark.parametrize("missing", ["POSTGRES_DB", "POSTGRES_USER", "RCLONE_REMOTE"])
def test_a_missing_setting_fails_naming_it_and_uploads_nothing(tmp_path, missing):
    env = "\n".join(line for line in GOOD.splitlines() if not line.startswith(missing + "=")) + "\n"
    result = run(tmp_path, env)
    assert result.returncode != 0 and missing in result.stderr
    assert not any(line.startswith("rclone") for line in calls(tmp_path))


def test_a_missing_env_file_fails(tmp_path):
    result = run(tmp_path, None)
    assert result.returncode != 0 and "not found" in result.stderr


@pytest.mark.parametrize("failure", [{"FAKE_DUMP_FAIL": "1"}, {"FAKE_DUMP_EMPTY": "1"}, {"FAKE_RCLONE_FAIL": "1"}])
def test_any_failure_is_a_nonzero_exit_so_cron_and_the_watchdog_notice(tmp_path, failure):
    result = run(tmp_path, GOOD, **failure)
    assert result.returncode != 0
    assert not list((tmp_path / "uploads").iterdir())


def test_secrets_are_never_printed(tmp_path):
    result = run(tmp_path, GOOD)
    out = result.stdout + result.stderr + "\n".join(calls(tmp_path))
    assert "supersecretpassword123" not in out and "SECRETTOKEN" not in out


def test_a_bad_db_name_is_refused(tmp_path):
    result = run(tmp_path, GOOD.replace("POSTGRES_DB=ytgen_bot", "POSTGRES_DB=a;b"))
    assert result.returncode != 0 and not calls(tmp_path)
