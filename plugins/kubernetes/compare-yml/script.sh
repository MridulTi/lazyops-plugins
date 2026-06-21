#!/bin/bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <prod-base-dir> <dr-base-dir>"
  exit 1
fi

PROD_BASE="$1"
DR_BASE="$2"

echo "Comparing nodegroup-name between PROD and DR..."
find "$PROD_BASE" -type f -name "values*.yaml" | while read -r prod_file; do
  rel_path="${prod_file#$PROD_BASE/}"
  dr_file="$DR_BASE/$rel_path"
  prod_nodegroup=$(grep -E '^[[:space:]]*nodegroup-name:' "$prod_file" | head -n1 | awk -F ':' '{print $2}' | xargs)
  if [[ ! -f "$dr_file" ]]; then
    echo "$rel_path: PROD=$prod_nodegroup DR=MISSING"
    continue
  fi
  dr_nodegroup=$(grep -E '^[[:space:]]*nodegroup-name:' "$dr_file" | head -n1 | awk -F ':' '{print $2}' | xargs)
  if [[ "$prod_nodegroup" != "$dr_nodegroup" ]]; then
    echo "$rel_path: PROD=$prod_nodegroup DR=$dr_nodegroup"
  fi
done
