#!/usr/bin/env bash
# Dumps the funnel Postgres (custom format, so a single table can be restored with `pg_restore -t`), uploads the dump to the
# S3-compatible offsite storage with rclone, and prunes remote dumps older than BACKUP_RETENTION_DAYS (default 14).
#
# Modelled on youtubegen-saas-wrapper's scripts/backup_db.sh. Differences: the dump name is
#   <db>_<YYYYmmdd>_<HHMMSS>_<token>.dump        (UTC; token = 8 hex chars)
# which is exactly what ytgen-infra's backup watchdog (scripts/backup_watchdog.sh) looks for, and the settings are READ from
# the checkout's `.env` (never sourced, never passed through argv): POSTGRES_USER, POSTGRES_DB and RCLONE_REMOTE.
# An optional HEALTHCHECKS_PING_URL in `.env` pings healthchecks.io on success/failure; without it detection of a missed or
# failed backup rests on the host watchdog and on this script's non-zero exit status.
#
# Install (owner, on the VPS, as the user that runs docker and has the rclone config; see documentation.md "Backup"):
#   30 3 * * *  cd /opt/actions-runner-ytgen-bot/_work/ytgen-bot/ytgen-bot && ./scripts/backup_db.sh >> /var/log/ytgen-bot-backup.log 2>&1
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="${ENV_FILE:-.env}"

# Parse KEY=value from the env file (last wins, quotes and CR stripped). Nothing is sourced or evaluated.
envval() {
  local v
  v=$(grep -E "^$1=" "${ENV_FILE}" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r' \
      | sed -e 's/^"//; s/"$//' -e "s/^'//; s/'\$//") || true
  printf '%s' "${v}"
}

[ -f "${ENV_FILE}" ] || { echo "backup: ${ENV_FILE} not found (run from the deployed checkout)" >&2; exit 1; }
POSTGRES_USER=$(envval POSTGRES_USER)
POSTGRES_DB=$(envval POSTGRES_DB)
RCLONE_REMOTE=$(envval RCLONE_REMOTE)
HEALTHCHECKS_PING_URL=$(envval HEALTHCHECKS_PING_URL)
BACKUP_RETENTION_DAYS=$(envval BACKUP_RETENTION_DAYS)
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

: "${POSTGRES_USER:?POSTGRES_USER not set in ${ENV_FILE}}"
: "${POSTGRES_DB:?POSTGRES_DB not set in ${ENV_FILE}}"
: "${RCLONE_REMOTE:?RCLONE_REMOTE not set in ${ENV_FILE}}"
[[ "${POSTGRES_DB}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "backup: POSTGRES_DB is not a plain identifier" >&2; exit 1; }
[[ "${BACKUP_RETENTION_DAYS}" =~ ^[0-9]+$ ]] || { echo "backup: BACKUP_RETENTION_DAYS must be a number" >&2; exit 1; }

if [ -z "${HEALTHCHECKS_PING_URL}" ]; then
  echo "HEALTHCHECKS_PING_URL not set; relying on the host backup watchdog and this script's exit status" >&2
fi

stamp=$(date -u +%Y%m%d_%H%M%S)
token=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
DUMP_NAME="${POSTGRES_DB}_${stamp}_${token}.dump"
DUMP_FILE="$(mktemp "/tmp/${POSTGRES_DB}_XXXXXX")"   # trailing X's: portable to BSD and GNU mktemp
ERR_LOG="$(mktemp)"

# One EXIT trap covers every way out: on a non-zero status the (optional) healthchecks.io ping reports the failure with the
# last stderr lines; the temp files are always removed.
finish() {
  local rc=$?
  if [ "${rc}" -ne 0 ]; then
    local last_lines
    last_lines="$(tail -n 20 "${ERR_LOG}" 2>/dev/null || true)"
    if [ -n "${HEALTHCHECKS_PING_URL}" ]; then
      curl -fsS -m 10 --retry 3 -d "${last_lines}" "${HEALTHCHECKS_PING_URL}/fail" >/dev/null 2>&1 || true
    fi
  fi
  rm -f "${DUMP_FILE:-}" "${ERR_LOG}"
  return "${rc}"
}
trap finish EXIT

exec 2> >(tee -a "${ERR_LOG}" >&2)

# pg_dump runs INSIDE the db container over its local socket (no password on a command line, no published port).
docker compose exec -T db pg_dump -U "${POSTGRES_USER}" -Fc "${POSTGRES_DB}" > "${DUMP_FILE}"
[ -s "${DUMP_FILE}" ] || { echo "backup: pg_dump produced an empty file" >&2; exit 1; }

# The file is uploaded under its final name; --s3-no-check-bucket because some S3-compatible providers (Beget) answer the
# HeadBucket/CreateBucket probe with 403 even though the object PUT works.
mv "${DUMP_FILE}" "/tmp/${DUMP_NAME}"
DUMP_FILE="/tmp/${DUMP_NAME}"
rclone copy "${DUMP_FILE}" "${RCLONE_REMOTE}/" --s3-no-check-bucket
rm -f "${DUMP_FILE}"

# Prune only this database's dumps.
rclone delete "${RCLONE_REMOTE}/" --min-age "${BACKUP_RETENTION_DAYS}d" --include "${POSTGRES_DB}_*.dump" --s3-no-check-bucket

if [ -n "${HEALTHCHECKS_PING_URL}" ]; then
  curl -fsS -m 10 --retry 3 "${HEALTHCHECKS_PING_URL}" >/dev/null
fi
echo "backup ok: ${DUMP_NAME}"
