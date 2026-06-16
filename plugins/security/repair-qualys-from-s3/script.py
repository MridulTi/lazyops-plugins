#!/usr/bin/env python3
"""
Repair Qualys agent on servers where it is failed or in error.

Supports multiple AWS accounts: assume role per account only to list IPs (EC2);
repair is done by SSH/SCP from this machine. Agent packages are copied from a local
directory (default ~/Downloads/qualys_packages); CentOS/RHEL/Amazon Linux use .rpm only.

Config file:
  - ROLE_ARN=( "arn:aws:iam::ACCOUNT_ID:role/RoleName" ... ) or ACCOUNT_ROLES=(...)  # one or more roles
  - IPS_MODE=tag: TAG_FILTERS (multiple), optional INSTANCE_FILTERS, EXCLUDE_TAGS; optional SKIP_ASG_INSTANCES=true
  - IPS_MODE=static: IPS="ip1 ip2 ..." or IPS_FILE="path/to/ips.txt" (one IP per line or space/comma separated)
  - QUALYS_LOCAL_PKG_DIR — folder with Qualys_Linux_*.deb / *.rpm (default: ~/Downloads/qualys_packages)
  - Other vars: ACTIVATION_ID, CUSTOMER_ID, SERVER_URI, AWS_REGION, SSH_KEY_DIR

Usage:
    python3 repair_qualys_from_s3.py <config_file> [--yes|-y]
    IPS_MODE=static with IPS="ip1 ip2" or IPS_FILE="path/to/ips.txt" for static IPs; --yes to run non-interactive (e.g. nohup).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Unbuffered output so nohup/redirected logs show progress in real time
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)


REQUIRED_VARS = (
    "ACTIVATION_ID",
    "CUSTOMER_ID",
    "SERVER_URI",
    "AWS_REGION",
    "IPS_MODE",
    "SSH_KEY_DIR",
)
AGENT_UBUNTU_X64 = "Qualys_Linux_X64.deb"
AGENT_UBUNTU_ARM = "Qualys_Linux_ARM64.deb"
AGENT_AMZN_X64 = "Qualys_Linux_X64.rpm"
AGENT_AMZN_ARM = "Qualys_Linux_ARM64.rpm"
SSH_USERS = ("ec2-user", "ubuntu", "centos")
AGENT_BIN = "/usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh"
SERVICE = "qualys-cloud-agent"

QUALYS_PROBE_SCRIPT = """set -e
ARCH=$(uname -m)
. /etc/os-release 2>/dev/null || true
echo "$ARCH"
echo "${ID:-unknown}"
"""

# Remote script: check Qualys status; stdout one of QUALYS_OK, QUALYS_*; exit 0 only for OK
CHECK_SCRIPT = f"""
AGENT_BIN="{AGENT_BIN}"
SERVICE="{SERVICE}"
if [[ ! -x "$AGENT_BIN" ]]; then echo "QUALYS_NOT_INSTALLED"; exit 1; fi
if systemctl is-failed --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_FAILED"; exit 1; fi
if ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_INACTIVE"; exit 1; fi
echo "QUALYS_OK"; exit 0
"""

# Remote script: try to start Qualys service (when installed but not running). Exit 0 if active after start.
START_SCRIPT = f"""
SERVICE="{SERVICE}"
sudo systemctl reset-failed "$SERVICE" 2>/dev/null || true
sudo systemctl start "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE"
"""


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
        # Strip inline comments (# ...) for non-array values
        if "#" in rest and not rest.startswith("("):
            rest = rest.split("#", 1)[0].strip().rstrip()
        if rest.startswith("("):
            # Array: collect until closing )
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
    if multi_account:
        if not config.get("ACCOUNT_ROLES") and not config.get("ROLE_ARN"):
            print("❌ ACCOUNT_ROLES or ROLE_ARN is required for multi-account mode")
            sys.exit(1)
        if config.get("IPS_MODE") != "tag":
            print("❌ IPS_MODE=tag is required when using ACCOUNT_ROLES (to list IPs per account)")
            sys.exit(1)
    missing = [v for v in REQUIRED_VARS if not config.get(v)]
    if missing:
        print(f"❌ Missing in config: {', '.join(missing)}")
        sys.exit(1)
    pkg_dir = Path(os.path.expanduser(str(config.get("QUALYS_LOCAL_PKG_DIR") or "~/Downloads/qualys_packages"))).resolve()
    if not pkg_dir.is_dir():
        print(f"❌ QUALYS_LOCAL_PKG_DIR is not a directory: {pkg_dir}")
        sys.exit(1)
    if config.get("IPS_MODE") == "static":
        if not config.get("IPS") and not (config.get("IPS_FILE") and Path(str(config.get("IPS_FILE"))).is_file()):
            print("❌ IPS_MODE=static requires IPS=\"ip1 ip2\" or IPS_FILE=\"path/to/ips.txt\"")
            sys.exit(1)


def account_id_from_role_arn(role_arn: str) -> str:
    """Extract account ID from role ARN (arn:aws:iam::123456789012:role/RoleName)."""
    parts = role_arn.split(":")
    if len(parts) >= 5:
        return parts[4]
    return role_arn


def assume_role(role_arn: str, session_name: str | None = None) -> dict:
    """Assume role and return dict with AccessKeyId, SecretAccessKey, SessionToken."""
    if not session_name:
        import time
        session_name = f"repair_qualys_{int(time.time())}"
    result = subprocess.run(
        [
            "aws", "sts", "assume-role",
            "--role-arn", role_arn,
            "--role-session-name", session_name,
            "--output", "json",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"❌ assume-role failed for {role_arn}: {result.stderr}")
        sys.exit(1)
    data = json.loads(result.stdout)
    return data["Credentials"]


def apply_credentials(creds: dict) -> None:
    """Set AWS credential environment variables for child processes."""
    os.environ["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
    os.environ["AWS_SESSION_TOKEN"] = creds["SessionToken"]


def clear_assumed_role_credentials() -> None:
    """Remove assumed-role env vars. Role is only for finding IPs; SSH/repair do not use it."""
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        os.environ.pop(key, None)


def get_ssh_keys(ssh_key_dir: str) -> list[Path]:
    keys = sorted(Path(ssh_key_dir).glob("*.pem"))
    if not keys:
        print(f"❌ No .pem keys found in {ssh_key_dir}")
        sys.exit(1)
    return keys


def _ensure_list(val: str | list | None) -> list:
    """Normalize config value to list of strings (single string -> [s], list -> list, empty -> [])."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    return [s] if s else []


