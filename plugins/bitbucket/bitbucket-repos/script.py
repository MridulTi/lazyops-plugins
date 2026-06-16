#!/usr/bin/env python3
"""
List Bitbucket Cloud repos you can access, your repo-level permission,
and the branch-level restrictions (where readable).

How Bitbucket branch permissions actually work:
  Repos are write-open by default. Admins add RESTRICTIONS like
  "no one can push to master except Alice". So per-branch permission
  for you = your repo permission MINUS any restriction you're not
  exempted from.

Limitation:
  GET /branch-restrictions requires repo admin. For non-admin repos
  the API returns 403 and we cannot read the rules. The script writes
  "unreadable" in that case.

Required token scopes:
  read:user:bitbucket
  read:workspace:bitbucket
  read:repository:bitbucket
  read:permission:bitbucket

Setup:
  export BITBUCKET_EMAIL="your_atlassian_email@example.com"
  export BITBUCKET_TOKEN="your_api_token"
  pip install requests
  python bitbucket_repos.py
  # output is also written to bitbucket_access.json
"""

import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPBasicAuth

email = os.getenv("BITBUCKET_EMAIL")
token = os.getenv("BITBUCKET_TOKEN")
if not email or not token:
    sys.exit("Set BITBUCKET_EMAIL and BITBUCKET_TOKEN environment variables.")

auth = HTTPBasicAuth(email, token)
headers = {"Accept": "application/json"}
API = "https://api.bitbucket.org/2.0"


def get(url):
    return requests.get(url, auth=auth, headers=headers, timeout=30)


def get_all(url):
    items = []
    while url:
        r = get(url)
        if r.status_code != 200:
            return items, r.status_code
        data = r.json()
        items.extend(data.get("values", []))
        url = data.get("next")
    return items, 200


# 0. Who am I? Need my UUID to check exemptions.
me = get(f"{API}/user").json()
my_uuid = me["uuid"]
print(f"Authenticated as: {me['display_name']}  uuid={my_uuid}\n")

# 1. Workspaces.
print("Fetching workspaces...")
workspaces, _ = get_all(f"{API}/user/workspaces?pagelen=100")
slugs = [w["workspace"]["slug"] for w in workspaces]
print(f"Found {len(slugs)} workspace(s): {slugs}\n")

# 2. Repos.
all_repos = []  # (workspace, slug, full_name)
for slug in slugs:
    print(f"Fetching repos in '{slug}'...")
    repos, _ = get_all(f"{API}/repositories/{slug}?pagelen=100")
    print(f"  -> {len(repos)} repos")
    for r in repos:
        all_repos.append((slug, r["slug"], r["full_name"]))


# 3. For each repo: infer repo-level permission AND fetch branch restrictions.
def analyze(item):
    workspace, repo_slug, full_name = item
    result = {
        "full_name": full_name,
        "repo_permission": "read",      # default lower bound
        "branch_rules": "unreadable",   # default
        "branches": {},                 # branch/pattern -> {kind: allowed?}
    }

    # Try to fetch branch restrictions. Success -> admin. Failure -> not admin.
    r = get(f"{API}/repositories/{workspace}/{repo_slug}/branch-restrictions?pagelen=100")
    if r.status_code == 200:
        result["repo_permission"] = "admin"
        rules = r.json().get("values", [])

        # Walk pagination.
        next_url = r.json().get("next")
        while next_url:
            rr = get(next_url)
            if rr.status_code != 200:
                break
            rules.extend(rr.json().get("values", []))
            next_url = rr.json().get("next")

        # Group rules by branch pattern.
        per_branch = {}
        for rule in rules:
            pattern = rule.get("pattern") or f"<{rule.get('branch_type', '?')}>"
            kind = rule["kind"]  # push, restrict_merges, force, delete, etc.

            # Am I exempted? (push and restrict_merges only)
            exempt_uuids = {u.get("uuid") for u in rule.get("users", [])}
            allowed = None
            if kind in ("push", "restrict_merges"):
                allowed = my_uuid in exempt_uuids
                # Note: group exemptions not resolved here — would need an
                # extra call per group. If False here but you're in an
                # exempted group, you actually do have access.

            per_branch.setdefault(pattern, {})[kind] = {
                "exempted_users": list(exempt_uuids),
                "exempted_groups": [g.get("slug") for g in rule.get("groups", [])],
                "you_allowed": allowed,
            }
        result["branch_rules"] = "readable"
        result["branches"] = per_branch
        return result

    # Not admin. Try the workspace-permissions probe to distinguish write vs read.
    r2 = get(f"{API}/workspaces/{workspace}/permissions/repositories/{repo_slug}")
    if r2.status_code == 200:
        result["repo_permission"] = "write"
    # else stays as "read"
    return result


print(f"\nAnalyzing {len(all_repos)} repos...")
with ThreadPoolExecutor(max_workers=15) as pool:
    analyses = list(pool.map(analyze, all_repos))

# 4. Print summary table + branch detail for repos where we can see it.
print(f"\n{'='*80}\nREPO-LEVEL PERMISSIONS\n{'='*80}")
for a in sorted(analyses, key=lambda x: x["full_name"]):
    print(f"  {a['full_name']:<60} [{a['repo_permission']:<5}] "
          f"branch_rules={a['branch_rules']}")

print(f"\n{'='*80}\nBRANCH-LEVEL RESTRICTIONS (admin repos only)\n{'='*80}")
for a in sorted(analyses, key=lambda x: x["full_name"]):
    if not a["branches"]:
        continue
    print(f"\n{a['full_name']}")
    for pattern, kinds in a["branches"].items():
        print(f"  pattern: {pattern}")
        for kind, info in kinds.items():
            you = info["you_allowed"]
            you_str = ("you=ALLOWED" if you is True
                       else "you=BLOCKED" if you is False
                       else "you=N/A")
            print(f"    {kind:<25} {you_str}  "
                  f"exempt_groups={info['exempted_groups']}")

# Summary counts.
counts = Counter(a["repo_permission"] for a in analyses)
print(f"\n{'='*80}\nSummary: {dict(counts)}")

# 5. Write full data to JSON for further processing.
out_path = "bitbucket_access.json"
with open(out_path, "w") as f:
    json.dump(analyses, f, indent=2)
print(f"Full data written to {out_path}")