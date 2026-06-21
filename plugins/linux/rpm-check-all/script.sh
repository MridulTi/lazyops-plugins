#!/usr/bin/env bash

set -u

HOST_FILE="${1:-hosts.txt}"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=5"

if [[ ! -f "$HOST_FILE" ]]; then
  echo "Host file not found: $HOST_FILE"
  exit 1
fi

while read -r HOST; do
  [[ -z "$HOST" ]] && continue
  [[ "$HOST" =~ ^# ]] && continue

  echo "=================================================="
  echo "Host: $HOST"

  ssh $SSH_OPTS "$HOST" '
    echo "IP(s):"
    hostname -I 2>/dev/null || hostname

    echo
    echo "Oracle Java/JDK/JRE RPMs:"

    rpm -qa --qf "%{NAME}|%{VERSION}-%{RELEASE}|%{VENDOR}\n" 2>/dev/null \
      | grep -Ei "(java|jdk|jre)" \
      | grep -Ei "oracle"

    if [[ $? -ne 0 ]]; then
      echo "No Oracle Java RPMs found"
    fi
  ' 2>/dev/null

  if [[ $? -ne 0 ]]; then
    echo "SSH failed: $HOST"
  fi

  echo
done < "$HOST_FILE"
