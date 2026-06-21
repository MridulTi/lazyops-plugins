#!/bin/bash

# -------------------------
# List all rules of a Load Balancer
# -------------------------

REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
if [[ -z "$REGION" ]]; then
  echo "ERROR: Set REGION or AWS_REGION" >&2
  exit 1
fi

if [ -z "$1" ]; then
  echo "Usage: $0 <load-balancer-name>"
  exit 1
fi

LB_NAME="$1"

echo "🔍 Fetching rules for Load Balancer: $LB_NAME (Region: $REGION)"
echo "-------------------------------------------------------------"

# Get Load Balancer ARN
LB_ARN=$(aws elbv2 describe-load-balancers \
  --names "$LB_NAME" \
  --region "$REGION" \
  --query "LoadBalancers[0].LoadBalancerArn" \
  --output text 2>/dev/null)

if [ "$LB_ARN" == "None" ] || [ -z "$LB_ARN" ]; then
  echo "❌ Load balancer '$LB_NAME' not found."
  exit 1
fi

# Get all Listeners
LISTENERS=$(aws elbv2 describe-listeners \
  --load-balancer-arn "$LB_ARN" \
  --region "$REGION" \
  --query "Listeners[].ListenerArn" \
  --output text)

if [ -z "$LISTENERS" ]; then
  echo "⚠️ No listeners found."
  exit 0
fi

for LISTENER_ARN in $LISTENERS; do
  echo -e "\n🎧 Listener ARN: $LISTENER_ARN"
  echo "----------------------------------------------"

  # Get all Rules for this listener
  RULES_JSON=$(aws elbv2 describe-rules \
    --listener-arn "$LISTENER_ARN" \
    --region "$REGION" \
    --output json)

  RULE_COUNT=$(echo "$RULES_JSON" | jq '.Rules | length')

  echo "📜 Found $RULE_COUNT rule(s)."

  echo "$RULES_JSON" | jq -r '
    .Rules[] |
    "Rule ARN: \(.RuleArn)\n  Priority: \(.Priority)\n  Conditions:" +
    (if (.Conditions | length) > 0 then
      (.Conditions[] | "    - Field: \(.Field)\n      Values: \(.Values // ["N/A"] | join(", "))")
     else
      "\n    - None"
     end) +
    "\n  Actions:" +
    (.Actions[] | "    - Type: \(.Type)\n      TargetGroupArn: \(.TargetGroupArn // "N/A")") +
    "\n----------------------------------------------"
  '
done

