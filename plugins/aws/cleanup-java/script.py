import argparse
import logging
import os
import sys
from getpass import getpass

try:
    import paramiko
    # Suppress paramiko's SSH exception tracebacks (they log at ERROR before raising)
    for _name in ("paramiko", "paramiko.transport"):
        logging.getLogger(_name).setLevel(logging.CRITICAL)
except ImportError:
    print("Install paramiko: pip install paramiko")
    sys.exit(1)

# Optional Ed25519 support (paramiko >= 2.2)
try:
    from paramiko.ed25519key import Ed25519Key
    HAS_ED25519 = True
except ImportError:
    HAS_ED25519 = False

# Optional AWS EC2 discovery
try:
    import boto3
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

SSH_USERS = ["ec2-user", "ubuntu"]
DEFAULT_SSH_PORT = 22
DEFAULT_TIMEOUT = 15  # Banner read can be slow; increase if you see "Error reading SSH protocol banner"
EXEC_TIMEOUT = 30
KEY_EXTENSIONS = (".pem", ".key", "")  # "" = no extension
SERVERS_ENCODING = "utf-8"
SERVERS_COMMENT_PREFIX = "#"
# Never delete these paths or use them as a Java installation root to remove
PROTECTED_PREFIX = "/usr/bin"
PROTECTED_ROOTS = ("/", "/usr")


def parse_java_vendor(version_output):
    """Parse 'java -version' output to determine vendor. Returns vendor name or 'Unknown'."""
    if not version_output:
        return "Unknown"
    text = version_output.lower()
    if "not found" in text or "no such file" in text or "command not found" in text:
        return "Not installed"
    if "openjdk" in text:
        return "OpenJDK"
    if "oracle" in text or "java(tm)" in text or "java se" in text:
        return "Oracle"
    if "amazon corretto" in text or "corretto" in text:
        return "Corretto"
    if "eclipse" in text or "temurin" in text or "adoptium" in text:
        return "Eclipse Temurin"
    if "zulu" in text:
        return "Zulu"
    return "Unknown"


def parse_servers_file(path, encoding=SERVERS_ENCODING):
    """Read server list: strip lines, skip empty and comment lines, support host:port."""
    servers = []
    with open(path, encoding=encoding) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(SERVERS_COMMENT_PREFIX):
                continue
            # Optional: strip inline comments
            if SERVERS_COMMENT_PREFIX in line:
                line = line.split(SERVERS_COMMENT_PREFIX)[0].strip()
            servers.append(line)
    return servers


# Key name prefixes/patterns to try first (case-insensitive)
PRIORITY_KEY_PREFIXES = tuple(os.environ.get("PRIORITY_KEY_PREFIXES", "").split(",")) if os.environ.get("PRIORITY_KEY_PREFIXES") else ()


def _key_priority(name):
    """Return (0, name) for prioritized keys, (1, name) otherwise."""
    lower = name.lower()
    if any(lower.startswith(p) for p in PRIORITY_KEY_PREFIXES if p):
        return (0, name)
    return (1, name)


def get_key_files(key_dir, key_extensions=KEY_EXTENSIONS):
    """List key files only (by extension); optional PRIORITY_KEY_PREFIXES env sorts keys first."""
    key_files = []
    for name in os.listdir(key_dir):
        path = os.path.join(key_dir, name)
        if not os.path.isfile(path):
            continue
        if key_extensions and not any(name.endswith(ext) or (ext == "" and "." not in name) for ext in key_extensions):
            continue
        key_files.append(name)
    key_files.sort(key=_key_priority)
    return key_files


def load_private_key(key_path, password=None):
    """Try RSA then Ed25519. Returns key or None."""
    try:
        return paramiko.RSAKey.from_private_key_file(key_path, password=password)
    except Exception:
        pass
    if HAS_ED25519:
        try:
            return Ed25519Key.from_private_key_file(key_path, password=password)
        except Exception:
            pass
    return None


