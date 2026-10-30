"""Coordinated graceful shutdown (SIGINT / app lifespan)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import threading
from typing import Any, Awaitable, Callable, List, Optional

_BANNER = "=" * 40
_TITLE = "POLYMARKET BOT"

_lock = threading.Lock()
_shutdown_event: Optional[asyncio.Event] = None
_shutdown_started = False
_shutdown_complete = False
_hooks: List[Callable[[], Awaitable[None]]] = []
_background_tasks: List[asyncio.Task] = []


def _log(msg: str) -> None:
    print(f"[INFO] {msg}")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def shutdown_requested() -> bool:
    ev = _shutdown_event
    if ev is None:
        return False
    return ev.is_set()


def get_shutdown_event() -> asyncio.Event:
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def register_shutdown_hook(coro_fn: Callable[[], Awaitable[None]]) -> None:
    _hooks.append(coro_fn)


def track_background_task(task: asyncio.Task) -> None:
    _background_tasks.append(task)
    task.add_done_callback(lambda t: _background_tasks.remove(t) if t in _background_tasks else None)


async def graceful_shutdown(*, reason: str = "Shutdown signal") -> None:
    global _shutdown_started, _shutdown_complete
    with _lock:
        if _shutdown_started:
            _warn("Shutdown already in progress")
            return
        _shutdown_started = True

    print(_BANNER)
    print(f" {_TITLE}")
    print(_BANNER)
    print()
    _log(f"Shutdown signal received ({reason})")
    _log("Stopping new market entries...")
    get_shutdown_event().set()
    _log("Stopping market scheduler...")
    _log("Closing WebSocket connections...")
    _log("Saving trading state...")
    _log("Reconciling open positions...")
    _log("Releasing background workers...")

    for hook in list(_hooks):
        try:
            await hook()
        except Exception as exc:
            _warn(f"Shutdown hook failed: {type(exc).__name__}: {exc}")

    pending = [t for t in list(_background_tasks) if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        done, still = await asyncio.wait(pending, timeout=8.0)
        for t in still:
            _warn(f"Background task did not stop in time: {t.get_name()}")
        for t in done:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    try:
        from utils.stopped_out_store import stopped_out_store

        stopped_out_store.close()
    except Exception as exc:
        _warn(f"Stopped-out store close: {exc}")

    try:
        from utils.inventory_strategy_store import inventory_strategy_store

        inventory_strategy_store.close()
    except Exception as exc:
        _warn(f"Inventory store close: {exc}")

    print()
    print(_BANNER)
    print(" BOT SHUTDOWN COMPLETE")
    print(_BANNER)
    _shutdown_complete = True


def _sync_signal_handler(signum: int, frame: Any) -> None:
    name = "Ctrl+C" if signum == signal.SIGINT else f"signal {signum}"
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(graceful_shutdown(reason=name))
            return
    except RuntimeError:
        pass
    with _lock:
        get_shutdown_event().set()
    _log(f"Shutdown signal received ({name})")


def install_signal_handlers() -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGINT, _sync_signal_handler)
    except (ValueError, OSError):
        pass
    with contextlib.suppress(ValueError, OSError, AttributeError):
        signal.signal(signal.SIGTERM, _sync_signal_handler)


async def cancel_on_shutdown(task: asyncio.Task, *, name: str = "") -> None:
    """Await task until shutdown, then cancel."""
    shutdown = get_shutdown_event()
    wait_shutdown = asyncio.create_task(shutdown.wait())
    done, _ = await asyncio.wait({task, wait_shutdown}, return_when=asyncio.FIRST_COMPLETED)
    if wait_shutdown in done:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
