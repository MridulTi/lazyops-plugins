#!/usr/bin/env python3
"""
Install or reinstall Qualys Cloud Agent on servers using a local .rpm / .deb (SCP), not S3.

Flow per host:
  1. SSH probe (same users/keys as install_cortex_from_local.py).
  2. If qualys-cloud-agent is active and healthy, skip (unless you force reinstall — not implemented).
  3. Otherwise: SCP the chosen package, remove old agent if present, install, run
     qualys-cloud-agent.sh with ActivationId / CustomerId / ServerUri, start systemd unit.

Config file (bash-style, same parser as install_cortex_from_local.py):
  - Exactly one of:
      LOCAL_QUALYS_PKG="/full/path/to/Qualys_Linux_X64.rpm"
    or
      LOCAL_QUALYS_DIR="/path/to/folder"
    When LOCAL_QUALYS_DIR is set, the script SSH-probes each host (``/etc/os-release`` ID, ``uname -m``),
    then picks a package: prefers Qualys_Linux_X64.deb / ARM64.deb / X64.rpm / ARM64.rpm (same names as
    repair_qualys_from_s3.py), else newest matching ``*.deb`` / ``*.rpm`` in the directory by arch hints
    in the filename (aarch64/arm64 vs x86_64/X64/amd64).
  - ACTIVATION_ID, CUSTOMER_ID, SERVER_URI (e.g. https://qualysapi.qualys.com)
  - SSH_KEY_DIR; IPS_MODE=static with IPS="ip1 ip2" and/or IPS_FILE="/path/to/hosts.txt"
    (file: one IP per line or space/comma separated; # starts a comment)
  - If IPS_FILE is set and IPS_MODE is omitted, static mode is used (unless TAG_FILTERS imply tag).
  - IPS_MODE=tag: AWS_REGION, TAG_FILTERS / INSTANCE_FILTERS, etc. (same semantics as Cortex script)
  - Optional: REMOTE_TMP_PARENT="/tmp"
  - Optional: --yes | -y  skip confirmation (e.g. nohup)

Usage:
  python3 install_qualys_from_local.py <config_file> [--yes|-y]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

SSH_USERS = ("ec2-user", "ubuntu", "centos")
SERVICE = "qualys-cloud-agent"
AGENT_BIN = "/usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh"

REQUIRED_LOCAL = ("SSH_KEY_DIR",)
REQUIRED_QUALYS = ("ACTIVATION_ID", "CUSTOMER_ID", "SERVER_URI")
REQUIRED_TAG = ("AWS_REGION",)

# Preferred filenames (same as repair_qualys_from_s3.py / Nexus layout)
AGENT_UBUNTU_X64 = "Qualys_Linux_X64.deb"
AGENT_UBUNTU_ARM = "Qualys_Linux_ARM64.deb"
AGENT_AMZN_X64 = "Qualys_Linux_X64.rpm"
AGENT_AMZN_ARM = "Qualys_Linux_ARM64.rpm"

RPM_OS_IDS = frozenset(
    {
        "amzn",
        "rhel",
        "centos",
        "rocky",
        "almalinux",
        "fedora",
        "ol",
        "sles",
        "opensuse-leap",
        "opensuse-tumbleweed",
    }
)
DEB_OS_IDS = frozenset({"ubuntu", "debian"})


def load_config(path: str) -> dict:
    """Parse bash-style config file (KEY=value and KEY=( "v1" "v2" ) including multiline)."""
    config: dict = {}
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


def _ensure_list(val: str | list | None) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    return [s] if s else []


def _parse_ips_file_content(text: str) -> list[str]:
    raw: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if not line:
            continue
        raw.extend(re.split(r"[\s,]+", line))
    return [x.strip() for x in raw if x.strip()]


def validate_qualys_bundle(config: dict) -> None:
    pkg_s = (config.get("LOCAL_QUALYS_PKG") or "").strip()
    dir_s = (config.get("LOCAL_QUALYS_DIR") or "").strip()
    if pkg_s and dir_s:
        print("❌ Set only one of LOCAL_QUALYS_PKG or LOCAL_QUALYS_DIR")
        sys.exit(1)
    if not pkg_s and not dir_s:
        print("❌ Set LOCAL_QUALYS_PKG (single .rpm/.deb) or LOCAL_QUALYS_DIR (folder of installers)")
        sys.exit(1)
    if pkg_s:
        p = Path(pkg_s).expanduser()
        if not p.is_file():
            print(f"❌ LOCAL_QUALYS_PKG not a file: {p}")
            sys.exit(1)
        low = p.name.lower()
        if not (low.endswith(".rpm") or low.endswith(".deb")):
            print("❌ LOCAL_QUALYS_PKG must be a .rpm or .deb")
            sys.exit(1)
    else:
        root = Path(dir_s).expanduser()
        if not root.is_dir():
            print(f"❌ LOCAL_QUALYS_DIR is not a directory: {root}")
            sys.exit(1)
        rpms = list(root.glob("*.rpm"))
        debs = list(root.glob("*.deb"))
        if not rpms and not debs:
            print(f"❌ LOCAL_QUALYS_DIR has no .rpm or .deb: {root}")
            sys.exit(1)


def classify_pkg_family(os_id: str) -> str | None:
    oid = (os_id or "unknown").strip().lower()
    if oid in RPM_OS_IDS or oid.startswith("amzn") or "rhel" in oid:
        return "rpm"
    if oid in DEB_OS_IDS:
        return "deb"
    return None


def classify_cpu_family(arch: str) -> str | None:
    ar = (arch or "").strip().lower()
    if ar in ("aarch64", "arm64"):
        return "aarch64"
    if ar in ("x86_64", "amd64"):
        return "x64"
    return None


def _filename_suggests_arm(name: str) -> bool:
    n = name.lower()
    return "aarch64" in n or "arm64" in n or "_arm" in n or "linux_arm" in n


def _filename_suggests_x64(name: str) -> bool:
    n = name.lower()
    if _filename_suggests_arm(n):
        return False
    return "x64" in n or "x86_64" in n or "amd64" in n or "linux_x" in n or "cloudagent" in n


def pick_package_from_dir(directory: Path, os_id: str, arch: str) -> tuple[Path | None, str]:
    """Choose .rpm or .deb under directory for remote OS/arch."""
    pkg = classify_pkg_family(os_id)
    if not pkg:
        return None, f"unsupported OS ID={os_id!r} (need rpm: amzn/rhel/… or deb: ubuntu/debian)"
    cpu = classify_cpu_family(arch)
    if not cpu:
        return None, f"unsupported arch={arch!r}"

    preferred: list[str]
    if pkg == "deb":
        preferred = [AGENT_UBUNTU_ARM, AGENT_UBUNTU_X64] if cpu == "aarch64" else [AGENT_UBUNTU_X64, AGENT_UBUNTU_ARM]
    else:
        preferred = [AGENT_AMZN_ARM, AGENT_AMZN_X64] if cpu == "aarch64" else [AGENT_AMZN_X64, AGENT_AMZN_ARM]

    for base in preferred:
        p = directory / base
        if p.is_file():
            return p, f"{base} for OS={os_id!r} arch={arch!r}"

    suffix = ".deb" if pkg == "deb" else ".rpm"
    candidates: list[Path] = []
    for p in directory.iterdir():
        if not p.is_file() or not p.name.lower().endswith(suffix):
            continue
        n = p.name
        if cpu == "aarch64":
            if not _filename_suggests_arm(n):
                continue
        else:
            if _filename_suggests_arm(n):
                continue
            if not (_filename_suggests_x64(n) or "qualys" in n.lower()):
                continue
        candidates.append(p)

    if not candidates:
        names = sorted(x.name for x in directory.iterdir() if x.is_file())[:40]
        extra = f" (files: {names})" if names else ""
        return None, f"no matching *{suffix} for {pkg}+{cpu} (REMOTE_OS_ID={os_id!r} REMOTE_ARCH={arch!r}){extra}"

    chosen = max(candidates, key=lambda x: x.stat().st_mtime)
    return chosen, f"{chosen.name} (newest match for OS={os_id!r} arch={arch!r})"


REMOTE_OS_PROBE = r"""
ARCH=$(uname -m)
ID_L="unknown"
if [[ -f /etc/os-release ]]; then
  # shellcheck source=/dev/null
  . /etc/os-release || true
  ID_L="${ID:-unknown}"
