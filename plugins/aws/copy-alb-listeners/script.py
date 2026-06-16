import boto3
import sys
import copy

if len(sys.argv) != 4:
    print("Usage: python copy_alb_listener_rules.py <alb_name> <copy_from_port> <copy_to_port>")
    sys.exit(1)

ALB_NAME = sys.argv[1]
FROM_PORT = int(sys.argv[2])
TO_PORT = int(sys.argv[3])

elbv2 = boto3.client("elbv2")


def get_alb_arn(name):
    lbs = elbv2.describe_load_balancers()["LoadBalancers"]
    for lb in lbs:
        if lb["LoadBalancerName"] == name:
            return lb["LoadBalancerArn"]
    return None


def get_listener_arn(alb_arn, port):
    listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
    for listener in listeners:
        if listener["Port"] == port:
            return listener["ListenerArn"]
    return None


def get_existing_priorities(listener_arn):
    rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
    return set(r["Priority"] for r in rules if r["Priority"] != "default")


def rule_already_exists(dest_rules, source_rule):
    for rule in dest_rules:
        if rule["Conditions"] == source_rule["Conditions"]:
            return True
    return False

def sanitize_conditions(conditions):
    cleaned = []

    for c in conditions:
        new_c = {"Field": c["Field"]}

        # Modern structured configs
        if "HostHeaderConfig" in c:
            new_c["HostHeaderConfig"] = c["HostHeaderConfig"]
        elif "PathPatternConfig" in c:
            new_c["PathPatternConfig"] = c["PathPatternConfig"]
        elif "HttpHeaderConfig" in c:
            new_c["HttpHeaderConfig"] = c["HttpHeaderConfig"]
        elif "QueryStringConfig" in c:
            new_c["QueryStringConfig"] = c["QueryStringConfig"]
        elif "SourceIpConfig" in c:
            new_c["SourceIpConfig"] = c["SourceIpConfig"]
        else:
            # fallback (older format)
            new_c["Values"] = c.get("Values", [])

        cleaned.append(new_c)

    return cleaned


def main():
    alb_arn = get_alb_arn(ALB_NAME)
    if not alb_arn:
        print(f"❌ ALB {ALB_NAME} not found")
        sys.exit(1)

    from_listener = get_listener_arn(alb_arn, FROM_PORT)
    to_listener = get_listener_arn(alb_arn, TO_PORT)

    if not from_listener:
        print(f"❌ No listener found on port {FROM_PORT}")
        sys.exit(1)

    if not to_listener:
        print(f"❌ No listener found on port {TO_PORT}")
        sys.exit(1)

    source_rules = elbv2.describe_rules(ListenerArn=from_listener)["Rules"]
    dest_rules = elbv2.describe_rules(ListenerArn=to_listener)["Rules"]
    existing_priorities = get_existing_priorities(to_listener)

    print(f"\n🔍 Found {len(source_rules)} rules on port {FROM_PORT}")
    print(f"🔍 Found {len(dest_rules)} rules on port {TO_PORT}")

    copied = 0

    for rule in source_rules:
        if rule["IsDefault"]:
            continue  # Skip default rule

        if rule["Priority"] in existing_priorities:
            print(f"⚠ Skipping priority {rule['Priority']} (already exists)")
            continue

        if rule_already_exists(dest_rules, rule):
            print(f"⚠ Skipping rule with same conditions (already exists)")
            continue

        print(f"➕ Copying rule priority {rule['Priority']}")
        elbv2.create_rule(
	    ListenerArn=to_listener,
	    Conditions=sanitize_conditions(rule["Conditions"]),
	    Actions=copy.deepcopy(rule["Actions"]),
	    Priority=int(rule["Priority"])
        )

        copied += 1

    print(f"\n✅ Done. {copied} rule(s) copied.")
    print("No rules were modified or deleted.")


if __name__ == "__main__":
    main()

