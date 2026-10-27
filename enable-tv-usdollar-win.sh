#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script with sudo on ropihung." >&2
  exit 1
fi

python3 - <<'PY'
from pathlib import Path

for cfg_path in [Path("/etc/cloudflared/config.yml"), Path("/home/ropi/HC/deploy/cloudflared-config.yml")]:
    if not cfg_path.exists():
        continue
    text = cfg_path.read_text(encoding="utf-8")
    if "tv.usdollar.win" not in text:
        target = "  - service: http_status:404"
        addition = "  - hostname: tv.usdollar.win\n    service: http://127.0.0.1:8010\n" + target
        if target in text:
            text = text.replace(target, addition, 1)
            cfg_path.write_text(text, encoding="utf-8")
            print(f"Added tv.usdollar.win -> http://127.0.0.1:8010 in {cfg_path}")
    else:
        print(f"tv.usdollar.win already present in {cfg_path}")
PY

systemctl restart cloudflared.service
systemctl is-active cloudflared.service
echo "cloudflared restarted with tv.usdollar.win -> http://127.0.0.1:8010"
