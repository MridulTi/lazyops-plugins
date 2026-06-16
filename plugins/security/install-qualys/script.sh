#!/bin/bash

[[ $# -lt 1 ]] && echo "Usage: $0 <config_file>" && exit 1
CONFIG_FILE="$1"
[[ ! -f "$CONFIG_FILE" ]] && echo "❌ Config file '$CONFIG_FILE' not found!" && exit 1

source "$CONFIG_FILE"

REQUIRED_VARS=(SSH_KEY IPS ACTIVATION_ID CUSTOMER_ID SERVER_URI AGENT_PACKAGE_PATH)
for var in "${REQUIRED_VARS[@]}"; do
    [[ -z "${!var}" ]] && echo "❌ Missing $var in config file." && exit 1
done

for ip in $IPS; do
    echo "🔍 Connecting to $ip ..."
    if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@"$ip" "echo SSH OK" &>/dev/null; then
        echo "✅ SSH Success"

        if ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ubuntu@"$ip" "systemctl is-active qualys-cloud-agent" | grep -q active; then
            echo "🟢 Agent already running on $ip. Skipping."
            continue
        fi

        echo "📦 Installing Qualys agent..."
        scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$AGENT_PACKAGE_PATH" ubuntu@"$ip":/tmp/
        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ubuntu@"$ip" bash -c "'
            sudo dpkg -i /tmp/QualysCloudAgentarm64.deb && \
            sudo /usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh ActivationId=$ACTIVATION_ID CustomerId=$CUSTOMER_ID ServerUri=$SERVER_URI
        '"

        ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ubuntu@"$ip" "sudo service qualys-cloud-agent status"
        echo "✅ Setup complete on $ip"
    else
        echo "❌ SSH Failed: $ip"
    fi
    echo "----------------------------------------------------"
done

