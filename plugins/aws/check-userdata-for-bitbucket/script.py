import os
import sys
import boto3
import base64
from botocore.exceptions import ClientError

SEARCH_TEXT = os.environ.get("SEARCH_TEXT")
if not SEARCH_TEXT:
    sys.exit("Set SEARCH_TEXT")

ec2 = boto3.client("ec2")


def get_user_data(instance_id):
    try:
        response = ec2.describe_instance_attribute(
            InstanceId=instance_id,
            Attribute="userData"
        )

        value = response.get("UserData", {}).get("Value")
        if not value:
            return ""

        return base64.b64decode(value).decode("utf-8", errors="ignore")

    except ClientError:
        return ""


paginator = ec2.get_paginator("describe_instances")

for page in paginator.paginate():
    for reservation in page.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            instance_id = instance["InstanceId"]
            user_data = get_user_data(instance_id)

            if SEARCH_TEXT in user_data:
                ip = instance.get("PrivateIpAddress") or instance.get("PublicIpAddress")
                if ip:
                    print(ip)
