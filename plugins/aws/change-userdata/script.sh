#!/bin/bash
set -e

# -------- Configuration --------
REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$REGION" ]]; then
  echo "ERROR: Set REGION or AWS_REGION" >&2
  exit 1
fi
CONFIG_FILE="config.ini"
USERDATA_FILE=$1
# --------------------------------

if [ -z "$USERDATA_FILE" ]; then
  echo "❌ Please provide a user data file as argument."
  echo "Usage: bash change_userdata.sh userData.sh"
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "❌ $CONFIG_FILE not found in current directory."
  exit 1
fi

# Read IPs (filter only valid IPv4s)
IPS=$(grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' "$CONFIG_FILE")

if [ -z "$IPS" ]; then
  echo "❌ No IPs found in $CONFIG_FILE"
  exit 1
fi

echo "📋 Found IPs:"
echo "$IPS"

# Encode the user data (macOS-compatible base64)
ENCODED_USERDATA=$(base64 "$USERDATA_FILE" | tr -d '\n')

echo "🔍 Starting EC2 User Data Update for $(echo "$IPS" | wc -w) instances in region: $REGION ..."
echo ""

# Process each IP
for ip in $IPS; do
  echo "➡️ Processing instance with IP: $ip"

  INSTANCE_ID=$(aws ec2 describe-instances \
    --filters "Name=private-ip-address,Values=$ip" \
    --query "Reservations[].Instances[].InstanceId" \
    --output text \
    --region "$REGION")

  if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" == "None" ]; then
    echo "⚠️  No instance found for IP: $ip — skipping."
    continue
  fi

  echo "🔹 Found instance: $INSTANCE_ID"

  aws ec2 modify-instance-attribute \
    --instance-id "$INSTANCE_ID" \
    --user-data "Value=$ENCODED_USERDATA" \
    --region "$REGION" >/dev/null

  if [ $? -eq 0 ]; then
    echo "✅ Updated user data for $INSTANCE_ID ($ip)"
  else
    echo "❌ Failed to update user data for $INSTANCE_ID ($ip)"
  fi

  echo ""
done

echo "🎉 All instances processed successfully!"

