#!/usr/bin/env bash
#
# Read Auto Scaling group names from a file, list EC2 private IPs, optionally
# probe / interactive-SSH / run Cortex cytool ON EACH INSTANCE via SSH.
#
# Usage:
#   ./force_cortex_id_change.sh                    # list IPs only
#   KEY_DIRECTORY=~/keys ./force_cortex_id_change.sh --ssh-probe
#   KEY_DIRECTORY=~/keys ./force_cortex_id_change.sh --ssh-login
#   KEY_DIRECTORY=~/keys ./force_cortex_id_change.sh --remote-cytool
#
# Env:
#   ASG_FILE               default: asg.txt
#   KEY_DIRECTORY          required for --ssh-probe, --ssh-login, --remote-cytool
#   CYTOOL_RECONNECT_ID    default: 954b23c390ac4f04b7c05152743b6dda (passed to cytool reconnect force)
#   AWS_REGION             for aws CLI if needed
#

set -euo pipefail

ASG_FILE="${ASG_FILE:-asg.txt}"
KEY_DIRECTORY="${KEY_DIRECTORY:-$HOME/Documents/bitbucket/All_Keys/tmp}"
SSH_USERS=(ec2-user ubuntu centos)
SSH_PROBE_CMD='echo SSH_PROBE_OK'
CYTOOL_RECONNECT_ID="${CYTOOL_RECONNECT_ID:-954b23c390ac4f04b7c05152743b6dda}"

usage() {
  sed -n '2,22p' "$0" | sed 's/^# //'
  echo ""
  echo "Options:"
  echo "  --ssh-probe       Try each IP with all keys × users; print what works"
  echo "  --ssh-login       Interactive ssh -t to each IP (first working user/key)"
  echo "  --remote-cytool   SSH to each IP and run: cytool reconnect force + connectivity check (needs sudo on host)"
  echo "  -h, --help        This help"
}

MODE="list"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-probe) MODE="probe" ;;
    --ssh-login) MODE="login" ;;
    --remote-cytool) MODE="remote-cytool" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
  shift
done

if [[ "$MODE" != "list" ]]; then
  if [[ -z "$KEY_DIRECTORY" || ! -d "$KEY_DIRECTORY" ]]; then
    echo "❌ KEY_DIRECTORY must be set to an existing directory (folder with .pem / id_rsa). Example:"
    echo "   KEY_DIRECTORY=\"\$HOME/.ssh/my_keys\" $0 --ssh-probe"
    exit 1
  fi
  KEY_DIRECTORY="$(cd "$KEY_DIRECTORY" && pwd)"
fi

if [[ ! -f "$ASG_FILE" ]]; then
  echo "❌ ASG file not found: $ASG_FILE"
  exit 1
fi

# Set by find_ssh_creds (globals for bash 3.2)
FOUND_SSH_USER=""
FOUND_SSH_KEY=""