def try_connect(host, key_dir, port=DEFAULT_SSH_PORT, timeout=DEFAULT_TIMEOUT, key_password=None):
    """Connect via SSH; host can be 'host' or 'host:port' (port overrides argument)."""
    hostname, connect_port = host, port
    if ":" in host and host.rfind(":") > 0:
        parts = host.rsplit(":", 1)
        if parts[1].isdigit():
            hostname, connect_port = parts[0], int(parts[1])
    for user in SSH_USERS:
        for key_file in get_key_files(key_dir):
            key_path = os.path.join(key_dir, key_file)
            key = load_private_key(key_path, key_password)
            if key is None:
                continue
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    hostname=hostname,
                    port=connect_port,
                    username=user,
                    pkey=key,
                    timeout=timeout,
                    allow_agent=False,
                    look_for_keys=False,
                )
                print(f"Connected to {hostname}:{connect_port} using {user} and {key_file}")
                return ssh
            except (paramiko.SSHException, OSError, EOFError, TimeoutError):
                continue
    return None


def run_cmd(ssh, cmd, timeout=EXEC_TIMEOUT):
    """Run command and return combined stdout + stderr; optional timeout (seconds)."""
    stdin, stdout, stderr = ssh.exec_command(cmd)
    stdout.channel.settimeout(timeout)
    try:
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
    except OSError:
        out, err = "", "command timed out or failed"
    return out + err


def shell_quote(path):
    """Quote path for safe use in shell (spaces/special chars)."""
    if not path:
        return "''"
    return "'" + path.replace("'", "'\"'\"'") + "'"



def install_mlocate(ssh, run_cmd_fn):
    """Install mlocate (apt or yum/dnf) and updatedb."""
    run_cmd_fn(ssh, "which updatedb 2>/dev/null && sudo updatedb 2>/dev/null")
    out = run_cmd_fn(ssh, "which locate 2>/dev/null")
    if "locate" in out:
        return
    # Try apt (Debian/Ubuntu)
    run_cmd_fn(ssh, "sudo apt-get update -y 2>/dev/null && sudo apt-get install -y mlocate 2>/dev/null")
    out = run_cmd_fn(ssh, "which locate 2>/dev/null")
    if "locate" in out:
        run_cmd_fn(ssh, "sudo updatedb 2>/dev/null")
        return
    # Try yum/dnf (RHEL/CentOS/Amazon Linux)
    run_cmd_fn(ssh, "sudo yum install -y mlocate 2>/dev/null || sudo dnf install -y mlocate 2>/dev/null")
    run_cmd_fn(ssh, "sudo updatedb 2>/dev/null")


def find_java_bins_with_find(ssh, run_cmd_fn):
    """Fallback: find /usr -name java -path '*/bin/java' when locate unavailable."""
    out = run_cmd_fn(ssh, "sudo find /usr /opt -type f -path '*/bin/java' 2>/dev/null")
    paths = [p.strip() for p in out.splitlines() if p.strip()]
    return list(set(os.path.dirname(os.path.dirname(p)) for p in paths))


def get_ec2_instances(region=None, use_public_ip=False, filters=None):
    """Return list of host strings (IP or host:port) for running EC2 instances.
    Prefer private IP unless use_public_ip=True (e.g. running from outside VPC).
    filters: optional list of dicts, e.g. [{"Name": "tag:Environment", "Values": ["prod"]}].
    """
    if not HAS_BOTO3:
        print("Install boto3 for EC2 discovery: pip install boto3")
        return []
    client = boto3.client("ec2", region_name=region)
    base_filters = [{"Name": "instance-state-name", "Values": ["running"]}]
    if filters:
        base_filters.extend(filters)
    hosts = []
    paginator = client.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=base_filters):
        for res in page.get("Reservations", []):
            for inst in res.get("Instances", []):
                if use_public_ip and inst.get("PublicIpAddress"):
                    hosts.append(inst["PublicIpAddress"])
                elif inst.get("PrivateIpAddress"):
                    hosts.append(inst["PrivateIpAddress"])
                elif inst.get("PublicIpAddress"):
                    hosts.append(inst["PublicIpAddress"])
    return hosts


