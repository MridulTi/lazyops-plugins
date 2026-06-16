#!/usr/bin/env python3
"""
Install or update Cortex (Traps PMD) on servers using a local tarball (SCP), not S3.

Flow per host:
  1. SSH probe (same users/keys as install_cortex_from_s3.py).
  2. SCP the local .tar.gz to the server.
  3. If traps_pmd is already running, run ``cytool connectivity check`` and verify
     the Distribution ID is one of the allowed PPSL IDs (not Obsolete/EOL).
  4. If ID is wrong/missing/EOL, or agent not running: remove old package, install
     from the copied tarball (rpm/deb + cortex.conf), restart traps_pmd.
  5. After install, run ``cytool connectivity check`` again and print output.

Config file (bash-style, same parser as install_cortex_from_s3.py):
  - Exactly one of:
      LOCAL_CORTEX_TAR="/full/path/to/Linux_8_7_100_136016_CE_rpm.tar.gz"
    or
      LOCAL_CORTEX_DIR="/path/to/folder"   # contains *_rpm*.tar.gz / *_deb*.tar.gz (x86_64 vs aarch64 by filename)
    When LOCAL_CORTEX_DIR is set, the script SSH-probes each host (``/etc/os-release`` ID, ``uname -m``),
    then picks the newest matching tarball (rpm vs deb; aarch64/arm64 in name vs x86_64/amd64).
  - SSH_KEY_DIR; IPS_MODE=static with IPS="ip1 ip2" and/or IPS_FILE="/path/to/hosts.txt"
    (file-only is fine: one IP per line or space/comma separated; # starts a comment)
  - If IPS_FILE is set and IPS_MODE is omitted, static mode is used (unless TAG_FILTERS imply tag).
  - IPS_MODE=tag: AWS_REGION, TAG_FILTERS / INSTANCE_FILTERS, etc.
  - Optional: REMOTE_TMP_PARENT="/tmp"  (tar lands under a unique subdir)

Usage:
  python3 install_cortex_from_local.py <config_file>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path


# Reuse same SSH / service constants as install_cortex_from_s3.py
SSH_USERS = ("ec2-user", "ubuntu", "centos")
SERVICE = "traps_pmd"

# Allowed Distribution IDs (Linux/Windows) — PPSL-supported; Obsolete/EOL rows excluded.
ALLOWED_DISTRIBUTION_IDS: frozenset[str] = frozenset(
    {
        "316381f645b64126a42cd2291c92582d",  # 7.9.100.95444 CE Linux
        "61de7ac112ec4b3c88a7df55a818523f",  # 7.9.101.108405 CE Linux
        "6eeacbdf70cd4f01b5df319f5b21abba",  # 7.9.101.118742 CE Linux
        "1d69f9c8505c4b1880fc9084eeb87a13",  # 8.3.100.124671 CE Linux
        "6172bc00438f4e659a59ac66452df826",  # 8.3.101.130563 CE Linux
        "77fdbb63b9864d74bd608c9a174a682a",  # 8.3.101.53522 CE Windows
        "4fa66050b827478db5af8530140855d0",  # 8.7.100.12907 CE Windows
        "954b23c390ac4f04b7c05152743b6dda",  # 8.7.100.136016 CE Linux
    }
)

REQUIRED_LOCAL = ("SSH_KEY_DIR",)
REQUIRED_TAG = ("AWS_REGION",)

# ``/etc/os-release`` ID -> package family (same idea as install_cortex_from_s3 remote install).
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
    """Split file into IP tokens; ignore blank lines and # comments."""
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