# Collect private IPs from all ASGs in file (unique, stable order)
collect_ips() {
  local -a ALL_IPS=()
  while read -r ASG_NAME; do
    [[ -z "${ASG_NAME//[[:space:]]/}" ]] && continue
    ASG_NAME="$(echo "$ASG_NAME" | xargs)"
    [[ -z "$ASG_NAME" ]] && continue

    echo "🔍 Processing ASG: $ASG_NAME" >&2

    INSTANCE_IDS=$(aws autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$ASG_NAME" \
      --query "AutoScalingGroups[0].Instances[].InstanceId" \
      --output text 2>/dev/null || true)

    if [[ -z "${INSTANCE_IDS// }" ]]; then
      echo "⚠️  No instances found in $ASG_NAME" >&2
      echo "-----------------------------" >&2
      continue
    fi

    local IPS
    IPS=$(aws ec2 describe-instances \
      --instance-ids $INSTANCE_IDS \
      --query "Reservations[].Instances[].PrivateIpAddress" \
      --output text 2>/dev/null || true)

    for IP in $IPS; do
      [[ -n "$IP" ]] && ALL_IPS+=("$IP")
    done
    echo "-----------------------------" >&2
  done < "$ASG_FILE"

  if [[ ${#ALL_IPS[@]} -eq 0 ]]; then
    return 0
  fi
  printf '%s\n' "${ALL_IPS[@]}" | awk '!seen[$0]++'
}

collect_keys() {
  shopt -s nullglob
  local keys=( "$KEY_DIRECTORY"/*.pem "$KEY_DIRECTORY"/id_rsa "$KEY_DIRECTORY"/id_ed25519 "$KEY_DIRECTORY"/id_ecdsa )
  shopt -u nullglob
  if [[ ${#keys[@]} -eq 0 ]]; then
    echo "❌ No private keys found in $KEY_DIRECTORY (*.pem or id_rsa/id_ed25519/id_ecdsa)" >&2
    exit 1
  fi
  printf '%s\n' "${keys[@]}"
}

# Sets FOUND_SSH_USER and FOUND_SSH_KEY on success; returns 1 if no combo works.
find_ssh_creds() {
  local ip="$1"
  local key user out
  FOUND_SSH_USER=""
  FOUND_SSH_KEY=""
  while IFS= read -r key; do
    [[ -f "$key" ]] || continue
    for user in "${SSH_USERS[@]}"; do
      out=$(ssh -o BatchMode=yes -o ConnectTimeout=7 -o StrictHostKeyChecking=no \
        -o IdentitiesOnly=yes -i "$key" "$user@$ip" "$SSH_PROBE_CMD" 2>/dev/null || true)
      if [[ "$out" == *SSH_PROBE_OK* ]]; then
        FOUND_SSH_USER="$user"
        FOUND_SSH_KEY="$key"
        return 0
      fi
    done
  done < <(collect_keys)
  return 1
}

try_ssh_probe() {
  local ip="$1"
  if find_ssh_creds "$ip"; then
    echo "✅ $ip  user=$FOUND_SSH_USER  key=$(basename "$FOUND_SSH_KEY")"
    return 0
  fi
  echo "❌ $ip  no working user/key"
  return 1
}

try_ssh_login() {
  local ip="$1"
  if ! find_ssh_creds "$ip"; then
    echo "❌ $ip — could not connect with any key/user"
    return 1
  fi
  echo ""
  echo "── Interactive SSH: $FOUND_SSH_USER@$ip  (key: $(basename "$FOUND_SSH_KEY"))"
  echo "   (exit the shell to continue to the next IP, or Ctrl+C to stop)"
  ssh -t -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
    -o IdentitiesOnly=yes -i "$FOUND_SSH_KEY" "$FOUND_SSH_USER@$ip" || true
  return 0
}

# Run cytool on the remote host (requires passwordless sudo for cytool, or it may fail)
run_remote_cytool() {
  local ip="$1"
  local id="$CYTOOL_RECONNECT_ID"
  if ! find_ssh_creds "$ip"; then
    echo "❌ $ip — could not SSH; skipping cytool"
    return 1
  fi
  echo ""
  echo "🔄 $ip — cytool reconnect + check (user=$FOUND_SSH_USER, key=$(basename "$FOUND_SSH_KEY"))"
  # Remote script: expand id from local env; \$ for remote vars
  ssh -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=no \
    -o IdentitiesOnly=yes -i "$FOUND_SSH_KEY" "$FOUND_SSH_USER@$ip" bash -s <<EOF
set -e
ID='$id'
CT=/opt/traps/bin/cytool
# Non-interactive SSH: use sudo -n only (configure NOPASSWD for cytool on the host)
if ! sudo -n "\$CT" reconnect force "\$ID"; then
  echo "❌ sudo -n \$CT reconnect failed — need passwordless sudo for \$CT on this host" >&2
  exit 1
fi
sudo -n "\$CT" connectivity_test 2>/dev/null | grep -i distribution || echo "(no distribution line in connectivity check)"
EOF
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "⚠️  $ip — remote cytool exited $rc"
  fi
  echo "-----------------------------"
  return 0
}

# --- main ---
IPS_LIST=$(collect_ips)

if [[ -z "${IPS_LIST// }" ]]; then
  echo "❌ No private IPs collected."
  exit 1
fi

case "$MODE" in
  list)
    echo "📋 Private IPs:"
    echo "$IPS_LIST"
    ;;
  probe)
    echo "🔐 SSH probe (users: ${SSH_USERS[*]}), keys from: $KEY_DIRECTORY"
    echo ""
    while IFS= read -r IP; do
      [[ -z "$IP" ]] && continue
      try_ssh_probe "$IP" || true
    done <<< "$IPS_LIST"
    ;;
  login)
    echo "🔐 Interactive SSH — keys from: $KEY_DIRECTORY"
    echo "   Users tried: ${SSH_USERS[*]}"
    echo ""
    while IFS= read -r IP; do
      [[ -z "$IP" ]] && continue
      read -r -p "Connect to $IP? [Y/n] " ans
      if [[ "${ans:-y}" =~ ^[Nn] ]]; then
        echo "   skipped"
        continue
      fi
      try_ssh_login "$IP" || true
    done <<< "$IPS_LIST"
    ;;
  remote-cytool)
    echo "🔐 Remote cytool on each IP — CYTOOL_RECONNECT_ID=$CYTOOL_RECONNECT_ID"
    echo "   keys: $KEY_DIRECTORY | users: ${SSH_USERS[*]}"
    echo ""
    while IFS= read -r IP; do
      [[ -z "$IP" ]] && continue
      run_remote_cytool "$IP" || true
    done <<< "$IPS_LIST"
    ;;
esac

exit 0
