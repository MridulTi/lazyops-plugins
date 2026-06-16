#!/bin/bash

# -------- CONFIG --------
IPS_FILE="${IPS_FILE:-$1}"
AWS_REGION="${AWS_REGION:-${AWS_AWS_REGION:-${AWS_DEFAULT_AWS_REGION:-}}}"
if [[ -z "$AWS_REGION" ]]; then
  echo "ERROR: Set AWS_REGION or AWS_AWS_REGION" >&2
  exit 1
fi

# -------- OUTPUT HOLDERS --------
QUALYS_IPS=""
CORTEX_IPS=""
NO_AGENT_IPS=""
ALL_BRANCHES=""
TAR_ASGS=""

echo "================ START ================"

while IFS= read -r IP || [[ -n "$IP" ]]; do
    [[ -z "$IP" ]] && continue

    echo "🔍 Processing $IP"

    # -------- GET INSTANCE --------
    INSTANCE_ID=$(aws ec2 describe-instances \
        --region "$AWS_REGION" \
        --query "Reservations[].Instances[?PrivateIpAddress=='$IP'].InstanceId" \
        --output text)

    if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
        echo "   ❌ Instance not found"
        continue
    fi

    # -------- GET USERDATA --------
    USERDATA=$(aws ec2 describe-instance-attribute \
        --region "$AWS_REGION" \
        --instance-id "$INSTANCE_ID" \
        --attribute userData \
        --query "UserData.Value" \
        --output text | base64 --decode 2>/dev/null)

    if [[ -z "$USERDATA" ]]; then
        echo "   ❌ No UserData"
        NO_AGENT_IPS="$NO_AGENT_IPS $IP"
        continue
    fi

    # -------- CHECK TAR-BASED SETUP --------
    if echo "$USERDATA" | grep -qi "subscriptions-ansible.*tar"; then
        echo "   ⏭️ Tar-based subscriptions-ansible detected"

        # -------- GET ASG NAME --------
        ASG_NAME=$(aws ec2 describe-instances \
            --region "$AWS_REGION" \
            --instance-ids "$INSTANCE_ID" \
            --query "Reservations[].Instances[].Tags[?Key=='aws:autoscaling:groupName'].Value" \
            --output text)

        if [[ -n "$ASG_NAME" && "$ASG_NAME" != "None" ]]; then
            echo "      ➤ ASG: $ASG_NAME"
            TAR_ASGS="$TAR_ASGS $ASG_NAME"
        else
            echo "      ➤ Not part of ASG"
        fi

        continue
    fi

    HAS_QUALYS=false
    HAS_CORTEX=false
    HAS_ANSIBLE=false

    # -------- CHECK QUALYS --------
    if echo "$USERDATA" | grep -qi "qualys"; then
        echo "   ✅ Qualys found"
        QUALYS_IPS="$QUALYS_IPS $IP"
        HAS_QUALYS=true
    fi

    # -------- CHECK CORTEX --------
    if echo "$USERDATA" | grep -qi "cortex"; then
        echo "   ✅ Cortex found"
        CORTEX_IPS="$CORTEX_IPS $IP"
        HAS_CORTEX=true
    fi

    # -------- CHECK ANSIBLE --------
    if echo "$USERDATA" | grep -qi "ansible-pull"; then
        echo "   ✅ Ansible found"
        HAS_ANSIBLE=true
    fi

    # -------- BRANCH EXTRACTION --------
    if [[ "$HAS_ANSIBLE" == true && ( "$HAS_QUALYS" == true || "$HAS_CORTEX" == true ) ]]; then

        BRANCH=$(echo "$USERDATA" | grep -i 'ansible-pull' | \
            grep -oE -- '--checkout[= ][^ ]+' | \
            sed -E 's/--checkout[= ]+//' | head -1)

        if [[ -n "$BRANCH" ]]; then
            echo "      ➤ Branch: $BRANCH"
            ALL_BRANCHES="$ALL_BRANCHES $BRANCH"
        fi
    fi

    # -------- NO MATCH --------
    if [[ "$HAS_QUALYS" == false && "$HAS_CORTEX" == false && "$HAS_ANSIBLE" == false ]]; then
        echo "   ❌ No Qualys/Cortex/Ansible"
        NO_AGENT_IPS="$NO_AGENT_IPS $IP"
    fi

done < "$IPS_FILE"

# -------- FINAL OUTPUT --------
echo ""
echo "================ FINAL RESULT ================"

echo ""
echo "✅ Qualys IPs:"
echo "$QUALYS_IPS" | tr ' ' '\n' | sort -u

echo ""
echo "✅ Cortex IPs:"
echo "$CORTEX_IPS" | tr ' ' '\n' | sort -u

echo ""
echo "❌ No Agent / No Ansible IPs:"
echo "$NO_AGENT_IPS" | tr ' ' '\n' | sort -u

echo ""
echo "📦 All branches used:"
echo "$ALL_BRANCHES" | tr ' ' '\n' | sort -u

echo ""
echo "🧩 ASGs using TAR-based subscriptions-ansible:"
echo "$TAR_ASGS" | tr ' ' '\n' | sort -u
