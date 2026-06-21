#!/usr/bin/env bash
# Repair Qualys agent on servers where it is failed or in error.
# Uses same config and server selection as install_qualys_from_s3.sh;
# only downloads from S3 and reinstalls on hosts where Qualys is failed or error.
[ -z "$BASH_VERSION" ] && {
  echo "❌ This script must be run with bash"
  exit 1
}
set -euo pipefail

############################################
# ARG CHECK
############################################
[[ $# -lt 1 ]] && echo "Usage: $0 <config_file>" && exit 1
CONFIG_FILE="$1"
[[ ! -f "$CONFIG_FILE" ]] && echo "❌ Config file '$CONFIG_FILE' not found!" && exit 1

source "$CONFIG_FILE"

############################################
# REQUIRED CONFIG VARS (same as install_qualys_from_s3.sh)
############################################
REQUIRED_VARS=(
  ACTIVATION_ID
  CUSTOMER_ID
  SERVER_URI
  S3_BUCKET_PATH
  AWS_REGION
  IPS_MODE
  SSH_KEY_DIR
)

for var in "${REQUIRED_VARS[@]}"; do
    [[ -z "${!var:-}" ]] && echo "❌ Missing $var in config file" && exit 1
done

############################################
# AGENT FILES (same as install_qualys_from_s3.sh)
############################################
AGENT_UBUNTU_X64="Qualys_Linux_X64.deb"
AGENT_UBUNTU_ARM="Qualys_Linux_ARM64.deb"
AGENT_AMZN_X64="Qualys_Linux_X64.rpm"
AGENT_AMZN_ARM="Qualys_Linux_ARM64.rpm"

############################################
# SSH USERS
############################################
SSH_USERS=("ec2-user" "ubuntu" "centos")

############################################
# LOAD SSH KEYS (Bash-3 safe)
############################################
SSH_KEYS=()

while IFS= read -r key; do
    [[ -n "$key" ]] && SSH_KEYS+=("$key")
done <<EOF
$(find "$SSH_KEY_DIR" -type f -name "*.pem" 2>/dev/null)
EOF

if [[ ${#SSH_KEYS[@]} -eq 0 ]]; then
    echo "❌ No SSH keys found in $SSH_KEY_DIR"
    exit 1
fi

############################################
# FETCH IPS (same logic as install_qualys_from_s3.sh)
############################################
IPS=()
IPS_FILE="/tmp/qualys_ips.txt"

fetch_ips_by_tags() {
    set +e
    echo "🔎 Fetching IPs using tag filters:"
    for f in "${TAG_FILTERS[@]}"; do
        echo "   - $f"
    done

    aws ec2 describe-instances \
        --region "$AWS_REGION" \
        --filters "${TAG_FILTERS[@]}" \
        --query 'Reservations[].Instances[]' \
        --output json > /tmp/instances.json

    [[ ! -s /tmp/instances.json ]] && {
        echo "❌ No instances found"
        exit 1
    }

    echo "🚫 Applying exclusion filters:"
    for e in "${EXCLUDE_TAGS[@]}"; do
        echo "   - NOT $e"
    done

    JQ_EXCLUDE='.'
    for ex in "${EXCLUDE_TAGS[@]}"; do
        KEY="${ex%%=*}"
        VAL="${ex##*=}"
        JQ_EXCLUDE="$JQ_EXCLUDE | select((.Tags // []) | map(select(.Key==\"$KEY\" and .Value==\"$VAL\")) | length == 0)"
    done

    jq -r "
      .[]
      | select(.PrivateIpAddress != null)
      | $JQ_EXCLUDE
      | .PrivateIpAddress
    " /tmp/instances.json > "$IPS_FILE"

    [[ ! -s "$IPS_FILE" ]] && {
        echo "❌ No IPs left after exclusions"
        exit 1
    }
    set -e
}

if [[ "$IPS_MODE" == "tag" ]]; then
    fetch_ips_by_tags

    IPS=()
    while IFS= read -r ip; do
        [[ -n "$ip" ]] && IPS+=("$ip")
    done < "$IPS_FILE"

elif [[ "$IPS_MODE" == "static" ]]; then
    IPS=($IPS)

else
    echo "❌ Invalid IPS_MODE"
    exit 1
fi

############################################
# CONFIRMATION
############################################

echo
echo "🧾 Target instances (will check Qualys status and reinstall only if failed/error):"
nl -w2 -s'. ' "$IPS_FILE"
echo

read -rp "Proceed? (yes/no): " CONFIRM
[[ "$CONFIRM" != "yes" ]] && {
    echo "❌ Aborted by user"
    exit 0
}

############################################
# MAIN LOOP: check status, reinstall only if failed/error
############################################
for ip in "${IPS[@]}"; do
    echo "🔍 Processing $ip"
    CONNECTED=false
    NEEDS_REPAIR=false

    for USER in "${SSH_USERS[@]}"; do
        for KEY in "${SSH_KEYS[@]}"; do
            echo "➡️ Checking status: $USER@$ip with $(basename "$KEY")"
            set +e
            STATUS=$(ssh -i "$KEY" \
                -o BatchMode=yes \
                -o ConnectTimeout=5 \
                -o StrictHostKeyChecking=no \
                -o IdentitiesOnly=yes \
                "$USER@$ip" 'bash -s' <<'CHECK'
AGENT_BIN="/usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh"
SERVICE="qualys-cloud-agent"
if [[ ! -x "$AGENT_BIN" ]]; then echo "QUALYS_NOT_INSTALLED"; exit 1; fi
if systemctl is-failed --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_FAILED"; exit 1; fi
if ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_INACTIVE"; exit 1; fi
echo "QUALYS_OK"; exit 0
CHECK
            )
            CHECK_RC=$?
            set -e

            if [[ $CHECK_RC -ne 0 ]]; then
                echo "⚠️ SSH check failed for $ip as $USER — trying next user/key"
                continue
            fi

            CONNECTED=true
            if [[ "$STATUS" == "QUALYS_OK" ]]; then
                echo "✅ $ip — Qualys OK, skipping"
            else
                echo "🔧 $ip — $STATUS, will reinstall"
                NEEDS_REPAIR=true
            fi
            break 2
        done
    done

    if [[ "$CONNECTED" == false ]]; then
        echo "❌ Could not connect to $ip — skipping"
        echo "--------------------------------------------"
        continue
    fi

    if [[ "$NEEDS_REPAIR" != true ]]; then
        echo "--------------------------------------------"
        continue
    fi

    # Reinstall using same steps as install_qualys_from_s3.sh
    for USER in "${SSH_USERS[@]}"; do
        for KEY in "${SSH_KEYS[@]}"; do
            echo "➡️ Reinstalling on $ip as $USER with $(basename "$KEY")"
            set +e
            ssh -i "$KEY" \
                -o BatchMode=yes \
                -o ConnectTimeout=5 \
                -o StrictHostKeyChecking=no \
                -o IdentitiesOnly=yes \
                "$USER@$ip" bash <<EOF
AGENT_UBUNTU_X64="$AGENT_UBUNTU_X64"
AGENT_UBUNTU_ARM="$AGENT_UBUNTU_ARM"
AGENT_AMZN_X64="$AGENT_AMZN_X64"
AGENT_AMZN_ARM="$AGENT_AMZN_ARM"
S3_BUCKET_PATH="$S3_BUCKET_PATH"
ACTIVATION_ID="$ACTIVATION_ID"
CUSTOMER_ID="$CUSTOMER_ID"
SERVER_URI="$SERVER_URI"

AGENT_BIN="/usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh"
SERVICE="qualys-cloud-agent"

# Stop before reinstall
sudo systemctl stop "\$SERVICE" 2>/dev/null || true

ARCH=\$(uname -m)
if [[ -f /etc/os-release ]]; then
    OS_ID=\$(. /etc/os-release && echo \$ID)
else
    OS_ID="unknown"
fi

case "\$OS_ID-\$ARCH" in
    ubuntu-x86_64)
        AGENT="\$AGENT_UBUNTU_X64"
        S3="\$S3_BUCKET_PATH/ubuntu"
        aws s3 cp "\$S3/\$AGENT" /tmp/\$AGENT || exit 1
        sudo dpkg -i /tmp/\$AGENT || true
        ;;
    ubuntu-aarch64|ubuntu-arm64)
        AGENT="\$AGENT_UBUNTU_ARM"
        S3="\$S3_BUCKET_PATH/ubuntu"
        aws s3 cp "\$S3/\$AGENT" /tmp/\$AGENT || exit 1
        sudo dpkg -i /tmp/\$AGENT || true
        ;;
    amzn-x86_64)
        AGENT="\$AGENT_AMZN_X64"
        S3="\$S3_BUCKET_PATH/amazon-linux"
        aws s3 cp "\$S3/\$AGENT" /tmp/\$AGENT || exit 1
        sudo rpm -Uvh /tmp/\$AGENT || true
        ;;
    amzn-aarch64|amzn-arm64)
        AGENT="\$AGENT_AMZN_ARM"
        S3="\$S3_BUCKET_PATH/amazon-linux"
        aws s3 cp "\$S3/\$AGENT" /tmp/\$AGENT || exit 1
        sudo rpm -Uvh /tmp/\$AGENT || true
        ;;
    *)
        echo "❌ Unsupported OS/ARCH: \$OS_ID-\$ARCH"
        exit 1
        ;;
esac

# Activate and start (same as install script)
sudo "\$AGENT_BIN" ActivationId=\$ACTIVATION_ID CustomerId=\$CUSTOMER_ID ServerUri=\$SERVER_URI
sudo systemctl start "\$SERVICE"
systemctl is-active --quiet "\$SERVICE"
EOF
            SSH_RC=$?
            set -e

            if [[ $SSH_RC -eq 0 ]]; then
                echo "🟢 Reinstall success on $ip as $USER"
                break 2
            else
                echo "⚠️ Reinstall failed: $ip as $USER with $(basename "$KEY")"
            fi
        done
    done

    echo "--------------------------------------------"
done
