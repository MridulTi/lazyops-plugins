#!/usr/bin/env python3
"""
Install Qualys Cloud Agent and/or Palo Alto Cortex (Traps) from local files over SSH (SCP).

One config file, shared IP discovery (static or EC2 tag filters), shared SSH keys/users.

  INSTALL_AGENTS="both"              # default: cortex + qualys
  INSTALL_AGENTS="cortex"
  INSTALL_AGENTS="qualys"
  INSTALL_AGENTS="remove_qualys"   # uninstall Qualys only (purge package + wipe agent dirs)
  INSTALL_AGENTS="remove_cortex"   # uninstall Cortex XDR (Traps) only — stop services, purge RPM/DEB, wipe /opt/traps, config
  INSTALL_AGENTS="cortex,remove_qualys"   # remove Qualys entirely, then install Cortex
  INSTALL_AGENTS="remove_cortex,cortex"    # purge old agent, then install from LOCAL_CORTEX_*
  INSTALL_AGENTS="install_agent"            # only cytool reconnect force (agent already installed); uses CORTEX_FORCE_DISTRIBUTION_ID or default 32-hex id
  INSTALL_AGENTS="cortex,install_agent"     # full Cortex install, then reconnect force again at end

Comma-separated tokens are allowed. Order on each host:
  remove_qualys → remove_cortex → cortex (install) → install_agent (reconnect force only) → qualys (install).

Cortex: LOCAL_CORTEX_TAR or LOCAL_CORTEX_DIR (same rules as install_cortex_from_local.py).
Qualys: LOCAL_QUALYS_PKG or LOCAL_QUALYS_DIR + ACTIVATION_ID, CUSTOMER_ID, SERVER_URI
        (same rules as install_qualys_from_local.py).

Common: SSH_KEY_DIR, IPS_MODE, IPS / IPS_FILE, TAG_FILTERS / INSTANCE_FILTERS, AWS_REGION (tag mode),
        ACCOUNT_ROLES / ROLE_ARN (multi-account + tag only), REMOTE_TMP_PARENT, optional --yes|-y.

Cortex post-install: /opt/traps/bin/cytool reconnect force uses CORTEX_FORCE_DISTRIBUTION_ID if set, else default
        954b23c390ac4f04b7c05152743b6dda (32 hex, optional dashes in override).

Parallel hosts (optional): MAX_PARALLEL_HOSTS=8 (or PARALLEL_HOSTS) — run up to N installs at once (default 8,
capped by host count and 64). Logs from different IPs may interleave; use 1 for strictly serial output.

Usage:
  python3 install_qualys_cortex_from_local.py <config_file> [--yes|-y]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

SSH_USERS = ("ec2-user", "ubuntu", "centos")

CORTEX_SERVICE = "traps_pmd"
QUALYS_SERVICE = "qualys-cloud-agent"
QUALYS_AGENT_BIN = "/usr/local/qualys/cloud-agent/bin/qualys-cloud-agent.sh"

ALLOWED_DISTRIBUTION_IDS: frozenset[str] = frozenset(
    {
        "316381f645b64126a42cd2291c92582d",
        "61de7ac112ec4b3c88a7df55a818523f",
        "6eeacbdf70cd4f01b5df319f5b21abba",
        "1d69f9c8505c4b1880fc9084eeb87a13",
        "6172bc00438f4e659a59ac66452df826",
        "77fdbb63b9864d74bd608c9a174a682a",
        "4fa66050b827478db5af8530140855d0",
        "954b23c390ac4f04b7c05152743b6dda",
    }
)

# Default Distribution ID for `cytool reconnect force` after install (override with CORTEX_FORCE_DISTRIBUTION_ID in config).
CORTEX_DEFAULT_RECONNECT_DISTRIBUTION_ID = "954b23c390ac4f04b7c05152743b6dda"

REQUIRED_LOCAL = ("SSH_KEY_DIR",)
REQUIRED_TAG = ("AWS_REGION",)

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

AGENT_UBUNTU_X64 = "Qualys_Linux_X64.deb"
AGENT_UBUNTU_ARM = "Qualys_Linux_ARM64.deb"
AGENT_AMZN_X64 = "Qualys_Linux_X64.rpm"
AGENT_AMZN_ARM = "Qualys_Linux_ARM64.rpm"


def get_install_agents(config: dict) -> frozenset[str]:
    raw = (config.get("INSTALL_AGENTS") or "both").strip().lower()
    parts = [p.strip() for p in re.split(r"[\s,+]+", raw) if p.strip()]
    if not parts:
        parts = ["both"]
    out: set[str] = set()
    for p in parts:
        if p in ("both", "all", "cortex+qualys", "cortex_and_qualys"):
            out.update(("cortex", "qualys"))
        elif p == "cortex":
            out.add("cortex")
        elif p in ("qualys", "qualys_cloud", "qualys-cloud"):
            out.add("qualys")
        elif p in ("remove_qualys", "uninstall_qualys", "qualys_remove", "no_qualys"):
            out.add("qualys_remove")
        elif p in ("remove_cortex", "uninstall_cortex", "cortex_remove", "no_cortex"):
            out.add("cortex_remove")
        elif p in (
            "install_agent",
            "cortex_reconnect",
            "cortex_reconnect_only",
            "reconnect_cortex",
            "cortex_reconnect_force",
            "cortex_install_agent",
        ):
            out.add("cortex_reconnect_only")
        else:
            print(f"❌ Invalid INSTALL_AGENTS token {p!r} in {config.get('INSTALL_AGENTS')!r}")
            print(
                "   Allowed: both, cortex, qualys, remove_qualys, remove_cortex, install_agent "
                "(and comma-separated combinations)."
            )
            sys.exit(1)
    return frozenset(out)


def load_config(path: str) -> dict:
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


def validate_cortex_bundle(config: dict) -> None:
    tar_s = (config.get("LOCAL_CORTEX_TAR") or "").strip()
    dir_s = (config.get("LOCAL_CORTEX_DIR") or "").strip()
    if tar_s and dir_s:
        print("❌ Set only one of LOCAL_CORTEX_TAR or LOCAL_CORTEX_DIR")
        sys.exit(1)
    if not tar_s and not dir_s:
        print("❌ Cortex enabled: set LOCAL_CORTEX_TAR or LOCAL_CORTEX_DIR")
        sys.exit(1)
    if tar_s:
        tar = Path(tar_s).expanduser()
        if not tar.is_file():
            print(f"❌ LOCAL_CORTEX_TAR not a file: {tar}")
            sys.exit(1)
        if not str(tar.name).lower().endswith((".tar.gz", ".tgz")):
            print("❌ LOCAL_CORTEX_TAR should be a .tar.gz (or .tgz)")
            sys.exit(1)
    else:
        root = Path(dir_s).expanduser()
        if not root.is_dir():
            print(f"❌ LOCAL_CORTEX_DIR is not a directory: {root}")
            sys.exit(1)
        bundles = sorted(root.glob("*.tar.gz")) + sorted(root.glob("*.tgz"))
        if not bundles:
            print(f"❌ LOCAL_CORTEX_DIR has no .tar.gz or .tgz: {root}")
            sys.exit(1)


def validate_qualys_bundle(config: dict) -> None:
    pkg_s = (config.get("LOCAL_QUALYS_PKG") or "").strip()
    dir_s = (config.get("LOCAL_QUALYS_DIR") or "").strip()
    if pkg_s and dir_s:
        print("❌ Set only one of LOCAL_QUALYS_PKG or LOCAL_QUALYS_DIR")
        sys.exit(1)
    if not pkg_s and not dir_s:
        print("❌ Qualys enabled: set LOCAL_QUALYS_PKG or LOCAL_QUALYS_DIR")
        sys.exit(1)
    if pkg_s:
        p = Path(pkg_s).expanduser()
        if not p.is_file():
            print(f"❌ LOCAL_QUALYS_PKG not a file: {p}")
            sys.exit(1)
        low = p.name.lower()
        if not (low.endswith(".rpm") or low.endswith(".deb")):
            print("❌ LOCAL_QUALYS_PKG must be .rpm or .deb")
            sys.exit(1)
    else:
        root = Path(dir_s).expanduser()
        if not root.is_dir():
            print(f"❌ LOCAL_QUALYS_DIR is not a directory: {root}")
            sys.exit(1)
        if not list(root.glob("*.rpm")) and not list(root.glob("*.deb")):
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


def pick_tarball_from_dir(directory: Path, os_id: str, arch: str) -> tuple[Path | None, str]:
    pkg = classify_pkg_family(os_id)
    if not pkg:
        return None, f"unsupported OS ID={os_id!r}"
    cpu = classify_cpu_family(arch)
    if not cpu:
        return None, f"unsupported REMOTE_ARCH={arch!r}"
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
        return None, f"no Cortex tarball for OS={os_id!r} arch={arch!r}"
    chosen = max(matches, key=lambda x: x.stat().st_mtime)
    return chosen, f"{chosen.name} for OS={os_id!r} arch={arch!r} ({pkg}, {cpu})"


def _filename_suggests_arm(name: str) -> bool:
    n = name.lower()
    return "aarch64" in n or "arm64" in n or "_arm" in n or "linux_arm" in n


def _filename_suggests_x64(name: str) -> bool:
    n = name.lower()
    if _filename_suggests_arm(n):
        return False
    return "x64" in n or "x86_64" in n or "amd64" in n or "linux_x" in n or "cloudagent" in n


def pick_package_from_dir(directory: Path, os_id: str, arch: str) -> tuple[Path | None, str]:
    pkg = classify_pkg_family(os_id)
    if not pkg:
        return None, f"unsupported OS ID={os_id!r}"
    cpu = classify_cpu_family(arch)
    if not cpu:
        return None, f"unsupported arch={arch!r}"

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
        return None, f"no matching *{suffix} for OS={os_id!r} arch={arch!r}"
    chosen = max(candidates, key=lambda x: x.stat().st_mtime)
    return chosen, f"{chosen.name} (newest match)"


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


def resolve_local_tarball(config: dict, ip: str, user: str, key_path: Path) -> tuple[Path | None, str]:
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


def local_pkg_kind(path: Path) -> str:
    low = path.name.lower()
    if low.endswith(".deb"):
        return "deb"
    if low.endswith(".rpm"):
        return "rpm"
    return "unknown"


def resolve_local_qualys_package(config: dict, ip: str, user: str, key_path: Path) -> tuple[Path | None, str, str | None]:
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
    agents = get_install_agents(config)

    for v in REQUIRED_LOCAL:
        if not config.get(v):
            print(f"❌ Missing in config: {v}")
            sys.exit(1)

    if "cortex" in agents:
        validate_cortex_bundle(config)
    if "qualys" in agents:
        for v in ("ACTIVATION_ID", "CUSTOMER_ID", "SERVER_URI"):
            if not str(config.get(v) or "").strip():
                print(f"❌ Qualys install enabled: missing {v}")
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

    name = session_name or f"install_agents_local_{int(time.time())}"
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
            print("❌ IPS_MODE=static produced no IPs")
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


PROBE_SCRIPT = "echo AGENT_ROLLOUT_PROBE_OK"


def check_ssh_connection(config: dict, ip: str) -> tuple[bool, str | None, str | None]:
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    for user in SSH_USERS:
        for key in keys:
            rc, out, _ = ssh_run(ip, user, key, PROBE_SCRIPT, timeout=25, use_tt=False)
            if rc == 0 and (out or "").strip() == "AGENT_ROLLOUT_PROBE_OK":
                return True, user, key.name
    return False, None, None


# --- Cortex ---


def extract_distribution_ids(text: str) -> set[str]:
    if not text:
        return set()
    norm = text.lower()
    found = set(re.findall(r"\b[0-9a-f]{32}\b", norm))
    for m in re.finditer(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", norm, re.I):
        compact = m.group(0).replace("-", "").lower()
        if len(compact) == 32:
            found.add(compact)
    return found


def connectivity_distribution_ok(text: str) -> tuple[bool, str]:
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


def cortex_reconnect_distribution_id_for_install(config: dict) -> str:
    raw = (config.get("CORTEX_FORCE_DISTRIBUTION_ID") or CORTEX_DEFAULT_RECONNECT_DISTRIBUTION_ID).strip()
    comp = raw.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", comp):
        raise ValueError(
            f"Invalid distribution id for cytool reconnect force: {raw!r} "
            "(set CORTEX_FORCE_DISTRIBUTION_ID to 32 hex chars, dashes optional)"
        )
    return comp


def build_cortex_reconnect_only_script(distribution_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", distribution_id):
        raise ValueError("distribution_id must be 32 lowercase hex chars")
    dist = distribution_id
    return f"""
