#!/usr/bin/env python3

import os
import re
import sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
BB_USER = os.getenv("BB_USER")
BB_TOKEN = os.getenv("BB_TOKEN")

if not BB_USER or not BB_TOKEN:
    print("ERROR: Set BB_USER and BB_TOKEN")
    sys.exit(1)

AUTH = (BB_USER, BB_TOKEN)
HEADERS = {"Content-Type": "application/json"}


def load_pr_links(path: str) -> list[str]:
    with open(path, "r") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def parse_pr_url(pr_url: str) -> tuple[str, str, str]:
    match = re.search(
        r"https://bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)",
        pr_url,
    )
    if not match:
        raise ValueError(f"Invalid PR URL: {pr_url}")
    return match.group(1), match.group(2), match.group(3)


def decline_pr(pr_url: str) -> None:
    try:
        workspace, repo, pr_id = parse_pr_url(pr_url)
        api_url = (
            f"https://api.bitbucket.org/2.0/repositories/"
            f"{workspace}/{repo}/pullrequests/{pr_id}/decline"
        )
        response = requests.post(api_url, auth=AUTH, headers=HEADERS, json={})
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Status {response.status_code}: {response.text}")
        print(f"[DECLINED] {pr_url}")
    except Exception as e:
        print(f"[ERROR] {pr_url}: {e}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: script.py <pr-links-file>")
        sys.exit(1)

    pr_links = load_pr_links(sys.argv[1])
    if not pr_links:
        print("No PR links found in file")
        sys.exit(1)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(decline_pr, url) for url in pr_links]
        for future in as_completed(futures):
            future.result()
    print("DONE")


if __name__ == "__main__":
    main()
