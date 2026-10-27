# `tvlighton` — Tapo P110M TV-to-Bulb Smart Home Controller & Web UI (`tv.usdollar.win`)

Zero-dependency local LAN smart home controller and real-time web console for TP-Link **Tapo P110M** smart plugs using the **KLAP v2** protocol.

* **TV Turned ON** &rarr; **Tapo A** (`192.168.31.9`) detects current/wattage rise &rarr; **`ropihung`** Raspberry Pi (`192.168.31.217`) signals **Tapo B** (`192.168.31.136`) &rarr; **Bulb Turns ON**.
* **Web Dashboard & Detailed Operation History**: Served on port `8010` (`http://ropihung.local:8010` / `https://tv.usdollar.win`).

---

## Hardware Inventory (`192.168.31.0/24`)

| Role | Nickname | Host / IP | MAC Address | Device ID |
| :--- | :--- | :--- | :--- | :--- |
| **Tapo A** (TV Current Sensor) | `WSC1F Tapo A P110M` | `192.168.31.9` | `EC-B9-31-25-07-E7` | `703641078388d1ea1b197481d1cbbae9` |
| **Tapo B** (Bulb Switch) | `WSC Tapo B P110M` | `192.168.31.136` | `EC-B9-31-24-FD-FF` | `0f8ba32d18c577b38c27f77031e37e2d` |
| **Controller SBC** | `ropihung` | `192.168.31.217` (`ropihung.local`) | `88:A2:9E:F0:91:30` | Raspberry Pi 5 (`aarch64`) |

---

## Web UI & REST API (`Port 8010` / `https://tv.usdollar.win`)

The `tvlighton.service` daemon runs both the `1.5s` KLAP v2 monitoring loop and a threaded HTTP web server on `0.0.0.0:8010`:

* **Live Tapo A & Tapo B Telemetry Cards**: Real-time wattage (`W` & `mW`), relay status (`ON`/`OFF`), today's energy (`Wh`) and runtime (`mins`), Wi-Fi RSSI (`dBm`), and KLAP v2 round-trip latency (`ms`).
* **Real-Time Power Telemetry Chart**: Live canvas plot of Tapo A (TV Watts) and Tapo B (Bulb Watts) against configurable ON/OFF thresholds.
* **Detailed Tapo A / Tapo B Operation History (`tvlighton_history.db`)**:
  * Logs every automatic trigger (`AUTO_TRIGGER_ON`, `AUTO_TRIGGER_OFF`), device power/relay state transition (`STATE_CHANGE`), manual switch (`MANUAL_CONTROL`), service/config update (`SERVICE_START`), and error (`ERROR`).
  * Records full device state snapshots (`Relay`, `W`, `mW`, `Today Wh`, `RSSI`, `KLAP Latency`) for both **Tapo A** and **Tapo B** on every operation.
  * Filterable by device, event type, free-text search, and exportable to **CSV**.

### REST Endpoints
* `GET /` — Interactive Web UI (`web_ui.html`)
* `GET /api/status` — Live status of Tapo A, Tapo B, thresholds, streaks, and chart telemetry
* `GET /api/history?limit=200&device=TAPO_B&event_type=AUTO_TRIGGER_ON` — Detailed operation history from SQLite
* `POST /api/control` — Manual switch (`{"target": "tapo_b", "state": true}`)
* `POST /api/config` — Live threshold update (`{"on_threshold_watts": 10.0, "off_threshold_watts": 5.0, "poll_interval_seconds": 1.5, "debounce_readings": 2}`)

---

## CLI Usage on `ropihung`

```bash
# 1. Discover Tapo units on LAN
python3 /home/ropi/tvlighton/tvlighton.py --discover

# 2. Check live power & relay status of Tapo A and Tapo B
python3 /home/ropi/tvlighton/tvlighton.py --status

# 3. Test switching Tapo B (Bulb) manually
python3 /home/ropi/tvlighton/tvlighton.py --bulb on
python3 /home/ropi/tvlighton/tvlighton.py --bulb off

# 4. Manage the background systemd service (runs monitor loop + Web UI on port 8010)
systemctl --user status tvlighton.service
systemctl --user restart tvlighton.service
```

---

## Enabling `tv.usdollar.win` on Cloudflare Tunnel (`ropihung`)

1. Run the helper script on `ropihung` to add `tv.usdollar.win -> http://127.0.0.1:8010` to `/etc/cloudflared/config.yml` and restart `cloudflared`:
   ```bash
   ssh -t ropihung 'sudo bash /home/ropi/tvlighton/enable-tv-usdollar-win.sh'
   ```
2. In your Cloudflare DNS dashboard for **`usdollar.win`**, ensure a **CNAME** record exists:
   * **Name**: `tv`
   * **Target**: `dd3d8fb1-4ca9-4ec1-b147-59633ba68506.cfargotunnel.com` (Proxied)
