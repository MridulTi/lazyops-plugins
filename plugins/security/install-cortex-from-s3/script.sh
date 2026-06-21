#!/usr/bin/env python3
"""
Install or update Cortex (Traps PMD) on servers from S3.

Supports multiple AWS accounts: assume role per account only to list IPs (EC2);
install is done by SSH to each host. Role credentials are cleared before SSH;
S3 access during install uses each server's own IAM instance profile.

Config file (bash-style, same as repair_qualys_from_s3.py):
  - ROLE_ARN=( "arn:aws:iam::ACCOUNT_ID:role/RoleName" ... ) or ACCOUNT_ROLES=(...)  # optional, for multi-account
  - IPS_MODE=tag (required if using roles): TAG_FILTERS (multiple), optional INSTANCE_FILTERS, EXCLUDE_TAGS, SKIP_ASG_INSTANCES
  - IPS_MODE=static: IPS="ip1 ip2 ..." or IPS_FILE="path/to/ips.txt" (one IP per line or space/comma separated)
  - S3_BUCKET_PATH, AWS_REGION, SSH_KEY_DIR
  - Optional: AWS_CLI_FALLBACK_PATH for S3 on the server

Usage:
    python3 install_cortex_from_s3.py <config_file>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


REQUIRED_VARS = ("S3_BUCKET_PATH", "AWS_REGION", "SSH_KEY_DIR")
SSH_USERS = ("ec2-user", "ubuntu", "centos")
SERVICE = "traps_pmd"

# Cortex package names (from install_cortex_from_s3.sh)
TAR_RPM_X64 = "Linux_8_7_100_136016_CE_rpm.tar.gz"
TAR_RPM_ARM = "Linux_8_7_100_136016_CE_rpm.aarch64.tar.gz"
TAR_DEB_X64 = "Linux_8_7_100_136016_CE_deb.tar.gz"
TAR_DEB_ARM = "Linux_8_7_100_136016_CE_deb.aarch64.tar.gz"


def load_config(path: str) -> dict:
    """Parse bash-style config file (KEY=value and KEY=( "v1" "v2" ) including multiline)."""
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
            print("❌ ACCOUNT_ROLES or ROLE_ARN is required for multi-account mode")
            sys.exit(1)
        if config.get("IPS_MODE") != "tag":
            print("❌ IPS_MODE=tag is required when using ROLE_ARN (to list IPs per account)")
            sys.exit(1)
    # TAG_FILTERS optional when IPS_MODE=tag; if empty, all instances (in region) are listed


def account_id_from_role_arn(role_arn: str) -> str:
    parts = role_arn.split(":")
    return parts[4] if len(parts) >= 5 else role_arn


def assume_role(role_arn: str, session_name: str | None = None) -> dict:
    import time
    name = session_name or f"install_cortex_{int(time.time())}"
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
        ips_file = (config.get("IPS_FILE") or "").strip()
        if ips_file and Path(ips_file).is_file():
            with open(ips_file) as f:
                raw.extend(re.split(r"[\s,]+", f.read()))
        return [x.strip() for x in raw if x.strip()]

    if mode == "tag":
        tag_filters = _ensure_list(config.get("TAG_FILTERS"))
        instance_filters = _ensure_list(config.get("INSTANCE_FILTERS"))
        exclude_tags = _ensure_list(config.get("EXCLUDE_TAGS"))

        if tag_filters or instance_filters:
            print("🔎 Fetching IPs using filters:")
            for f in tag_filters:
                print(f"   - [tag] {f}")
            for f in instance_filters:
                print(f"   - [instance] {f}")
        else:
            print("🔎 Fetching IPs: no TAG_FILTERS (listing all running instances in region)")

        cmd = [
            "aws", "ec2", "describe-instances",
            "--region", region,
            "--query", "Reservations[].Instances[]",
            "--output", "json",
        ]
        all_filters = tag_filters + instance_filters
        if not all_filters:
            all_filters = ["Name=instance-state-name,Values=running"]
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
        if skip_asg:
            print("🚫 Skipping instances in an Auto Scaling Group")

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

        ips = []
        for inst in instances:
            if not inst.get("PrivateIpAddress"):
                continue
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
            if skip_asg and tags.get("aws:autoscaling:groupName"):
                continue
            skip = any(tags.get(k) == v for k, v in exclude_set)
            if not skip:
                ips.append(inst["PrivateIpAddress"])

        if not ips and not allow_empty:
            print("❌ No IPs left after exclusions")
            sys.exit(1)
        return ips

    print("❌ Invalid IPS_MODE")
    sys.exit(1)


def ssh_run(ip: str, user: str, key_path: Path, script: str, timeout: int = 120, use_tt: bool = False, login_shell: bool = False) -> tuple[int, str, str]:
    """Run script over SSH. use_tt=True allocates a TTY. login_shell=True uses bash -l so PATH matches Qualys (same as interactive SSH)."""
    cmd = [
        "ssh",
        "-i", str(key_path),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=7",
        "-o", "StrictHostKeyChecking=no",
        "-o", "IdentitiesOnly=yes",
        f"{user}@{ip}",
        "bash",
        *(["-l", "-s"] if login_shell else ["-s"]),
    ]
    if use_tt:
        cmd.insert(1, "-tt")
    result = subprocess.run(
        cmd,
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


# One-line probe to find which (user, key) can connect — same idea as Qualys check_qualys (short script first).
PROBE_SCRIPT = "echo CORTEX_PROBE_OK"


def check_cortex_connection(config: dict, ip: str) -> tuple[bool, str | None, str | None]:
    """Try each (user, key); return (True, user, key_name) on first successful SSH, else (False, None, None)."""
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for user in SSH_USERS:
        for key in keys:
            # Probe without TTY (-tt) so we don't hang on MOTD/banner waiting for input; 20s timeout
            rc, out, _ = ssh_run(ip, user, key, PROBE_SCRIPT, timeout=20, use_tt=False)
            if rc == 0 and (out or "").strip() == "CORTEX_PROBE_OK":
                return True, user, key.name
    return False, None, None


def _shell_quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def install_cortex(config: dict, ip: str, user: str, key_path: Path) -> tuple[bool, str, str]:
    """Run Cortex install/update on host. Returns (success, stdout, stderr)."""
    s3_path = _shell_quote(config["S3_BUCKET_PATH"])
    aws_fallback = _shell_quote(config.get("AWS_CLI_FALLBACK_PATH") or "")

    script = f"""
