#!/usr/bin/env bash
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
# REQUIRED CONFIG VARS
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
# AGENT FILES
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
# FETCH IPS
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

    # Build jq exclusion dynamically
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
echo "🧾 Target instances:"
nl -w2 -s'. ' "$IPS_FILE"
echo

read -rp "Proceed with Qualys installation on these hosts? (yes/no): " CONFIRM
[[ "$CONFIRM" != "yes" ]] && {
    echo "❌ Aborted by user"
    exit 0
}

############################################
# MAIN LOOP (FAILURE SAFE)
############################################
for ip in "${IPS[@]}"; do
    echo "🔍 Processing $ip"
    CONNECTED=false

    for USER in "${SSH_USERS[@]}"; do
        for KEY in "${SSH_KEYS[@]}"; do
            echo "➡️ Trying $USER@$ip with $(basename "$KEY")"
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

# Define agent path and service
AGENT_BIN="/usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh"
SERVICE="qualys-cloud-agent"

# If already installed, skip download
if [[ -x "\$AGENT_BIN" ]]; then
    echo "✅ Qualys agent already installed"
else
    ARCH=\$(uname -m)
    # Safe OS_ID assignment
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
            sudo rpm -ivh /tmp/\$AGENT || true
            ;;
        amzn-aarch64|amzn-arm64)
            AGENT="\$AGENT_AMZN_ARM"
            S3="\$S3_BUCKET_PATH/amazon-linux"
            aws s3 cp "\$S3/\$AGENT" /tmp/\$AGENT || exit 1
            sudo rpm -ivh /tmp/\$AGENT || true
            ;;
        *)
            echo "❌ Unsupported OS/ARCH: \$OS_ID-\$ARCH"
            exit 1
            ;;
    esac
fi

# Always run activation
sudo "\$AGENT_BIN" ActivationId=$ACTIVATION_ID CustomerId=$CUSTOMER_ID ServerUri=$SERVER_URI
sudo systemctl start "\$SERVICE"
systemctl is-active --quiet "\$SERVICE"
EOF
            SSH_RC=$?
	    set -e

            if [[ $SSH_RC -eq 0 ]]; then
                CONNECTED=true
                echo "🟢 Success on $ip as $USER"
		break 2
            else
                echo "⚠️ Failed: $ip as $USER with $(basename "$KEY")"
            fi
        done
    done

    if [[ "$CONNECTED" == false ]]; then
        echo "❌ All attempts failed for $ip — continuing"
    fi

    echo "--------------------------------------------"
done

