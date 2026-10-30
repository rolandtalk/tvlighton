import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


app_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(app_dir))
spec = importlib.util.spec_from_file_location("tvlighton", app_dir / "tvlighton.py")
tvlighton = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tvlighton)


class AutomationDecisionTest(unittest.TestCase):
    def decide(self, bulb_is_on, high=0, low=0, dark=0, bright=0, debounce=2, auto_off=True):
        return tvlighton.decide_automatic_bulb_action(
            bulb_is_on, high, low, dark, bright, debounce, auto_off
        )

    def test_turns_on_only_when_tv_active_and_dark(self):
        self.assertIs(self.decide(False, high=2, dark=2), True)
        self.assertIsNone(self.decide(False, high=2, dark=1))
        self.assertIsNone(self.decide(False, high=1, dark=2))

    def test_turns_off_when_tv_is_off_or_lux_is_above_range(self):
        self.assertIs(self.decide(True, low=2, dark=2), False)
        self.assertIs(self.decide(True, high=2, bright=2), False)

    def test_lux_continuously_reenables_bulb_when_room_is_dark_again(self):
        self.assertIs(self.decide(True, high=2, bright=2), False)
        self.assertIs(self.decide(False, high=2, dark=2), True)

    def test_holds_state_until_debounce_is_met(self):
        self.assertIsNone(self.decide(True, high=2, bright=1))
        self.assertIsNone(self.decide(False, high=2, dark=1))

    def test_tv_power_is_on_only_inside_selected_range(self):
        classify = tvlighton.classify_tv_power
        self.assertEqual(classify(40, 40, 120, 5), "on")
        self.assertEqual(classify(80, 40, 120, 5), "on")
        self.assertEqual(classify(120, 40, 120, 5), "on")
        self.assertEqual(classify(4, 40, 120, 5), "off")
        self.assertEqual(classify(121, 40, 120, 5), "off")
        self.assertEqual(classify(20, 40, 120, 5), "transition")

    def test_tv_sessions_record_start_end_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = tvlighton.HistoryStore(Path(tmp_dir) / "sessions.db")
            session_id = store.start_tv_session(started_epoch=1000)
            self.assertEqual(store.start_tv_session(started_epoch=1010), session_id)

            active = store.query_tv_sessions(now_epoch=1065)
            self.assertEqual(len(active), 1)
            self.assertTrue(active[0]["active"])
            self.assertEqual(active[0]["duration_seconds"], 65)

            self.assertEqual(store.end_tv_session(ended_epoch=1125), session_id)
            completed = store.query_tv_sessions(now_epoch=1200)[0]
            self.assertFalse(completed["active"])
            self.assertEqual(completed["duration_seconds"], 125)
            self.assertIsNone(store.end_tv_session(ended_epoch=1300))

    def test_lux_history_records_camera_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = tvlighton.HistoryStore(Path(tmp_dir) / "lux.db")
            light = {
                "online": True,
                "value": 42.5,
                "source": "IMX290 CSI",
                "exposure_time_us": 1234,
                "analogue_gain": 2.0,
                "digital_gain": 1.0,
                "colour_temperature_k": 4200,
            }
            sample_id = store.record_lux_sample(light, sampled_epoch=900)
            self.assertIsNotNone(sample_id)
            sample = store.query_lux_samples()[0]
            self.assertEqual(sample["lux"], 42.5)
            self.assertEqual(sample["exposure_time_us"], 1234)
            self.assertEqual(sample["sampled_epoch"], 900)

    def test_next_lux_sample_is_next_quarter_hour(self):
        self.assertEqual(tvlighton.next_lux_sample_epoch(1), 900)
        self.assertEqual(tvlighton.next_lux_sample_epoch(899), 900)
        self.assertEqual(tvlighton.next_lux_sample_epoch(900), 1800)

    def test_parses_internet_daylight_times(self):
        payload = {
            "daily": {
                "time": ["2026-10-30"],
                "sunrise": ["2026-10-30T05:59"],
                "sunset": ["2026-10-30T17:14"],
            }
        }
        daylight = tvlighton.parse_daylight_payload(payload, "2026-10-30")
        self.assertEqual(daylight["sunrise_time"], "05:59")
        self.assertEqual(daylight["sunset_time"], "17:14")
        self.assertEqual(daylight["source"], "Open-Meteo")


if __name__ == "__main__":
    unittest.main()
