#!/bin/bash

if [ $# -lt 4 ]; then
  echo "Usage: $0 <username> <ssh_key> <public_key_file> <ip1> [ip2 ip3 ...]"
  exit 1
fi

USERNAME="$1"
SSH_KEY="$2"
PUBKEY_FILE="$3"
shift 3

if [ ! -f "$PUBKEY_FILE" ]; then
  echo "Public key file '$PUBKEY_FILE' not found!"
  exit 1
fi

PUBKEY_CONTENT=$(cat "$PUBKEY_FILE")

# Array to store failed IPs
failed_ips=()

for IP in "$@"; do
  echo "Processing $IP ..."
  
  ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY" "$USERNAME@$IP" bash -c "'
    grep -qxF \"$PUBKEY_CONTENT\" ~/.ssh/authorized_keys || echo \"$PUBKEY_CONTENT\" >> ~/.ssh/authorized_keys
  '"
  
  if [ $? -eq 0 ]; then
    echo "✅ Key appended successfully on $IP"
  else
    echo "❌ Failed to append key on $IP"
    failed_ips+=("$IP")
  fi
done

# Summary of failures
if [ ${#failed_ips[@]} -gt 0 ]; then
  echo -e "\n⚠️  Failed to append key on the following IP(s):"
  for ip in "${failed_ips[@]}"; do
    echo " - $ip"
  done
else
  echo -e "\n🎉 Key appended successfully on all IPs!"
fi

