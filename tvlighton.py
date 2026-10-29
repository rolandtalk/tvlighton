#!/usr/bin/env python3
"""tvlighton — Smart Home Controller & Web UI (Tapo A TV Sensor -> CCX -> Tapo B Bulb Switch)."""

import argparse
import base64
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from tapo_klap import TapoAuthError, TapoKlapClient, discover_tapo_devices


APP_DIR = Path(__file__).parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "tvlighton_history.db"
WEB_UI_PATH = APP_DIR / "web_ui.html"
DEFAULT_WEB_PORT = int(os.environ.get("TVLIGHTON_PORT", "8010"))
DEFAULT_CAMERA_STATE_URL = "http://127.0.0.1:8080/state"
DEFAULT_CAMERA_SAMPLE_INTERVAL = 5.0


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[{_now_str()}] {msg}", flush=True)


def _decode_nickname(raw_nickname: Optional[str], fallback: str) -> str:
    if not raw_nickname:
        return fallback
    try:
        return base64.b64decode(raw_nickname).decode("utf-8")
    except Exception:
        return raw_nickname


def read_camera_light(state_url: str, timeout: float = 3.0) -> Dict[str, Any]:
    """Return the camera pipeline's auto-exposure lux estimate and metadata."""
    request = urllib.request.Request(state_url, headers={"User-Agent": "tvlighton/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    light = payload.get("result", {}).get("light", {})
    if not light.get("online") or light.get("lux") is None:
        raise RuntimeError("Camera lux metadata is unavailable")
    value = round(float(light["lux"]), 1)

    return {
        "online": True,
        "value": value,
        "unit": "lux",
        "sampled_at": _now_str(),
        "source": "IMX290 CSI",
        "exposure_time_us": light.get("exposure_time_us"),
        "analogue_gain": light.get("analogue_gain"),
        "digital_gain": light.get("digital_gain"),
        "colour_temperature_k": light.get("colour_temperature_k"),
        "error": None,
    }


def decide_automatic_bulb_action(
    bulb_is_on: bool,
    high_streak: int,
    low_streak: int,
    dark_streak: int,
    bright_streak: int,
    debounce_readings: int,
    auto_off: bool,
) -> Optional[bool]:
    """Return True for ON, False for OFF, or None when no relay change is due."""
    if (
        not bulb_is_on
        and high_streak >= debounce_readings
        and dark_streak >= debounce_readings
    ):
        return True
    if bulb_is_on and (
        (auto_off and low_streak >= debounce_readings)
        or bright_streak >= debounce_readings
    ):
        return False
    return None


# ---------------------------------------------------------------------------
# SQLite Operation History & Telemetry Store
# ---------------------------------------------------------------------------
class HistoryStore:
    """Thread-safe SQLite database storing detailed Tapo A and Tapo B operation history."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operations_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_epoch REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    device TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    trigger_reason TEXT NOT NULL,
                    tapo_a_relay INTEGER NOT NULL DEFAULT 1,
                    tapo_a_watts REAL NOT NULL DEFAULT 0.0,
                    tapo_a_mw INTEGER NOT NULL DEFAULT 0,
                    tapo_a_today_wh INTEGER NOT NULL DEFAULT 0,
                    tapo_a_rssi INTEGER NOT NULL DEFAULT 0,
                    tapo_b_relay INTEGER NOT NULL DEFAULT 0,
                    tapo_b_watts REAL NOT NULL DEFAULT 0.0,
                    tapo_b_mw INTEGER NOT NULL DEFAULT 0,
                    tapo_b_today_wh INTEGER NOT NULL DEFAULT 0,
                    tapo_b_rssi INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_ts ON operations_log(ts_epoch DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_dev ON operations_log(device, event_type)"
            )

            # Seed initial historical operations from earlier today if table is empty
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM operations_log")
            if cur.fetchone()["cnt"] == 0:
                seed_rows = [
                    (
                        time.time() - 3900,
                        "2026-10-27 21:14:43",
                        "SYSTEM",
                        "SERVICE_START",
                        "Initial Automation Daemon Start",
                        "Connected to Tapo A (192.168.31.9) and Tapo B (192.168.31.136) over KLAP v2",
                        1, 14.39, 14389, 17, -39,
                        1, 2.15, 2153, 2, -50,
                        42,
                    ),
                    (
                        time.time() - 3750,
                        "2026-10-27 21:17:28",
                        "SYSTEM",
                        "SERVICE_START",
                        "Threshold Calibrated (ON >= 10.0W, OFF <= 5.0W)",
                        "Adjusted TV ON threshold from 15.0W to 10.0W to match 14.39W active load on Tapo A",
                        1, 14.39, 14389, 17, -39,
                        1, 2.15, 2153, 2, -50,
                        38,
                    ),
                    (
                        time.time() - 3600,
                        "2026-10-27 21:19:43",
                        "TAPO_B",
                        "AUTO_TRIGGER_OFF",
                        "Turned Tapo B (Bulb) OFF",
                        "Tapo A TV power dropped to 0.00W (<= 5.0W OFF threshold for 2 consecutive readings)",
                        1, 0.00, 0, 17, -39,
                        0, 0.00, 0, 2, -50,
                        45,
                    ),
                    (
                        time.time() - 3595,
                        "2026-10-27 21:19:48",
                        "TAPO_B",
                        "AUTO_TRIGGER_ON",
                        "Turned Tapo B (Bulb) ON",
                        "Tapo A TV power jumped to 14.54W (>= 10.0W ON threshold for 2 consecutive readings)",
                        1, 14.54, 14540, 18, -39,
                        1, 2.15, 2153, 2, -50,
                        49,
                    ),
                    (
                        time.time() - 3350,
                        "2026-10-27 21:23:55",
                        "TAPO_B",
                        "AUTO_TRIGGER_OFF",
                        "Turned Tapo B (Bulb) OFF",
                        "Tapo A TV power dropped to 0.00W (<= 5.0W OFF threshold for 2 consecutive readings)",
                        1, 0.00, 0, 19, -40,
                        0, 0.00, 0, 2, -52,
                        44,
                    ),
                ]
                conn.executemany(
                    """
                    INSERT INTO operations_log (
                        ts_epoch, timestamp, device, event_type, action, trigger_reason,
                        tapo_a_relay, tapo_a_watts, tapo_a_mw, tapo_a_today_wh, tapo_a_rssi,
                        tapo_b_relay, tapo_b_watts, tapo_b_mw, tapo_b_today_wh, tapo_b_rssi,
                        latency_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    seed_rows,
                )
            conn.commit()

    def record_operation(
        self,
        device: str,
        event_type: str,
        action: str,
        trigger_reason: str,
        state_a: Dict[str, Any],
        state_b: Dict[str, Any],
        latency_ms: int = 0,
    ) -> None:
        now_ep = time.time()
        now_ts = _now_str()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operations_log (
                    ts_epoch, timestamp, device, event_type, action, trigger_reason,
                    tapo_a_relay, tapo_a_watts, tapo_a_mw, tapo_a_today_wh, tapo_a_rssi,
                    tapo_b_relay, tapo_b_watts, tapo_b_mw, tapo_b_today_wh, tapo_b_rssi,
                    latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ep,
                    now_ts,
                    device,
                    event_type,
                    action,
                    trigger_reason,
                    1 if state_a.get("relay_on", True) else 0,
                    float(state_a.get("watts", 0.0)),
                    int(state_a.get("mw", 0)),
                    int(state_a.get("today_wh", 0)),
                    int(state_a.get("rssi", 0)),
                    1 if state_b.get("relay_on", False) else 0,
                    float(state_b.get("watts", 0.0)),
                    int(state_b.get("mw", 0)),
                    int(state_b.get("today_wh", 0)),
                    int(state_b.get("rssi", 0)),
                    int(latency_ms),
                ),
            )
            conn.commit()

    def query_history(
        self,
        limit: int = 200,
        device: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if device:
            clauses.append("device = ?")
            params.append(device)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM operations_log {where_sql} ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_summary_stats(self) -> Dict[str, int]:
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM operations_log").fetchone()[0]
            auto_cnt = conn.execute(
                "SELECT COUNT(*) FROM operations_log WHERE event_type IN ('AUTO_TRIGGER_ON', 'AUTO_TRIGGER_OFF')"
            ).fetchone()[0]
            manual_cnt = conn.execute(
                "SELECT COUNT(*) FROM operations_log WHERE event_type = 'MANUAL_CONTROL'"
            ).fetchone()[0]
            return {
                "total_ops": total,
                "auto_triggers": auto_cnt,
                "manual_ops": manual_cnt,
            }


# ---------------------------------------------------------------------------
# Shared Controller Runtime State
# ---------------------------------------------------------------------------
class TvLightOnRuntime:
    def __init__(self, cfg_path: Path, email: str, password: str):
        self.cfg_path = cfg_path
        self.cfg = load_config(cfg_path)
        self.cfg.setdefault("automation", {}).setdefault("lux_threshold", 60.0)
        self.email = email
        self.password = password
        self.lock = threading.Lock()
        self.history = HistoryStore(DB_PATH)

        self.client_a = TapoKlapClient(self.cfg["tapo_a_tv_sensor"]["ip"], email, password)
        self.client_b = TapoKlapClient(self.cfg["tapo_b_bulb_switch"]["ip"], email, password)

        self.state_a: Dict[str, Any] = {
            "nickname": "WSC1F Tapo A P110M",
            "ip": self.cfg["tapo_a_tv_sensor"]["ip"],
            "mac": self.cfg["tapo_a_tv_sensor"]["mac"],
            "relay_on": True,
            "watts": 0.0,
            "mw": 0,
            "today_wh": 0,
            "today_runtime": 0,
            "rssi": 0,
            "latency_ms": 0,
        }
        self.state_b: Dict[str, Any] = {
            "nickname": "WSC Tapo B P110M",
            "ip": self.cfg["tapo_b_bulb_switch"]["ip"],
            "mac": self.cfg["tapo_b_bulb_switch"]["mac"],
            "relay_on": False,
            "watts": 0.0,
            "mw": 0,
            "today_wh": 0,
            "today_runtime": 0,
            "rssi": 0,
            "latency_ms": 0,
        }
        self.high_streak = 0
        self.low_streak = 0
        self.dark_streak = 0
        self.bright_streak = 0
        self.last_updated = "--"
        self.recent_samples: Deque[Dict[str, Any]] = deque(maxlen=120)
        camera_cfg = self.cfg.get("camera_light", {})
        self.camera_state_url = str(
            camera_cfg.get("state_url", DEFAULT_CAMERA_STATE_URL)
        )
        self.camera_sample_interval = max(
            1.0,
            float(camera_cfg.get("sample_interval_seconds", DEFAULT_CAMERA_SAMPLE_INTERVAL)),
        )
        self.camera_light: Dict[str, Any] = {
            "online": False,
            "value": None,
            "unit": "lux",
            "sampled_at": "--",
            "source": "IMX290 CSI",
            "error": "Waiting for first camera sample",
        }

    def update_camera_light(self) -> None:
        try:
            self.camera_light = read_camera_light(self.camera_state_url)
        except Exception as err:
            self.camera_light = {
                "online": False,
                "value": None,
                "unit": "lux",
                "sampled_at": _now_str(),
                "source": "IMX290 CSI",
                "error": str(err),
            }

    def poll_unit(self, client: TapoKlapClient, unit_cfg: Dict[str, Any], fallback_name: str) -> Dict[str, Any]:
        t0 = time.perf_counter()
        info = client.get_device_info()
        energy = client.get_energy_usage()
        latency_ms = int((time.perf_counter() - t0) * 1000)
        mw = int(energy.get("current_power", 0))
        return {
            "nickname": _decode_nickname(info.get("nickname"), fallback_name),
            "ip": client.host,
            "mac": unit_cfg.get("mac", ""),
            "relay_on": bool(info.get("device_on", False)),
            "watts": round(mw / 1000.0, 3),
            "mw": mw,
            "today_wh": int(energy.get("today_energy", 0)),
            "today_runtime": int(energy.get("today_runtime", 0)),
            "rssi": int(info.get("rssi", 0)),
            "latency_ms": latency_ms,
        }

    def manual_control(self, target: str, desired_on: bool) -> Dict[str, Any]:
        with self.lock:
            t0 = time.perf_counter()
            if target == "tapo_a":
                self.client_a.set_device_on(desired_on)
                lat = int((time.perf_counter() - t0) * 1000)
                self.state_a = self.poll_unit(self.client_a, self.cfg["tapo_a_tv_sensor"], "Tapo A")
                action = f"Manual Tapo A Relay {'ON' if desired_on else 'OFF'}"
                reason = f"User manually set Tapo A (TV Sensor) relay to {'ON' if desired_on else 'OFF'} via Web UI"
                self.history.record_operation(
                    "TAPO_A", "MANUAL_CONTROL", action, reason, self.state_a, self.state_b, lat
                )
                return {"ok": True, "message": action}
            elif target == "tapo_b":
                self.client_b.set_device_on(desired_on)
                lat = int((time.perf_counter() - t0) * 1000)
                self.state_b = self.poll_unit(self.client_b, self.cfg["tapo_b_bulb_switch"], "Tapo B")
                action = f"Manual Tapo B (Bulb) {'ON' if desired_on else 'OFF'}"
                reason = f"User manually switched Tapo B (Bulb) {'ON' if desired_on else 'OFF'} via Web UI"
                self.history.record_operation(
                    "TAPO_B", "MANUAL_CONTROL", action, reason, self.state_a, self.state_b, lat
                )
                return {"ok": True, "message": action}
            else:
                return {"ok": False, "error": f"Unknown target: {target}"}

    def update_thresholds(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            auto = self.cfg.setdefault("automation", {})
            old_on = auto.get("on_threshold_watts", 10.0)
            old_off = auto.get("off_threshold_watts", 5.0)
            old_lux = auto.get("lux_threshold", 60.0)
            if "on_threshold_watts" in payload:
                auto["on_threshold_watts"] = float(payload["on_threshold_watts"])
            if "off_threshold_watts" in payload:
                auto["off_threshold_watts"] = float(payload["off_threshold_watts"])
            if "lux_threshold" in payload:
                auto["lux_threshold"] = max(0.0, float(payload["lux_threshold"]))
            if "poll_interval_seconds" in payload:
                auto["poll_interval_seconds"] = max(0.5, float(payload["poll_interval_seconds"]))
            if "debounce_readings" in payload:
                auto["debounce_readings"] = max(1, int(payload["debounce_readings"]))
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            os.chmod(self.cfg_path, 0o600)

            action = (
                f"Updated Thresholds (ON >= {auto['on_threshold_watts']}W, "
                f"OFF <= {auto['off_threshold_watts']}W, Light-On 0-{auto['lux_threshold']} lux)"
            )
            reason = (
                f"Changed via Web UI from ON>={old_on}W / OFF<={old_off}W / Light-On 0-{old_lux} lux to "
                f"ON>={auto['on_threshold_watts']}W / OFF<={auto['off_threshold_watts']}W "
                f"/ Light-On 0-{auto['lux_threshold']} lux "
                f"(poll={auto['poll_interval_seconds']}s, debounce={auto['debounce_readings']})"
            )
            self.history.record_operation(
                "SYSTEM", "SERVICE_START", action, reason, self.state_a, self.state_b, 0
            )
            return {"ok": True, "automation": auto}

    def get_status_snapshot(self) -> Dict[str, Any]:
        return {
            "hostname": socket.gethostname(),
            "timestamp": self.last_updated,
            "tapo_a": self.state_a,
            "tapo_b": self.state_b,
            "camera_light": dict(self.camera_light),
            "high_streak": self.high_streak,
            "low_streak": self.low_streak,
            "dark_streak": self.dark_streak,
            "bright_streak": self.bright_streak,
            "automation": self.cfg.get("automation", {}),
            "stats": self.history.get_summary_stats(),
            "recent_samples": list(self.recent_samples),
        }


# ---------------------------------------------------------------------------
# Built-in Web UI HTTP Server
# ---------------------------------------------------------------------------
def make_http_handler(runtime: TvLightOnRuntime):
    class TvLightOnHTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Silence standard per-poll access logs to keep systemd journal clean
            pass

        def _send_json(self, data: Any, status: int = 200) -> None:
            raw = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_HEAD(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                html_bytes = WEB_UI_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(html_bytes)
                return

            if path == "/api/status":
                self._send_json(runtime.get_status_snapshot())
                return

            if path == "/api/history":
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int(qs.get("limit", ["200"])[0])
                device = qs.get("device", [None])[0]
                event_type = qs.get("event_type", [None])[0]
                items = runtime.history.query_history(limit=limit, device=device, event_type=event_type)
                self._send_json({"items": items})
                return

            self._send_json({"error": "Not found"}, status=404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                payload = json.loads(body)
            except Exception:
                self._send_json({"ok": False, "error": "Invalid JSON"}, status=400)
                return

            if parsed.path == "/api/control":
                target = payload.get("target", "")
                state = bool(payload.get("state", False))
                try:
                    res = runtime.manual_control(target, state)
                    self._send_json(res, status=200 if res.get("ok") else 400)
                except Exception as err:
                    self._send_json({"ok": False, "error": str(err)}, status=500)
                return

            if parsed.path == "/api/config":
                try:
                    res = runtime.update_thresholds(payload)
                    self._send_json(res)
                except Exception as err:
                    self._send_json({"ok": False, "error": str(err)}, status=500)
                return

            self._send_json({"error": "Not found"}, status=404)

    return TvLightOnHTTPHandler


# ---------------------------------------------------------------------------
# Configuration & CLI Utilities
# ---------------------------------------------------------------------------
def load_config(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_credentials(cfg: Dict[str, Any], args: argparse.Namespace) -> tuple[str, str]:
    creds = cfg.get("tapo_credentials", {})
    email = getattr(args, "email", None) or os.environ.get("TAPO_EMAIL") or creds.get("email", "")
    password = getattr(args, "password", None) or os.environ.get("TAPO_PASSWORD") or creds.get("password", "")
    return email.strip(), password


def refresh_device_ips_by_mac(cfg: Dict[str, Any]) -> Optional[str]:
    """Updates Tapo A and Tapo B IPs if DHCP reassigned them on the router."""
    subnet = cfg.get("automation", {}).get("subnet_prefix", "192.168.31")
    discovered = discover_tapo_devices(subnet_prefix=subnet)
    changes = []
    for key in ("tapo_a_tv_sensor", "tapo_b_bulb_switch"):
        dev_cfg = cfg.get(key, {})
        target_mac = dev_cfg.get("mac", "").upper().replace(":", "-")
        target_id = dev_cfg.get("device_id", "")
        for ip, info in discovered.items():
            mac = info.get("mac", "").upper().replace(":", "-")
            dev_id = info.get("device_id", "")
            if (target_mac and mac == target_mac) or (target_id and dev_id == target_id):
                if dev_cfg.get("ip") != ip:
                    old_ip = dev_cfg.get("ip")
                    dev_cfg["ip"] = ip
                    msg = f"Updated {dev_cfg.get('name', key)} IP: {old_ip} -> {ip}"
                    _log(msg)
                    changes.append(msg)
    return "; ".join(changes) if changes else None


def print_status(client_a: TapoKlapClient, client_b: TapoKlapClient, cfg: Dict[str, Any]) -> None:
    for label, client, unit_cfg in [
        ("Tapo A (TV Sensor)", client_a, cfg["tapo_a_tv_sensor"]),
        ("Tapo B (Bulb Switch)", client_b, cfg["tapo_b_bulb_switch"]),
    ]:
        info = client.get_device_info()
        energy = client.get_energy_usage()
        nickname = _decode_nickname(info.get("nickname"), label)
        relay_on = info.get("device_on", False)
        power_w = float(energy.get("current_power", 0.0)) / 1000.0
        today_wh = energy.get("today_energy", 0)
        today_min = energy.get("today_runtime", 0)
        rssi = info.get("rssi", "N/A")
        print(
            f"• {label} [{nickname}] @ {client.host} ({unit_cfg.get('mac')})\n"
            f"    Relay State : {'ON' if relay_on else 'OFF'}\n"
            f"    Live Power  : {power_w:.2f} W ({energy.get('current_power', 0)} mW)\n"
            f"    Today Usage : {today_wh} Wh ({today_min} mins) | Wi-Fi RSSI: {rssi} dBm"
        )


def run_monitor_and_web_server(runtime: TvLightOnRuntime, web_port: int) -> None:
    # Start HTTP server thread
    server = ThreadingHTTPServer(("0.0.0.0", web_port), make_http_handler(runtime))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    _log(f"Web UI & REST API listening on http://0.0.0.0:{web_port}")

    def camera_light_loop() -> None:
        while True:
            runtime.update_camera_light()
            time.sleep(runtime.camera_sample_interval)

    threading.Thread(target=camera_light_loop, daemon=True).start()

    # Initial poll of Tapo A & Tapo B
    try:
        with runtime.lock:
            runtime.state_a = runtime.poll_unit(runtime.client_a, runtime.cfg["tapo_a_tv_sensor"], "Tapo A")
            runtime.state_b = runtime.poll_unit(runtime.client_b, runtime.cfg["tapo_b_bulb_switch"], "Tapo B")
            runtime.last_updated = _now_str()

            keep_a_on = bool(runtime.cfg.get("automation", {}).get("keep_tapo_a_always_on", True))
            if keep_a_on and not runtime.state_a["relay_on"]:
                _log("Tapo A relay was OFF; switching Tapo A ON so TV has power...")
                t0 = time.perf_counter()
                runtime.client_a.set_device_on(True)
                lat = int((time.perf_counter() - t0) * 1000)
                runtime.state_a["relay_on"] = True
                runtime.history.record_operation(
                    "TAPO_A",
                    "STATE_CHANGE",
                    "Auto-Restored Tapo A Relay ON",
                    "keep_tapo_a_always_on enabled; restored TV power supply relay",
                    runtime.state_a,
                    runtime.state_b,
                    lat,
                )

            auto = runtime.cfg.get("automation", {})
            runtime.history.record_operation(
                "SYSTEM",
                "SERVICE_START",
                f"tvlighton Service Active (Port {web_port})",
                f"Monitoring Tapo A ({runtime.client_a.host}) -> Tapo B ({runtime.client_b.host}), "
                f"ON >= {auto.get('on_threshold_watts', 10.0)}W, "
                f"OFF <= {auto.get('off_threshold_watts', 5.0)}W, "
                f"Light-On 0-{auto.get('lux_threshold', 60.0)} lux",
                runtime.state_a,
                runtime.state_b,
                runtime.state_a["latency_ms"] + runtime.state_b["latency_ms"],
            )
    except Exception as err:
        _log(f"Initial poll warning: {err}")

    prev_a_active: Optional[bool] = None
    prev_b_relay: Optional[bool] = runtime.state_b.get("relay_on")

    while True:
        auto_cfg = runtime.cfg.get("automation", {})
        poll_sec = float(auto_cfg.get("poll_interval_seconds", 1.5))
        on_watts = float(auto_cfg.get("on_threshold_watts", 10.0))
        off_watts = float(auto_cfg.get("off_threshold_watts", 5.0))
        lux_threshold = float(auto_cfg.get("lux_threshold", 60.0))
        debounce_n = int(auto_cfg.get("debounce_readings", 2))
        auto_off = bool(auto_cfg.get("turn_off_bulb_when_tv_off", True))
        keep_a_on = bool(auto_cfg.get("keep_tapo_a_always_on", True))

        try:
            with runtime.lock:
                runtime.state_a = runtime.poll_unit(runtime.client_a, runtime.cfg["tapo_a_tv_sensor"], "Tapo A")
                runtime.state_b = runtime.poll_unit(runtime.client_b, runtime.cfg["tapo_b_bulb_switch"], "Tapo B")
                runtime.last_updated = _now_str()

                watts_a = runtime.state_a["watts"]
                bulb_is_on = runtime.state_b["relay_on"]
                light_lux = (
                    float(runtime.camera_light["value"])
                    if runtime.camera_light.get("online")
                    and runtime.camera_light.get("value") is not None
                    else None
                )

                runtime.recent_samples.append(
                    {
                        "ts": runtime.last_updated,
                        "a_w": watts_a,
                        "b_w": runtime.state_b["watts"],
                        "a_on": runtime.state_a["relay_on"],
                        "b_on": bulb_is_on,
                        "camera_lux": runtime.camera_light.get("value"),
                    }
                )

                # Detect external relay change on Tapo B (e.g., via physical button or Tapo App)
                if prev_b_relay is not None and bulb_is_on != prev_b_relay:
                    runtime.history.record_operation(
                        "TAPO_B",
                        "STATE_CHANGE",
                        f"Tapo B Relay Changed to {'ON' if bulb_is_on else 'OFF'}",
                        "Detected external state change on Tapo B (physical button or Tapo mobile app)",
                        runtime.state_a,
                        runtime.state_b,
                        runtime.state_b["latency_ms"],
                    )
                    prev_b_relay = bulb_is_on

                # Ensure Tapo A stays ON if keep_tapo_a_always_on is true
                if keep_a_on and not runtime.state_a["relay_on"]:
                    t0 = time.perf_counter()
                    runtime.client_a.set_device_on(True)
                    lat = int((time.perf_counter() - t0) * 1000)
                    runtime.state_a["relay_on"] = True
                    runtime.history.record_operation(
                        "TAPO_A",
                        "STATE_CHANGE",
                        "Restored Tapo A Relay ON",
                        "Tapo A was turned off; automatically restored ON so TV sensor remains powered",
                        runtime.state_a,
                        runtime.state_b,
                        lat,
                    )

                # Track streaks & log significant TV power state transitions on Tapo A
                if watts_a >= on_watts:
                    runtime.high_streak += 1
                    runtime.low_streak = 0
                elif watts_a <= off_watts:
                    runtime.low_streak += 1
                    runtime.high_streak = 0
                else:
                    runtime.high_streak = 0
                    runtime.low_streak = 0

                if light_lux is None:
                    runtime.dark_streak = 0
                    runtime.bright_streak = 0
                elif light_lux <= lux_threshold:
                    runtime.dark_streak += 1
                    runtime.bright_streak = 0
                else:
                    runtime.bright_streak += 1
                    runtime.dark_streak = 0

                if runtime.high_streak == debounce_n and prev_a_active is not True:
                    prev_a_active = True
                    runtime.history.record_operation(
                        "TAPO_A",
                        "STATE_CHANGE",
                        f"Tapo A TV Active Power Detected ({watts_a:.2f}W)",
                        f"TV power rose to {watts_a:.2f}W ({runtime.state_a['mw']} mW) >= {on_watts:.1f}W for {debounce_n} consecutive readings",
                        runtime.state_a,
                        runtime.state_b,
                        runtime.state_a["latency_ms"],
                    )
                elif runtime.low_streak == debounce_n and prev_a_active is not False:
                    if prev_a_active is True:
                        runtime.history.record_operation(
                            "TAPO_A",
                            "STATE_CHANGE",
                            f"Tapo A TV Standby/Off Detected ({watts_a:.2f}W)",
                            f"TV power dropped to {watts_a:.2f}W ({runtime.state_a['mw']} mW) <= {off_watts:.1f}W for {debounce_n} consecutive readings",
                            runtime.state_a,
                            runtime.state_b,
                            runtime.state_a["latency_ms"],
                        )
                    prev_a_active = False

                # Keep Tapo B lit only while TV is active and lux is inside the 0-X range.
                bulb_action = decide_automatic_bulb_action(
                    bulb_is_on,
                    runtime.high_streak,
                    runtime.low_streak,
                    runtime.dark_streak,
                    runtime.bright_streak,
                    debounce_n,
                    auto_off,
                )
                if bulb_action is True:
                    _log(
                        f"TV active ({watts_a:.2f}W) and room dark ({light_lux:.1f} lux "
                        f"<= {lux_threshold:.1f}) -> Turning Tapo B (Bulb) ON"
                    )
                    t0 = time.perf_counter()
                    runtime.client_b.set_device_on(True)
                    lat = int((time.perf_counter() - t0) * 1000)
                    runtime.state_b = runtime.poll_unit(runtime.client_b, runtime.cfg["tapo_b_bulb_switch"], "Tapo B")
                    prev_b_relay = runtime.state_b["relay_on"]
                    runtime.history.record_operation(
                        "TAPO_B",
                        "AUTO_TRIGGER_ON",
                        "Turned Tapo B (Bulb) ON",
                        f"Triggered by TV load ({watts_a:.2f}W >= {on_watts:.1f}W) and "
                        f"light inside range ({light_lux:.1f} lux <= {lux_threshold:.1f}) "
                        f"for {debounce_n} readings",
                        runtime.state_a,
                        runtime.state_b,
                        lat,
                    )
                elif bulb_action is False:
                    if auto_off and runtime.low_streak >= debounce_n:
                        off_reason = (
                            f"TV standby ({watts_a:.2f}W <= {off_watts:.1f}W) "
                            f"for {debounce_n} readings"
                        )
                    else:
                        off_reason = (
                            f"light above range ({light_lux:.1f} lux > {lux_threshold:.1f}) "
                            f"for {debounce_n} readings"
                        )
                    _log(f"{off_reason} -> Turning Tapo B (Bulb) OFF")
                    t0 = time.perf_counter()
                    runtime.client_b.set_device_on(False)
                    lat = int((time.perf_counter() - t0) * 1000)
                    runtime.state_b = runtime.poll_unit(runtime.client_b, runtime.cfg["tapo_b_bulb_switch"], "Tapo B")
                    prev_b_relay = runtime.state_b["relay_on"]
                    runtime.history.record_operation(
                        "TAPO_B",
                        "AUTO_TRIGGER_OFF",
                        "Turned Tapo B (Bulb) OFF",
                        f"Triggered by {off_reason}",
                        runtime.state_a,
                        runtime.state_b,
                        lat,
                    )

        except TapoAuthError as err:
            _log(f"Authentication error: {err}")
            runtime.history.record_operation(
                "SYSTEM", "ERROR", "KLAP Authentication Error", str(err), runtime.state_a, runtime.state_b, 0
            )
            sys.exit(1)
        except Exception as err:
            _log(f"Communication warning ({err}); checking if DHCP IP changed...")
            try:
                ip_change = refresh_device_ips_by_mac(runtime.cfg)
                runtime.client_a.host = runtime.cfg["tapo_a_tv_sensor"]["ip"]
                runtime.client_b.host = runtime.cfg["tapo_b_bulb_switch"]["ip"]
                if ip_change:
                    runtime.history.record_operation(
                        "SYSTEM", "STATE_CHANGE", "DHCP IP Auto-Updated", ip_change, runtime.state_a, runtime.state_b, 0
                    )
            except Exception:
                pass

        time.sleep(poll_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="tvlighton — Tapo P110M TV -> Bulb Smart Home Controller & Web UI")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config.json")
    parser.add_argument("--email", type=str, help="TP-Link Tapo account email")
    parser.add_argument("--password", type=str, help="TP-Link Tapo account password")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="Web UI HTTP port (default 8010)")
    parser.add_argument("--discover", action="store_true", help="Scan LAN for Tapo P110M devices and exit")
    parser.add_argument("--status", action="store_true", help="Read live power & relay status of Tapo A and Tapo B")
    parser.add_argument("--bulb", choices=["on", "off"], help="Manually switch Tapo B (Bulb) on or off")
    parser.add_argument("--monitor", action="store_true", help="Run continuous TV -> Bulb automation loop + Web UI")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.discover:
        subnet = cfg.get("automation", {}).get("subnet_prefix", "192.168.31")
        _log(f"Scanning {subnet}.0/24 for Tapo devices on UDP port 20002...")
        devices = discover_tapo_devices(subnet_prefix=subnet)
        for ip, info in sorted(devices.items()):
            print(
                f"  • {ip:15s} | Model: {info.get('device_model')} | "
                f"MAC: {info.get('mac')} | Device ID: {info.get('device_id')}"
            )
        return

    email, password = resolve_credentials(cfg, args)
    if not email or not password:
        print(
            "Error: TP-Link Tapo credentials are required for KLAP v2 local communication.\n"
            f"Run: python3 {APP_DIR / 'setup_credentials.py'}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.bulb:
        runtime = TvLightOnRuntime(args.config, email, password)
        target_state = args.bulb == "on"
        _log(f"Switching Tapo B ({runtime.client_b.host}) {'ON' if target_state else 'OFF'}...")
        runtime.state_a = runtime.poll_unit(runtime.client_a, cfg["tapo_a_tv_sensor"], "Tapo A")
        res = runtime.manual_control("tapo_b", target_state)
        _log(res.get("message", "Done."))
        return

    if args.status:
        client_a = TapoKlapClient(cfg["tapo_a_tv_sensor"]["ip"], email, password)
        client_b = TapoKlapClient(cfg["tapo_b_bulb_switch"]["ip"], email, password)
        print_status(client_a, client_b, cfg)
        return

    runtime = TvLightOnRuntime(args.config, email, password)
    run_monitor_and_web_server(runtime, args.port)


if __name__ == "__main__":
    main()
