import os
import sys
import boto3
import base64
import gzip

SEARCH_TEXT = os.environ.get("SEARCH_TEXT")
if not SEARCH_TEXT:
    sys.exit("Set SEARCH_TEXT")

asg = boto3.client("autoscaling")
ec2 = boto3.client("ec2")


def decode_userdata(value):
    if not value:
        return ""

    raw = base64.b64decode(value)

    try:
        return gzip.decompress(raw).decode("utf-8", errors="ignore")
    except Exception:
        return raw.decode("utf-8", errors="ignore")


def resolve_launch_template_version(lt_id, version):
    if version not in ["$Default", "$Latest"]:
        return str(version)

    resp = ec2.describe_launch_templates(
        LaunchTemplateIds=[lt_id]
    )

    lt = resp["LaunchTemplates"][0]

    if version == "$Default":
        return str(lt["DefaultVersionNumber"])

    return str(lt["LatestVersionNumber"])


def get_lt_userdata(lt_id, version):
    resolved_version = resolve_launch_template_version(lt_id, version)

    resp = ec2.describe_launch_template_versions(
        LaunchTemplateId=lt_id,
        Versions=[resolved_version]
    )

    versions = resp.get("LaunchTemplateVersions", [])
    if not versions:
        return "", resolved_version

    data = versions[0].get("LaunchTemplateData", {})
    return decode_userdata(data.get("UserData")), resolved_version


def extract_launch_template(group):
    if "LaunchTemplate" in group:
        return group["LaunchTemplate"]

    mip = group.get("MixedInstancesPolicy")
    if mip:
        return mip.get("LaunchTemplate", {}).get("LaunchTemplateSpecification")

    return None


paginator = asg.get_paginator("describe_auto_scaling_groups")

matching_asgs = []

for page in paginator.paginate():
    for group in page.get("AutoScalingGroups", []):

        # skip desired capacity = 0
        if group.get("DesiredCapacity", 0) == 0:
            continue

        lt = extract_launch_template(group)
        if not lt:
            continue

        lt_id = lt.get("LaunchTemplateId")
        lt_version = lt.get("Version", "$Default")

        if not lt_id:
            continue

        user_data, _ = get_lt_userdata(lt_id, lt_version)

        if SEARCH_TEXT in user_data:
            matching_asgs.append(group["AutoScalingGroupName"])

# -------- FINAL OUTPUT --------
for name in matching_asgs:
    print(name)
