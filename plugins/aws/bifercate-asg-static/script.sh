#!/bin/bash

INPUT_FILE="${INPUT_FILE:-$1}"
OUTPUT_FILE="${OUTPUT_FILE:-$2}"

echo "Name,Type" > $OUTPUT_FILE

while IFS= read -r name; do
    # Check ASG
    asg=$(aws autoscaling describe-auto-scaling-groups \
        --query "AutoScalingGroups[?AutoScalingGroupName=='$name'].AutoScalingGroupName" \
        --output text)

    if [[ ! -z "$asg" ]]; then
        echo "$name,ASG" >> $OUTPUT_FILE
    else
        # Check EC2 instances with Name tag
        instance=$(aws ec2 describe-instances \
            --filters "Name=tag:Name,Values=$name" \
            --query "Reservations[].Instances[].InstanceId" \
            --output text)

        if [[ ! -z "$instance" ]]; then
            echo "$name,STATIC" >> $OUTPUT_FILE
        else
            echo "$name,NOT_FOUND" >> $OUTPUT_FILE
        fi
    fi

done < "$INPUT_FILE"

echo "Done. Output saved to $OUTPUT_FILE"
