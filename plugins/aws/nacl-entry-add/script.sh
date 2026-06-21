#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <cidr> [port]"
  exit 1
fi

CIDR="$1"
PORT="${2:-514}"
REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$REGION" ]]; then
  echo "ERROR: Set REGION or AWS_REGION" >&2
  exit 1
fi

echo "Using region: $REGION"

NACLS=$(aws ec2 describe-network-acls --region "$REGION" \
  --query "NetworkAcls[*].NetworkAclId" \
  --output text)

for NACL in $NACLS; do
  echo "Processing NACL: $NACL"
  RULES=$(aws ec2 describe-network-acls --region "$REGION" \
    --network-acl-ids "$NACL" \
    --query "NetworkAcls[].Entries[].RuleNumber" \
    --output text)

  RULE_NUM=1900
  while echo "$RULES" | grep -qw "$RULE_NUM"; do
    RULE_NUM=$((RULE_NUM + 10))
  done

  EXISTING=$(aws ec2 describe-network-acls --region "$REGION" \
    --network-acl-ids "$NACL" \
    --query "NetworkAcls[].Entries[?CidrBlock=='$CIDR' && RuleAction=='allow' && PortRange.From==\`$PORT\`]" \
    --output text)

  if [[ -n "$EXISTING" ]]; then
    echo "  Rule already exists, skipping..."
    continue
  fi

  echo "  Adding outbound rule..."
  aws ec2 create-network-acl-entry --region "$REGION" \
    --network-acl-id "$NACL" \
    --egress \
    --rule-number $((RULE_NUM + 1)) \
    --protocol tcp \
    --port-range From=1024,To=65535 \
    --cidr-block "$CIDR" \
    --rule-action allow
done

echo "Done."
