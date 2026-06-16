#!/usr/bin/env bash
#
# For each IP in a file, SSH with keys from a configured directory (.pem only),
# preferring merchant.pem when present, then other keys. Tries users ec2-user,
# centos, ubuntu. Installs AWS SSM Agent if missing;
# otherwise restarts the agent. A failed host is logged and skipped; processing
# continues with the next IP. Always exits 0; failed IPs are listed at the end.
#
# Note: ssh uses -n so stdin is not read from the hosts file when the IP list is
# fed to `while read ... done < hosts.txt` (otherwise ssh would eat the file).
#
# Usage: ./bootstrap-ssm-agent.sh -f hosts.txt -c ssm_bootstrap.conf
#
# Config: SSH_SHOW_REMOTE_ERR=1 shows remote stderr (useful when bootstrap fails
# after a successful SSH; default hides stderr while trying many keys).
#
set -euo pipefail

usage() {
  echo "Usage: $0 -f <ips_file> -c <config_file>" >&2
  exit 1
}

IPS_FILE=""
CONFIG_FILE=""

while getopts "f:c:h" opt; do
  case "$opt" in
    f) IPS_FILE="$OPTARG" ;;
    c) CONFIG_FILE="$OPTARG" ;;
    h|*) usage ;;
  esac
done

[[ -n "$IPS_FILE" && -n "$CONFIG_FILE" ]] || usage
[[ -f "$IPS_FILE" ]] || { echo "IPs file not found: $IPS_FILE" >&2; exit 1; }
[[ -f "$CONFIG_FILE" ]] || { echo "Config file not found: $CONFIG_FILE" >&2; exit 1; }

# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${SSH_KEY_DIR:?Set SSH_KEY_DIR in $CONFIG_FILE}"
: "${SSH_USERS:=ec2-user centos ubuntu}"
: "${SSH_EXTRA_OPTS:=}"
: "${SSH_SHOW_REMOTE_ERR:=0}"

if [[ ! -d "$SSH_KEY_DIR" ]]; then
  echo "SSH_KEY_DIR is not a directory: $SSH_KEY_DIR" >&2
  exit 1
fi

