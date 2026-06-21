#!/usr/bin/env bash
set -euo pipefail

# -------- INPUTS --------
REGION="$1"
CIDR="$2"
PORTS_CSV="$3"
DIRECTION="${4:-inbound}"
PROTOCOL="${5:-tcp}"
CONCURRENCY="${6:-20}"

if [[ -z "${REGION:-}" || -z "${CIDR:-}" || -z "${PORTS_CSV:-}" ]]; then
  echo "Usage: $0 <region> <cidr> <ports_csv> [inbound|outbound] [protocol] [concurrency]"
  echo "Example (inbound):  $0 us-east-1 10.0.0.0/16 22,80,443 inbound tcp 16"
  echo "Example (outbound): $0 us-east-1 10.0.0.0/16 443 outbound tcp 16"
  exit 1
fi

if [[ "$DIRECTION" != "inbound" && "$DIRECTION" != "outbound" ]]; then
  echo "Error: direction must be 'inbound' or 'outbound' (got '$DIRECTION')." >&2
  exit 1
fi

if [[ -z "${PROTOCOL:-}" ]]; then
  echo "Error: protocol must be non-empty (example: tcp, udp)." >&2
  exit 1
fi

if ! [[ "$CONCURRENCY" =~ ^[0-9]+$ ]] || (( CONCURRENCY < 1 )); then
  echo "Error: concurrency must be a positive integer (got '$CONCURRENCY')." >&2
  exit 1
fi

IFS=',' read -ra PORTS <<< "$PORTS_CSV"

echo "🔍 Region   : $REGION"
echo "🔐 CIDR     : $CIDR"
echo "🚪 Ports    : ${PORTS[*]}"
echo "📡 Protocol : $PROTOCOL"
echo "↔️ Direction: $DIRECTION"
echo "⚙️ Parallel  : $CONCURRENCY"
echo

FAIL_FILE="$(mktemp -t sg_rule_failures.XXXXXX)"
cleanup() { rm -f "$FAIL_FILE"; }
trap cleanup EXIT

CIDR_FILTER_NAME="cidr-ipv4"
if [[ "$CIDR" == *:* ]]; then
  CIDR_FILTER_NAME="cidr-ipv6"
fi

process_sg() {
  local SG="$1"
  local PORT
  local -a LOCAL_PORTS=()
  IFS=',' read -ra LOCAL_PORTS <<< "$PORTS_CSV"

  echo "➡️ Security Group: $SG"

  for PORT in "${LOCAL_PORTS[@]}"; do
    echo "   🔎 Checking port $PORT"

    # ---- CHECK IF EXACT RULE EXISTS ----
    # Filters match exact: group-id + direction(is-egress) + cidr + from/to port + protocol
    local IS_EGRESS
    if [[ "$DIRECTION" == "outbound" ]]; then IS_EGRESS="true"; else IS_EGRESS="false"; fi

    local EXISTS
    EXISTS=$(aws ec2 describe-security-group-rules \
      --region "$REGION" \
      --filters \
        Name=group-id,Values="$SG" \
        Name=is-egress,Values="$IS_EGRESS" \
        Name="$CIDR_FILTER_NAME",Values="$CIDR" \
        Name=from-port,Values="$PORT" \
        Name=to-port,Values="$PORT" \
        Name=ip-protocol,Values="$PROTOCOL" \
      --query 'SecurityGroupRules[].SecurityGroupRuleId' \
      --output text 2>/dev/null || true)

    if [[ -n "$EXISTS" ]]; then
      echo "      ✅ Already exists"
      continue
    fi

    echo "      ➕ Adding rule..."
    local AUTH_CMD
    if [[ "$DIRECTION" == "inbound" ]]; then
      AUTH_CMD=(aws ec2 authorize-security-group-ingress)
    else
      AUTH_CMD=(aws ec2 authorize-security-group-egress)
    fi

    if "${AUTH_CMD[@]}" \
      --region "$REGION" \
      --group-id "$SG" \
      --protocol "$PROTOCOL" \
      --port "$PORT" \
      --cidr "$CIDR"; then
      echo "      ✅ Added"
    else
      echo "      ❌ Failed"
      echo "$SG (port $PORT)" >> "$FAIL_FILE"
    fi
  done

  echo
}

# -------- FETCH SECURITY GROUPS --------
SG_IDS=$(aws ec2 describe-security-groups \
  --region "$REGION" \
  --query 'SecurityGroups[?GroupName!=`default`].GroupId' \
  --output text)

export REGION CIDR PORTS_CSV DIRECTION PROTOCOL CIDR_FILTER_NAME FAIL_FILE
export -f process_sg

# Run up to CONCURRENCY SGs in parallel
printf '%s\n' $SG_IDS | xargs -P "$CONCURRENCY" -I {} bash -c 'process_sg "$@"' _ {}

# -------- SUMMARY --------
if [[ -s "$FAIL_FILE" ]]; then
  echo "❌ Failed Security Groups:"
  sed 's/^/ - /' "$FAIL_FILE"
  exit 1
else
  echo "🎉 All rules verified/added successfully"
fi

