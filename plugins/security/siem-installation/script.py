import paramiko
import boto3

ec2 = boto3.client('ec2')

ssh_client = paramiko.SSHClient()
ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

usernames=['ubuntu', 'ec2-user', 'centos']

def save_instance():
    response = ec2.describe_instances()
    for reservation in response['Reservations']:
        with open("servers.txt", "a") as f:
            for instance in reservation['Instances']:
                f.write(instance['PrivateIpAddress'] + "\n")

def list_instances():
    with open("servers.txt", "r") as f:
        for line in f:
            print(line.strip())

def install_siem():
    with open("servers.txt", "r") as f:
        for line in f:
            ip = line.strip()
            for username in usernames:
                ssh_client.connect(hostname=ip, username=username)
                ssh_client.exec_command('sudo apt-get install -y rsyslog')
                ssh_client.close()
