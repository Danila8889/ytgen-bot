"""An ephemeral Postgres for the test run: never a production database, never a well-known host port.

Preferred: a throw-away `postgres:16-alpine` container published on 127.0.0.1 with a docker-chosen random port
(32768+; the VPS host's own 5432/5433/5434/6379/6380/8088/8090/3000/8080/8100/5555/8081 are all far below).
Fallback for a machine without docker: the local `initdb`/`postgres` binaries on a random free loopback port with
the socket and data in a temp dir. Everything is torn down when the test session ends.
"""
from __future__ import annotations

import contextlib
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import URL

HOST_PORTS_IN_USE = {5432, 5433, 5434, 6379, 6380, 8088, 8090, 3000, 8080, 8100, 5555, 8081}
LABEL = "ytgen-bot-test-pg"
IMAGE = "postgres:16-alpine"
DB = "funnel_test"
USER = "funnel_test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port not in HOST_PORTS_IN_USE
    return port


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run(*cmd: str, timeout: int = 120) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    return done.stdout.strip()


@contextlib.contextmanager
def _docker_pg() -> Iterator[URL]:
    # Containers left behind by an earlier killed run (this runner executes one test job at a time).
    stale = _run("docker", "ps", "-aq", "--filter", f"label={LABEL}")
    if stale:
        subprocess.run(["docker", "rm", "-f", *stale.split()], capture_output=True, check=False)
    password = secrets.token_hex(12)
    name = f"{LABEL}-{uuid.uuid4().hex[:8]}"
    _run(
        "docker", "run", "-d", "--rm", "--name", name, "--label", LABEL,
        "-e", f"POSTGRES_PASSWORD={password}", "-e", f"POSTGRES_DB={DB}", "-e", f"POSTGRES_USER={USER}",
        "-p", "127.0.0.1::5432", "--tmpfs", "/var/lib/postgresql/data",
        IMAGE, "-c", "fsync=off", "-c", "max_connections=60",
    )
    try:
        mapping = ""
        for _ in range(40):  # the published port can lag the container start by a moment
            lines = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True, text=True,
                                   check=False).stdout.splitlines()
            if lines:
                mapping = lines[0]
                break
            time.sleep(0.25)
        assert mapping, "docker did not publish the postgres port"
        port = int(mapping.rsplit(":", 1)[1])
        assert port not in HOST_PORTS_IN_USE and mapping.startswith(("127.0.0.1", "0.0.0.0")), mapping
        deadline = time.monotonic() + 60
        while True:
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", USER, "-d", DB],
                capture_output=True, check=False,
            )
            if ready.returncode == 0:
                # the entrypoint restarts postgres once after init; wait until a real query works twice in a row
                time.sleep(1.0)
                again = subprocess.run(
                    ["docker", "exec", name, "psql", "-U", USER, "-d", DB, "-c", "select 1"],
                    capture_output=True, check=False,
                )
                if again.returncode == 0:
                    break
            if time.monotonic() > deadline:
                raise RuntimeError("ephemeral postgres did not become ready")
            time.sleep(0.5)
        yield URL.create("postgresql+asyncpg", username=USER, password=password, host="127.0.0.1", port=port, database=DB)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _local_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/opt/postgresql@16/bin", "/usr/local/opt/postgresql@16/bin", "/usr/lib/postgresql/16/bin"):
        candidate = Path(base) / name
        if candidate.exists():
            return str(candidate)
    return None


@contextlib.contextmanager
def _local_pg() -> Iterator[URL]:
    initdb, postgres = _local_bin("initdb"), _local_bin("postgres")
    if not initdb or not postgres:
        raise RuntimeError("neither docker nor local postgres binaries are available for the DB tests")
    with tempfile.TemporaryDirectory(prefix="ytgen-bot-pg-") as tmp:
        data = Path(tmp) / "data"
        sock = Path(tmp) / "sock"
        sock.mkdir()
        _run(initdb, "-D", str(data), "-U", USER, "--auth=trust", "-E", "UTF8")
        port = _free_port()
        proc = subprocess.Popen(
            [postgres, "-D", str(data), "-p", str(port), "-c", "listen_addresses=127.0.0.1",
             "-c", f"unix_socket_directories={sock}", "-c", "fsync=off"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("local postgres did not start")
                time.sleep(0.3)
            time.sleep(0.5)
            createdb = _local_bin("createdb")
            assert createdb
            _run(createdb, "-h", "127.0.0.1", "-p", str(port), "-U", USER, DB)
            yield URL.create("postgresql+asyncpg", username=USER, password="x", host="127.0.0.1", port=port, database=DB)
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            proc.kill()


@contextlib.contextmanager
def ephemeral_postgres() -> Iterator[URL]:
    with (_docker_pg() if _docker_ok() else _local_pg()) as url:
        yield url