set -e
DIST="{dist}"
echo "CORTEX_INSTALL_AGENT_START" >&2
sudo -n true 2>/dev/null || {{ echo "CORTEX_RECONNECT_ERROR: sudo requires password" >&2; exit 1; }}
CYTOOL_BIN="/opt/traps/bin/cytool"
[[ -z "$CYTOOL_BIN" ]] && {{ echo "CORTEX_RECONNECT_ERROR: cytool not found (install Cortex agent first)" >&2; exit 1; }}
echo "CORTEX_RECONNECT_ONLY: sudo $CYTOOL_BIN reconnect force $DIST" >&2
sudo -n "$CYTOOL_BIN" reconnect force "$DIST" || {{ echo "CORTEX_RECONNECT_ERROR: reconnect force failed" >&2; exit 1; }}
echo "CORTEX_INSTALL_AGENT_DONE"
"""


def process_cortex_reconnect_only_on_host(config: dict, ip: str, user: str, key_path: Path) -> bool:
    """Run cytool reconnect force only (no tarball). Agent must already be on the host."""
    try:
        dist = cortex_reconnect_distribution_id_for_install(config)
    except ValueError as e:
        print(f"❌ Cortex install_agent: {e}")
        return False
    print(f"   ℹ️ Cortex install_agent: cytool reconnect force with distribution id {dist}")
    try:
        body = build_cortex_reconnect_only_script(dist)
    except ValueError as e:
        print(f"❌ Cortex install_agent: {e}")
        return False
    rc, out, err = ssh_run(ip, user, key_path, body, timeout=120, login_shell=True)
    combined = f"{out}\n{err}".strip()
    for line in combined.splitlines()[-25:]:
        print(f"      {line}")
    if rc != 0 or "CORTEX_RECONNECT_ERROR" in combined:
        print(f"❌ Cortex install_agent failed (rc={rc})")
        return False
    if "CORTEX_INSTALL_AGENT_DONE" not in combined:
        print("   ⚠️ Cortex install_agent: CORTEX_INSTALL_AGENT_DONE marker missing — review output")
    print("   🟢 Cortex install_agent: reconnect force completed")
    return True


CORTEX_CHECK_SCRIPT = r"""
set -e
SERVICE="traps_pmd"
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  echo "TRAPS_PMD_STATUS=RUNNING"
else
  echo "TRAPS_PMD_STATUS=NOT_RUNNING"
  exit 0