fi
echo "REMOTE_OS_ID=${ID_L}"
echo "REMOTE_ARCH=${ARCH}"
"""


def parse_remote_os_probe(text: str) -> tuple[str, str]:
    os_id, arch = "unknown", "unknown"
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("REMOTE_OS_ID="):
            os_id = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("REMOTE_ARCH="):
            arch = line.split("=", 1)[1].strip()
    return os_id, arch


def local_pkg_kind(path: Path) -> str:
    low = path.name.lower()
    if low.endswith(".deb"):
        return "deb"
    if low.endswith(".rpm"):
        return "rpm"
    return "unknown"


def resolve_local_package(config: dict, ip: str, user: str, key_path: Path) -> tuple[Path | None, str, str | None]:
    """
    Return (path, reason, expected_kind or None).
    For LOCAL_QUALYS_PKG, expected_kind is deb/rpm for remote validation.
    """
    pkg_s = (config.get("LOCAL_QUALYS_PKG") or "").strip()
    if pkg_s:
        p = Path(pkg_s).expanduser().resolve()
        if not p.is_file():
            return None, f"missing file {p}", None
        k = local_pkg_kind(p)
        return p, str(p), k if k != "unknown" else None

    dir_s = (config.get("LOCAL_QUALYS_DIR") or "").strip()
    root = Path(dir_s).expanduser().resolve()
    rc, out, err = ssh_run(ip, user, key_path, REMOTE_OS_PROBE, timeout=30, login_shell=True)
    combined = f"{out}\n{err}".strip()
    if rc != 0:
        return None, f"remote OS probe failed (rc={rc}): {combined[:500]}", None
    os_id, arch = parse_remote_os_probe(combined)
    picked, msg = pick_package_from_dir(root, os_id, arch)
    if picked is None:
        return None, msg, None
    return picked, msg, local_pkg_kind(picked)


def validate_config(config: dict, multi_account: bool = False) -> None:
    for v in REQUIRED_LOCAL:
        if not config.get(v):
            print(f"❌ Missing in config: {v}")
            sys.exit(1)
    for v in REQUIRED_QUALYS:
        if not str(config.get(v) or "").strip():
            print(f"❌ Missing in config: {v}")
            sys.exit(1)
    validate_qualys_bundle(config)

    mode = config.get("IPS_MODE") or "tag"
    if mode == "tag":
        for v in REQUIRED_TAG:
            if not config.get(v):
                print(f"❌ Missing in config for IPS_MODE=tag: {v}")
                sys.exit(1)
    if mode == "static":
        ips_inline = config.get("IPS")
        has_inline = bool(str(ips_inline or "").strip()) or (
            isinstance(ips_inline, list) and any(str(x).strip() for x in ips_inline)
        )
        ips_file = (config.get("IPS_FILE") or "").strip()
        if ips_file:
            fp = Path(ips_file).expanduser()
            if not fp.is_file():
                print(f"❌ IPS_FILE not found or not a file: {fp}")
                sys.exit(1)
        if not has_inline and not ips_file:
            print("❌ IPS_MODE=static requires IPS and/or IPS_FILE (non-empty)")
            sys.exit(1)
    if multi_account:
        if not config.get("ACCOUNT_ROLES") and not config.get("ROLE_ARN"):
            print("❌ ACCOUNT_ROLES or ROLE_ARN is required for multi-account mode")
            sys.exit(1)
        if config.get("IPS_MODE") != "tag":
            print("❌ IPS_MODE=tag is required when using ROLE_ARN")
            sys.exit(1)


def account_id_from_role_arn(role_arn: str) -> str:
    parts = role_arn.split(":")
    return parts[4] if len(parts) >= 5 else role_arn


def assume_role(role_arn: str, session_name: str | None = None) -> dict:
    import time

    name = session_name or f"install_qualys_local_{int(time.time())}"
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
    keys = sorted(Path(ssh_key_dir).expanduser().glob("*.pem"))
    if not keys:
        print(f"❌ No .pem keys found in {ssh_key_dir}")
        sys.exit(1)
    return keys


def get_ips(config: dict, allow_empty: bool = False) -> list[str]:
    mode = config.get("IPS_MODE") or "tag"

    if mode == "static":
        raw: list[str] = []
        ips_cfg = config.get("IPS")
        if isinstance(ips_cfg, str) and ips_cfg.strip():
            raw.extend(re.split(r"[\s,]+", ips_cfg))
        elif isinstance(ips_cfg, list):
            raw.extend(str(x).strip() for x in ips_cfg if str(x).strip())
        ips_file = (config.get("IPS_FILE") or "").strip()
        if ips_file:
            fp = Path(ips_file).expanduser()
            if not fp.is_file():
                print(f"❌ IPS_FILE not a file: {fp}")
                sys.exit(1)
            raw.extend(_parse_ips_file_content(fp.read_text(encoding="utf-8")))
        out = [x.strip() for x in raw if x.strip()]
        if not out and not allow_empty:
            print("❌ IPS_MODE=static produced no IPs (check IPS / IPS_FILE)")
            sys.exit(1)
        return out

    if mode == "tag":
        region = config["AWS_REGION"]
        tag_filters = _ensure_list(config.get("TAG_FILTERS"))
        instance_filters = _ensure_list(config.get("INSTANCE_FILTERS"))
        exclude_tags = _ensure_list(config.get("EXCLUDE_TAGS"))

        cmd = [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            region,
            "--query",
            "Reservations[].Instances[]",
            "--output",
            "json",
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
    cmd = [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "IdentitiesOnly=yes",
        f"{user}@{ip}",
        "bash",
        *(["-l", "-s"] if login_shell else ["-s"]),
    ]
    if use_tt:
        cmd.insert(1, "-tt")
    result = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def scp_file(local_path: Path, ip: str, user: str, key_path: Path, remote_path: str, timeout: int = 600) -> tuple[int, str, str]:
    cmd = [
        "scp",
        "-i",
        str(key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "IdentitiesOnly=yes",
        str(local_path),
        f"{user}@{ip}:{remote_path}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


PROBE_SCRIPT = "echo QUALYS_PROBE_OK"

CHECK_SCRIPT = f"""
AGENT_BIN="{AGENT_BIN}"
SERVICE="{SERVICE}"
if [[ ! -x "$AGENT_BIN" ]]; then echo "QUALYS_NOT_INSTALLED"; exit 1; fi
if systemctl is-failed --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_FAILED"; exit 1; fi
if ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_INACTIVE"; exit 1; fi
echo "QUALYS_OK"; exit 0
"""


def check_ssh_connection(config: dict, ip: str) -> tuple[bool, str | None, str | None]:
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for user in SSH_USERS:
        for key in keys:
            rc, out, _ = ssh_run(ip, user, key, PROBE_SCRIPT, timeout=25, use_tt=False)
            if rc == 0 and (out or "").strip() == "QUALYS_PROBE_OK":
                return True, user, key.name
    return False, None, None


def qualys_status_from_check(combined: str) -> str:
    for line in (combined or "").splitlines():
        line = line.strip()
        if line.startswith("QUALYS_"):
            return line
    return ""


def build_install_script(pkg_basename: str, remote_dir: str, install_kind: str) -> str:
    if re.search(r"[^A-Za-z0-9._+-]", pkg_basename):
        raise ValueError("Unsafe package basename; use only letters, digits, . _ + -")
    pb = pkg_basename
    rd = remote_dir.replace("$", "\\$")
    ik = install_kind
    return f"""
