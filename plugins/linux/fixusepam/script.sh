#!/bin/bash

if [ $# -lt 2 ]; then
    echo "Usage: $0 <ssh-key> <ip1> [ip2 ip3 ...]"
    exit 1
fi

SSH_KEY="$1"
shift

for ip in "$@"; do
    echo "🔍 Checking SSH to $ip ..."
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ec2-user@"$ip" "echo 'SSH connection successful'" 2>/dev/null

    if [ $? -eq 0 ]; then
        echo "✅ SSH Success: $ip"

        echo "⚙️  Fixing UsePAM config on $ip ..."
        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$ip" 'bash -s' << 'EOF'
SSHD_CONFIG="/etc/ssh/sshd_config"
BACKUP_FILE="/etc/ssh/sshd_config.bak.$(date +%F_%T)"

if sudo grep -q "^#UsePAM[[:space:]]\+yes" "$SSHD_CONFIG"; then
    echo "📦 Backing up sshd_config to $BACKUP_FILE"
    sudo cp "$SSHD_CONFIG" "$BACKUP_FILE"

    echo "🛠️  Uncommenting UsePAM yes"
    sudo sed -i "s/^#UsePAM[[:space:]]\+yes/UsePAM yes/" "$SSHD_CONFIG"

    echo "🧪 Testing sshd config..."
    if sudo sshd -t; then
        echo "♻️ Reloading sshd"
        sudo systemctl reload sshd && echo "✅ Reloaded successfully"
    else
        echo "❌ sshd config invalid. Reverting changes"
        sudo cp "$BACKUP_FILE" "$SSHD_CONFIG"
    fi
else
    echo "ℹ️  UsePAM yes is already active or not found"
fi
EOF

    else
        echo "❌ SSH Failed: $ip"
    fi

    echo "------------------------------------------"
done

