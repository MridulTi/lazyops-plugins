#!/bin/bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <ssh-base-url> <repos-file> <clone-dir>"
  echo "Example: $0 git@bitbucket.org:myworkspace repos.txt ./repos"
  exit 1
fi

BASE_SSH_URL="${1%/}"
INPUT_FILE="$2"
CLONE_DIR="$3"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "File not found: $INPUT_FILE" >&2
  exit 1
fi

mkdir -p "$CLONE_DIR"
cd "$CLONE_DIR" || exit 1

while IFS= read -r repo || [[ -n "$repo" ]]; do
  [[ -z "$repo" || "$repo" =~ ^# ]] && continue
  REPO_URL="${BASE_SSH_URL}/${repo}.git"
  if [[ -d "$repo" ]]; then
    echo "Skipping $repo (already exists)"
    continue
  fi
  echo "Cloning $repo..."
  git clone "$REPO_URL" && echo "Cloned $repo" || echo "Failed $repo"
done < "$INPUT_FILE"

echo "Done"