set -e
echo "QUALYS_LOCAL_INSTALL_START" >&2
SERVICE="{SERVICE}"
AGENT_BIN="{AGENT_BIN}"
TMP="{rd}"
PKG="{pb}"
KIND="{ik}"
sudo -n true 2>/dev/null || {{ echo "QUALYS_ERROR: sudo requires password" >&2; exit 1; }}
mkdir -p "$TMP"
cd "$TMP"
[[ -f "$PKG" ]] || {{ echo "QUALYS_ERROR: missing package $TMP/$PKG" >&2; exit 1; }}

sudo systemctl stop "$SERVICE" 2>/dev/null || true
if command -v rpm &>/dev/null; then
  sudo rpm -e qualys-cloud-agent 2>/dev/null || true
elif command -v dpkg &>/dev/null; then
  sudo dpkg -r qualys-cloud-agent 2>/dev/null || true
fi

if [[ "$KIND" == "deb" ]]; then
  sudo dpkg -i "$PKG" || {{ echo "QUALYS_ERROR: dpkg install failed" >&2; exit 1; }}
elif [[ "$KIND" == "rpm" ]]; then
  if sudo dnf install -y "$PKG" 2>/dev/null; then
    true
  elif sudo yum install -y "$PKG" 2>/dev/null; then
    true
  else
    sudo rpm -Uvh "$PKG" || {{ echo "QUALYS_ERROR: rpm install failed" >&2; exit 1; }}
  fi
