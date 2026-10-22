#!/usr/bin/env bash
# Post-deploy check, used by both the deploy and the rollback path of ci.yml:
#   1. the funnel database (`db`) is running and healthy,
#   2. the one-shot `migrate` service finished with exit code 0 (Alembic is at head; a failed migration stops the deploy),
#   3. the bot container exists and becomes `running` + `healthy` (restart loops never get there),
#   4. `python -m ytgen_bot selfcheck` inside it passes: getMe against Telegram, one authenticated read-only call to the
#      wrapper (proves the token, the URL and that BOT_SERVICE_API_KEY matches), the DB reachable and migrated to head,
#      and the wrapper's public tariff list valid for the funnel (is_monthly + capacity on every row). NO live kie.ai call:
#      the AI provider is checked by configuration only.
# Prints no secrets: the bot's logs go through its redaction filter and the check prints reasons only.
set -euo pipefail

service_id() {
  docker compose ps -a -q "$1" | head -n1
}

wait_for_healthy() {
  local name="$1" cid state health
  cid=$(service_id "${name}")
  if [ -z "${cid}" ]; then
    echo "::error::the ${name} container does not exist"
    exit 1
  fi
  for _ in $(seq 1 24); do
    state=$(docker inspect -f '{{.State.Status}}' "${cid}")
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}")
    if [ "${state}" = "running" ] && [ "${health}" = "healthy" ]; then
      return 0
    fi
    sleep 5
  done
  echo "::error::the ${name} container is not running+healthy (state=${state:-?} health=${health:-?}); last log lines:"
  docker compose logs --tail 30 "${name}" || true
  exit 1
}

# 1. database
wait_for_healthy db

# 2. migrations: the one-shot service must have exited 0
mid=$(service_id migrate)
if [ -z "${mid}" ]; then
  echo "::error::the migrate container does not exist"
  exit 1
fi
mig_state=$(docker inspect -f '{{.State.Status}}' "${mid}")
mig_code=$(docker inspect -f '{{.State.ExitCode}}' "${mid}")
if [ "${mig_state}" != "exited" ] || [ "${mig_code}" != "0" ]; then
  echo "::error::database migrations did not complete (state=${mig_state} exit=${mig_code}); last log lines:"
  docker compose logs --tail 30 migrate || true
  exit 1
fi

# 3. bot
wait_for_healthy bot

# 4. self-check
if ! docker compose exec -T bot /app/.venv/bin/python -m ytgen_bot selfcheck; then
  echo "::error::bot self-check failed (getMe, wrapper, database or tariff contract); last log lines:"
  docker compose logs --tail 30 bot || true
  exit 1
fi
echo "database healthy, migrations applied, bot container healthy, self-check passed"
