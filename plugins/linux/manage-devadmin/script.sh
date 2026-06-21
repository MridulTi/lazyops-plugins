#!/bin/bash

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
SSH_USER="${SSH_USER:-ubuntu}"
DRY_RUN="${DRY_RUN:-false}"
API_BASE_URL="${API_BASE_URL:?Set API_BASE_URL}"
DEVADMIN_USER="devadmin"
DEVADMIN_GROUP="devadmin"
ROLES=("devadmin")  # Default role, can be overridden with -R option
AUTO_APPROVE="false"

# Function to print colored messages (to stderr so they don't interfere with command substitution)
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1" >&2
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Function to display usage
usage() {
    cat << EOF
Usage: $0 -k <key_file> -t <teamname> [OPTIONS]

Required:
  -k, --key FILE          Path to SSH private key file
  -t, --teamname NAME     Team name for user lookup

Optional:
  -r, --region REGION     AWS region (default: AWS_REGION env)
  -u, --ssh-user USER     SSH user (default: ubuntu)
  -T, --techteam TEAM     Tech team filter (optional)
  -R, --role ROLE         Role(s) to extract SSH keys for (can specify multiple times, default: devadmin)
  -d, --dry-run           Dry run mode (uses inventory-server.ini)
  -h, --help              Show this help message
  -y, --auto-approve      Skip confirmation prompt and proceed automatically

Examples:
  $0 -k ~/.ssh/key.pem -t MYTEAM
  $0 -k ~/.ssh/key.pem -t MYTEAM -r us-east-1
  $0 -k ~/.ssh/key.pem -t MYTEAM -d
  $0 -k ~/.ssh/key.pem -t MYTEAM -T TECH_TEAM
  $0 -k ~/.ssh/key.pem -t MYTEAM -R devadmin -R devops
  $0 -k ~/.ssh/key.pem -t MYTEAM -R devadmin -R devops -R admin
EOF
    exit 1
}

# Parse command line arguments
KEY=""
TEAMNAME=""
TECHTEAM=""
ROLES=()  # Reset to empty, will be populated or use default

while [[ $# -gt 0 ]]; do
    case $1 in
        -k|--key)
            KEY="$2"
            shift 2
            ;;
        -t|--teamname)
            TEAMNAME="$2"
            shift 2
            ;;
        -r|--region)
            AWS_REGION="$2"
            shift 2
            ;;
        -u|--ssh-user)
            SSH_USER="$2"
            shift 2
            ;;
        -T|--techteam)
            TECHTEAM="$2"
            shift 2
            ;;
        -R|--role)
            ROLES+=("$2")
            shift 2
            ;;
        -d|--dry-run)
            DRY_RUN="true"
            shift
            ;;
        -h|--help)
            usage
            ;;
        -y|--auto-approve)
            AUTO_APPROVE="true"
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            ;;
    esac
done

