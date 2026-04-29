#!/usr/bin/env bash
#
# Sync Performance — backup script.
#
# Snapshots the SQLite DB (online, consistent) and the RO uploads directory,
# then ships them to S3. Designed to run from cron every hour / day.
#
# Required environment variables:
#   SYNC_DATA_DIR        Path to the live data dir (DB + uploads). Defaults to /var/lib/sync.
#   SYNC_BACKUP_BUCKET   S3 bucket for offsite backups (e.g. sync-perf-backups)
#   AWS_REGION           e.g. ap-south-1   (or rely on the instance role)
#
# Optional:
#   SYNC_BACKUP_PREFIX   Prefix inside the bucket. Defaults to $(hostname).
#   SYNC_RETENTION_DAYS  Local backup retention; defaults to 7. Older local files removed.
#
# Idempotent — safe to run repeatedly. Exits non-zero on any failure so cron
# (or systemd timer + OnFailure=) can alert.

set -euo pipefail

DATA_DIR="${SYNC_DATA_DIR:-/var/lib/sync}"
BUCKET="${SYNC_BACKUP_BUCKET:?SYNC_BACKUP_BUCKET is required}"
PREFIX="${SYNC_BACKUP_PREFIX:-$(hostname)}"
RETENTION_DAYS="${SYNC_RETENTION_DAYS:-7}"

LOCAL_BACKUP_DIR="${DATA_DIR}/backups"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DAY="$(date -u +%Y/%m/%d)"
DB_SRC="${DATA_DIR}/app.db"
UPLOADS_SRC="${DATA_DIR}/instance/uploads/ro"

mkdir -p "${LOCAL_BACKUP_DIR}"

DB_DUMP="${LOCAL_BACKUP_DIR}/app-${TS}.db"
UPLOADS_TAR="${LOCAL_BACKUP_DIR}/uploads-${TS}.tar.gz"

echo "[backup] $(date -u) starting; data_dir=${DATA_DIR} bucket=s3://${BUCKET}/${PREFIX}/"

# 1. Online consistent SQLite snapshot via .backup (handles WAL mode safely;
#    a plain `cp app.db` can corrupt under concurrent writes).
if [[ ! -f "${DB_SRC}" ]]; then
  echo "[backup] WARNING: ${DB_SRC} does not exist — skipping DB step"
else
  sqlite3 "${DB_SRC}" ".backup '${DB_DUMP}'"
  echo "[backup] DB snapshot: ${DB_DUMP} ($(stat -c '%s' "${DB_DUMP}" 2>/dev/null || stat -f '%z' "${DB_DUMP}") bytes)"
fi

# 2. Tar the RO uploads (preserves filenames + sizes; does NOT preserve uid/gid).
if [[ -d "${UPLOADS_SRC}" ]]; then
  tar -czf "${UPLOADS_TAR}" -C "$(dirname "${UPLOADS_SRC}")" "$(basename "${UPLOADS_SRC}")"
  echo "[backup] Uploads archive: ${UPLOADS_TAR}"
else
  echo "[backup] WARNING: ${UPLOADS_SRC} does not exist — skipping uploads step"
fi

# 3. Push to S3. The `aws` CLI must be available and configured (instance role,
#    env-var creds, or ~/.aws/credentials).
S3_BASE="s3://${BUCKET}/${PREFIX}/${DAY}"
[[ -f "${DB_DUMP}" ]]      && aws s3 cp "${DB_DUMP}"      "${S3_BASE}/" --only-show-errors
[[ -f "${UPLOADS_TAR}" ]]  && aws s3 cp "${UPLOADS_TAR}"  "${S3_BASE}/" --only-show-errors
# Sync incremental upload-by-upload too — cheap when only a few RO docs were added.
[[ -d "${UPLOADS_SRC}" ]]  && aws s3 sync "${UPLOADS_SRC}" "s3://${BUCKET}/${PREFIX}/uploads-live/" --only-show-errors

# 4. Local retention — only keep N days of local snapshots.
find "${LOCAL_BACKUP_DIR}" -maxdepth 1 -type f -name 'app-*.db'        -mtime "+${RETENTION_DAYS}" -delete
find "${LOCAL_BACKUP_DIR}" -maxdepth 1 -type f -name 'uploads-*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "[backup] $(date -u) complete; pushed to ${S3_BASE}/"
