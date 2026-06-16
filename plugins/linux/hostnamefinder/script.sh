#!/bin/bash
# Description: Print EC2 Name tag values for given hostnames (one per line)
# Usage: ./get_name_tags_by_hostname.sh hostnames.txt

HOSTNAMES_FILE="$1"

if [ -z "$HOSTNAMES_FILE" ]; then
  echo "Usage: $0 <file-with-hostnames>"
  exit 1
fi

if [ ! -f "$HOSTNAMES_FILE" ]; then
  echo "Error: File '$HOSTNAMES_FILE' not found!"
  exit 1
fi

while read -r HOSTNAME; do
  [ -z "$HOSTNAME" ] && continue

  INSTANCE_ID=$(aws ec2 describe-instances \
    --filters "Name=private-dns-name,Values=$HOSTNAME" \
    --query "Reservations[].Instances[].InstanceId" \
    --output text)

  if [ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ]; then
    NAME_TAG=$(aws ec2 describe-tags \
      --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=Name" \
      --query "Tags[].Value" \
      --output text)
    
    # Print Name tag if found
    [ -n "$NAME_TAG" ] && echo "$NAME_TAG"
  fi
done < "$HOSTNAMES_FILE"

