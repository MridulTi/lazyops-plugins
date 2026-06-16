# lazyops-plugins

Workflow packs for [LazyOps](https://github.com/mridultiwari/LazyOps). Each plugin lives under a pack directory and declares its pack in `workflow.yaml`.

## Packs

| Pack | Plugins | Description |
|------|---------|-------------|
| [aws](plugins/aws) | 43 | EC2, ASG, networking, IAM, S3, load balancers, and cost optimization |
| [kubernetes](plugins/kubernetes) | 2 | Cluster operations, Helm values, and deployment workflows |
| [security](plugins/security) | 17 | Qualys, Cortex, SIEM, and infosec compliance tooling |
| [bitbucket](plugins/bitbucket) | 7 | Repository migration, access audits, and pull request automation |
| [jenkins](plugins/jenkins) | 2 | Jenkins upgrades and CI/CD SSH audits |
| [linux](plugins/linux) | 20 | SSH server administration, users, shells, and remote commands |
| [monitoring](plugins/monitoring) | 2 | Metrics exporters and observability agent setup |
| [data](plugins/data) | 2 | Kafka, Aerospike, and data platform utilities |
| [mobile](plugins/mobile) | 1 | Android build and release automation |

## Layout

```
plugins/
├── aws/
│   ├── pack.yaml
│   ├── upload-s3-logs/
│   │   ├── workflow.yaml   # pack: aws
│   │   └── script.sh
│   └── ...
├── kubernetes/
│   ├── pack.yaml
│   └── restart-pods/
└── ...
```

## Adding a plugin

1. Pick the pack that best matches the workflow's primary domain.
2. Create a folder under `plugins/<pack>/<plugin-id>/`.
3. Add `workflow.yaml` with `pack: <pack>` set alongside the required LazyOps fields.

To recategorize plugins after editing the pack map, run:

```bash
python3 scripts/categorize_packs.py
```
