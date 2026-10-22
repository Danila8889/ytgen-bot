#!/usr/bin/env bash
# Post-deploy check, used by both the deploy and the rollback path of ci.yml:
#   1. the bot container exists and becomes `running` + `healthy` (restart loops never get there),
#   2. `python -m ytgen_bot selfcheck` inside it passes: getMe against Telegram and one authenticated,
#      read-only call to the wrapper (proves the token, the URL and that BOT_SERVICE_API_KEY matches).
# Prints no secrets: the bot's logs go through its redaction filter and the check prints reasons only.
set -euo pipefail

cid=$(docker compose ps -q bot)
if [ -z "${cid}" ]; then
  echo "::error::the bot container does not exist"
  exit 1
fi

ok=""
for _ in $(seq 1 24); do
  state=$(docker inspect -f '{{.State.Status}}' "${cid}")
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${cid}")
  if [ "${state}" = "running" ] && [ "${health}" = "healthy" ]; then
    ok="1"
    break
  fi
  sleep 5
done

if [ -z "${ok}" ]; then
  echo "::error::the bot container is not running+healthy (state=${state:-?} health=${health:-?}); last log lines:"
  docker compose logs --tail 30 bot || true
  exit 1
fi

if ! docker compose exec -T bot /app/.venv/bin/python -m ytgen_bot selfcheck; then
  echo "::error::bot self-check failed (getMe and/or the wrapper call); last log lines:"
  docker compose logs --tail 30 bot || true
  exit 1
fi
echo "bot container healthy, self-check passed"