fi
CYTOOL=/opt/traps/bin/cytool
echo "CYTOOL_STATUS=FOUND:$CYTOOL"
# cytool -> pmd uses IPC. Error 107 = ENOTCONN (endpoint not connected): pmd still starting, overlapping
# connectivity_test, or broken agent. Try both subcommands; capture+print output for allowlist parsing.
for attempt in 1 2 3 4 5; do
  out_chk=$(sudo -n "$CYTOOL" connectivity_test 2>&1) || true
  echo "$out_chk"
  echo "$out_chk" | grep -qE '[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' && break
  out_tst=$(sudo -n "$CYTOOL" connectivity_test 2>&1) || true
  echo "$out_tst"
  echo "$out_tst" | grep -qE '[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' && break
  echo "CORTEX_NOTE: connectivity attempt $attempt: no Distribution ID in output; retry in 5s (IPC 107 / test in progress)" >&2
  [[ "$attempt" -lt 5 ]] && sleep 5
done
"""


CORTEX_REMOVE_SCRIPT = rf"""
set -e
SERVICE="{CORTEX_SERVICE}"
echo "CORTEX_REMOVE_START" >&2
sudo -n true 2>/dev/null || {{ echo "CORTEX_REMOVE_ERROR: sudo requires password" >&2; exit 1; }}

