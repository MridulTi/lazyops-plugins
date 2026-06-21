#!/usr/bin/env python3
"""
Audit Jenkins Bitbucket SSH keys/config on remote servers.

Uses the same login/discovery logic as repair_qualys_from_s3.py:
- Same config file (ROLE_ARN, IPS_MODE=tag, TAG_FILTERS, SSH_KEY_DIR, AWS_REGION, etc.)
- Assume role per account, list IPs, prompt, then SSH to each host
- On each host: find which user runs Jenkins (ps -ef), then list that user's
  .ssh directory and any Bitbucket-related keys or config

Usage:
    python3 jenkins_bitbucket_ssh_audit.py <config_file>

Config: Same as repair_qualys (e.g. qualys.conf). At minimum: AWS_REGION, SSH_KEY_DIR, IPS_MODE=tag.
Optional: TAG_FILTERS, INSTANCE_FILTERS, EXCLUDE_TAGS, SKIP_ASG_INSTANCES, ROLE_ARN.
  TAG_MUST_CONTAIN=jenkins — keep only instances where any tag (key or value) contains "jenkins" (case-insensitive).
  If using only this, set INSTANCE_FILTERS e.g. Name=instance-state-name,Values=running (TAG_FILTERS can be empty).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Required only for IP discovery and SSH
REQUIRED_VARS = ("AWS_REGION", "SSH_KEY_DIR")
SSH_USERS = ("ec2-user", "ubuntu", "centos")

# Remote script: find Jenkins process user(s), then list .ssh and Bitbucket-related items
DISCOVER_SCRIPT = r"""
set -e
echo "=== JENKINS PROCESS USER(S) ==="
# Find processes that look like Jenkins master or agent (java -jar jenkins.war, agent.jar, or executable named jenkins)
JENKINS_USERS=$(ps -ef 2>/dev/null | grep -v grep | grep -iE 'jenkins|java.*jenkins\.war|java.*agent\.jar' | awk '{print $1}' | sort -u || true)
if [ -z "$JENKINS_USERS" ]; then
  echo "(no Jenkins process found)"
  echo "=== END ==="
  exit 0
