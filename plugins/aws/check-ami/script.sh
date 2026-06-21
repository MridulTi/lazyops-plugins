#!/usr/bin/env bash
set -euo pipefail

CSV_FILE="$1"

if [[ ! -f "$CSV_FILE" ]]; then
  echo "❌ CSV file not found: $CSV_FILE" >&2
  exit 1
fi

echo "🔍 Checking AMI usage from CSV: $CSV_FILE"
echo

declare -a UNUSED_AMIS=()

echo "Region,AMI_ID,AMI_Name,Status"

# Skip header
tail -n +2 "$CSV_FILE" | while IFS=',' read -r REGION AMI_ID AMI_NAME; do
  REGION="$(echo "$REGION" | xargs)"
  AMI_ID="$(echo "$AMI_ID" | xargs)"
  AMI_NAME="$(echo "$AMI_NAME" | xargs)"

  [[ -z "$REGION" || -z "$AMI_ID" ]] && continue

  echo "➡️ Checking $AMI_ID in $REGION..." >&2

  USED=false

  ########################
  # 1️⃣ EC2 INSTANCES
  ########################
  if aws ec2 describe-instances \
      --region "$REGION" \
      --filters "Name=image-id,Values=$AMI_ID" \
      --query 'Reservations[].Instances[].InstanceId' \
      --output text | grep -q .; then
    USED=true
  fi

  ########################
  # 2️⃣ LAUNCH TEMPLATES
  ########################
  if [[ "$USED" == "false" ]]; then
    aws ec2 describe-launch-templates \
      --region "$REGION" \
      --query 'LaunchTemplates[].LaunchTemplateId' \
      --output text | tr '\t' '\n' | while read -r LT_ID; do
        aws ec2 describe-launch-template-versions \
          --region "$REGION" \
          --launch-template-id "$LT_ID" \
          --versions '$Latest' '$Default' \
          --query 'LaunchTemplateVersions[].LaunchTemplateData.ImageId' \
          --output text
      done | grep -q "$AMI_ID" && USED=true
  fi

  ########################
  # 3️⃣ AUTO SCALING GROUPS
  ########################
  if [[ "$USED" == "false" ]]; then
    aws autoscaling describe-auto-scaling-groups \
      --region "$REGION" \
      --query 'AutoScalingGroups[].LaunchTemplate.LaunchTemplateId' \
      --output text | tr '\t' '\n' | while read -r ASG_LT; do
        [[ -z "$ASG_LT" ]] && continue
        aws ec2 describe-launch-template-versions \
          --region "$REGION" \
          --launch-template-id "$ASG_LT" \
          --versions '$Latest' '$Default' \
          --query 'LaunchTemplateVersions[].LaunchTemplateData.ImageId' \
          --output text
      done | grep -q "$AMI_ID" && USED=true
  fi

  ########################
  # RESULT
  ########################
  if [[ "$USED" == "true" ]]; then
    echo "$REGION,$AMI_ID,$AMI_NAME,IN_USE"
  else
    echo "$REGION,$AMI_ID,$AMI_NAME,UNUSED"
    UNUSED_AMIS+=("$REGION,$AMI_ID,$AMI_NAME")
  fi

done

echo
echo "================ UNUSED AMIs ================"

if [[ "${#UNUSED_AMIS[@]}" -eq 0 ]]; then
  echo "✅ No unused AMIs found"
  exit 0
fi

printf "%s\n" "${UNUSED_AMIS[@]}"

echo
read -rp "❓ Do you want to DELETE ALL unused AMIs listed above? (yes/no): " CONFIRM

if [[ "$CONFIRM" != "yes" ]]; then
  echo "ℹ️ Deletion aborted by user"
  exit 0
fi

echo
echo "🗑️ Deleting unused AMIs..."

for ENTRY in "${UNUSED_AMIS[@]}"; do
  IFS=',' read -r REGION AMI_ID AMI_NAME <<< "$ENTRY"

  echo "➡️ Deleting $AMI_ID in $REGION"
  aws ec2 deregister-image \
    --region "$REGION" \
    --image-id "$AMI_ID"
done

echo "✅ Deletion completed"

