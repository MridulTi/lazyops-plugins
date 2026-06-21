import argparse
import os
import sys

try:
    import paramiko
except ImportError:
    print("Install paramiko: pip install paramiko")
    sys.exit(1)

# Optional Ed25519 support (paramiko >= 2.2)
try:
    from paramiko.ed25519key import Ed25519Key
    HAS_ED25519 = True
except ImportError:
    HAS_ED25519 = False

SSH_USERS = ["ec2-user", "ubuntu"]
DEFAULT_SSH_PORT = 22
DEFAULT_TIMEOUT = 10
EXEC_TIMEOUT = 30
KEY_EXTENSIONS = (".pem", ".key", "")  # "" = no extension
SERVERS_ENCODING = "utf-8"
SERVERS_COMMENT_PREFIX = "#"


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


def get_key_files(key_dir, key_extensions=KEY_EXTENSIONS):
    """List key files only (by extension); hidden files like .pem included."""
    key_files = []
    for name in os.listdir(key_dir):
        path = os.path.join(key_dir, name)
        if not os.path.isfile(path):
            continue
        if key_extensions and not any(name.endswith(ext) or (ext == "" and "." not in name) for ext in key_extensions):
            continue
        key_files.append(name)
    return key_files


def load_private_key(key_path, password=None):
    """Try RSA then Ed25519. Returns key or None."""
    for loader, need_pass in [
        (paramiko.RSAKey.from_private_key_file, True),
        (paramiko.RSAKey.from_private_key_file, False),
    ]:
        try:
            if need_pass and password is not None:
                return loader(key_path, password=password)
            if not need_pass:
                return loader(key_path)
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
            except Exception:
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



def main():
    with open(SERVERS_FILE) as f:
        servers = [line.strip() for line in f if line.strip()]

    all_removals = {}

    for server in servers:
        print("\n======================================")
        print(f"Processing {server}")
        print("======================================")

        ssh = try_connect(server)
        if not ssh:
            print(f"Could not connect to {server}")
            continue

        print("Installing mlocate...")
        run_cmd(ssh, "sudo apt-get update -y")
        run_cmd(ssh, "sudo apt-get install -y mlocate")
        run_cmd(ssh, "sudo updatedb")

        print("Searching for Java installations...")
        locate_output = run_cmd(
            ssh,
            "sudo locate bin/java | grep -v corretto"
        )

        java_bins = list(set([os.path.dirname(os.path.dirname(p.strip()))
                              for p in locate_output.splitlines() if p.strip()]))

        removals = []

        for java_dir in java_bins:
            print(f"\nChecking {java_dir}")

            if not java_dir:
                continue

            version = run_cmd(
                ssh,
                f"sudo {java_dir}/bin/java -version"
            )

            print(version.strip())

            if "oracle" in version.lower():
                print("Oracle Java detected")

                symlinks = run_cmd(
                    ssh,
                    f"sudo find / -type l -lname '{java_dir}*' 2>/dev/null"
                )

                processes = run_cmd(
                    ssh,
                    f"ps -ef | grep '{java_dir}' | grep -v grep"
                )

                print("\nSymlinks:")
                print(symlinks.strip() or "None")

                print("\nRunning Processes:")
                print(processes.strip() or "None")

                removals.append(java_dir)

        if removals:
            all_removals[server] = removals

        ssh.close()

    if not all_removals:
        print("\nNo Oracle Java installations found anywhere.")
        sys.exit(0)

    print("\n======================================")
    print("Oracle Java installations found:")
    print("======================================")

    for server, dirs in all_removals.items():
        print(f"\n{server}:")
        for d in dirs:
            print(f"  {d}")

    confirm = input("\nType 'yes' to remove ALL these installations: ")

    if confirm.strip().lower() != "yes":
        print("Aborting removal.")
        sys.exit(0)

    print("\nStarting removal...")

    for server in all_removals:
        ssh = try_connect(server, KEY_DIR, port=args.port, timeout=args.timeout)
        if not ssh:
            print(f"Skipping {server}, could not reconnect.")
            continue

        for d in all_removals[server]:
            print(f"Removing {d} from {server}")
            run_cmd(ssh, f"sudo rm -rf {d}")

        ssh.close()

    print("\nRemoval completed.")


if __name__ == "__main__":
    main()
