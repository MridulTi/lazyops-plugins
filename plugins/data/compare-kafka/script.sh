#!/bin/bash

PROD_CSV="$1"
DR_CSV="$2"

if [[ -z "$PROD_CSV" || -z "$DR_CSV" ]]; then
    echo "Usage: $0 prod_topics.csv dr_topics.csv"
    exit 1
fi

# Extract topics ignoring the header
prod_topics=$(tail -n +2 "$PROD_CSV" | awk -F',' '{print $1}' | sort)
dr_topics=$(tail -n +2 "$DR_CSV" | awk -F',' '{print $1}' | sort)

# Create temp files
prod_tmp=$(mktemp)
dr_tmp=$(mktemp)

echo "$prod_topics" > "$prod_tmp"
echo "$dr_topics" > "$dr_tmp"

echo "===== TOPIC COMPARISON REPORT ====="

echo -e "\n### Topics ONLY in PROD ###"
comm -23 "$prod_tmp" "$dr_tmp"

echo -e "\n### Topics ONLY in DR ###"
comm -13 "$prod_tmp" "$dr_tmp"

#echo -e "\n### Topics COMMON in BOTH ###"
#comm -12 "$prod_tmp" "$dr_tmp"

# Cleanup
rm -f "$prod_tmp" "$dr_tmp"

