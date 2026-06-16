#!/bin/bash

# -------- CONFIG --------
IPS_FILE="${IPS_FILE:-$1}"
SSH_KEY_DIR="${SSH_KEY_DIR:?Set SSH_KEY_DIR}"

USERS=("ec2-user" "ubuntu" "centos")

ACTIVATION_IDS_FILE="${ACTIVATION_IDS_FILE:-activation-ids.txt}"
if [[ ! -f "$ACTIVATION_IDS_FILE" ]]; then echo "Set ACTIVATION_IDS_FILE" >&2; exit 1; fi
mapfile -t EXPECTED_ACTIVATION_IDS < <(grep -v "^#" "$ACTIVATION_IDS_FILE" | grep -v "^[[:space:]]*$" || true)

DIST_IDS_FILE="${DIST_IDS_FILE:-dist-ids.txt}"
if [[ ! -f "$DIST_IDS_FILE" ]]; then echo "Set DIST_IDS_FILE" >&2; exit 1; fi
mapfile -t VALID_DIST_IDS < <(grep -v "^#" "$DIST_IDS_FILE" | grep -v "^[[:space:]]*$" || true)

QUALYS_FAILED=()
CORTEX_FAILED=()

# -------- SSH CONNECT --------
ssh_connect() {
    local ip=$1

    for user in "${USERS[@]}"; do
        for key in "$SSH_KEY_DIR"/*; do
            ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$key" "$user@$ip" "echo ok" &>/dev/null
            if [[ $? -eq 0 ]]; then
                echo "$user|$key"
                return 0
            fi
        done
    done
    return 1
}

# -------- MAIN LOOP --------
while IFS= read -r IP || [[ -n "$IP" ]]; do
    IP=$(echo "$IP" | xargs)
    [[ -z "$IP" || "$IP" =~ ^# ]] && continue

    echo "🔍 Processing $IP"

    CONN=$(ssh_connect "$IP")
    if [[ $? -ne 0 ]]; then
        echo "   ❌ SSH Failed"
        QUALYS_FAILED+=("$IP")
        CORTEX_FAILED+=("$IP")
        continue
    fi

    USER=$(echo "$CONN" | cut -d'|' -f1)
    KEY=$(echo "$CONN" | cut -d'|' -f2)

    echo "   ✅ Connected via $USER"

    # -------- QUALYS CHECK --------
    QUALYS_STATUS=$(ssh -n -i "$KEY" "$USER@$IP" "
        sudo systemctl is-active qualys-cloud-agent 2>/dev/null ||
        (ps -ef | grep -i qualys | grep -v grep >/dev/null && echo running || echo stopped)
    ")

    ACTIVATION_ID=$(ssh -n -i "$KEY" "$USER@$IP" "
        sudo grep -i activation /etc/qualys/cloud-agent/qualys-cloud-agent.conf 2>/dev/null | awk -F= '{print \$2}'
    " | tr -d '[:space:]')

    VALID_ACTIVATION=false
    for id in "${EXPECTED_ACTIVATION_IDS[@]}"; do
        [[ "$ACTIVATION_ID" == "$id" ]] && VALID_ACTIVATION=true && break
    done

    if [[ "$QUALYS_STATUS" != "active" && "$QUALYS_STATUS" != "running" ]] || [[ "$VALID_ACTIVATION" != true ]]; then
        echo "   ❌ Qualys issue (Status: $QUALYS_STATUS, Activation: $ACTIVATION_ID)"
        QUALYS_FAILED+=("$IP")
    else
        echo "   ✅ Qualys OK"
    fi

    # -------- CORTEX CHECK --------
    CORTEX_RUNNING=$(ssh -n -i "$KEY" "$USER@$IP" "
        ps -ef | grep pmd | grep -v grep >/dev/null && echo running || echo stopped
    ")

    DIST_IDS=$(ssh -n -i "$KEY" "$USER@$IP" "
        sudo grep -h -i -- '--distribution-id' \
            /etc/panw/cortex.conf 2>/dev/null | \
        sed -E 's/.*--distribution-id[[:space:]]+([^[:space:]]+).*/\1/'
    ")

    echo "distribution id : $DIST_IDS"
    
    VALID=false

    for id in "${VALID_DIST_IDS[@]}"; do
        if echo "$DIST_IDS" | grep -qx "$id"; then
            VALID=true
            break
        fi
    done
    
    if [[ "$CORTEX_RUNNING" != "running" ]] || [[ "$VALID" != true ]]; then
        echo "   ❌ Cortex issue"
        CORTEX_FAILED+=("$IP")
    else
        echo "   ✅ Cortex OK"
    fi

done < "$IPS_FILE"

# -------- FINAL OUTPUT --------
echo ""
echo "================ FINAL RESULT ================"

echo "❌ Qualys Failed IPs:"
echo "${QUALYS_FAILED[@]}" | tr ' ' ','

echo ""
echo "❌ Cortex Failed IPs:"
echo "${CORTEX_FAILED[@]}" | tr ' ' ','
