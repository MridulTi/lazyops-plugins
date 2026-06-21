#!/usr/bin/env bash
set -euo pipefail

REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
KEY_PATH="${SSH_KEY_PATH:?Set SSH_KEY_PATH}"
EC2_TAG_FILTER="${EC2_TAG_FILTER:?Set EC2_TAG_FILTER, e.g. Name=tag:techteam,Values=my-team}"

if [[ -z "$REGION" ]]; then
  echo "ERROR: Set REGION or AWS_REGION" >&2
  exit 1
fi

SSH_USERS=("ubuntu" "ec2-user" "centos")

echo "Fetching instances..."
IPS=$(aws ec2 describe-instances \
  --region "$REGION" \
  --filters "$EC2_TAG_FILTER" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].PrivateIpAddress' \
  --output text)

if [[ -z "$IPS" ]]; then
  echo "No instances found"
  exit 0
fi

read -p "Proceed with OS check via SSH? (yes/no): " ans
[[ "$ans" != "yes" ]] && exit 0

for ip in $IPS; do
  for user in "${SSH_USERS[@]}"; do
    OS=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
         -i "$KEY_PATH" "$user@$ip" \
         "source /etc/os-release 2>/dev/null && echo \$ID \$VERSION_ID" 2>/dev/null || true)
    if [[ -n "$OS" ]]; then
      echo "$ip: $OS"
      break
    fi
  done
done
