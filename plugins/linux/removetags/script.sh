#!/bin/bash

# Get instances with tag ec2-start (any value)
start_instance_ids=$(aws ec2 describe-instances \
  --filters "Name=tag-key,Values=ec2-start" \
  --query "Reservations[].Instances[].InstanceId" \
  --output text)

# Get instances with tag ec2-stop (any value)
stop_instance_ids=$(aws ec2 describe-instances \
  --filters "Name=tag-key,Values=ec2-stop" \
  --query "Reservations[].Instances[].InstanceId" \
  --output text)

echo "Removing 'ec2-start' tag from instances:"
for instance_id in $start_instance_ids; do
  echo " - $instance_id"
  aws ec2 delete-tags --resources "$instance_id" --tags Key=ec2-start
done

echo "Removing 'ec2-stop' tag from instances:"
for instance_id in $stop_instance_ids; do
  echo " - $instance_id"
  aws ec2 delete-tags --resources "$instance_id" --tags Key=ec2-stop
done

echo "Done."