for u in traps_pmd traps_fwctl traps_fileinfo traps_report traps_updater traps_agent; do
  sudo systemctl stop "$u" 2>/dev/null || true
  sudo systemctl disable "$u" 2>/dev/null || true
  sudo systemctl reset-failed "$u" 2>/dev/null || true
done

if command -v dpkg &>/dev/null; then
  for p in cortex-agent traps-agent panw-cortex-agent; do
    sudo apt-get -y purge "$p" 2>/dev/null || true
    sudo dpkg --purge "$p" 2>/dev/null || true
  done
fi
if command -v rpm &>/dev/null; then
  for p in cortex-agent traps-agent panw-cortex-agent; do
    sudo rpm -e --nodeps "$p" 2>/dev/null || true
  done
  for p in $(rpm -qa 2>/dev/null | grep -iE 'cortex-agent|traps-agent|panw-cortex|^traps_' || true); do
    [[ -n "$p" ]] && sudo rpm -e --nodeps "$p" 2>/dev/null || true
  done
fi

sudo rm -rf /opt/traps /etc/traps /var/log/traps /etc/cortex /etc/panw 2>/dev/null || true
sudo rm -rf /etc/systemd/system/traps_pmd.service.d 2>/dev/null || true
sudo rm -f /etc/systemd/system/multi-user.target.wants/traps_pmd.service 2>/dev/null || true
sudo rm -f /lib/systemd/system/traps_pmd.service /usr/lib/systemd/system/traps_pmd.service 2>/dev/null || true
for u in traps_pmd traps_fwctl traps_fileinfo traps_report traps_updater traps_agent; do
  sudo rm -f /etc/systemd/system/multi-user.target.wants/"$u".service 2>/dev/null || true
  sudo rm -f /lib/systemd/system/"$u".service /usr/lib/systemd/system/"$u".service 2>/dev/null || true
done

