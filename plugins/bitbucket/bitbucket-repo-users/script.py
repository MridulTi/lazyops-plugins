#!/usr/bin/env python3
"""
List users and their repo access for Bitbucket Cloud repositories where
the authenticated user has admin access.

Output:
  - Console summary
  - JSON file: bitbucket_repo_users_access.json
  - CSV file:  bitbucket_repo_users_access.csv

Required environment variables:
  BITBUCKET_EMAIL
  BITBUCKET_TOKEN

Recommended token scopes:
  read:user:bitbucket
  read:workspace:bitbucket
  read:repository:bitbucket
  read:permission:bitbucket

Usage:
  python bitbucket_repo_users.py
"""

import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

import requests
from requests.auth import HTTPBasicAuth

API = "https://api.bitbucket.org/2.0"
TIMEOUT = 30
MAX_WORKERS = 15


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing environment variable: {name}")
    return value


EMAIL = require_env("BITBUCKET_EMAIL")
TOKEN = require_env("BITBUCKET_TOKEN")
AUTH = HTTPBasicAuth(EMAIL, TOKEN)
HEADERS = {"Accept": "application/json"}


def get(url: str) -> requests.Response:
    return requests.get(url, auth=AUTH, headers=HEADERS, timeout=TIMEOUT)


def get_all(url: str) -> Tuple[List[Dict], int]:
    items: List[Dict] = []
    while url:
        resp = get(url)
        if resp.status_code != 200:
            return items, resp.status_code
        body = resp.json()
        items.extend(body.get("values", []))
        url = body.get("next")
    return items, 200


def fetch_repos() -> List[Tuple[str, str, str]]:
    workspaces, ws_status = get_all(f"{API}/user/workspaces?pagelen=100")
    if ws_status != 200:
        sys.exit(f"Failed to fetch workspaces (status={ws_status})")

    repos: List[Tuple[str, str, str]] = []
    for ws in workspaces:
        slug = ws["workspace"]["slug"]
        ws_repos, repo_status = get_all(f"{API}/repositories/{slug}?pagelen=100")
        if repo_status != 200:
            print(f"Skipping workspace '{slug}' (repo list status={repo_status})")
            continue
        for repo in ws_repos:
            repos.append((slug, repo["slug"], repo["full_name"]))
    return repos


def fetch_repo_users(repo_item: Tuple[str, str, str]) -> Dict:
    workspace, repo_slug, full_name = repo_item
    url = f"{API}/repositories/{workspace}/{repo_slug}/permissions-config/users?pagelen=100"
    users, status = get_all(url)

    if status == 403:
        return {
            "full_name": full_name,
            "workspace": workspace,
            "repo_slug": repo_slug,
            "admin_access": False,
            "status_code": 403,
            "users": [],
        }
    if status != 200:
        return {
            "full_name": full_name,
            "workspace": workspace,
            "repo_slug": repo_slug,
            "admin_access": False,
            "status_code": status,
            "users": [],
        }

    repo_users = []
    for item in users:
        user = item.get("user", {}) or {}
        # Bitbucket Cloud usually does NOT expose other users' email addresses
        # in this endpoint unless explicitly available in your org configuration.
        email = (
            user.get("email")
            or user.get("email_address")
            or item.get("email")
            or None
        )
        repo_users.append(
            {
                "display_name": user.get("display_name"),
                "nickname": user.get("nickname"),
                "uuid": user.get("uuid"),
                "account_id": user.get("account_id"),
                "email": email,
                "access": item.get("permission"),
            }
        )

    return {
        "full_name": full_name,
        "workspace": workspace,
        "repo_slug": repo_slug,
        "admin_access": True,
        "status_code": 200,
        "users": sorted(repo_users, key=lambda u: (u.get("display_name") or "", u.get("uuid") or "")),
    }


def main() -> None:
    me_resp = get(f"{API}/user")
    if me_resp.status_code != 200:
        sys.exit(f"Authentication failed (status={me_resp.status_code})")
    me = me_resp.json()
    print(f"Authenticated as: {me.get('display_name')} ({me.get('uuid')})")

    print("Fetching repositories...")
    repos = fetch_repos()
    print(f"Total repos discovered: {len(repos)}")

    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_repo_users, repo) for repo in repos]
        for future in as_completed(futures):
            results.append(future.result())

    admin_repos = [r for r in results if r["admin_access"]]
    skipped_repos = [r for r in results if not r["admin_access"]]

    print(f"Repos with admin access: {len(admin_repos)}")
    print(f"Repos skipped (non-admin or errors): {len(skipped_repos)}")

    out_json = "bitbucket_repo_users_access.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(
            {
                "authenticated_user": {
                    "display_name": me.get("display_name"),
                    "uuid": me.get("uuid"),
                },
                "admin_repo_count": len(admin_repos),
                "skipped_repo_count": len(skipped_repos),
                "repos": sorted(admin_repos, key=lambda r: r["full_name"]),
                "skipped": sorted(skipped_repos, key=lambda r: r["full_name"]),
            },
            f,
            indent=2,
        )

    out_csv = "bitbucket_repo_users_access.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "repo_full_name",
                "workspace",
                "repo_slug",
                "user_display_name",
                "user_nickname",
                "user_uuid",
                "user_account_id",
                "user_email",
                "access",
            ]
        )
        for repo in sorted(admin_repos, key=lambda r: r["full_name"]):
            for user in repo["users"]:
                writer.writerow(
                    [
                        repo["full_name"],
                        repo["workspace"],
                        repo["repo_slug"],
                        user.get("display_name"),
                        user.get("nickname"),
                        user.get("uuid"),
                        user.get("account_id"),
                        user.get("email"),
                        user.get("access"),
                    ]
                )

    print(f"Wrote JSON: {out_json}")
    print(f"Wrote CSV : {out_csv}")
    print("Note: user email is often unavailable from Bitbucket Cloud APIs for other users.")


if __name__ == "__main__":
    main()
