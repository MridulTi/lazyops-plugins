import boto3
import json
import os
import sys
from datetime import datetime


def _env_bool(name: str, default: bool = True) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y"}


def _require_region() -> str:
    region = os.environ.get("REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        sys.exit("Set REGION or AWS_REGION")
    return region


DRY_RUN = _env_bool("DRY_RUN", default=True)
REGION = _require_region()

ec2 = boto3.client("ec2", region_name=REGION)


def get_peering_details(pcx_id):
    try:
        response = ec2.describe_vpc_peering_connections(
            VpcPeeringConnectionIds=[pcx_id]
        )
        pcx = response["VpcPeeringConnections"][0]
        return {
            "pcx_id": pcx_id,
            "accepter_vpc": pcx["AccepterVpcInfo"].get("VpcId"),
            "requester_vpc": pcx["RequesterVpcInfo"].get("VpcId"),
            "accepter_cidr": pcx["AccepterVpcInfo"].get("CidrBlock"),
            "requester_cidr": pcx["RequesterVpcInfo"].get("CidrBlock"),
            "status": pcx["Status"]["Code"],
        }
    except Exception as e:
        print(f"Failed to fetch peering {pcx_id}: {e}")
        return None


def find_routes_for_peering(pcx_id):
    matched_routes = []
    paginator = ec2.get_paginator("describe_route_tables")
    for page in paginator.paginate():
        for rt in page["RouteTables"]:
            rt_id = rt["RouteTableId"]
            for route in rt.get("Routes", []):
                if route.get("VpcPeeringConnectionId") == pcx_id:
                    matched_routes.append({
                        "RouteTableId": rt_id,
                        "DestinationCidrBlock": route.get("DestinationCidrBlock"),
                        "VpcPeeringConnectionId": pcx_id,
                    })
    return matched_routes


def delete_routes(routes):
    for route in routes:
        rt_id = route["RouteTableId"]
        cidr = route["DestinationCidrBlock"]
        if DRY_RUN:
            print(f"[DRY RUN] Would delete route -> RT: {rt_id}, CIDR: {cidr}")
            continue
        try:
            print(f"Deleting route -> RT: {rt_id}, CIDR: {cidr}")
            ec2.delete_route(RouteTableId=rt_id, DestinationCidrBlock=cidr)
        except Exception as e:
            print(f"Failed deleting route {route}: {e}")


def main(file_path):
    with open(file_path, "r") as f:
        pcx_ids = [line.strip() for line in f if line.strip()]

    final_backup = []
    for pcx_id in pcx_ids:
        print(f"\nProcessing Peering: {pcx_id}")
        details = get_peering_details(pcx_id)
        if not details:
            continue
        routes = find_routes_for_peering(pcx_id)
        if not routes:
            print("  No routes found")
            continue
        print(f"  Found {len(routes)} route(s)")
        final_backup.append({"peering": details, "routes": routes})
        delete_routes(routes)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_file = f"peering_backup_{timestamp}.json"
    with open(backup_file, "w") as f:
        json.dump(final_backup, f, indent=4)
    print(f"\nBackup saved: {backup_file}")
    if DRY_RUN:
        print("DRY RUN enabled -> no routes were deleted")
    else:
        print("Routes deleted successfully")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: script.py <peering-ids-file>")
        sys.exit(1)
    main(sys.argv[1])