sudo systemctl daemon-reload 2>/dev/null || true
echo "CORTEX_REMOVE_DONE"
"""


def process_cortex_remove_on_host(config: dict, ip: str, user: str, key_path: Path) -> bool:
    print("   🗑️ Cortex: stopping services, purging packages, removing /opt/traps and config...")
    rc, out, err = ssh_run(ip, user, key_path, CORTEX_REMOVE_SCRIPT, timeout=300, login_shell=True)
    combined = f"{out}\n{err}".strip()
    for line in combined.splitlines()[-35:]:
        print(f"      {line}")
    if rc != 0 or "CORTEX_REMOVE_ERROR" in combined:
        print(f"❌ Cortex: removal failed (rc={rc})")
        return False
    if "CORTEX_REMOVE_DONE" not in combined:
        print("   ⚠️ Cortex: removal finished but CORTEX_REMOVE_DONE marker missing — review output")
    print("   🟢 Cortex: removal completed")
    return True


def build_cortex_install_script(tar_basename: str, remote_dir: str, reconnect_distribution_id: str) -> str:
    if re.search(r"[^A-Za-z0-9._+-]", tar_basename):
        raise ValueError("Unsafe tar basename; use only letters, digits, . _ + -")
    if not re.fullmatch(r"[0-9a-f]{32}", reconnect_distribution_id):
        raise ValueError("reconnect_distribution_id must be 32 lowercase hex chars")
    tb = tar_basename
    rd = remote_dir.replace("$", "\\$")
    svc = CORTEX_SERVICE
    dist = reconnect_distribution_id
    return f"""
set -e
echo "CORTEX_LOCAL_INSTALL_START" >&2
SERVICE="{svc}"
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
  [[ -n "$PKGNAME" ]] && sudo apt-get -y purge "$PKGNAME" 2>/dev/null || true
  [[ -n "$PKGNAME" ]] && sudo dpkg --purge "$PKGNAME" 2>/dev/null || true
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

CYTOOL_BIN=/opt/traps/bin/cytool
CONF_PATH=/etc/cortex/cortex.conf
[[ -f "$CONF_PATH" ]] || {{ echo "CORTEX_ERROR: missing $CONF_PATH" >&2; exit 1; }}
# Distribution ID for reconnect force (from config CORTEX_FORCE_DISTRIBUTION_ID or built-in default)
DIST_RAW="{dist}"
echo "CORTEX_RECONNECT: sudo $CYTOOL_BIN reconnect force $DIST_RAW" >&2
sudo -n "$CYTOOL_BIN" reconnect force "$DIST_RAW" || true

echo "CORTEX_LOCAL_INSTALL_DONE"
"""


def process_cortex_on_host(config: dict, ip: str, user: str, key_path: Path) -> bool:
    rc, check_out, check_err = ssh_run(ip, user, key_path, CORTEX_CHECK_SCRIPT, timeout=120, login_shell=True)
    combined = f"{check_out}\n{check_err}".strip()
    print("   --- Cortex: traps_pmd / cytool (pre) ---")
    for line in combined.splitlines()[:40]:
        print(f"      {line}")
    if len(combined.splitlines()) > 40:
        print("      ... (truncated)")

    running = "TRAPS_PMD_STATUS=RUNNING" in combined
    need_install = True
    if running:
        ok_dist, reason = connectivity_distribution_ok(combined)
        if ok_dist:
            print(f"   ✅ Cortex: skip reinstall ({reason})")
            need_install = False
        else:
            print(f"   ⚠️ Cortex: reinstall required: {reason}")
    else:
        print("   ℹ️ Cortex: traps_pmd not active — installing from tarball")

    if not need_install:
        return True

    local_tar, pick_reason = resolve_local_tarball(config, ip, user, key_path)
    if local_tar is None:
        print(f"❌ Cortex: could not resolve bundle: {pick_reason}")
        return False
    print(f"   📦 Cortex: {pick_reason}")

    parent = (config.get("REMOTE_TMP_PARENT") or "/tmp").strip().rstrip("/")
    remote_uid = uuid.uuid4().hex[:12]
    remote_dir = f"{parent}/cortex_local_{remote_uid}"
    remote_tar_path = f"{remote_dir}/{local_tar.name}"

    prep_rc, _, prep_err = ssh_run(ip, user, key_path, f"mkdir -p {remote_dir} && chmod 755 {remote_dir}\n", timeout=30)
    if prep_rc != 0:
        print(f"❌ Cortex: could not create remote dir: {prep_err}")
        return False

    print(f"   SCP {local_tar.name} -> {ip}:{remote_tar_path}")
    scp_rc, _, scp_err = scp_file(local_tar, ip, user, key_path, remote_tar_path)
    if scp_rc != 0:
        print(f"❌ Cortex: SCP failed: {scp_err}")
        return False

    try:
        rid = cortex_reconnect_distribution_id_for_install(config)
        install_body = build_cortex_install_script(local_tar.name, remote_dir, rid)
        print(f"   ℹ️ Cortex: cytool reconnect force will use distribution id {rid}")
    except ValueError as e:
        print(f"❌ Cortex: {e}")
        return False

    print("   🔧 Cortex: installing...")
    in_rc, in_out, in_err = ssh_run(ip, user, key_path, install_body, timeout=420, login_shell=True)
    install_log = f"{in_out}\n{in_err}".strip()
    if in_rc != 0 or "CORTEX_ERROR" in install_log:
        print(f"❌ Cortex: install failed (rc={in_rc})")
        for ln in install_log.splitlines()[-25:]:
            print(f"      {ln}")
        return False
    print("   🟢 Cortex: install completed")

    rc2, post_out, post_err = ssh_run(ip, user, key_path, CORTEX_CHECK_SCRIPT, timeout=120, login_shell=True)
    post_combined = f"{post_out}\n{post_err}".strip()
    print("   --- Cortex: cytool (post) ---")
    for line in post_combined.splitlines():
        print(f"      {line}")
    ok_after, reason_after = connectivity_distribution_ok(post_combined)
    if ok_after:
        print(f"   ✅ Cortex post-check: {reason_after}")
    else:
        print(f"   ⚠️ Cortex post-check: {reason_after}")
    return True


# --- Qualys ---


QUALYS_CHECK_SCRIPT = f"""
AGENT_BIN="{QUALYS_AGENT_BIN}"
SERVICE="{QUALYS_SERVICE}"
if [[ ! -x "$AGENT_BIN" ]]; then echo "QUALYS_NOT_INSTALLED"; exit 1; fi
if systemctl is-failed --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_FAILED"; exit 1; fi
if ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then echo "QUALYS_SERVICE_INACTIVE"; exit 1; fi
echo "QUALYS_OK"; exit 0
"""


QUALYS_REMOVE_SCRIPT = rf"""
set -e
SERVICE="{QUALYS_SERVICE}"
echo "QUALYS_REMOVE_START" >&2
sudo -n true 2>/dev/null || {{ echo "QUALYS_REMOVE_ERROR: sudo requires password" >&2; exit 1; }}

