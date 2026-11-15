#!/usr/bin/env bash
# Backup of the knowledge hub's non-rebuildable data.
#
# Dumps only the T2/T3 tables and mirrors the blob bucket. The git-derived tables
# (documents, document_versions, chunks, entities_cache) are NOT dumped: they are rebuilt
# from Forgejo with `kh rebuild`.
#
# Restore (as the Postgres superuser, after `kh migrate` on an empty database):
#   pg_restore --no-owner -d kh kh-t2t3-<date>.dump     (INSERT format, so RLS does not block it)
#   kh rebuild                 # git-derived tables
#   kh recompute-visibility    # once: the audience-map loader does not recompute when no
#                              # __config__ hash is stored, which is the case after a restore
#   kh requeue-embeddings      # rebuilds the chunks / embeddings
# Blobs: `aws s3 sync "${BACKUP_DIR}/blobs/" s3://$KH_S3_BUCKET --endpoint-url "$KH_S3_ENDPOINT"`.
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
  -t kh.raw_records \
  -t kh.raw_audience_map \
  -t kh.derived_records \
  -t kh.derived_sources \
  -t kh.candidates \
  -t kh.candidate_sources \
  -t kh.conflicts \
  -t kh.conflict_items \
  -f "${BACKUP_DIR}/kh-t2t3-${DATE}.dump"

aws s3 sync --endpoint-url "$KH_S3_ENDPOINT" "s3://$KH_S3_BUCKET" "${BACKUP_DIR}/blobs/"

echo "backup ok ${DATE}"
