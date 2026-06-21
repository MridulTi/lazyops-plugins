#!/bin/bash

if [ $# -lt 2 ]; then
    echo "Usage: $0 <ssh-key> <ip1> [ip2 ip3 ...]"
    exit 1
fi

SSH_KEY="$1"
shift

for ip in "$@"; do
    echo "Checking SSH to $ip ..."
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no infosec@"$ip" "echo 'SSH connection successful'" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "✅ SSH Success: $ip"
	cat /etc/passwd | grep -i infosec
    else
        echo "❌ SSH Failed: $ip"
    fi
done