sudo systemctl stop "$SERVICE" 2>/dev/null || true
sudo systemctl disable "$SERVICE" 2>/dev/null || true
sudo systemctl reset-failed "$SERVICE" 2>/dev/null || true

if command -v dpkg &>/dev/null; then
  sudo apt-get -y purge qualys-cloud-agent 2>/dev/null || true
  sudo dpkg --purge qualys-cloud-agent 2>/dev/null || true
fi
if command -v rpm &>/dev/null; then
  sudo rpm -e --nodeps qualys-cloud-agent 2>/dev/null || true
  sudo dnf remove -y qualys-cloud-agent 2>/dev/null || true
  sudo yum remove -y qualys-cloud-agent 2>/dev/null || true
fi

# Residual paths (Linux cloud agent typical layout)
sudo rm -rf /usr/local/qualys /etc/qualys /var/log/qualys /opt/qualys /var/qualys 2>/dev/null || true
sudo rm -f /etc/systemd/system/multi-user.target.wants/"$SERVICE".service 2>/dev/null || true
sudo rm -f /lib/systemd/system/"$SERVICE".service /usr/lib/systemd/system/"$SERVICE".service 2>/dev/null || true

sudo systemctl daemon-reload 2>/dev/null || true
echo "QUALYS_REMOVE_DONE"
"""


def qualys_status_from_check(combined: str) -> str:
    for line in (combined or "").splitlines():
        line = line.strip()
        if line.startswith("QUALYS_"):
            return line
    return ""


def process_qualys_remove_on_host(config: dict, ip: str, user: str, key_path: Path) -> bool:
    print("   🗑️ Qualys: purging package and agent directories...")
    rc, out, err = ssh_run(ip, user, key_path, QUALYS_REMOVE_SCRIPT, timeout=300, login_shell=True)
    combined = f"{out}\n{err}".strip()
    for line in combined.splitlines()[-30:]:
        print(f"      {line}")
    if rc != 0 or "QUALYS_REMOVE_ERROR" in combined:
        print(f"❌ Qualys: removal failed (rc={rc})")
        return False
    if "QUALYS_REMOVE_DONE" not in combined:
        print("   ⚠️ Qualys: removal finished but QUALYS_REMOVE_DONE marker missing — review output")
    print("   🟢 Qualys: removal completed")
    return True


def build_qualys_install_script(pkg_basename: str, remote_dir: str, install_kind: str) -> str:
    if re.search(r"[^A-Za-z0-9._+-]", pkg_basename):
        raise ValueError("Unsafe package basename; use only letters, digits, . _ + -")
    pb = pkg_basename
    rd = remote_dir.replace("$", "\\$")
    ik = install_kind
    svc = QUALYS_SERVICE
    abin = QUALYS_AGENT_BIN
    return f"""
