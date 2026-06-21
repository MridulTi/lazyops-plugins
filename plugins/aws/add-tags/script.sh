#!/usr/bin/env bash
set -e

if [ -z "$BASH_VERSION" ]; then
  echo "Please run with bash" >&2
  exit 1
fi

AWS_REGION="${AWS_REGION:-${REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$AWS_REGION" ]]; then
  echo "ERROR: Set AWS_REGION or REGION" >&2
  exit 1
fi

TAGS_FILE="${TAGS_FILE:-${1:-}}"
INSTANCE_IDS="${INSTANCE_IDS:-${2:-}}"

if [[ -z "$TAGS_FILE" || -z "$INSTANCE_IDS" ]]; then
  echo "Usage: TAGS_FILE=tags.txt INSTANCE_IDS='i-xxx i-yyy' $0 [tags-file] [instance-ids]"
  echo "  tags-file: one tag per line as Key=Value"
  exit 1
fi

if [[ ! -f "$TAGS_FILE" ]]; then
  echo "Tags file not found: $TAGS_FILE" >&2
  exit 1
fi

mapfile -t TAGS_TO_ADD < <(grep -v '^#' "$TAGS_FILE" | grep -v '^[[:space:]]*$' || true)

for INSTANCE_ID in $INSTANCE_IDS; do
  echo "Updating $INSTANCE_ID"
  TAG_ARGS=()
  for TAG in "${TAGS_TO_ADD[@]}"; do
    KEY="${TAG%%=*}"
    VALUE="${TAG#*=}"
    TAG_ARGS+=(Key="$KEY",Value="$VALUE")
  done
  aws ec2 create-tags --region "$AWS_REGION" --resources "$INSTANCE_ID" --tags "${TAG_ARGS[@]}"
done
