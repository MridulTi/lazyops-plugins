#!/usr/bin/env bash

# ===== CONFIG =====
KEY_DIR="$HOME/Documents/bitbucket/All_Keys"
USERS=("ec2-user" "ubuntu" "centos")

# Priority keys (in order)
PRIORITY_KEYS=(${SSH_PRIORITY_KEYS:-})

# ===== INPUT =====
read -p "Enter target IP: " TARGET_IP

echo ""
echo "Choose user:"
echo "1) ec2-user"
echo "2) ubuntu"
echo "3) centos"
echo "4) TRY ALL"
read -p "Enter choice [1-4]: " USER_CHOICE

# ===== USER SELECTION =====
case $USER_CHOICE in
  1) SELECTED_USERS=("ec2-user") ;;
  2) SELECTED_USERS=("ubuntu") ;;
  3) SELECTED_USERS=("centos") ;;
  4) SELECTED_USERS=("${USERS[@]}") ;;
  *)
    echo "❌ Invalid option"
    exit 1
    ;;
esac

# ===== BUILD SORTED KEY LIST =====
SORTED_KEYS=()

# Add priority keys first (if they exist)
for PK in "${PRIORITY_KEYS[@]}"; do
  if [[ -f "$KEY_DIR/$PK" ]]; then
    SORTED_KEYS+=("$KEY_DIR/$PK")
  fi
done

# Add remaining keys (excluding already added ones)
for KEY in "$KEY_DIR"/*.pem; do
  BASENAME=$(basename "$KEY")
  if [[ ! " ${PRIORITY_KEYS[*]} " =~ " $BASENAME " ]]; then
    SORTED_KEYS+=("$KEY")
  fi
done

# ===== SSH ATTEMPTS =====
for USER in "${SELECTED_USERS[@]}"; do
  echo ""
  echo "🔍 Trying user: $USER"

  for KEY in "${SORTED_KEYS[@]}"; do
    echo "  🔑 Key: $(basename "$KEY")"

    ssh -o BatchMode=yes \
        -o ConnectTimeout=5 \
        -o StrictHostKeyChecking=no \
        -i "$KEY" "$USER@$TARGET_IP" exit 2>/dev/null

    if [[ $? -eq 0 ]]; then
      echo ""
      echo "✅ SUCCESS!"
      echo "User: $USER"
      echo "Key : $(basename "$KEY")"
      echo "Logging in..."
      ssh -i "$KEY" "$USER@$TARGET_IP"
      exit 0
    fi
  done
done

echo ""
echo "❌ No valid user/key combination worked"
exit 1