def main():
    parser = argparse.ArgumentParser(
        description="Find and remove Oracle Java installations on remote servers (SSH).",
        epilog="If 'ssh host' works but this script fails with 'Error reading SSH protocol banner', "
        "your CLI may use ProxyJump/bastion or ~/.ssh/config. This script connects directly to each host. "
        "Try --timeout 25 or run from a host that can reach the targets (e.g. bastion).",
    )
    parser.add_argument("key_dir", help="Directory containing SSH private keys (.pem, .key)")
    parser.add_argument(
        "servers_file",
        nargs="?",
        default=None,
        help="Path to file listing one host (or host:port) per line (omit when using --all-ec2)",
    )
    parser.add_argument("--all-ec2", action="store_true", help="Discover servers from AWS EC2 (all running instances in region) instead of a file")
    parser.add_argument("--region", default=None, help="AWS region for --all-ec2 (default: use AWS_REGION or AWS_DEFAULT_REGION env)")
    parser.add_argument("--ec2-public-ip", action="store_true", help="Use public IPs for EC2 instances (default: private IP; use when running outside VPC)")
    parser.add_argument("--dry-run", action="store_true", help="Only report; do not remove anything")
    parser.add_argument("--port", type=int, default=DEFAULT_SSH_PORT, help=f"SSH port (default {DEFAULT_SSH_PORT})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"SSH connect timeout in seconds (default {DEFAULT_TIMEOUT}); increase if you get 'Error reading SSH protocol banner'")
    parser.add_argument("--exec-timeout", type=int, default=EXEC_TIMEOUT, help=f"Per-command timeout in seconds (default {EXEC_TIMEOUT})")
    parser.add_argument("--skip-mlocate", action="store_true", help="Skip installing mlocate; use find only (slower)")
    parser.add_argument("--no-confirm", action="store_true", help="Skip confirmation prompt (use with --dry-run or at your own risk)")
    parser.add_argument("--encoding", default=SERVERS_ENCODING, help=f"Encoding for servers file (default {SERVERS_ENCODING})")
    parser.add_argument("--key-passphrase", action="store_true", help="Prompt for SSH key passphrase (for encrypted keys)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show SSH connection errors / paramiko tracebacks")
    parser.add_argument("--force", action="store_true", help="Remove Oracle Java even if running processes use it (default: skip such installations)")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("paramiko").setLevel(logging.DEBUG)

    key_password = None
    if args.key_passphrase:
        key_password = getpass("SSH key passphrase (or press Enter if none): ")
        if not key_password:
            key_password = None

    KEY_DIR = args.key_dir
    if not os.path.isdir(KEY_DIR):
        print("SSH key directory not found.")
        sys.exit(1)

    if args.all_ec2:
        if args.servers_file:
            print("Ignoring servers_file when --all-ec2 is set.")
        region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            print("For --all-ec2 set AWS_REGION (or AWS_DEFAULT_REGION) or use --region.")
            sys.exit(1)
        print(f"Discovering EC2 instances in {region} (use_public_ip={args.ec2_public_ip})...")
        servers = get_ec2_instances(region=region, use_public_ip=args.ec2_public_ip)
        if not servers:
            print("No running EC2 instances found.")
            sys.exit(1)
        print(f"Found {len(servers)} instance(s).")
    else:
        if not args.servers_file:
            print("Provide servers_file or use --all-ec2 to discover servers from AWS EC2.")
            sys.exit(1)
        if not os.path.isfile(args.servers_file):
            print("Servers file not found.")
            sys.exit(1)
        servers = parse_servers_file(args.servers_file, args.encoding)
        if not servers:
            print("No servers found in file (empty or only comments).")
            sys.exit(1)

    run_cmd_fn = lambda ssh, cmd: run_cmd(ssh, cmd, timeout=args.exec_timeout)
    all_removals = {}

    for server in servers:
        print("\n======================================")
        print(f"Processing {server}")
        print("======================================")

        ssh = try_connect(server, KEY_DIR, port=args.port, timeout=args.timeout, key_password=key_password)
        if not ssh:
            print(f"Could not connect to {server} (skipping).")
            continue

        if not args.skip_mlocate:
            print("Installing/updating mlocate...")
            install_mlocate(ssh, run_cmd_fn)
        else:
            print("Skipping mlocate (using find fallback).")

        print("Searching for Java installations...")
        locate_output = run_cmd_fn(
            ssh,
            "sudo locate bin/java 2>/dev/null | grep -v corretto || true"
        )
        java_bins = [
            os.path.dirname(os.path.dirname(p.strip()))
            for p in locate_output.splitlines() if p.strip()
        ]
        if not java_bins:
            java_bins = find_java_bins_with_find(ssh, run_cmd_fn)
        java_bins = list(set(java_bins))

        removals = []  # list of (java_dir, symlink_paths, process_lines) – only added if no running processes (unless --force)

        for java_dir in java_bins:
            if not java_dir:
                continue
            if java_dir in PROTECTED_ROOTS or java_dir == PROTECTED_PREFIX or java_dir.startswith(PROTECTED_PREFIX + "/"):
                continue
            java_bin = f"{java_dir}/bin/java"
            version = run_cmd_fn(ssh, f"sudo {shell_quote(java_bin)} -version 2>&1")
            vendor = parse_java_vendor(version)
            if vendor != "Oracle":
                continue
            symlinks_out = run_cmd_fn(ssh, f"sudo find / -type l -lname {shell_quote(java_dir + '*')} 2>/dev/null || true")
            symlink_paths = [p.strip() for p in symlinks_out.splitlines() if p.strip()]
            # Check for running processes using this Java (exe or cwd under java_dir)
            processes_out = run_cmd_fn(ssh, f"ps -ef 2>/dev/null | grep -F {shell_quote(java_dir)} | grep -v grep || true")
            process_lines = [p.strip() for p in processes_out.splitlines() if p.strip()]

            if process_lines and not args.force:
                print(f"      Skipping {java_dir} – running processes use it (use --force to remove anyway):")
                for line in process_lines[:5]:
                    print(f"        {line}")
                if len(process_lines) > 5:
                    print(f"        ... and {len(process_lines) - 5} more")
                continue
            removals.append((java_dir, symlink_paths, process_lines))

        if removals:
            all_removals[server] = removals
            print(f"  → Will remove {len(removals)} Oracle installation(s) and their symlinks:")
            for java_dir, symlinks, _ in removals:
                print(f"      Directory: {java_dir}")
                for s in symlinks:
                    if s == PROTECTED_PREFIX or s.startswith(PROTECTED_PREFIX + "/"):
                        print(f"        Symlink (protected, won't remove): {s}")
                    else:
                        print(f"        Symlink: {s}")
                if not symlinks:
                    print(f"        (no symlinks)")
        else:
            print(f"  → No removable Oracle Java found on {server} (only non-Oracle or none).")

        ssh.close()

    if not all_removals:
        print("\nNo Oracle Java installations found anywhere.")
        sys.exit(0)

    print("\n======================================")
    print("ONLY THE FOLLOWING WILL BE REMOVED (Oracle JDK + their symlinks; /usr/bin never touched):")
    print("======================================")

    for server, entries in all_removals.items():
        print(f"\n{server}:")
        for java_dir, symlinks, _ in entries:
            print(f"  Directory to remove: {java_dir}")
            if symlinks:
                for s in symlinks:
                    if s == PROTECTED_PREFIX or s.startswith(PROTECTED_PREFIX + "/"):
                        print(f"    Symlink (protected, will NOT remove): {s}")
                    else:
                        print(f"    Symlink to remove: {s}")
            else:
                print(f"    (no symlinks found)")

    if args.dry_run:
        print("\n[DRY-RUN] No removals performed. Run without --dry-run to apply.")
        sys.exit(0)

    if not args.no_confirm:
        confirm = input("\nType 'yes' to remove ALL these installations: ")
        if confirm.strip().lower() != "yes":
            print("Aborting removal.")
            sys.exit(0)

    print("\nStarting removal (breaking symlinks first, then removing Oracle Java directories)...")

    for server in all_removals:
        ssh = try_connect(server, KEY_DIR, port=args.port, timeout=args.timeout, key_password=key_password)
        if not ssh:
            print(f"Skipping {server}, could not reconnect.")
            continue
        for java_dir, symlinks, _ in all_removals[server]:
            if java_dir in PROTECTED_ROOTS or java_dir == PROTECTED_PREFIX or java_dir.startswith(PROTECTED_PREFIX + "/"):
                print(f"Skipping protected path: {java_dir}")
                continue
            for sym in symlinks:
                if not sym:
                    continue
                # Never remove symlinks under /usr/bin (e.g. /usr/bin/java) – we do not touch that folder
                if sym == PROTECTED_PREFIX or sym.startswith(PROTECTED_PREFIX + "/"):
                    print(f"  Skipping symlink (protected): {sym}")
                    continue
                print(f"  Breaking symlink: {sym}")
                run_cmd_fn(ssh, f"sudo rm -f {shell_quote(sym)}")
            print(f"Removing directory {java_dir} from {server}")
            run_cmd_fn(ssh, f"sudo rm -rf {shell_quote(java_dir)}")
        ssh.close()

    print("\nRemoval completed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
