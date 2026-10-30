import unittest
from pathlib import Path

from utils.listener_window import listener_window_is_open


class ListenerWindowTests(unittest.TestCase):
    def test_socket_can_open_at_preconnect_boundary(self):
        self.assertTrue(listener_window_is_open(95.0, 95))
        self.assertTrue(listener_window_is_open(90.0, 95))
        self.assertTrue(listener_window_is_open(89.99, 95))
        self.assertTrue(listener_window_is_open(1.0, 95))

    def test_does_not_open_before_preconnect_window(self):
        self.assertFalse(listener_window_is_open(95.001, 95))
        self.assertFalse(listener_window_is_open(96.0, 95))
        self.assertFalse(listener_window_is_open(300.0, 95))

    def test_does_not_open_at_or_after_expiry(self):
        self.assertFalse(listener_window_is_open(0.0, 95))
        self.assertFalse(listener_window_is_open(-0.1, 95))

    def test_invalid_values_fail_closed(self):
        for left in (None, float("nan"), float("inf"), "bad"):
            self.assertFalse(listener_window_is_open(left, 95))
        self.assertFalse(listener_window_is_open(10, 0))

    def test_worker_listener_uses_listener_activate_secs(self):
        source = Path("bot.py").read_text(encoding="utf-8")
        start = source.index("    async def price_listener(self):")
        end = source.index("    # ═", start)
        body = source[start:end]
        self.assertIn("activate = self.worker_config.listener_activate_secs", body)
        self.assertIn("if seconds_left <= activate:", body)

    def test_loaded_worker_config_has_listener_and_entry_windows(self):
        from config import WORKER_CONFIGS
        self.assertTrue(WORKER_CONFIGS)
        for worker in WORKER_CONFIGS:
            self.assertGreaterEqual(worker.listener_activate_secs, 1)
            self.assertGreaterEqual(worker.entry_seconds_left, 1)

    def test_config_exposes_directional_entry_settings(self):
        source = Path("config.py").read_text(encoding="utf-8")
        self.assertIn("entry_price", source)
        self.assertIn("stop_loss_pct", source)


if __name__ == "__main__":
    unittest.main()
