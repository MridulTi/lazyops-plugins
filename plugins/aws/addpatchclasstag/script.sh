#!/bin/bash

# Set region (update this if needed)
REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$REGION" ]]; then
  echo "ERROR: Set REGION or AWS_REGION" >&2
  exit 1
fi

# Tag key and value to be added
TAG_KEY="ec2-start"
TAG_VALUE="Yes"

# Roles to search for
# ROLES=("jenkins","elasticsearch","redis","kafka","zookeeper","grafana-mars","bastion")
# ROLES=("jenkins","elasticsearch","kibana","mysql","bastion","logstash","redis")
# ROLES=("flink","elasticsearch","prometheus","redis","bastion")
Value="True"
KEY="Static"

# Collect instance IDs
INSTANCE_IDS=()

echo "Looking for instances with roles: ${ROLES[*]}..."

for ROLE in "${ROLES[@]}"; do
  IDS=$(aws ec2 describe-instances \
    --region "$REGION" \
    --filters "Name=tag:"$KEY",Values=$Value" "Name=instance-state-name,Values=running,stopped" \
    --query "Reservations[].Instances[].InstanceId" \
    --output text)
  
  if [[ -n "$IDS" ]]; then
    INSTANCE_IDS+=($IDS)
  fi
done

# Remove duplicates (if any)
UNIQUE_IDS=$(echo "${INSTANCE_IDS[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' ')

if [[ -z "$UNIQUE_IDS" ]]; then
  echo "No instances found with specified roles."
  exit 0
fi

# Tag the instances
echo "Tagging ${#UNIQUE_IDS[@]} instances: $UNIQUE_IDS with $TAG_KEY=$TAG_VALUE"

aws ec2 create-tags \
  --region "$REGION" \
  --resources $UNIQUE_IDS \
  --tags Key=$TAG_KEY,Value=$TAG_VALUE

if [[ $? -eq 0 ]]; then
  echo "Tag applied successfully."
else
  echo "Failed to apply tag."
fi
