#!/usr/bin/env python3
"""Interactive terminal setup for Tapo credentials (hidden password input) + sync to ropihung."""

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from tapo_klap import TapoAuthError, TapoKlapClient
from tvlighton import print_status

CONFIG_PATH = Path(__file__).parent / "config.json"


def main() -> None:
    print("=== TP-Link Tapo Local KLAP v2 Setup ===")
    email = input("Enter your Tapo Account Email: ").strip()
    password = getpass.getpass("Enter your Tapo Account Password (hidden): ")

    if not email or not password:
        print("Error: Email and password cannot be empty.", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    print("\n1. Verifying local KLAP v2 authentication with Tapo A and Tapo B...")
    client_a = TapoKlapClient(cfg["tapo_a_tv_sensor"]["ip"], email, password)
    client_b = TapoKlapClient(cfg["tapo_b_bulb_switch"]["ip"], email, password)

    try:
        print_status(client_a, client_b, cfg)
    except TapoAuthError as err:
        print(f"\nAuthentication failed: {err}", file=sys.stderr)
        sys.exit(1)

    # Save locally with 0600 permissions
    cfg["tapo_credentials"] = {"email": email, "password": password}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)
    print(f"\n2. Saved credentials to {CONFIG_PATH} (permissions 0600).")

    # Sync to ropihung and start service
    print("3. Syncing config.json to ropihung and starting tvlighton.service...")
    subprocess.run(
        ["scp", str(CONFIG_PATH), "ropihung:/home/ropi/tvlighton/config.json"],
        check=True,
    )
    subprocess.run(
        [
            "ssh",
            "ropihung",
            "chmod 600 /home/ropi/tvlighton/config.json && "
            "systemctl --user daemon-reload && "
            "systemctl --user enable --now tvlighton.service && "
            "systemctl --user restart tvlighton.service && "
            "sleep 2 && "
            "systemctl --user status tvlighton.service --no-pager",
        ],
        check=True,
    )
    print("\nSuccess! Tapo TV -> Bulb automation is now live on ropihung.")


if __name__ == "__main__":
    main()