else
  echo "QUALYS_ERROR: invalid KIND=$KIND" >&2
  exit 1
fi

sudo "$AGENT_BIN" ActivationId=___AID___ CustomerId=___CID___ ServerUri=___URI___
sudo systemctl daemon-reload 2>/dev/null || true
sudo systemctl enable "$SERVICE" 2>/dev/null || true
sudo systemctl restart "$SERVICE" 2>/dev/null || sudo systemctl start "$SERVICE"
systemctl is-active --quiet "$SERVICE" || {{ echo "QUALYS_ERROR: service not active" >&2; exit 1; }}
echo "QUALYS_LOCAL_INSTALL_DONE"
"""


def _shell_single_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def process_host(config: dict, ip: str, keys: list[Path]) -> bool:
    connected, user, key_name = check_ssh_connection(config, ip)
    if not connected or not user or not key_name:
        print(f"❌ Could not SSH to {ip}")
        return False
    key_path = next((k for k in keys if k.name == key_name), keys[0])
    print(f"   SSH: {user} @ {ip} (key {key_name})")

    rc0, check_out, check_err = ssh_run(ip, user, key_path, CHECK_SCRIPT, timeout=60, login_shell=True)
    combined_pre = f"{check_out}\n{check_err}".strip()
    print("   --- qualys pre-check ---")
    for line in combined_pre.splitlines()[:15]:
        print(f"      {line}")
    status = qualys_status_from_check(combined_pre)
    if status == "QUALYS_OK":
        print("   ✅ Qualys already healthy — skipping install")
        return True

    local_pkg, pick_reason, fixed_kind = resolve_local_package(config, ip, user, key_path)
    if local_pkg is None:
        print(f"❌ Could not resolve local package: {pick_reason}")
        return False

    rc_probe, out_probe, err_probe = ssh_run(ip, user, key_path, REMOTE_OS_PROBE, timeout=30, login_shell=True)
    probe_text = f"{out_probe}\n{err_probe}".strip()
    os_id, arch = parse_remote_os_probe(probe_text)
    remote_family = classify_pkg_family(os_id)
    pkg_kind = local_pkg_kind(local_pkg)
    if fixed_kind and remote_family and pkg_kind != remote_family:
        print(f"❌ Package type {pkg_kind} does not match remote OS family {remote_family} (ID={os_id!r})")
        return False

    print(f"   📦 {pick_reason}")

    parent = (config.get("REMOTE_TMP_PARENT") or "/tmp").strip().rstrip("/")
    remote_uid = uuid.uuid4().hex[:12]
    remote_dir = f"{parent}/qualys_local_{remote_uid}"
    remote_pkg_path = f"{remote_dir}/{local_pkg.name}"

    prep_rc, _, prep_err = ssh_run(ip, user, key_path, f"mkdir -p {remote_dir} && chmod 755 {remote_dir}\n", timeout=30)
    if prep_rc != 0:
        print(f"❌ Could not create remote dir: {prep_err}")
        return False

    print(f"   SCP {local_pkg.name} -> {ip}:{remote_pkg_path}")
    scp_rc, _, scp_err = scp_file(local_pkg, ip, user, key_path, remote_pkg_path)
    if scp_rc != 0:
        print(f"❌ SCP failed: {scp_err}")
        return False

    aid = str(config["ACTIVATION_ID"]).strip()
    cid = str(config["CUSTOMER_ID"]).strip()
    uri = str(config["SERVER_URI"]).strip()
    try:
        body_template = build_install_script(local_pkg.name, remote_dir, pkg_kind)
    except ValueError as e:
        print(f"❌ {e}")
        return False

    install_body = (
        body_template.replace("___AID___", _shell_single_quote(aid))
        .replace("___CID___", _shell_single_quote(cid))
        .replace("___URI___", _shell_single_quote(uri))
    )

    print("   🔧 Installing Qualys (package + activation + service)...")
    in_rc, in_out, in_err = ssh_run(ip, user, key_path, install_body, timeout=420, login_shell=True)
    install_log = f"{in_out}\n{in_err}".strip()
    if in_rc != 0 or "QUALYS_ERROR" in install_log:
        print(f"❌ Install failed (rc={in_rc})")
        for ln in install_log.splitlines()[-25:]:
            print(f"      {ln}")
        return False
    print("   🟢 Install completed")

    rc2, post_out, post_err = ssh_run(ip, user, key_path, CHECK_SCRIPT, timeout=60, login_shell=True)
    post_combined = f"{post_out}\n{post_err}".strip()
    print("   --- qualys post-check ---")
    for line in post_combined.splitlines():
        print(f"      {line}")
    if qualys_status_from_check(post_combined) == "QUALYS_OK":
        print("   ✅ Post-check: QUALYS_OK")
    else:
        print("   ⚠️ Post-check: review output above")
    return True


def run_for_ips(config: dict, ips: list[str]) -> list[str]:
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    failed: list[str] = []
    for ip in ips:
        print(f"\n🔍 {ip}")
        if not process_host(config, ip, keys):
            failed.append(ip)
        print("--------------------------------------------")
    return failed


def main() -> None:
    argv = [a for a in sys.argv[1:] if a not in ("--yes", "-y")]
    auto_yes = len(argv) < len(sys.argv) - 1

    if not argv:
        print("Usage: install_qualys_from_local.py <config_file> [--yes|-y]")
        sys.exit(1)

    config_path = argv[0]
    if not Path(config_path).is_file():
        print(f"❌ Config file not found: {config_path}")
        sys.exit(1)

    config = load_config(config_path)
    account_roles = config.get("ACCOUNT_ROLES") or config.get("ROLE_ARN") or []
    if isinstance(account_roles, str):
        account_roles = [account_roles]
    multi_account = len(account_roles) > 0

    if not config.get("IPS_MODE"):
        has_ip_file = bool((config.get("IPS_FILE") or "").strip())
        has_ips = bool(str(config.get("IPS") or "").strip()) or (
            isinstance(config.get("IPS"), list) and any(str(x).strip() for x in (config.get("IPS") or []))
        )
        if _ensure_list(config.get("TAG_FILTERS")):
            config["IPS_MODE"] = "tag"
        elif has_ips or has_ip_file:
            config["IPS_MODE"] = "static"
        else:
            config["IPS_MODE"] = "tag"

    validate_config(config, multi_account=multi_account)

    if multi_account:
        all_failed: list[str] = []
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
                print("   No instances — skipping.")
                clear_assumed_role_credentials()
                continue
            print(f"\n🧾 IPs ({len(ips)}): {', '.join(ips)}")
            if not auto_yes:
                confirm = input(f"Run local Qualys install on account {account_id}? (yes/no): ").strip().lower()
            else:
                confirm = "yes"
                print("   Proceeding (--yes).")
            clear_assumed_role_credentials()
            if confirm != "yes":
                print("   Skipped.")
                continue
            all_failed.extend(run_for_ips(config, ips))
        failed = all_failed
    else:
        ips = get_ips(config)
        print(f"\n🧾 Targets ({len(ips)}): {', '.join(ips)}")
        if not auto_yes:
            confirm = input("Proceed with local Qualys install? (yes/no): ").strip().lower()
        else:
            confirm = "yes"
            print("Proceeding (--yes).")
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
        failed = run_for_ips(config, ips)

    print()
    if failed:
        print(f"⚠️ Failed: {', '.join(failed)}")
        sys.exit(1)
    print("✅ Finished all targets")
    print("🏁 Local Qualys rollout finished")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted (Ctrl+C)")
        sys.exit(130)
