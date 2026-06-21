#!/bin/bash

if [ $# -lt 2 ]; then
    echo "Usage: INFOSEC_SSH_USER=<user> $0 <ssh-key> <ip1> [ip2 ip3 ...]"
    exit 1
fi

: "${INFOSEC_SSH_USER:?Set INFOSEC_SSH_USER env var (SSH match user for UsePAM block)}"

SSH_KEY="$1"
shift

for ip in "$@"; do
    echo "🔍 Checking SSH to $ip ..."
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ec2-user@"$ip" "echo 'SSH connection successful'" 2>/dev/null

    if [ $? -eq 0 ]; then
        echo "✅ SSH Success: $ip"

        echo "🔎 Ensuring 'UsePAM no' for ${INFOSEC_SSH_USER:?Set INFOSEC_SSH_USER env var} on $ip ..."
        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$ip" "
            MATCH_BLOCK=\"Match User ${INFOSEC_SSH_USER}\"
            PAM_SETTING=\"    UsePAM no\"
            FILE=\"/etc/ssh/sshd_config\"

            if ! grep -q \"\$MATCH_BLOCK\" \"\$FILE\"; then
                echo \"⚙️ Adding Match block for ${INFOSEC_SSH_USER}...\"
                echo -e \"\\n\$MATCH_BLOCK\\n\$PAM_SETTING\" | sudo tee -a \"\$FILE\" > /dev/null
                sudo systemctl reload sshd
                echo \"✅ Match block added and sshd reloaded.\"
            elif ! awk \"/\$MATCH_BLOCK/,/^Match/{ if (\\\$1 == \\\"UsePAM\\\" && \\\$2 == \\\"no\\\") found=1 } END { exit !found }\" \"\$FILE\"; then
                echo \"⚠️ Match block exists but missing 'UsePAM no'. Updating...\"
                sudo sed -i \"/\$MATCH_BLOCK/,/^Match/{/UsePAM/ s/.*/    UsePAM no/}\" \"\$FILE\"
                sudo systemctl reload sshd
                echo \"✅ Updated Match block and reloaded sshd.\"
            else
                echo \"✅ 'UsePAM no' already correctly set for ${INFOSEC_SSH_USER}\"
            fi
        "
    else
        echo "❌ SSH Failed: $ip"
    fi
done

