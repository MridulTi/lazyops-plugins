#!/bin/bash

set -euo pipefail

echo "🔍 Finding Security Groups with 0.0.0.0/0 in INBOUND rules only"
echo

aws ec2 describe-security-groups --output json |
jq -c '.SecurityGroups[]' | while read -r sg; do

  SG_ID=$(jq -r '.GroupId' <<<"$sg")
  SG_NAME=$(jq -r '.GroupName' <<<"$sg")

  INBOUND_OPEN=$(jq -e '
    .IpPermissions[]? |
    select(
      (.IpRanges[]?.CidrIp == "0.0.0.0/0") or
      (.Ipv6Ranges[]?.CidrIpv6 == "::/0")
    )
  ' <<<"$sg" >/dev/null 2>&1 && echo yes || echo no)

  [[ "$INBOUND_OPEN" == "no" ]] && continue

  echo "🚨 Security Group: $SG_NAME ($SG_ID)"
  echo "   🔓 Open INBOUND: 0.0.0.0/0 or ::/0"
  echo "   🔗 Attached resources:"

  ENIS=$(aws ec2 describe-network-interfaces \
    --filters Name=group-id,Values="$SG_ID" \
    --output json)

  if [[ $(jq '.NetworkInterfaces | length' <<<"$ENIS") -eq 0 ]]; then
    echo "      ❌ Not attached to any resource"
    echo
    continue
  fi

  jq -c '.NetworkInterfaces[]' <<<"$ENIS" | while read -r eni; do
    ENI_ID=$(jq -r '.NetworkInterfaceId' <<<"$eni")
    DESC=$(jq -r '.Description // "N/A"' <<<"$eni")
    INSTANCE_ID=$(jq -r '.Attachment.InstanceId // empty' <<<"$eni")

    if [[ -n "$INSTANCE_ID" ]]; then
      NAME=$(aws ec2 describe-instances \
        --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].Tags[?Key==`Name`].Value | [0]' \
        --output text 2>/dev/null)

      echo "      🖥️ EC2: $INSTANCE_ID (${NAME:-no-name})"
    else
      echo "      🔌 ENI: $ENI_ID ($DESC)"
    fi
  done

  echo
done