echo "CORTEX_INSTALL_START" >&2
set -e
S3_BUCKET_PATH="{s3_path}"
AWS_CLI_FALLBACK="{aws_fallback}"
SERVICE="{SERVICE}"

# Require passwordless sudo up front
sudo -n true 2>/dev/null || {{ echo "CORTEX_ERROR: sudo requires password; add NOPASSWD for this user" >&2; exit 1; }}

# Try PATH aws first, then fallback, then /usr/bin/aws, then sudo.
download_from_s3() {{
  local src="$1" dest="$2" err
  if aws s3 cp "$src" "$dest" 2>/dev/null; then return 0; fi
  if [[ -n "$AWS_CLI_FALLBACK" && -x "$AWS_CLI_FALLBACK" ]] && "$AWS_CLI_FALLBACK" s3 cp "$src" "$dest" 2>/dev/null; then return 0; fi
  if [[ -x /usr/bin/aws ]] && /usr/bin/aws s3 cp "$src" "$dest" 2>/dev/null; then return 0; fi
  if sudo -n /usr/bin/aws s3 cp "$src" "$dest" 2>/dev/null; then return 0; fi
  if sudo -n aws s3 cp "$src" "$dest" 2>/dev/null; then return 0; fi
  err=`aws s3 cp "$src" "$dest" 2>&1` || err=`sudo -n aws s3 cp "$src" "$dest" 2>&1` || true
  echo "CORTEX_ERROR: S3 download failed: $err" >&2
  return 1
}}

if systemctl is-active --quiet $SERVICE 2>/dev/null; then
  STATUS="RUNNING"
elif command -v traps_pmd >/dev/null 2>&1; then
  STATUS="INSTALLED_STOPPED"
else
  STATUS="NOT_INSTALLED"
fi

ARCH=`uname -m`
OS=`source /etc/os-release 2>/dev/null && echo "$ID" || echo "unknown"`
TMP=/tmp/cortex_install
mkdir -p "$TMP"
cd "$TMP"

# OS detection
case "$OS" in
amzn|rhel|centos|rocky|almalinux)
  [[ "$ARCH" == "x86_64" ]] && TAR="{TAR_RPM_X64}" || TAR="{TAR_RPM_ARM}"
  INSTALL_CMD="sudo rpm -Uvh --replacepkgs"
  ;;
ubuntu|debian)
  [[ "$ARCH" == "aarch64" ]] && TAR="{TAR_DEB_ARM}" || TAR="{TAR_DEB_X64}"
  INSTALL_CMD="sudo dpkg -i"
  ;;
*)
  echo "CORTEX_ERROR: Unsupported OS: $OS" >&2
  exit 1
  ;;
esac

download_from_s3 "$S3_BUCKET_PATH/$TAR" "./$TAR" || exit 1
sudo tar -xzf "$TAR"
CONF=`ls *.conf 2>/dev/null | head -1`
[[ -z "$CONF" ]] && {{ echo "CORTEX_ERROR: No .conf in tarball" >&2; exit 1; }}
sudo mkdir -p /etc/cortex
sudo cp "$CONF" /etc/cortex/cortex.conf