def get_ips(config: dict, allow_empty: bool = False) -> list[str]:
    """Same logic as install_qualys_from_s3.sh: tag or static. If allow_empty=True, returns [] instead of exiting when no IPs."""
    mode = config["IPS_MODE"]
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
        instance_filters = _ensure_list(config.get("INSTANCE_FILTERS"))  # non-tag: e.g. instance-state-name
        exclude_tags = _ensure_list(config.get("EXCLUDE_TAGS"))
        if not tag_filters:
            print("❌ IPS_MODE=tag but TAG_FILTERS not set")
            sys.exit(1)

        print("🔎 Fetching IPs using filters:")
        for f in tag_filters:
            print(f"   - [tag] {f}")
        for f in instance_filters:
            print(f"   - [instance] {f}")

        cmd = [
            "aws", "ec2", "describe-instances",
            "--region", region,
            "--query", "Reservations[].Instances[]",
            "--output", "json",
        ]
        # All filters must be arguments to a single --filters (repeated --filters = only last one used by AWS CLI)
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
        if skip_asg:
            print("🚫 Skipping instances that are in an Auto Scaling Group (tag aws:autoscaling:groupName)")

        print("🚫 Applying exclusion filters:")
        for e in exclude_tags:
            print(f"   - NOT {e}")

        exclude_set = set()
        for ex in exclude_tags:
            ex = (ex or "").strip()
            if not ex:
                continue
            # Support: "key=value" (tag key=value) or "Name=tag:Key,Values=val" / "Key=...,Values=..."
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
                continue  # Skip instances that are in an ASG
            skip = False
            for k, v in exclude_set:
                if tags.get(k) == v:
                    skip = True
                    break
            if not skip:
                ips.append(inst["PrivateIpAddress"])

        if not ips and not allow_empty:
            print("❌ No IPs left after exclusions")
            sys.exit(1)
        return ips

    print("❌ Invalid IPS_MODE")
    sys.exit(1)


