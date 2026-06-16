import paramiko
import concurrent.futures
import os
import sys
import shlex
import threading

USERS = ["ec2-user", "ubuntu", "centos"]
SSH_KEY_DIR = os.environ.get("SSH_KEY_DIR")
if not SSH_KEY_DIR:
    sys.exit("Set SSH_KEY_DIR")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in {"1", "true", "yes"}
GIT_OLD_ORG = os.environ.get("GIT_OLD_ORG")
GIT_NEW_ORG = os.environ.get("GIT_NEW_ORG")
if not GIT_OLD_ORG or not GIT_NEW_ORG:
    sys.exit("Set GIT_OLD_ORG and GIT_NEW_ORG")

EXCLUDE_PREFIXES = [
    "/var/lib/jenkins"
]

DRY_RUN_REPO_FILE = "dry_run_repo_urls.txt"

repo_urls_lock = threading.Lock()
repo_urls = set()


def get_ssh_keys():
    keys = []

    for f in os.listdir(SSH_KEY_DIR):
        path = os.path.join(SSH_KEY_DIR, f)

        if not os.path.isfile(path):
            continue

        if f.endswith(".pem") or f.endswith(".key") or f.endswith(".rsa"):
            keys.append(path)

    return keys


SSH_KEYS = get_ssh_keys()


def run_cmd(ssh, cmd):
    wrapped = f"sudo bash -lc {shlex.quote(cmd)}"

    stdin, stdout, stderr = ssh.exec_command(wrapped)

    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="ignore")
    err = stderr.read().decode(errors="ignore")

    return rc, out.strip(), err.strip()


def connect(ip):
    for user in USERS:
        for key in SSH_KEYS:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            try:
                ssh.connect(
                    hostname=ip,
                    username=user,
                    key_filename=key,
                    timeout=8,
                    look_for_keys=False,
                    allow_agent=False
                )

                print(f"{ip}: connected as {user} using {os.path.basename(key)}")
                return ssh

            except Exception:
                ssh.close()

    return None


def ensure_locate(ssh):
    rc, _, _ = run_cmd(ssh, "command -v locate")
    if rc == 0:
        return

    rc, _, _ = run_cmd(ssh, "command -v apt-get")
    if rc == 0:
        run_cmd(
            ssh,
            "apt-get update -y && apt-get install -y plocate mlocate || apt-get install -y mlocate"
        )
        return

    rc, _, _ = run_cmd(ssh, "command -v yum")
    if rc == 0:
        run_cmd(ssh, "yum install -y mlocate")
        return

    rc, _, _ = run_cmd(ssh, "command -v dnf")
    if rc == 0:
        run_cmd(ssh, "dnf install -y mlocate")


def refresh_locate_db(ssh):
    run_cmd(ssh, "updatedb")


def is_excluded(path):
    return any(path.startswith(prefix) for prefix in EXCLUDE_PREFIXES)


def get_git_dirs(ssh):
    refresh_locate_db(ssh)

    rc, out, _ = run_cmd(ssh, "locate '/.git' 2>/dev/null")

    if rc != 0 or not out:
        rc, out, _ = run_cmd(
            ssh,
            "find / -type d -name .git 2>/dev/null"
        )

    dirs = []

    for x in out.splitlines():
        x = x.strip()

        if not x:
            continue

        if is_excluded(x):
            continue

        dirs.append(x)

    return dirs


def process_repo(ssh, git_dir):
    global repo_urls

    config = f"{git_dir}/config"

    rc, out, _ = run_cmd(
        ssh,
        f"grep "$GIT_OLD_ORG" '{config}' || true"
    )

    if not out:
        return

    repo_dir = os.path.dirname(git_dir)

    matching_urls = []

    for line in out.splitlines():
        line = line.strip()

        if GIT_OLD_ORG not in line:
            continue

        if "=" in line:
            url = line.split("=", 1)[1].strip()
        else:
            url = line

        matching_urls.append(url)

    if DRY_RUN:
        print(f"    [DRY RUN] would update: {config}")

        if matching_urls:
            print("    [DRY RUN] matching repo URLs:")
            for url in matching_urls:
                print(f"      {url}")

            with repo_urls_lock:
                repo_urls.update(matching_urls)

        print(f"    [DRY RUN] would run: git pull in {repo_dir}")
        return

    print(f"    updating {config}")

    run_cmd(
        ssh,
        f"sed -i "s/$GIT_OLD_ORG/$GIT_NEW_ORG/g" '{config}'"
    )

    rc, _, err = run_cmd(
        ssh,
        f"cd '{repo_dir}' && git pull"
    )

    if rc == 0:
        print(f"    git pull success: {repo_dir}")
    else:
        print(f"    git pull failed: {repo_dir}")
        if err:
            print(f"      {err}")


def process_host(ip):
    print(f"\n===== {ip} =====")

    ssh = connect(ip)

    if not ssh:
        print(f"{ip}: connection failed")
        return

    try:
        ensure_locate(ssh)

        git_dirs = get_git_dirs(ssh)

        if not git_dirs:
            print("  no .git directories found")
            return

        for git_dir in git_dirs:
            try:
                process_repo(ssh, git_dir)
            except Exception as e:
                print(f"    failed for {git_dir}: {e}")

    finally:
        ssh.close()


def main():
    if not SSH_KEYS:
        print("No SSH keys found")
        return

    with open("ips.txt") as f:
        ips = [line.strip() for line in f if line.strip()]

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        executor.map(process_host, ips)

    if DRY_RUN:
        with open(DRY_RUN_REPO_FILE, "w") as f:
            for url in sorted(repo_urls):
                f.write(url + "\n")

        print(f"\nDry-run repo URL list written to: {DRY_RUN_REPO_FILE}")


if __name__ == "__main__":
    main()
