#!/bin/bash

# ------------------------------------------------------
# Compare ALB Rules (Path Patterns) between two regions
# ------------------------------------------------------
# Usage:
#   ./compare_lb_rules_cross_region.sh <prod-lb-name> <prod-region> <dr-lb-name> <dr-region>
#
# Example:
#   ./compare_lb_rules_cross_region.sh prod-lb ap-south-1 dr-lb ap-south-2
# ------------------------------------------------------

PROD_LB="$1"
PROD_REGION="$2"
DR_LB="$3"
DR_REGION="$4"

if [[ -z "$PROD_LB" || -z "$PROD_REGION" || -z "$DR_LB" || -z "$DR_REGION" ]]; then
  echo "Usage: $0 <prod-lb-name> <prod-region> <dr-lb-name> <dr-region>"
  exit 1
fi

TMP_DIR=$(mktemp -d)
PROD_PATHS="$TMP_DIR/prod_paths.txt"
DR_PATHS="$TMP_DIR/dr_paths.txt"

echo "🔍 Comparing ALB path-pattern rules across regions:"
echo "   🟢 PROD: $PROD_LB ($PROD_REGION)"
echo "   🔵 DR:   $DR_LB ($DR_REGION)"
echo "----------------------------------------------------------"

# Function to extract path-patterns from a given ALB
get_paths() {
  local LB_NAME="$1"
  local REGION="$2"
  local OUT_FILE="$3"

  LB_ARN=$(aws elbv2 describe-load-balancers \
    --names "$LB_NAME" \
    --region "$REGION" \
    --query "LoadBalancers[0].LoadBalancerArn" \
    --output text 2>/dev/null)

  if [[ "$LB_ARN" == "None" || -z "$LB_ARN" ]]; then
    echo "❌ Load balancer '$LB_NAME' not found in region '$REGION'!"
    exit 1
  fi

  LISTENERS=$(aws elbv2 describe-listeners \
    --load-balancer-arn "$LB_ARN" \
    --region "$REGION" \
    --query "Listeners[].ListenerArn" \
    --output text)

  : > "$OUT_FILE"

  for L in $LISTENERS; do
    aws elbv2 describe-rules \
      --listener-arn "$L" \
      --region "$REGION" \
      --output json | jq -r '
        .Rules[].Conditions[]
        | select(.Field=="path-pattern")
        | .Values[]
      ' >> "$OUT_FILE"
  done

  sort -u "$OUT_FILE" -o "$OUT_FILE"
}

# Fetch paths from both regions
echo "📥 Fetching paths from PROD ($PROD_REGION)..."
get_paths "$PROD_LB" "$PROD_REGION" "$PROD_PATHS"

echo "📥 Fetching paths from DR ($DR_REGION)..."
get_paths "$DR_LB" "$DR_REGION" "$DR_PATHS"

# Compare and show difference
echo "----------------------------------------------------------"
echo "🧾 Paths present in PROD but missing in DR:"
comm -23 "$PROD_PATHS" "$DR_PATHS" || echo "✅ All paths in PROD exist in DR."

# Optional summary
echo
echo "----------------------------------------------------------"
echo "📊 Summary:"
echo "  - PROD paths ($PROD_REGION): $(wc -l < "$PROD_PATHS")"
echo "  - DR paths   ($DR_REGION):   $(wc -l < "$DR_PATHS")"


