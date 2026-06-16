import os
import boto3
import csv
import sys

route53 = boto3.client("route53")

def find_hosted_zone(domain):
    """Find the best matching Hosted Zone for a domain."""
    zones = route53.list_hosted_zones()["HostedZones"]
    domain = domain.rstrip(".")

    zones = sorted(zones, key=lambda z: len(z["Name"]), reverse=True)

    for z in zones:
        zone_name = z["Name"].rstrip(".")
        if domain.endswith(zone_name):
            return z["Id"].replace("/hostedzone/", "")
    return None


def record_exists(zone_id, domain):
    """Check if a record already exists."""
    records = route53.list_resource_record_sets(
        HostedZoneId=zone_id,
        StartRecordName=domain,
        StartRecordType="CNAME"
    )

    for r in records["ResourceRecordSets"]:
        if r["Name"].rstrip(".") == domain.rstrip("."):
            return True
    return False


def create_record(domain, alb):
    zone_id = find_hosted_zone(domain)
    if not zone_id:
        print(f"❌ No hosted zone found for {domain}")
        return

    # Check if record already exists
    if record_exists(zone_id, domain):
        print(f"⚠️ SKIPPED: {domain} already exists")
        return

    print(f"→ Creating new record: {domain} → {alb}")

    try:
        route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "CREATE",
                        "ResourceRecordSet": {
                            "Name": domain,
                            "Type": "CNAME",
                            "TTL": 60,
                            "ResourceRecords": [{"Value": alb}]
                        }
                    }
                ]
            }
        )
        print(f"✔ SUCCESS: {domain} → {alb}")

    except Exception as e:
        print(f"❌ FAILED creating {domain}: {e}")


def main(csv_file):
    with open(csv_file, "r") as f:
        reader = csv.DictReader(f)

        for row in reader:
            domain_col = os.environ.get("DOMAIN_COLUMN", "domain")
            alb_col = os.environ.get("ALB_COLUMN", "alb_endpoint")
            domain = row[domain_col].strip()
            alb = row[alb_col].strip()
            create_record(domain, alb)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: script.py records.csv  (columns: DOMAIN_COLUMN, ALB_COLUMN env)")
        sys.exit(1)

    main(sys.argv[1])
