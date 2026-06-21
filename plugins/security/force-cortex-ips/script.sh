#!/bin/bash

CYTOOL_RECONNECT_ID="${CYTOOL_RECONNECT_ID:?Set CYTOOL_RECONNECT_ID env var (32-hex Cortex distribution ID)}"
COMMAND="/opt/traps/bin/cytool reconnect force $CYTOOL_RECONNECT_ID"
USER="${SSH_USER:-ec2-user}"
IP_FILE="${IP_FILE:-$1}"

KEY_DIR="${SSH_KEY_DIR:?Set SSH_KEY_DIR env var (directory containing .pem keys)}"

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
