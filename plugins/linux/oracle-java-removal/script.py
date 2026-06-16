import os
import subprocess
import paramiko

def run(cmd):
    return subprocess.check_output(cmd,shell=True,text=True).strip()

def check_os():
    if os.path.exists("/etc/os-release"):
        with open("/etc/os-release","r") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.split("=")[1].strip()
    return None 

def ssh_server(ip):
    users=("ec2-user","ubuntu","centos")
    All_Keys="~/Documents/bitbucket/All_Keys"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for keys in All_Keys:
        for user in users:
            ssh.connect(
                hostname=ip, 
                username=user, 
                key_filename=keys
            )
            return ssh
    return None


def check_arch():
    arch=run("uname -m")
    return arch

def detect_oracle_java():
    os_name=check_os()
    if os_name=="ubuntu":
        run("sudo apt-get update -y && sudo apt-get install -y mlocate")
    if os_name=="centos" or os_name=="redhat" or os_name=="fedora" or os_name=="amzn":
        run("sudo yum update -y && sudo yum install -y mlocate")
    run("sudo updatedb")
    java_bins=run("locate bin/java").splitlines()
    return java_bins

def check_java_version(java_bin):
    version=run(f"{java_bin} -version")
    os=run(f"cat /etc/os-release | grep ID").split("=").strip()
    return version,os

def install_java_corretto(os,version):
    arch=check_arch()
    if os=="ubuntu":
        run(f"cd /opt && wget https://corretto.aws/downloads/latest/amazon-corretto-{version}-{arch}-linux-jdk.deb")
        run(f"sudo dpkg -i amazon-corretto-{version}-{arch}-linux-jdk.deb")
    else:
        run(f"cd /opt && wget https://corretto.aws/downloads/latest/amazon-corretto-{version}-{arch}-linux-jdk.rpm")
        run(f"sudo yum localinstall -y amazon-corretto-{version}-{arch}-linux-jdk.rpm")
    return f"/usr/lib/jvm/java-{version}-amazon-corretto" if version!="8" else f"/usr/lib/jvm/java-1.8.0-amazon-corretto"

def replace_java_bin(java_bin,new_java_bin):
    java_folder=os.path.dirname(java_bin)
    bkp_folder=java_folder+"_bkp"
    run(f"sudo mv {java_folder} {bkp_folder}")
    run(f"sudo ln -s {new_java_bin} {java_folder}")
    return bkp_folder

def check_any_java_service(java_bin):
    lines=run("ps -ef | grep java").splitlines()
    for line in lines:
        if java_bin in line:
            run("service tomcat restart")
        else:
            print (f"Skipping {line} because it is not {java_bin}")

def main():
    print ("Detecting Oracle Java")
    print ("--------------------------------")
    with open("./ips.conf",r) as f:
        for line in f:
            ip=line.strip()
            if not ip or ip.startswith("#"):
                continue
            client=ssh_server(ip)
            if client is None:
                print(f"No SSH for {ip}")
                continue
            try:
                java_bins=detect_oracle_java()
                for java_bin in java_bins:
                    version,os=check_java_version(java_bin)
                    if "SE" in version:
                        new_java_bin=install_java_corretto(os,version)
                        bkp_folder=replace_java_bin(java_bin,new_java_bin)
                        check_any_java_service(java_bin)
                        run("rm -rf "+bkp_folder)
                else:
                    print (f"Skipping {java_bin} because it is not SE")
            finally:
                client.close()

    
if __name__ == "__main__":
    main()