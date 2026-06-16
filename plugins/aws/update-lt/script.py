#!/usr/bin/env python3

import json
import os
import sys

import boto3


def _require_region():
    region = os.environ.get("REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        sys.exit("Set REGION or AWS_REGION")
    return region


REGION = _require_region()
ec2 = boto3.client("ec2", region_name=REGION)
asg_client = boto3.client("autoscaling", region_name=REGION)


def load_ami_map(path: str) -> dict:
    with open(path, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        sys.exit("AMI map file must be a JSON object of source_ami -> target_ami")
    return data


def get_asg_names(path: str):
    with open(path, "r") as f:
        return [line.strip() for line in f if line.strip()]


def get_lt_version_details(lt_id):
    response = ec2.describe_launch_template_versions(
        LaunchTemplateId=lt_id,
        Versions=["$Latest", "$Default"],
    )
    latest = None
    default = None
    for v in response["LaunchTemplateVersions"]:
        if v.get("DefaultVersion", False):
            default = v
        latest = v
    return latest, default


def resolve_asg_version(lt_id, version):
    if version == "$Latest":
        latest, _ = get_lt_version_details(lt_id)
        return latest["VersionNumber"], "LATEST"
    if version == "$Default":
        _, default = get_lt_version_details(lt_id)
        return default["VersionNumber"], "DEFAULT"
    return int(version), "FIXED"


def get_launch_template_data(lt_id, version):
    response = ec2.describe_launch_template_versions(
        LaunchTemplateId=lt_id,
        Versions=[str(version)],
    )
    return response["LaunchTemplateVersions"][0]


def create_new_version(lt_id, source_version, to_ami):
    response = ec2.create_launch_template_version(
        LaunchTemplateId=lt_id,
        SourceVersion=str(source_version),
        LaunchTemplateData={"ImageId": to_ami},
    )
    return response["LaunchTemplateVersion"]["VersionNumber"]


def freeze_asg(asg_name, lt_id, version):
    asg_client.update_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": str(version)},
    )


def process_asg(asg_name, ami_map):
    print(f"\nASG: {asg_name}")
    response = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    if not response["AutoScalingGroups"]:
        print("  ASG not found")
        return
    asg = response["AutoScalingGroups"][0]
    if "LaunchTemplate" not in asg:
        print("  No launch template")
        return
    lt = asg["LaunchTemplate"]
    lt_id = lt["LaunchTemplateId"]
    version = lt["Version"]
    resolved_version, mode = resolve_asg_version(lt_id, version)
    lt_data = get_launch_template_data(lt_id, resolved_version)
    current_ami = lt_data["LaunchTemplateData"].get("ImageId")
    if current_ami not in ami_map:
        print(f"  Skipping (AMI {current_ami} not in map)")
        return
    to_ami = ami_map[current_ami]
    print(f"  {current_ami} -> {to_ami}")
    new_version = create_new_version(lt_id, resolved_version, to_ami)
    print(f"  New LT version: {new_version}")
    if mode in ("LATEST", "DEFAULT"):
        freeze_asg(asg_name, lt_id, resolved_version)


def main():
    if len(sys.argv) != 3:
        print("Usage: script.py <ami-map.json> <asg-list-file>")
        sys.exit(1)
    ami_map = load_ami_map(sys.argv[1])
    asg_names = get_asg_names(sys.argv[2])
    print(f"Processing {len(asg_names)} ASGs...")
    for asg_name in asg_names:
        process_asg(asg_name, ami_map)


if __name__ == "__main__":
    main()
