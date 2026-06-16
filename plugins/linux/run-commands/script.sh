#!/bin/bash

# === CONFIG ===
KEY_PATH="~/Documents/bitbucket/All_Keys/payments-new.pem"     # path to SSH key
USER="ec2-user"                    # SSH username
SCRIPT="remote_script.sh"          # script to run remotely
CONFIG_FILE="${CONFIG_FILE:-$1}"           # list of IPs

# === CHECKS ===
if [ ! -f "$SCRIPT" ]; then
  echo "❌ Script file '$SCRIPT' not found!"
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "❌ Config file '$CONFIG_FILE' not found!"
  exit 1
fi

# === MAIN LOOP ===
for IP in $(cat "$CONFIG_FILE"); do
  echo "----------------------------------------"
  echo "🚀 Running script on $IP"
  echo "----------------------------------------"

  scp -i "$KEY_PATH" -o StrictHostKeyChecking=no "$SCRIPT" "$USER@$IP:/tmp/$SCRIPT"
  ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no "$USER@$IP" "sudo bash /tmp/$SCRIPT"

  echo ""
done

echo "✅ Script executed on all servers!"

