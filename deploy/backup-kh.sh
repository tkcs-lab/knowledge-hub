#!/usr/bin/env bash
# Backup of the knowledge hub's non-rebuildable data.
#
# Dumps kh.records, the dataset catalog (kh.datasets, kh.columns, kh.saved_queries, kh.dataset_syncs,
# kh.bi_roles) and every table of schema kh_data (rows and history). Groups and labels come from kh.yaml
# (`kh setup apply`). pg_dump ignores -n when -t is given, so the kh_data tables are selected with -t 'kh_data.*'.
# Documents and chunks are rebuilt from Forgejo with `kh rebuild`. The blob bucket is mirrored.
#
# Restore (as the Postgres superuser, after `kh migrate` on an empty database), in this order:
#   pg_restore --no-owner -d kh kh-t2t3-<date>.dump     (INSERT format, so RLS does not block it)
#   kh setup apply
#   kh rebuild
#   kh requeue-embeddings
#   S3 bucket sync, as for blobs: `aws s3 sync "${BACKUP_DIR}/blobs/" s3://$KH_S3_BUCKET --endpoint-url "$KH_S3_ENDPOINT"`.
#
# Runs as role kh_migrator: its USING (true) policy lets pg_dump read the FORCE-RLS tables
# when --enable-row-security is given. Secrets are read from files only.
set -euo pipefail

: "${KH_DB_MIGRATOR_PASSWORD_FILE:?set KH_DB_MIGRATOR_PASSWORD_FILE}"
: "${KH_S3_ENDPOINT:?set KH_S3_ENDPOINT}"
: "${KH_S3_BUCKET:?set KH_S3_BUCKET}"
: "${KH_S3_ACCESS_KEY_FILE:?set KH_S3_ACCESS_KEY_FILE}"
: "${KH_S3_SECRET_KEY_FILE:?set KH_S3_SECRET_KEY_FILE}"
: "${BACKUP_DIR:?set BACKUP_DIR}"

DB_HOST="${KH_DB_HOST:-postgres}"
DB_PORT="${KH_DB_PORT:-5432}"
DB_NAME="${KH_DB_NAME:-kh}"
DATE="$(date +%Y-%m-%d)"

mkdir -p "${BACKUP_DIR}/blobs"

PGPASSWORD="$(cat "$KH_DB_MIGRATOR_PASSWORD_FILE")"
export PGPASSWORD
AWS_ACCESS_KEY_ID="$(cat "$KH_S3_ACCESS_KEY_FILE")"
AWS_SECRET_ACCESS_KEY="$(cat "$KH_S3_SECRET_KEY_FILE")"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION="${KH_S3_REGION:-garage}"

pg_dump -Fc --no-owner --enable-row-security --inserts \
  -h "$DB_HOST" -p "$DB_PORT" -U kh_migrator -d "$DB_NAME" \
  -t kh.records -t kh.datasets -t kh.columns -t kh.saved_queries -t kh.dataset_syncs -t kh.bi_roles \
  -t 'kh_data.*' \
  -f "${BACKUP_DIR}/kh-t2t3-${DATE}.dump"

aws s3 sync --endpoint-url "$KH_S3_ENDPOINT" "s3://$KH_S3_BUCKET" "${BACKUP_DIR}/blobs/"

echo "backup ok ${DATE}"
