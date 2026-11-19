#!/usr/bin/env bash
# Encrypted backup of BOTH database and .env. Run from repository root.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
: "$AGE_RECIPIENT"
command -v age >/dev/null || { echo "Install age encryption tool first"; exit 1; }
command -v docker >/dev/null || { echo "Docker Compose is required"; exit 1; }
test -r .env || { echo ".env is missing"; exit 1; }
DEST="$1"
mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DB_TMP="$DEST/santa-$STAMP.pgcustom.age.tmp"
ENV_TMP="$DEST/santa-$STAMP.env.age.tmp"
trap 'rm -f "$DB_TMP" "$ENV_TMP"' EXIT
docker compose exec -T db pg_dump -U santa -d santa -Fc |
  age -r "$AGE_RECIPIENT" > "$DB_TMP"
age -r "$AGE_RECIPIENT" .env > "$ENV_TMP"
mv "$DB_TMP" "$DEST/santa-$STAMP.pgcustom.age"
mv "$ENV_TMP" "$DEST/santa-$STAMP.env.age"
echo "Created encrypted backup files in $DEST"
echo "COPY them OFF THE VPS. Keep the age private identity key elsewhere."
