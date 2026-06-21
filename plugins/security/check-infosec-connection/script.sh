#!/bin/bash

# Usage: ./check_ssh.sh <ssh-key> <username>

if [ $# -lt 2 ]; then
    echo "Usage: $0 <ssh-key> <username>"
    exit 1
fi

SSH_KEY="$1"
USERNAME="$2"
CONFIG_FILE="config.ini"

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Config file '$CONFIG_FILE' not found!"
    exit 1
fi

# Read IPs from config.ini (assuming one IP per line or comma-separated)
IPS=$(grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' "$CONFIG_FILE")

if [ -z "$IPS" ]; then
    echo "❌ No IPs found in $CONFIG_FILE"
    exit 1
fi

# Array to store failed IPs
FAILED_IPS=()

# Loop through each IP
for ip in $IPS; do
    echo "🔍 Checking SSH to $ip ..."
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$USERNAME"@"$ip" "echo 'SSH connection successful'" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "✅ SSH Success: $ip"
    else
        echo "❌ SSH Failed: $ip"
        FAILED_IPS+=("$ip")
    fi
done

# Print all failed IPs at the end
if [ ${#FAILED_IPS[@]} -ne 0 ]; then
    echo -e "\n⚠️  SSH failed for the following IPs:"
    for failed_ip in "${FAILED_IPS[@]}"; do
        echo "$failed_ip"
    done
else
    echo -e "\n🎉 SSH succeeded for all IPs!"
fi