# If no roles specified, use default
if [[ ${#ROLES[@]} -eq 0 ]]; then
    ROLES=("devadmin")
fi

# Validate required parameters
if [[ -z "$KEY" ]] || [[ -z "$TEAMNAME" ]]; then
    log_error "key and teamname are required"
    usage
fi

# Check if key file exists
if [[ ! -f "$KEY" ]]; then
    log_error "SSH key file not found: $KEY"
    exit 1
fi

# Set key file permissions
chmod 600 "$KEY" 2>/dev/null || true

log_info "Dry-run mode: $DRY_RUN"
log_info "AWS Region: $AWS_REGION"
log_info "Team name: $TEAMNAME"
[[ -n "$TECHTEAM" ]] && log_info "Tech team: $TECHTEAM"
log_info "Roles to extract: ${ROLES[*]}"

# Function to fetch IPs from AWS
fetch_ips_from_aws() {
    log_info "Fetching EC2 instance IPs from AWS..."

    # Test AWS connectivity
    if ! aws sts get-caller-identity --region "$AWS_REGION" &>/dev/null; then
        log_error "AWS CLI test failed. Please check your AWS credentials and region configuration."
        exit 1
    fi

    # Build filter arguments (each filter must be a separate argument)
    local filter_args=(
        "Name=instance-state-name,Values=running"
    )
    if [[ -n "$TECHTEAM" ]]; then
        filter_args+=("Name=tag:TechTeam,Values=$TECHTEAM")
    fi

    log_info "AWS filter: ${filter_args[*]}"

    # Fetch IPs
    local aws_output
    local aws_exit
    aws_output=$(aws ec2 describe-instances \
        --region "$AWS_REGION" \
        --filters "${filter_args[@]}" \
        --query 'Reservations[].Instances[].PrivateIpAddress' \
        --output text 2>&1)
    aws_exit=$?

    if [[ $aws_exit -ne 0 ]]; then
        log_error "AWS CLI command failed with exit code: $aws_exit"
        log_error "Error output: $aws_output"
        exit 1
    fi

    local ips
    ips=$(echo "$aws_output" | tr '\t' '\n' | grep -v '^$' | sort -u)

    if [[ -z "$ips" ]]; then
        log_error "No running EC2 instances found with:"
        log_error "  - teamname=$TEAMNAME"
        [[ -n "$TECHTEAM" ]] && log_error "  - techteam=$TECHTEAM"
        log_error "  - region=$AWS_REGION"
        log_error ""
        log_error "Please verify:"
        log_error "  1. The tags are correct on your EC2 instances"
        log_error "  2. Instances are in 'running' state"
        log_error "  3. The AWS region is correct"
        log_error "  4. You have proper AWS credentials configured"
        exit 1
    fi

    # Convert to array (bash 3.x compatible)
    INSTANCE_IPS=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && INSTANCE_IPS+=("$line")
    done <<< "$ips"
    log_info "Found ${#INSTANCE_IPS[@]} instance(s): ${INSTANCE_IPS[*]}"

    # Save to inventory file
    {
        echo "[servers]"
        printf '%s\n' "${INSTANCE_IPS[@]}"
    } > inventory-server.ini

    log_info "Server IPs saved to inventory-server.ini"
}

# Function to load IPs from inventory file (dry-run)
load_ips_from_inventory() {
    log_info "Loading IPs from inventory-server.ini (dry-run mode)..."

    if [[ ! -f "inventory-server.ini" ]]; then
        log_error "inventory-server.ini file not found. Please create it with [servers] section and IP addresses."
        exit 1
    fi

    # Extract IPs from inventory file (bash 3.x compatible)
    INSTANCE_IPS=()
    while IFS= read -r line; do
        line=$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        if [[ -n "$line" ]] && [[ "$line" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            INSTANCE_IPS+=("$line")
        fi
    done < inventory-server.ini

    if [[ ${#INSTANCE_IPS[@]} -eq 0 ]]; then
        log_error "No IP addresses found in inventory-server.ini [servers] section"
        exit 1
    fi

    log_info "DRY-RUN MODE: Loaded ${#INSTANCE_IPS[@]} IP(s) from inventory-server.ini: ${INSTANCE_IPS[*]}"
}

# Function to fetch user data from API
fetch_user_data() {
    log_info "Fetching user data for team: $TEAMNAME"

    local api_url="${API_BASE_URL}/${TEAMNAME}"
    local user_file="users_${TEAMNAME}.json"

    log_info "API URL: $api_url"

    # Try to fetch with verbose output on error
    local curl_output
    curl_output=$(curl -s -w "\nHTTP_CODE:%{http_code}" -f -o "$user_file" "$api_url" 2>&1) || {
        local curl_exit=$?
        log_error "curl failed with exit code: $curl_exit"
        log_error "curl output: $curl_output"
        log_error "Failed to fetch user data from API: $api_url"
        return 1
    }

    if [[ ! -f "$user_file" ]] || [[ ! -s "$user_file" ]]; then
        log_error "User data file is empty or not created: $user_file"
        return 1
    fi

    log_info "User data saved to $user_file ($(wc -l < "$user_file") lines)"
    echo "$user_file"
}

# Function to extract SSH keys for specified roles from JSON
extract_ssh_keys() {
    local user_file="$1"

    if [[ ! -f "$user_file" ]]; then
        log_error "User file not found: $user_file"
        return 1
    fi

    # Check if jq is available
    if ! command -v jq &> /dev/null; then
        log_error "jq is required but not installed. Please install jq to parse JSON."
        log_error "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
        return 1
    fi

    log_info "Parsing JSON file: $user_file"
    log_info "Extracting SSH keys for roles: ${ROLES[*]}"

    # Build jq filter for multiple roles
    local role_filter=""
    local first=true
    for role in "${ROLES[@]}"; do
        if [[ "$first" == "true" ]]; then
            role_filter=".value.role == \"$role\""
            first=false
        else
            role_filter="$role_filter or .value.role == \"$role\""
        fi
    done

    # Extract SSH keys for all specified roles
    local keys
    keys=$(jq -r ".users | to_entries[] | select($role_filter) | .value.ssh_key // empty" "$user_file" 2>&1)
    local jq_exit=$?

    if [[ $jq_exit -ne 0 ]]; then
        log_error "jq parsing failed with exit code: $jq_exit"
        log_error "jq output: $keys"
        return 1
    fi

    if [[ -z "$keys" ]] || [[ "$keys" == "" ]]; then
        log_error "No users found with roles: ${ROLES[*]} for team $TEAMNAME"
        log_info "Checking JSON structure..."
        jq '.' "$user_file" | head -20
        return 1
    fi

    # Convert to array (one key per line), removing duplicates
    DEVADMIN_KEYS=()
    local keys_seen=""  # Track seen keys as a delimited string
    while IFS= read -r key_line; do
        if [[ -n "$key_line" ]]; then
            # Check if we've seen this key before using string matching
            # Use a unique delimiter that won't appear in SSH keys
            local key_marker="|||$key_line|||"
            if [[ "$keys_seen" != *"$key_marker"* ]]; then
                DEVADMIN_KEYS+=("$key_line")
                keys_seen="${keys_seen}${key_marker}"
            fi
        fi
    done <<< "$keys"

    log_info "Found ${#DEVADMIN_KEYS[@]} unique SSH key(s) for roles: ${ROLES[*]}"
    return 0
}

# Function to execute remote command via SSH
ssh_exec() {
    local ip="$1"
    local cmd="$2"

    ssh -i "$KEY" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 \
        -o BatchMode=yes \
        "${SSH_USER}@${ip}" \
        "sudo bash -c '$cmd'" 2>&1
    return $?
}

# Function to check SSH connectivity
check_ssh_connection() {
    local ip="$1"

    if ssh -i "$KEY" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 \
        -o BatchMode=yes \
        "${SSH_USER}@${ip}" \
        "echo 'connected'" &>/dev/null; then
        return 0
    else
        return 1
    fi
}

# Function to configure server
configure_server() {
    local ip="$1"
    local errors=0

    log_info "Configuring server: $ip"

    # Check SSH connectivity
    if ! check_ssh_connection "$ip"; then
        log_error "  Cannot connect to $ip via SSH"
        return 1
    fi

    # Create devadmin group
    log_info "  Creating devadmin group..."
    if ! ssh_exec "$ip" "getent group $DEVADMIN_GROUP >/dev/null 2>&1 || groupadd $DEVADMIN_GROUP" >/dev/null 2>&1; then
        log_warn "  Group may already exist or creation failed"
    fi

    # Create devadmin user
    log_info "  Creating devadmin user..."
    if ! ssh_exec "$ip" "id $DEVADMIN_USER >/dev/null 2>&1 || useradd -g $DEVADMIN_GROUP -s /bin/bash -m $DEVADMIN_USER" >/dev/null 2>&1; then
        log_warn "  User may already exist or creation failed"
    fi

    # Create .ssh directory
    log_info "  Setting up .ssh directory..."
    if ! ssh_exec "$ip" "
        mkdir -p /home/$DEVADMIN_USER/.ssh
        chown $DEVADMIN_USER:$DEVADMIN_GROUP /home/$DEVADMIN_USER/.ssh
        chmod 700 /home/$DEVADMIN_USER/.ssh
    " >/dev/null 2>&1; then
        log_warn "  Failed to create .ssh directory"
        ((errors++))
    fi

    # Manage SSH keys (exclusive - replace all keys)
    log_info "  Managing SSH keys..."
    local keys_temp
    keys_temp=$(mktemp)
    printf '%s\n' "${DEVADMIN_KEYS[@]}" > "$keys_temp"

    if scp -i "$KEY" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 \
        "$keys_temp" "${SSH_USER}@${ip}:/tmp/authorized_keys_temp" &>/dev/null; then
        if ! ssh_exec "$ip" "
            mv /tmp/authorized_keys_temp /home/$DEVADMIN_USER/.ssh/authorized_keys
            chown $DEVADMIN_USER:$DEVADMIN_GROUP /home/$DEVADMIN_USER/.ssh/authorized_keys
            chmod 600 /home/$DEVADMIN_USER/.ssh/authorized_keys
        " >/dev/null 2>&1; then
            log_warn "  Failed to update SSH keys"
            ((errors++))
        fi
    else
        log_warn "  Failed to upload SSH keys"
        ((errors++))
    fi
    rm -f "$keys_temp"

    # Configure sudoers
    log_info "  Configuring sudoers..."
    if ! ssh_exec "$ip" "
        cat > /etc/sudoers.d/devadmin << 'SUDOERS_EOF'
Defaults:devadmin !requiretty

Cmnd_Alias SYSTEMCTL = /bin/systemctl *, /usr/bin/systemctl *
Cmnd_Alias LOGS      = /bin/journalctl, /usr/bin/journalctl
Cmnd_Alias READONLY  = /bin/cat, /usr/bin/cat, /usr/bin/less, /usr/bin/tail
Cmnd_Alias DOCKER    = /usr/bin/docker *
Cmnd_Alias K8S       = /usr/bin/kubectl *
Cmnd_Alias CHMOD     = /bin/chmod /opt/*, /bin/chmod /var/log/*
Cmnd_Alias CHOWN     = /bin/chown /opt/*, /bin/chown /var/log/*

%devadmin ALL=(ALL) NOPASSWD: SYSTEMCTL, LOGS, READONLY, DOCKER, K8S, CHMOD, CHOWN
SUDOERS_EOF
        chmod 0440 /etc/sudoers.d/devadmin
        visudo -cf /etc/sudoers.d/devadmin
    " >/dev/null 2>&1; then
        log_warn "  Failed to configure sudoers"
        ((errors++))
    fi

    if [[ $errors -eq 0 ]]; then
        log_info "  ✓ Successfully configured $ip"
        return 0
    else
        log_warn "  ⚠ Completed with $errors error(s) for $ip"
        return 1
    fi
}

# Main execution
main() {
    # Fetch or load IPs
    if [[ "$DRY_RUN" == "true" ]]; then
        load_ips_from_inventory
    else
        fetch_ips_from_aws
    fi

    # Prompt for confirmation
    echo ""
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "DRY-RUN MODE: Using IPs from inventory-server.ini"
    else
        echo "Server IPs have been saved to inventory-server.ini"
    fi
    echo "Found ${#INSTANCE_IPS[@]} server(s) to configure: ${INSTANCE_IPS[*]}"
    echo ""
    if [[ "$AUTO_APPROVE" == "true" ]]; then
        log_warn "Auto-approve enabled. Proceeding without confirmation."
    else
        read -p "Do you want to proceed with devadmin user creation? (yes/y to proceed, any other key to abort): " confirmation
        if [[ ! "$confirmation" =~ ^[Yy]([Ee][Ss])?$ ]]; then
            log_info "User creation aborted by user. No changes made to servers."
            exit 0
        fi
    fi

    # Fetch user data
    log_info "Step 1: Fetching user data from API..."
    local user_file
    if ! user_file=$(fetch_user_data); then
        log_error "Failed to fetch user data"
        exit 1
    fi

    # Extract SSH keys for specified roles
    log_info "Step 2: Extracting SSH keys for roles: ${ROLES[*]}..."
    if ! extract_ssh_keys "$user_file"; then
        log_error "Failed to extract SSH keys for roles: ${ROLES[*]}"
        exit 1
    fi

    # Configure each server
    log_info "Step 3: Configuring servers..."
    local success_ips=()
    local failed_ips=()

    for ip in "${INSTANCE_IPS[@]}"; do
        log_info "Processing server: $ip"
        if configure_server "$ip"; then
            success_ips+=("$ip")
        else
            failed_ips+=("$ip")
        fi
        echo ""
    done

    # Summary with colored IP lists
    echo ""
    echo "=========================================="
    log_info "Configuration Summary"
    echo "=========================================="

    if [[ ${#success_ips[@]} -gt 0 ]]; then
        echo -e "${GREEN}✓ Successfully configured (${#success_ips[@]}):${NC}"
        for ip in "${success_ips[@]}"; do
            echo -e "  ${GREEN}✓${NC} $ip"
        done
        echo ""
    fi

    if [[ ${#failed_ips[@]} -gt 0 ]]; then
        echo -e "${RED}✗ Failed to configure (${#failed_ips[@]}):${NC}"
        for ip in "${failed_ips[@]}"; do
            echo -e "  ${RED}✗${NC} $ip"
        done
        echo ""
    fi

    if [[ ${#success_ips[@]} -eq ${#INSTANCE_IPS[@]} ]]; then
        log_info "All servers configured successfully!"
    elif [[ ${#failed_ips[@]} -eq ${#INSTANCE_IPS[@]} ]]; then
        log_error "All servers failed to configure!"
    else
        log_warn "Partial success: ${#success_ips[@]} succeeded, ${#failed_ips[@]} failed"
    fi
}

# Run main function
main