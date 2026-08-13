#!/usr/bin/env bash
# Phase D0 — nightly PostgreSQL backup foundation. pg_dump the production DB (custom format),
# gzip, retain 14 days. Off-box copy (S3/rsync) is added later; this is the local foundation.
set -euo pipefail

ATP_HOME=/opt/atp
ENV_FILE="$ATP_HOME/atp.env"
OUT_DIR="$ATP_HOME/backups"
RETAIN_DAYS=14

set -a; . "$ENV_FILE"; set +a
mkdir -p "$OUT_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$OUT_DIR/${ATP_PROD_DB}_${STAMP}.dump"

docker exec atp_postgres pg_dump -U "$ATP_APP_USER" -d "$ATP_PROD_DB" -Fc > "$OUT"
gzip -f "$OUT"
echo "backup written: ${OUT}.gz"

# prune old backups
find "$OUT_DIR" -name "${ATP_PROD_DB}_*.dump.gz" -mtime +"$RETAIN_DAYS" -delete
