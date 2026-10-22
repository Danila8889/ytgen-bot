"""Docs stay current: every env var is documented everywhere it must be, and the interim-MVP note is where the owner asked."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
DOC = (ROOT / "documentation.md").read_text(encoding="utf-8")
CLAUDE = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
CONFIG = (ROOT / "src" / "ytgen_bot" / "config.py").read_text(encoding="utf-8")
EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

CONFIG_NAMES = set(re.findall(r'"([A-Z][A-Z0-9_]{3,})"', CONFIG))
# names that appear in config.py as plain strings but are not environment variables
CONFIG_NAMES -= {"DEBUG", "INFO", "WARNING", "ERROR"}
EXTRA_NAMES = {"RCLONE_REMOTE", "DOCKER_LOG_MAX_SIZE", "DOCKER_LOG_MAX_FILE"}  # read outside config.py


def test_every_config_variable_is_in_env_example_the_readme_and_the_architecture_doc():
    assert {"KIE_API_KEY", "BOT_DB_PASSWORD", "MANAGER_CHAT_ID", "LLM_MODEL", "POSTGRES_DB"} <= CONFIG_NAMES
    for name in sorted(CONFIG_NAMES | EXTRA_NAMES):
        assert f"{name}=" in EXAMPLE, f"{name} missing from .env.example"
        assert name in README, f"{name} missing from README.md"
        assert name in DOC, f"{name} missing from documentation.md"


def test_the_interim_mvp_note_is_in_the_readme_the_architecture_doc_and_the_env_table():
    for text in (README, DOC):
        assert "interim MVP choice" in text
        assert "has not yet been profiled or debugged for availability and cost" in text
        assert "single provider" in text and "no failover" in text
        assert "must be revisited" in text
    row = next(line for line in DOC.splitlines() if line.startswith("| `LLM_MODEL`"))
    assert "Interim MVP choice" in row and "no failover" in row
    assert "interim" in EXAMPLE.lower() and "no failover" in EXAMPLE.lower()


def test_the_amended_invariants_are_in_claude_md_and_the_old_stateless_claim_is_gone():
    assert "Stateful for funnel data only" in CLAUDE and "ISOLATION" in CLAUDE
    assert "A **stateless** Telegram bot" not in CLAUDE and "No database, no persisted state" not in CLAUDE
    assert "stateless" not in README.lower().replace("stateful for funnel data only", "")
    assert "## 2. What the bot remembers" in DOC and "**Funnel data**" in DOC


def test_the_three_new_secrets_the_commands_and_the_backup_steps_are_documented():
    for secret in ("KIE_API_KEY", "BOT_DB_PASSWORD", "MANAGER_CHAT_ID"):
        assert f"gh secret set {secret}" in DOC
    for command in ("funnel-report", "ai-sample --n 20", "ai-replay", "migrate"):
        assert command in README and command in DOC
    assert "crontab -e" in DOC and "scripts/backup_db.sh" in DOC and "backup_watchdog.sh" in DOC
    assert "additive only" in DOC.lower()


def test_the_static_facts_that_can_go_stale_are_listed():
    assert "## 13. Facts that can go stale" in DOC
    for fact in ("dubbing", "competitor-analysis", "music"):
        assert fact in DOC