def validate_cortex_bundle(config: dict) -> None:
    """Require exactly one of LOCAL_CORTEX_TAR (file) or LOCAL_CORTEX_DIR (directory of bundles)."""
    tar_s = (config.get("LOCAL_CORTEX_TAR") or "").strip()
    dir_s = (config.get("LOCAL_CORTEX_DIR") or "").strip()
    if tar_s and dir_s:
        print("❌ Set only one of LOCAL_CORTEX_TAR or LOCAL_CORTEX_DIR")
        sys.exit(1)
    if not tar_s and not dir_s:
        print("❌ Set LOCAL_CORTEX_TAR (single .tar.gz) or LOCAL_CORTEX_DIR (directory of OS/arch-specific bundles)")
        sys.exit(1)
    if tar_s:
        tar = Path(tar_s).expanduser()
        if not tar.is_file():
            print(f"❌ LOCAL_CORTEX_TAR not a file: {tar}")
            sys.exit(1)
        if not str(tar.name).lower().endswith((".tar.gz", ".tgz")):
            print("❌ LOCAL_CORTEX_TAR should be a .tar.gz (or .tgz) Cortex bundle")
            sys.exit(1)
    else:
        root = Path(dir_s).expanduser()
        if not root.is_dir():
            print(f"❌ LOCAL_CORTEX_DIR is not a directory: {root}")
            sys.exit(1)
        bundles = sorted(root.glob("*.tar.gz")) + sorted(root.glob("*.tgz"))
        if not bundles:
            print(f"❌ LOCAL_CORTEX_DIR has no .tar.gz or .tgz files: {root}")
            sys.exit(1)


def classify_pkg_family(os_id: str) -> str | None:
    """Map ``/etc/os-release`` ``ID=`` to ``rpm`` or ``deb``."""
    oid = (os_id or "unknown").strip().lower()
    if oid in RPM_OS_IDS or oid.startswith("amzn") or "rhel" in oid:
        return "rpm"
    if oid in DEB_OS_IDS:
        return "deb"
    return None


def classify_cpu_family(arch: str) -> str | None:
    """Map ``uname -m`` to ``x64`` or ``aarch64``."""
    ar = (arch or "").strip().lower()
    if ar in ("aarch64", "arm64"):
        return "aarch64"
    if ar in ("x86_64", "amd64"):
        return "x64"
    return None


def pick_tarball_from_dir(directory: Path, os_id: str, arch: str) -> tuple[Path | None, str]:
    """
    Choose the newest matching ``*.tar.gz`` / ``*.tgz`` under ``directory``.
    Names are expected to follow Palo Alto style: ``..._CE_rpm.tar.gz``, ``..._CE_rpm.aarch64.tar.gz``,
    ``..._CE_deb.tar.gz``, ``..._CE_deb.aarch64.tar.gz`` (``_rpm`` / ``_deb`` substrings).
    """
    pkg = classify_pkg_family(os_id)
    if not pkg:
        return None, f"unsupported or unknown OS ID={os_id!r} (expected rpm: amzn/rhel/… or deb: ubuntu/debian)"
    cpu = classify_cpu_family(arch)
    if not cpu:
        return None, f"unsupported REMOTE_ARCH={arch!r} (need x86_64, amd64, aarch64, or arm64)"
    want_rpm = pkg == "rpm"
    matches: list[Path] = []
    for p in directory.iterdir():
        if not p.is_file():
            continue
        n = p.name.lower()
        if not (n.endswith(".tar.gz") or n.endswith(".tgz")):
            continue
        if want_rpm:
            if "_rpm" not in n:
                continue
        else:
            if "_deb" not in n:
                continue
        is_arm_named = "aarch64" in n or "arm64" in n
        if cpu == "aarch64":
            if not is_arm_named:
                continue
        else:
            if is_arm_named:
                continue
        matches.append(p)

    if not matches:
        hint = "rpm+aarch64" if want_rpm and cpu == "aarch64" else "rpm+x86_64" if want_rpm else "deb+aarch64" if cpu == "aarch64" else "deb+x86_64"
        names = sorted(x.name for x in directory.iterdir() if x.is_file())[:30]
        extra = f" (showing up to 30 files: {names})" if names else ""
        return None, f"no tarball in directory for {hint} (REMOTE_OS_ID={os_id!r} REMOTE_ARCH={arch!r}){extra}"

    chosen = max(matches, key=lambda x: x.stat().st_mtime)
    reason = f"{chosen.name} for OS={os_id!r} arch={arch!r} ({pkg}, {cpu})"
    return chosen, reason


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


