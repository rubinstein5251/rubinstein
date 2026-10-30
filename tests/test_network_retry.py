import asyncio
import unittest

from utils.network_retry import create_with_network_retries, is_transient_connectivity_error


class NetworkRetryTests(unittest.TestCase):
    def test_detects_dns_error_in_exception_chain(self):
        try:
            try:
                raise OSError(11001, "getaddrinfo failed")
            except OSError as inner:
                raise RuntimeError("PolyApiException: Request exception!") from inner
        except RuntimeError as exc:
            self.assertTrue(is_transient_connectivity_error(exc))

    def test_does_not_retry_authentication_error(self):
        self.assertFalse(is_transient_connectivity_error(RuntimeError("invalid API credentials")))

    def test_retries_transient_error_then_succeeds(self):
        attempts = {"count": 0}
        waits = []
        reports = []

        def factory():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise OSError("getaddrinfo failed")
            return "connected"

        async def fake_sleep(seconds):
            waits.append(seconds)

        async def fake_run_sync(fn):
            return fn()

        result = asyncio.run(create_with_network_retries(
            factory,
            max_attempts=4,
            sleep=fake_sleep,
            run_sync=fake_run_sync,
            report=reports.append,
        ))
        self.assertEqual(result, "connected")
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(waits, [2, 4])
        self.assertEqual(len(reports), 2)

    def test_does_not_retry_non_network_error(self):
        attempts = {"count": 0}

        def factory():
            attempts["count"] += 1
            raise ValueError("Missing PRIVATE_KEY")

        async def fake_run_sync(fn):
            return fn()

        with self.assertRaisesRegex(ValueError, "Missing PRIVATE_KEY"):
            asyncio.run(create_with_network_retries(
                factory, max_attempts=5, run_sync=fake_run_sync,
                sleep=lambda _seconds: asyncio.sleep(0), report=lambda _message: None,
            ))
        self.assertEqual(attempts["count"], 1)


if __name__ == "__main__":
    unittest.main()
