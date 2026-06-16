#!/bin/bash

SG_ID="$1"
REGION="$2"

if [[ -z "$SG_ID" || -z "$REGION" ]]; then
  echo "Usage: $0 <security-group-id> <region>"
  exit 1
fi

OUTPUT="inbound_rules.csv"

SG_JSON=$(aws ec2 describe-security-groups \
  --group-ids "$SG_ID" \
  --region "$REGION" \
  --output json)

GROUP_NAME=$(echo "$SG_JSON" | jq -r '.SecurityGroups[0].GroupName // empty')
if [[ "$GROUP_NAME" == "default" ]]; then
  echo "Error: refusing to operate on default security group ($SG_ID)." >&2
  echo "Use a non-default security group." >&2
  exit 1
fi

echo "IP,Port,Protocol" > "$OUTPUT"

echo "$SG_JSON" | jq -c '.SecurityGroups[0].IpPermissions // [] | .[]' | while read -r rule; do

    PORT_FROM=$(echo "$rule" | jq -r '.FromPort')
    PORT_TO=$(echo "$rule" | jq -r '.ToPort')
    PROTO=$(echo "$rule" | jq -r '.IpProtocol')

    # Handle -1 for all ports (e.g., ICMP)
    if [[ "$PORT_FROM" == "null" ]]; then PORT_FROM="all"; fi
    if [[ "$PORT_TO" == "null" ]]; then PORT_TO="all"; fi

    PORT_RANGE="${PORT_FROM}-${PORT_TO}"

    # IPv4 ranges
    echo "$rule" | jq -r '.IpRanges[].CidrIp' 2>/dev/null | while read -r ip; do
      echo "$ip,$PORT_RANGE,$PROTO" >> "$OUTPUT"
    done

    # IPv6 ranges
    echo "$rule" | jq -r '.Ipv6Ranges[].CidrIpv6' 2>/dev/null | while read -r ip6; do
      echo "$ip6,$PORT_RANGE,$PROTO" >> "$OUTPUT"
    done

done

echo "✅ CSV created: $OUTPUT"
