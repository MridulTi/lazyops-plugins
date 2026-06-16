#!/bin/bash

# --- Configuration ---
CONFIG_FILE="${CONFIG_FILE:-$1}"           # List of server IPs, one per line
URL="https://github.com/oliver006/redis_exporter/releases/download/v1.59.0/redis_exporter-v1.59.0.linux-arm64.tar.gz"
EXPORTER_BIN="/usr/bin/redis_exporter"
REDIS_PASSWORD="r3dis_emnF3ZW50"
MASTER_PORT=7000
SLAVE_PORT=7001
SSH_KEY="~/Documents/bitbucket/All_Keys/payments-new.pem"

# --- Check if file exists ---
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "❌ Config file '$CONFIG_FILE' not found!"
  exit 1
fi

# --- Read each IP and perform operations ---
while read -r SERVER_IP; do
  [[ -z "$SERVER_IP" ]] && continue  # skip blank lines
  echo "🚀 Setting up Redis Exporter on $SERVER_IP ..."

  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ec2-user@"$SERVER_IP" bash -s <<EOF
    set -e

    echo "📦 Downloading redis_exporter..."
    wget -q "$URL" -O /tmp/redis_exporter.tar.gz
    tar -xzf /tmp/redis_exporter.tar.gz -C /tmp/
    sudo mv /tmp/redis_exporter*/redis_exporter $EXPORTER_BIN
    sudo chmod +x $EXPORTER_BIN

    echo "🛠️ Creating service files..."

    # Master
    sudo bash -c 'cat <<SERVICE > /etc/systemd/system/redis_exporter-master.service
[Unit]
Description=Redis Exporter (Master)
Wants=network-online.target
After=network-online.target

[Service]
User=root
Group=root
Type=simple
ExecStart=$EXPORTER_BIN \\
    -web.listen-address ":9121" \\
    -redis.addr "redis://$SERVER_IP:$MASTER_PORT" \\
    -redis.password "$REDIS_PASSWORD"

[Install]
WantedBy=multi-user.target
SERVICE'

    # Slave
    sudo bash -c 'cat <<SERVICE > /etc/systemd/system/redis_exporter-slave.service
[Unit]
Description=Redis Exporter (Slave)
Wants=network-online.target
After=network-online.target

[Service]
User=root
Group=root
Type=simple
ExecStart=$EXPORTER_BIN \\
    -web.listen-address ":9122" \\
    -redis.addr "redis://$SERVER_IP:$SLAVE_PORT" \\
    -redis.password "$REDIS_PASSWORD"

[Install]
WantedBy=multi-user.target
SERVICE'

    echo "🔄 Reloading and starting services..."
    sudo systemctl daemon-reload
    sudo systemctl enable redis_exporter-master redis_exporter-slave
    sudo systemctl restart redis_exporter-master redis_exporter-slave

    echo "✅ Redis Exporter setup complete on $SERVER_IP"
EOF

done < "$CONFIG_FILE"

echo "🎉 Setup completed on all servers!"

