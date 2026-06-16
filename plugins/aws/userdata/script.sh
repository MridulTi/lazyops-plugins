#!/bin/bash -xe
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1

: "${S3_CONFIG_URI:?Set S3_CONFIG_URI, e.g. s3://my-bucket/scripts/provision-dr.sh}"

yum install telnet vim -y
python3 -m pip install ansible
aws s3 cp "$S3_CONFIG_URI" .
yum install git ansible --nogpgcheck -y

cat <<EOF | sudo tee /etc/yum.repos.d/influxdb.repo
[influxdb]
name = InfluxDB Repository - RHEL \$releasever
baseurl = https://repos.influxdata.com/rhel/\$releasever/\$basearch/stable
enabled = 1
gpgcheck = 1
gpgkey = https://repos.influxdata.com/influxdb.key
EOF

sudo sed -i "s/\$releasever/$(rpm -E %{rhel})/g" /etc/yum.repos.d/influxdb.repo
yum install telegraf --nogpgcheck -y
sh provision.sh