def ssh_run(ip: str, user: str, key_path: Path, script: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run script over SSH. Returns (returncode, stdout, stderr). Uses login shell (-l) so PATH/env match interactive SSH."""
    cmd = [
        "ssh",
        "-i", str(key_path),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-o", "IdentitiesOnly=yes",
        f"{user}@{ip}",
        "bash",
        "-l",
        "-s",
    ]
    result = subprocess.run(
        cmd,
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def check_qualys(config: dict, ip: str) -> tuple[bool, str | None, str | None, str]:
    """
    Try SSH users/keys; run status check.
    Returns (connected, user, key_name, status).
    status is one of QUALYS_OK, QUALYS_NOT_INSTALLED, QUALYS_SERVICE_FAILED, QUALYS_SERVICE_INACTIVE.
    """
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for user in SSH_USERS:
        for key in keys:
            rc, out, err = ssh_run(ip, user, key, CHECK_SCRIPT, timeout=15)
            if rc != 0 and not out:
                continue  # SSH or connection failed
            # We might have connected but script failed (exit 1) and printed status
            status = (out or "").strip().splitlines()[-1] if out else ""
            if status.startswith("QUALYS_"):
                return True, user, key.name, status
    return False, None, None, ""


def try_start_qualys(ip: str, user: str, key_path: Path) -> bool:
    """Try to start Qualys service on host. Returns True if service is active after start."""
    rc, _, _ = ssh_run(ip, user, key_path, START_SCRIPT, timeout=30)
    return rc == 0


def qualys_local_pkg_dir(config: dict) -> Path:
    """Directory on this machine containing Qualys_Linux_*.deb / *.rpm (see QUALYS_LOCAL_PKG_DIR)."""
    return Path(os.path.expanduser(str(config.get("QUALYS_LOCAL_PKG_DIR") or "~/Downloads/qualys_packages"))).resolve()


def resolve_qualys_agent(os_id: str, arch: str) -> tuple[str, str] | None:
    """
    Map probed OS + arch to local package filename and installer kind.
    CentOS/RHEL/Rocky/Alma/Amazon: RPM only. Ubuntu/Debian: .deb.
    """
    oid = (os_id or "").lower().strip()
    arch = (arch or "").strip()
    if oid in ("ubuntu", "debian"):
        if arch in ("x86_64", "amd64"):
            return AGENT_UBUNTU_X64, "deb"
        if arch in ("aarch64", "arm64"):
            return AGENT_UBUNTU_ARM, "deb"
        return None
    if oid in ("amzn", "rhel", "centos", "rocky", "almalinux"):
        if arch in ("x86_64", "amd64"):
            return AGENT_AMZN_X64, "rpm"
        if arch in ("aarch64", "arm64"):
            return AGENT_AMZN_ARM, "rpm"
        return None
    return None


def scp_qualys_file(local_path: Path, ip: str, user: str, key_path: Path, remote_path: str) -> tuple[bool, str]:
    target = f"{user}@{ip}:{remote_path}"
    cmd = [
        "scp",
        "-i", str(key_path),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "StrictHostKeyChecking=no",
        "-o", "IdentitiesOnly=yes",
        str(local_path),
        target,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "scp failed").strip()
    return True, ""


def _shell_quote(s: str) -> str:
    """Escape for use inside double-quoted bash string."""
    return (s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`"))

def _print_reinstall_error(stdout: str, stderr: str, max_lines: int = 15) -> None:
    """Print last lines of stdout/stderr to help debug reinstall failures."""
    had_output = False
    for label, text in [("stdout", stdout), ("stderr", stderr)]:
        if not text:
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        had_output = True
        show = lines[-max_lines:] if len(lines) > max_lines else lines
        print(f"   [{label}]")
        for line in show:
            print(f"      {line}")
        print()
    if not had_output:
        print("   (no output captured — possible SSH timeout or connection drop during reinstall)")
        print()


def reinstall_qualys(config: dict, ip: str, user: str, key_path: Path) -> tuple[bool, str, str]:
    """Probe OS/arch, SCP package from QUALYS_LOCAL_PKG_DIR, reinstall on host. Returns (success, stdout, stderr)."""
    pkg_dir = qualys_local_pkg_dir(config)

    rc, out, err = ssh_run(ip, user, key_path, QUALYS_PROBE_SCRIPT, timeout=30)
    if rc != 0:
        return False, (out or ""), err or f"probe failed rc={rc}"

    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return False, out or "", err or "could not parse OS/arch probe"

    arch, os_id = lines[0], lines[1]
    resolved = resolve_qualys_agent(os_id, arch)
    if not resolved:
        return False, f"Unsupported OS/ARCH: {os_id}-{arch}", err or ""

    agent_name, install_kind = resolved
    local_pkg = pkg_dir / agent_name
    if not local_pkg.is_file():
        return False, "", f"Missing local package (copy Qualys installers into {pkg_dir}): {agent_name}"

    mkdir_rc, _, mkdir_err = ssh_run(ip, user, key_path, "mkdir -p /tmp", timeout=20)
    if mkdir_rc != 0:
        return False, "", mkdir_err or "mkdir /tmp failed"

    remote_pkg = f"/tmp/{agent_name}"
    ok_scp, scp_err = scp_qualys_file(local_pkg, ip, user, key_path, remote_pkg)
    if not ok_scp:
        return False, "", f"SCP failed: {scp_err}"

    aid = _shell_quote(config["ACTIVATION_ID"])
    cid = _shell_quote(config["CUSTOMER_ID"])
    uri = _shell_quote(config["SERVER_URI"])
    agent_q = _shell_quote(agent_name)

    reinstall_script = f"""
set -e
AGENT="{agent_q}"
INSTALL_KIND="{install_kind}"
ACTIVATION_ID="{aid}"
CUSTOMER_ID="{cid}"
SERVER_URI="{uri}"
AGENT_BIN="{AGENT_BIN}"
SERVICE="{SERVICE}"

sudo systemctl stop "$SERVICE" 2>/dev/null || true

if command -v rpm &>/dev/null; then
    sudo rpm -e qualys-cloud-agent 2>/dev/null || true
elif command -v dpkg &>/dev/null; then
    sudo dpkg -r qualys-cloud-agent 2>/dev/null || true
fi

[[ -f "/tmp/$AGENT" ]] || {{ echo "Package missing after SCP: /tmp/$AGENT" >&2; exit 1; }}

if [[ "$INSTALL_KIND" == "deb" ]]; then
  sudo dpkg -i "/tmp/$AGENT" || true
elif [[ "$INSTALL_KIND" == "rpm" ]]; then
  sudo rpm -Uvh "/tmp/$AGENT" || true
else
  echo "Invalid INSTALL_KIND: $INSTALL_KIND" >&2
  exit 1
fi

sudo "$AGENT_BIN" ActivationId="$ACTIVATION_ID" CustomerId="$CUSTOMER_ID" ServerUri="$SERVER_URI"
sudo systemctl start "$SERVICE"
systemctl is-active --quiet "$SERVICE"
"""

    rc, out, err = ssh_run(ip, user, key_path, reinstall_script, timeout=300)
    return rc == 0, (out or ""), (err or "")


def run_repair_for_ips(config: dict, ips: list[str]) -> None:
    """Run Qualys check/reinstall for the given list of IPs."""
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for ip in ips:
        print(f"🔍 Processing {ip}")

        connected, user, key_name, status = check_qualys(config, ip)

        if not connected:
            print(f"❌ Could not connect to {ip} — skipping")
            print("--------------------------------------------")
            continue

        if status == "QUALYS_OK":
            print(f"✅ {ip} — Qualys OK, skipping")
            print("--------------------------------------------")
            continue

        key_path = next((k for k in keys if k.name == key_name), keys[0])

        # If installed but not running, try to start first; only reinstall if start fails or not installed
        if status in ("QUALYS_SERVICE_INACTIVE", "QUALYS_SERVICE_FAILED"):
            if try_start_qualys(ip, user, key_path):
                print(f"🟢 {ip} — started Qualys (was {status})")
                print("--------------------------------------------")
                continue
            # Start failed, fall through to reinstall

        print(f"🔧 {ip} — {status}, installing/reinstalling...")
        def try_reinstall(u: str, k: Path) -> tuple[bool, str, str]:
            ok, out, err = reinstall_qualys(config, ip, u, k)
            return ok, out, err

        ok, out, err = try_reinstall(user, key_path)
        if ok:
            print(f"🟢 Reinstall success on {ip} as {user}")
        else:
            # Save first failure (user/key that passed check) — usually the real reinstall error
            first_fail_out, first_fail_err = out, err
            # Try every other (user, key) combination
            done = False
            for u in SSH_USERS:
                for k in keys:
                    if (u, k) == (user, key_path):
                        continue
                    print(f"   Trying {u} with {k.name}...")
                    ok, out, err = try_reinstall(u, k)
                    if ok:
                        print(f"🟢 Reinstall success on {ip} as {u}")
                        done = True
                        break
                if done:
                    break
            if not done:
                print(f"⚠️ Reinstall failed for {ip}")
                print("   First attempt (user/key that passed check) — likely the real cause:")
                _print_reinstall_error(first_fail_out, first_fail_err)

        print("--------------------------------------------")


def main() -> None:
    args = [a for a in sys.argv[1:] if a not in ("--yes", "-y")]
    auto_yes = len(args) < len(sys.argv) - 1
    if not args:
        print("Usage: repair_qualys_from_s3.py <config_file> [--yes|-y]")
        sys.exit(1)

    config_path = args[0]
    if not Path(config_path).is_file():
        print(f"❌ Config file '{config_path}' not found!")
        sys.exit(1)

    config = load_config(config_path)
    # Support both ACCOUNT_ROLES and ROLE_ARN (qualys.conf style)
    account_roles = config.get("ACCOUNT_ROLES") or config.get("ROLE_ARN") or []
    if isinstance(account_roles, str):
        account_roles = [account_roles]
    multi_account = len(account_roles) > 0

    validate_config(config, multi_account=multi_account)

    if multi_account:
        # Multi-account: assume role per account, list IPs, prompt, then run if yes
        for role_arn in account_roles:
            role_arn = role_arn.strip()
            if not role_arn:
                continue
            account_id = account_id_from_role_arn(role_arn)
            print()
            print("=" * 60)
            print(f"📦 Account {account_id} (role: {role_arn})")
            print("=" * 60)

            creds = assume_role(role_arn)
            apply_credentials(creds)

            ips = get_ips(config, allow_empty=True)

            if not ips:
                print("   No instances found in this account — skipping.")
                continue

            print()
            print(f"🧾 IPs in account {account_id} ({len(ips)} host(s)):")
            for i, ip in enumerate(ips, 1):
                print(f"  {i}. {ip}")
            print()

            if not auto_yes:
                confirm = input(f"Run repair on account {account_id} ({len(ips)} IPs)? (yes/no): ").strip().lower()
                if confirm != "yes":
                    print("   Skipped by user.")
                    continue
            else:
                print("   Proceeding (--yes).")

            # Role was only for listing IPs; clear it before SSH (packages copied via SCP from this machine)
            clear_assumed_role_credentials()
            run_repair_for_ips(config, ips)
    else:
        # Single-account: no assume role, get IPs once, single prompt
        ips = get_ips(config)

        print()
        print("🧾 Target instances (check Qualys status, reinstall only if failed/error):")
        for i, ip in enumerate(ips, 1):
            print(f"  {i}. {ip}")
        print()

        if not auto_yes:
            confirm = input("Proceed? (yes/no): ").strip().lower()
            if confirm != "yes":
                print("❌ Aborted by user")
                sys.exit(0)
        else:
            print("Proceeding (--yes).")

        run_repair_for_ips(config, ips)


if __name__ == "__main__":
    main()
