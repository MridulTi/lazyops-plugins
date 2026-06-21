#!/bin/bash
set -euo pipefail

INPUT_FILE="$1"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "❌ Input file $INPUT_FILE not found"
  exit 1
fi

ALL_IPS=()

while read -r NAME; do
  [[ -z "$NAME" ]] && continue
  echo "🔍 Input: $NAME"

  # Step 1: Remove last numeric suffix (e.g., -245)
  CLEANED=$(echo "$NAME" | sed -E 's/-[0-9]+$//')

  # Step 2: Remove -ump and everything after (e.g., -ump-v1-9)
  PREFIX=$(echo "$CLEANED" | sed -E 's/-ump.*//')

  echo "   ➤ Searching ASGs with smart prefix: $PREFIX"

  # Step 3: Find all matching ASGs containing this prefix
  ASGS=$(aws autoscaling describe-auto-scaling-groups \
    --query "AutoScalingGroups[?contains(AutoScalingGroupName, \`$PREFIX\`)].AutoScalingGroupName" \
    --output text)

  if [[ -z "$ASGS" ]]; then
    echo "   ❌ No ASGs found for prefix $PREFIX"
    continue
  fi

  # Step 4: For each ASG, get running instance IPs
  for ASG in $ASGS; do
    echo "   ✅ Found ASG: $ASG"

    INSTANCE_IDS=$(aws autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$ASG" \
      --query "AutoScalingGroups[].Instances[].InstanceId" \
      --output text)

    if [[ -z "$INSTANCE_IDS" ]]; then
      echo "      ⚠️ No instances running in $ASG"
      continue
    fi

    IPS=$(aws ec2 describe-instances \
      --instance-ids $INSTANCE_IDS \
      --query "Reservations[].Instances[].PrivateIpAddress" \
      --output text)

    echo "      📌 IPs: $IPS"

    # Collect all IPs
    for ip in $IPS; do
      ALL_IPS+=("$ip")
    done
  done

  echo "--------------------------------------------------"

done < "$INPUT_FILE"

# Step 5: Print consolidated IP list
echo "✅ All collected IPs:"
for ip in "${ALL_IPS[@]}"; do
  echo "$ip"
done
