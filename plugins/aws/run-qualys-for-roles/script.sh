#!/usr/bin/env bash
set -euo pipefail

# Usage: ./script.sh [roles-file]
# roles-file: one IAM role ARN per line (or set ROLE_ARNS_FILE)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE

ROLES_FILE="${1:-${ROLE_ARNS_FILE:-roles.txt}}"
if [[ ! -f "$ROLES_FILE" ]]; then
  echo "Roles file not found: $ROLES_FILE" >&2
  echo "Usage: $0 [roles-file]" >&2
  exit 1
fi

mapfile -t ROLE_ARNS < <(grep -v '^#' "$ROLES_FILE" | grep -v '^[[:space:]]*$' || true)
if [[ ${#ROLE_ARNS[@]} -eq 0 ]]; then
  echo "No role ARNs found in $ROLES_FILE" >&2
  exit 1
fi

export_credentials() {
  local role_arn="$1"
  local session_name="${2:-qualys-repair-session}"
  eval "$(aws sts assume-role \
    --role-arn "$role_arn" \
    --role-session-name "$session_name" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text | awk '{print "export AWS_ACCESS_KEY_ID="$1" AWS_SECRET_ACCESS_KEY="$2" AWS_SESSION_TOKEN="$3}')"
}

for role_arn in "${ROLE_ARNS[@]}"; do
  echo "Assuming role: $role_arn"
  export_credentials "$role_arn"
  ansible-playbook repair_qualys.yml "$@"
done
