#!/usr/bin/env python3
"""
Find all EC2 instances in an AWS account, read their user data, and list every
branch used when the subscription-ansible repo is used with the cortex role (-t cortex).

Usage:
    # Default credentials, single region
    python3 list_cortex_branches_from_userdata.py [--region REGION]

    # All regions in account
    python3 list_cortex_branches_from_userdata.py --all-regions

    # Assume role (e.g. from cortex.conf-style config)
    python3 list_cortex_branches_from_userdata.py --config cortex.conf [--all-regions]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import io
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

try:
    import boto3
except ImportError:
    print("❌ boto3 is required. Install with: pip install boto3", file=sys.stderr)
    sys.exit(1)

# Support both subscription-ansible and subscriptions-ansible (Bitbucket repo names)
REPO_NAMES = ("subscription-ansible", "subscriptions-ansible")
cortex_TAG = "cortex"

# Patterns: ansible-playbook/ansible with cortex tag
ANSIBLE_cortex_PATTERNS = [
    re.compile(r"ansible(?:-playbook)?\s+[^;|\n]*-t\s+cortex\b", re.IGNORECASE),
    re.compile(r"ansible(?:-playbook)?\s+[^;|\n]*--tags\s+(?:['\"]?)\s*cortex\b", re.IGNORECASE),
    re.compile(r"ansible(?:-playbook)?\s+[^;|\n]*--tags\s+[^'\s]+cortex[^'\s]*", re.IGNORECASE),
]
# ansible-pull: -t with comma-separated list containing cortex, or -t "$TAGS" with TAGS=...cortex...
ANSIBLE_PULL_cortex_PATTERNS = [
    re.compile(r"ansible-pull\s+[^;|\n]*-t\s+[^;|\n]*\bcortex\b", re.IGNORECASE),
    re.compile(r"ansible-pull\s+[^;|\n]*-t\s+[\"']?\$TAGS[\"']?", re.IGNORECASE),
]
TAGS_CONTAINS_cortex = re.compile(r"\bTAGS\s*=\s*[^;\n]*\bcortex\b", re.IGNORECASE)

# Branch extraction: --checkout on same line as ansible-pull (preferred), or BRANCH= / other patterns
CHECKOUT_ON_LINE = re.compile(r"--checkout\s+(?:[\"']?)([^\"'\s]+)(?:[\"']?)", re.IGNORECASE)
BRANCH_PATTERNS = [
    re.compile(r"-e\s+(?:['\"]?)branch=([^\s'\"]+)", re.IGNORECASE),
    re.compile(r"--extra-vars\s+(?:['\"]?).*?\bbranch=([^\s'\"]+)", re.IGNORECASE),
    re.compile(r"\b(?:GIT_)?BRANCH\s*=\s*['\"]?([^'\";\s\n#]+)['\"]?", re.IGNORECASE),
    re.compile(r"git\s+checkout\s+(?:-b\s+)?['\"]?([^\s'\"]+)['\"]?", re.IGNORECASE),
    re.compile(r"git\s+clone\s+[^;|\n]*-b\s+['\"]?([^\s'\"]+)['\"]?", re.IGNORECASE),
    re.compile(r"ansible_branch\s*=\s*['\"]?([^'\";\s\n]+)['\"]?", re.IGNORECASE),
]


def load_config(path: str) -> dict:
    """Parse bash-style config (KEY=value and KEY=( "v1" "v2" ))."""
    config = {}
    with open(path) as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip().rstrip()
        if "#" in rest and not rest.startswith("("):
            rest = rest.split("#", 1)[0].strip().rstrip()
        if rest.startswith("("):
            arr_parts = [rest]
            depth = rest.count("(") - rest.count(")")
            i += 1
            while i < len(lines) and depth > 0:
                arr_parts.append(lines[i])
                depth += lines[i].count("(") - lines[i].count(")")
                i += 1
            arr_str = " ".join(arr_parts)
            config[key] = re.findall(r'["\']([^"\']*)["\']', arr_str)
            continue
        if rest.startswith('"') and rest.endswith('"'):
            config[key] = rest[1:-1].replace('\\"', '"')
        elif rest.startswith("'") and rest.endswith("'"):
            config[key] = rest[1:-1]
        else:
            config[key] = rest
        i += 1
    return config


def assume_role(role_arn: str, session_name: str | None = None) -> dict:
    """Assume role and return credentials dict."""
    import time
    session_name = session_name or f"cortex_branches_{int(time.time())}"
    result = subprocess.run(
        [
            "aws", "sts", "assume-role",
            "--role-arn", role_arn,
            "--role-session-name", session_name,
            "--output", "json",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"❌ assume-role failed for {role_arn}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(result.stdout)
    return data["Credentials"]


def apply_credentials(creds: dict) -> None:
    os.environ["AWS_ACCESS_KEY_ID"] = creds["AccessKeyId"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = creds["SecretAccessKey"]
    os.environ["AWS_SESSION_TOKEN"] = creds["SessionToken"]


def decode_userdata(raw: str | bytes | None) -> str:
    """Decode EC2 user data (base64, optionally gzip)."""
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            raw = base64.b64decode(raw, validate=True)
        except Exception:
            return ""
    # AWS sometimes uses base64(gzip(data))
    if isinstance(raw, bytes) and raw[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                return gz.read().decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def has_subscription_ansible(userdata: str) -> bool:
    return any(repo in userdata for repo in REPO_NAMES)


def has_cortex_role_in_ansible(userdata: str) -> bool:
    """True if userdata contains ansible/ansible-playbook/ansible-pull with cortex tag."""
    for pat in ANSIBLE_cortex_PATTERNS:
        if pat.search(userdata):
            return True
    for pat in ANSIBLE_PULL_cortex_PATTERNS:
        if pat.search(userdata):
            return True
    # ansible-pull -t "$TAGS" with TAGS=...cortex... elsewhere in script
    if TAGS_CONTAINS_cortex.search(userdata) and re.search(r"ansible-pull\s+[^;|\n]*-t\s+\$TAGS", userdata, re.IGNORECASE):
        return True
    return False


def extract_branches(userdata: str) -> list[str]:
    """
    Extract branch names used when cortex is run.
    Prefers --checkout on the same line as ansible-pull with cortex; else BRANCH= / other patterns.
    """
    branches = []
    # 1) Branches from the line that runs ansible-pull with cortex (--checkout is the branch for that run)
    for line in userdata.splitlines():
        if "ansible-pull" not in line or "cortex" not in line and "-t" not in line:
            # Also consider line with -t $TAGS (TAGS set elsewhere with cortex)
            if "-t" in line and "$TAGS" in line and TAGS_CONTAINS_cortex.search(userdata):
                pass
            else:
                continue
        if TAGS_CONTAINS_cortex.search(userdata) and re.search(r"-t\s+\$TAGS", line, re.IGNORECASE):
            pass
        for m in CHECKOUT_ON_LINE.finditer(line):
            b = m.group(1).strip().strip("'\"")
            if b in ("$BRANCH", "BRANCH"):
                # Resolve from BRANCH= in userdata
                br = re.search(r"\bBRANCH\s*=\s*['\"]?([^'\";\s\n#]+)['\"]?", userdata, re.IGNORECASE)
                if br:
                    b = br.group(1).strip().strip("'\"")
                else:
                    continue
            if b and b not in branches:
                branches.append(b)
    # 2) If no branch from cortex line, use BRANCH= (common when ansible-pull uses --checkout "$BRANCH")
    if not branches:
        for pat in BRANCH_PATTERNS:
            for m in pat.finditer(userdata):
                b = m.group(1).strip().strip("'\"")
                if b and b not in branches:
                    branches.append(b)
    return branches


def get_ec2_regions(all_regions: bool) -> list[str]:
    if all_regions:
        try:
            r = subprocess.run(
                ["aws", "ec2", "describe-regions", "--query", "Regions[].RegionName", "--output", "text"],
                capture_output=True,
                text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return [x.strip() for x in r.stdout.strip().split()]
        except Exception:
            pass
        # Fallback: common regions
        return [
            "ap-south-1", "ap-south-2", "ap-southeast-1", "ap-southeast-2",
            "ap-northeast-1", "ap-northeast-2", "eu-west-1", "eu-central-1",
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
        ]
    return []


def get_instances_and_userdata(region: str, ec2_client) -> list[tuple[dict, str]]:
    """Return list of (instance_dict, decoded_userdata) for all instances in region."""
    paginator = ec2_client.get_paginator("describe_instances")
    results = []
    for page in paginator.paginate():
        for res in page.get("Reservations") or []:
            for inst in res.get("Instances") or []:
                instance_id = inst.get("InstanceId")
                if not instance_id:
                    continue
                try:
                    att = ec2_client.describe_instance_attribute(
                        InstanceId=instance_id,
                        Attribute="userData",
                    )
                    ud = att.get("UserData", {}).get("Value")
                except Exception:
                    ud = None
                decoded = decode_userdata(ud) if ud else ""
                results.append((inst, decoded))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List branches used with cortex role in subscription-ansible across EC2 user data",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"), help="AWS region (default: AWS_REGION env)")
    parser.add_argument("--all-regions", action="store_true", help="Scan all EC2 regions in the account")
    parser.add_argument("--config", metavar="FILE", help="Config file with ROLE_ARN or ACCOUNT_ROLES for assume-role")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print instance IDs and branches per instance")
    args = parser.parse_args()

    account_roles = []
    if args.config:
        if not Path(args.config).is_file():
            print(f"❌ Config file not found: {args.config}", file=sys.stderr)
            sys.exit(1)
        config = load_config(args.config)
        account_roles = config.get("ACCOUNT_ROLES") or config.get("ROLE_ARN") or []
        if isinstance(account_roles, str):
            account_roles = [account_roles]
        account_roles = [r.strip() for r in account_roles if r.strip()]

    if not account_roles:
        account_roles = [None]  # use default credentials

    all_branches = set()
    branch_to_instances = defaultdict(list)
    regions_to_scan = get_ec2_regions(args.all_regions) if args.all_regions else [args.region]

    for role_arn in account_roles:
        if role_arn:
            creds = assume_role(role_arn)
            apply_credentials(creds)
            account_id = role_arn.split(":")[4] if ":" in role_arn else role_arn
            print(f"📦 Account (role): {account_id}", file=sys.stderr)
        else:
            account_id = "default"

        for region in regions_to_scan:
            try:
                ec2 = boto3.client("ec2", region_name=region)
            except Exception as e:
                print(f"⚠️ Skip region {region}: {e}", file=sys.stderr)
                continue

            instances_with_ud = get_instances_and_userdata(region, ec2)
            if not instances_with_ud:
                continue

            for inst, userdata in instances_with_ud:
                if not has_subscription_ansible(userdata):
                    continue
                if not has_cortex_role_in_ansible(userdata):
                    continue
                branches = extract_branches(userdata)
                if not branches:
                    branches = ["<unknown>"]
                iid = inst.get("InstanceId", "")
                name = next((t["Value"] for t in (inst.get("Tags") or []) if t.get("Key") == "Name"), "")
                for b in branches:
                    all_branches.add(b)
                    branch_to_instances[b].append((account_id, region, iid, name))

                if args.verbose:
                    print(f"  {region} {iid} {name}: branches {branches}", file=sys.stderr)

    # Output: unique branches used when cortex role is used with subscription-ansible
    print("Branches used when role cortex (-t cortex) is used with subscription-ansible:")
    print("-" * 60)
    for branch in sorted(all_branches):
        count = len(branch_to_instances.get(branch, []))
        print(f"  {branch}\t({count} instance(s))")
    if not all_branches:
        print("  (none found)")

    if args.verbose and branch_to_instances:
        print("\nPer-branch instance summary:", file=sys.stderr)
        for b in sorted(branch_to_instances.keys()):
            for acc, reg, iid, name in branch_to_instances[b][:5]:
                print(f"  {b}: {acc} {reg} {iid} {name}", file=sys.stderr)
            if len(branch_to_instances[b]) > 5:
                print(f"  ... and {len(branch_to_instances[b]) - 5} more", file=sys.stderr)


if __name__ == "__main__":
    main()
