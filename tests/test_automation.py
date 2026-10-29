import importlib.util
import sys
import unittest
from pathlib import Path


app_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(app_dir))
spec = importlib.util.spec_from_file_location("tvlighton", app_dir / "tvlighton.py")
tvlighton = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tvlighton)


class AutomationDecisionTest(unittest.TestCase):
    def decide(self, bulb_is_on, high=0, low=0, dark=0, bright=0, lockout=False, debounce=2, auto_off=True):
        return tvlighton.decide_automatic_bulb_action(
            bulb_is_on, high, low, dark, bright, lockout, debounce, auto_off
        )

    def test_turns_on_only_when_tv_active_and_dark(self):
        self.assertIs(self.decide(False, high=2, dark=2), True)
        self.assertIsNone(self.decide(False, high=2, dark=1))
        self.assertIsNone(self.decide(False, high=1, dark=2))

    def test_turns_off_when_tv_is_off_or_lux_is_above_range(self):
        self.assertIs(self.decide(True, low=2, dark=2), False)
        self.assertIs(self.decide(True, high=2, bright=2), False)

    def test_lux_lockout_prevents_bulb_feedback_cycle(self):
        self.assertIsNone(self.decide(False, high=2, dark=2, lockout=True))

    def test_holds_state_until_debounce_is_met(self):
        self.assertIsNone(self.decide(True, high=2, bright=1))
        self.assertIsNone(self.decide(False, high=2, dark=1))


if __name__ == "__main__":
    unittest.main()