shopt -s nullglob
KEYS=("$SSH_KEY_DIR"/*.pem)
shopt -u nullglob

if [[ ${#KEYS[@]} -eq 0 ]]; then
  echo "No .pem files found in $SSH_KEY_DIR" >&2
  exit 1
fi

# Prefer merchant.pem (try first) when it exists in the key directory.
PREFERRED_KEY="$SSH_KEY_DIR/merchant.pem"
if [[ -f "$PREFERRED_KEY" ]]; then
  ORDERED_KEYS=("$PREFERRED_KEY")
  for k in "${KEYS[@]}"; do
    [[ "$k" == "$PREFERRED_KEY" ]] && continue
    ORDERED_KEYS+=("$k")
  done
  KEYS=("${ORDERED_KEYS[@]}")
fi

chmod_fix() {
  local k="$1"
  chmod 600 "$k" 2>/dev/null || true
}

# Remote payload: install SSM agent or restart if present.
remote_bootstrap_ssm() {
  bash -s <<'REMOTE'
set -euo pipefail

if systemctl list-unit-files 2>/dev/null | grep -q '^amazon-ssm-agent\.service'; then
  echo "[remote] amazon-ssm-agent unit found; restarting"
  sudo systemctl restart amazon-ssm-agent
  sudo systemctl enable amazon-ssm-agent >/dev/null 2>&1 || true
  sudo systemctl --no-pager --full status amazon-ssm-agent || true
  exit 0
fi

if command -v amazon-ssm-agent >/dev/null 2>&1; then
  echo "[remote] amazon-ssm-agent binary found; restarting"
  sudo systemctl restart amazon-ssm-agent 2>/dev/null || sudo service amazon-ssm-agent restart 2>/dev/null || true
  exit 0
fi

echo "[remote] SSM agent not detected; attempting install"

if [[ -f /etc/os-release ]]; then
  # shellcheck source=/dev/null
  . /etc/os-release
else
  echo "[remote] /etc/os-release missing; cannot detect OS" >&2
  exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)
    SSM_LINUX_DIR="linux_amd64"
    SSM_DEB_DIR="debian_amd64"
    ;;
  aarch64|arm64)
    SSM_LINUX_DIR="linux_arm64"
    SSM_DEB_DIR="debian_arm64"
    ;;
  *)
    echo "[remote] Unsupported machine architecture: $ARCH (supported: x86_64, aarch64, arm64)" >&2
    exit 1
    ;;
esac
echo "[remote] Architecture: $ARCH (using S3 paths ${SSM_LINUX_DIR} / ${SSM_DEB_DIR})"

case "${ID:-}" in
  amzn)
    if command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y amazon-ssm-agent
    else
      sudo yum install -y amazon-ssm-agent
    fi
    ;;
  rhel|centos|rocky|almalinux|fedora)
    REGION="$(curl -sS --max-time 2 http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true)"
    REGION="${REGION:-us-east-1}"
    RPM_URL="https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/${SSM_LINUX_DIR}/amazon-ssm-agent.rpm"
    if command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y "$RPM_URL"
    else
      sudo yum install -y "$RPM_URL"
    fi
    ;;
  ubuntu|debian)
    # Many hosts ship mysql.list for repo.mysql.com; apt update fails with NO_PUBKEY/EXPKEYSIG
    # B7B3B788A8D3785C if the signing key was never imported or rotated. Import current MySQL
    # keys before apt-get update so SSM agent install can proceed.
    echo "[remote] Ensuring MySQL APT signing keys (if needed for apt update)"
    sudo mkdir -p /etc/apt/keyrings /etc/apt/trusted.gpg.d
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "https://repo.mysql.com/RPM-GPG-KEY-mysql-2023" 2>/dev/null | sudo gpg --dearmor -o /etc/apt/keyrings/mysql-2023.gpg 2>/dev/null || true
      curl -fsSL "https://repo.mysql.com/RPM-GPG-KEY-mysql-2022" 2>/dev/null | sudo gpg --dearmor -o /etc/apt/keyrings/mysql-2022.gpg 2>/dev/null || true
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- "https://repo.mysql.com/RPM-GPG-KEY-mysql-2023" 2>/dev/null | sudo gpg --dearmor -o /etc/apt/keyrings/mysql-2023.gpg 2>/dev/null || true
      wget -qO- "https://repo.mysql.com/RPM-GPG-KEY-mysql-2022" 2>/dev/null | sudo gpg --dearmor -o /etc/apt/keyrings/mysql-2022.gpg 2>/dev/null || true
    fi
    if command -v apt-key >/dev/null 2>&1; then
      sudo apt-key adv --keyserver keyserver.ubuntu.com --recv-keys B7B3B788A8D3785C 2>/dev/null || \
        sudo apt-key adv --keyserver hkps://keys.openpgp.org:443 --recv-keys B7B3B788A8D3785C 2>/dev/null || true
    fi
    if ! sudo apt-get update -y; then
      echo "[remote] apt-get update still failing; temporarily disabling repo.mysql.com lists to install SSM only" >&2
      shopt -s nullglob
      MYSQL_LIST_BACK=()
      for mf in /etc/apt/sources.list.d/mysql*.list; do
        [[ -f "$mf" ]] || continue
        sudo mv "$mf" "${mf}.disabled-by-ssm-bootstrap"
        MYSQL_LIST_BACK+=("${mf}.disabled-by-ssm-bootstrap")
      done
      shopt -u nullglob
      sudo apt-get update -y
      sudo apt-get install -y curl
      REGION="$(curl -sS --max-time 2 http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true)"
      REGION="${REGION:-us-east-1}"
      curl -sS "https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/${SSM_DEB_DIR}/amazon-ssm-agent.deb" -o /tmp/amazon-ssm-agent.deb
      sudo dpkg -i /tmp/amazon-ssm-agent.deb || sudo apt-get -f install -y
      for bak in "${MYSQL_LIST_BACK[@]}"; do
        [[ -f "$bak" ]] && sudo mv "$bak" "${bak%.disabled-by-ssm-bootstrap}"
      done
      sudo systemctl daemon-reload 2>/dev/null || true
      sudo systemctl enable amazon-ssm-agent >/dev/null 2>&1 || true
      sudo systemctl restart amazon-ssm-agent
      sudo systemctl --no-pager --full status amazon-ssm-agent || true
      exit 0
    fi
    sudo apt-get install -y curl
    REGION="$(curl -sS --max-time 2 http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true)"
    REGION="${REGION:-us-east-1}"
    curl -sS "https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/${SSM_DEB_DIR}/amazon-ssm-agent.deb" -o /tmp/amazon-ssm-agent.deb
    sudo dpkg -i /tmp/amazon-ssm-agent.deb || sudo apt-get -f install -y
    ;;
  *)
    echo "[remote] Unsupported ID=${ID:-unknown}" >&2
    exit 1
    ;;
esac

sudo systemctl daemon-reload 2>/dev/null || true
sudo systemctl enable amazon-ssm-agent >/dev/null 2>&1 || true
sudo systemctl restart amazon-ssm-agent
sudo systemctl --no-pager --full status amazon-ssm-agent || true
REMOTE
}

try_host() {
  local ip="$1"
  local user key rc
  for user in $SSH_USERS; do
    for key in "${KEYS[@]}"; do
      chmod_fix "$key"
      echo "==> $ip: trying user=$user key=$(basename "$key")"
      # OpenSSH: exit 255 = SSH-level error (e.g. auth). Any other code is usually
      # the remote command's exit status — auth worked; do not burn through other keys.
      # Do not run `set -e` here: it would leak into the caller and exit the IP loop
      # on the next host when try_host returns non-zero (set -e exits before host_rc=$?).
      set +e
      # shellcheck disable=SC2086
      if [[ "$SSH_SHOW_REMOTE_ERR" == "1" ]]; then
        ssh -n $SSH_EXTRA_OPTS -i "$key" -o BatchMode=yes -o IdentitiesOnly=yes \
          "${user}@${ip}" "bash -lc $(printf %q "$(declare -f remote_bootstrap_ssm); remote_bootstrap_ssm")"
      else
        ssh -n $SSH_EXTRA_OPTS -i "$key" -o BatchMode=yes -o IdentitiesOnly=yes \
          "${user}@${ip}" "bash -lc $(printf %q "$(declare -f remote_bootstrap_ssm); remote_bootstrap_ssm")" 2>/dev/null
      fi
      rc=$?
      case "$rc" in
        0)
          echo "==> $ip: success as $user with $(basename "$key")"
          return 0
          ;;
        255)
          continue
          ;;
        *)
          echo "==> $ip: SSH OK as $user with $(basename "$key") but bootstrap failed (exit $rc). Fix the host or run ssh without hiding stderr to see errors. Not trying other keys for this user." >&2
          return 1
          ;;
      esac
    done
  done
  echo "==> $ip: FAILED (no working user/key combo)" >&2
  return 1
}

FAILED_IPS=()
# Keep errexit off for the whole IP loop: try_host used to run `set -e` after ssh,
# which leaked global errexit on and made the next `try_host "$ip"` exit the script
# before `host_rc=$?` when a host failed.
set +e
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
  ip="${line//[[:space:]]/}"
  try_host "$ip"
  host_rc=$?
  if [[ "$host_rc" -ne 0 ]]; then
    FAILED_IPS+=("$ip")
    echo "==> $ip: skipping to next IP" >&2
  fi
done < "$IPS_FILE"
set -e

if [[ ${#FAILED_IPS[@]} -gt 0 ]]; then
  echo "Completed with ${#FAILED_IPS[@]} failure(s). Failed IPs:" >&2
  printf '  %s\n' "${FAILED_IPS[@]}" >&2
else
  echo "All hosts processed successfully."
fi
exit 0
