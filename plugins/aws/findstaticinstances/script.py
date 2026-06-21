import boto3
import pandas as pd
import os

OUTPUT_FILE = "static_servers.xlsx"
import os
import sys

def _require_region():
    region = os.environ.get("REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        sys.exit("Set REGION or AWS_REGION")
    return region

REGION = _require_region()


def get_account_id():
    sts = boto3.client('sts')
    return sts.get_caller_identity()['Account']


# 🔹 Step 1: Find PROD VPCs
def get_prod_vpcs():
    ec2 = boto3.client('ec2', region_name=REGION)

    prod_vpcs = set()

    response = ec2.describe_vpcs()

    for vpc in response['Vpcs']:
        tags = vpc.get('Tags', [])

        for tag in tags:
            key = tag['Key'].lower()
            value = tag['Value'].lower()

            if (
                (key in ['env', 'environment'] and value in ['prod', 'production','prod-comm']) or
                ('prod' in value)
            ):
                prod_vpcs.add(vpc['VpcId'])

    print(f"✅ Detected PROD VPCs: {prod_vpcs}")
    return prod_vpcs


# 🔹 Step 2: Get static instances ONLY in PROD VPC
def get_static_instances(account_id, prod_vpcs):
    ec2 = boto3.client('ec2', region_name=REGION)
    instances_data = []

    paginator = ec2.get_paginator('describe_instances')

    for page in paginator.paginate():
        for reservation in page['Reservations']:
            for instance in reservation['Instances']:

                vpc_id = instance.get('VpcId')

                # ✅ Filter only PROD VPC
                if vpc_id not in prod_vpcs:
                    continue

                tags = instance.get('Tags', [])

                is_asg = any(tag['Key'] == 'aws:autoscaling:groupName' for tag in tags)

                if not is_asg:
                    instances_data.append({
                        "InstanceId": instance.get('InstanceId'),
                        "IP": instance.get('PrivateIpAddress'),
                        "VpcId": vpc_id,
                        "AccountId": account_id,
                        "Region": REGION,
			"Name": any(tag['Key'] == 'Name' for tag in tags)
                    })

    return instances_data


# 🔹 MAIN
account_id = get_account_id()

prod_vpcs = get_prod_vpcs()

if not prod_vpcs:
    raise Exception("❌ No PROD VPCs detected. Check tagging strategy.")

data = get_static_instances(account_id, prod_vpcs)

df = pd.DataFrame(data)

# ✅ Append logic
if os.path.exists(OUTPUT_FILE):
    existing_df = pd.read_excel(OUTPUT_FILE)
    combined_df = pd.concat([existing_df, df], ignore_index=True).drop_duplicates()
else:
    combined_df = df

combined_df.to_excel(OUTPUT_FILE, index=False)

print(f"✅ Data appended for account: {account_id} in {REGION}")
