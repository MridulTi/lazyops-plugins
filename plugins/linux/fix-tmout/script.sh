#!/bin/bash

# Script to fix TMOUT readonly issue on remote servers
# Usage: ./fix_tmout.sh <fix_username> <fix_ssh_key> <check_username> <check_ssh_key> [config_file]
# Example: ./fix_tmout.sh admin admin_key.pem checker checker_key.pem config.ini

set -e

# Check if required parameters are provided
if [ $# -lt 4 ]; then
    echo "Usage: $0 <fix_username> <fix_ssh_key> <check_username> <check_ssh_key> [config_file]"
    echo "Example: $0 admin admin_key.pem checker checker_key.pem config.ini"
    echo ""
    echo "Parameters:"
    echo "  fix_username    - Username to use for fixing TMOUT (needs sudo access)"
    echo "  fix_ssh_key     - SSH key file for fixing"
    echo "  check_username  - Username to use for verification/checking"
    echo "  check_ssh_key   - SSH key file for checking"
    echo "  config_file     - Config file with IP addresses (default: config.ini)"
    exit 1
fi

FIX_USERNAME="$1"
FIX_SSH_KEY="$2"
CHECK_USERNAME="$3"
CHECK_SSH_KEY="$4"
CONFIG_FILE="${5:-config.ini}"

# Check if SSH keys exist
if [ ! -f "$FIX_SSH_KEY" ]; then
    echo "Error: Fix SSH key file '$FIX_SSH_KEY' not found"
    exit 1
fi

if [ ! -f "$CHECK_SSH_KEY" ]; then
    echo "Error: Check SSH key file '$CHECK_SSH_KEY' not found"
    exit 1
fi

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Config file '$CONFIG_FILE' not found"
    exit 1
fi

# Set proper permissions for SSH keys
chmod 600 "$FIX_SSH_KEY"
chmod 600 "$CHECK_SSH_KEY"

# Extract IPs from config.ini (assuming format: ip= or host= or just IP addresses)
IPS=$(grep -E "^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+|^ip=|^host=" "$CONFIG_FILE" | sed -E 's/^[^=]*=//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

if [ -z "$IPS" ]; then
    echo "Error: No IP addresses found in $CONFIG_FILE"
    echo "Expected format: ip=10.0.0.1 or just 10.0.0.1"
    exit 1
fi

# Function to verify TMOUT fix on a remote server
verify_tmout() {
    local ip="$1"
    local username="$2"
    local ssh_key="$3"
    
    echo "  Verifying fix on $ip..."
    
    # Create a verification script
    local verify_script=$(cat <<'VERIFY_SCRIPT'
#!/bin/bash
TMOUT_FILE="/etc/profile.d/tmout.sh"

if [ ! -f "$TMOUT_FILE" ]; then
    echo "    File not found"
    exit 1
fi

# Check if export TMOUT comes before readonly TMOUT
export_line=$(grep -n "export TMOUT" "$TMOUT_FILE" | head -1 | cut -d: -f1)
readonly_line=$(grep -n "readonly TMOUT" "$TMOUT_FILE" | head -1 | cut -d: -f1)

if [ -z "$export_line" ] || [ -z "$readonly_line" ]; then
    echo "    Warning: export or readonly TMOUT not found"
    exit 1
fi

if [ "$export_line" -lt "$readonly_line" ]; then
    echo "    ✓ Fix verified: export TMOUT comes before readonly TMOUT"
    exit 0
else
    echo "    ✗ Fix failed: export TMOUT does not come before readonly TMOUT"
    exit 1
fi
VERIFY_SCRIPT
)

    # Execute verification via check credentials
    if ssh -i "$ssh_key" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        "$username@$ip" "bash -s" <<< "$verify_script" 2>/dev/null; then
        return 0
    else
        return 1
    fi
}

# Function to fix TMOUT on a remote server
fix_tmout() {
    local ip="$1"
    local username="$2"
    local ssh_key="$3"
    
    echo "=========================================="
    echo "Processing IP: $ip"
    echo "=========================================="
    
    # Create a temporary script to run on remote server
    local remote_script=$(cat <<'REMOTE_SCRIPT'
#!/bin/bash
set -e

TMOUT_FILE="/etc/profile.d/tmout.sh"
BACKUP_FILE="/etc/profile.d/tmout.sh.backup.$(date +%Y%m%d_%H%M%S)"

# Check if file exists
if [ ! -f "$TMOUT_FILE" ]; then
    echo "  Warning: $TMOUT_FILE does not exist on this server"
    exit 0
fi

# Create backup
sudo cp "$TMOUT_FILE" "$BACKUP_FILE"
echo "  Created backup: $BACKUP_FILE"

# Read the file content
TEMP_FILE=$(mktemp)
sudo cat "$TMOUT_FILE" > "$TEMP_FILE"

# Check if readonly TMOUT exists
if ! grep -q "readonly TMOUT" "$TEMP_FILE"; then
    echo "  No 'readonly TMOUT' found in file. File may already be correct."
    rm -f "$TEMP_FILE"
    exit 0
fi

# Create fixed content
FIXED_FILE=$(mktemp)

# Process the file line by line
while IFS= read -r line; do
    # Skip the old readonly TMOUT line (we'll add it at the end)
    if [[ "$line" =~ ^[[:space:]]*readonly[[:space:]]+TMOUT ]]; then
        continue
    fi
    echo "$line" >> "$FIXED_FILE"
done < "$TEMP_FILE"

# Add readonly TMOUT at the end (after export TMOUT if it exists)
if grep -q "export TMOUT" "$FIXED_FILE"; then
    # Add readonly after export TMOUT
    sed -i '/export TMOUT/a readonly TMOUT' "$FIXED_FILE"
else
    # If no export found, add both export and readonly at the end
    echo "" >> "$FIXED_FILE"
    echo "# Set session timeout - CIS ID AMZN LNX 2-5.5.4" >> "$FIXED_FILE"
    echo "export TMOUT=600" >> "$FIXED_FILE"
    echo "readonly TMOUT" >> "$FIXED_FILE"
fi

# Write the fixed content back
sudo cp "$FIXED_FILE" "$TMOUT_FILE"
sudo chmod 644 "$TMOUT_FILE"

# Clean up temp files
rm -f "$TEMP_FILE" "$FIXED_FILE"

echo "  Successfully fixed $TMOUT_FILE"
echo "  Backup saved at: $BACKUP_FILE"
REMOTE_SCRIPT
)

    # Execute the remote script using fix credentials
    if ssh -i "$ssh_key" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        "$username@$ip" "bash -s" <<< "$remote_script"; then
        echo "  ✓ Successfully fixed TMOUT on $ip"
        return 0
    else
        echo "  ✗ Failed to fix TMOUT on $ip"
        return 1
    fi
}

# Process each IP
SUCCESS_COUNT=0
FAILED_COUNT=0
FAILED_IPS=()

for ip in $IPS; do
    # Skip empty lines
    [ -z "$ip" ] && continue
    
    # Fix using fix credentials
    if fix_tmout "$ip" "$FIX_USERNAME" "$FIX_SSH_KEY"; then
        # Verify using check credentials
        if verify_tmout "$ip" "$CHECK_USERNAME" "$CHECK_SSH_KEY"; then
            ((SUCCESS_COUNT++))
        else
            echo "  ⚠ Fix applied but verification failed for $ip"
            ((FAILED_COUNT++))
            FAILED_IPS+=("$ip")
        fi
    else
        ((FAILED_COUNT++))
        FAILED_IPS+=("$ip")
    fi
    echo ""
done

# Summary
echo "=========================================="
echo "Summary:"
echo "=========================================="
echo "Total IPs processed: $((SUCCESS_COUNT + FAILED_COUNT))"
echo "Successful: $SUCCESS_COUNT"
echo "Failed: $FAILED_COUNT"

if [ $FAILED_COUNT -gt 0 ]; then
    echo ""
    echo "Failed IPs:"
    for ip in "${FAILED_IPS[@]}"; do
        echo "  - $ip"
    done
    exit 1
fi

echo ""
echo "All servers have been fixed successfully!"
exit 0


