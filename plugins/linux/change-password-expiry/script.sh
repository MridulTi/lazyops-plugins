#!/bin/bash

if [ $# -lt 3 ]; then
    echo "Usage: $0 <ssh-key> <user> <username> <ip1> [ip2 ip3 ...]"
    exit 1
fi

SSH_KEY="$1"
USER="$2"
USERNAME="$3"
shift 3

for ip in "$@"; do
    echo "🔗 Connecting to $ip as $USER ..."
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$USER@$ip" "
        echo '✅ SSH connection successful';

        echo '🔍 Checking account and password expiry...';
        EXPIRED=0
        OUTPUT=\$(sudo chage -l $USERNAME)

        echo \"\$OUTPUT\" | grep -q 'Account expires[[:space:]]*:.*[0-9]' && {
            ACCOUNT_EXPIRY=\$(echo \"\$OUTPUT\" | grep 'Account expires' | awk -F: '{print \$2}' | xargs)
            if [ \"\$ACCOUNT_EXPIRY\" != \"never\" ]; then
                echo \"⚠️ Account Expiry: \$ACCOUNT_EXPIRY\"
                EXPIRED=1
            fi
        }

        echo \"\$OUTPUT\" | grep -q 'Password expires[[:space:]]*:.*[0-9]' && {
            PASSWORD_EXPIRY=\$(echo \"\$OUTPUT\" | grep 'Password expires' | awk -F: '{print \$2}' | xargs)
            if [ \"\$PASSWORD_EXPIRY\" != \"never\" ]; then
                echo \"⚠️ Password Expiry: \$PASSWORD_EXPIRY\"
                EXPIRED=1
            fi
        }

        if [ \"\$EXPIRED\" -eq 1 ]; then
            echo '🔧 Removing password expiration...';
            sudo chage -M -1 -E -1 $USERNAME && echo '✅ Password expiry removed';
        else
            echo '✅ Account and password are not expired';
        fi
    " 2>/dev/null

    if [ $? -eq 0 ]; then
        echo "✅ Success: $ip"
    else
        echo "❌ SSH Failed or script error: $ip"
    fi

    echo "----------------------------------"
done