set -e
echo "QUALYS_LOCAL_INSTALL_START" >&2
SERVICE="{svc}"
AGENT_BIN="{abin}"
TMP="{rd}"
PKG="{pb}"
KIND="{ik}"
sudo -n true 2>/dev/null || {{ echo "QUALYS_ERROR: sudo requires password" >&2; exit 1; }}
mkdir -p "$TMP"
cd "$TMP"
[[ -f "$PKG" ]] || {{ echo "QUALYS_ERROR: missing package $TMP/$PKG" >&2; exit 1; }}

sudo systemctl stop "$SERVICE" 2>/dev/null || true
sudo systemctl disable "$SERVICE" 2>/dev/null || true
if command -v rpm &>/dev/null; then
  sudo rpm -e --nodeps qualys-cloud-agent 2>/dev/null || true
fi
if command -v dpkg &>/dev/null; then
  sudo apt-get -y purge qualys-cloud-agent 2>/dev/null || true
  sudo dpkg --purge qualys-cloud-agent 2>/dev/null || true
fi
sudo rm -rf /usr/local/qualys /etc/qualys /var/log/qualys /opt/qualys /var/qualys 2>/dev/null || true

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


def process_qualys_on_host(config: dict, ip: str, user: str, key_path: Path) -> bool:
    rc0, check_out, check_err = ssh_run(ip, user, key_path, QUALYS_CHECK_SCRIPT, timeout=60, login_shell=True)
    combined_pre = f"{check_out}\n{check_err}".strip()
    print("   --- Qualys: pre-check ---")
    for line in combined_pre.splitlines()[:15]:
        print(f"      {line}")
    if qualys_status_from_check(combined_pre) == "QUALYS_OK":
        print("   ✅ Qualys: healthy — skipping install")
        return True

    local_pkg, pick_reason, _fixed_kind = resolve_local_qualys_package(config, ip, user, key_path)
    if local_pkg is None:
        print(f"❌ Qualys: could not resolve package: {pick_reason}")
        return False

    rc_probe, out_probe, err_probe = ssh_run(ip, user, key_path, REMOTE_OS_PROBE, timeout=30, login_shell=True)
    os_id, arch = parse_remote_os_probe(f"{out_probe}\n{err_probe}".strip())
    remote_family = classify_pkg_family(os_id)
    pkg_kind = local_pkg_kind(local_pkg)
    if remote_family and pkg_kind != remote_family:
        print(f"❌ Qualys: package {pkg_kind} does not match OS family {remote_family} (ID={os_id!r})")
        return False

    print(f"   📦 Qualys: {pick_reason}")

    parent = (config.get("REMOTE_TMP_PARENT") or "/tmp").strip().rstrip("/")
    remote_uid = uuid.uuid4().hex[:12]
    remote_dir = f"{parent}/qualys_local_{remote_uid}"
    remote_pkg_path = f"{remote_dir}/{local_pkg.name}"

    prep_rc, _, prep_err = ssh_run(ip, user, key_path, f"mkdir -p {remote_dir} && chmod 755 {remote_dir}\n", timeout=30)
    if prep_rc != 0:
        print(f"❌ Qualys: could not create remote dir: {prep_err}")
        return False

    print(f"   SCP {local_pkg.name} -> {ip}:{remote_pkg_path}")
    scp_rc, _, scp_err = scp_file(local_pkg, ip, user, key_path, remote_pkg_path)
    if scp_rc != 0:
        print(f"❌ Qualys: SCP failed: {scp_err}")
        return False

    aid = str(config["ACTIVATION_ID"]).strip()
    cid = str(config["CUSTOMER_ID"]).strip()
    uri = str(config["SERVER_URI"]).strip()
    try:
        body_template = build_qualys_install_script(local_pkg.name, remote_dir, pkg_kind)
    except ValueError as e:
        print(f"❌ Qualys: {e}")
        return False

    install_body = (
        body_template.replace("___AID___", _shell_single_quote(aid))
        .replace("___CID___", _shell_single_quote(cid))
        .replace("___URI___", _shell_single_quote(uri))
    )

    print("   🔧 Qualys: installing...")
    in_rc, in_out, in_err = ssh_run(ip, user, key_path, install_body, timeout=420, login_shell=True)
    install_log = f"{in_out}\n{in_err}".strip()
    if in_rc != 0 or "QUALYS_ERROR" in install_log:
        print(f"❌ Qualys: install failed (rc={in_rc})")
        for ln in install_log.splitlines()[-25:]:
            print(f"      {ln}")
        return False
    print("   🟢 Qualys: install completed")

    rc2, post_out, post_err = ssh_run(ip, user, key_path, QUALYS_CHECK_SCRIPT, timeout=60, login_shell=True)
    post_combined = f"{post_out}\n{post_err}".strip()
    print("   --- Qualys: post-check ---")
    for line in post_combined.splitlines():
        print(f"      {line}")
    if qualys_status_from_check(post_combined) == "QUALYS_OK":
        print("   ✅ Qualys post-check: QUALYS_OK")
    else:
        print("   ⚠️ Qualys post-check: review output above")
    return True


