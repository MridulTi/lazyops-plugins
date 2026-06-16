#!/bin/bash

# Usage: ./change_shell.sh <ssh-key> <username> <user-to-change-shell>
if [ $# -lt 3 ]; then
    echo "Usage: $0 <ssh-key> <ssh-username> <user-to-change-shell>"
    exit 1
fi

SSH_KEY="$1"
SSH_USER="$2"
TARGET_USER="$3"
CONFIG_FILE="config.ini"

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Config file '$CONFIG_FILE' not found!"
    exit 1
fi

# Extract IPs from config.ini
IPS=$(grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' "$CONFIG_FILE")

if [ -z "$IPS" ]; then
    echo "❌ No IPs found in $CONFIG_FILE"
    exit 1
fi

# Loop through each IP
for ip in $IPS; do
    echo "🔄 Changing shell for user '$TARGET_USER' on $ip ..."
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$SSH_USER@$ip" \
    "sudo usermod -s /bin/bash $TARGET_USER" 2>/dev/null

    if [ $? -eq 0 ]; then
        echo "✅ Shell changed successfully on $ip"
    else
        echo "❌ Failed to change shell on $ip"
    fi
done

