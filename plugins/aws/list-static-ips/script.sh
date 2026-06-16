#!/bin/bash

REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$REGION" ]]; then
  echo "ERROR: Set REGION or AWS_REGION" >&2
  exit 1
fi
OUTPUT_FILE="non_asg_instances.csv"

echo "Hostname,InstanceID,PrivateIP,State" > "$OUTPUT_FILE"

aws ec2 describe-instances \
    --region "$REGION" \
    --query '
    Reservations[].Instances[?!(
        Tags[?Key==`aws:autoscaling:groupName`]
    )].[ 
        Tags[?Key==`Name`].Value | [0],
        InstanceId,
        PrivateIpAddress,
        State.Name
    ]' \
    --output text | while read HOSTNAME INSTANCE_ID PRIVATE_IP STATE
do
    echo "${HOSTNAME:-N/A},${INSTANCE_ID:-N/A},${PRIVATE_IP:-N/A},${STATE:-N/A}" >> "$OUTPUT_FILE"
done

echo "CSV generated: $OUTPUT_FILE"
