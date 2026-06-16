#!/bin/bash

# Script to add cron job for manage-dev-admin.sh
# This will run the script at 00:00 (midnight) every day

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/manage-dev-admin.sh"

# Default values - modify these as needed
KEY_PATH="${KEY_PATH:-}"
TEAMNAME="${TEAMNAME:-}"
TECHTEAM="${TECHTEAM:-}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
SSH_USER="${SSH_USER:-ubuntu}"
ROLES="${ROLES:-devadmin}"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/manage-dev-admin-$(date +%Y%m%d).log"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Build the command
CRON_CMD="$SCRIPT_PATH"

if [[ -n "$KEY_PATH" ]]; then
    CRON_CMD="$CRON_CMD -k $KEY_PATH"
else
    echo "ERROR: KEY_PATH must be set"
    echo "Usage: KEY_PATH=/path/to/key.pem TEAMNAME=TEAMNAME $0"
    exit 1
fi

if [[ -n "$TEAMNAME" ]]; then
    CRON_CMD="$CRON_CMD -t $TEAMNAME"
else
    echo "ERROR: TEAMNAME must be set"
    echo "Usage: KEY_PATH=/path/to/key.pem TEAMNAME=TEAMNAME $0"
    exit 1
fi

if [[ -n "$TECHTEAM" ]]; then
    CRON_CMD="$CRON_CMD -T $TECHTEAM"
fi

if [[ -n "$AWS_REGION" ]]; then
    CRON_CMD="$CRON_CMD -r $AWS_REGION"
fi

if [[ -n "$SSH_USER" ]]; then
    CRON_CMD="$CRON_CMD -u $SSH_USER"
fi

# Add roles (can be multiple)
if [[ -n "$ROLES" ]]; then
    for role in $ROLES; do
        CRON_CMD="$CRON_CMD -R $role"
    done
fi

# Add logging
CRON_CMD="$CRON_CMD >> $LOG_FILE 2>&1"

# Create cron entry (runs at 00:00 every day)
CRON_ENTRY="0 0 * * * $CRON_CMD"

echo "Cron entry to be added:"
echo "$CRON_ENTRY"
echo ""
read -p "Do you want to add this to your crontab? (yes/no): " confirm

if [[ "$confirm" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    # Add to crontab
    (crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -
    echo "Cron job added successfully!"
    echo "View your crontab with: crontab -l"
    echo "Logs will be written to: $LOG_FILE"
else
    echo "Cancelled. Cron entry not added."
    echo ""
    echo "To add manually, run:"
    echo "crontab -e"
    echo ""
    echo "Then add this line:"
    echo "$CRON_ENTRY"
fi