PKG=`find . -maxdepth 1 -name '*.rpm' 2>/dev/null | head -1`
[[ -z "$PKG" ]] && PKG=`find . -maxdepth 1 -name '*.deb' 2>/dev/null | head -1`
[[ -z "$PKG" ]] && {{ echo "CORTEX_ERROR: No .rpm/.deb in tarball" >&2; exit 1; }}

# Stop traps_pmd and remove existing Cortex package so install/upgrade succeeds
sudo systemctl stop $SERVICE 2>/dev/null || true
case "$OS" in
amzn|rhel|centos|rocky|almalinux)
  RPM_QF='%{{NAME}}'
  PKGNAME=`rpm -qp --qf "\$RPM_QF" "\$PKG" 2>/dev/null` || true
  [[ -n "\$PKGNAME" ]] && sudo rpm -e "\$PKGNAME" --nodeps 2>/dev/null || true
  ;;
ubuntu|debian)
  PKGNAME=`dpkg -f "\$PKG" Package 2>/dev/null` || true
  [[ -n "\$PKGNAME" ]] && sudo dpkg -r "\$PKGNAME" 2>/dev/null || true
  ;;
esac

# Install prerequisites required by Cortex (policycoreutils-python-utils on AL2023; policycoreutils-python on AL2)
case "$OS" in
amzn|rhel|centos|rocky|almalinux)
  if ! sudo dnf install -y openssl ca-certificates policycoreutils-python-utils selinux-policy-devel 2>/dev/null; then
    if ! sudo yum install -y openssl ca-certificates policycoreutils-python-utils selinux-policy-devel 2>/dev/null; then
      echo "CORTEX_ERROR: Failed to install prerequisites: selinux-policy-devel, policycoreutils-python-utils" >&2
      exit 1
    fi
  fi
  sudo dnf install -y policycoreutils-python 2>/dev/null || sudo yum install -y policycoreutils-python 2>/dev/null || true
  ;;
esac
sudo timeout 300 $INSTALL_CMD "$PKG" || {{ echo "CORTEX_ERROR: Package install failed: $INSTALL_CMD" >&2; exit 1; }}

sudo mkdir -p /etc/systemd/system/traps_pmd.service.d
sudo tee /etc/systemd/system/traps_pmd.service.d/override.conf >/dev/null <<'OVERRIDE'
[Service]
ExecStart=
ExecStart=/opt/traps/bin/pmd --config /etc/cortex/cortex.conf
OVERRIDE

