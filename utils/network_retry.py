"""Small, testable retry helpers for transient API/DNS failures at startup."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Awaitable
from typing import TypeVar

T = TypeVar("T")

_TRANSIENT_MARKERS = (
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "timed out",
    "request exception!",
)


def is_transient_connectivity_error(exc: BaseException) -> bool:
    """Inspect chained exceptions for network errors, not auth/config failures."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = f"{type(current).__name__}: {current}".lower()
        if any(marker in message for marker in _TRANSIENT_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


async def create_with_network_retries(
    factory: Callable[[], T],
    *,
    max_attempts: int = 5,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    run_sync: Callable[..., Awaitable[T]] = asyncio.to_thread,
    report: Callable[[str], object] = print,
) -> T:
    """Run a synchronous constructor off-loop; retry only transient network failures."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    delays = (2, 4, 8, 16, 30)
    for attempt in range(1, max_attempts + 1):
        try:
            return await run_sync(factory)
        except Exception as exc:
            if not is_transient_connectivity_error(exc) or attempt >= max_attempts:
                raise
            delay = delays[min(attempt - 1, len(delays) - 1)]
            report(
                f"⚠️ Polymarket API/DNS connection failed during startup "
                f"(attempt {attempt}/{max_attempts}): {exc}. "
                f"Retrying in {delay}s; no orders will be submitted."
            )
            await sleep(delay)
    raise RuntimeError("Account initialization retry loop ended unexpectedly")
