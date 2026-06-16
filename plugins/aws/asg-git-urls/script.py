import os
import sys
import boto3
import base64
import gzip
import re

ASG_FILE = os.environ.get("ASG_FILE") or (sys.argv[1] if len(sys.argv) > 1 else None)
if not ASG_FILE:
    sys.exit("Usage: script.py <asg-list-file> or set ASG_FILE")

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


def get_launch_template_userdata(lt_id, version):
    resolved_version = resolve_launch_template_version(
        lt_id,
        version
    )

    resp = ec2.describe_launch_template_versions(
        LaunchTemplateId=lt_id,
        Versions=[resolved_version]
    )

    versions = resp.get("LaunchTemplateVersions", [])
    if not versions:
        return "", resolved_version

    data = versions[0].get("LaunchTemplateData", {})
    user_data = data.get("UserData")

    return decode_userdata(user_data), resolved_version


def extract_launch_template(group):
    if "LaunchTemplate" in group:
        return group["LaunchTemplate"]

    mip = group.get("MixedInstancesPolicy")
    if mip:
        return (
            mip.get("LaunchTemplate", {})
               .get("LaunchTemplateSpecification")
        )

    return None


def extract_git_urls(text):
    if not text:
        return []

    pattern = r'(?:git@|https?://)[^\s"\']+'
    matches = re.findall(pattern, text)

    cleaned = set()

    for m in matches:
        cleaned.add(m.strip())

    return sorted(cleaned)


def get_asg(name):
    resp = asg.describe_auto_scaling_groups(
        AutoScalingGroupNames=[name]
    )

    groups = resp.get("AutoScalingGroups", [])

    if not groups:
        return None

    return groups[0]


def main():
    with open(ASG_FILE) as f:
        asg_names = [
            line.strip()
            for line in f
            if line.strip()
        ]

    for asg_name in asg_names:
        try:
            group = get_asg(asg_name)

            if not group:
                print(f"\nASG not found: {asg_name}")
                continue

            lt = extract_launch_template(group)

            if not lt:
                print(f"\nNo launch template: {asg_name}")
                continue

            lt_id = lt.get("LaunchTemplateId")
            version = lt.get("Version", "$Default")

            if not lt_id:
                print(f"\nNo launch template id: {asg_name}")
                continue

            user_data, resolved_version = get_launch_template_userdata(
                lt_id,
                version
            )

            git_urls = extract_git_urls(user_data)

            if git_urls:
                print(f"\nASG: {asg_name}")
                print(f"Configured Version: {version}")
                print(f"Resolved Version: {resolved_version}")

                for url in git_urls:
                    print(f"  {url}")
            else:
                print(f"\nASG: {asg_name}")
                print("  No git URLs found")

        except Exception as e:
            print(f"\nFailed: {asg_name} -> {e}")


if __name__ == "__main__":
    main()
