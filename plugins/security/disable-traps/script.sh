#!/bin/bash

# Usage:
# ./disable_traps.sh <username> <ssh_key> <ip1> <ip2> <ip3> ...
# Example:
# ./disable_traps.sh ec2-user ~/.ssh/mykey.pem 10.0.0.1 10.0.0.2

if [ $# -lt 3 ]; then
    echo "Usage: $0 <username> <ssh_key> <ip1> [ip2 ip3 ...]"
    exit 1
fi

USER=$1
SSH_KEY=$2
shift 2

for IP in "$@"; do
    echo "🔹 Connecting to $IP ..."
    ssh -o StrictHostKeyChecking=no -i "$SSH_KEY" "$USER@$IP" \
        "sudo systemctl stop traps_pmd && sudo systemctl disable traps_pmd"

    if [ $? -eq 0 ]; then
        echo "✅ traps_pmd stopped & disabled on $IP"
    else
        echo "❌ Failed on $IP"
    fi
done
