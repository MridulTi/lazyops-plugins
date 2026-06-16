#!/usr/bin/env python3
"""
Install/configure Cortex, Qualys, and SIEM forwarding.
All org-specific values must be supplied via environment variables or a JSON config file.

Required env (or pass CONFIG_FILE path as first argument):
  CONFIG_FILE              Optional JSON with keys below
  SIEM_FORWARD_HOST        e.g. siem.example.com
  NEXUS_BASE_URL           e.g. https://nexus.example.com/repository/devops
  QUALYS_SERVER_URI        e.g. https://qualys.example.com/CloudAgent/
  QUALYS_CUSTOMER_ID
  CORTEX_VALID_IDS         Comma-separated distribution IDs
  ACTIVATION_IDS_JSON      JSON map: {"prod": {"account_id": "activation-id"}, ...}
"""

import json
import os
import subprocess
import sys


def load_settings():
    cfg = {}
    config_file = os.environ.get("CONFIG_FILE") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if config_file:
        with open(config_file, "r") as f:
            cfg = json.load(f)

    def get(key, env_key=None, required=True):
        val = cfg.get(key) or os.environ.get(env_key or key)
        if required and not val:
            sys.exit(f"Missing required setting: {key} (env {env_key or key})")
        return val

    settings = {
        "siem_forward_host": get("siem_forward_host", "SIEM_FORWARD_HOST"),
        "nexus_base_url": get("nexus_base_url", "NEXUS_BASE_URL").rstrip("/"),
        "qualys_server_uri": get("qualys_server_uri", "QUALYS_SERVER_URI"),
        "qualys_customer_id": get("qualys_customer_id", "QUALYS_CUSTOMER_ID"),
        "cortex_valid_ids": get("cortex_valid_ids", "CORTEX_VALID_IDS").split(","),
        "activation_ids": json.loads(get("activation_ids", "ACTIVATION_IDS_JSON")),
    }
    return settings


def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def rsyslog_conf(host):
    return f"auth.*,authpriv.* @{host}\n"


def main():
    settings = load_settings()
    print("Configuration loaded. Implement installation steps for your environment.")
    print(f"SIEM host: {settings['siem_forward_host']}")
    print(f"Nexus: {settings['nexus_base_url']}")
    print("Set CONFIG_FILE or env vars and extend this script for your platform.")


if __name__ == "__main__":
    main()
