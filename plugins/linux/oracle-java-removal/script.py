import glob
import os
import subprocess

import paramiko


def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def check_os():
    if os.path.exists("/etc/os-release"):
        with open("/etc/os-release", "r") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.split("=")[1].strip().strip('"')
    return None


def ssh_server(ip):
    users = ("ec2-user", "ubuntu", "centos")
    key_dir = os.environ.get("SSH_KEY_DIR")
    if not key_dir:
        raise RuntimeError("Set SSH_KEY_DIR env var (directory containing .pem keys)")
    key_dir = os.path.expanduser(key_dir)
    key_files = sorted(glob.glob(os.path.join(key_dir, "*.pem")))
    if not key_files:
        raise RuntimeError(f"No .pem keys found in SSH_KEY_DIR: {key_dir}")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for key_path in key_files:
        for user in users:
            try:
                ssh.connect(hostname=ip, username=user, key_filename=key_path, timeout=10)
                return ssh
            except Exception:
                continue
    return None


def check_arch():
    return run("uname -m")


def detect_oracle_java():
    os_name = check_os()
    if os_name == "ubuntu":
        run("sudo apt-get update -y && sudo apt-get install -y mlocate")
    if os_name in ("centos", "redhat", "fedora", "amzn"):
        run("sudo yum update -y && sudo yum install -y mlocate")
    run("sudo updatedb")
    return run("locate bin/java").splitlines()


def check_java_version(java_bin):
    version = run(f"{java_bin} -version")
    os_id = run("grep ^ID= /etc/os-release").split("=", 1)[1].strip().strip('"')
    return version, os_id


def install_java_corretto(os_id, version):
    arch = check_arch()
    if os_id == "ubuntu":
        run(
            f"cd /opt && wget https://corretto.aws/downloads/latest/"
            f"amazon-corretto-{version}-{arch}-linux-jdk.deb"
        )
        run(f"sudo dpkg -i amazon-corretto-{version}-{arch}-linux-jdk.deb")
    else:
        run(
            f"cd /opt && wget https://corretto.aws/downloads/latest/"
            f"amazon-corretto-{version}-{arch}-linux-jdk.rpm"
        )
        run(f"sudo yum localinstall -y amazon-corretto-{version}-{arch}-linux-jdk.rpm")
    if version == "8":
        return "/usr/lib/jvm/java-1.8.0-amazon-corretto"
    return f"/usr/lib/jvm/java-{version}-amazon-corretto"


def replace_java_bin(java_bin, new_java_bin):
    java_folder = os.path.dirname(java_bin)
    bkp_folder = java_folder + "_bkp"
    run(f"sudo mv {java_folder} {bkp_folder}")
    run(f"sudo ln -s {new_java_bin} {java_folder}")
    return bkp_folder


def check_any_java_service(java_bin):
    lines = run("ps -ef | grep java").splitlines()
    for line in lines:
        if java_bin in line:
            run("service tomcat restart")
        else:
            print(f"Skipping {line} because it is not {java_bin}")


def main():
    ips_file = os.environ.get("IPS_FILE", "./ips.conf")
    print("Detecting Oracle Java")
    print("--------------------------------")
    with open(ips_file, "r") as f:
        for line in f:
            ip = line.strip()
            if not ip or ip.startswith("#"):
                continue
            client = ssh_server(ip)
            if client is None:
                print(f"No SSH for {ip}")
                continue
            try:
                java_bins = detect_oracle_java()
                for java_bin in java_bins:
                    version, os_id = check_java_version(java_bin)
                    if "SE" in version:
                        new_java_bin = install_java_corretto(os_id, version)
                        bkp_folder = replace_java_bin(java_bin, new_java_bin)
                        check_any_java_service(java_bin)
                        run("rm -rf " + bkp_folder)
                    else:
                        print(f"Skipping {java_bin} because it is not SE")
            finally:
                client.close()


if __name__ == "__main__":
    main()
