#!/usr/bin/env bash
# Usage: AGE_IDENTITY_FILE=/safe/age-key.txt bash scripts/restore.sh DB.age ENV.age
# CAUTION: destructive restore of the event database; only on target VPS.
set -euo pipefail
cd "$(dirname "$0")/.."
: "$AGE_IDENTITY_FILE"
test "$#" -eq 2 || { echo "Usage: restore.sh <DB.pgcustom.age> <ENV.env.age>"; exit 1; }
command -v age >/dev/null || { echo "Install age first"; exit 1; }
DB_BACKUP="$1"
ENV_BACKUP="$2"
umask 077
age -d -i "$AGE_IDENTITY_FILE" "$ENV_BACKUP" > .env.restored
echo "Encrypted environment recovered as .env.restored."
echo "Review it, copy it to .env, then rerun restore."
if ! cmp -s .env.restored .env; then
  echo "Refusing DB restore: encrypted .env backup has not been applied."
  exit 2
fi
docker compose up -d db
docker compose stop bot || true
age -d -i "$AGE_IDENTITY_FILE" "$DB_BACKUP" |
  docker compose exec -T db pg_restore -U santa -d santa \
    --clean --if-exists --no-owner --single-transaction
docker compose up -d --build bot
echo "Restore completed. Verify /status and /health in Telegram."
