#!/usr/bin/env python3

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

PR_LINKS = []
PR_LOCK = threading.Lock()

BB_USER = os.getenv("BB_USER")
BB_TOKEN = os.getenv("BB_TOKEN")

if not BB_USER or not BB_TOKEN:
    print("ERROR: Set BB_USER and BB_TOKEN")
    sys.exit(1)

AUTH = (BB_USER, BB_TOKEN)
HEADERS = {"Content-Type": "application/json"}


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        cfg = json.load(f)
    required = [
        "bitbucket_workspace",
        "repositories",
        "old_string",
        "new_string",
        "new_branch_prefix",
    ]
    missing = [k for k in required if k not in cfg]
    if missing:
        sys.exit(f"Config missing keys: {', '.join(missing)}")
    cfg.setdefault("excluded_repos", [])
    cfg.setdefault("excluded_git_url", "")
    cfg.setdefault("target_branches", [])
    cfg.setdefault("max_repo_workers", 5)
    cfg.setdefault("max_branch_workers", 5)
    return cfg

BASE_API = ""
CONFIG: dict = {}


def run(cmd, cwd=None):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise Exception(
            f"\nCOMMAND FAILED:\n{cmd}\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )

    return result.stdout.strip()


def get_repo_name(repo_url):
    return repo_url.split("/")[-1].replace(".git", "")


def branch_exists(repo, branch):
    url = f"{BASE_API}/{repo}/refs/branches/{branch}"

    response = requests.get(url, auth=AUTH)

    return response.status_code == 200


def create_pr(repo, source_branch, target_branch):
    url = f"{BASE_API}/{repo}/pullrequests"

    payload = {
        "title": f"Replace {CONFIG['old_string']} with {CONFIG['new_string']}",
        "source": {
            "branch": {
                "name": source_branch
            }
        },
        "destination": {
            "branch": {
                "name": target_branch
            }
        },
        "description": (
            f"Automated replacement of "
            f"`{CONFIG['old_string']}` with `{CONFIG['new_string']}`"
        ),
        "close_source_branch": True
    }

    response = requests.post(
        url,
        auth=AUTH,
        headers=HEADERS,
        data=json.dumps(payload)
    )

    if response.status_code not in [200, 201]:
        raise Exception(
            f"PR creation failed for {repo}:{target_branch}\n"
            f"{response.text}"
        )

    pr_data = response.json()

    return pr_data["links"]["html"]["href"]


def process_branch(repo_url, repo, branch):
    temp_dir = tempfile.mkdtemp(
        prefix=f"{repo}-{branch.replace('/', '-')}-"
    )

    try:
        print(f"[INFO] Processing {repo}:{branch}")

        clone_cmd = (
            f"GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' "
            f"git clone "
            f"--single-branch "
            f"--branch '{branch}' "
            f"'{repo_url}' repo"
        )

        run(clone_cmd, cwd=temp_dir)

        repo_path = os.path.join(temp_dir, "repo")

        modified = False

        for root, dirs, files in os.walk(repo_path):

            # Skip .git directory
            dirs[:] = [d for d in dirs if d != ".git"]

            for file in files:
                file_path = os.path.join(root, file)

                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()

                except Exception:
                    # Skip binary/non-text files
                    continue

                if CONFIG['old_string'] in content:

                    # -----------------------------------------
                    # PROTECT verifier-k8s URL
                    # -----------------------------------------
                    new_content = content.replace(
                        CONFIG['excluded_git_url'],
                        "__VERIFIER_K8S_TEMP__"
                    )

                    # -----------------------------------------
                    # MAIN REPLACEMENT
                    # -----------------------------------------
                    new_content = new_content.replace(
                        CONFIG['old_string'],
                        CONFIG['new_string']
                    )

                    # -----------------------------------------
                    # RESTORE verifier-k8s URL
                    # -----------------------------------------
                    new_content = new_content.replace(
                        "__VERIFIER_K8S_TEMP__",
                        CONFIG['excluded_git_url']
                    )

                    if new_content != content:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(new_content)

                        modified = True

        if not modified:
            print(f"[NO CHANGE] {repo}:{branch}")
            return

        new_branch = (
            f"{CONFIG['new_branch_prefix']}-"
            f"{branch.replace('/', '-')}"
        )

        run(f"git checkout -b '{new_branch}'", cwd=repo_path)

        run("git add .", cwd=repo_path)

        commit_msg = (
            f"Replace {CONFIG['old_string']} with {CONFIG['new_string']}"
        )

        run(
            f"git commit -m '{commit_msg}'",
            cwd=repo_path
        )

        run(
            f"GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' "
            f"git push origin '{new_branch}'",
            cwd=repo_path
        )

        pr_url = create_pr(
            repo=repo,
            source_branch=new_branch,
            target_branch=branch
        )

        print(f"[PR CREATED] {repo}:{branch}")
        print(pr_url)

        with PR_LOCK:
            PR_LINKS.append({
                "repo": repo,
                "branch": branch,
                "pr": pr_url
            })

    except Exception as e:
        print(f"[ERROR] {repo}:{branch}")
        print(str(e))

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def process_repo(repo_url):
    try:
        repo = get_repo_name(repo_url)

        if repo_url.strip() in CONFIG['excluded_repos']:
            print(f"[SKIPPED] {repo}")
            return

        print(f"\n========== {repo} ==========")

        valid_branches = []

        for branch in CONFIG['target_branches']:
            if branch_exists(repo, branch):
                valid_branches.append(branch)
            else:
                print(f"[SKIPPED] Branch does not exist: {repo}:{branch}")

        if not valid_branches:
            print(f"[INFO] No valid branches found for {repo}")
            return

        print(f"[INFO] Target branches: {valid_branches}")

        with ThreadPoolExecutor(
            max_workers=CONFIG['max_branch_workers']
        ) as executor:

            futures = [
                executor.submit(
                    process_branch,
                    repo_url,
                    repo,
                    branch
                )
                for branch in valid_branches
            ]

            for future in as_completed(futures):
                future.result()

    except Exception as e:
        print(f"[ERROR] Repo processing failed")
        print(str(e))


# =========================================================
# MAIN
# =========================================================

def main(cfg):
    global BASE_API, CONFIG
    CONFIG = cfg
    BASE_API = f"https://api.bitbucket.org/2.0/repositories/{CONFIG['bitbucket_workspace']}"
    repos = list(set(CONFIG['repositories']))

    print(f"\nTotal repos configured: {len(repos)}")

    with ThreadPoolExecutor(
        max_workers=CONFIG['max_repo_workers']
    ) as executor:

        futures = [
            executor.submit(process_repo, repo_url)
            for repo_url in repos
        ]

        for future in as_completed(futures):
            future.result()

    print("\n==============================")
    print("ALL CREATED PR LINKS")
    print("==============================\n")

    for item in PR_LINKS:
        print(
            f"{item['repo']} | "
            f"{item['branch']} | "
            f"{item['pr']}"
        )

    print("\nDONE")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: script.py <config.json>")
        sys.exit(1)
    main(load_config(sys.argv[1]))