sudo systemctl daemon-reload
sudo systemctl enable traps_pmd
sudo systemctl restart traps_pmd
systemctl is-active traps_pmd || {{ echo "CORTEX_ERROR: traps_pmd failed to start" >&2; exit 1; }}
"""
    # Login shell (same as Qualys) so PATH matches interactive SSH and aws resolves like repair_qualys_from_s3.py; no TTY to avoid MOTD hang.
    rc, out, err = ssh_run(ip, user, key_path, script, timeout=360, use_tt=False, login_shell=True)
    return rc == 0, out, err


def run_install_for_ips(config: dict, ips: list[str]) -> list[str]:
    """Run Cortex install on each IP. Same pattern as Qualys: probe first to find working (user, key), then run install with that."""
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    failed = []

    for ip in ips:
        print(f"🔍 Processing {ip}")

        # Step 1: Find a (user, key) that can connect — short probe script, like Qualys check_qualys
        connected, user, key_name = check_cortex_connection(config, ip)
        if not connected:
            print(f"❌ Could not connect to {ip} — skipping")
            print("--------------------------------------------")
            failed.append(ip)
            continue

        key_path = next((k for k in keys if k.name == key_name), keys[0])
        print(f"   Using {user} with {key_name} for install")

        # Step 2: Run full install with the working (user, key) first
        ok, out, err = install_cortex(config, ip, user, key_path)
        if ok:
            print(f"🟢 Cortex install/update success on {ip} as {user}")
            print("--------------------------------------------")
            continue

        # Step 3: If install failed, try other (user, key) combos (like Qualys retry)
        first_fail_out, first_fail_err = out, err
        done = False
        for u in SSH_USERS:
            for k in keys:
                if (u, k) == (user, key_path):
                    continue
                print(f"   Trying {u} with {k.name}...")
                ok, out, err = install_cortex(config, ip, u, k)
                if ok:
                    print(f"🟢 Cortex install/update success on {ip} as {u}")
                    done = True
                    break
            if done:
                break

        if not done:
            print(f"⚠️ Failed on {ip}")
            # Ensure strings (avoid AttributeError if install_cortex ever returns non-str)
            err_str = str(first_fail_err) if first_fail_err is not None else ""
            out_str = str(first_fail_out) if first_fail_out is not None else ""
            combined = f"{err_str}\n{out_str}"
            # Extract and show REAL CAUSE first (CORTEX_ERROR or rpm/dnf error lines)
            real_cause_lines = []
            error_substrings = (
                "CORTEX_ERROR:", "Prerequisites not met", "not met.", "Please install missing",
                "scriptlet failed", "exit status 1", "Permission denied", "MISSING",
                "error:", "install failed", "failed to start",
            )
            for raw_line in combined.splitlines():
                s = (raw_line or "").strip()
                if not s or "Completed" in s or s.startswith("download:"):
                    continue
                if any(x in s for x in error_substrings):
                    real_cause_lines.append(s)
            if real_cause_lines:
                print("   >>> REAL CAUSE:")
                for ln in real_cause_lines[-8:]:
                    print(f"      {ln}")
                print("   ---")
            print("   First attempt — full output:")
            skip_banner = ("authorized uses only", "last login:", "connection closed")
            for label, text in [("stderr", err_str), ("stdout", out_str)]:
                if not text:
                    continue
                try:
                    lines = [ln for ln in text.splitlines() if (ln or "").strip()]
                except Exception:
                    lines = [repr(text)[:200]]
                filtered = [ln for ln in lines if not any(skip in (ln or "").lower() for skip in skip_banner)]
                if label == "stdout":
                    filtered = [ln for ln in filtered if "Completed " not in ln or "MiB/s" not in ln]
                if not filtered:
                    continue
                if label == "stderr":
                    print(f"   [{label}]")
                    for ln in filtered:
                        print(f"      {ln}")
                else:
                    show = filtered[-50:] if len(filtered) > 50 else filtered
                    if len(filtered) > 50:
                        print(f"   [{label}] (last 50 lines, progress omitted)")
                    else:
                        print(f"   [{label}]")
                    for ln in show:
                        print(f"      {ln}")
            if not err_str and not out_str:
                print("   (no output captured)")
            err_lower = err_str.lower()
            if "403" in err_lower or "forbidden" in err_lower:
                print("   💡 403 = instance profile needs s3:GetObject on the bucket/prefix.")
            combined_lower = combined.lower()
            if "prerequisites not met" in combined_lower or "selinux-policy-devel" in combined_lower or "missing" in combined_lower:
                print("   💡 Prerequisites = install on host: sudo yum install -y selinux-policy-devel policycoreutils-python")
            print("   Common causes: 403 = S3 permissions; prerequisites (selinux-policy-devel); sudo NOPASSWD; traps_pmd failed to start.")
            failed.append(ip)
        print("--------------------------------------------")

    return failed


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: install_cortex_from_s3.py <config_file>")
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

    if not config.get("IPS_MODE") and not config.get("TAG_FILTERS") and not config.get("IPS"):
        config["IPS_MODE"] = "tag"  # default for backward compat with configs that only have TAG_FILTERS
    if not config.get("IPS_MODE") and _ensure_list(config.get("TAG_FILTERS")):
        config["IPS_MODE"] = "tag"

    validate_config(config, multi_account=multi_account)

    if multi_account:
        for role_arn in account_roles:
            role_arn = role_arn.strip()
            if not role_arn:
                continue
            account_id = account_id_from_role_arn(role_arn)
            print()
            print("=" * 60)
            print(f"📦 Account {account_id}")
            print("=" * 60)

            apply_credentials(assume_role(role_arn))
            ips = get_ips(config, allow_empty=True)

            if not ips:
                print("   No instances in this account — skipping.")
                continue

            print()
            print(f"🧾 IPs in account {account_id} ({len(ips)} host(s)):")
            for i, ip in enumerate(ips, 1):
                print(f"  {i}. {ip}")
            print()

            confirm = input(f"Run Cortex install on account {account_id} ({len(ips)} IPs)? (yes/no): ").strip().lower()
            if confirm != "yes":
                print("   Skipped by user.")
                continue

            clear_assumed_role_credentials()
            failed = run_install_for_ips(config, ips)
            if failed:
                print(f"⚠️ Failed on: {', '.join(failed)}")
    else:
        ips = get_ips(config)
        print()
        print("🧾 Target instances:")
        for i, ip in enumerate(ips, 1):
            print(f"  {i}. {ip}")
        print()

        confirm = input("Proceed with Cortex installation on these hosts? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("❌ Aborted by user")
            sys.exit(0)

        failed = run_install_for_ips(config, ips)

    print()
    if failed:
        print(f"⚠️ Failed on: {', '.join(failed)}")
    else:
        print("✅ Cortex successfully applied on all targets")
    print("🏁 Cortex rollout finished")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user (Ctrl+C)")
        sys.exit(130)
