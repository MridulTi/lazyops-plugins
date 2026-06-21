#!/usr/bin/env bash
# Restart Aerospike when the service is down (e.g. after Linux OOM killer).
# Run as root (or a user allowed to restart the unit). Example cron:
#   */2 * * * * root /path/to/aerospike-oom-recovery.sh
#
# Env overrides:
#   AEROSPIKE_SERVICE   systemd unit name (default: aerospike)
#   AEROSPIKE_RECOVERY_LOG   log file path

set -u

SERVICE="${AEROSPIKE_SERVICE:-aerospike}"
LOG="${AEROSPIKE_RECOVERY_LOG:-/var/log/aerospike-oom-recovery.log}"
LOCK="/tmp/aerospike-recovery.${SERVICE}.lock"

log() {
  # shellcheck disable=SC2329
  printf '[%s] %s\n' "$(date -Iseconds 2>/dev/null || date)" "$*" >>"$LOG" 2>&1
}

if ! command -v systemctl >/dev/null 2>&1; then
  log "ERROR: systemctl not found; install systemd or edit this script for your init."
  exit 1
fi

exec 200>"$LOCK" || exit 1
flock -n 200 || exit 0

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  exit 0
fi

# Optional: correlate with recent OOM in kernel ring buffer (best-effort).
if command -v dmesg >/dev/null 2>&1; then
  if dmesg -T 2>/dev/null | tail -300 | grep -qiE 'out of memory|oom-kill|Killed process'; then
    log "NOTICE: recent OOM-related lines in dmesg (see: dmesg -T | tail -100)"
  fi
fi

log "WARN: $SERVICE is not active — attempting restart"

if systemctl restart "$SERVICE" 2>>"$LOG"; then
  sleep 4
  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    log "OK: $SERVICE is active after restart"
    exit 0
  fi
  log "ERROR: restart ran but $SERVICE is still not active (check: journalctl -u $SERVICE -b)"
  exit 1
fi

log "ERROR: systemctl restart $SERVICE failed"
exit 1