def resolve_local_tarball(config: dict, ip: str, user: str, key_path: Path) -> tuple[Path | None, str]:
    """
    Return (path, human reason). For LOCAL_CORTEX_DIR, SSH-probes the host to pick a bundle.
    """
    tar_s = (config.get("LOCAL_CORTEX_TAR") or "").strip()
    if tar_s:
        p = Path(tar_s).expanduser().resolve()
        if not p.is_file():
            return None, f"missing file {p}"
        return p, str(p)

    dir_s = (config.get("LOCAL_CORTEX_DIR") or "").strip()
    root = Path(dir_s).expanduser().resolve()
    rc, out, err = ssh_run(ip, user, key_path, REMOTE_OS_PROBE, timeout=30, login_shell=True)
    combined = f"{out}\n{err}".strip()
    if rc != 0:
        return None, f"remote OS probe failed (rc={rc}): {combined[:500]}"
    os_id, arch = parse_remote_os_probe(combined)
    picked, msg = pick_tarball_from_dir(root, os_id, arch)
    if picked is None:
        return None, msg
    return picked, msg


def validate_config(config: dict, multi_account: bool = False) -> None:
    for v in REQUIRED_LOCAL:
        if not config.get(v):
            print(f"❌ Missing in config: {v}")
            sys.exit(1)
    validate_cortex_bundle(config)

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

    name = session_name or f"install_cortex_local_{int(time.time())}"
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


PROBE_SCRIPT = "echo CORTEX_PROBE_OK"


def check_cortex_connection(config: dict, ip: str) -> tuple[bool, str | None, str | None]:
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for user in SSH_USERS:
        for key in keys:
            rc, out, _ = ssh_run(ip, user, key, PROBE_SCRIPT, timeout=25, use_tt=False)
            if rc == 0 and (out or "").strip() == "CORTEX_PROBE_OK":
                return True, user, key.name
    return False, None, None


