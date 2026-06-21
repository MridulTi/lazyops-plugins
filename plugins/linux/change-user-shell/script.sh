#!/bin/bash

# Check if enough arguments are passed
if [ $# -lt 3 ]; then
    echo "Usage: $0 <target_username> <ssh_key> <ip1> [ip2 ip3 ...]"
    exit 1
fi

TARGET_USER="$1"  # The user to check/change on remote
SSH_KEY="$2"
shift 2

# Loop through each IP
for ip in "$@"; do
    echo "Connecting to $ip..."

    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$ip" "bash -s" <<EOF
if id "$TARGET_USER" &>/dev/null; then
    USER_SHELL=\$(getent passwd "$TARGET_USER" | cut -d: -f7)
    if [[ "\$USER_SHELL" == *nologin* ]]; then
        echo "User '$TARGET_USER' has shell '\$USER_SHELL'. Changing to /bin/bash..."
        sudo usermod -s /bin/bash "$TARGET_USER" && echo "✅ Shell updated successfully." || echo "❌ Failed to update shell."
    else
        echo "ℹ️  User '$TARGET_USER' already has valid shell: \$USER_SHELL"
    fi
else
    echo "❌ User '$TARGET_USER' does not exist on host."
fi
EOF

    echo "Finished with $ip"
    echo "------------------------"
done

