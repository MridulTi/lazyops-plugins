#!/bin/bash

COMMAND="/opt/traps/bin/cytool reconnect force 954b23c390ac4f04b7c05152743b6dda"
USER="ec2-user"
IP_FILE="${IP_FILE:-$1}"

KEY_DIR="$HOME/Documents/bitbucket/All-Keys"

while read -r IP || [[ -n "$IP" ]]; do

    [[ -z "$IP" ]] && continue

    echo "🔗 Connecting to $IP..."

    SUCCESS=false

    for KEY_FILE in "$KEY_DIR"/*.pem; do

        [[ ! -f "$KEY_FILE" ]] && continue

        echo "   ➤ Trying key: $(basename "$KEY_FILE")"

        ssh -o StrictHostKeyChecking=no \
            -o BatchMode=yes \
            -o ConnectTimeout=5 \
            -i "$KEY_FILE" \
            "$USER@$IP" "$COMMAND" >/dev/null 2>&1

        if [[ $? -eq 0 ]]; then
            echo "✅ Success on $IP using $(basename "$KEY_FILE")"
            SUCCESS=true
            break
        fi
    done

    if [[ "$SUCCESS" = false ]]; then
        echo "❌ All keys failed for $IP"
    fi

    echo "-----------------------------"

done < "$IP_FILE"
