"""Graceful shutdown coordinator."""

from __future__ import annotations

import asyncio
import io
import sys
import unittest
from contextlib import redirect_stdout

from utils import graceful_shutdown as gs


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    def _reset_shutdown_state(self) -> None:
        gs._shutdown_started = False
        gs._shutdown_complete = False
        gs._shutdown_scheduled = False
        gs._sigint_count = 0
        gs._active_shutdown = None
        gs._async_lock = None
        gs._shutdown_event = asyncio.Event()

    async def test_shutdown_sets_event_and_banner(self):
        self._reset_shutdown_state()
        buf = io.StringIO()
        with redirect_stdout(buf):
            await gs.graceful_shutdown(reason="Ctrl+C")
        out = buf.getvalue()
        self.assertTrue(gs.get_shutdown_event().is_set())
        self.assertIn("POLYMARKET BOT", out)
        self.assertIn("Shutdown signal received", out)
        self.assertIn("BOT SHUTDOWN COMPLETE", out)
        self.assertNotIn("Traceback", out)

    async def test_idempotent_shutdown(self):
        self._reset_shutdown_state()
        gs._shutdown_scheduled = False
        gs._active_shutdown = None
        gs._async_lock = None
        await asyncio.gather(
            gs.graceful_shutdown(reason="first"),
            gs.graceful_shutdown(reason="second"),
        )
        self.assertTrue(gs._shutdown_complete)


if __name__ == "__main__":
    unittest.main()