def process_host(config: dict, ip: str, keys: list[Path], agents: frozenset[str]) -> bool:
    connected, user, key_name = check_ssh_connection(config, ip)
    if not connected or not user or not key_name:
        print(f"❌ Could not SSH to {ip}")
        return False
    key_path = next((k for k in keys if k.name == key_name), keys[0])
    print(f"   SSH: {user} @ {ip} (key {key_name})")

    ok = True
    if "qualys_remove" in agents:
        print("\n   ---------- Qualys (remove) ----------")
        ok = process_qualys_remove_on_host(config, ip, user, key_path) and ok
    if "cortex_remove" in agents:
        print("\n   ---------- Cortex (remove) ----------")
        ok = process_cortex_remove_on_host(config, ip, user, key_path) and ok
    if "cortex" in agents:
        print("\n   ---------- Cortex ----------")
        ok = process_cortex_on_host(config, ip, user, key_path) and ok
    if "cortex_reconnect_only" in agents:
        print("\n   ---------- Cortex (install_agent: reconnect force) ----------")
        ok = process_cortex_reconnect_only_on_host(config, ip, user, key_path) and ok
    if "qualys" in agents:
        print("\n   ---------- Qualys (install) ----------")
        ok = process_qualys_on_host(config, ip, user, key_path) and ok
    return ok


def _parallel_host_workers(config: dict, num_hosts: int) -> int:
    raw = str(config.get("MAX_PARALLEL_HOSTS") or config.get("PARALLEL_HOSTS") or "8").strip()
    try:
        w = int(raw)
    except ValueError:
        w = 8
    return max(1, min(w, num_hosts, 64))


def run_for_ips(config: dict, ips: list[str], agents: frozenset[str]) -> list[str]:
    keys = get_ssh_keys(config["SSH_KEY_DIR"])
    failed: list[str] = []
    workers = _parallel_host_workers(config, len(ips))
    if workers == 1 or len(ips) == 1:
        for ip in ips:
            print(f"\n🔍 {ip}")
            if not process_host(config, ip, keys, agents):
                failed.append(ip)
            print("--------------------------------------------")
        return failed

    print(f"\n⚡ Parallel mode: up to {workers} hosts at once (MAX_PARALLEL_HOSTS).", flush=True)
    io_lock = threading.Lock()
    fail_lock = threading.Lock()

    def run_one(ip: str) -> tuple[str, bool]:
        with io_lock:
            print(f"\n🔍 {ip}", flush=True)
        ok = process_host(config, ip, keys, agents)
        with io_lock:
            print(f"-------------------------------------------- [{ip}]", flush=True)
        return ip, ok

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_ip = {pool.submit(run_one, ip): ip for ip in ips}
        for fut in as_completed(future_to_ip):
            ip = future_to_ip[fut]
            try:
                _ip_ret, ok = fut.result()
            except Exception as exc:  # noqa: BLE001 — surface worker crash
                with fail_lock:
                    failed.append(ip)
                with io_lock:
                    print(f"❌ {ip}: worker crashed: {exc}", flush=True)
                continue
            if not ok:
                with fail_lock:
                    failed.append(ip)
    return failed


def main() -> None:
    argv = [a for a in sys.argv[1:] if a not in ("--yes", "-y")]
    auto_yes = len(argv) < len(sys.argv) - 1

    if not argv:
        print("Usage: install_qualys_cortex_from_local.py <config_file> [--yes|-y]")
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
    agents = get_install_agents(config)
    agent_label = ", ".join(sorted(agents))

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
                confirm = input(f"Run [{agent_label}] install on account {account_id}? (yes/no): ").strip().lower()
            else:
                confirm = "yes"
                print("   Proceeding (--yes).")
            clear_assumed_role_credentials()
            if confirm != "yes":
                print("   Skipped.")
                continue
            all_failed.extend(run_for_ips(config, ips, agents))
        failed = all_failed
    else:
        ips = get_ips(config)
        print(f"\n🧾 Targets ({len(ips)}): {', '.join(ips)}")
        print(f"   Agents: {agent_label}")
        if not auto_yes:
            confirm = input("Proceed with local install? (yes/no): ").strip().lower()
        else:
            confirm = "yes"
            print("Proceeding (--yes).")
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
        failed = run_for_ips(config, ips, agents)

    print()
    if failed:
        print(f"⚠️ Failed: {', '.join(failed)}")
        sys.exit(1)
    print("✅ Finished all targets")
    print("🏁 Local Qualys/Cortex rollout finished")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted (Ctrl+C)")
        sys.exit(130)
