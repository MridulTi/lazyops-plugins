import csv
from collections import defaultdict
from tabulate import tabulate

def read_csv(filepath):
    with open(filepath, mode='r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return [normalize_headers(row) for row in reader]

def normalize_headers(row):
    return {k.strip(): v for k, v in row.items()}

def generate_vuln_key(row):
    return (
        row['Title'].strip(),
        row['Asset Name'].strip(),
        row['Asset IPV4'].strip(),
        row['Protocol'].strip(),
        row['Port'].strip()
    )

def save_to_file(file_name, content):
    with open(file_name, 'w') as f:
        f.write(content)

def compare_vulnerabilities(old_file, new_file, output_file):
    old_vulns = read_csv(old_file)
    new_vulns = read_csv(new_file)

    old_map = {generate_vuln_key(row): row for row in old_vulns}
    new_map = {generate_vuln_key(row): row for row in new_vulns}
    
    removed = defaultdict(list)
    status_changed = defaultdict(list)

    for key, old_row in old_map.items():
        if key not in new_map:
            removed[old_row['Asset IPV4']].append(old_row)
        else:
            new_row = new_map[key]
            if old_row['Status'].strip() != new_row['Status'].strip():
                status_changed[old_row['Asset IPV4']].append((old_row, new_row))

    output = ""

    # Removed vulnerabilities
    output += f"\n✅ Total Vulnerabilities Removed: {sum(len(v) for v in removed.values())}\n"
    for ip, entries in removed.items():
        output += f"\n📍 Vulnerabilities Removed for IP: {ip}\n"
        table_data = []
        for r in entries:
            table_data.append([
                r['Title'],
                r['Severity'],
                r['Protocol'],
                r['Port'],
                r['Status']
            ])
        output += tabulate(
            table_data,
            headers=["Title", "Severity", "Protocol", "Port", "Status"],
            tablefmt="grid"
        ) + "\n"

    # Status changed vulnerabilities
    output += f"\n🔁 Total Vulnerabilities with Status Changed: {sum(len(v) for v in status_changed.values())}\n"
    for ip, changes in status_changed.items():
        output += f"\n📍 Vulnerability Status Changes for IP: {ip}\n"
        table_data = []
        for old_r, new_r in changes:
            table_data.append([
                old_r['Title'],
                old_r['Severity'],
                old_r['Protocol'],
                old_r['Port'],
                old_r['Status'],
                new_r['Status']
            ])
        output += tabulate(
            table_data,
            headers=["Title", "Severity", "Protocol", "Port", "Old Status", "New Status"],
            tablefmt="grid"
        ) + "\n"

    # Save the output to a file
    save_to_file(output_file, output)
    print(f"Results saved to {output_file}")

# Example usage
compare_vulnerabilities('old_scan.csv', 'new_scan.csv', 'vulnerability_comparison.txt')
