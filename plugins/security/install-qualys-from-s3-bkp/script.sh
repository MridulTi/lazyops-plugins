#!/bin/bash

[[ $# -lt 1 ]] && echo "Usage: $0 <config_file>" && exit 1
CONFIG_FILE="$1"
[[ ! -f "$CONFIG_FILE" ]] && echo "❌ Config file '$CONFIG_FILE' not found!" && exit 1

source "$CONFIG_FILE"

REQUIRED_VARS=(SSH_KEY IPS ACTIVATION_ID CUSTOMER_ID SERVER_URI S3_BUCKET_PATH)
for var in "${REQUIRED_VARS[@]}"; do
    [[ -z "${!var}" ]] && echo "❌ Missing $var in config file." && exit 1
done

for ip in $IPS; do
    echo "🔍 Connecting to $ip ..."
    
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@"$ip" bash <<EOF

# Check if agent is already running
if systemctl is-active --quiet qualys-cloud-agent; then
    echo "🟢 Qualys agent is already running on $ip. Skipping installation."
    exit 0
fi

# Detect architecture and OS
ARCH=\$(uname -m)
OS_ID=\$(grep ^ID= /etc/os-release | cut -d= -f2 | tr -d '"')

echo "📋 Detected OS: \$OS_ID, Arch: \$ARCH"

# Determine package name and install command
if [[ "\$ARCH" == "x86_64" ]]; then
    AGENT_BASE="QualysCloudAgentx64"
elif [[ "\$ARCH" == "aarch64" || "\$ARCH" == "arm64" ]]; then
    AGENT_BASE="QualysCloudAgentarm64"
else
    echo "❌ Unsupported architecture: \$ARCH"
    exit 1
fi

if [[ "\$OS_ID" == "ubuntu" || "\$OS_ID" == "debian" ]]; then
    AGENT_FILE="\${AGENT_BASE}.deb"
    INSTALL_CMD="sudo dpkg -i /tmp/\$AGENT_FILE"
elif [[ "\$OS_ID" == "amzn" || "\$OS_ID" == "rhel" || "\$OS_ID" == "centos" ]]; then
    AGENT_FILE="\${AGENT_BASE}.rpm"
    INSTALL_CMD="sudo rpm -ivh /tmp/\$AGENT_FILE"
else
    echo "❌ Unsupported OS: \$OS_ID"
    exit 1
fi

echo "⬇️ Downloading agent package from S3..."
aws s3 cp "$S3_BUCKET_PATH/\$AGENT_FILE" /tmp/\$AGENT_FILE || { echo "❌ Failed to download package"; exit 1; }

echo "📦 Installing agent..."
\$INSTALL_CMD || { echo "❌ Installation failed"; exit 1; }

echo "⚙️ Activating agent..."
sudo /usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh ActivationId=$ACTIVATION_ID CustomerId=$CUSTOMER_ID ServerUri=$SERVER_URI || { echo "❌ Activation failed"; exit 1; }

echo "✅ Qualys agent installed and activated."

echo "🔎 Checking service status..."
systemctl status qualys-cloud-agent | grep -q running && echo "✅ Agent is running." || echo "⚠️ Agent not running."

EOF

    echo "----------------------------------------------------"
done