fi
echo "$JENKINS_USERS"
echo ""
for JUSER in $JENKINS_USERS; do
  echo "=== USER: $JUSER ==="
  HOME_DIR=$(getent passwd "$JUSER" 2>/dev/null | cut -d: -f6)
  if [ -z "$HOME_DIR" ]; then
    echo "  (no passwd entry / home dir)"
    continue
  fi
  SSH_DIR="$HOME_DIR/.ssh"
  if [ ! -d "$SSH_DIR" ]; then
    echo "  .ssh: (not a directory or missing)"
    continue
  fi
  echo "  Home: $HOME_DIR"
  echo "  .ssh listing (sudo):"
  sudo ls -la "$SSH_DIR" 2>/dev/null || ls -la "$SSH_DIR" 2>/dev/null || echo "  (cannot list)"
  echo "  --- Keys / files in .ssh ---"
  for f in "$SSH_DIR"/*; do
    [ -e "$f" ] || continue
    base=$(basename "$f")
    echo "    $base"
  done
  echo "  --- Content of files that have 'git' or 'bitbucket' in filename or content ---"
  # Use sudo ls to get file list (SSH user often cannot read jenkins .ssh dir)
  for base in $(sudo ls -1 "$SSH_DIR" 2>/dev/null || ls -1 "$SSH_DIR" 2>/dev/null); do
    [ -n "$base" ] || continue
    f="$SSH_DIR/$base"
    sudo test -f "$f" 2>/dev/null || test -f "$f" 2>/dev/null || continue
    case "$base" in known_hosts.old) continue ;; esac
    content=$(sudo cat "$f" 2>/dev/null || cat "$f" 2>/dev/null)
    echo "$base" | grep -qiE 'git|bitbucket' || echo "$content" | grep -qiE 'git|bitbucket' || continue
    echo "  --- $base ---"
    if echo "$content" | head -1 | grep -q '-----BEGIN'; then
      echo "  (private key; showing derived public key only)"
      sudo ssh-keygen -y -f "$f" 2>/dev/null || ssh-keygen -y -f "$f" 2>/dev/null || echo "  (could not derive public key)"
    else
      echo "$content"
    fi
  done
  if [ -f "$SSH_DIR/config" ]; then
    echo "  --- config (full; look for Host bitbucket / IdentityFile) ---"
    sudo cat "$SSH_DIR/config" 2>/dev/null || cat "$SSH_DIR/config" 2>/dev/null || echo "  (cannot read)"
  fi
  echo ""
done
echo "=== END ==="
"""


def load_config(path: str) -> dict:
    """Parse bash-style config file (same as repair_qualys_from_s3.py)."""
    config = {}
    with open(path) as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip().rstrip()
        if "#" in rest and not rest.startswith("("):
            rest = rest.split("#", 1)[0].strip().rstrip()
        if rest.startswith("("):
            arr_parts = [rest]
            depth = rest.count("(") - rest.count(")")
            i += 1
            while i < len(lines) and depth > 0:
                arr_parts.append(lines[i])
                depth += lines[i].count("(") - lines[i].count(")")
                i += 1
            arr_str = " ".join(arr_parts)
            vals = re.findall(r'["\']([^"\']*)["\']', arr_str)
            config[key] = vals
            continue
        if rest.startswith('"') and rest.endswith('"'):
            config[key] = rest[1:-1].replace('\\"', '"')
        elif rest.startswith("'") and rest.endswith("'"):
            config[key] = rest[1:-1]
        else:
            config[key] = rest
        i += 1
    return config


def validate_config(config: dict, multi_account: bool = False) -> None:
    for v in REQUIRED_VARS:
        if not config.get(v):
            print(f"❌ Missing in config: {v}")
            sys.exit(1)
    if multi_account:
        if not config.get("ACCOUNT_ROLES") and not config.get("ROLE_ARN"):
            print("❌ ACCOUNT_ROLES or ROLE_ARN required for multi-account mode")
            sys.exit(1)
        if config.get("IPS_MODE") != "tag":
            print("❌ IPS_MODE=tag required when using ROLE_ARN")
            sys.exit(1)
    if config.get("IPS_MODE") == "tag":
        if not _ensure_list(config.get("TAG_FILTERS")) and not (config.get("TAG_MUST_CONTAIN") or "").strip():
            print("❌ TAG_FILTERS or TAG_MUST_CONTAIN required when IPS_MODE=tag")
            sys.exit(1)


def account_id_from_role_arn(role_arn: str) -> str:
    parts = role_arn.split(":")
    return parts[4] if len(parts) >= 5 else role_arn


def assume_role(role_arn: str, session_name: str | None = None) -> dict:
    import time
    name = session_name or f"jenkins_audit_{int(time.time())}"
    result = subprocess.run(
        ["aws", "sts", "assume-role", "--role-arn", role_arn, "--role-session-name", name, "--output", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"❌ assume-role failed: {result.stderr}")
        sys.exit(1)
    return json.loads(result.stdout)["Credentials"]


def apply_credentials(creds: dict) -> None:
    os.environ["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
    os.environ["AWS_SESSION_TOKEN"] = creds["SessionToken"]


def clear_assumed_role_credentials() -> None:
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        os.environ.pop(key, None)


def get_ssh_keys(ssh_key_dir: str) -> list[Path]:
    keys = sorted(Path(ssh_key_dir).glob("*.pem"))
    if not keys:
        print(f"❌ No .pem keys found in {ssh_key_dir}")
        sys.exit(1)
    return keys


def _ensure_list(val: str | list | None) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    return [s] if s else []


def get_ips(config: dict, allow_empty: bool = False) -> list[str]:
    mode = config.get("IPS_MODE") or "tag"
    region = config["AWS_REGION"]
    if mode == "static":
        raw = config.get("IPS") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,]+", raw)
        return [x.strip() for x in raw if x.strip()]
    if mode == "tag":
        tag_filters = _ensure_list(config.get("TAG_FILTERS"))
        instance_filters = _ensure_list(config.get("INSTANCE_FILTERS"))
        exclude_tags = _ensure_list(config.get("EXCLUDE_TAGS"))
        tag_must_contain = (config.get("TAG_MUST_CONTAIN") or "").strip()
        if not tag_filters and not tag_must_contain:
            print("❌ TAG_FILTERS or TAG_MUST_CONTAIN required for tag mode")
            sys.exit(1)
        if not tag_filters and not instance_filters:
            print("❌ At least one of TAG_FILTERS or INSTANCE_FILTERS required for EC2 API")
            sys.exit(1)
        cmd = [
            "aws", "ec2", "describe-instances",
            "--region", region,
            "--query", "Reservations[].Instances[]",
            "--output", "json",
        ]
        all_filters = tag_filters + instance_filters
        if all_filters:
            cmd.append("--filters")
            cmd.extend(all_filters)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("❌ aws describe-instances failed:", result.stderr)
            sys.exit(1)
        instances = json.loads(result.stdout)
        if not instances:
            if allow_empty:
                return []
            print("❌ No instances found")
            sys.exit(1)
        skip_asg = str(config.get("SKIP_ASG_INSTANCES") or "").strip().lower() in ("true", "yes", "1")
        exclude_set = set()
        for ex in exclude_tags:
            ex = (ex or "").strip()
            if not ex:
                continue
            if "Values=" in ex.replace("Value=", "Values="):
                parts = ex.replace("Value=", "Values=")
                k, v = parts.split("Values=", 1)
                k = k.replace("Key=", "").replace("Name=tag:", "").strip().rstrip(",")
                exclude_set.add((k.strip(), v.strip()))
            elif "=" in ex:
                k, v = ex.split("=", 1)
                exclude_set.add((k.strip(), v.strip()))
        if tag_must_contain:
            tag_sub = tag_must_contain.lower()
        ips = []
        for inst in instances:
            if not inst.get("PrivateIpAddress"):
                continue
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
            if tag_must_contain:
                if not any(tag_sub in (k or "").lower() or tag_sub in (v or "").lower() for k, v in tags.items()):
                    continue
            if skip_asg and tags.get("aws:autoscaling:groupName"):
                continue
            if any(tags.get(k) == v for k, v in exclude_set):
                continue
            ips.append(inst["PrivateIpAddress"])
        if not ips and not allow_empty:
            print("❌ No IPs left after exclusions")
            sys.exit(1)
        return ips
    print("❌ Invalid IPS_MODE")
    sys.exit(1)


def ssh_run(ip: str, user: str, key_path: Path, script: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = [
        "ssh", "-i", str(key_path),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=7", "-o", "StrictHostKeyChecking=no", "-o", "IdentitiesOnly=yes",
        f"{user}@{ip}", "bash", "-l", "-s",
    ]
    result = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def probe_connection(config: dict, ip: str) -> tuple[bool, str | None, Path | None]:
    """Find a (user, key) that can SSH to ip. Returns (True, user, key_path) or (False, None, None)."""
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for u in SSH_USERS:
        for k in keys:
            rc, out, _ = ssh_run(ip, u, k, "echo JENKINS_AUDIT_PROBE_OK", timeout=15)
            if rc == 0 and (out or "").strip() == "JENKINS_AUDIT_PROBE_OK":
                return True, u, k
    return False, None, None


def run_discovery(config: dict, ip: str, user: str, key_path: Path) -> tuple[bool, str]:
    """Run Jenkins/Bitbucket SSH discovery on host. Returns (success, output)."""
    rc, out, err = ssh_run(ip, user, key_path, DISCOVER_SCRIPT, timeout=45)
    text = (out or "") + ("\n" + err if err else "")
    return rc == 0, text


def run_audit_for_ips(config: dict, ips: list[str]) -> None:
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for ip in ips:
        print(f"\n🔍 {ip}")
        connected, ssh_user, key_path = probe_connection(config, ip)
        if not connected:
            print("   ❌ Could not connect — skipping")
            print("--------------------------------------------")
            continue
        ok, output = run_discovery(config, ip, ssh_user, key_path)
        if ok:
            print("   --- Jenkins user(s) and .ssh / Bitbucket keys or config ---")
            for line in (output or "").splitlines():
                print(f"   {line}")
        else:
            print("   ⚠️ Discovery failed or partial output:")
            for line in (output or "").splitlines():
                print(f"   {line}")
        print("--------------------------------------------")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: jenkins_bitbucket_ssh_audit.py <config_file>")
        sys.exit(1)
    config_path = sys.argv[1]
    if not Path(config_path).is_file():
        print(f"❌ Config file '{config_path}' not found!")
        sys.exit(1)

    config = load_config(config_path)
    account_roles = config.get("ACCOUNT_ROLES") or config.get("ROLE_ARN") or []
    if isinstance(account_roles, str):
        account_roles = [account_roles]
    multi_account = len(account_roles) > 0
    if not config.get("IPS_MODE") and _ensure_list(config.get("TAG_FILTERS")):
        config["IPS_MODE"] = "tag"
    validate_config(config, multi_account=multi_account)

    if multi_account:
        for role_arn in account_roles:
            role_arn = (role_arn or "").strip()
            if not role_arn:
                continue
            account_id = account_id_from_role_arn(role_arn)
            print("\n" + "=" * 60)
            print(f"📦 Account {account_id}")
            print("=" * 60)
            apply_credentials(assume_role(role_arn))
            ips = get_ips(config, allow_empty=True)
            if not ips:
                print("   No instances in this account — skipping.")
                continue
            print(f"\n🧾 IPs in account {account_id} ({len(ips)} host(s)):")
            for i, ip in enumerate(ips, 1):
                print(f"  {i}. {ip}")
            print()
            confirm = input(f"Run Jenkins/Bitbucket SSH audit on account {account_id} ({len(ips)} IPs)? (yes/no): ").strip().lower()
            if confirm != "yes":
                print("   Skipped by user.")
                continue
            clear_assumed_role_credentials()
            run_audit_for_ips(config, ips)
    else:
        ips = get_ips(config)
        print("\n🧾 Target instances:")
        for i, ip in enumerate(ips, 1):
            print(f"  {i}. {ip}")
        print()
        confirm = input("Proceed with Jenkins/Bitbucket SSH audit on these hosts? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("❌ Aborted by user")
            sys.exit(0)
        run_audit_for_ips(config, ips)
    print("\n🏁 Audit finished")


if __name__ == "__main__":
    main()
