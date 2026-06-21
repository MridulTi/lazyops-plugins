import subprocess
import paramiko
import configparser

class CheckOS:
	def __init__(self):
		self.config = configparser.ConfigParser()
		self.config.read_file(open('ips.cfg'))
		self.ssh_client = paramiko.SSHClient()

	def checkos(self):
		public_key = self.config.get('DEFAULT','privateKey')
		IPS= self.config.get('DEFAULT','ips')
		username=self.config.get('DEFAULT','username')
		
		for ip in IPS:
			self.ssh_client.connect(hostname=ip, username=username, pkey=public_key)
			result=subprocess.run(['cat','/etc/os-release/'],capture_output=True,text=True)
			if (result.returncode==0):
				print(f"for the IP: {ip} {result.stdout}")
			

if __name__ == "__main__":
	CO=CheckOS()
	CO.checkos()

