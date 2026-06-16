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

        echo "🔎 Ensuring 'UsePAM no' for PPSL_infosec on $ip ..."
        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$ip" '
            MATCH_BLOCK="Match User PPSL_infosec"
            PAM_SETTING="    UsePAM no"
            FILE="/etc/ssh/sshd_config"

            if ! grep -q "$MATCH_BLOCK" "$FILE"; then
                echo "⚙️ Adding Match block for PPSL_infosec..."
                echo -e "\n$MATCH_BLOCK\n$PAM_SETTING" | sudo tee -a "$FILE" > /dev/null
                sudo systemctl reload sshd
                echo "✅ Match block added and sshd reloaded."
            elif ! awk "/$MATCH_BLOCK/,/^Match/{ if (\$1 == \"UsePAM\" && \$2 == \"no\") found=1 } END { exit !found }" "$FILE"; then
                echo "⚠️ Match block exists but missing 'UsePAM no'. Updating..."
                sudo sed -i "/$MATCH_BLOCK/,/^Match/{/UsePAM/ s/.*/    UsePAM no/}" "$FILE"
                sudo systemctl reload sshd
                echo "✅ Updated Match block and reloaded sshd."
            else
                echo "✅ 'UsePAM no' already correctly set for PPSL_infosec"
            fi
        '
    else
        echo "❌ SSH Failed: $ip"
    fi
done

