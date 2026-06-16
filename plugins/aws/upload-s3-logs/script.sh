#!/bin/bash
set -euo pipefail

: "${BACKUP_S3:?Set BACKUP_S3 bucket name}"
: "${SRC_DIR:?Set SRC_DIR source log directory}"
: "${S3_PREFIX:?Set S3_PREFIX path prefix inside bucket, e.g. archive/logs}"

host="$(hostname -i 2>/dev/null || hostname -s)"
REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
BKP_FLD="${BKP_FLD:-$(basename "$SRC_DIR")}"

shopt -s nullglob
for file in "${SRC_DIR}"/*.log.*.zst; do
  base="$(basename "$file")"
  if [[ "$base" =~ ^([A-Za-z0-9_-]+)\.log\.([0-9]{4})-([0-9]{2})-([0-9]{2})-([0-9]{2})\.zst$ ]]; then
    SERVICE="${SERVICE_NAME:-${BASH_REMATCH[1]}}"
    YEAR="${BASH_REMATCH[2]}"
    MONTH="${BASH_REMATCH[3]}"
    DAY="${BASH_REMATCH[4]}"
    prefix="s3://${BACKUP_S3}/${S3_PREFIX}/${SERVICE}/${YEAR}/${MONTH}/${DAY}/${host}/${BKP_FLD}/"
    echo "Uploading ${base} to ${prefix}"
    aws s3 cp "$file" "${prefix}${base}" ${REGION:+--region "$REGION"}
  fi
done
echo "Done."
