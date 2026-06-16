#!/bin/bash
set -euo pipefail

# IAM User to attach policy to
IAM_ROLE="${IAM_ROLE:?Set IAM_ROLE}"

# Inline policy name
POLICY_NAME="${POLICY_NAME:?Set POLICY_NAME}"

# Create a temporary JSON policy file (safer than inline JSON string)
POLICY_FILE="$(mktemp)"
cat > "$POLICY_FILE" <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "VisualEditor0",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeSecurityGroupRules",
                "ec2:DescribeSecurityGroupReferences",
                "ec2:DescribeVpcPeeringConnections",
                "ec2:DescribeSecurityGroupVpcAssociations",
                "ec2:DescribeSubnets",
                "ec2:DescribeRouteTables",
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeVpcs"
            ],
            "Resource": "*"
        }
    ]
}
EOF

# Load SSO helper script that contains login_aws + cleanup_aws_config
SSO_HELPER="${SSO_HELPER_SCRIPT:?Set SSO_HELPER_SCRIPT to your saml/sso helper script}"
source "$SSO_HELPER"

# Run everything inside a function so sourcing does not exit the script
run_main() {

    cleanup_aws_config

    echo "Fetching AWS account list..."
    account_list=$(list_accounts)

    accounts=$(echo "$account_list" | grep -E "^[0-9]+ " | awk '{print $1, $3}')

    echo "Accounts found:"
    echo "$accounts"
    echo

    while read -r line_num account_id; do
        echo "========================================"
        echo "Switching to account #$line_num ($account_id)"
        echo "========================================"

        # Try logging in
        if ! login_aws "$line_num" >/dev/null 2>&1; then
            echo "❌ ERROR: Failed to login to account $account_id — skipping..."
            echo
            continue
        fi

        echo "Checking if inline policy already exists in $account_id ..."

        # Check if policy exists
        if aws iam get-role-policy \
            --role-name "$IAM_ROLE" \
            --policy-name "$POLICY_NAME" >/dev/null 2>&1; then

            echo "⚠️ Policy '$POLICY_NAME' already exists in account $account_id — skipping..."
            echo
            continue
        fi

        echo "Creating inline policy in $account_id ..."

        # Attempt to create the policy
        if ! aws iam put-role-policy \
                --role-name "$IAM_ROLE" \
                --policy-name "$POLICY_NAME" \
                --policy-document "file://$POLICY_FILE" >/dev/null 2>&1; then

            echo "❌ ERROR: Failed to apply policy in account $account_id — skipping..."
            echo
            continue
        fi

        echo "✔ Policy created successfully in account $account_id"
        echo

    done <<< "$accounts"

    echo "🎉 DONE — Inline policy applied to all AWS accounts where needed."
}
# Run the main logic
run_main

