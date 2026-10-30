# `tvlighton` — Tapo P110M TV-to-Bulb Smart Home Controller & Web UI (`tv.usdollar.win`)

Zero-dependency local LAN smart home controller and real-time web console for TP-Link **Tapo P110M** smart plugs using the **KLAP v2** protocol, with an IMX290 camera Lux gate.

* **TV Turned ON + room inside the Light-On Range** &rarr; **Tapo A** (`192.168.31.9`) detects current/wattage and **CCX** reads native camera Lux metadata &rarr; **Tapo B** (`192.168.31.136`) turns the bulb on.
* **Above the Light-On Range or TV OFF** &rarr; **Tapo B turns OFF**. Camera Lux is evaluated continuously, so Tapo B may turn on again as soon as the room returns to the configured range while the TV remains active.
* **Overview & Detail Dashboard**: The compact live-control Overview is served at `/`; diagnostics, manual controls, telemetry, and runtime parameters are available at `/detail`.
* **TV-On History**: The Overview immediately shows an open `ON NOW` row whenever Tapo A confirms that the TV is ACTIVE, then records TV-off time and final duration when it leaves the On Range. This history is independent of Tapo B availability. Detailed device operations remain in SQLite for future queries but are not rendered in the UI.

---

## Hardware Inventory (`192.168.31.0/24`)

| Role | Nickname | Host / IP | MAC Address | Device ID |
| :--- | :--- | :--- | :--- | :--- |
| **Tapo A** (TV Current Sensor) | `WSC1F Tapo A P110M` | `192.168.31.9` | `EC-B9-31-25-07-E7` | `703641078388d1ea1b197481d1cbbae9` |
| **Tapo B** (Bulb Switch) | `WSC Tapo B P110M` | `192.168.31.136` | `EC-B9-31-24-FD-FF` | `0f8ba32d18c577b38c27f77031e37e2d` |
| **Controller SBC** | `CCX` | `192.168.31.204` (`ccx.local`) | `B8:27:EB:2B:92:A3` | Raspberry Pi 3 Model A+ Rev 1.0 |
| **Camera light source** | `IMX290 CSI` | `http://ccx.local:8080/state` | — | Native libcamera Lux/exposure/gain metadata |

---

## Web UI & REST API (`Port 8010` / `https://tv.usdollar.win`)

The `tvlighton.service` daemon runs both the `1.5s` KLAP v2 monitoring loop and a threaded HTTP web server on `0.0.0.0:8010`:

* **Live Tapo A & Tapo B Telemetry Cards**: Real-time wattage (`W` & `mW`), relay status (`ON`/`OFF`), today's energy (`Wh`) and runtime (`mins`), Wi-Fi RSSI (`dBm`), and KLAP v2 round-trip latency (`ms`).
* **Real-Time Power Telemetry Chart**: Live canvas plot of Tapo A (TV Watts) and Tapo B (Bulb Watts) against configurable ON/OFF thresholds.
* **CCX Camera Light**: Native IMX290 Lux, exposure, and gain telemetry. The orange **Light-On Range** slider controls the maximum Lux at which the bulb may be lit.
* **Slider Controls**: The dual-handle TV On Range uses `0 / 40 / 80 / 120 W`, with the active interval colored between its circular endpoints. TV OFF remains separately adjustable, and the Light-On Range uses `0 / 15 / 30 / 60 / 120 lux`. Selections are saved to `config.json`.
* **Detailed Tapo A / Tapo B Operation History (`tvlighton_history.db`)**:
  * Logs every automatic trigger (`AUTO_TRIGGER_ON`, `AUTO_TRIGGER_OFF`), device power/relay state transition (`STATE_CHANGE`), manual switch (`MANUAL_CONTROL`), service/config update (`SERVICE_START`), and error (`ERROR`).
  * Records full device state snapshots (`Relay`, `W`, `mW`, `Today Wh`, `RSSI`, `KLAP Latency`) for both **Tapo A** and **Tapo B** on every operation.
  * Retained in the `operations_log` SQL table and available through the API for future queries, without exposing device-operation details in the dashboard.
* **TV Session History (`tv_sessions`)**: Stores each TV-on start, TV-off end, and total duration. Existing sessions are reconstructed once from Tapo A state-change records during migration.

### REST Endpoints
* `GET /` — Interactive Web UI (`web_ui.html`)
* `GET /detail` — Diagnostics, energy/RSSI, manual controls, power chart, poll interval, and debounce settings (`detail.html`)
* `GET /api/status` — Live status of Tapo A, Tapo B, camera Lux, thresholds, debounce streaks, and chart telemetry
* `GET /api/lux-history?limit=1000` — Persisted camera Lux samples, recorded immediately at service start and every 15 minutes on quarter-hour boundaries
* `GET /api/daylight` — Internet-sourced Taipei sunrise and sunset times from Open-Meteo, cached for six hours
* `POST /api/settings-auth` — Verify the Sensors Setting password and issue a short-lived token required by `POST /api/config`
* `GET /api/history?limit=200&device=TAPO_B&event_type=AUTO_TRIGGER_ON` — Detailed operation history from SQLite
* `GET /api/tv-sessions?limit=100` — TV viewing sessions with start, end, active status, and duration
* `POST /api/control` — Manual switch (`{"target": "tapo_b", "state": true}`)
* `POST /api/config` — Live threshold update (`{"on_threshold_watts": 40.0, "on_max_watts": 120.0, "off_threshold_watts": 5.0, "lux_threshold": 60.0, "poll_interval_seconds": 1.5, "debounce_readings": 2}`)

---

## CLI Usage on `CCX`

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

## Camera Lux Metadata on CCX

`tvlighton.py` reads `result.light.lux` from the local camera streamer's `/state` endpoint. The matching streamer source is tracked at `deploy/cca/cca-csi-stream.py`; it adds continuous rpicam metadata parsing without writing an ever-growing metadata file.

Install it at `/usr/local/lib/cca/cca-csi-stream.py`, restart `cca-stream.service`, and confirm:

```bash
curl http://127.0.0.1:8080/state
```

## Existing `tv.usdollar.win` Cloudflare Tunnel (`ropihung`)

The Cloudflare tunnel remains on `ropihung`, while the controller runs on CCX. The socket-proxy units under `deploy/ropihung/` forward only the dashboard traffic from `127.0.0.1:8010` on ropihung to `192.168.31.204:8010` on CCX.

Install and enable them as the `ropi` user on ropihung:

```bash
cp deploy/ropihung/tvlighton-proxy.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tvlighton-proxy.socket
```

For a fresh tunnel configuration on `ropihung`:

1. Run the helper script on `ropihung` to add `tv.usdollar.win -> http://127.0.0.1:8010` to `/etc/cloudflared/config.yml` and restart `cloudflared`:
   ```bash
   ssh -t ropihung 'sudo bash /home/ropi/tvlighton/enable-tv-usdollar-win.sh'
   ```
2. In your Cloudflare DNS dashboard for **`usdollar.win`**, ensure a **CNAME** record exists:
   * **Name**: `tv`
   * **Target**: `dd3d8fb1-4ca9-4ec1-b147-59633ba68506.cfargotunnel.com` (Proxied)
