#!/bin/bash

# Region setup
AWS_REGION="${AWS_REGION:-${AWS_AWS_REGION:-${AWS_DEFAULT_AWS_REGION:-}}}"
if [[ -z "$AWS_REGION" ]]; then
  echo "ERROR: Set AWS_REGION or AWS_AWS_REGION" >&2
  exit 1
fi
DATE_TAG=$(date +"%Y%m%d-%H%M%S")

# Output files
VOL_FILE="unused_volumes_$DATE_TAG.txt"
AMI_FILE="unused_amis_$DATE_TAG.txt"
SNAP_FILE="unused_snapshots_$DATE_TAG.txt"
SG_FILE="unused_sgs_$DATE_TAG.txt"  # New file for unused security groups

echo "🚀 Starting EBS/AMI/Snapshot/SG Cleanup Check in $AWS_REGION"

############################
# Unattached EBS Volumes
############################
echo -e "\n📦 Unattached EBS Volumes:"
aws ec2 describe-volumes \
  --region "$AWS_REGION" \
  --filters Name=status,Values=available \
  --query "Volumes[*].[VolumeId,Size,AvailabilityZone,CreateTime]" \
  --output table | tee "$VOL_FILE"

############################
# Unused AMIs (<= year 2024)
############################
echo -e "\n🖼️ Unused Private AMIs (created <= 2024 and not referenced by any EC2 instance):"

# Step 1: Get AMIs in use by running instances
aws ec2 describe-instances \
  --region "$AWS_REGION" \
  --query 'Reservations[*].Instances[*].ImageId' \
  --output text | sort | uniq > used_amis.tmp

# Step 2: Get all **private** AMIs with their creation date
aws ec2 describe-images \
  --region "$AWS_REGION" \
  --filters "Name=is-public,Values=false" \
  --query 'Images[*].[ImageId,Name,CreationDate]' \
  --output text > all_amis.tmp

# Step 3: Filter unused ones AND created in or before 2024
awk '$3 ~ /^[0-9]{4}/ && substr($3, 1, 4) <= 2024' all_amis.tmp | grep -vFf used_amis.tmp > "$AMI_FILE"

# Step 4: Display results
if [[ -s "$AMI_FILE" ]]; then
  echo "✅ Found unused private AMIs created <= 2024:"
  awk '{print $1, $3}' "$AMI_FILE"
else
  echo "🎉 No unused private AMIs found that are older than or from 2024."
fi

############################
# Unused Snapshots
############################
echo -e "\n📸 Unused Snapshots (not used directly or by used AMIs):"

# Get all snapshots owned by self
aws ec2 describe-snapshots \
  --region "$AWS_REGION" \
  --owner-ids self \
  --query "Snapshots[*].[SnapshotId,StartTime,VolumeSize]" \
  --output text > all_snapshots.tmp

# Get snapshot IDs used by used AMIs
aws ec2 describe-images \
  --region "$AWS_REGION" \
  --image-ids $(cat used_amis.tmp) 2>/dev/null \
  --query 'Images[*].BlockDeviceMappings[*].Ebs.SnapshotId' \
  --output text | sort | uniq > used_snapshots.tmp

# Exclude used snapshots
grep -vFf used_snapshots.tmp all_snapshots.tmp | awk '{print $1}' > "$SNAP_FILE"

if [[ -s "$SNAP_FILE" ]]; then
  echo "✅ Found potentially unused snapshots:"
  cat "$SNAP_FILE"
else
  echo "🎉 No unused snapshots found."
fi

############################
# Unused Security Groups (SGs)
############################
echo -e "\n🛡️ Unused Security Groups (not associated with any ENI):"

# Step 1: Get all security groups
aws ec2 describe-security-groups \
  --region "$AWS_REGION" \
  --query "SecurityGroups[*].GroupId" \
  --output text | sort | uniq > all_sgs.tmp

# Step 2: Get all SGs in use by any ENI
aws ec2 describe-network-interfaces \
  --region "$AWS_REGION" \
  --query "NetworkInterfaces[*].Groups[*].GroupId" \
  --output text | sort | uniq > used_sgs.tmp

# Step 3: Filter unused SGs
grep -vFf used_sgs.tmp all_sgs.tmp | awk '{print $1}' > "$SG_FILE"

# Step 4: Display results
if [[ -s "$SG_FILE" ]]; then
  echo "✅ Found unused security groups:"
  cat "$SG_FILE"
else
  echo "🎉 No unused security groups found."
fi


# Clean up temp files
rm -f used_amis.tmp all_amis.tmp all_snapshots.tmp used_snapshots.tmp all_sgs.tmp used_sgs.tmp

echo -e "\n✅ Script complete! Output files:"
echo " - $VOL_FILE"
echo " - $AMI_FILE"
echo " - $SNAP_FILE"
echo " - $SG_FILE"
