#!/bin/bash

if [ $# -lt 2 ]; then
    echo "Usage: $0 <ssh-key> <ip1> [ip2 ip3 ...]"
    exit 1
fi

SSH_KEY="$1"
shift

found_users=()

for ip in "$@"; do
    echo "🔍 Checking $ip ..."
    users=$(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ec2-user@"$ip" '
        grep -i "infosec" /etc/passwd | cut -d: -f1 | while read user; do
            auth_file="/home/$user/.ssh/authorized_keys"
            if sudo test -f "$auth_file"; then
                if sudo grep -q "shivam" "$auth_file"; then
                    echo "$user"
                fi
            fi
        done
    ' 2>/dev/null)

    if [ -n "$users" ]; then
        for u in $users; do
            echo "✅ Found user: $u on $ip"
            found_users+=("$u")
        done
    else
        echo "❌ No matching user on $ip"
    fi
done

# Print distinct usernames
if [ ${#found_users[@]} -gt 0 ]; then
    echo -e "\n✅ Distinct list of usernames with 'shivam' key:"
    printf "%s\n" "${found_users[@]}" | sort -u
else
    echo -e "\nℹ️ No users matched the criteria."
fi

