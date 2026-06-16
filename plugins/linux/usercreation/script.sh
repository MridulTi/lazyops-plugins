#!/bin/bash

CONFIG_FILE="usercreation.conf"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Config file $CONFIG_FILE not found!"
    exit 1
fi

# Parse SSH key path (key=...)
SSH_KEY=$(grep '^key=' "$CONFIG_FILE" | head -n1 | cut -d'=' -f2- | tr -d ' ')

if [[ -z "$SSH_KEY" || ! -f "$SSH_KEY" ]]; then
    echo "Invalid or missing SSH key path in config file"
    exit 1
fi

# Parse servers under [servers] section
SERVERS=()
in_servers=0
while IFS= read -r line; do
    [[ "$line" =~ ^#.*$ ]] && continue  # skip comments
    if [[ "$line" =~ ^\[servers\]$ ]]; then
        in_servers=1
        continue
    elif [[ "$line" =~ ^\[.*\]$ ]]; then
        in_servers=0
    fi
    if (( in_servers )); then
        [[ -z "$line" ]] && continue
        SERVERS+=("$line")
    fi
done < "$CONFIG_FILE"

# Parse users under [users] section
USERS=()
in_users=0
while IFS= read -r line; do
    [[ "$line" =~ ^#.*$ ]] && continue  # skip comments
    if [[ "$line" =~ ^\[users\]$ ]]; then
        in_users=1
        continue
    elif [[ "$line" =~ ^\[.*\]$ ]]; then
        in_users=0
    fi
    if (( in_users )); then
        [[ -z "$line" ]] && continue
        USERS+=("$line")
    fi
done < "$CONFIG_FILE"

if [ ${#SERVERS[@]} -eq 0 ]; then
    echo "No servers found in config"
    exit 1
fi

if [ ${#USERS[@]} -eq 0 ]; then
    echo "No users found in config"
    exit 1
fi

CURRENT_HOST=$(hostname)

# Create users locally
echo "Creating users on current server: $CURRENT_HOST"
for user in "${USERS[@]}"; do
    id -u "$user" > /dev/null 2>&1 || sudo useradd -m "$user" && echo "User $user created on $CURRENT_HOST" || echo "Failed to create $user on $CURRENT_HOST"
done

# Function to create users remotely
create_users() {
    local server="$1"
    local pubkey_file="/home/ec2-user/.ssh/authorized_keys"
    local password="TempPass123!"

    echo "Creating users and deploying authorized_keys on $server ..."

    # Copy public key to remote server
    scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$pubkey_file" ec2-user@"$server":/tmp/pubkey

    for user in "${USERS[@]}"; do
        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$server" <<EOF
            sudo bash -c '
                # Create user if missing
                if ! id -u "$user" > /dev/null 2>&1; then
                    useradd -m -s /bin/bash "$user"
                    echo "User $user created"
                fi

                # Set password and force reset
                echo "$user:$password" | chpasswd
                chage -d 0 "$user"

                # Setup authorized_keys
                mkdir -p /home/$user/.ssh
                cp /tmp/pubkey /home/$user/.ssh/authorized_keys
                chown -R $user:$user /home/$user/.ssh
                chmod 700 /home/$user/.ssh
                chmod 600 /home/$user/.ssh/authorized_keys

                # Add sudoers file for the user
                echo "$user ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/$user
                chmod 440 /etc/sudoers.d/$user

                # Enable password login for the user
                if ! grep -q "^Match User $user" /etc/ssh/sshd_config; then
                    echo -e "\\nMatch User $user\\n    PasswordAuthentication yes" >> /etc/ssh/sshd_config
                fi
            '
EOF
    done

    # Test SSH config and reload if valid
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$server" "sudo sshd -t && sudo systemctl restart sshd || echo '⚠️ sshd config error — not restarted'"

    # Cleanup temp public key
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$server" "rm -f /tmp/pubkey"
}


# Iterate over servers and create users remotely
for server in "${SERVERS[@]}"; do
    if [[ "$server" == "$CURRENT_HOST" ]]; then
        echo "Skipping current host $server (already handled)."
        continue
    fi

    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ec2-user@"$server" "echo 'SSH connection successful'" >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "✅ SSH Success: $server"
        create_users "$server"
    else
        echo "❌ SSH Failed: $server - skipping user creation"
    fi
done

