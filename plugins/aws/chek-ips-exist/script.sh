#!/bin/bash

IP_FILE="${IP_FILE:-$1}"

if [ ! -f "$IP_FILE" ]; then
  echo "Error: File '$IP_FILE' not found!"
  exit 1
fi

echo "Fetching all EC2 instance private IPs from AWS..."

AWS_PRIVATE_IPS=$(aws ec2 describe-instances \
  --query "Reservations[].Instances[].NetworkInterfaces[].PrivateIpAddresses[].PrivateIpAddress" \
  --output text | tr '\t' '\n' | sort -u)

# Sort the IPs from ip.txt
SORTED_IPS=$(sort "$IP_FILE")

# Create temp files
TMP_IPS=$(mktemp)
TMP_AWS=$(mktemp)

echo "$SORTED_IPS" > "$TMP_IPS"
echo "$AWS_PRIVATE_IPS" > "$TMP_AWS"

echo "IPs NOT found as private IPs in AWS EC2 instances:"
comm -23 "$TMP_IPS" "$TMP_AWS"

# Cleanup
rm "$TMP_IPS" "$TMP_AWS"

