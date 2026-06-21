#!/usr/bin/env python3
"""Move plugins into pack directories and add pack metadata to workflow.yaml."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

PACKS: dict[str, list[str]] = {
    "aws": [
        "add-certificate-lb",
        "add-tags",
        "addpatchclasstag",
        "asg-git-urls",
        "asg-ips",
        "asg-ips-ssh",
        "backup-ami",
        "batch-start-linux-ami-patch-automation",
        "bifercate-asg-static",
        "change-userdata",
        "check-ami",
        "check-anywhere-in-sg",
        "check-asg-for-qualys-cortex",
        "check-asg-for-qualys-cortex-dashboard",
        "check-platform",
        "check-user-data-ips",
        "check-userdata-bitbucket-asg",
        "check-userdata-for-bitbucket",
        "checkunusedvolumes",
        "chek-ips-exist",
        "cleanup-java",
        "cleanup-java-updated",
        "compare-lb",
        "copy-alb-listeners",
        "create-iam",
        "delete-peering-route",
        "ensure-lb-peering-routes",
        "finalcostopt",
        "finalcostoptall",
        "findstaticinstances",
        "iam-policy-update-multiple-accounts",
        "lb-details",
        "list-qualys-branches-from-userdata",
        "list-static-ips",
        "nacl-entry-add",
        "port-cidr-whitelisting-sg",
        "route53-domain-creation",
        "run-qualys-for-roles",
        "sg-to-csv",
        "ssh-ssm",
        "update-lt",
        "upload-s3-logs",
        "userdata",
    ],
    "kubernetes": [
        "compare-yml",
        "restart-pods",
    ],
    "security": [
        "check-infosec-connection",
        "check-infosec-user",
        "cortex-qualys-siem-integration",
        "disable-traps",
        "force-cortex-id-change",
        "force-cortex-ips",
        "install-cortex-from-local",
        "install-cortex-from-s3",
        "install-qualys",
        "install-qualys-cortex-from-local",
        "install-qualys-from-local",
        "install-qualys-from-s3",
        "install-qualys-from-s3-bkp",
        "qualys-report-compare",
        "remove-usepam-infosec",
        "repair-qualys-from-s3",
        "siem-installation",
    ],
    "bitbucket": [
        "bitbucket-repo-users",
        "bitbucket-repos",
        "clone-repo",
        "decline-pr",
        "git-pull-update",
        "migrate-repo-all-branch",
        "migrate-repo-particular-branch",
    ],
    "jenkins": [
        "jenkins-bitbucket-ssh-audit",
        "jenkins-inplace-upgrade",
    ],
    "linux": [
        "addpubkey",
        "change-bash-login",
        "change-password-expiry",
        "change-user-shell",
        "changepassword",
        "check-connection",
        "check-user-name",
        "fix-tmout",
        "fixusepam",
        "hostnamefinder",
        "login-server",
        "manage-devadmin",
        "oracle-java-removal",
        "os-check",
        "remote-script",
        "removetags",
        "rpm-check-all",
        "run-commands",
        "setup-cron",
        "usercreation",
    ],
    "monitoring": [
        "setup-redis-exporter",
        "setup-telegraf",
    ],
    "data": [
        "aerospike-oom-recovery",
        "compare-kafka",
    ],
    "mobile": [
        "intenralbusiness",
    ],
}

PACK_META = {
    "aws": {
        "name": "AWS",
        "description": "EC2, ASG, networking, IAM, S3, load balancers, and cost optimization.",
    },
    "kubernetes": {
        "name": "Kubernetes",
        "description": "Cluster operations, Helm values, and deployment workflows.",
    },
    "security": {
        "name": "Security",
        "description": "Qualys, Cortex, SIEM, and infosec compliance tooling.",
    },
    "bitbucket": {
        "name": "Bitbucket",
        "description": "Repository migration, access audits, and pull request automation.",
    },
    "jenkins": {
        "name": "Jenkins",
        "description": "Jenkins upgrades and CI/CD SSH audits.",
    },
    "linux": {
        "name": "Linux",
        "description": "SSH server administration, users, shells, and remote commands.",
    },
    "monitoring": {
        "name": "Monitoring",
        "description": "Metrics exporters and observability agent setup.",
    },
    "data": {
        "name": "Data",
        "description": "Kafka, Aerospike, and data platform utilities.",
    },
    "mobile": {
        "name": "Mobile",
        "description": "Android build and release automation.",
    },
}


def plugin_dirs() -> list[Path]:
    found: list[Path] = []
    for path in sorted(PLUGINS.rglob("workflow.yaml")):
        found.append(path.parent)
    return found


def locate_plugin(plugin_id: str) -> Path | None:
    for path in plugin_dirs():
        if path.name == plugin_id:
            return path
    return None


def add_pack_to_yaml(yaml_path: Path, pack: str) -> None:
    text = yaml_path.read_text(encoding="utf-8")
    if re.search(r"^pack:\s", text, re.MULTILINE):
        text = re.sub(r"^pack:.*$", f"pack: {pack}", text, count=1, flags=re.MULTILINE)
    else:
        text = text.replace("\nversion:", f"\npack: {pack}\n\nversion:", 1)
    yaml_path.write_text(text, encoding="utf-8")


def write_pack_manifest(pack_id: str) -> None:
    meta = PACK_META[pack_id]
    pack_dir = PLUGINS / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"id: {pack_id}",
        "",
        f'name: "{meta["name"]}"',
        "",
        f'description: "{meta["description"]}"',
        "",
    ]
    (pack_dir / "pack.yaml").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    assigned = {plugin for plugins in PACKS.values() for plugin in plugins}
    discovered = {path.name for path in plugin_dirs()}
    missing = sorted(discovered - assigned)
    extra = sorted(assigned - discovered)
    if missing:
        raise SystemExit(f"Unassigned plugins: {', '.join(missing)}")
    if extra:
        raise SystemExit(f"Unknown plugins in PACKS map: {', '.join(extra)}")

    moved = 0
    updated = 0
    for pack_id, plugin_ids in PACKS.items():
        write_pack_manifest(pack_id)
        pack_dir = PLUGINS / pack_id
        for plugin_id in sorted(plugin_ids):
            src = locate_plugin(plugin_id)
            if src is None:
                raise SystemExit(f"Plugin not found: {plugin_id}")
            dest = pack_dir / plugin_id
            if src.resolve() == dest.resolve():
                pass
            elif dest.exists():
                raise SystemExit(f"Destination already exists: {dest}")
            else:
                shutil.move(str(src), str(dest))
                moved += 1
            yaml_path = dest / "workflow.yaml"
            add_pack_to_yaml(yaml_path, pack_id)
            updated += 1

    print(f"Moved {moved} plugins into packs")
    print(f"Updated pack metadata in {updated} workflow.yaml files")
    for pack_id, plugin_ids in PACKS.items():
        print(f"  {pack_id}: {len(plugin_ids)} plugins")


if __name__ == "__main__":
    main()