def extract_distribution_ids(text: str) -> set[str]:
    """Pull 32-hex IDs from cytool output (with or without hyphens)."""
    if not text:
        return set()
    norm = text.lower()
    # 32 contiguous hex
    found = set(re.findall(r"\b[0-9a-f]{32}\b", norm))
    # UUID-style
    for m in re.finditer(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", norm, re.I):
        compact = m.group(0).replace("-", "").lower()
        if len(compact) == 32:
            found.add(compact)
    return found


def connectivity_distribution_ok(text: str) -> tuple[bool, str]:
    """
    Return (ok, reason). Not OK if Obsolete/EOL wording; OK if any allowed ID appears.
    """
    if not (text or "").strip():
        return False, "empty connectivity output"
    low = text.lower()
    if "obsolete version" in low or "eolagent" in low or "eol agent" in low:
        return False, "EOL/Obsolete marker in connectivity output"
    ids_found = extract_distribution_ids(text)
    if ids_found & ALLOWED_DISTRIBUTION_IDS:
        return True, f"allowed Distribution ID present: {ids_found & ALLOWED_DISTRIBUTION_IDS}"
    if ids_found:
        return False, f"Distribution ID(s) not in allowlist: {ids_found}"
    return False, "no Distribution ID matched in output (expected 32-hex allowlist)"


# Remote: detect traps_pmd running, run cytool connectivity check, print markers for parser
CHECK_SCRIPT = r"""
set -e
SERVICE="traps_pmd"
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  echo "TRAPS_PMD_STATUS=RUNNING"
else
  echo "TRAPS_PMD_STATUS=NOT_RUNNING"
  exit 0
fi
CYTOOL=""
if command -v cytool >/dev/null 2>&1; then CYTOOL=$(command -v cytool)
elif [[ -x /opt/traps/bin/cytool ]]; then CYTOOL=/opt/traps/bin/cytool
fi
if [[ -z "$CYTOOL" ]]; then
  echo "CYTOOL_STATUS=MISSING"
  exit 0
fi
echo "CYTOOL_STATUS=FOUND:$CYTOOL"
# Palo Alto cytool — correct subcommand is "connectivity" (not "connectivty")
sudo -n "$CYTOOL" connectivity check 2>&1 || true
"""


def build_install_script(tar_basename: str, remote_dir: str) -> str:
    """Install from tarball already at $remote_dir/$tar_basename (shell-escaped basename only)."""
    # tar_basename must not contain shell metacharacters — validated on client
    if re.search(r"[^A-Za-z0-9._+-]", tar_basename):
        raise ValueError("Unsafe tar basename; use only letters, digits, . _ + -")
    tb = tar_basename
    rd = remote_dir.replace("$", "\\$")
    return f"""
set -e
echo "CORTEX_LOCAL_INSTALL_START" >&2
SERVICE="{SERVICE}"
TMP="{rd}"
TAR="{tb}"
sudo -n true 2>/dev/null || {{ echo "CORTEX_ERROR: sudo requires password" >&2; exit 1; }}
mkdir -p "$TMP"
cd "$TMP"
[[ -f "$TAR" ]] || {{ echo "CORTEX_ERROR: missing tarball $TMP/$TAR" >&2; exit 1; }}

ARCH=`uname -m`
OS=`source /etc/os-release 2>/dev/null && echo "$ID" || echo "unknown"`

case "$OS" in
amzn|rhel|centos|rocky|almalinux)
  INSTALL_CMD="sudo rpm -Uvh --replacepkgs"
  ;;
ubuntu|debian)
  INSTALL_CMD="sudo dpkg -i"
  ;;
*)
  echo "CORTEX_ERROR: Unsupported OS: $OS" >&2
  exit 1
  ;;
esac

sudo tar -xzf "$TAR"
CONF=`ls *.conf 2>/dev/null | head -1`
[[ -z "$CONF" ]] && {{ echo "CORTEX_ERROR: No .conf in tarball" >&2; exit 1; }}
sudo mkdir -p /etc/cortex
sudo cp "$CONF" /etc/cortex/cortex.conf

PKG=`find . -maxdepth 1 -name '*.rpm' 2>/dev/null | head -1`
[[ -z "$PKG" ]] && PKG=`find . -maxdepth 1 -name '*.deb' 2>/dev/null | head -1`
[[ -z "$PKG" ]] && {{ echo "CORTEX_ERROR: No .rpm/.deb in tarball" >&2; exit 1; }}

sudo systemctl stop $SERVICE 2>/dev/null || true
case "$OS" in
amzn|rhel|centos|rocky|almalinux)
  PKGNAME=$(rpm -qp --queryformat '%{{NAME}}' "$PKG" 2>/dev/null) || true
  [[ -n "$PKGNAME" ]] && sudo rpm -e "$PKGNAME" --nodeps 2>/dev/null || true
  ;;
ubuntu|debian)
  PKGNAME=`dpkg -f "$PKG" Package 2>/dev/null` || true
  [[ -n "$PKGNAME" ]] && sudo dpkg -r "$PKGNAME" 2>/dev/null || true
  ;;
esac

case "$OS" in
amzn|rhel|centos|rocky|almalinux)
  if ! sudo dnf install -y openssl ca-certificates policycoreutils-python-utils selinux-policy-devel 2>/dev/null; then
    if ! sudo yum install -y openssl ca-certificates policycoreutils-python-utils selinux-policy-devel 2>/dev/null; then
      echo "CORTEX_ERROR: Failed to install prerequisites" >&2
      exit 1
    fi
  fi
  sudo dnf install -y policycoreutils-python 2>/dev/null || sudo yum install -y policycoreutils-python 2>/dev/null || true
  ;;
esac
sudo timeout 300 $INSTALL_CMD "$PKG" || {{ echo "CORTEX_ERROR: Package install failed" >&2; exit 1; }}

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
echo "CORTEX_LOCAL_INSTALL_DONE"
"""


def process_host(config: dict, ip: str, keys: list[Path]) -> bool:
    connected, user, key_name = check_cortex_connection(config, ip)
    if not connected or not user or not key_name:
        print(f"❌ Could not SSH to {ip}")
        return False
    key_path = next((k for k in keys if k.name == key_name), keys[0])
    print(f"   SSH: {user} @ {ip} (key {key_name})")

    rc, check_out, check_err = ssh_run(ip, user, key_path, CHECK_SCRIPT, timeout=120, login_shell=True)
    combined = f"{check_out}\n{check_err}".strip()
    print("   --- traps_pmd / cytool (pre) ---")
    for line in combined.splitlines()[:40]:
        print(f"      {line}")
    if len(combined.splitlines()) > 40:
        print("      ... (truncated)")

    running = "TRAPS_PMD_STATUS=RUNNING" in combined
    need_install = True
    if running:
        ok_dist, reason = connectivity_distribution_ok(combined)
        if ok_dist:
            print(f"   ✅ Distribution check OK — skipping reinstall ({reason})")
            need_install = False
        else:
            print(f"   ⚠️ Reinstall required: {reason}")
            need_install = True
    else:
        print("   ℹ️ traps_pmd not running (or not active) — installing from tarball")

    if not need_install:
        return True

    local_tar, pick_reason = resolve_local_tarball(config, ip, user, key_path)
    if local_tar is None:
        print(f"❌ Could not resolve local Cortex bundle: {pick_reason}")
        return False
    print(f"   📦 {pick_reason}")

    parent = (config.get("REMOTE_TMP_PARENT") or "/tmp").strip().rstrip("/")
    remote_uid = uuid.uuid4().hex[:12]
    remote_dir = f"{parent}/cortex_local_{remote_uid}"
    remote_tar_path = f"{remote_dir}/{local_tar.name}"

    prep_rc, _, prep_err = ssh_run(ip, user, key_path, f"mkdir -p {remote_dir} && chmod 755 {remote_dir}\n", timeout=30)
    if prep_rc != 0:
        print(f"❌ Could not create remote dir: {prep_err}")
        return False

    print(f"   SCP {local_tar.name} -> {ip}:{remote_tar_path}")
    scp_rc, _, scp_err = scp_file(local_tar, ip, user, key_path, remote_tar_path)
    if scp_rc != 0:
        print(f"❌ SCP failed: {scp_err}")
        return False

    try:
        install_body = build_install_script(local_tar.name, remote_dir)
    except ValueError as e:
        print(f"❌ {e}")
        return False

    print("   🔧 Running install (remove old package, install from SCP'd tar, restart traps_pmd)...")
    in_rc, in_out, in_err = ssh_run(ip, user, key_path, install_body, timeout=420, login_shell=True)
    install_log = f"{in_out}\n{in_err}".strip()
    if in_rc != 0 or "CORTEX_ERROR" in install_log:
        print(f"❌ Install failed (rc={in_rc})")
        for ln in install_log.splitlines()[-25:]:
            print(f"      {ln}")
        return False
    print("   🟢 Install completed")

    # Post-check
    rc2, post_out, post_err = ssh_run(ip, user, key_path, CHECK_SCRIPT, timeout=120, login_shell=True)
    post_combined = f"{post_out}\n{post_err}".strip()
    print("   --- cytool connectivity (post-install) ---")
    for line in post_combined.splitlines():
        print(f"      {line}")
    ok_after, reason_after = connectivity_distribution_ok(post_combined)
    if ok_after:
        print(f"   ✅ Post-check: {reason_after}")
    else:
        print(f"   ⚠️ Post-check: {reason_after} (review output above)")
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
    if len(sys.argv) < 2:
        print("Usage: install_cortex_from_local.py <config_file>")
        sys.exit(1)

    config_path = sys.argv[1]
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
            confirm = input(f"Run local Cortex install on account {account_id}? (yes/no): ").strip().lower()
            clear_assumed_role_credentials()
            if confirm != "yes":
                print("   Skipped.")
                continue
            all_failed.extend(run_for_ips(config, ips))
        failed = all_failed
    else:
        ips = get_ips(config)
        print(f"\n🧾 Targets ({len(ips)}): {', '.join(ips)}")
        confirm = input("Proceed with local tarball Cortex install? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
        failed = run_for_ips(config, ips)

    print()
    if failed:
        print(f"⚠️ Failed: {', '.join(failed)}")
        sys.exit(1)
    print("✅ Finished all targets")
    print("🏁 Local Cortex rollout finished")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted (Ctrl+C)")
        sys.exit(130)
