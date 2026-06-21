#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-${1:-}}"
if [[ -z "$DOMAIN" ]]; then echo "Set DOMAIN or pass as first argument" >&2; exit 1; fi
REGION="$(aws configure get region)"

if [[ -z "$REGION" ]]; then
  echo "❌ AWS region is not set"
  exit 1
fi

echo "🌍 Region: $REGION"
echo "🔐 Looking for ACM certificate: $DOMAIN"

CERT_ARN=$(aws acm list-certificates \
  --region "$REGION" \
  --certificate-statuses ISSUED \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" \
  --output text)

if [[ -z "$CERT_ARN" || "$CERT_ARN" == "None" ]]; then
  echo "❌ Certificate '$DOMAIN' not found in ACM for region $REGION"
  echo "➡️ Nothing to do. Exiting."
  exit 0
fi

echo "✅ Certificate found: $CERT_ARN"
echo

LB_ARNS=$(aws elbv2 describe-load-balancers \
  --region "$REGION" \
  --query "LoadBalancers[?Type=='application'].LoadBalancerArn" \
  --output text)

if [[ -z "$LB_ARNS" ]]; then
  echo "ℹ️ No Application Load Balancers found"
  exit 0
fi

for LB_ARN in $LB_ARNS; do
  LB_NAME=$(aws elbv2 describe-load-balancers \
    --region "$REGION" \
    --load-balancer-arns "$LB_ARN" \
    --query "LoadBalancers[0].LoadBalancerName" \
    --output text)

  echo "➡️ ALB: $LB_NAME"

  LISTENER_ARNS=$(aws elbv2 describe-listeners \
    --region "$REGION" \
    --load-balancer-arn "$LB_ARN" \
    --query "Listeners[?Protocol=='HTTPS'].ListenerArn" \
    --output text)

  if [[ -z "$LISTENER_ARNS" ]]; then
    echo "   ⏭ No HTTPS listeners"
    continue
  fi

  for LISTENER_ARN in $LISTENER_ARNS; do
    if aws elbv2 describe-listeners \
      --region "$REGION" \
      --listener-arns "$LISTENER_ARN" \
      --query "Listeners[0].Certificates[].CertificateArn" \
      --output text | tr '\t' '\n' | grep -Fxq "$CERT_ARN"; then
      echo "   ✅ Certificate already attached"
      continue
    fi

    echo "   ➕ Adding certificate"
    aws elbv2 add-listener-certificates \
      --region "$REGION" \
      --listener-arn "$LISTENER_ARN" \
      --certificates CertificateArn="$CERT_ARN"

    echo "   🎉 Certificate added"
  done

  echo
done

echo "✅ Completed successfully"

