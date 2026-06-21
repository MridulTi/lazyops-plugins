import boto3
import os
import sys

def _require_region():
    region = os.environ.get("REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        sys.exit("Set REGION or AWS_REGION")
    return region

REGION = _require_region()

if len(sys.argv) != 2:
    print("Usage: script.py <target-cidr>")
    sys.exit(1)

TARGET_CIDR = sys.argv[1]

ec2 = boto3.client("ec2", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)


def get_peering_connection(cidr):
    resp = ec2.describe_vpc_peering_connections()
    for pcx in resp["VpcPeeringConnections"]:
        if pcx["Status"]["Code"] != "active":
            continue
        for c in pcx["AccepterVpcInfo"].get("CidrBlockSet", []):
            if c["CidrBlock"] == cidr:
                return pcx["VpcPeeringConnectionId"]
    return None


def get_peering_for_vpc(vpc_id, cidr):
    resp = ec2.describe_vpc_peering_connections()
    for pcx in resp["VpcPeeringConnections"]:
        if pcx["Status"]["Code"] != "active":
            continue

        req = pcx["RequesterVpcInfo"]
        acc = pcx["AccepterVpcInfo"]

        if req["VpcId"] == vpc_id:
            cidrs = acc.get("CidrBlockSet", [])
        elif acc["VpcId"] == vpc_id:
            cidrs = req.get("CidrBlockSet", [])
        else:
            continue

        if any(c["CidrBlock"] == cidr for c in cidrs):
            return pcx["VpcPeeringConnectionId"]

    return None


def get_lb_subnet_map():
    lb_map = {}
    lbs = elbv2.describe_load_balancers()["LoadBalancers"]
    for lb in lbs:
        lb_map[lb["LoadBalancerName"]] = [
            az["SubnetId"] for az in lb["AvailabilityZones"]
        ]
    return lb_map


def get_route_table(subnet_id):
    # Subnet-associated RT
    resp = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
    )
    if resp["RouteTables"]:
        rt = resp["RouteTables"][0]
        return rt["RouteTableId"], rt["Routes"]

    # Fallback to VPC main RT
    subnet = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
    vpc_id = subnet["VpcId"]

    resp = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "association.main", "Values": ["true"]},
        ]
    )

    rt = resp["RouteTables"][0]
    return rt["RouteTableId"], rt["Routes"]


def route_exists(routes, cidr):
    return any(r.get("DestinationCidrBlock") == cidr for r in routes)


def main():
    pcx_any = get_peering_connection(TARGET_CIDR)
    if not pcx_any:
        print(f"❌ No active peering found for {TARGET_CIDR}")
        sys.exit(1)

    print(f"✅ Peering connection exists for {TARGET_CIDR}\n")

    lb_map = get_lb_subnet_map()
    missing = []

    for lb, subnets in lb_map.items():
        for subnet in subnets:
            rt_id, routes = get_route_table(subnet)

            if route_exists(routes, TARGET_CIDR):
                print(f"✔ OK     | LB: {lb} | Subnet: {subnet} | RT: {rt_id}")
            else:
                print(f"❌ MISSING | LB: {lb} | Subnet: {subnet} | RT: {rt_id}")
                missing.append((subnet, rt_id))

    if not missing:
        print("\n🎉 All LB subnets already have the route")
        return

    print(f"\n⚠️  {len(missing)} subnet(s) missing route to {TARGET_CIDR}")
    choice = input("Do you want to add missing routes? (yes/no): ").strip().lower()

    if choice != "yes":
        print("🚫 No changes made")
        return

    for subnet, rt_id in set(missing):
        subnet_info = ec2.describe_subnets(SubnetIds=[subnet])["Subnets"][0]
        vpc_id = subnet_info["VpcId"]

        pcx_id = get_peering_for_vpc(vpc_id, TARGET_CIDR)
        if not pcx_id:
            print(f"❌ No peering for VPC {vpc_id} → {TARGET_CIDR}, skipping {rt_id}")
            continue

        print(f"➕ Adding route in {rt_id} via {pcx_id}")
        ec2.create_route(
            RouteTableId=rt_id,
            DestinationCidrBlock=TARGET_CIDR,
            VpcPeeringConnectionId=pcx_id,
        )

    print("\n✅ Routes added successfully")


if __name__ == "__main__":
    main()

