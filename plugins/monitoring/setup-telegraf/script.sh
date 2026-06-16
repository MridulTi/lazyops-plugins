#!/bin/bash

# ======================================
#  Multi-server Telegraf Setup Script
# ======================================
# Usage: ./install_telegraf.sh telegraf.conf
# Requires: config.ini (list of IPs) and SSH key with root/ec2-user access

CONFIG_FILE="${CONFIG_FILE:-$1}"
TELEGRAF_CONF_FILE="$1"
SSH_KEY="~/Documents/bitbucket/All_Keys/payments-new.pem"
USER="ec2-user"
REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$REGION" ]]; then
  echo "ERROR: Set REGION or AWS_REGION" >&2
  exit 1
fi

if [ -z "$TELEGRAF_CONF_FILE" ]; then
  echo "❌ Usage: $0 <telegraf.conf>"
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "❌ Missing config.ini file!"
  exit 1
fi

if [ ! -f "$TELEGRAF_CONF_FILE" ]; then
  echo "❌ telegraf.conf file not found!"
  exit 1
fi

echo "🔹 Starting Telegraf installation on servers in region: $REGION..."

for ip in $(cat "$CONFIG_FILE"); do
  echo "--------------------------------------------"
  echo "🚀 Setting up Telegraf on $ip ..."
  echo "--------------------------------------------"

  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no $USER@$ip "bash -s" <<'EOF'
ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
  TELEGRAF_URL="https://repos.influxdata.com/rhel/7/aarch64/stable/telegraf-1.26.0-1.aarch64.rpm"
else
  TELEGRAF_URL="https://repos.influxdata.com/rhel/7/x86_64/stable/telegraf-1.26.0-1.x86_64.rpm"
fi

echo "📦 Downloading Telegraf RPM..."
wget -q \$TELEGRAF_URL -O /tmp/telegraf.rpm

if sudo yum install -y /tmp/telegraf.rpm; then
  echo "✅ Telegraf installed successfully."
else
  echo "❌ Installation failed for \$TELEGRAF_URL"
  exit 1
fi

sudo systemctl stop telegraf || true
EOF

  echo "📁 Uploading telegraf.conf to $ip..."
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$TELEGRAF_CONF_FILE" $USER@$ip:/tmp/telegraf.conf

  echo "⚙️  Moving config and starting Telegraf..."
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no $USER@$ip "
    sudo mv /tmp/telegraf.conf /etc/telegraf/telegraf.conf &&
    sudo chown root:root /etc/telegraf/telegraf.conf &&
    sudo chmod 644 /etc/telegraf/telegraf.conf &&
    sudo systemctl enable telegraf &&
    sudo systemctl restart telegraf
  "

  echo "🔍 Checking status on $ip..."
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no $USER@$ip "sudo systemctl status telegraf --no-pager | head -15"

done

echo "🎉 Telegraf setup completed on all servers!"

