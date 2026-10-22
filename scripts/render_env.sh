#!/usr/bin/env bash
# Renders .env from two layers: .env.production (committed, non-secret) plus the GitHub secrets. Called from
# both the deploy path and the rollback path of ci.yml's `deploy` job, so the two cannot drift.
#
# The `deploy` job maps each secret into the process environment (`env:` block of the job), because
# GitHub exposes secrets only through literal `${{ secrets.NAME }}` references in the workflow YAML.
#
# Fails loudly, naming the variable (never its value), when anything the bot needs is missing:
#   - a secret that is absent or empty,
#   - a required non-secret value in .env.production that is empty.
set -euo pipefail

SECRET_KEYS=(
  BOT_TOKEN
  BOT_SERVICE_API_KEY
)
REQUIRED_PLAIN_KEYS=(
  WRAPPER_BASE_URL
  SUPPORT_CONTACT
)

[ -f .env.production ] || { echo "::error::.env.production is missing"; exit 1; }

problems=0
for key in "${SECRET_KEYS[@]}"; do
  value="${!key:-}"
  if [ -z "${value}" ]; then
    echo "::error::${key} is empty - the GitHub secret is missing (gh secret set ${key} -R Danila8889/ytgen-bot)"
    problems=1
  fi
done

for key in "${REQUIRED_PLAIN_KEYS[@]}"; do
  value=$(grep -E "^${key}=" .env.production | tail -n1 | cut -d= -f2- || true)
  if [ -z "${value}" ]; then
    echo "::error::${key} is missing or empty in .env.production"
    problems=1
  elif [[ "${value}" =~ [\$\"\'\`\\] ]]; then
    echo "::error::${key} in .env.production contains a character docker compose would interpret (\$ \" ' \` \\)"
    problems=1
  fi
done

if [ -n "${BOT_SERVICE_API_KEY:-}" ] && [ "${#BOT_SERVICE_API_KEY}" -lt 32 ]; then
  echo "::error::BOT_SERVICE_API_KEY is shorter than 32 characters (the wrapper treats such a key as 'bot API off')"
  problems=1
fi
if [ -n "${BOT_TOKEN:-}" ] && ! [[ "${BOT_TOKEN}" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  echo "::error::BOT_TOKEN does not look like a Telegram bot token"
  problems=1
fi
if [ -n "${BOT_SERVICE_API_KEY:-}" ] && ! [[ "${BOT_SERVICE_API_KEY}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "::error::BOT_SERVICE_API_KEY contains characters outside [A-Za-z0-9_-] (use: openssl rand -hex 32)"
  problems=1
fi

if [ "${problems}" -ne 0 ]; then
  echo "::error::refusing to render .env: see the errors above (nothing was deployed)"
  exit 1
fi

umask 077
{
  cat .env.production
  echo
  for key in "${SECRET_KEYS[@]}"; do
    echo "${key}=${!key}"
  done
} > .env
