import asyncio
import contextlib
import json
import logging
import math
import os
import random
import re
import threading
import requests  # type: ignore
import websockets  # type: ignore
import time as t
import aiohttp
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from typing import Deque, Dict, Any, Optional, cast, List, Tuple
from dotenv import load_dotenv  # type: ignore
from web3 import Web3  # type: ignore
from web3.types import TxParams, Wei  # type: ignore
from collections import deque
from zoneinfo import ZoneInfo

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderPayload, OrderType, ApiCreds, AssetType, BalanceAllowanceParams
from py_clob_client_v2.order_builder.constants import BUY, SELL
from py_clob_client_v2 import Side, SignatureTypeV2

from rich.console import Console
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich import box
from rich.text import Text

from config import (
    DRY_RUN_DEFAULT,
    MIN_SHARES,
    PNL_FILES,
    TOTAL_BOTS,
    TRADING_ASSETS,
    TRADING_ASSETS_UPPER,
    WORKER_CONFIGS,
    WorkerConfig,
    asset_pnl_filename,
    validate_trading_assets,
    worker_key,
)
from strategies.base import MomentumDecision
from strategies.momentum import InventoryAccumulationStrategy, MomentumStrategy
from utils.order_book_math import (
    executable_buy_from_asks,
    executable_sell_from_bids,
    best_level_fallback,
)
from utils.inventory_cost import matched_combined_average, InventorySnapshot
from utils.inventory_balance import (
    EffectiveInventory,
    BuyApproval,
    BuySizePlan,
    plan_single_leg_buy,
    log_buy_plan,
)
from strategies.arbigab_engines import ArbigabEngineRouter, economic_position
from utils.momentum_inventory import PositionInventory
from utils.canonical_ledger import canonical_ledger
from utils.inventory_cost import InventorySnapshot, matched_combined_average
from utils.momentum_risk import (
    entry_window_ok,
    is_order_fully_filled,
    stop_loss_price,
    stop_loss_triggered,
)
from utils.clob_helpers import clamp_buy_price, clamp_sell_price, parse_order_type

console = Console()
load_dotenv()
validate_trading_assets()

# ── Structured trade logger ──────────────────────────────────────────────────
_exec_logger = logging.getLogger("emiliano.execution")
_exec_logger.setLevel(logging.DEBUG)
if not _exec_logger.handlers:
    _fh = logging.FileHandler("emiliano_execution.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _exec_logger.addHandler(_fh)

def exec_log(event: str, **kwargs):
    payload = {"event": event, "ts": t.time(), **kwargs}
    _exec_logger.info(json.dumps(payload))


# ── Configuration ────────────────────────────────────────────────────────────

DRY_MODE = DRY_RUN_DEFAULT
LOG_FILE  = "emiliano_trades.txt"


def _atomic_write_json_file(file_path: str, data: dict) -> None:
    """Write JSON atomically; retry replace on Windows when the target is locked."""
    temp_file = file_path + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    last_err: Optional[Exception] = None
    for attempt in range(5):
        try:
            os.replace(temp_file, file_path)
            return
        except (PermissionError, OSError) as exc:
            last_err = exc
            t.sleep(0.05 * (attempt + 1))
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    with contextlib.suppress(OSError):
        os.remove(temp_file)
    if last_err and not os.path.isfile(file_path):
        raise last_err


def live_on_chain_setup_required() -> bool:
    """True when any enabled worker will place real on-chain orders."""
    if DRY_MODE:
        return False
    return any(wc.enabled and not wc.dry_run for wc in WORKER_CONFIGS)

# Keep the official endpoint as default, but permit an explicit deployment override.
# A hostname/DNS failure is a network configuration problem; changing this value
# is only appropriate when the deployment intentionally uses another supported host.
HOST        = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip().rstrip("/")
WS_URL      = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL   = "https://gamma-api.polymarket.com/markets"
POLYGON_RPC = os.getenv("POLYGON_RPC")

PUSD_ADDRESS      = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_E            = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE      = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
STANDARD_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER  = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]+$")


def _hex_nibbles(value: str) -> str:
    v = value.strip()
    return v[2:] if v.lower().startswith("0x") else v


def _validate_private_key_env(name: str, value: str) -> str:
    v = value.strip()
    body = _hex_nibbles(v)
    if not _HEX_RE.match(v):
        raise ValueError(f"{name} must be a hex private key (0x + 64 hex characters).")
    if len(body) != 64:
        raise ValueError(
            f"{name} must be 0x followed by 64 hex characters (got {len(body)}). "
            f"Did you paste a wallet address instead of a private key?"
        )
    return v if v.lower().startswith("0x") else f"0x{body}"


def _validate_wallet_address_env(name: str, value: str) -> str:
    v = value.strip()
    body = _hex_nibbles(v)
    if not _HEX_RE.match(v):
        raise ValueError(f"{name} must be a hex Ethereum address.")
    if len(body) == 64:
        raise ValueError(
            f"{name} looks like a private key (64 hex chars). "
            "Use your Polymarket wallet public address here (0x + 40 hex chars). "
            "In Polymarket: profile / deposit → copy wallet address, not your key."
        )
    if len(body) != 40:
        raise ValueError(
            f"{name} must be 0x followed by 40 hex characters "
            f"(got {len(body)})."
        )
    prefixed = v if v.lower().startswith("0x") else f"0x{body}"
    return Web3.to_checksum_address(prefixed)


def _resolve_polymarket_signature_type(signer: str, funder: str) -> int:
    """
    Pick CLOB signature_type for order signing.

    New Polymarket accounts use deposit wallets and require POLY_1271 (3).
    Legacy proxy/Safe accounts use 1 or 2 — set POLY_SIGNATURE_TYPE in .env.

    Defaults (POLY_WALLET_MODE=auto or unset):
      0 (EOA)        — funder and signer are the same wallet
      3 (POLY_1271)  — funder != signer (deposit wallet / new accounts)

    Override with POLY_SIGNATURE_TYPE=1 for legacy Polymarket proxy wallets.
    """
    override = os.getenv("POLY_SIGNATURE_TYPE", "").strip()
    if override:
        return int(override)

    wallet_mode = os.getenv("POLY_WALLET_MODE", "auto").strip().lower()
    if signer.lower() == funder.lower():
        return int(SignatureTypeV2.EOA)

    if wallet_mode == "legacy":
        return int(SignatureTypeV2.POLY_PROXY)
    # auto / deposit — required for "use the deposit wallet flow" accounts
    return int(SignatureTypeV2.POLY_1271)


_SIGNATURE_TYPE_LABELS = {
    int(SignatureTypeV2.EOA): "EOA (0)",
    int(SignatureTypeV2.POLY_PROXY): "POLY_PROXY (1)",
    int(SignatureTypeV2.POLY_GNOSIS_SAFE): "POLY_GNOSIS_SAFE (2)",
    int(SignatureTypeV2.POLY_1271): "POLY_1271 (3 — smart contract signer)",
}

BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
RESET  = "\033[0m"

# ── Market interval (5m vs 15m crypto up/down markets) ────────────────────────
# MARKET_INTERVAL : "5m" | "5" | "15m" | "15"   (default "5m")
#   Controls slug format ({asset}-updown-{5m|15m}-{ts}), timestamp rounding,
#   and default listener/entry windows (full market duration unless overridden).
#
# LISTENER_ACTIVATE_SECONDS / ENTRY_SECONDS_LEFT :
#   Default to the full market duration for the selected interval.
#   Set explicitly in .env to override (e.g. ENTRY_SECONDS_LEFT=60).

def _parse_market_interval(raw: str) -> Tuple[int, str]:
    """Return (duration_seconds, slug_label) e.g. (300, '5m') or (900, '15m')."""
    token = (raw or "5m").strip().lower().rstrip("m")
    if token in ("15",):
        return 900, "15m"
    if token in ("5",):
        return 300, "5m"
    raise ValueError(f"unsupported MARKET_INTERVAL={raw!r} — use 5m or 15m")


_raw_market_interval = os.getenv("MARKET_INTERVAL", "5m").strip()
try:
    MARKET_INTERVAL_SECONDS, MARKET_INTERVAL_SLUG = _parse_market_interval(_raw_market_interval)
except ValueError as _interval_err:
    print(f"⚠️  {_interval_err} — falling back to 5m.")
    MARKET_INTERVAL_SECONDS, MARKET_INTERVAL_SLUG = 300, "5m"

LISTENER_ACTIVATE_SECONDS = int(
    os.getenv("LISTENER_ACTIVATE_SECONDS", str(MARKET_INTERVAL_SECONDS))
)
ENTRY_SECONDS_LEFT = int(
    os.getenv("ENTRY_SECONDS_LEFT", str(MARKET_INTERVAL_SECONDS))
)

print(f"📊 MARKET_INTERVAL={MARKET_INTERVAL_SLUG} ({MARKET_INTERVAL_SECONDS}s) | "
      f"listener={LISTENER_ACTIVATE_SECONDS}s | entry_window={ENTRY_SECONDS_LEFT}s")


def _interval_start(ts: int) -> int:
    """Snap a unix timestamp to the start of its market window."""
    return (ts // MARKET_INTERVAL_SECONDS) * MARKET_INTERVAL_SECONDS


def market_slug(asset: str, start_ts: int) -> str:
    """Polymarket slug for a crypto up/down market, e.g. btc-updown-5m-1710000000."""
    return f"{asset.lower()}-updown-{MARKET_INTERVAL_SLUG}-{start_ts}"


def current_interval_starts(now_ts: Optional[int] = None) -> Tuple[int, int]:
    """Return (current_window_start, next_window_start) unix timestamps."""
    now = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    base = _interval_start(now)
    return base, base + MARKET_INTERVAL_SECONDS


# Locked-price sentinels. Entry is skipped when the price equals either value.
LOCKED_LOW  = 0.01  # 1c  — market resolved or buy-side liquidity exhausted
LOCKED_HIGH = 1.00  # 100c — market fully resolved

FINAL_PRICE   = float(os.getenv("FINAL_PRICE", "0.70"))

BINANCE_PRIME_THRESHOLD   = float(os.getenv("BINANCE_PRIME_THRESHOLD",   "0.20"))
BINANCE_STALE_CUTOFF_SECS = float(os.getenv("BINANCE_STALE_CUTOFF_SECS", "5.0"))
BINANCE_DEPTH_LIMIT       = int(os.getenv("BINANCE_DEPTH_LIMIT",          "20"))

MIN_FILL_DELTA   = float(os.getenv("MIN_FILL_DELTA",   "0.05"))


# ═════════════════════════════════════════════════════════════════════════════
# TRADING SCHEDULE  — weekday-only entry gate
#
# Philosophy
# ──────────
# Only NEW trade entries are blocked on weekends. Everything else continues
# running normally: existing positions are monitored and exited via TP/SL,
# the portfolio chart keeps updating, Redis persistence keeps writing, wallet
# audits and PnL merges keep running, and the WebSocket price listener stays
# connected so the bot can react immediately when Monday arrives.
#
# Design
# ──────
# A single module-level function `is_trading_allowed()` is the ONLY place
# the weekday check lives. It is called exactly once per check_logic() tick,
# right before the IDLE entry branch. No other code path is touched because:
#   - execute_order() handles both BUY and SELL → cannot be gated
#   - confirmed_execute() handles both BUY and SELL → cannot be gated
#   - start() handles market scanning, not entry decisions → not touched
#   - price_listener() handles WS plumbing and expiry → not touched
#   - TP/SL exit path (_check_single_side_exit → market_exit) → not touched
#
# Timezone
# ────────
# All datetime arithmetic in this codebase uses UTC (datetime.now(timezone.utc)
# throughout). The ZoneInfo("America/New_York") reference in the terminal
# dashboard is display-only and does not affect trading decisions.
#
# The trading-schedule timezone is separately configurable via the
# TRADING_TIMEZONE environment variable (default "UTC"). This makes weekday
# calculation deterministic and independent of the Render server's locale.
#
# Configuration (all optional — sensible defaults work without any .env change)
# ───────────────────────────────────────────────────────────────────────────────
# TRADING_TIMEZONE   : IANA timezone string for weekday calculation.
#                      Default: "UTC"
#                      Example: TRADING_TIMEZONE=America/New_York
#
# TRADING_DAYS       : Comma-separated list of 0-indexed weekday numbers
#                      (Monday=0 … Sunday=6) that are allowed for new entries.
#                      Default: "0,1,2,3,4"  (Monday through Friday)
#                      Example: TRADING_DAYS=0,1,2,3,4,5  (adds Saturday)
#
# WEEKEND_TRADING    : Set to "true" to bypass the schedule entirely.
#                      Useful for testing, or if you decide to trade weekends.
#                      Default: "false"
# ═════════════════════════════════════════════════════════════════════════════

# Parse TRADING_TIMEZONE (validated once at import time).
def resolve_timezone(name: str):
    """
    Return tzinfo for an IANA timezone name.

    UTC/GMT use datetime.timezone.utc so the app starts on Windows even when
    the optional tzdata package is not installed. Other zones need tzdata
    (listed in requirements.txt).
    """
    key = (name or "UTC").strip()
    if key.upper() in ("UTC", "GMT", "ETC/UTC", "ETC/GMT", "Z"):
        return timezone.utc
    try:
        return ZoneInfo(key)
    except Exception:
        print(
            f"⚠️  [schedule] Unknown or unavailable TRADING_TIMEZONE={key!r} "
            f"(install tzdata on Windows) — falling back to UTC."
        )
        return timezone.utc


_TRADING_TZ_NAME: str = os.getenv("TRADING_TIMEZONE", "UTC").strip()
TRADING_TZ = resolve_timezone(_TRADING_TZ_NAME)
if TRADING_TZ is timezone.utc and _TRADING_TZ_NAME.upper() not in (
    "UTC", "GMT", "ETC/UTC", "ETC/GMT", "Z",
):
    _TRADING_TZ_NAME = "UTC"

# Display-only ET zone for terminal dashboard labels (optional tzdata).
try:
    ET_ZONE = ZoneInfo("America/New_York")
except Exception:
    ET_ZONE = timezone.utc

# Parse TRADING_DAYS. Validated once at import time. Stored as a frozenset
# for O(1) membership tests on every price tick.
_raw_trading_days: str = os.getenv("TRADING_DAYS", "0,1,2,3,4").strip()
try:
    TRADING_DAYS: frozenset = frozenset(
        int(d.strip()) for d in _raw_trading_days.split(",") if d.strip().isdigit()
    )
    if not TRADING_DAYS:
        raise ValueError("empty set")
except Exception:
    print(f"⚠️  [schedule] Invalid TRADING_DAYS={_raw_trading_days!r} — defaulting to Mon-Fri.")
    TRADING_DAYS = frozenset({0, 1, 2, 3, 4})

# Kill-switch: set WEEKEND_TRADING=true to bypass the schedule entirely.
WEEKEND_TRADING_ENABLED: bool = os.getenv("WEEKEND_TRADING", "false").lower() == "true"

# Human-readable day names used in log messages.
_DAY_NAMES: Dict[int, str] = {
    0: "Monday", 1: "Tuesday",  2: "Wednesday",
    3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday",
}

# Throttle the "entry blocked" log message to at most once per minute so the
# log file / stdout is not flooded on every WebSocket tick (which can fire
# hundreds of times per minute).
_last_weekend_log_ts: float = 0.0
_WEEKEND_LOG_INTERVAL_SEC: float = 60.0


def is_trading_allowed() -> bool:
    """
    Return True when new trade entries are permitted, False when they must be
    blocked (weekend / non-trading day).

    THIS IS THE SINGLE, CENTRALIZED TRADING-HOURS CHECK FOR THE ENTIRE BOT.
    No other function, class, or module contains weekday logic. Every code
    path that needs to know "can we open a new position right now?" calls
    this function — there are zero inline weekday checks anywhere else.

    Behavior
    ────────
    • Returns True on Mon–Fri (weekday numbers 0–4 by default, or whatever
      is configured in TRADING_DAYS).
    • Returns False on Sat–Sun unless WEEKEND_TRADING=true is set.
    • The weekday is evaluated in TRADING_TZ (default UTC), not the server's
      local timezone, so the result is deterministic regardless of where
      Render hosts the bot.
    • When WEEKEND_TRADING=true the function always returns True (full bypass).

    Does NOT affect
    ───────────────
    • TP/SL monitoring on existing positions (those always run).
    • SELL / exit order execution (those always run).
    • Portfolio chart updates (those always run).
    • Redis persistence, PnL merges, wallet audits (those always run).
    • The WebSocket price listener (stays connected through weekends).
    """
    if WEEKEND_TRADING_ENABLED:
        return True

    now_in_tz = datetime.now(TRADING_TZ)
    weekday   = now_in_tz.weekday()   # 0=Monday … 6=Sunday
    return weekday in TRADING_DAYS


def log_weekend_block(asset_type: str, side: str, price_cents: int) -> None:
    """
    Emit a throttled, audit-friendly log entry when a new-position entry is
    blocked because trading is not currently permitted (weekend / non-trading
    day).

    Throttled to at most one message per _WEEKEND_LOG_INTERVAL_SEC across
    the entire process (not per-worker) so the logs stay readable during the
    ~48 hours of a full weekend.

    Parameters
    ──────────
    asset_type  : e.g. "btc", "eth" — the worker's asset identifier.
    side        : "YES" or "NO" — the side that would have been entered.
    price_cents : integer cents price that triggered the 90c threshold
                  (e.g. 92 meaning the market was at 92c).
    """
    global _last_weekend_log_ts
    now = t.time()
    if now - _last_weekend_log_ts < _WEEKEND_LOG_INTERVAL_SEC:
        return   # throttled — already logged within the last minute
    _last_weekend_log_ts = now

    now_in_tz   = datetime.now(TRADING_TZ)
    weekday     = now_in_tz.weekday()
    day_name    = _DAY_NAMES.get(weekday, f"day-{weekday}")
    ts_str      = now_in_tz.strftime("%Y-%m-%d %H:%M:%S")
    tz_label    = _TRADING_TZ_NAME
    allowed_names = ", ".join(
        _DAY_NAMES[d] for d in sorted(TRADING_DAYS) if d in _DAY_NAMES
    )

    msg = (
        f"🚫 [SCHEDULE] [{asset_type.upper()}] New-position entry BLOCKED — "
        f"it is {day_name} ({ts_str} {tz_label}). "
        f"Trading permitted only on: {allowed_names}. "
        f"Would have bought {side} @ {price_cents}c. "
        f"Existing TP/SL monitoring and portfolio tracking continue normally."
    )
    print(msg)
    exec_log(
        "entry_blocked_schedule",
        asset=asset_type,
        side=side,
        price_cents=price_cents,
        day=day_name,
        timestamp_tz=ts_str,
        timezone=tz_label,
        allowed_trading_days=sorted(TRADING_DAYS),
    )


# ═════════════════════════════════════════════════════════════════════════════
# TRADE STATE MACHINE
# ═════════════════════════════════════════════════════════════════════════════

class TradeState(Enum):
    # No position open. Bot monitors YES and NO for a qualifying ≥90c entry.
    IDLE    = auto()
    # Single directional position is held. Entry logic is fully suppressed.
    # Only TP/SL monitoring runs from this state.
    FILLED  = auto()
    # TP or SL triggered. Sell order is in flight.
    EXITING = auto()
    # Trade cycle complete. reset_state() returns to IDLE for the next market.
    CLOSED  = auto()
    # Unexpected failure. Logged; no further orders until reset_state().
    ERROR   = auto()


class OrderState(Enum):
    IDLE    = auto()
    PENDING = auto()


# ─────────────────────────────────────────────────────────────────────────────
# LOCKED-PRICE GUARD
# ─────────────────────────────────────────────────────────────────────────────

def normalize_polymarket_price(price: float) -> float:
    """CLOB feeds may use 0–1 dollars or 0–100 cents."""
    if price <= 0:
        return 0.0
    if price > 1.0:
        return price / 100.0
    return price


def is_locked_price(price: float) -> bool:
    # Round to 4 d.p. to absorb floating-point noise from the WebSocket feed.
    rounded = round(normalize_polymarket_price(price), 4)
    return rounded <= LOCKED_LOW or rounded >= LOCKED_HIGH


# ═════════════════════════════════════════════════════════════════════════════
# REDIS PERSISTENCE LAYER
# ═════════════════════════════════════════════════════════════════════════════

UPSTASH_URL   = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
_redis_available = bool(UPSTASH_URL and UPSTASH_TOKEN)

def _redis_headers() -> dict:
    return {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

def redis_get(key: str) -> Optional[str]:
    if not _redis_available:
        return None
    try:
        resp = requests.get(f"{UPSTASH_URL}/get/{key}", headers=_redis_headers(), timeout=4)
        return resp.json().get("result")
    except Exception as e:
        print(f"⚠️ Redis GET error ({key}): {e}")
        return None

def redis_set(key: str, value: str) -> bool:
    if not _redis_available:
        return False
    try:
        resp = requests.get(
            f"{UPSTASH_URL}/set/{key}/{requests.utils.quote(value, safe='')}",  # type: ignore
            headers=_redis_headers(), timeout=4,
        )
        return resp.json().get("result") == "OK"
    except Exception as e:
        print(f"⚠️ Redis SET error ({key}): {e}")
        return False

def redis_set_json(key: str, obj: Any) -> bool:
    return redis_set(key, json.dumps(obj))

def redis_get_json(key: str) -> Optional[Any]:
    raw = redis_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# PORTFOLIO HISTORY — persistent equity-curve storage
#
# Redis key  : emiliano:portfolio:history
# Value      : JSON list of {"t": unix_ms, "v": portfolio_pnl_dollars}
#              sorted oldest-first, capped at MAX_HISTORY_POINTS entries.
#
# Backfill   : On startup (called from main.py's startup_event) we inspect
#              all emiliano:{asset}:trades keys already in Redis, reconstruct
#              the full cross-asset portfolio equity curve from those trade
#              records, and persist it to emiliano:portfolio:history.
#              This means the chart shows real history from the very first
#              trade — not just from the day this feature was deployed.
#
# Idempotent : The backfill compares the number of trade records in Redis
#              against the existing history length. If history already covers
#              every trade it does nothing. If new trades exist beyond what is
#              already stored it merges only the missing tail. Safe to rerun.
#
# Live feed  : log_pnl() calls portfolio_history_snapshot() immediately after
#              every completed trade so the curve updates in real time without
#              waiting for any background flush interval.
# ═════════════════════════════════════════════════════════════════════════════

PORTFOLIO_HISTORY_KEY = "emiliano:portfolio:history"
MAX_HISTORY_POINTS    = 60_000          # ~1 year at 1 pt per 10 min

# Tracked assets — loaded once from config.py (ASSET / TRADING_ASSETS env vars).

# ── Flat-period snapshot throttling ───────────────────────────────────────
# ROOT CAUSE OF "CHART KEEPS GROWING WITH IDENTICAL VALUES":
# portfolio_history_snapshot() used to unconditionally append a brand new
# {t, v} point every time it was called — once per completed trade (every
# market round) AND once every 60 s from main.py's idle-period heartbeat
# loop — even when the value had not changed at all since the previous
# point. Over weeks of mostly-flat PnL this writes thousands of points that
# are visually and informationally identical, bloating Redis storage and the
# /api/history payload without adding any real information.
#
# Fix: a new point is only persisted when the value changed by more than
# PORTFOLIO_FLAT_EPSILON. Optional PORTFOLIO_HEARTBEAT_SEC (>0) may append
# same-value points after that interval (default 0 = disabled). The chart
# extends flat segments to "now" at render time — no heartbeat spam in Redis.
PORTFOLIO_FLAT_EPSILON  = float(os.getenv("PORTFOLIO_FLAT_EPSILON",  "0.005"))   # half a cent
PORTFOLIO_HEARTBEAT_SEC = int(os.getenv("PORTFOLIO_HEARTBEAT_SEC", "0"))          # 0 = off

CHART_PERIOD_MS: Dict[str, int] = {
    "1D":  24 * 3600 * 1000,
    "1W":  7  * 24 * 3600 * 1000,
    "1M":  30 * 24 * 3600 * 1000,
    "1Y":  365 * 24 * 3600 * 1000,
}

# ── Process-wide worker registry ──────────────────────────────────────────
# ROOT CAUSE OF CHART SPIKES (see notes above portfolio_history_snapshot):
# log_pnl() used to push *one asset's* cumulative_pnl into the shared,
# cross-asset portfolio history key. That single-asset value would land
# right next to genuinely correct cross-asset totals (written every 60 s by
# main.py's portfolio_snapshot_loop, and by the startup backfill), producing
# a sharp up/down spike on every single trade close.
#
# Fix: every MarketWorker registers itself here at construction time. Any
# code that needs "the total portfolio PnL right now" sums cumulative_pnl
# across every registered worker instead of using one worker's own value.
_worker_registry: List["MarketWorker"] = []
_worker_registry_lock = threading.Lock()


def _register_worker(worker: "MarketWorker") -> None:
    with _worker_registry_lock:
        if worker not in _worker_registry:
            _worker_registry.append(worker)


def _portfolio_total_pnl() -> float:
    """Sum cumulative_pnl across every live MarketWorker in this process.

    This is the single source of truth for 'total portfolio PnL right now' —
    every write into emiliano:portfolio:history must go through this (or the
    equivalent get_global_stats()-based total in main.py) rather than any
    one worker's own self.cumulative_pnl.
    """
    with _worker_registry_lock:
        workers = list(_worker_registry)
    total = 0.0
    for w in workers:
        try:
            v = getattr(w, "cumulative_pnl", 0.0)
            if _is_finite_number(v):
                total += v
        except Exception:
            continue
    return round(total, 4)


# ── Value / timestamp validation ──────────────────────────────────────────

def _is_finite_number(v: Any) -> bool:
    """True only for real, finite int/float values. Rejects None, NaN, Inf,
    bool, strings, etc. Used to keep corrupted values out of persisted
    history and to reject them again at read time as a second line of
    defense."""
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v))
    except Exception:
        return False


def sanitize_portfolio_history(
    points: List[Dict],
    *,
    drop_isolated_spikes: bool = True,
    collapse_flat_runs: bool = False,
    flat_epsilon: float = PORTFOLIO_FLAT_EPSILON,
    min_flat_run: int = 2,
) -> List[Dict]:
    """
    Single source of truth for cleaning a portfolio-history point list before
    it is either persisted to Redis or served to the chart.

    Guarantees on the returned list:
      • Every point has a finite numeric 't' (int, unix ms) and 'v' (float).
      • No two points share the same 't' (last-write-wins on duplicates).
      • Strictly increasing 't' (i.e. chronological order is enforced, not
        just sorted — true duplicates are already gone by the time we sort).
      • Optionally drops "isolated spikes": a single point whose value jumps
        far away from both neighbors and then jumps right back, which is the
        exact signature of a one-off corrupted write landing between two
        otherwise-correct totals.
      • Optionally collapses "flat runs": two or more consecutive points
        whose values are all within flat_epsilon of each other are reduced
        to just their first and last point. Two endpoints fully describe a
        flat horizontal segment, so this is lossless for charting
        while sharply bounding storage/payload growth during long periods
        with no PnL change.

    This function is intentionally conservative — genuine, large, sustained
    portfolio swings are never removed or altered, only redundant duplicate
    points and one-off corrupted spikes.
    """
    cleaned: Dict[int, float] = {}
    for p in points:
        if not isinstance(p, dict):
            continue
        t_raw = p.get("t")
        v_raw = p.get("v")
        if t_raw is None or v_raw is None:
            continue
        try:
            t_ms = int(t_raw)
        except (TypeError, ValueError):
            continue
        if not _is_finite_number(v_raw):
            continue
        if t_ms <= 0:
            continue
        v = round(float(v_raw), 4)
        # Last write wins for exact-duplicate timestamps (e.g. a retried
        # write, or a 60-s loop tick that lands on the same millisecond as a
        # trade-close write).
        cleaned[t_ms] = v

    ordered = [{"t": ts, "v": v} for ts, v in sorted(cleaned.items())]

    if drop_isolated_spikes and len(ordered) >= 3:
        ordered = _drop_isolated_spikes(ordered)

    if collapse_flat_runs and len(ordered) >= min_flat_run:
        ordered = compress_flat_runs(ordered, epsilon=flat_epsilon, min_run=min_flat_run)

    return ordered


def compress_flat_runs(points: List[Dict], epsilon: float = PORTFOLIO_FLAT_EPSILON,
                        min_run: int = 2) -> List[Dict]:
    """
    Collapse runs of 2-or-more consecutive points whose values are all within
    `epsilon` of the run's first value down to just the first and last point
    of that run.

    Why this is safe / lossless for rendering: a straight horizontal line
    segment is fully described by its two endpoints. Any interior points
    with (effectively) the same value add nothing visually — the SVG line
    drawn through 50 identical points looks pixel-identical to the line
    drawn through just the first and last of them. Short runs (1-2 points)
    are left untouched since there's nothing to compress.

    This is applied both when persisting to Redis (keeps stored history
    compact as it grows) and when serving /api/history (keeps the JSON
    payload small even if older, pre-compaction data is still in Redis).
    """
    n = len(points)
    if n < min_run:
        return list(points)

    out: List[Dict] = []
    i = 0
    while i < n:
        j = i
        base_v = points[i]["v"]
        while j + 1 < n and abs(points[j + 1]["v"] - base_v) <= epsilon:
            j += 1
        run_len = j - i + 1
        if run_len >= min_run:
            out.append(points[i])
            out.append(points[j])
        else:
            out.extend(points[i:j + 1])
        i = j + 1
    return out


def _drop_isolated_spikes(points: List[Dict]) -> List[Dict]:
    """
    Remove single-point spikes: a point whose value jumps far from BOTH
    neighbors, where the neighbors themselves are close to each other (i.e.
    the series jumps away and immediately jumps back). This is the exact
    shape produced by a stray bad write landing between two correct points,
    and it is conservative enough to leave real, sustained PnL moves intact.
    """
    if len(points) < 3:
        return points

    values = [p["v"] for p in points]
    diffs  = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    diffs_sorted = sorted(diffs)
    mid = len(diffs_sorted) // 2
    median_step = diffs_sorted[mid] if diffs_sorted else 0.0

    # Floor so that a perfectly flat or near-flat series doesn't make the
    # spike threshold collapse to (near) zero.
    floor = max(5.0, median_step * 4)

    keep = [True] * len(points)
    for i in range(1, len(points) - 1):
        prev_v, cur_v, next_v = values[i - 1], values[i], values[i + 1]
        jump_in  = abs(cur_v - prev_v)
        jump_out = abs(next_v - cur_v)
        settle   = abs(next_v - prev_v)
        if jump_in < floor or jump_out < floor:
            continue
        # Both surrounding jumps are large, but the series basically returns
        # to where it started → this point is an isolated spike.
        if settle <= max(jump_in, jump_out) * 0.35:
            keep[i] = False

    return [p for p, k in zip(points, keep) if k]

# Timestamp format written by log_pnl() via datetime.now(TRADING_TZ).strftime(...)
_TS_PRIMARY = "%Y-%m-%d %H:%M:%S"

# Additional formats found in older records or alternative paths.
_TS_FORMATS: List[str] = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S%z",
]


def _parse_ts(ts_str: str) -> Optional[int]:
    """Parse a trade timestamp string → Unix milliseconds. Returns None on failure."""
    if not ts_str:
        return None
    for fmt in _TS_FORMATS:
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                # Naive strings from log_pnl() are written in TRADING_TZ.
                dt = dt.replace(tzinfo=TRADING_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TRADING_TZ)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


# Max closed-trade records kept per worker in Redis. Local JSON files are uncapped.
# ~200 B/trade → 10 000 trades ≈ 2 MB/key — fine for Redis; raise via env if needed.
REDIS_TRADES_MAX: int = max(int(os.getenv("REDIS_TRADES_MAX", "10000")), 500)


def _trade_dedup_key(trade: Dict) -> Tuple[str, str, float]:
    """Identity for merge/dedup: (timestamp, slug, pnl)."""
    ts = str(trade.get("timestamp") or "")
    slug = str(trade.get("slug") or "")
    pnl_raw = trade.get("pnl")
    pnl = round(float(pnl_raw), 4) if _is_finite_number(pnl_raw) else 0.0
    return (ts, slug, pnl)


def _merge_trade_lists(*sources: List[Dict]) -> List[Dict]:
    """Merge trade lists from multiple stores, dedupe, sort oldest-first."""
    merged: Dict[Tuple[str, str, float], Dict] = {}
    for trades in sources:
        if not trades:
            continue
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            merged[_trade_dedup_key(trade)] = trade

    def _sort_key(tr: Dict) -> int:
        ts_ms = _parse_ts(tr.get("timestamp", ""))
        return ts_ms if ts_ms is not None else 0

    return sorted(merged.values(), key=_sort_key)


def _load_local_trades_for_asset(asset: str, window: str = "5m") -> List[Dict]:
    """Uncapped trade history from local PnL JSON files."""
    out: List[Dict] = []
    fp = asset_pnl_filename(asset, window)
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            trades = data.get("trades", [])
            if isinstance(trades, list):
                out.extend(trades)
    except Exception as e:
        print(f"⚠️ Could not read {fp}: {e}")

    legacy_fp = f"{asset.lower()}_pnl_history.json"
    try:
        if os.path.exists(legacy_fp):
            with open(legacy_fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            trades = data.get("trades", [])
            if isinstance(trades, list):
                out.extend(trades)
    except Exception as e:
        print(f"⚠️ Could not read {legacy_fp}: {e}")
    return out


def _load_redis_trades_for_asset(asset: str, window: str = "5m") -> List[Dict]:
    """Trade history from Redis (may be shorter than local if previously truncated)."""
    if not _redis_available:
        return []
    redis_key = f"emiliano:{asset.lower()}:{window.lower()}:trades"
    trades = redis_get_json(redis_key)
    if trades and isinstance(trades, list):
        return trades
    legacy = redis_get_json(f"emiliano:{asset.lower()}:trades")
    if legacy and isinstance(legacy, list):
        return legacy
    return []


def _load_worker_stats(asset: str, window: str = "5m") -> Dict[str, float]:
    """Aggregate PnL counters for one worker (Redis stats key or local file header)."""
    asset_l = asset.lower()
    window_l = window.lower()
    redis_key = f"emiliano:{asset_l}:{window_l}:stats"
    if _redis_available:
        data = redis_get_json(redis_key)
        if not data:
            data = redis_get_json(f"emiliano:{asset_l}:stats")
        if data:
            wins = int(data.get("wins", 0))
            losses = int(data.get("losses", 0))
            return {
                "total_pnl": float(data.get("total_pnl", 0.0)),
                "wins": wins,
                "losses": losses,
                "trades": wins + losses,
            }

    for fp in (asset_pnl_filename(asset_l, window_l), f"{asset_l}_pnl_history.json"):
        try:
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                wins = int(data.get("wins", 0))
                losses = int(data.get("losses", 0))
                return {
                    "total_pnl": float(data.get("total_pnl", 0.0)),
                    "wins": wins,
                    "losses": losses,
                    "trades": wins + losses,
                }
        except Exception:
            continue
    return {"total_pnl": 0.0, "wins": 0, "losses": 0, "trades": 0}


def _load_trades_for_asset(asset: str, window: str = "5m") -> List[Dict]:
    """
    Return the fullest trade list for one worker.

    Merges uncapped local PnL files with Redis and dedupes by
    (timestamp, slug, pnl). Local files win on duplicates because they
    are merged last.
    """
    redis_trades = _load_redis_trades_for_asset(asset, window)
    local_trades = _load_local_trades_for_asset(asset, window)
    merged = _merge_trade_lists(redis_trades, local_trades)

    redis_pnl = sum(
        float(t.get("pnl", 0))
        for t in redis_trades
        if _is_finite_number(t.get("pnl"))
    )
    local_pnl = sum(
        float(t.get("pnl", 0))
        for t in local_trades
        if _is_finite_number(t.get("pnl"))
    )
    merged_pnl = sum(
        float(t.get("pnl", 0))
        for t in merged
        if _is_finite_number(t.get("pnl"))
    )
    stats = _load_worker_stats(asset, window)
    print(
        f"📊 [{asset.upper()} {window}] trades load — "
        f"redis: {len(redis_trades)} (${redis_pnl:.2f}) | "
        f"local: {len(local_trades)} (${local_pnl:.2f}) | "
        f"merged: {len(merged)} (${merged_pnl:.2f}) | "
        f"stats: {stats['trades']} (${stats['total_pnl']:.2f})"
    )
    if stats["trades"] > len(merged):
        print(
            f"⚠️ [{asset.upper()} {window}] trade list shorter than stats counter "
            f"({len(merged)} records vs {stats['trades']} logged trades) — "
            f"calendar totals may undercount until local history is restored."
        )
    return merged


def _iter_realized_trades(
    tz_name: Optional[str] = None,
) -> List[Tuple[Dict, float, int, datetime]]:
    """All closed trades across workers: (trade, pnl, ts_ms, dt_in_tz)."""
    tz = resolve_timezone(tz_name or _TRADING_TZ_NAME)
    rows: List[Tuple[Dict, float, int, datetime]] = []
    for wc in WORKER_CONFIGS:
        for trade in _load_trades_for_asset(wc.asset, wc.window):
            pnl_raw = trade.get("pnl")
            if pnl_raw is None or not _is_finite_number(pnl_raw):
                continue
            pnl = round(float(pnl_raw), 4)
            ts_ms = _parse_ts(trade.get("timestamp", ""))
            if ts_ms is None:
                continue
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(tz)
            rows.append((trade, pnl, ts_ms, dt))
    return rows


def compute_biggest_realized_win() -> float:
    """Largest single-trade realized profit across all workers (Redis/local PnL records)."""
    best = 0.0
    for wc in WORKER_CONFIGS:
        for trade in _load_trades_for_asset(wc.asset, wc.window):
            pnl = trade.get("pnl")
            if pnl is None or not _is_finite_number(pnl):
                continue
            v = float(pnl)
            if v > best:
                best = v
    return best


def compute_daily_pnl_calendar(
    year: int,
    month: int,
    *,
    tz_name: Optional[str] = None,
    week_start: int = 0,
    scale_max: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Bucket realized PnL by calendar day for one month.

    Reads closed-trade records via _load_trades_for_asset (Redis/local PnL
    history). Read-only — does not mutate Redis, files, or worker state.
    """
    if month < 1 or month > 12:
        raise ValueError("month must be 1–12")
    if week_start not in (0, 1):
        raise ValueError("week_start must be 0 (Sunday) or 1 (Monday)")

    effective_tz_name = tz_name or _TRADING_TZ_NAME

    if scale_max is None or scale_max <= 0:
        scale_max = float(os.getenv("CALENDAR_PNL_SCALE_MAX", "100"))
    scale_max = max(float(scale_max), 0.01)

    buckets: Dict[str, Dict[str, Any]] = {}

    def _bucket(day_key: str) -> Dict[str, Any]:
        if day_key not in buckets:
            buckets[day_key] = {
                "pnl": 0.0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "best_trade": None,
                "worst_trade": None,
            }
        return buckets[day_key]

    for _trade, pnl, ts_ms, dt in _iter_realized_trades(effective_tz_name):
        if dt.year != year or dt.month != month:
            continue

        day_key = dt.strftime("%Y-%m-%d")
        b = _bucket(day_key)
        b["pnl"] = round(b["pnl"] + pnl, 4)
        b["trades"] += 1
        if pnl > 0:
            b["wins"] += 1
        elif pnl < 0:
            b["losses"] += 1

        if b["best_trade"] is None or pnl > b["best_trade"]:
            b["best_trade"] = pnl
        if b["worst_trade"] is None or pnl < b["worst_trade"]:
            b["worst_trade"] = pnl

    total_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    for b in buckets.values():
        total_pnl = round(total_pnl + b["pnl"], 4)
        total_trades += b["trades"]
        total_wins += b["wins"]
        total_losses += b["losses"]

    win_rate = round((total_wins / total_trades) * 100, 1) if total_trades > 0 else 0.0

    stats_trades = 0
    stats_pnl = 0.0
    for wc in WORKER_CONFIGS:
        ws = _load_worker_stats(wc.asset, wc.window)
        stats_trades += ws["trades"]
        stats_pnl = round(stats_pnl + ws["total_pnl"], 4)

    # Log a sample Jul 25+ bucket when present (helps verify timezone bucketing).
    for day_key in sorted(buckets.keys()):
        if day_key >= "2026-07-25":
            b = buckets[day_key]
            print(
                f"📅 [calendar] day {day_key}: "
                f"{b['trades']} trades, pnl=${b['pnl']:.2f} (tz={effective_tz_name})"
            )

    if stats_trades > 0 and (
        total_trades < stats_trades
        or abs(total_pnl - stats_pnl) > max(0.05, abs(stats_pnl) * 0.01)
    ):
        print(
            f"⚠️ [calendar] {year}-{month:02d} month_summary "
            f"({total_trades} trades, ${total_pnl:.2f}) "
            f"≠ portfolio stats ({stats_trades} trades, ${stats_pnl:.2f}) — "
            f"trade records are incomplete or timestamps were mis-bucketed."
        )
    else:
        print(
            f"✅ [calendar] {year}-{month:02d} month_summary "
            f"{total_trades} trades, ${total_pnl:.2f} "
            f"(portfolio stats: {stats_trades} trades, ${stats_pnl:.2f})"
        )

    return {
        "year": year,
        "month": month,
        "timezone": effective_tz_name,
        "week_start": week_start,
        "scale_max": round(scale_max, 2),
        "month_summary": {
            "total_pnl": round(total_pnl, 2),
            "trades": total_trades,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": win_rate,
        },
        "days": buckets,
    }


def _build_equity_curve(all_trades_by_asset: Dict[str, List[Dict]]) -> List[Dict]:
    """
    Given {asset: [trade, ...]}, build a time-sorted portfolio equity curve.

    Algorithm:
      1. Tag every trade with its asset and parse its timestamp.
      2. Sort all events globally by timestamp.
      3. Walk the events, maintaining each asset's last known cumulative PnL.
      4. At each event emit {t: unix_ms, v: sum_of_all_asset_pnls}.

    The result is a list of {"t": unix_ms, "v": float} dicts, oldest-first.
    Duplicate timestamps are de-duplicated (last value wins).
    """
    events: List[Tuple[int, str, float]] = []  # (unix_ms, asset, cumulative_pnl)

    for asset, trades in all_trades_by_asset.items():
        for trade in trades:
            cum = trade.get("cumulative_pnl")
            if cum is None or not _is_finite_number(cum):
                continue
            ts_ms = _parse_ts(trade.get("timestamp", ""))
            if ts_ms is None:
                continue
            events.append((ts_ms, asset, float(cum)))

    if not events:
        return []

    events.sort(key=lambda e: e[0])

    asset_pnl: Dict[str, float] = {wc.key: 0.0 for wc in WORKER_CONFIGS}
    seen_ts: Dict[int, float]   = {}

    for ts_ms, asset, cum_pnl in events:
        asset_pnl[asset] = cum_pnl
        total = round(sum(asset_pnl.values()), 4)
        seen_ts[ts_ms] = total          # last-write-wins for duplicate timestamps

    curve = [{"t": ts, "v": v} for ts, v in sorted(seen_ts.items())]
    curve = sanitize_portfolio_history(curve, drop_isolated_spikes=False)
    return curve


def portfolio_history_backfill() -> int:
    """
    Idempotent backfill: reads all existing emiliano:{asset}:trades data from
    Redis and reconstructs emiliano:portfolio:history.

    Returns the number of backfill points written (0 = nothing to do / no data).

    Idempotency guarantee
    ─────────────────────
    • Count total trade records across all assets in Redis (N_trades).
    • Read existing portfolio history length (N_hist).
    • If N_hist >= N_trades → history already covers every trade → skip.
    • Otherwise rebuild the full curve and MERGE:
        – Points whose timestamps already exist in history are NOT overwritten
          so any manually-entered or live-snapshot points are preserved.
        – New trade-derived points are inserted into the correct chronological
          position.
    """
    if not _redis_available:
        print("ℹ️  [backfill] Redis not available — skipping portfolio history backfill.")
        return 0

    print("🔄 [backfill] Inspecting existing Redis trade data...")

    # Step 1: load all trade records
    all_trades: Dict[str, List[Dict]] = {}
    total_trade_count = 0
    for wc in WORKER_CONFIGS:
        trades = _load_trades_for_asset(wc.asset, wc.window)
        all_trades[wc.key] = trades
        print(f"  [{wc.asset.upper()} {wc.window}] {len(trades)} trade records found")
        total_trade_count += len(trades)

    if total_trade_count == 0:
        print("ℹ️  [backfill] No trade records found in Redis — nothing to backfill.")
        return 0

    # Step 2: check how many history points already exist
    existing_history: List[Dict] = redis_get_json(PORTFOLIO_HISTORY_KEY) or []
    existing_count = len(existing_history)

    if existing_count >= total_trade_count:
        print(f"✅ [backfill] Portfolio history already has {existing_count} points "
              f"covering {total_trade_count} trades — skipping rebuild.")
        return 0

    print(f"📊 [backfill] History has {existing_count} pts, trades have {total_trade_count} "
          f"records — rebuilding equity curve...")

    # Step 3: build the full curve from trade records
    backfill_curve = _build_equity_curve(all_trades)
    if not backfill_curve:
        print("⚠️  [backfill] Could not build equity curve (no parseable timestamps).")
        return 0

    # Step 4: merge with existing (already-sanitized) history
    # Existing points that post-date the last backfill point (live snapshots
    # recorded since previous deploy) are preserved; backfill replaces older pts.
    existing_history = sanitize_portfolio_history(
        existing_history, drop_isolated_spikes=False, collapse_flat_runs=True,
    )
    last_backfill_t = backfill_curve[-1]["t"] if backfill_curve else 0
    live_tail = [p for p in existing_history if p["t"] > last_backfill_t]

    merged: Dict[int, float] = {}
    for p in backfill_curve:
        merged[p["t"]] = p["v"]
    for p in live_tail:
        merged[p["t"]] = p["v"]    # live points win on any overlap

    final_curve = sanitize_portfolio_history(
        [{"t": ts, "v": v} for ts, v in sorted(merged.items())],
        drop_isolated_spikes=False,
        collapse_flat_runs=True,
    )

    # Cap to max points
    if len(final_curve) > MAX_HISTORY_POINTS:
        final_curve = final_curve[-MAX_HISTORY_POINTS:]

    # Step 5: write back (best-effort backup of the pre-backfill state so a
    # bad rebuild can always be rolled back manually)
    if existing_history:
        redis_set_json(f"{PORTFOLIO_HISTORY_KEY}:backup:pre_backfill", existing_history)

    ok = redis_set_json(PORTFOLIO_HISTORY_KEY, final_curve)
    if ok:
        print(f"✅ [backfill] Wrote {len(final_curve)} portfolio history points to Redis "
              f"({len(backfill_curve)} from trades + {len(live_tail)} live tail).")
    else:
        print("❌ [backfill] Redis write failed.")
        return 0

    return len(backfill_curve)


# Serializes all read-modify-write cycles against PORTFOLIO_HISTORY_KEY within
# this process. log_pnl() (per-trade writes) and portfolio_snapshot_loop in
# main.py (60-s heartbeat writes) both call portfolio_history_snapshot(), and
# without this lock two near-simultaneous calls could each read the same
# "existing" list, then write back, with one call's point silently lost
# (a classic lost-update race). A threading.Lock (not asyncio.Lock) is used
# deliberately: log_pnl is a plain synchronous function and may be called
# from sync contexts (e.g. the cleanup/migration script) as well as from
# inside async methods, so the lock must work in both.
#
# Note on scope: this protects against races *within a single process*. If
# this dashboard is ever scaled to more than one Render instance writing the
# same Redis key, a true cross-process lock (Redis MULTI/Lua, or a proper
# distributed mutex) would be required — the lightweight Upstash REST client
# used here only exposes plain GET/SET, not atomic compare-and-swap.
_history_write_lock = threading.Lock()

# Tracks the most recent round-key written per (asset, slug) so a duplicate
# log_pnl() call for the same market round (e.g. a retried exit handler)
# cannot double-write a snapshot for that round.
_recent_round_writes: Dict[str, float] = {}
_ROUND_DEDUP_TTL_SEC = 600  # 10 minutes — far longer than one 5-min round


def _round_already_written(round_key: Optional[str]) -> bool:
    if not round_key:
        return False
    now = t.time()
    # Opportunistically prune old entries so this dict never grows unbounded.
    expired = [k for k, ts in _recent_round_writes.items() if now - ts > _ROUND_DEDUP_TTL_SEC]
    for k in expired:
        _recent_round_writes.pop(k, None)
    return round_key in _recent_round_writes


def _mark_round_written(round_key: Optional[str]) -> None:
    if round_key:
        _recent_round_writes[round_key] = t.time()


def _is_flat_update(last_point: Optional[Dict], new_v: float, now_ms: int) -> bool:
    """
    True when this write would be a redundant duplicate of the last stored
    point: the value hasn't meaningfully changed AND the heartbeat interval
    hasn't elapsed yet. This is the core of the "only write when something
    actually changed" fix — see PORTFOLIO_FLAT_EPSILON / PORTFOLIO_HEARTBEAT_SEC
    above for the full rationale.
    """
    if not last_point:
        return False
    value_unchanged = abs(new_v - last_point["v"]) <= PORTFOLIO_FLAT_EPSILON
    heartbeat_due = (
        PORTFOLIO_HEARTBEAT_SEC > 0
        and (now_ms - last_point["t"]) >= (PORTFOLIO_HEARTBEAT_SEC * 1000)
    )
    return value_unchanged and not heartbeat_due


def portfolio_history_snapshot(total_pnl: float, round_key: Optional[str] = None) -> bool:
    """
    Record one live portfolio-equity observation.

    Called by MarketWorker.log_pnl() right after every completed trade so the
    chart updates the moment a market round closes — no background flush delay.

    Also called by main.py's portfolio_snapshot_loop every 60 s during normal
    operation so the curve stays continuous even in idle periods with no trades.

    Parameters
    ──────────
    total_pnl : the TOTAL portfolio PnL across every asset right now — never
                a single asset's own cumulative_pnl. Callers must use
                _portfolio_total_pnl() (or main.py's get_global_stats()
                total) to compute this.
    round_key : optional unique key (e.g. "{asset}:{slug}") identifying the
                market round this write corresponds to. When provided, a
                second call with the same round_key within
                _ROUND_DEDUP_TTL_SEC is ignored — this is the "only one valid
                snapshot per market round" safeguard.

    IMPORTANT — this does NOT always write a new point. A new {t, v} point is
    only ever persisted to Redis when:
        (a) the value changed by more than PORTFOLIO_FLAT_EPSILON since the
            last stored point, or
        (b) at least PORTFOLIO_HEARTBEAT_SEC has elapsed since the last
            stored point (so the curve keeps visibly extending to "now"
            during long flat stretches instead of stopping dead).
    Calls that are pure no-ops because of (a)/(b) failing still return True —
    "nothing needed to change" is a success, not a failure.

    Returns True on success (including no-op skips for duplicate round_keys
    or flat/unchanged values), False on a real failure.
    """
    if not _is_finite_number(total_pnl):
        print(f"⚠️ portfolio_history_snapshot rejected non-finite value: {total_pnl!r}")
        return False

    if _round_already_written(round_key):
        print(f"ℹ️ portfolio_history_snapshot skipped duplicate round write for {round_key!r}")
        return True

    if not _redis_available:
        _mark_round_written(round_key)
        return False

    with _history_write_lock:
        try:
            existing: List[Dict] = redis_get_json(PORTFOLIO_HISTORY_KEY) or []
            existing = sanitize_portfolio_history(
                existing, drop_isolated_spikes=False, collapse_flat_runs=True,
            )
            now_ms = int(t.time() * 1000)
            new_v  = round(float(total_pnl), 4)
            last_point = existing[-1] if existing else None

            # Avoid near-duplicate timestamps: if the last point is within
            # 2 s just update it in place rather than adding a near-zero-
            # width vertical segment. This takes priority over the flat-
            # update check below since it represents the same instant, not
            # a separate observation.
            if last_point and (now_ms - last_point["t"]) < 2000:
                existing[-1]["v"] = new_v
                changed = True
            elif _is_flat_update(last_point, new_v, now_ms):
                # ── THE FIX ───────────────────────────────────────────────
                # Value is unchanged from the last stored point and the
                # heartbeat interval hasn't elapsed — skip the write
                # entirely. No Redis round-trip, no new duplicate point.
                changed = False
            else:
                existing.append({"t": now_ms, "v": new_v})
                changed = True

            if not changed:
                _mark_round_written(round_key)
                return True

            # Re-sanitize AND collapse any flat runs before writing, so
            # stored history stays compact as it grows even in edge cases
            # the write-gate above doesn't catch (e.g. historical data
            # merged in from elsewhere).
            existing = sanitize_portfolio_history(
                existing, drop_isolated_spikes=False, collapse_flat_runs=True,
            )

            if len(existing) > MAX_HISTORY_POINTS:
                existing = existing[-MAX_HISTORY_POINTS:]

            ok = redis_set_json(PORTFOLIO_HISTORY_KEY, existing)
            if ok:
                _mark_round_written(round_key)
            return ok
        except Exception as e:
            print(f"⚠️ portfolio_history_snapshot error: {e}")
            return False


def filter_points_for_period(
    points: List[Dict],
    period: str,
    *,
    now_ms: Optional[int] = None,
) -> List[Dict]:
    """
    Rolling-window filter for chart data.

    Includes points within the period lookback (24h / 7d / 30d / 365d).
    Does NOT prepend anchor points before the window — x-axis starts at the
    first real data timestamp so new sessions are not padded with empty time.
    """
    now_ms = now_ms if now_ms is not None else int(t.time() * 1000)
    cleaned = sanitize_portfolio_history(
        points, drop_isolated_spikes=True, collapse_flat_runs=True,
    )
    if not cleaned:
        return []

    period_key = period.upper()
    if period_key == "ALL":
        return cleaned

    period_ms = CHART_PERIOD_MS.get(period_key)
    if period_ms is None:
        return cleaned

    cutoff = now_ms - period_ms
    filtered = [p for p in cleaned if p["t"] >= cutoff]
    if not filtered:
        return [cleaned[-1]]
    return filtered


def chart_time_domain(points: List[Dict]) -> Tuple[int, int]:
    """Data-driven x-axis bounds: [first timestamp, last timestamp]."""
    if not points:
        return (0, 0)
    return (int(points[0]["t"]), int(points[-1]["t"]))


def downsample_chart_points(points: List[Dict], max_points: int = 600) -> List[Dict]:
    """Downsample while preserving every value-change anchor."""
    n = len(points)
    if n <= max_points:
        return list(points)

    keep_idx = {0, n - 1}
    for i in range(1, n):
        if abs(points[i]["v"] - points[i - 1]["v"]) > PORTFOLIO_FLAT_EPSILON:
            keep_idx.add(i)

    anchors = [points[i] for i in sorted(keep_idx)]
    if len(anchors) <= max_points:
        return anchors

    step = max(1, len(anchors) // max_points)
    sampled = anchors[::step]
    if sampled[-1]["t"] != points[-1]["t"]:
        sampled.append(points[-1])
    return sampled


def prepare_chart_history(
    points: List[Dict],
    period: str = "1D",
    *,
    now_ms: Optional[int] = None,
    max_points: int = 600,
) -> List[Dict]:
    """
    Sanitize, period-filter, and downsample for /api/history.

    No synthetic gap-fill. X-axis domain is derived client-side from the
    first and last returned timestamps (data-driven, not fixed 24h padding).
    """
    filtered = filter_points_for_period(points, period, now_ms=now_ms)
    return downsample_chart_points(filtered, max_points=max_points)


def portfolio_history_get(period: str = "ALL") -> List[Dict]:
    """
    Fetch portfolio history filtered to the requested period.
    period: '1D' | '1W' | '1M' | '1Y' | 'ALL'
    Returns list of {t: unix_ms, v: pnl} dicts, oldest-first, fully
    sanitized (validated, de-duplicated, strictly chronological, isolated
    single-point spikes removed, and long flat runs collapsed to their two
    boundary points).
    Always returns at least the most recent point as a baseline.
    """
    raw_pts: List[Dict] = redis_get_json(PORTFOLIO_HISTORY_KEY) or []
    all_pts = sanitize_portfolio_history(
        raw_pts, drop_isolated_spikes=True, collapse_flat_runs=True,
    )

    if not all_pts or period.upper() == "ALL":
        return all_pts

    period_ms: Optional[int] = {
        "1D":  24 * 3600 * 1000,
        "1W":  7  * 24 * 3600 * 1000,
        "1M":  30 * 24 * 3600 * 1000,
        "1Y":  365 * 24 * 3600 * 1000,
    }.get(period.upper())

    if period_ms is None:
        return all_pts

    cutoff   = int(t.time() * 1000) - period_ms
    filtered = [p for p in all_pts if p["t"] >= cutoff]

    # Always return at least one point so the chart has a left-edge anchor
    if not filtered and all_pts:
        filtered = [all_pts[-1]]

    return filtered


# ═════════════════════════════════════════════════════════════════════════════
# TRADE HISTORY — persistent BUY/SELL execution log (Positions / History tabs)
#
# Redis key  : emiliano:trade:history
# Value      : JSON list of execution records, oldest-first, capped at
#              MAX_TRADE_HISTORY entries.
#
# Each record:
#   { id, timestamp, timestamp_ms, asset, market, slug,
#     action ("buy"|"sell"|"redeem"), side ("YES"|"NO"),
#     price, size }
#
# Action semantics
# ────────────────
# buy    — opening entry (confirmed BUY fill)
# sell   — early exit (stop-loss, take-profit, manual cashout)
# redeem — held to market resolution / settlement
#
# Live writes : log_trade() → append_trade_history()
# Backfill    : trade_history_backfill() reads emiliano:{asset}:trades and
#               local {asset}_pnl_history.json to synthesize BUY/SELL rows
#               from stored round details on startup.
# ═════════════════════════════════════════════════════════════════════════════

TRADE_HISTORY_KEY     = "emiliano:trade:history"
TRADE_HISTORY_LOCAL   = "trade_history.json"
MAX_TRADE_HISTORY     = 500
_trade_history_lock   = threading.Lock()
_trade_history_ids: set = set()   # in-process dedup cache


def _normalize_trade_action(action: str) -> str:
    """Normalize to buy | sell | redeem (lowercase)."""
    a = (action or "").lower().strip()
    if a in ("redeem", "redemption", "hodl", "settle", "settlement", "resolved"):
        return "redeem"
    if a in (
        "sell", "sell_exit", "exit", "cashout",
        "stop_loss", "take_profit", "manual_cashout",
    ):
        return "sell"
    if a in ("buy", "purchase", "entry"):
        return "buy"
    a_up = (action or "").upper()
    if a_up == "SELL":
        return "sell"
    if a_up == "BUY":
        return "buy"
    return "buy"


def _trade_record_id(
    asset: str, slug: str, action: str, side: str,
    timestamp_ms: int, price: float, size: float,
) -> str:
    return (
        f"{asset.lower()}:{slug}:{_normalize_trade_action(action)}:"
        f"{side}:{timestamp_ms}:{round(price, 4)}:{round(size, 4)}"
    )


def _load_trade_history_raw() -> List[Dict]:
    if _redis_available:
        data = redis_get_json(TRADE_HISTORY_KEY)
        if data and isinstance(data, list):
            return data
    try:
        if os.path.exists(TRADE_HISTORY_LOCAL):
            with open(TRADE_HISTORY_LOCAL, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"⚠️ Could not read {TRADE_HISTORY_LOCAL}: {e}")
    return []


def _persist_trade_history(records: List[Dict]) -> bool:
    capped = records[-MAX_TRADE_HISTORY:]
    ok = False
    if _redis_available:
        ok = redis_set_json(TRADE_HISTORY_KEY, capped)
    try:
        temp = TRADE_HISTORY_LOCAL + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(capped, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, TRADE_HISTORY_LOCAL)
        ok = True
    except Exception as e:
        print(f"⚠️ Local trade history write failed: {e}")
    return ok


def append_trade_history(record: Dict) -> bool:
    """
    Append one BUY/SELL execution record. Idempotent by record id.
    Returns True if stored (or already present), False on hard failure.
    """
    required = ("asset", "action", "side", "price", "size")
    if not all(record.get(k) is not None for k in required):
        return False

    ts_ms = record.get("timestamp_ms")
    if not ts_ms:
        ts_str = record.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parsed = _parse_ts(ts_str)
        ts_ms = parsed if parsed else int(t.time() * 1000)

    asset  = str(record["asset"]).upper()
    slug   = str(record.get("slug") or asset.lower())
    action = _normalize_trade_action(str(record["action"]))
    side   = str(record["side"]).upper()
    price  = round(float(record["price"]), 4)
    size   = round(float(record["size"]), 4)

    if side not in ("YES", "NO", "MOMENTUM") or size <= 0:
        return False
    if price < 0:
        return False
    # Losing redemption settles at $0 — valid for redeem only.
    if price <= 0 and action != "redeem":
        return False

    rec_id = record.get("id") or _trade_record_id(
        asset, slug, action, side, int(ts_ms), price, size,
    )

    entry = {
        "id":            rec_id,
        "timestamp":     record.get("timestamp") or datetime.fromtimestamp(
            ts_ms / 1000, tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_ms":  int(ts_ms),
        "asset":         asset,
        "market":        record.get("market") or f"{asset} Up or Down",
        "slug":          slug,
        "action":        action,
        "side":          side,
        "price":         price,
        "size":          size,
    }
    for extra in ("yes_price", "no_price", "yes_size", "no_size", "window"):
        if record.get(extra) is not None:
            entry[extra] = record[extra]

    with _trade_history_lock:
        if rec_id in _trade_history_ids:
            return True
        existing = _load_trade_history_raw()
        if any(r.get("id") == rec_id for r in existing):
            _trade_history_ids.add(rec_id)
            return True
        existing.append(entry)
        if _persist_trade_history(existing):
            _trade_history_ids.add(rec_id)
            return True
    return False


def _normalize_history_record(rec: Dict) -> Dict:
    """Ensure every stored/read record uses buy | sell | redeem."""
    out = dict(rec)
    out["action"] = _normalize_trade_action(rec.get("action", "buy"))
    return out


def get_trade_history(limit: int = 10) -> List[Dict]:
    """Return the most recent `limit` trade records, newest first."""
    with _trade_history_lock:
        records = _load_trade_history_raw()
    for r in records:
        if r.get("id"):
            _trade_history_ids.add(r["id"])
    sorted_recs = sorted(records, key=lambda x: x.get("timestamp_ms", 0), reverse=True)
    return [_normalize_history_record(r) for r in sorted_recs[: max(1, limit)]]


def _synthesize_history_from_pnl_trade(trade: Dict, asset: str, window: str = "5m") -> List[Dict]:
    """Build BUY (+ optional SELL) rows from one emiliano:{asset}:{window}:trades entry."""
    out: List[Dict] = []
    details = trade.get("details") or {}
    side    = details.get("side") or details.get("bought_side") or trade.get("side")
    if not side:
        return out

    slug    = trade.get("slug") or asset
    market  = trade.get("market") or f"{asset.upper()} Up or Down"
    ts_str  = trade.get("timestamp") or ""
    ts_ms   = _parse_ts(ts_str) or int(t.time() * 1000)
    size    = float(details.get("size") or details.get("shares") or MIN_SHARES)

    entry_price_raw = details.get("entry_price")
    entry_price = None
    if entry_price_raw is not None:
        entry_price = float(entry_price_raw)
        if entry_price > 1.0:
            entry_price = entry_price / 100.0

    if entry_price and entry_price > 0:
        out.append({
            "asset": asset.upper(), "window": window, "slug": slug, "market": market,
            "action": "buy", "side": str(side).upper(),
            "price": entry_price, "size": size,
            "timestamp": ts_str, "timestamp_ms": max(1, ts_ms - 1000),
        })

    exit_price_raw = details.get("exit_price")
    exit_price = None
    if exit_price_raw is not None:
        exit_price = float(exit_price_raw)
        if exit_price > 1.0:
            exit_price = exit_price / 100.0

    outcome = (trade.get("type") or "").upper()
    if exit_price is not None and outcome in (
        "TAKE_PROFIT", "STOP_LOSS", "MANUAL_CASHOUT",
    ):
        out.append({
            "asset": asset.upper(), "window": window, "slug": slug, "market": market,
            "action": "sell", "side": str(side).upper(),
            "price": max(exit_price, 0.0), "size": size,
            "timestamp": ts_str, "timestamp_ms": ts_ms,
        })
    elif outcome == "HODL":
        market_outcome = details.get("outcome")
        won = market_outcome and str(market_outcome).upper() == str(side).upper()
        settle = 1.0 if won else 0.0
        out.append({
            "asset": asset.upper(), "window": window, "slug": slug, "market": market,
            "action": "redeem", "side": str(side).upper(),
            "price": settle, "size": size,
            "timestamp": ts_str, "timestamp_ms": ts_ms,
        })

    return out


def trade_history_backfill() -> int:
    """
    Idempotent backfill of emiliano:trade:history from existing PnL trade
    records. Returns count of newly appended records.
    """
    if not _redis_available and not os.path.exists(TRADE_HISTORY_LOCAL):
        pass  # still attempt local-only backfill below

    print("🔄 [trade-history] Inspecting existing trade records for backfill...")
    synthesized: List[Dict] = []
    for wc in WORKER_CONFIGS:
        for trade in _load_trades_for_asset(wc.asset, wc.window):
            synthesized.extend(_synthesize_history_from_pnl_trade(trade, wc.asset, wc.window))

    if not synthesized:
        print("ℹ️  [trade-history] No trade records to backfill from.")
        return 0

    synthesized.sort(key=lambda x: x.get("timestamp_ms", 0))
    added = 0
    for rec in synthesized:
        before = len(_trade_history_ids)
        if append_trade_history(rec):
            with _trade_history_lock:
                if len(_trade_history_ids) > before:
                    added += 1
    print(f"✅ [trade-history] Backfill complete — {added} new records added.")
    return added


def collect_open_positions(workers: List["MarketWorker"]) -> List[Dict]:
    """
    Return one snapshot per open position (FILLED state only).
    Duplicate ids are suppressed — at most one row per asset/market round.
    """
    seen: set = set()
    out: List[Dict] = []
    for worker in workers:
        snap = worker.get_position_snapshot()
        if not snap:
            continue
        pid = snap["id"]
        if pid in seen:
            continue
        seen.add(pid)
        out.append(snap)
    return out


def find_worker(
    workers: List["MarketWorker"], asset: str, window: str,
) -> Optional["MarketWorker"]:
    a = asset.strip().lower()
    w = window.strip().lower()
    for worker in workers:
        if worker.asset_type == a and worker.window_slug == w:
            return worker
    return None


def find_worker_by_asset(workers: List["MarketWorker"], asset: str) -> Optional["MarketWorker"]:
    """Backward-compatible lookup — returns first worker matching asset."""
    key = asset.strip().lower()
    for worker in workers:
        if worker.asset_type == key:
            return worker
    return None


# ═════════════════════════════════════════════════════════════════════════════
# BINANCE DEPTH SIGNAL  (display / context only — not a trade gate)
# ═════════════════════════════════════════════════════════════════════════════

class BinanceDepthSignal:
    _instances: Dict[str, "BinanceDepthSignal"] = {}
    _instance_lock = asyncio.Lock()

    @classmethod
    async def get_or_create(cls, symbol: str) -> "BinanceDepthSignal":
        async with cls._instance_lock:
            sym = symbol.upper()
            if sym not in cls._instances:
                inst = cls(sym)
                cls._instances[sym] = inst
                asyncio.create_task(inst._run())
            return cls._instances[sym]

    def __init__(self, symbol: str):
        self.symbol      = symbol
        self.imbalance   = 0.0
        self.momentum    = 0.0
        self.last_update = 0.0
        self._history: deque = deque(maxlen=8)
        self._price_history: deque = deque(maxlen=4000)
        self.last_trade_price = 0.0
        self.last_trade_time = 0.0
        self.price_delta = 0.0
        self._running    = False

    @property
    def is_fresh(self) -> bool:
        now = t.time()
        last = max(float(self.last_update or 0), float(self.last_trade_time or 0))
        if last <= 0:
            return False
        return (now - last) < BINANCE_STALE_CUTOFF_SECS

    @property
    def is_primed(self) -> bool:
        return self.is_fresh and abs(self.imbalance) >= BINANCE_PRIME_THRESHOLD

    @property
    def signal_label(self) -> str:
        if not self.is_fresh:
            return "STALE"
        if abs(self.imbalance) >= BINANCE_PRIME_THRESHOLD:
            return "STRONGLY BULL ↑" if self.imbalance > 0 else "STRONGLY BEAR ↓"
        if abs(self.imbalance) >= 0.10:
            return "MILDLY BULL ↑"   if self.imbalance > 0 else "MILDLY BEAR ↓"
        return "NEUTRAL"

    @property
    def price_ready(self) -> bool:
        return self.last_trade_price > 0 and len(self._price_history) >= 2

    @property
    def age_seconds(self) -> float:
        return max(0.0, t.time() - self.last_trade_time) if self.last_trade_time else 999.0

    async def _run(self):
        if self._running:
            return
        self._running = True
        depth_stream = f"{self.symbol.lower()}usdt@depth{BINANCE_DEPTH_LIMIT}@100ms"
        trade_stream = f"{self.symbol.lower()}usdt@aggTrade"
        url    = f"wss://stream.binance.com:9443/stream?streams={depth_stream}/{trade_stream}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=15) as ws:
                    print(f"📡 [Binance WS] Connected: {depth_stream} + {trade_stream}")
                    async for raw in ws:
                        msg  = json.loads(raw)
                        data = msg.get("data", msg)
                        event_type = data.get("e")
                        if event_type == "aggTrade":
                            try:
                                px = float(data.get("p", 0.0))
                            except (TypeError, ValueError):
                                px = 0.0
                            if px > 0:
                                now = t.time()
                                self.last_trade_price = px
                                self.last_trade_time = now
                                self.last_update = now
                                self._price_history.append((now, px))
                                retention = max(
                                    130.0,
                                    float(os.getenv("BINANCE_PRICE_HISTORY_SEC", "150")),
                                )
                                hist_cutoff = now - retention
                                while self._price_history and self._price_history[0][0] < hist_cutoff:
                                    self._price_history.popleft()
                                lookback = max(0.10, float(os.getenv("BINANCE_MOMENTUM_LOOKBACK", "1.0")))
                                lb_cutoff = now - lookback
                                lb_samples = [(ts, p) for ts, p in self._price_history if ts >= lb_cutoff]
                                if lb_samples:
                                    base = lb_samples[0][1]
                                    self.price_delta = (px - base) / base if base else 0.0
                                # Event-driven wakeup: the strategy router can react to
                                # Binance aggTrade without waiting for a Polymarket tick.
                                with _worker_registry_lock:
                                    targets = [w for w in _worker_registry if w.asset_type == self.symbol.lower()]
                                for worker in targets:
                                    asyncio.create_task(worker.on_binance_tick())
                        bids = data.get("b", [])
                        asks = data.get("a", [])
                        if bids and asks:
                            self._process(bids, asks)
            except Exception as e:
                print(f"⚠️ [Binance WS] {self.symbol}: {e} — reconnecting in 3s")
                await asyncio.sleep(3)

    def _process(self, bids: list, asks: list):
        def weighted_vol(levels: list) -> float:
            tw = tv = 0.0
            for i, item in enumerate(levels[:20]):
                qty = float(item[1])
                w   = 1.0 / (i + 1) ** 0.6
                tv += qty * w
                tw += w
            return tv / tw if tw > 0 else 0.0

        bid_v = weighted_vol(bids)
        ask_v = weighted_vol(asks)
        total = bid_v + ask_v
        if total <= 0:
            return
        raw = (bid_v - ask_v) / total
        self._history.append(raw)
        self.last_update = t.time()
        if len(self._history) >= 4:
            recent         = list(self._history)[-4:]
            self.imbalance = sum(recent) / len(recent)
            self.momentum  = recent[-1] - recent[0]
        else:
            self.imbalance = raw
            self.momentum  = 0.0


# ═════════════════════════════════════════════════════════════════════════════
# ACCOUNT SERVICE — GLOBAL, SINGLE-INSTANCE, BOT-LEVEL
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything in this class represents work that is tied to the WALLET / ACCOUNT,
# not to any individual market. There is exactly one AccountService for the
# entire bot process, constructed once in main() and shared by reference into
# every MarketWorker. Nothing in here is duplicated per asset.
#
# Responsibilities:
#   • Single Web3 connection + single ClobClient (auth derived/created once)
#   • One-time startup wallet audit (balances + on-chain approvals)
#   • Shared order-placement / order-status / cancel helpers used by all workers
#   • Shared on-chain share-balance lookups used by all workers
#   • Global periodic PnL merge background task (+ on-demand merge_now())
#
# Per-market logic (price listening, entry/exit, TP/SL, per-asset PnL bookkeeping)
# stays in MarketWorker — see below.
# ═════════════════════════════════════════════════════════════════════════════

class AccountService:
    def __init__(self):
        pk_raw     = os.getenv("PRIVATE_KEY")
        funder_raw = os.getenv("FUNDER_ADDRESS")
        if not pk_raw or not funder_raw:
            raise ValueError("Missing PRIVATE_KEY or FUNDER_ADDRESS in .env")

        pk     = _validate_private_key_env("PRIVATE_KEY", pk_raw)
        funder = _validate_wallet_address_env("FUNDER_ADDRESS", funder_raw)

        self.w3             = Web3(Web3.HTTPProvider(POLYGON_RPC))
        self.wallet_address = funder
        self.signer_address = self.w3.eth.account.from_key(pk).address
        self.private_key    = pk
        sig_type = _resolve_polymarket_signature_type(self.signer_address, funder)
        self.signature_type = sig_type

        print(f"Signer : {self.signer_address}")
        print(f"Funder : {self.wallet_address}")
        print(f"CLOB signature_type: {_SIGNATURE_TYPE_LABELS.get(sig_type, sig_type)}")

        _l1_client = ClobClient(
            host=HOST, key=pk, chain_id=137, funder=funder, signature_type=sig_type  # type: ignore
        )

        print("🔑 Authenticating with Polymarket (V2)...")
        try:
            raw_creds = _l1_client.derive_api_key()
            if raw_creds is None or not getattr(raw_creds, 'api_key', None):
                print("⚠️  No existing key found — creating new one...")
                raw_creds = _l1_client.create_api_key()
            print("✅ API Authentication Successful")
        except Exception as e:
            print(f"❌ Authentication Failed: {e}")
            raise

        # Single shared ClobClient — used by every MarketWorker for order
        # placement / status / cancellation. There is only ever one of these
        # for the whole process, regardless of how many assets are tracked.
        self.client = ClobClient(
            host=HOST, key=pk, chain_id=137, funder=funder,
            signature_type=sig_type, creds=raw_creds,  # type: ignore
        )

        if sig_type == int(SignatureTypeV2.POLY_1271):
            self._verify_deposit_wallet_deployed()
            self._sync_clob_collateral_balance()

        # Guards so audit/init work can never accidentally run twice even if
        # something calls these methods more than once.
        self._audited = False
        self._merge_task: Optional[asyncio.Task] = None

    def _verify_deposit_wallet_deployed(self) -> None:
        """Warn when the funder address has no on-chain contract code."""
        try:
            code = self.w3.eth.get_code(self.wallet_address)
            if not code or code in (b"", b"\x00"):
                print(
                    f"\n{YELLOW}⚠️  [DEPOSIT WALLET] {self.wallet_address} has no contract "
                    f"code on Polygon.{RESET}\n"
                    "   Deploy it first: place one trade on polymarket.com, or use the\n"
                    "   py-builder-relayer-client deploy_deposit_wallet() flow.\n"
                    "   Orders will fail until the wallet is deployed."
                )
            else:
                print(f"✅ Deposit wallet contract found at {self.wallet_address}")
        except Exception as e:
            print(f"⚠️  Could not verify deposit wallet deployment: {e}")

    def _sync_clob_collateral_balance(self) -> None:
        """Required for deposit-wallet (POLY_1271) accounts before placing orders."""
        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            print("✅ CLOB collateral balance/allowance synced (deposit wallet)")
        except Exception as e:
            print(f"⚠️  CLOB balance sync failed (non-fatal at init): {e}")

    # ── On-chain approvals (account-level — run once for the whole wallet) ──

    def set_approvals(self, operator_address: str, label: str):
        print(f"⏳ Sending approval for {label}...")
        ctf_abi = [
            {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
             "name": "setApprovalForAll", "outputs": [], "type": "function"}
        ]
        ctf_contract   = self.w3.eth.contract(
            address=self.w3.to_checksum_address(CTF_CONTRACT), abi=ctf_abi)
        signer_account = self.w3.eth.account.from_key(os.getenv("PRIVATE_KEY"))
        signer_address = signer_account.address
        try:
            gas_balance = self.w3.eth.get_balance(signer_address)
            if gas_balance < self.w3.to_wei(0.01, 'ether'):
                print(f"❌ Signer ({signer_address}) needs at least 0.01 POL for gas.")
                return False
            current_gas_price   = self.w3.eth.gas_price
            increased_gas_price = Wei(int(current_gas_price * 1.2))
            tx_params: TxParams = {
                'from':     signer_address,
                'nonce':    self.w3.eth.get_transaction_count(signer_address, "pending"),
                'gas':      100000,
                'gasPrice': increased_gas_price,
                'chainId':  137,
            }
            tx = ctf_contract.functions.setApprovalForAll(
                self.w3.to_checksum_address(operator_address), True
            ).build_transaction(tx_params)
            signed_tx = self.w3.eth.account.sign_transaction(tx, os.getenv("PRIVATE_KEY"))
            tx_hash   = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"✅ Approval sent! Hash: {tx_hash.hex()}")
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120, poll_latency=2.0)
            return True
        except Exception as e:
            print(f"❌ Approval failed for {label}: {e}")
            return False

    def get_pol_balance(self):
        balance_wei = self.w3.eth.get_balance(self.wallet_address)
        return float(self.w3.from_wei(balance_wei, 'ether'))

    def _funder_private_key(self) -> Optional[str]:
        """Return the key that controls FUNDER_ADDRESS approvals."""
        funder_pk_raw = os.getenv("FUNDER_PRIVATE_KEY", "").strip()
        if funder_pk_raw:
            try:
                return _validate_private_key_env("FUNDER_PRIVATE_KEY", funder_pk_raw)
            except ValueError as e:
                print(f"❌ {e}")
                return None
        if self.wallet_address.lower() == self.signer_address.lower():
            return self.private_key
        return None

    def check_and_approve_pusd(self, spender_address: str, label: str) -> bool:
        pusd_abi = [
            {"inputs": [{"name": "owner",   "type": "address"}, {"name": "spender", "type": "address"}],
             "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
            {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount",  "type": "uint256"}],
             "name": "approve",   "outputs": [{"name": "", "type": "bool"}],    "type": "function"},
        ]
        pusd_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(PUSD_ADDRESS), abi=pusd_abi)
        spender           = self.w3.to_checksum_address(spender_address)
        current_allowance = pusd_contract.functions.allowance(self.wallet_address, spender).call()
        if current_allowance < 1_000_000:
            funder_pk = self._funder_private_key()
            if not funder_pk:
                print(
                    f"❌ Cannot approve pUSD for {label}. Set FUNDER_PRIVATE_KEY in .env "
                    f"(required when FUNDER_ADDRESS differs from the signer)."
                )
                return False
            print(f"🔓 [GAS] Approving pUSD for {label}...")
            tx_from = self.wallet_address
            tx_params = cast(TxParams, {
                'from':     tx_from,
                'nonce':    self.w3.eth.get_transaction_count(tx_from, "pending"),
                'gas':      60000,
                'gasPrice': int(self.w3.eth.gas_price * 1.2),
                'chainId':  137,
            })
            raw_tx    = pusd_contract.functions.approve(spender, 2**256 - 1).build_transaction(tx_params)
            signed_tx = self.w3.eth.account.sign_transaction(raw_tx, funder_pk)
            tx_hash   = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            self.w3.eth.wait_for_transaction_receipt(tx_hash, poll_latency=2.0)
            print(f"✅ {label} pUSD: Approved.")
        else:
            print(f"✅ {label} pUSD: Already Approved")
        return True

    def check_and_approve_shares(self, operator_address: str, label: str) -> bool:
        ctf_abi = [
            {"inputs": [{"name": "account",  "type": "address"}, {"name": "operator", "type": "address"}],
             "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
            {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
             "name": "setApprovalForAll", "outputs": [], "type": "function"},
        ]
        ctf_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(CTF_CONTRACT), abi=ctf_abi)
        operator    = self.w3.to_checksum_address(operator_address)
        is_approved = ctf_contract.functions.isApprovedForAll(self.wallet_address, operator).call()
        if not is_approved:
            print(f"🔓 [POL TX] Funder granting {label} permission to handle shares...")
            funder_pk = self._funder_private_key()
            if not funder_pk:
                print(
                    f"❌ Cannot approve {label}. Set FUNDER_PRIVATE_KEY in .env "
                    f"(required when FUNDER_ADDRESS differs from the signer)."
                )
                return False
            tx_from = self.wallet_address
            tx_params: TxParams = {
                'from':     tx_from,
                'nonce':    self.w3.eth.get_transaction_count(tx_from, "pending"),
                'gas':      120000,
                'gasPrice': Wei(int(self.w3.eth.gas_price * 1.5)),
                'chainId':  137,
            }
            raw_tx    = ctf_contract.functions.setApprovalForAll(operator, True).build_transaction(tx_params)
            signed_tx = self.w3.eth.account.sign_transaction(raw_tx, funder_pk)
            tx_hash   = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"⏳ Confirming {label} approval... Hash: {tx_hash.hex()}")
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            print(f"✅ {label} Shares Enabled.")
        else:
            print(f"✅ {label} Shares: Already Approved")
        return True

    def run_wallet_audit(self) -> bool:
        """
        Account-level wallet audit: balance check + (if not DRY_MODE) on-chain
        approvals. This runs EXACTLY ONCE for the entire bot process — at
        startup, before any MarketWorker begins trading — regardless of how
        many assets are being tracked. It must never be called per-asset.
        """
        if self._audited:
            print("ℹ️  Wallet audit already completed this session — skipping duplicate run.")
            return True

        min_abi = [
            {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
             "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "decimals",
             "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
        ]
        pusd_contract  = self.w3.eth.contract(
            address=self.w3.to_checksum_address(PUSD_ADDRESS), abi=min_abi)
        usdce_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(USDC_E), abi=min_abi)

        signer_pusd  = pusd_contract.functions.balanceOf(self.signer_address).call() / 10**6
        funder_pusd  = pusd_contract.functions.balanceOf(self.wallet_address).call() / 10**6
        funder_usdce = usdce_contract.functions.balanceOf(self.wallet_address).call() / 10**6

        print(f"\n💵 Signer pUSD Balance : {signer_pusd:.2f} pUSD")
        print(f"💵 Funder pUSD Balance : {funder_pusd:.2f} pUSD")
        print(f"💵 Funder USDC.e       : {funder_usdce:.2f} USDC.e  (legacy — not used as collateral)")

        if live_on_chain_setup_required():
            if self.signature_type == int(SignatureTypeV2.POLY_1271):
                # Deposit wallets: approvals are set via relayer WALLET batches,
                # not direct EOA/funder PK transactions. Sync CLOB state instead.
                self._verify_deposit_wallet_deployed()
                self._sync_clob_collateral_balance()
            else:
                if not self._funder_private_key():
                    print(
                        "\n❌ Live mode requires FUNDER_PRIVATE_KEY in .env when FUNDER_ADDRESS "
                        "is a proxy wallet (different from PRIVATE_KEY signer).\n"
                        "   Set DRY_RUN=true to skip on-chain approvals for paper trading, or "
                        "add FUNDER_PRIVATE_KEY for live trading."
                    )
                    return False
                operators = [
                    (STANDARD_EXCHANGE, "Main Exchange"),
                    (NEG_RISK_EXCHANGE, "Neg-Risk Exchange"),
                    (NEG_RISK_ADAPTER,  "Neg-Risk Adapter"),
                ]
                for addr, label in operators:
                    if not self.check_and_approve_pusd(addr, label):
                        return False
                    if not self.check_and_approve_shares(addr, label):
                        return False

        self._audited = True
        return True

    # ── Shared on-chain balance helpers (used by every MarketWorker) ────────

    async def get_onchain_share_balance_async(self, token_id: str, retries: int = 3) -> float:
        abi = [{"inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
                "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(CTF_CONTRACT), abi=abi)
        for attempt in range(retries):
            try:
                raw_balance = contract.functions.balanceOf(
                    self.wallet_address, int(token_id)).call()
                return float(raw_balance / 10**6)
            except Exception as e:
                if attempt < retries - 1:
                    wait = (attempt + 1) * 2
                    print(f"⚠️ RPC Glitch: {e}. Retrying in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    print(f"❌ CRITICAL: On-chain balance check failed after {retries} attempts.")
                    return -1.0
        return -1.0

    def get_onchain_share_balance(self, token_id: str, retries: int = 3) -> float:
        """Synchronous version kept for non-async call sites (approvals, audits)."""
        abi = [{"inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
                "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(CTF_CONTRACT), abi=abi)
        for attempt in range(retries):
            try:
                raw_balance = contract.functions.balanceOf(
                    self.wallet_address, int(token_id)).call()
                return float(raw_balance / 10**6)
            except Exception as e:
                if attempt < retries - 1:
                    wait = (attempt + 1) * 2
                    print(f"⚠️ RPC Glitch: {e}. Retrying in {wait}s...")
                    t.sleep(wait)
                else:
                    print(f"❌ CRITICAL: On-chain balance check failed after {retries} attempts.")
                    return -1.0
        return -1.0

    async def merge_shares(self, active_market: Optional[Dict[str, Any]], amount_to_merge: float):
        """Merge YES+NO shares back into pUSD on-chain. Account-level operation —
        takes the market dict explicitly since the position itself is per-market."""
        if active_market is None:
            print("❌ Merge aborted: No active market metadata found.")
            return
        CTF_MAIN = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
        ctf_abi = [{
            "name": "mergePositions", "type": "function",
            "inputs": [
                {"name": "collateralToken",    "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId",        "type": "bytes32"},
                {"name": "partition",          "type": "uint256[]"},
                {"name": "amount",             "type": "uint256"},
            ],
            "outputs": [],
        }]
        target_address = self.w3.to_checksum_address(CTF_MAIN)
        contract       = self.w3.eth.contract(address=target_address, abi=ctf_abi)
        raw_amount     = int(amount_to_merge * 10**6)
        try:
            parent_id = "0x" + "0" * 64
            partition = [1, 2]
            cond_id   = active_market.get('condition_id')
            if not cond_id:
                print("❌ Market metadata missing condition_id.")
                return
            nonce = self.w3.eth.get_transaction_count(self.signer_address, "pending")
            tx = contract.functions.mergePositions(
                PUSD_ADDRESS, parent_id, cond_id, partition, raw_amount
            ).build_transaction({
                'from':     self.signer_address,
                'gas':      180000,
                'gasPrice': self.w3.eth.gas_price,
                'nonce':    nonce,
            })
            signed  = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            print(f"♻️ Capital Recycled (pUSD)! Hash: {tx_hash.hex()}")
            return tx_hash
        except Exception as e:
            print(f"❌ On-chain Merge Error: {e}")

    # ── Shared order execution helpers (used by every MarketWorker) ─────────

    def create_and_post_order(
        self,
        side_str: str,
        price: float,
        size: float,
        token_id: str,
        order_type: str = "FOK",
    ):
        """Build + submit an order through the single shared ClobClient (FOK default)."""
        order_args   = OrderArgs(price=price, size=size, side=side_str, token_id=token_id)
        signed_order = self.client.create_order(order_args)
        ot = parse_order_type(order_type)
        try:
            return self.client.post_order(signed_order, cast(OrderType, ot))
        except Exception as e:
            err = str(e).lower()
            if self.signature_type == int(SignatureTypeV2.POLY_1271) and (
                "deposit wallet" in err or "api key" in err or "maker address" in err
            ):
                print("🔄 Retrying order after CLOB balance sync (deposit wallet)...")
                self._sync_clob_collateral_balance()
                signed_order = self.client.create_order(order_args)
                return self.client.post_order(signed_order, cast(OrderType, ot))
            raise

    def get_order_status(self, order_id: str):
        return self.client.get_order(order_id)

    def cancel_order(self, order_id: str):
        payload = OrderPayload(orderID=order_id)
        return self.client.cancel_order(payload)

    # ── Global periodic PnL merge (background scheduler, single instance) ───

    def start_pnl_merge_scheduler(self):
        """
        Launches the recurring PnL-merge background task exactly once for the
        whole bot. This task runs on a fixed interval (MERGE_INTERVAL_SECONDS)
        independent of any individual trade completing — it is the bot-level
        equivalent of "account/PnL updates" and must not be created per asset.
        """
        if self._merge_task is not None and not self._merge_task.done():
            print("ℹ️  PnL merge scheduler already running — skipping duplicate start.")
            return self._merge_task
        self._merge_task = asyncio.create_task(self._pnl_merge_loop())
        return self._merge_task

    async def _pnl_merge_loop(self):
        while True:
            await asyncio.sleep(MERGE_INTERVAL_SECONDS)
            try:
                merge_all_pnl(send_telegram_notify=False)
            except Exception as e:
                print(f"⚠️ Scheduled PnL merge failed: {e}")

    def merge_now(self, send_telegram_notify: bool = False):
        """On-demand merge, called by a MarketWorker right after a trade closes."""
        merge_all_pnl(send_telegram_notify=send_telegram_notify)

# ═════════════════════════════════════════════════════════════════════════════
# MARKET WORKER — ONE INSTANCE PER TRACKED ASSET/MARKET
# ═════════════════════════════════════════════════════════════════════════════
#
# Everything in this class is scoped to a single asset (e.g. "btc", "eth").
# All wallet/account-level concerns (Web3 connection, ClobClient, wallet audit,
# on-chain approvals, the global PnL-merge scheduler) have been moved OUT of
# this class and into AccountService, which is constructed once in main() and
# passed in here by reference (`account`). MarketWorker never creates its own
# Web3 connection, never calls derive_api_key/create_api_key, and never runs
# the wallet audit — it borrows the shared AccountService for all of that.
#
# What legitimately stays per-instance here:
#   • Order book / price-listener WebSocket subscription for this asset
#   • Entry signal generation, TP/SL exit logic for this asset's position
#   • Per-asset PnL bookkeeping (own Redis keys / own JSON history file)
#   • Per-asset dashboard state
# ═════════════════════════════════════════════════════════════════════════════

class MarketWorker:
    def __init__(self, worker_config: WorkerConfig, account: "AccountService"):
        self.worker_config = worker_config
        self.account = account

        self.w3             = account.w3
        self.client         = account.client
        self.wallet_address = account.wallet_address
        self.signer_address = account.signer_address
        self.private_key    = account.private_key

        self.asset_type    = worker_config.asset.lower()
        self.window_slug   = worker_config.window
        self.active_market: Optional[Dict[str, Any]] = None
        self.prices: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self.bids: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self.ask_sizes: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self._quote_updated_at: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self.transitioning = False

        self.session_profit = 0.0
        self.trade_count    = 0

        _saved              = self._load_pnl_stats(self.asset_type, self.window_slug)
        self.cumulative_pnl: float = _saved["total_pnl"]
        self.wins: int             = _saved["wins"]
        self.losses: int           = _saved["losses"]
        self._logged_exit_order_ids: set[str] = set()
        canonical_ledger.hydrate_worker_from_saved_stats(
            self.worker_config.key,
            total_pnl=self.cumulative_pnl,
            wins=self.wins,
            losses=self.losses,
        )
        _ledger = canonical_ledger.worker_summary(self.worker_config.key)
        if abs(_ledger.realized_pnl) > 1e-9:
            self.cumulative_pnl = round(_ledger.realized_pnl, 4)
            self.wins = _ledger.wins
            self.losses = _ledger.losses
        self.trade_count = self.wins + self.losses

        self.trade_state: TradeState = TradeState.IDLE
        self.position_side: Optional[str] = None
        self.position_size: float = 0.0
        self.entry_price:   float = 0.0

        self.exited            = False
        self.processed_markets = set()
        self.entry_timestamp   = None
        self.market_start_time: Optional[float] = None

        self.last_trade_time = 0
        self.logged_markets  = set()

        self.order_state: OrderState = OrderState.IDLE
        self.inventory = PositionInventory()
        self.entry_count = 0
        self._open_orders: Dict[str, Tuple[str, float, float]] = {}

        self.last_yes_update = 0.0
        self.last_no_update  = 0.0
        self._ask_history: Dict[str, Deque[Tuple[float, float]]] = {
            "YES": deque(maxlen=80),
            "NO": deque(maxlen=80),
        }
        self._knife_block_until: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self._knife_low: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self._knife_latched: Dict[str, bool] = {"YES": False, "NO": False}
        self._ws_market_data_stale: bool = False
        self._processed_fill_order_ids: set[str] = set()

        self.market_outcome  = None
        self.final_yes_price = 0.0
        self.final_no_price  = 0.0

        self.market_slug = None
        self.seen_markets = set()

        self._order_lock = asyncio.Lock()
        self._binance_eval_lock = asyncio.Lock()
        self._stop_loss_lock = asyncio.Lock()
        self._stop_loss_exit_in_flight: bool = False
        self._market_entry_guard: set[str] = set()
        self._book_cache: Dict[str, Tuple[float, Any]] = {}
        self._last_public_quote_refresh: float = 0.0
        self.engine_router = ArbigabEngineRouter()
        self._engine_orders: Dict[str, Tuple[str, float, float, str, str]] = {}

        label = f"{self.asset_type.upper()} {self.window_slug}"
        self.dashboard = {
            "asset":              self.asset_type.upper(),
            "window":             self.window_slug,
            "yes":                0,
            "no":                 0,
            "timer":              "--:--",
            "listener":           "--:--",
            "status":             "WAITING",
            "outcome":            "PENDING",
            "combined_bid_c":     0,
            "yes_bid_c":          0,
            "no_bid_c":           0,
            "yes_shares":         0.0,
            "no_shares":          0.0,
            "yes_avg_price_c":    0.0,
            "no_avg_price_c":     0.0,
            "pair_avg_price_c":   0.0,
            "momentum_entry_threshold_c": round(worker_config.momentum_entry_threshold * 100),
            "stop_loss_pct":      0.0,
            "gabagool_initial_entry_threshold_c": round(worker_config.gabagool_initial_entry_threshold * 100, 1),
            "gabagool_max_pair_cost_c": round(worker_config.gabagool_max_pair_cost * 100, 1),
            "gabagool_min_profit_margin_c": round(worker_config.gabagool_min_profit_margin * 100, 1),
            "gabagool_max_unpaired_shares": worker_config.gabagool_max_unpaired_shares,
            "gabagool_min_time_to_resolution": worker_config.gabagool_min_time_to_resolution,
            "gabagool_inventory_soft_limit": worker_config.gabagool_inventory_soft_limit,
            "gabagool_endgame_seconds": worker_config.gabagool_endgame_seconds,
            "gabagool_endgame_max_unpaired": worker_config.gabagool_endgame_max_unpaired,
            "gabagool_falling_knife_enabled": worker_config.gabagool_falling_knife_enabled,
            "gabagool_falling_knife_window_seconds": worker_config.gabagool_falling_knife_window_seconds,
            "gabagool_falling_knife_min_drop_c": worker_config.gabagool_falling_knife_min_drop * 100.0,
            "falling_knife_blocked": False,
            "falling_knife_side": "-",
            "falling_knife_drop_c": 0.0,
            "entry_count":        0,
            "engine":             "idle",
            "preemptive_cancel_active": False,
            "preemptive_cancel_side": "-",
            "position_delta":      0.0,
            "pnl_if_up":            0.0,
            "pnl_if_down":          0.0,
            "mark_pnl":             0.0,
            "invested":             0.0,
            "locked_profit":        0.0,
            "matched_pairs":        0.0,
        }
        self.recent_logs: Deque[str] = deque(maxlen=4)

        self._last_logged_slug: Optional[str] = None

        self._locked_skip_logged: bool = False

        wc = worker_config
        if wc.inventory_accumulation_enabled and not wc.momentum_strategy_enabled:
            self.strategy = InventoryAccumulationStrategy()
        else:
            self.strategy = MomentumStrategy()

        _register_worker(self)

    # ═════════════════════════════════════════════════════════════════════
    # REDIS-BACKED PnL PERSISTENCE  (per-asset — each market keeps its own
    # trade history / win-loss record, which is intentionally NOT shared)
    # ═════════════════════════════════════════════════════════════════════

    def _redis_stats_key(self) -> str:
        return f"emiliano:{self.asset_type}:{self.window_slug}:stats"

    def _redis_trades_key(self) -> str:
        return f"emiliano:{self.asset_type}:{self.window_slug}:trades"

    @staticmethod
    def _load_pnl_stats(asset_type: str, window: str) -> dict:
        redis_key = f"emiliano:{asset_type}:{window}:stats"
        if _redis_available:
            data = redis_get_json(redis_key)
            if data:
                print(f"✅ [{asset_type.upper()} {window}] Loaded PnL stats from Redis: "
                      f"PnL=${data.get('total_pnl', 0):.2f} "
                      f"W{data.get('wins', 0)}/L{data.get('losses', 0)}")
                return {
                    "total_pnl": float(data.get("total_pnl", 0.0)),
                    "wins":      int(data.get("wins", 0)),
                    "losses":    int(data.get("losses", 0)),
                }
            legacy = redis_get_json(f"emiliano:{asset_type}:stats")
            if legacy:
                print(f"✅ [{asset_type.upper()} {window}] Loaded legacy PnL stats from Redis")
                return {
                    "total_pnl": float(legacy.get("total_pnl", 0.0)),
                    "wins":      int(legacy.get("wins", 0)),
                    "losses":    int(legacy.get("losses", 0)),
                }

        file_path = asset_pnl_filename(asset_type, window)
        try:
            if os.path.exists(file_path):
                with open(file_path, "r") as f:
                    data = json.load(f)
                result = {
                    "total_pnl": float(data.get("total_pnl", 0.0)),
                    "wins":      int(data.get("wins", 0)),
                    "losses":    int(data.get("losses", 0)),
                }
                if _redis_available:
                    print(f"📤 [{asset_type.upper()} {window}] Migrating local stats → Redis...")
                    redis_set_json(redis_key, result)
                return result
        except Exception:
            pass

        legacy_path = f"{asset_type}_pnl_history.json"
        try:
            if os.path.exists(legacy_path):
                with open(legacy_path, "r") as f:
                    data = json.load(f)
                return {
                    "total_pnl": float(data.get("total_pnl", 0.0)),
                    "wins":      int(data.get("wins", 0)),
                    "losses":    int(data.get("losses", 0)),
                }
        except Exception:
            pass

        return {"total_pnl": 0.0, "wins": 0, "losses": 0}

    def _save_stats_to_redis(self):
        if not _redis_available:
            return
        payload = {
            "total_pnl":    round(self.cumulative_pnl, 4),
            "wins":         self.wins,
            "losses":       self.losses,
            "win_rate":     (f"{round((self.wins / (self.wins + self.losses)) * 100, 2)}%"
                             if (self.wins + self.losses) > 0 else "0%"),
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        ok = redis_set_json(self._redis_stats_key(), payload)
        if ok:
            print(f"💾 [{self.asset_type.upper()}] Stats saved to Redis: "
                  f"PnL=${self.cumulative_pnl:.2f} W{self.wins}/L{self.losses}")
        else:
            print(f"⚠️ [{self.asset_type.upper()}] Redis save failed — in-memory state preserved.")

    def _append_trade_to_redis(self, entry: dict):
        if not _redis_available:
            return
        existing = redis_get_json(self._redis_trades_key()) or []
        existing.append(entry)
        if len(existing) > REDIS_TRADES_MAX:
            existing = existing[-REDIS_TRADES_MAX:]
        redis_set_json(self._redis_trades_key(), existing)

    # ═════════════════════════════════════════════════════════════════════
    # DASHBOARD HELPERS
    # ═════════════════════════════════════════════════════════════════════

    def add_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.recent_logs.append(f"[{timestamp}] {message}")

    def update_dashboard(self):
        inv = self.inventory
        self.dashboard["outcome"] = self.market_outcome or "PENDING"
        if inv.yes_shares > 0 or inv.no_shares > 0:
            side = "YES" if inv.yes_shares >= inv.no_shares else "NO"
            sh = inv.shares(side)
            self.dashboard["status"] = f"{side} {sh:.0f} sh"

    def get_market_countdown(self) -> str:
        """Return the live market countdown from the absolute expiry timestamp.

        The old dashboard timer was only refreshed when a Polymarket WebSocket
        price event arrived.  During a quiet book that made the displayed clock
        freeze even though the market was still advancing.  The countdown must
        be derived from the clock, not from market-message frequency.
        """
        if not self.active_market or not self.active_market.get("expiry"):
            return "--:--"
        expiry = self.active_market["expiry"]
        now = datetime.now(timezone.utc)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        seconds_left = max(0, int((expiry - now).total_seconds()))
        mins, secs = divmod(seconds_left, 60)
        return f"{mins:02d}:{secs:02d}"

    def get_listener_countdown(self) -> str:
        if not self.active_market:
            return "--:--"
        seconds_left = self.market_seconds_left()
        activate = self.worker_config.listener_activate_secs
        if seconds_left <= activate:
            return "00:00"
        wait_seconds = seconds_left - activate
        mins, secs   = divmod(wait_seconds, 60)
        return f"{mins:02d}:{secs:02d}"

    def is_dry_run(self) -> bool:
        wc = self.worker_config
        if not wc.live_trading_enabled:
            return True
        return wc.dry_run

    def resolved_entry_fok_limit(self):
        """Legacy minimum entry (unused by momentum strategy)."""
        from utils.entry_limit import resolve_minimum_entry_price

        return resolve_minimum_entry_price(self.worker_config.entry_price)

    def resolved_momentum_max_entry(self):
        from utils.entry_limit import resolve_max_entry_price

        return resolve_max_entry_price(self.worker_config.momentum_max_entry_price)

    async def graceful_stop(self) -> None:
        """Stop loops without liquidating open positions."""
        self.exited = True
        for order_id, (side, size, price) in list(self._open_orders.items()):
            if order_id in ("dry-run", None):
                continue
            try:
                await self.poll_order_fill(order_id, size, price)
            except Exception as exc:
                exec_log("shutdown_order_reconcile", order_id=order_id, error=str(exc))

    def market_seconds_left(self) -> int:
        if not self.active_market:
            return 0
        remaining = self.active_market["expiry"] - datetime.now(timezone.utc)
        return max(0, int(remaining.total_seconds()))

    def in_late_entry_window(self) -> bool:
        wc = self.worker_config
        left = self.market_seconds_left()
        if wc.entry_window_seconds <= 0:
            return left > 0
        return 0 < left <= wc.entry_window_seconds

    def _token_id(self, side: str) -> Optional[str]:
        if not self.active_market:
            return None
        return str(
            self.active_market["yes_id"] if side == "YES" else self.active_market["no_id"]
        )

    def refresh_order_book(self, side: str, *, max_age_seconds: float = 1.5) -> None:
        token_id = self._token_id(side)
        if not token_id:
            return
        now = t.monotonic()
        cached = self._book_cache.get(side)
        if cached and now - cached[0] < max_age_seconds:
            return
        try:
            book = self.account.client.get_order_book(token_id)
            self._book_cache[side] = (now, book)
        except Exception as exc:
            exec_log("order_book_fetch_failed", side=side, error=str(exc))

    @staticmethod
    def _book_best_prices(book: Any) -> Tuple[float, float]:
        """Return (best_ask, best_bid) from a CLOB order book snapshot."""
        from utils.order_book_math import best_ask_from_levels, best_bid_from_levels

        asks = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
        bids = book.get("bids") if isinstance(book, dict) else getattr(book, "bids", None)
        return best_ask_from_levels(asks), best_bid_from_levels(bids)

    def refresh_public_quotes(self, *, min_interval_seconds: float = 0.75) -> None:
        """Pull fresh top-of-book prices for dashboard + strategy (REST fallback)."""
        if not self.active_market:
            return
        now = t.monotonic()
        if now - self._last_public_quote_refresh < min_interval_seconds:
            return
        self._last_public_quote_refresh = now
        for side in ("YES", "NO"):
            self.refresh_order_book(side, max_age_seconds=0.0)
            cached = self._book_cache.get(side)
            if not cached:
                continue
            book = cached[1]
            ask, bid = self._book_best_prices(book)
            from utils.order_book_math import best_ask_size_from_levels

            asks = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
            ask_sz = best_ask_size_from_levels(asks)
            if ask > 0:
                self.prices[side] = normalize_polymarket_price(ask)
                self._touch_quote(side, ask_size=ask_sz if ask_sz > 0 else 0.0)
            if bid > 0:
                self.bids[side] = normalize_polymarket_price(bid)

    def executable_buy(self, side: str, size: float) -> Optional[Tuple[float, float]]:
        """Volume-weighted ask walk for ``size`` shares (full size required)."""
        return self.executable_buy_partial(side, size, require_full_size=True)

    def executable_buy_partial(
        self,
        side: str,
        size: float,
        *,
        require_full_size: bool = False,
    ) -> Optional[Tuple[float, float]]:
        """Volume-weighted ask walk; partial fills allowed when require_full_size=False."""
        self.refresh_order_book(side)
        cached = self._book_cache.get(side)
        if cached:
            book = cached[1]
            asks = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
            exe = executable_buy_from_asks(
                asks, size, require_full_size=require_full_size,
            )
            if exe:
                return exe
        ask = float(self.best_ask(side))
        fb = best_level_fallback(side, ask, size, self.best_ask_size(side))
        if fb and not require_full_size:
            return fb
        if fb and require_full_size and fb[1] >= size - 1e-6:
            return fb
        return None if require_full_size else fb

    def executable_sell(self, side: str, size: float) -> Optional[Tuple[float, float]]:
        self.refresh_order_book(side)
        cached = self._book_cache.get(side)
        if cached:
            book = cached[1]
            bids = book.get("bids") if isinstance(book, dict) else getattr(book, "bids", None)
            exe = executable_sell_from_bids(bids, size, require_full_size=False)
            if exe:
                return exe
        bid = float(self.best_bid(side))
        if bid <= 0:
            return None
        return bid, min(size, self.inventory.shares(side))

    async def resolve_fill_price(
        self, order_id: str, requested: float, limit_price: float,
    ) -> float:
        _size, px = await self.poll_order_fill(order_id, requested, limit_price)
        return px if _size > 0 else round(limit_price, 4)

    def best_bid(self, side: str) -> float:
        """Return the live best bid for a token."""
        return self.bids.get(side, 0.0)

    def best_ask(self, side: str) -> float:
        """Return the live executable best ask for a token.

        ``prices`` is populated from Polymarket ``best_ask`` updates in the
        market WebSocket listener. Spread-capture BUY decisions use this side
        of the book rather than the bid.
        """
        return self.prices.get(side, 0.0)

    def best_ask_size(self, side: str) -> float:
        """Best-level ask size when the feed exposes it; else order-size fallback."""
        sz = float(self.ask_sizes.get(side, 0.0))
        if sz > 0:
            return sz
        return float(self.worker_config.max_order_size)

    def max_quote_age_seconds(self) -> Optional[float]:
        now = t.monotonic()
        ages = [
            now - self._quote_updated_at[s]
            for s in ("YES", "NO")
            if self._quote_updated_at.get(s, 0.0) > 0
        ]
        return max(ages) if ages else None

    def canonical_market_id(self) -> Optional[str]:
        if not self.active_market:
            return None
        cid = self.active_market.get("condition_id")
        if cid:
            return str(cid)
        slug = self.active_market.get("slug")
        return str(slug) if slug else None

    def _touch_quote(self, side: str, *, ask_size: float = 0.0) -> None:
        self._quote_updated_at[side] = t.monotonic()
        if ask_size > 0:
            self.ask_sizes[side] = float(ask_size)

    def ask_history_for_knife(self, side: str) -> list[tuple[float, float]]:
        """Wall-clock ask samples for falling-knife filter."""
        return list(self._ask_history.get(side.upper(), ()))

    def record_fill_idempotent(
        self, order_id: Optional[str], side: str, qty: float, px: float,
    ) -> bool:
        """Return False when this fill was already applied (duplicate event)."""
        if not order_id or order_id in ("dry-run",):
            return True
        key = f"{order_id}:{side}:{qty:.6f}:{px:.6f}"
        if key in self._processed_fill_order_ids:
            return False
        self._processed_fill_order_ids.add(key)
        return True

    def _record_ask_snapshot(self, side: str, price: float) -> None:
        if price <= 0:
            return
        self._touch_quote(side)
        self._ws_market_data_stale = False
        now = t.time()
        hist = self._ask_history[side]
        hist.append((now, float(price)))
        low = self._knife_low.get(side, 0.0)
        self._knife_low[side] = price if low <= 0 else min(low, price)
        # Keep enough recent data for the configured windows but do not let a
        # long-lived market accumulate stale observations.
        wc = self.worker_config
        hist_sec = 45.0
        if wc.inventory_accumulation_enabled:
            hist_sec = max(
                45.0,
                float(getattr(wc, "momentum_lookback_5s", 5)) + 15.0,
            )
        cutoff = now - max(10.0, wc.gabagool_falling_knife_window_seconds * 3.0, hist_sec)
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def is_falling_knife(self, side: str, ask: float) -> bool:
        """Return True when a fresh entry is trying to catch a fast selloff.

        This gates only the initial single-leg entry. Existing inventory is
        never hedged or averaged into; it is held until stop-loss or settlement.
        """
        cfg = self.worker_config
        if not cfg.gabagool_falling_knife_enabled or ask <= 0:
            return False
        now = t.monotonic()
        hist = list(self._ask_history.get(side, ()))
        if len(hist) < 3:
            return False

        window = cfg.gabagool_falling_knife_window_seconds
        fast_window = cfg.gabagool_falling_knife_fast_window_seconds
        def price_at_or_before(seconds: float) -> Optional[float]:
            target = now - seconds
            candidates = [px for ts, px in hist if ts <= target]
            return candidates[-1] if candidates else None

        old = price_at_or_before(window)
        fast_old = price_at_or_before(fast_window)
        window_drop = (old - ask) if old is not None else 0.0
        fast_drop = (fast_old - ask) if fast_old is not None else 0.0

        recent = [px for ts, px in hist if ts >= now - min(window, 2.0)] + [ask]
        down_moves = sum(1 for a, b in zip(recent, recent[1:]) if b < a - 0.002)
        monotone = len(recent) >= 4 and down_moves >= max(2, len(recent) - 2)

        # Local low / recovery latch: after a knife is detected, require the
        # price to recover before allowing another attempt. This prevents the
        # bot from buying 46c, then immediately buying again at 45c/44c.
        low = self._knife_low.get(side, ask) or ask
        blocked = now < self._knife_block_until.get(side, 0.0)
        recovered = ask >= low + (cfg.gabagool_falling_knife_recovery_cents / 100.0)
        if self._knife_latched.get(side, False) and not recovered:
            return True
        if recovered:
            self._knife_block_until[side] = 0.0
            self._knife_latched[side] = False
            self._knife_low[side] = ask
        elif blocked:
            return True

        local_fast = fast_drop >= cfg.gabagool_falling_knife_fast_drop
        local_sustained = window_drop >= cfg.gabagool_falling_knife_min_drop and monotone
        if not (local_fast or local_sustained):
            return False

        # Optional Binance confirmation. A local selloff is sufficient to
        # block; Binance simply strengthens the diagnosis and is not required
        # for the safety gate to work.
        binance_confirmed = False
        try:
            from bot import BinanceDepthSignal
            symbol = self.asset_type.upper()
            signal = BinanceDepthSignal._instances.get(symbol)
            if signal is not None and signal.is_fresh:
                against = -signal.imbalance if side == "YES" else signal.imbalance
                against_momentum = -signal.momentum if side == "YES" else signal.momentum
                binance_confirmed = (
                    against >= cfg.gabagool_falling_knife_binance_confirm
                    or against_momentum >= cfg.gabagool_falling_knife_binance_confirm
                )
        except Exception:
            pass

        # Fast drops are dangerous on their own. Slower drops must look like a
        # genuine sequence of lower asks, and Binance confirmation makes the
        # block more decisive when available.
        dangerous = local_fast or (local_sustained and (binance_confirmed or monotone))
        if dangerous:
            self._knife_block_until[side] = now + cfg.gabagool_falling_knife_cooldown_seconds
            self._knife_latched[side] = True
            self._knife_low[side] = min(low, ask)
            self.dashboard["falling_knife_blocked"] = True
            self.dashboard["falling_knife_side"] = side
            self.dashboard["falling_knife_drop_c"] = round(max(window_drop, fast_drop) * 100.0, 2)
            print(
                f"🛑 [FALLING KNIFE] {self.asset_type.upper()} {self.window_slug} | "
                f"block NEW {side} entry @ {ask:.3f} | "
                f"drop={max(window_drop, fast_drop)*100:.1f}c/{window:.1f}s "
                f"binance={'CONFIRMED' if binance_confirmed else 'local-only'}"
            )
            return True
        return False

    def can_enter(self) -> bool:
        wc = self.worker_config
        return entry_window_ok(
            self.market_seconds_left(),
            entry_seconds_left=wc.entry_seconds_left,
            min_entry_seconds_left=wc.min_entry_seconds_left,
        )

    def _track_order(self, order_id: Optional[str], side: str, size: float, price: float) -> None:
        if order_id and order_id not in ("dry-run", None):
            self._open_orders[order_id] = (side, size, price)

    def _untrack_order(self, order_id: Optional[str]) -> None:
        if order_id:
            self._open_orders.pop(order_id, None)

    async def cancel_order_confirmed(
        self, order_id: str, requested: float, limit_price: float,
    ) -> Tuple[float, float]:
        """Cancel an order and return its final cumulative matched quantity.

        Never drop local tracking unless the venue confirms a terminal state.
        A timeout is intentionally surfaced to callers so they cannot replace
        an order whose cancellation status is unknown.
        """
        if order_id in ("dry-run", None):
            return 0.0, round(limit_price, 4)

        self._try_cancel_order(order_id)
        deadline = t.time() + 5.0
        last_size = 0.0
        last_price = round(limit_price, 4)
        while t.time() < deadline:
            await asyncio.sleep(0.35)
            last_size, last_price = await self.poll_order_fill(
                order_id, requested, limit_price,
            )
            try:
                info = self.account.get_order_status(order_id)
                if isinstance(info, str):
                    info = json.loads(info)
                if isinstance(info, dict):
                    status = str(info.get("status", "")).lower()
                    if status in ("cancelled", "canceled", "filled", "matched", "closed", "expired"):
                        self._untrack_order(order_id)
                        return last_size, last_price
            except Exception:
                # Status uncertainty is not evidence that the order is gone.
                pass
        exec_log("cancel_unconfirmed", order_id=order_id, requested=requested,
                 cumulative_filled=last_size)
        raise TimeoutError(f"order cancellation not confirmed: {order_id}")

    async def cancel_all_open_orders(self) -> None:
        for order_id, (side, size, price) in list(self._open_orders.items()):
            print(
                f"🚫 [GABAGOOL CLEANUP] Cancelling open {side} order {order_id} "
                f"({size:.2f}@{round(price*100)}c)"
            )
            try:
                await self.cancel_order_confirmed(order_id, size, price)
            except TimeoutError:
                # Keep uncertain orders tracked. Caller must not settle/reset a
                # market while an order may still be able to fill.
                continue
        # Do not clear the dictionary wholesale: only confirmed terminal orders
        # are removed by cancel_order_confirmed().

    async def place_exit_sell(
        self,
        side: str,
        price: float,
        size: float,
        *,
        stop_loss_only: bool = False,
        exit_purpose: str = "stop_loss",
    ) -> Tuple[float, float]:
        """FOK sell for fixed take-profit or stop-loss exits only."""
        purpose = exit_purpose if not stop_loss_only else "stop_loss"
        if purpose not in ("stop_loss", "take_profit"):
            print("🛑 [SELL BLOCKED] exit sell rejected: unknown exit purpose")
            return 0.0, round(price, 4)
        ok, order_id, _filled = await self.place_order_raw(
            side, price, size, order_type="FOK", action="SELL",
            _position_exit_authorized=True,
        )
        if not ok or not order_id:
            return 0.0, round(price, 4)
        if order_id == "dry-run":
            return float(size), round(price, 4)
        fill_size, fill_price = await self.poll_order_fill(order_id, size, price)
        return fill_size, fill_price

    def entry_order_size(self, legs: List[str]) -> Optional[float]:
        """Pick order size (random in configured range) and clamp to headroom."""
        wc = self.worker_config
        if wc.random_order_size:
            target = random.uniform(wc.order_size_min, wc.order_size_max)
            target = round(target, 1)
        else:
            target = float(wc.order_size_max)
        per_leg: List[float] = []
        cap = max(wc.max_shares, wc.max_position_size)
        for side in legs:
            room = self.inventory.headroom(side, cap)
            if room < MIN_SHARES:
                return None
            per_leg.append(min(target, room, float(wc.max_order_size)))
        size = min(per_leg)
        if size < MIN_SHARES:
            return None
        return round(size, 4)

    def validate_order_size(self, side: str, size: float) -> bool:
        wc = self.worker_config
        inv = self.inventory
        # Reserve headroom for outstanding BUY orders as well as filled shares.
        # Deduplicate IDs because some engine orders are also in _open_orders.
        pending_by_id: Dict[str, Tuple[str, float, float]] = {}
        for oid, row in getattr(self, "_open_orders", {}).items():
            pending_by_id[oid] = row  # entry orders are BUY orders
        for oid, row in getattr(self, "_engine_orders", {}).items():
            if len(row) >= 4 and str(row[3]).upper() != "BUY":
                continue
            pending_by_id.setdefault(oid, row[:3])
        pending_buy = sum(
            max(0.0, float(row[1])) for row in pending_by_id.values()
            if str(row[0]).upper() == side
        )
        projected = inv.shares(side) + pending_buy + size
        if not math.isfinite(float(size)) or not math.isfinite(float(projected)):
            print(f"❌ [SIZE CHECK] {side} rejected: non-finite size or exposure")
            return False
        if size <= 0:
            print(f"❌ [SIZE CHECK] {side} rejected: size={size} <= 0")
            return False
        if size > wc.max_order_size + 1e-9:
            print(
                f"❌ [SIZE CHECK] {side} rejected: size={size} "
                f"> max_order_size={wc.max_order_size}"
            )
            exec_log(
                "order_size_rejected", side=side, size=size,
                reason="max_order_size", asset=self.asset_type, window=self.window_slug,
            )
            return False
        cap = max(wc.max_shares, wc.max_position_size)
        if projected > cap + 1e-9:
            print(
                f"❌ [SIZE CHECK] {side} rejected: projected inventory "
                f"{projected:.4f} > max_shares={wc.max_shares} "
                f"(current={inv.shares(side):.4f}, order={size:.4f})"
            )
            exec_log(
                "order_size_rejected", side=side, size=size,
                reason="max_shares", projected=projected,
                asset=self.asset_type, window=self.window_slug,
            )
            return False
        return True

    def pending_buy_quantities(self) -> Tuple[float, float]:
        """Outstanding BUY size per side (open + engine orders)."""
        pending_by_id: Dict[str, Tuple[str, float, float]] = {}
        for oid, row in getattr(self, "_open_orders", {}).items():
            pending_by_id[oid] = row
        for oid, row in getattr(self, "_engine_orders", {}).items():
            if len(row) >= 4 and str(row[3]).upper() != "BUY":
                continue
            pending_by_id.setdefault(oid, row[:3])
        py = sum(
            max(0.0, float(row[1]))
            for row in pending_by_id.values()
            if str(row[0]).upper() == "YES"
        )
        pn = sum(
            max(0.0, float(row[1]))
            for row in pending_by_id.values()
            if str(row[0]).upper() == "NO"
        )
        return py, pn

    def effective_inventory(self) -> EffectiveInventory:
        wc = self.worker_config
        py, pn = (0.0, 0.0)
        if wc.account_for_pending_buy_orders:
            py, pn = self.pending_buy_quantities()
        inv = self.inventory
        return EffectiveInventory(
            yes_confirmed=float(inv.yes_shares),
            no_confirmed=float(inv.no_shares),
            pending_yes=py,
            pending_no=pn,
        )

    def plan_accumulation_buy(
        self,
        side: str,
        requested_qty: float,
        *,
        purpose: str,
        allow_first_leg_entry: bool = False,
    ) -> BuySizePlan:
        wc = self.worker_config
        snap = InventorySnapshot.from_ledger(self.inventory, fee_rate=wc.spread_capture_fee_rate)
        eff = self.effective_inventory()
        market = self.active_market.get("question", "") if self.active_market else ""
        plan = plan_single_leg_buy(
            snap=snap,
            eff=eff,
            side=side,
            requested_qty=float(requested_qty),
            min_order_size=float(wc.min_order_size),
            max_share_imbalance=float(wc.max_share_imbalance),
            max_unmatched_shares=float(wc.max_unmatched_shares),
            balance_inventory_before_buy=bool(wc.balance_inventory_before_buy),
            allow_unmatched_accumulation=bool(wc.allow_unmatched_accumulation),
            purpose=purpose,
            allow_first_leg_entry=allow_first_leg_entry,
            max_shares_per_side=max(float(wc.max_shares), float(wc.max_position_size)),
        )
        log_buy_plan(plan, market=market, purpose=purpose)
        if plan.approval == BuyApproval.REJECTED:
            self.dashboard["entry_block_reason"] = plan.reason
        return plan

    def log_locked_market_skip(self, yes_bid: float, no_bid: float) -> None:
        if self._locked_skip_logged:
            return
        self._locked_skip_logged = True
        yes_c = round(yes_bid * 100) if yes_bid > 0 else 0
        no_c = round(no_bid * 100) if no_bid > 0 else 0
        print(
            f"⏭️  [LOCKED] {self.asset_type.upper()} {self.window_slug} | "
            f"Skipping market — YES={yes_c}c NO={no_c}c "
            f"(resolved at {round(LOCKED_LOW*100)}c or {round(LOCKED_HIGH*100)}c)"
        )

    def _update_momentum_dashboard(self) -> None:
        up_bid = self.bids.get("YES", 0.0)
        down_bid = self.bids.get("NO", 0.0)
        combined = (up_bid + down_bid) if up_bid > 0 and down_bid > 0 else 0.0
        inv = self.inventory
        self.dashboard["combined_bid_c"] = round(combined * 100)
        self.dashboard["yes_bid_c"] = round(up_bid * 100)
        self.dashboard["no_bid_c"] = round(down_bid * 100)
        self.dashboard["yes_shares"] = round(inv.yes_shares, 2)
        self.dashboard["no_shares"] = round(inv.no_shares, 2)
        yes_avg = inv.avg_cost("YES")
        no_avg = inv.avg_cost("NO")
        self.dashboard["yes_avg_price"] = round(yes_avg, 4)
        self.dashboard["no_avg_price"] = round(no_avg, 4)
        self.dashboard["yes_avg_price_c"] = round(yes_avg * 100, 1) if inv.yes_shares > 0 else 0.0
        self.dashboard["no_avg_price_c"] = round(no_avg * 100, 1) if inv.no_shares > 0 else 0.0
        if inv.yes_shares > 0 and inv.no_shares > 0:
            self.dashboard["pair_avg_price_c"] = round((yes_avg + no_avg) * 100, 1)
        else:
            self.dashboard["pair_avg_price_c"] = 0.0
        if inv.yes_shares > 0 or inv.no_shares > 0:
            side = "YES" if inv.yes_shares >= inv.no_shares else "NO"
            sh = inv.shares(side)
            self.dashboard["status"] = f"{side} {sh:.0f} sh"
        wc = self.worker_config
        self.dashboard["momentum_entry_threshold_c"] = round(wc.momentum_entry_threshold * 100)
        self.dashboard["stop_loss_pct"] = wc.stop_loss_pct
        self.dashboard["price_99_entry_window_seconds"] = wc.price_99_entry_window_seconds
        self.dashboard["gabagool_initial_entry_threshold_c"] = round(wc.gabagool_initial_entry_threshold * 100, 1)
        self.dashboard["gabagool_max_pair_cost_c"] = round(wc.gabagool_max_pair_cost * 100, 1)
        self.dashboard["gabagool_min_profit_margin_c"] = round(wc.gabagool_min_profit_margin * 100, 1)
        self.dashboard["gabagool_max_unpaired_shares"] = wc.gabagool_max_unpaired_shares
        self.dashboard["gabagool_min_time_to_resolution"] = wc.gabagool_min_time_to_resolution
        self.dashboard["gabagool_inventory_soft_limit"] = wc.gabagool_inventory_soft_limit
        self.dashboard["gabagool_endgame_seconds"] = wc.gabagool_endgame_seconds
        self.dashboard["gabagool_endgame_max_unpaired"] = wc.gabagool_endgame_max_unpaired
        self.dashboard["entry_count"] = self.entry_count
        for side in ("YES", "NO"):
            shares = inv.shares(side)
            if shares > 0:
                entry = inv.avg_cost(side)
                self.dashboard[f"{side.lower()}_stop_c"] = round(
                    stop_loss_price(entry, wc.stop_loss_pct) * 100, 1,
                )
            else:
                self.dashboard[f"{side.lower()}_stop_c"] = 0.0

    async def place_strategy_fok_buy(
        self, side: str, price: float, size: float, *, purpose: str = "directional_entry",
    ) -> Tuple[Optional[str], float]:
        if not self.validate_order_size(side, float(size)):
            exec_log("strategy_buy_blocked", side=side, reason="invalid_size", purpose=purpose)
            return None, 0.0
        wc = self.worker_config
        if float(size) < wc.min_order_size - 1e-9:
            return None, 0.0
        ok, order_id, filled = await self.place_order_raw(
            side, price, size, order_type="FOK", action="BUY",
            _strategy_buy_authorized=True,
        )
        if not ok:
            return None, 0.0
        if order_id:
            self._track_order(order_id, side, float(size), price)
        if filled and order_id:
            self._untrack_order(order_id)
            return order_id, float(size)
        return order_id, 0.0

    async def place_strategy_fak_buy(
        self, side: str, price: float, size: float, *, purpose: str = "momentum_inventory",
    ) -> Tuple[Optional[str], float]:
        if not self.validate_order_size(side, float(size)):
            exec_log("strategy_buy_blocked", side=side, reason="invalid_size", purpose=purpose)
            return None, 0.0
        wc = self.worker_config
        if float(size) < wc.min_order_size - 1e-9:
            return None, 0.0
        ot = str(getattr(wc, "inventory_execution_type", "") or wc.order_type or "FAK").upper()
        if ot not in ("FAK", "FOK"):
            ot = "FAK"
        ok, order_id, filled = await self.place_order_raw(
            side, price, size, order_type=ot, action="BUY",
            _strategy_buy_authorized=True,
            _inventory_accumulation_authorized=True,
        )
        if not ok:
            return None, 0.0
        if order_id:
            self._track_order(order_id, side, float(size), price)
        if filled and order_id:
            self._untrack_order(order_id)
            return order_id, float(filled) if filled else float(size)
        return order_id, 0.0

    place_accumulation_fok = place_strategy_fok_buy
    place_late_entry_fok = place_strategy_fok_buy
    place_binance_fok_entry = place_strategy_fok_buy
    place_inventory_entry_fok = place_strategy_fok_buy
    place_spread_entry_fok = place_strategy_fok_buy

    async def place_recovery_sell(
        self, side: str, price: float, size: float,
    ) -> Tuple[float, float]:
        print("🛑 [SELL BLOCKED] recovery sells are disabled (stop-loss FOK only)")
        return 0.0, round(price, 4)

    @staticmethod
    def _extract_order_fill_price(info: Dict[str, Any], limit_price: float) -> float:
        for key in ("avg_price", "average_price", "fill_price", "price"):
            raw = info.get(key)
            if raw is None:
                continue
            try:
                px = float(raw)
            except (TypeError, ValueError):
                continue
            if px > 1.0:
                px /= 100.0
            if px > 0:
                return round(px, 4)
        return round(limit_price, 4)

    async def poll_order_fill(
        self, order_id: str, requested: float, limit_price: float,
    ) -> Tuple[float, float]:
        """Return (filled_size, fill_price). fill_price falls back to limit_price."""
        try:
            info = self.account.get_order_status(order_id)
            if isinstance(info, str):
                info = json.loads(info)
            if isinstance(info, dict):
                matched = (
                    info.get("size_matched")
                    or info.get("sizeMatched")
                    or info.get("matched_size")
                    or 0
                )
                fill_size = min(float(matched), float(requested))
                if fill_size > 0:
                    fill_price = self._extract_order_fill_price(info, limit_price)
                    return fill_size, fill_price
        except Exception:
            pass
        return 0.0, round(limit_price, 4)

    def log_entry_trades(
        self,
        *,
        fills: Dict[str, Tuple[float, float]],
    ) -> None:
        """Persist one history row per capture with actual fill prices."""
        if not fills:
            return
        market_name = (
            self.active_market["question"] if self.active_market else "Unknown Market"
        )
        slug = (
            (self.active_market.get("slug") if self.active_market else None)
            or self.market_slug
            or self.asset_type
        )
        ts_ms = int(t.time() * 1000)

        for side, (fill_size, fill_price) in fills.items():
            append_trade_history({
                "asset":       self.asset_type.upper(),
                "window":      self.window_slug,
                "market":      market_name,
                "slug":        slug,
                "action":      "buy",
                "side":        side,
                "price":       round(fill_price, 4),
                "size":        round(fill_size, 4),
                "timestamp_ms": ts_ms,
            })

    def get_unrealized_pnl(self) -> Tuple[float, float]:
        """Mark-to-market for inventory (matched pairs @ $1, unpaired @ bid)."""
        inv = self.inventory
        if inv.yes_shares <= MIN_FILL_DELTA and inv.no_shares <= MIN_FILL_DELTA:
            return 0.0, 0.0

        matched = inv.matched_pairs
        yes_unpaired = max(0.0, inv.yes_shares - matched)
        no_unpaired = max(0.0, inv.no_shares - matched)
        yes_bid = self.bids.get("YES", 0.0)
        no_bid = self.bids.get("NO", 0.0)

        mark_value = (
            matched * 1.0
            + yes_unpaired * yes_bid
            + no_unpaired * no_bid
        )
        total_cost = inv.yes_cost + inv.no_cost
        unrealized = mark_value - total_cost
        roi_pct = (unrealized / total_cost * 100) if total_cost > 0 else 0.0
        return round(unrealized, 4), round(roi_pct, 2)

    def get_current_pnl(self) -> Tuple[float, float, str]:
        pnl_dollars, pnl_pct = self.get_unrealized_pnl()
        if pnl_dollars == 0.0 and pnl_pct == 0.0:
            inv = self.inventory
            if inv.yes_shares <= MIN_FILL_DELTA and inv.no_shares <= MIN_FILL_DELTA:
                return 0.0, 0.0, "white"
        color = "red" if pnl_dollars < 0 else "green"
        return pnl_dollars, pnl_pct, color

    def log_to_file(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} {message}\n")
        except Exception:
            pass

    def log_trade(self, side: str, price: float, action: str = "BUY",
                  size: float = 0.0, order_id: Optional[str] = None):
        mode_label  = "[DRY]" if self.is_dry_run() else "[LIVE]"
        market_name = self.active_market['question'] if self.active_market else "Unknown Market"
        slug        = ((self.active_market.get("slug") if self.active_market else None)
                       or self.market_slug or self.asset_type)
        price_cents = f"{round(price * 100)}c"
        msg = (f"{mode_label} {action}: {side} @ {price_cents} "
               f"size={size:.4f} | Market: {market_name}")
        self.log_to_file(msg)
        exec_log(
            "trade", mode=mode_label, action=action, side=side,
            price=price, size=size, order_id=order_id, market=market_name,
            window=self.window_slug,
        )
        if size > 0 and price > 0 and side in ("YES", "NO"):
            append_trade_history({
                "asset":    self.asset_type.upper(),
                "window":   self.window_slug,
                "market":   market_name,
                "slug":     slug,
                "action":   action,
                "side":     side,
                "price":    price,
                "size":     size,
            })

    def notify_confirmed_fill(
        self,
        *,
        side: str,
        action: str,
        quantity: float,
        price: float,
        order_id: Optional[str] = None,
        realized_pnl_delta: float = 0.0,
        count_win_loss: bool = False,
    ) -> None:
        """Record a confirmed fill in the canonical ledger and refresh dashboard P&L."""
        fee_rate = self.worker_config.estimated_fee_rate
        fee = float(quantity) * float(price) * fee_rate
        market_id = self.canonical_market_id() or ""
        canonical_ledger.record_fill(
            worker_key=self.worker_config.key,
            market_id=market_id,
            order_id=order_id,
            side=side,
            action=action,
            price=float(price),
            quantity=float(quantity),
            fee=fee,
            realized_pnl_delta=float(realized_pnl_delta),
            count_win_loss=count_win_loss,
        )
        self.sync_accounting_dashboard()

    def sync_accounting_dashboard(self) -> None:
        """Publish realized/unrealized/total P&L from inventory + canonical ledger."""
        inv = self.inventory
        unreal, unreal_pct = self.get_unrealized_pnl()
        canonical_ledger.backfill_worker_realized_if_missing(
            self.worker_config.key, inv.realized_pnl,
        )
        fee_rate = self.worker_config.estimated_fee_rate
        snap = InventorySnapshot.from_ledger(inv, fee_rate=fee_rate)
        combined = matched_combined_average(snap)
        inv_view = {
            "yes_shares": inv.yes_shares,
            "no_shares": inv.no_shares,
            "unpaired_yes": snap.unpaired_yes,
            "unpaired_no": snap.unpaired_no,
            "matched_pairs": inv.matched_pairs,
            "matched_combined_cost": round(combined, 4) if combined is not None else None,
        }
        view = canonical_ledger.build_accounting_view(
            worker_key=self.worker_config.key,
            unrealized_pnl=unreal,
            inventory_snapshot=inv_view,
        )
        summary = canonical_ledger.worker_summary(self.worker_config.key)
        self.dashboard.update(view)
        self.dashboard["unrealized_pnl_pct"] = unreal_pct
        self.cumulative_pnl = view["realized_pnl"]
        self.wins = summary.wins
        self.losses = summary.losses
        self.trade_count = summary.wins + summary.losses
        self._flush_pnl_stats_totals()

    def _flush_pnl_stats_totals(self) -> None:
        """Keep per-asset JSON/Redis totals aligned with the canonical ledger (no new trade row)."""
        file_path = asset_pnl_filename(self.asset_type, self.window_slug)
        total_trades = self.wins + self.losses
        win_rate_str = (
            f"{round((self.wins / total_trades) * 100, 2)}%" if total_trades > 0 else "0%"
        )
        self._save_stats_to_redis()
        default_data = {"total_pnl": 0.0, "wins": 0, "losses": 0, "win_rate": "0%", "trades": []}
        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = dict(default_data)
        except Exception:
            data = dict(default_data)
        data["total_pnl"] = round(self.cumulative_pnl, 4)
        data["wins"] = self.wins
        data["losses"] = self.losses
        data["win_rate"] = win_rate_str
        try:
            _atomic_write_json_file(file_path, data)
        except Exception as e:
            print(f"⚠️ PnL totals flush failed (non-fatal): {e}")
        try:
            canonical_ledger.sync_bot_pnl_snapshot()
        except Exception:
            pass

    def inventory_snapshot_for_api(self) -> dict:
        self.sync_accounting_dashboard()
        inv = self.inventory
        return {
            **self.dashboard,
            "inventory": inv.snapshot(),
        }

    # ── Market fetching ────────────────────────────────────────────────

    def fetch_target_market(self, url: str) -> bool:
        try:
            match = re.search(r"/(?:event|market)/([^/?#]+)", url)
            if not match:
                print(f"❌ Could not parse slug from URL: {url}")
                return False
            slug = match.group(1)
            if slug in self.processed_markets:
                print(f"⏭️  BLOCKED: Already processed market: {slug}")
                return False
            resp = requests.get(f"{GAMMA_URL}?slug={slug}").json()
            if not resp:
                print(f"❌ API returned no data for slug: {slug}")
                return False
            m            = resp[0]
            end_date_str = m.get("endDate")
            end_dt       = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now          = datetime.now(timezone.utc)
            if now >= end_dt:
                print(f"🛑 Market EXPIRED.")
                return False
            clob_ids = m.get("clobTokenIds")
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = None
            yes_id = str(clob_ids[0])
            no_id = str(clob_ids[1])
            if outcomes and len(outcomes) >= 2 and len(clob_ids) >= 2:
                labels = [str(o).strip().lower() for o in outcomes]
                up_idx = next(
                    (i for i, lab in enumerate(labels) if lab in ("up", "yes", "higher")),
                    None,
                )
                down_idx = next(
                    (i for i, lab in enumerate(labels) if lab in ("down", "no", "lower")),
                    None,
                )
                if up_idx is not None and down_idx is not None:
                    yes_id = str(clob_ids[up_idx])
                    no_id = str(clob_ids[down_idx])
            condition_id = m.get("conditionId") or m.get("condition_id") or slug
            start_ts = 0
            try:
                start_ts = int(str(slug).rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                start_ts = 0
            from utils.market_reference import infer_oracle_from_gamma

            self.active_market = {
                "question": m.get('question'),
                "yes_id":   yes_id,
                "no_id":    no_id,
                "expiry":   end_dt,
                "slug":     slug,
                "condition_id": str(condition_id),
                "start_ts": start_ts,
                "oracle_label": infer_oracle_from_gamma(m),
                "gamma": m,
            }
            self.market_slug = slug
            print(f"🎯 Market: {self.active_market['question']} | Slug: {slug}")
            return True
        except Exception as e:
            print(f"Market Fetch Error: {e}")
            return False

    # ── WebSocket price listener ───────────────────────────────────────

    async def price_listener(self):
        if not self.active_market:
            return
        activate = self.worker_config.listener_activate_secs
        from utils.graceful_shutdown import shutdown_requested

        while True:
            if shutdown_requested():
                break
            now          = datetime.now(timezone.utc)
            remaining    = self.active_market["expiry"] - now
            seconds_left = int(remaining.total_seconds())
            if seconds_left <= activate:
                break
            sleep_time = min(5, seconds_left - activate)
            print(f"⏳ Waiting for WS window: {seconds_left}s left...", end="\r")
            await asyncio.sleep(max(1, sleep_time))

        print(f"\n\n📡 Starting WebSocket listener in final {activate}s window...")

        async for ws in websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, close_timeout=10):
            try:
                sub_msg = {
                    "operation":  "subscribe",
                    "type":       "market",
                    "assets_ids": [self.active_market["yes_id"], self.active_market["no_id"]],
                }
                await ws.send(json.dumps(sub_msg))
                print(f"\n📡 Subscription active for: {self.active_market['question']}")

                async for message in ws:
                    if self.exited or shutdown_requested():
                        await ws.close()
                        break
                    now          = datetime.now(timezone.utc)
                    remaining    = self.active_market["expiry"] - now
                    seconds_left = int(remaining.total_seconds())
                    if seconds_left <= 0:
                        print("\n⌛ Market expired. Closing listener...")
                        await asyncio.sleep(1)
                        await self.on_market_expired()
                        await ws.close()
                        return
                    mins, secs = divmod(seconds_left, 60)
                    timer_str  = f"{mins:02d}:{secs:02d}"

                    data   = json.loads(message)
                    events = data if isinstance(data, list) else [data]
                    for ev in events:
                        e_type = ev.get("event_type")
                        if e_type in ["book", "initial_state"]:
                            for asset in ev.get("assets", []):
                                aid = asset.get("asset_id")
                                ask = float(asset.get("best_ask", 0))
                                bid = float(asset.get("best_bid", 0))
                                ask_sz = float(
                                    asset.get("ask_size")
                                    or asset.get("best_ask_size")
                                    or asset.get("size")
                                    or 0
                                )
                                if aid == self.active_market["yes_id"]:
                                    if ask > 0:
                                        self.prices["YES"] = normalize_polymarket_price(ask)
                                        self._touch_quote("YES", ask_size=ask_sz)
                                    if bid > 0:
                                        self.bids["YES"] = normalize_polymarket_price(bid)
                                elif aid == self.active_market["no_id"]:
                                    if ask > 0:
                                        self.prices["NO"] = normalize_polymarket_price(ask)
                                        self._touch_quote("NO", ask_size=ask_sz)
                                    if bid > 0:
                                        self.bids["NO"] = normalize_polymarket_price(bid)
                        elif e_type == "price_change":
                            for change in ev.get("price_changes", []):
                                aid = change.get("asset_id")
                                raw_ask = change.get("best_ask")
                                raw_bid = change.get("best_bid")
                                ask = float(raw_ask) if raw_ask not in (None, "") else 0.0
                                bid = float(raw_bid) if raw_bid not in (None, "") else 0.0
                                if aid == self.active_market["yes_id"]:
                                    if ask > 0:
                                        self.prices["YES"] = normalize_polymarket_price(ask)
                                        self._touch_quote("YES")
                                    if bid > 0:
                                        self.bids["YES"] = normalize_polymarket_price(bid)
                                elif aid == self.active_market["no_id"]:
                                    if ask > 0:
                                        self.prices["NO"] = normalize_polymarket_price(ask)
                                        self._touch_quote("NO")
                                    if bid > 0:
                                        self.bids["NO"] = normalize_polymarket_price(bid)

            except websockets.exceptions.ConnectionClosed:
                print("\n⚠️ Connection lost. Reconnecting in 3s...")
                self._book_cache.clear()
                self._ws_market_data_stale = True
                self._ask_history = {"YES": deque(maxlen=80), "NO": deque(maxlen=80)}
                self._knife_block_until = {"YES": 0.0, "NO": 0.0}
                self._knife_low = {"YES": 0.0, "NO": 0.0}
                self._knife_latched = {"YES": False, "NO": False}
                self.dashboard["entry_block_reason"] = "REJECT_STALE_ORDERBOOK"
                await self.cancel_all_open_orders()
                await asyncio.sleep(3)
                continue
            except Exception as e:
                print(f"\n❌ Listener Error: {e}")
                await asyncio.sleep(5)
                continue

    async def quote_poll_loop(self) -> None:
        """REST quotes + strategy ticks for the full market (including pre-WS wait)."""
        from utils.graceful_shutdown import shutdown_requested

        while self.active_market and not self.exited and not shutdown_requested():
            now = datetime.now(timezone.utc)
            if self.active_market["expiry"] - now <= timedelta(seconds=0):
                break
            remaining = self.active_market["expiry"] - now
            seconds_left = int(remaining.total_seconds())
            mins, secs = divmod(seconds_left, 60)
            timer_str = f"{mins:02d}:{secs:02d}"
            try:
                self.refresh_public_quotes(min_interval_seconds=0.5)
                if self.prices.get("YES", 0.0) > 0 or self.prices.get("NO", 0.0) > 0:
                    await self.check_logic(timer_str)
            except Exception as exc:
                exec_log("quote_poll_error", error=str(exc))
            await asyncio.sleep(1.0)

    # ═════════════════════════════════════════════════════════════════════
    # CORE ENTRY LOGIC — SINGLE-LEG DIRECTIONAL
    # ═════════════════════════════════════════════════════════════════════

    async def on_binance_tick(self) -> None:
        if not self.active_market:
            return
        if self._binance_eval_lock.locked():
            return
        async with self._binance_eval_lock:
            try:
                await self.strategy.on_binance_tick(self)
                self._update_economic_dashboard()
                self._update_momentum_dashboard()
                self.update_dashboard()
            except Exception as exc:
                print(f"⚠️ [BINANCE FALLBACK] {self.asset_type.upper()} {self.window_slug}: {exc}")

    async def check_logic(self, timer: str):
        """Polymarket tick handler — directional entry + stop-loss, dashboard."""
        y = normalize_polymarket_price(float(self.prices.get("YES", 0.0) or 0.0))
        n = normalize_polymarket_price(float(self.prices.get("NO", 0.0) or 0.0))
        if y > 0:
            self.prices["YES"] = y
        if n > 0:
            self.prices["NO"] = n
        y_c = round(y * 100) if y > 0 else 0
        n_c = round(n * 100) if n > 0 else 0
        self.dashboard["yes"]   = y_c
        self.dashboard["no"]    = n_c
        self.dashboard["timer"] = timer
        self._update_momentum_dashboard()
        self.update_dashboard()

        if y > 0:
            self._record_ask_snapshot("YES", y)
        if n > 0:
            self._record_ask_snapshot("NO", n)
        if y <= 0 and n <= 0:
            return
        self.dashboard.setdefault("falling_knife_blocked", False)
        self.dashboard["falling_knife_blocked"] = False
        self.dashboard["falling_knife_side"] = "-"

        # Keep the shared adverse-selection cancellation safeguard, but route
        # entries through the configured exact-99c strategy. The legacy three-
        # engine router uses bid/maker prices and could never satisfy the exact
        # ask-price rule consistently.
        self._update_economic_dashboard()
        await self.engine_router.cancel.run(self)
        await self.strategy.on_market_tick(self)
        self._update_economic_dashboard()
        self.update_dashboard()

    def binance_signal(self):
        symbol = self.asset_type.upper()
        return BinanceDepthSignal._instances.get(symbol)

    def min_fill_delta(self) -> float:
        return MIN_FILL_DELTA

    def _update_economic_dashboard(self) -> None:
        """Refresh dashboard economics from the current single-leg inventory.

        The legacy execution engines were removed, but these read-only position
        metrics are still used by the listener and Binance tick handlers. Keep
        the method available so a dashboard refresh cannot crash the listeners.
        This method only calculates and publishes metrics; it does not place,
        cancel, or otherwise alter orders.
        """
        pos = economic_position(self)
        self.dashboard.update({
            "position_delta": round(pos.position_delta, 4),
            "pnl_if_up": round(pos.pnl_if_yes, 4),
            "pnl_if_down": round(pos.pnl_if_no, 4),
            "mark_pnl": round(pos.mark_pnl, 4),
            "invested": round(pos.invested, 4),
            "locked_profit": round(pos.locked_profit, 4),
            "matched_pairs": round(pos.matched_pairs, 4),
            "engine_position_delta": round(pos.position_delta, 4),
        })

    async def cancel_engine_orders(self, side: Optional[str] = None) -> int:
        count = 0
        for oid, (oside, size, price, action, engine) in list(self._engine_orders.items()):
            if side is not None and oside != side:
                continue
            try:
                if oid != "dry-run":
                    await self.cancel_order_confirmed(oid, size, price)
                self._engine_orders.pop(oid, None)
                count += 1
            except Exception as exc:
                print(f"⚠️ [ENGINE CANCEL] {oid}: {exc}")
        return count

    # ── Order execution ────────────────────────────────────────────────

    async def place_order_raw(
        self,
        side: str,
        price: float,
        size: float,
        *,
        order_type: str = "FOK",
        action: str = "BUY",
        _stop_loss_authorized: bool = False,
        _position_exit_authorized: bool = False,
        _strategy_buy_authorized: bool = False,
        _inventory_accumulation_authorized: bool = False,
        _late_entry_authorized: bool = False,
        _binance_fok_authorized: bool = False,
        _inventory_authorized: bool = False,
        _spread_capture_authorized: bool = False,
        _recovery_authorized: bool = False,
    ) -> Tuple[bool, Optional[str], bool]:
        """Submit one order; return (accepted, order_id, filled_immediately)."""
        if not self.active_market:
            return False, None, False

        token_id = (self.active_market["yes_id"] if side == "YES"
                    else self.active_market["no_id"])
        if action.upper() == "SELL":
            clean_price = clamp_sell_price(price)
        else:
            clean_price = clamp_buy_price(price)

        exit_ok = _position_exit_authorized or _stop_loss_authorized
        if action.upper() == "SELL" and not exit_ok:
            exec_log("sell_blocked_non_authorized", side=side, price=clean_price,
                     size=size, window=self.window_slug)
            print(
                f"🛑 [SELL BLOCKED] {self.asset_type.upper()} {self.window_slug} | "
                f"SELL allowed only for take-profit or stop-loss FOK"
            )
            return False, None, False

        if action.upper() == "SELL" and order_type.upper() != "FOK":
            exec_log("sell_blocked_non_fok", side=side, order_type=order_type)
            print(f"🛑 [SELL BLOCKED] non-FOK sell rejected ({order_type})")
            return False, None, False

        if action.upper() == "BUY":
            buy_ok = (
                _strategy_buy_authorized
                or _inventory_accumulation_authorized
                or _late_entry_authorized
                or _binance_fok_authorized
                or _inventory_authorized
                or _spread_capture_authorized
            )
            if not buy_ok:
                exec_log("buy_blocked_non_strategy", side=side, price=clean_price,
                         size=size, window=self.window_slug)
                print("🛑 [BUY BLOCKED] only the directional strategy may submit BUY orders")
                return False, None, False
            if not self.validate_order_size(side, float(size)):
                return False, None, False
            if order_type.upper() != "FOK":
                exec_log("buy_blocked_non_fok", side=side, order_type=order_type)
                print(f"🛑 [BUY BLOCKED] non-FOK buy rejected ({order_type})")
                return False, None, False
            order_type = "FOK"

        if self.is_dry_run():
            print(f"\n🧪 [DRY] {action} {size} {side} @ {round(clean_price*100)}c "
                  f"mode={order_type}")
            exec_log("dry_run_order", action=action, side=side, price=clean_price, size=size,
                     order_type=order_type, window=self.window_slug)
            return True, "dry-run", False

        try:
            resp = self.account.create_and_post_order(
                action.upper(), clean_price, float(size), token_id, order_type=order_type,
            )
        except Exception as e:
            exec_log("order_failed", action=action, side=side, error=str(e), order_type=order_type)
            print(f"❌ Order placement failed: {e}")
            return False, None, False

        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id")
        elif isinstance(resp, str):
            try:
                parsed = json.loads(resp)
                order_id = parsed.get("orderID") or parsed.get("order_id")
            except Exception:
                pass

        filled = False
        if order_type.upper() == "FOK" and order_id:
            await asyncio.sleep(0.5)
            try:
                info = self.account.get_order_status(order_id)
                if isinstance(info, str):
                    info = json.loads(info)
                if isinstance(info, dict):
                    st = str(info.get("status", "")).lower()
                    filled = st in ("filled", "matched", "closed")
            except Exception:
                pass

        exec_log("order_submit", action=action, side=side, price=clean_price, size=size,
                 order_type=order_type, order_id=order_id, filled=filled)
        return True, order_id, filled


    def _try_cancel_order(self, order_id: str):
        try:
            result = self.account.cancel_order(order_id)
            print(f"🚫 Cancel sent for {order_id}: {result}")
            exec_log("cancel_sent", order_id=order_id, result=str(result)[:100])
        except Exception as e:
            print(f"⚠️ Cancel failed for {order_id}: {e}")
            exec_log("cancel_failed", order_id=order_id, error=str(e))

    # ── On-chain balance helpers (thin pass-throughs to AccountService) ────

    async def get_onchain_share_balance_async(self, token_id: str, retries: int = 3) -> float:
        return await self.account.get_onchain_share_balance_async(token_id, retries=retries)

    def get_onchain_share_balance(self, token_id: str, retries: int = 3) -> float:
        return self.account.get_onchain_share_balance(token_id, retries=retries)

    async def merge_shares(self, amount_to_merge: float):
        """Merge YES+NO shares back into pUSD on-chain for this worker's active market."""
        return await self.account.merge_shares(self.active_market, amount_to_merge)

    def get_position_snapshot(self) -> Optional[Dict]:
        """Live inventory row for the dashboard Positions tab."""
        inv = self.inventory
        if inv.yes_shares <= MIN_FILL_DELTA and inv.no_shares <= MIN_FILL_DELTA:
            return None
        slug = ((self.active_market.get("slug") if self.active_market else None)
                or self.market_slug or self.asset_type)
        market_name = ((self.active_market.get("question") if self.active_market else None)
                       or f"{self.asset_type.upper()} Up or Down")
        pnl_dollars, pnl_pct = self.get_unrealized_pnl()
        total_cost = inv.yes_cost + inv.no_cost
        yes_avg_c = round(inv.avg_cost("YES") * 100, 1) if inv.yes_shares > 0 else 0.0
        no_avg_c = round(inv.avg_cost("NO") * 100, 1) if inv.no_shares > 0 else 0.0
        yes_bid_c = round(self.bids.get("YES", 0.0) * 100, 1)
        no_bid_c = round(self.bids.get("NO", 0.0) * 100, 1)
        pair_avg_c = (
            round((inv.avg_cost("YES") + inv.avg_cost("NO")) * 100, 1)
            if inv.matched_pairs > 0 else 0.0
        )
        combined_bid_c = (
            round((self.bids.get("YES", 0.0) + self.bids.get("NO", 0.0)) * 100, 1)
            if yes_bid_c > 0 and no_bid_c > 0 else 0.0
        )

        # Display fields for Positions tab (entry → current market bid).
        has_yes = inv.yes_shares > MIN_FILL_DELTA
        has_no = inv.no_shares > MIN_FILL_DELTA
        if has_yes and not has_no:
            display_side = "YES"
            entry_price_cents = yes_avg_c
            current_price_cents = yes_bid_c
            entry_price = round(inv.avg_cost("YES"), 4)
            current_price = round(self.bids.get("YES", 0.0), 4)
        elif has_no and not has_yes:
            display_side = "NO"
            entry_price_cents = no_avg_c
            current_price_cents = no_bid_c
            entry_price = round(inv.avg_cost("NO"), 4)
            current_price = round(self.bids.get("NO", 0.0), 4)
        elif inv.matched_pairs > 0:
            display_side = "SPREAD"
            entry_price_cents = pair_avg_c
            current_price_cents = combined_bid_c
            entry_price = round(inv.avg_cost("YES") + inv.avg_cost("NO"), 4)
            current_price = round(
                (self.bids.get("YES", 0.0) + self.bids.get("NO", 0.0)), 4,
            )
        elif has_yes and has_no:
            if inv.yes_shares >= inv.no_shares:
                display_side = "YES"
                entry_price_cents = yes_avg_c
                current_price_cents = yes_bid_c
                entry_price = round(inv.avg_cost("YES"), 4)
                current_price = round(self.bids.get("YES", 0.0), 4)
            else:
                display_side = "NO"
                entry_price_cents = no_avg_c
                current_price_cents = no_bid_c
                entry_price = round(inv.avg_cost("NO"), 4)
                current_price = round(self.bids.get("NO", 0.0), 4)
        else:
            display_side = "GABAGOOL"
            entry_price_cents = 0.0
            current_price_cents = 0.0
            entry_price = 0.0
            current_price = 0.0

        return {
            "id":                  f"{self.asset_type}:{self.window_slug}:{slug}:momentum",
            "asset":               self.asset_type.upper(),
            "window":              self.window_slug,
            "market":              market_name,
            "slug":                slug,
            "side":                display_side,
            "strategy":            "directional_fok",
            "yes_shares":          round(inv.yes_shares, 4),
            "no_shares":           round(inv.no_shares, 4),
            "yes_avg_price_c":     yes_avg_c,
            "no_avg_price_c":      no_avg_c,
            "yes_bid_c":           yes_bid_c,
            "no_bid_c":            no_bid_c,
            "pair_avg_price_c":    pair_avg_c,
            "matched_pairs":       round(inv.matched_pairs, 4),
            "position_imbalance":  round(inv.imbalance, 4),
            "entry_price":         entry_price,
            "current_price":       current_price,
            "entry_price_cents":   entry_price_cents,
            "current_price_cents": current_price_cents,
            "roi_pct":             pnl_pct,
            "unrealized_pnl":      pnl_dollars,
            "size":                round(inv.yes_shares + inv.no_shares, 4),
            "size_usd":            round(total_cost, 2),
            "cashout_available":   False,
            "entry_count":         self.entry_count,
            "order_state":         self.order_state.name,
        }

    # ── State reset ────────────────────────────────────────────────────

    async def reset_state(self):
        self.trade_state     = TradeState.IDLE
        self.position_side   = None
        self.position_size   = 0.0
        self.entry_price     = 0.0
        self.entry_timestamp = None
        self.market_start_time = None
        self.market_outcome    = None
        self.final_yes_price   = 0.0
        self.final_no_price    = 0.0
        self.exited            = False
        self.market_slug       = None
        self.prices            = {"YES": 0.0, "NO": 0.0}
        self.bids              = {"YES": 0.0, "NO": 0.0}
        self._ask_history     = {"YES": deque(maxlen=80), "NO": deque(maxlen=80)}
        self._knife_block_until = {"YES": 0.0, "NO": 0.0}
        self._knife_low        = {"YES": 0.0, "NO": 0.0}
        self._knife_latched    = {"YES": False, "NO": False}
        self.dashboard["yes"]               = 0
        self.dashboard["no"]                = 0
        self.dashboard["timer"]             = "--:--"
        self.dashboard["listener"]          = "--:--"
        self.dashboard["outcome"]           = "PENDING"
        self.dashboard["bought_side"]       = ""
        self.dashboard["entry_price"]       = 0.0
        self.dashboard["status"]            = "WAITING"
        self.dashboard["profit"]            = 0.0
        self.dashboard["price_delta"]       = 0.0
        self.dashboard["signal_stale"]      = True
        self.dashboard["combined_bid_c"]    = 0
        self.dashboard["yes_bid_c"]         = 0
        self.dashboard["no_bid_c"]          = 0
        self.dashboard["yes_shares"]        = 0.0
        self.dashboard["no_shares"]         = 0.0
        self.dashboard["yes_avg_price"]     = 0.0
        self.dashboard["no_avg_price"]      = 0.0
        self.dashboard["yes_avg_price_c"]   = 0.0
        self.dashboard["no_avg_price_c"]    = 0.0
        self.dashboard["pair_avg_price_c"]  = 0.0
        self.dashboard["gabagool_inventory_soft_limit"] = self.worker_config.gabagool_inventory_soft_limit
        self.dashboard["gabagool_endgame_max_unpaired"] = self.worker_config.gabagool_endgame_max_unpaired
        self.dashboard["gabagool_falling_knife_enabled"] = self.worker_config.gabagool_falling_knife_enabled
        self.dashboard["gabagool_falling_knife_window_seconds"] = self.worker_config.gabagool_falling_knife_window_seconds
        self.dashboard["gabagool_falling_knife_min_drop_c"] = self.worker_config.gabagool_falling_knife_min_drop * 100.0
        self.dashboard["falling_knife_blocked"] = False
        self.dashboard["falling_knife_side"] = "-"
        self.dashboard["falling_knife_drop_c"] = 0.0
        self.dashboard["gabagool_initial_entry_threshold_c"] = round(self.worker_config.gabagool_initial_entry_threshold * 100, 1)
        self.dashboard["gabagool_max_pair_cost_c"] = round(self.worker_config.gabagool_max_pair_cost * 100, 1)
        self.dashboard["gabagool_min_profit_margin_c"] = round(self.worker_config.gabagool_min_profit_margin * 100, 1)
        self.dashboard["entry_count"]       = 0
        self.order_state = OrderState.IDLE
        self.inventory.reset()
        self.entry_count = 0
        self._open_orders.clear()
        self._engine_orders.clear()
        # _engine_fills was removed with the legacy execution engines; do not
        # reference it during reset. Keep reset compatible with cancellation-only routing.
        self._locked_skip_logged = False
        self._book_cache.clear()
        self._market_entry_guard.clear()
        self.recent_logs.clear()
        self.update_dashboard()
        print("\n♻️ Full state reset after trade/exit. Ready for next market.")

    # ── PnL logging ────────────────────────────────────────────────────

    def log_pnl(self, outcome_type: str, pnl_amount: float, details: dict):
        file_path       = asset_pnl_filename(self.asset_type, self.window_slug)
        slug            = ((self.active_market.get("slug") if self.active_market else None)
                           or self.market_slug or "unknown")
        market_question = ((self.active_market.get("question", "Unknown")
                            if self.active_market else "Unknown Market"))

        # ── Validation: reject corrupted PnL values outright ─────────────
        # A NaN/Inf/None pnl_amount (e.g. from a divide-by-zero upstream, or
        # a malformed fill response) must never be allowed to corrupt
        # cumulative_pnl or the persisted history — once a bad value is
        # added in, every later point is wrong forever.
        if not _is_finite_number(pnl_amount):
            print(f"❌ [{self.asset_type.upper()}] log_pnl rejected non-finite pnl_amount "
                  f"({pnl_amount!r}) for market {slug} — trade NOT recorded.")
            return

        # ── Per-round dedup guard ──────────────────────────────────────────
        # "Only one valid snapshot per market round": if this exact slug was
        # already logged (e.g. a retried exit path firing twice), skip the
        # second call entirely rather than double-counting the trade.
        round_key = f"{self.asset_type}:{self.window_slug}:{slug}"
        exit_oid = details.get("order_id") if isinstance(details, dict) else None
        if outcome_type in ("STOP_LOSS", "TAKE_PROFIT") and exit_oid:
            if str(exit_oid) in self._logged_exit_order_ids:
                print(f"ℹ️ [{self.asset_type.upper()}] log_pnl skipped duplicate exit "
                      f"order {exit_oid}.")
                return
            self._logged_exit_order_ids.add(str(exit_oid))
        elif slug != "unknown" and slug == self._last_logged_slug:
            print(f"ℹ️ [{self.asset_type.upper()}] log_pnl skipped duplicate call for "
                  f"already-logged market {slug}.")
            return

        if outcome_type in ("GABAGOOL_SETTLE", "STOP_LOSS", "TAKE_PROFIT"):
            if outcome_type == "GABAGOOL_SETTLE":
                market_id = self.canonical_market_id() or ""
                canonical_ledger.record_fill(
                    worker_key=self.worker_config.key,
                    market_id=market_id,
                    order_id=f"settle:{market_id}",
                    side="SETTLE",
                    action="SETTLE",
                    price=1.0,
                    quantity=1.0,
                    fee=0.0,
                    realized_pnl_delta=float(pnl_amount),
                    count_win_loss=True,
                )
            summary = canonical_ledger.worker_summary(self.worker_config.key)
            self.cumulative_pnl = round(summary.realized_pnl, 4)
            self.wins = summary.wins
            self.losses = summary.losses
        else:
            self.cumulative_pnl = round(self.cumulative_pnl + pnl_amount, 4)
            if pnl_amount > 0:
                self.wins += 1
            else:
                self.losses += 1
        self.trade_count = self.wins + self.losses
        total_trades     = self.trade_count
        win_rate_str     = (f"{round((self.wins / total_trades) * 100, 2)}%"
                            if total_trades > 0 else "0%")

        duration = round(t.time() - (self.entry_timestamp or t.time()), 2)
        entry = {
            "timestamp":      datetime.now(TRADING_TZ).strftime(_TS_PRIMARY),
            "market":         market_question,
            "slug":           slug,
            "type":           outcome_type,
            "pnl":            round(pnl_amount, 4),
            "cumulative_pnl": self.cumulative_pnl,
            "details":        {**details, "duration_seconds": duration},
        }

        self._save_stats_to_redis()
        self._append_trade_to_redis(entry)
        self._last_logged_slug = slug

        # ── Live portfolio history snapshot ──────────────────────────────
        # Push a portfolio-total point to emiliano:portfolio:history right
        # now so the chart updates the moment this trade closes, without
        # waiting for any background flush interval.
        #
        # IMPORTANT (this was the root cause of the chart spikes): this MUST
        # be the TOTAL PnL across every asset, never self.cumulative_pnl for
        # this asset alone. Using a single asset's own PnL here produced a
        # sharp spike/drop on every trade close, because that value would be
        # written right next to genuinely correct cross-asset totals from
        # the 60-s background loop and the startup backfill.
        portfolio_history_snapshot(_portfolio_total_pnl(), round_key=round_key)

        default_data = {"total_pnl": 0.0, "wins": 0, "losses": 0,
                        "win_rate": "0%", "trades": []}
        try:
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    data = json.load(f)
            else:
                data = default_data
        except Exception:
            data = default_data

        data["total_pnl"] = self.cumulative_pnl
        data["wins"]      = self.wins
        data["losses"]    = self.losses
        data["win_rate"]  = win_rate_str
        data["trades"].append(entry)

        try:
            _atomic_write_json_file(file_path, data)
        except Exception as e:
            print(f"⚠️ Local JSON write failed (non-fatal, Redis has the data): {e}")

    # ── Dashboard data ─────────────────────────────────────────────────

    def get_dashboard_data(self) -> dict:
        from utils.strategy_display import enrich_dashboard_payload

        self.sync_accounting_dashboard()
        pnl_dollars, pnl_pct, _ = self.get_current_pnl()
        inv = self.inventory
        wc = self.worker_config
        side = str(self.dashboard.get("position_side") or "").upper().strip()
        if side not in ("YES", "NO"):
            side = ""
        pos_size = float(self.dashboard.get("position_size") or 0.0)
        if pos_size <= 0 and side:
            pos_size = inv.shares(side)

        if side and pos_size > 0:
            position_text = f"{side} {pos_size:.1f} sh"
        else:
            position_text = "-"

        entry_status = self.dashboard.get("entry_order_status", "IDLE")
        exit_status = self.dashboard.get("exit_order_status", "IDLE")
        if self.order_state == OrderState.PENDING:
            if entry_status == "IDLE":
                entry_status = "SUBMITTED"

        market_start_iso = None
        market_end_iso = None
        if self.active_market and self.active_market.get("expiry"):
            expiry = self.active_market["expiry"]
            start = expiry - timedelta(seconds=self._interval_seconds())
            market_end_iso = expiry.isoformat()
            market_start_iso = start.isoformat()

        schedule_ok = is_trading_allowed()
        try:
            self.refresh_public_quotes()
        except Exception:
            pass

        max_res = self.resolved_momentum_max_entry()
        trade_phase = self.dashboard.get("strategy_state", "idle")
        yes_avg = inv.avg_cost("YES") if inv.yes_shares > 0 else 0.0
        no_avg = inv.avg_cost("NO") if inv.no_shares > 0 else 0.0
        fee_rate = wc.estimated_fee_rate
        inv_snap = InventorySnapshot.from_ledger(inv, fee_rate=fee_rate)
        combined = matched_combined_average(inv_snap)
        inventory_mode = bool(wc.inventory_accumulation_enabled) and not bool(
            wc.momentum_strategy_enabled,
        )
        if inventory_mode:
            strategy_label = "Inventory accumulation"
            strategy_name = "Pair accumulation"
            strategy_key = "inventory_pair_accumulation"
        else:
            strategy_label = "BTC/ETH Momentum"
            strategy_name = "BTC/ETH Momentum"
            strategy_key = "momentum_directional"
        payload = {
            "asset": self.asset_type.upper(),
            "window": self.window_slug,
            "market_id": self.canonical_market_id() or "",
            "strategy": strategy_key,
            "strategy_label": strategy_label,
            "strategy_name": strategy_name,
            "inventory_accumulation_enabled": inventory_mode,
            "yes_spent_usd": round(inv.yes_cost, 4),
            "no_spent_usd": round(inv.no_cost, 4),
            "total_invested_usd": round(inv.yes_cost + inv.no_cost, 4),
            "matched_combined_cost": round(combined, 4) if combined is not None else None,
            "matched_combined_cost_c": (
                round(combined * 100, 2) if combined is not None else 0.0
            ),
            "target_combined_pair_cost_c": round(
                float(wc.target_combined_pair_cost or wc.combined_cost_target) * 100, 2,
            ),
            "inventory_next_action": self.dashboard.get("inventory_next_action", ""),
            "inventory_action_reason": self.dashboard.get("inventory_action_reason", ""),
            "accumulation_phase": self.dashboard.get("accumulation_phase", ""),
            "first_leg_fill": dict(self.dashboard.get("first_leg_fill") or {}),
            "second_leg_fill": dict(self.dashboard.get("second_leg_fill") or {}),
            "hedge_fills": dict(self.dashboard.get("hedge_fills") or self.dashboard.get("first_leg_fill") or {}),
            "dca_fills": dict(self.dashboard.get("dca_fills") or self.dashboard.get("second_leg_fill") or {}),
            "matched_pairs": round(inv.matched_pairs, 4),
            "yes_shares": round(inv.yes_shares, 4),
            "no_shares": round(inv.no_shares, 4),
            "yes_avg_price": round(yes_avg, 4),
            "no_avg_price": round(no_avg, 4),
            "yes_avg_price_c": round(yes_avg * 100, 2) if inv.yes_shares > 0 else 0.0,
            "no_avg_price_c": round(no_avg * 100, 2) if inv.no_shares > 0 else 0.0,
            "pair_avg_price_c": (
                round((yes_avg + no_avg) * 100, 2)
                if inv.yes_shares > 0 and inv.no_shares > 0 else 0.0
            ),
            "bought_side": self.dashboard.get("bought_side") or side,
            **{k: self.dashboard[k] for k in (
                "stop_loss_event",
                "entry_decision",
                "momentum_signal_display",
                "signal_quality_score",
                "entry_net_edge",
                "take_profit_pct",
            ) if k in self.dashboard},
            "trade_phase": trade_phase,
            "strategy_state": trade_phase,
            "max_entry_price": max_res.minimum_price,
            "max_entry_price_c": round(max_res.minimum_price * 100, 2),
            "min_net_edge": wc.min_net_edge,
            "min_net_edge_pct": round(wc.min_net_edge * 100, 2),
            "momentum_direction": self.dashboard.get("momentum_direction", ""),
            "yes_net_edge": self.dashboard.get("yes_net_edge", 0.0),
            "no_net_edge": self.dashboard.get("no_net_edge", 0.0),
            "estimated_yes_probability": self.dashboard.get("estimated_yes_probability", 0.0),
            "estimated_no_probability": self.dashboard.get("estimated_no_probability", 0.0),
            "reference_price": self.dashboard.get("reference_price", 0.0),
            "current_underlying_price": self.dashboard.get("current_underlying_price", 0.0),
            "resolution_oracle": self.dashboard.get("resolution_oracle", ""),
            "buy_limit_price": self.dashboard.get("buy_limit_price", 0.0),
            "buy_limit_price_c": self.dashboard.get("buy_limit_price_c", 0.0),
            "entry_fill_price": self.dashboard.get("entry_fill_price", 0.0),
            "entry_fill_price_c": self.dashboard.get("entry_fill_price_c", 0.0),
            "filled_size": self.dashboard.get("filled_size", 0.0),
            "stop_loss_pct": wc.stop_loss_pct,
            "stop_loss_pct_display": round(wc.stop_loss_pct * 100, 1),
            "order_size": wc.order_size_max,
            "execution_type": "FOK",
            "position_side": side,
            "position_size": pos_size,
            "executable_bid": self.dashboard.get("executable_bid", 0.0),
            "executable_bid_c": self.dashboard.get("executable_bid_c", 0),
            "stop_loss_trigger_price": self.dashboard.get("stop_loss_trigger_price", 0.0),
            "stop_loss_trigger_c": self.dashboard.get("stop_loss_trigger_c", 0),
            "take_profit_pct": wc.take_profit_pct,
            "take_profit_pct_display": round(wc.take_profit_pct * 100, 1),
            "take_profit_trigger_price": self.dashboard.get("take_profit_trigger_price", 0.0),
            "take_profit_trigger_c": self.dashboard.get("take_profit_trigger_c", 0),
            "exit_fill_price": self.dashboard.get("exit_fill_price", 0.0),
            "exit_fill_price_c": self.dashboard.get("exit_fill_price_c", 0),
            "exit_reason": self.dashboard.get("exit_reason", ""),
            "exit_order_type": self.dashboard.get("exit_order_type", "FOK"),
            "unrealized_pnl": self.dashboard.get("unrealized_pnl", pnl_dollars),
            "realized_pnl": self.dashboard.get("realized_pnl", self.cumulative_pnl),
            "cumulative_realized_pnl": self.dashboard.get(
                "cumulative_realized_pnl", self.cumulative_pnl,
            ),
            "total_net_pnl": self.dashboard.get("total_net_pnl"),
            "overall_net_pnl": self.dashboard.get(
                "overall_net_pnl", self.dashboard.get("total_net_pnl"),
            ),
            "fees_paid": self.dashboard.get("fees_paid", 0.0),
            "entry_order_status": entry_status,
            "exit_order_status": exit_status,
            "market_stopped_out": self.dashboard.get("market_stopped_out", False),
            "entry_block_reason": self.dashboard.get("entry_block_reason", "") or "",
            "yes": round(normalize_polymarket_price(float(self.prices.get("YES", 0) or 0)) * 100, 2),
            "no": round(normalize_polymarket_price(float(self.prices.get("NO", 0) or 0)) * 100, 2),
            "yes_ask_c": self.dashboard.get("yes_ask_c", 0),
            "no_ask_c": self.dashboard.get("no_ask_c", 0),
            "yes_bid_c": self.dashboard.get("yes_bid_c", 0),
            "no_bid_c": self.dashboard.get("no_bid_c", 0),
            "timer": self.get_market_countdown(),
            "listener": self.get_listener_countdown(),
            "market_status": self.dashboard.get("outcome", "PENDING"),
            "position": position_text,
            "outcome": self.dashboard.get("outcome", "PENDING"),
            "dry_run": self.is_dry_run(),
            "pnl_dollars": pnl_dollars,
            "pnl_pct": pnl_pct,
            "cumulative_pnl": round(self.cumulative_pnl, 2),
            "wins": self.wins,
            "losses": self.losses,
            "trade_count": self.trade_count,
            "entry_count": self.entry_count,
            "win_rate": (round((self.wins / self.trade_count) * 100, 1) if self.trade_count > 0 else 0.0),
            "market_start_iso": market_start_iso,
            "market_end_iso": market_end_iso,
            "trading_allowed": schedule_ok,
            "trading_tz": _TRADING_TZ_NAME,
            "entry_allowed": schedule_ok,
            "status": self.dashboard.get("strategy_display_state", trade_phase),
        }
        return enrich_dashboard_payload(payload)

    async def on_market_expired(self) -> None:
        """Cancel open orders, merge matched pairs, and settle inventory."""
        await self.cancel_all_open_orders()
        if self._open_orders:
            self.dashboard["settlement_status"] = "pending_open_orders"
            exec_log("settlement_deferred", asset=self.asset_type, window=self.window_slug,
                     reason="open_orders_unconfirmed", open_order_count=len(self._open_orders))
            print("⚠️ Settlement deferred: one or more orders may still be active.")
            return

        self.final_yes_price = self.prices.get("YES", 0.0)
        self.final_no_price  = self.prices.get("NO",  0.0)
        if self.final_yes_price > FINAL_PRICE:
            self.market_outcome = "YES"
        elif self.final_no_price > FINAL_PRICE:
            self.market_outcome = "NO"
        else:
            self.market_outcome = "UNKNOWN"
        self.dashboard["outcome"] = self.market_outcome
        self.dashboard["strategy_state"] = "market_completed"

        inv = self.inventory
        matched = inv.matched_pairs
        if matched > MIN_FILL_DELTA:
            if self.is_dry_run():
                print(f"🧪 [DRY] Would merge {matched:.2f} matched YES+NO pairs on-chain")
            else:
                await self.merge_shares(matched)

        self._settle_market()

    def print_final_summary(self):
        """Backward-compatible sync wrapper — prefer on_market_expired in async paths."""
        self.final_yes_price = self.prices.get("YES", 0.0)
        self.final_no_price  = self.prices.get("NO",  0.0)
        if self.final_yes_price > FINAL_PRICE:
            self.market_outcome = "YES"
        elif self.final_no_price > FINAL_PRICE:
            self.market_outcome = "NO"
        else:
            self.market_outcome = "UNKNOWN"
        self.dashboard["outcome"] = self.market_outcome
        self._settle_market()

    def _settle_market(self) -> None:
        inv = self.inventory
        outcome = self.market_outcome or "UNKNOWN"
        if outcome not in {"YES", "NO"}:
            self.dashboard["settlement_status"] = "pending_unknown_outcome"
            exec_log("settlement_deferred", asset=self.asset_type, window=self.window_slug,
                     reason="outcome_unknown", yes_price=self.final_yes_price,
                     no_price=self.final_no_price)
            print("⚠️ Settlement deferred: outcome is UNKNOWN; ledger left unchanged.")
            return
        self.dashboard["settlement_status"] = "settled"
        price_in_cents = lambda p: f"{round(p * 100)}c"

        if inv.yes_shares <= MIN_FILL_DELTA and inv.no_shares <= MIN_FILL_DELTA:
            print(f"\n{BOLD}{YELLOW}ℹ️  No Gabagool inventory this market "
                  f"({self.entry_count} entry attempts).{RESET}")
            self._finish_market_merge()
            return

        matched = inv.matched_pairs
        yes_unpaired = max(0.0, inv.yes_shares - matched)
        no_unpaired = max(0.0, inv.no_shares - matched)
        total_cost = inv.yes_cost + inv.no_cost

        settlement = matched * 1.0
        if outcome == "YES":
            settlement += yes_unpaired * 1.0
        elif outcome == "NO":
            settlement += no_unpaired * 1.0

        actual_profit = round(settlement - total_cost, 4)
        if self.entry_count >= 1 and (yes_unpaired > MIN_FILL_DELTA or no_unpaired > MIN_FILL_DELTA):
            try:
                from utils.exit_performance import exit_performance_store
                exit_performance_store.record_settlement(
                    self.worker_config.key,
                    pnl=actual_profit,
                )
                self.dashboard["strategy_state"] = "position_settled"
                self.dashboard["exit_reason"] = "market_settlement"
            except Exception:
                pass

        print(f"\n\n{BOLD}{GREEN}--- 📊 GABAGOOL SETTLEMENT ---{RESET}")
        print(f"⏱️ Market Outcome:      {outcome}")
        print(f"📦 Entries this round:  {self.entry_count}")
        print(f"📊 YES shares:          {inv.yes_shares:.4f} "
              f"(avg {price_in_cents(inv.avg_cost('YES'))})")
        print(f"📊 NO shares:           {inv.no_shares:.4f} "
              f"(avg {price_in_cents(inv.avg_cost('NO'))})")
        print(f"🔗 Matched pairs:       {matched:.4f}")
        print(f"📉 Total invested:      ${total_cost:.4f}")
        print(f"💵 Settlement value:    ${settlement:.4f}")
        print(f"📈 Net Profit:          {GREEN if actual_profit >= 0 else RED}"
              f"${actual_profit:.4f}{RESET}")

        if matched > 0:
            self.log_trade("YES", 1.0, "redeem", size=matched)
        if outcome == "YES" and yes_unpaired > 0:
            self.log_trade("YES", 1.0, "redeem", size=yes_unpaired)
        elif outcome == "NO" and no_unpaired > 0:
            self.log_trade("NO", 1.0, "redeem", size=no_unpaired)

        self.log_pnl("GABAGOOL_SETTLE", actual_profit, {
            "outcome":          outcome,
            "yes_shares":       inv.yes_shares,
            "no_shares":        inv.no_shares,
            "matched_pairs":    matched,
            "yes_unpaired":     yes_unpaired,
            "no_unpaired":      no_unpaired,
            "total_cost":       total_cost,
            "settlement_value": settlement,
            "entry_count":      self.entry_count,
        })
        canonical_ledger.sync_bot_pnl_snapshot()
        self._finish_market_merge()

    def _finish_market_merge(self) -> None:
        global completed_markets
        completed_markets += 1
        if completed_markets >= TOTAL_BOTS:
            self.account.merge_now(send_telegram_notify=True)
            completed_markets = 0
        else:
            self.account.merge_now(send_telegram_notify=False)

    # ── Market scanner ─────────────────────────────────────────────────

    def _interval_seconds(self) -> int:
        return self.worker_config.interval_seconds

    def _interval_start(self, ts: int) -> int:
        sec = self._interval_seconds()
        return (ts // sec) * sec

    def current_interval_starts(self, now_ts: Optional[int] = None) -> Tuple[int, int]:
        now = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
        base = self._interval_start(now)
        return base, base + self._interval_seconds()

    def get_candidate_markets(self, asset: Optional[str] = None) -> list:
        slug_asset = (asset or self.asset_type).lower()
        now       = int(datetime.now(timezone.utc).timestamp())
        intervals = list(self.current_interval_starts(now))
        markets   = []
        sec = self._interval_seconds()
        for ts in intervals:
            slug = self.worker_config.market_slug(ts)
            try:
                resp = requests.get(f"{GAMMA_URL}?slug={slug}").json()
                if resp:
                    markets.append({
                        "slug":     slug,
                        "url":      f"https://polymarket.com/event/{resp[0]['slug']}",
                        "start_ts": ts,
                    })
            except Exception:
                pass
        return markets

    def pick_next_market(self, markets: list) -> Optional[dict]:
        now_ts  = int(datetime.now(timezone.utc).timestamp())
        sec = self._interval_seconds()
        current = [m for m in markets if m["start_ts"] <= now_ts < m["start_ts"] + sec]
        if current:
            return current[0]
        future = [m for m in markets if m["start_ts"] > now_ts]
        if not future:
            return None
        return min(future, key=lambda x: x["start_ts"])

    async def start(self):
        """Per-worker trading loop for one (asset, window) pair."""
        wc = self.worker_config
        print(f"🤖 EmilianoBot — Momentum + delayed adjustment → "
              f"{self.asset_type.upper()} {self.window_slug} markets...")
        print(f"  Market interval   : {wc.window} ({wc.interval_seconds}s)")
        print(f"  Listener window   : final {wc.listener_activate_secs}s")
        print(f"  Min net edge      : {wc.min_net_edge:.2%}")
        print(f"  Max entry price   : {wc.momentum_max_entry_price:.2f}")
        print(f"  Stop loss         : {wc.stop_loss_pct:.0%}")
        print(f"  Live trading      : {wc.live_trading_enabled} (dry_run={self.is_dry_run()})")
        if wc.entry_window_seconds > 0:
            print(f"  Entry window      : final {wc.entry_window_seconds}s")
        else:
            print(f"  Entry window      : disabled (whole market)")
        print(f"  Order type        : FOK only")
        if wc.random_order_size:
            order_size_label = (
                f"{wc.order_size_min}-{wc.order_size_max} shares random "
                f"(max order {wc.max_order_size})"
            )
        else:
            order_size_label = f"{wc.order_size_max} shares (max order {wc.max_order_size})"
        print(f"  Order size        : {order_size_label}")
        print(f"  Max inventory     : {wc.max_shares} shares per leg")
        print(f"  Entry window      : {wc.min_entry_seconds_left}s < t <= {wc.entry_seconds_left}s")
        print(f"  Fill monitor      : {wc.fill_timeout_ms}ms timeout / "
              f"{wc.fill_poll_ms}ms poll")
        if self.is_dry_run():
            print(f"  Dry-run fill delay: {wc.dry_run_fill_delay_min_ms}-"
                  f"{wc.dry_run_fill_delay_max_ms}ms per leg")
        print(f"  Dry run           : {self.is_dry_run()}")
        from utils.graceful_shutdown import shutdown_requested

        session = requests.Session()
        sec = wc.interval_seconds
        while not shutdown_requested():
            try:
                now_ts     = int(datetime.now(timezone.utc).timestamp())
                intervals  = list(self.current_interval_starts(now_ts))
                candidates = []
                for ts in intervals:
                    slug = wc.market_slug(ts)
                    try:
                        resp = session.get(f"{GAMMA_URL}?slug={slug}", timeout=2).json()
                        if resp:
                            candidates.append({
                                "url":      f"https://polymarket.com/event/{resp[0]['slug']}",
                                "start_ts": ts,
                                "slug":     slug,
                            })
                    except Exception:
                        continue

                current = [m for m in candidates if m["start_ts"] <= now_ts < m["start_ts"] + sec]
                if current:
                    target = current[0]
                else:
                    future = [m for m in candidates if m["start_ts"] > now_ts]
                    if not future:
                        print("⏳ No market → retrying...")
                        await asyncio.sleep(2)
                        continue
                    target = min(future, key=lambda x: x["start_ts"])

                print(f"\n🎯 Target: {target['slug']}")
                if not self.fetch_target_market(target["url"]):
                    print("❌ Market fetch failed → retry loop")
                    await asyncio.sleep(1)
                    continue

                await self.reset_state()
                now_ts = int(datetime.now(timezone.utc).timestamp())
                start  = target["start_ts"]
                if start <= now_ts < start + sec:
                    print("⚡ Already inside active market → starting immediately")

                print("📡 Starting quote poll + WebSocket listener...")
                poll_task = asyncio.create_task(self.quote_poll_loop())
                try:
                    await self.price_listener()
                finally:
                    poll_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await poll_task
                print("🏁 Market complete → instant rescan")

            except Exception as e:
                print(f"❌ Scheduler Error: {e}")
                await asyncio.sleep(2)


# Backward-compatible alias: existing external code/imports that reference
# `EmilianoBot` (e.g. main.py) keep working without modification.
EmilianoBot = MarketWorker

# ═════════════════════════════════════════════════════════════════════════════
# TERMINAL DASHBOARD
# ═════════════════════════════════════════════════════════════════════════════

def create_dashboard(bots):
    # Compute schedule status once for the whole render cycle.
    _trading_ok   = is_trading_allowed()
    _schedule_str = (
        "[bold green]ENTRIES: OPEN (weekday)[/bold green]"
        if _trading_ok else
        "[bold red]ENTRIES: BLOCKED (weekend / non-trading day)[/bold red]"
    )
    _tz_label = _TRADING_TZ_NAME

    layout = Layout()
    layout.split_column(
        Layout(
            Panel(
                f"[bold cyan]EMILIANO BOT — Momentum Inventory Accumulation (FOK)[/bold cyan]\n"
                f"Schedule ({_tz_label}): {_schedule_str}",
                style="bold green", box=box.ROUNDED,
            ),
            size=4,
        ),
        Layout(name="main"),
    )
    layout["main"].split_row(
        Layout(name="col1", ratio=1),
        Layout(name="col2", ratio=1),
    )

    for i, bot in enumerate(bots):
        d           = bot.dashboard
        pnl_dollars, pnl_pct, pnl_color = bot.get_current_pnl()
        listener_cd = bot.get_listener_countdown()

        display_status = d.get("strategy_display_state", d.get("status", "WAITING FOR ENTRY"))
        entry_c = d.get("entry_price_c", d.get("buy_limit_price_c", 0))
        stop_pct = round(float(d.get("stop_loss_pct", 0.20)) * 100)
        side = d.get("position_side") or "—"
        limit_c = d.get("buy_limit_price_c", 0)
        fill_c = d.get("entry_fill_price_c", 0)
        strategy_text = (
            f"min entry>={entry_c}c | stop {stop_pct}% | FOK | "
            f"side={side} | limit={limit_c}c | fill={fill_c}c"
        )

        pos_size = float(d.get("position_size") or 0)
        card_color = "cyan" if pos_size > 0 else "blue"

        # Time window label
        if bot.active_market and bot.active_market.get("expiry"):
            expiry_utc  = bot.active_market["expiry"]
            et_zone     = ET_ZONE
            expiry_et   = expiry_utc.astimezone(et_zone)
            start_et    = expiry_et - timedelta(seconds=bot._interval_seconds())
            time_window = (f"{start_et.strftime('%b %d')}, "
                           f"{start_et.strftime('%I:%M%p')}-{expiry_et.strftime('%I:%M%p')} ET")
        else:
            time_window = "Waiting for market..."

        bought_text = (
            f"[dim]{strategy_text} | size={bot.worker_config.order_size_max} sh[/dim]"
        )

        card = Panel(
            Text.from_markup(
                f"""[yellow]YES:[/] {d.get('yes', 0):>3}c    [yellow]NO:[/] {d.get('no', 0):>3}c
[cyan]Timer:[/] {d.get('timer', '--:--')}
[cyan]Listener:[/] {listener_cd}
[magenta]Status:[/] {display_status}
{bought_text}
[bold]ROI:[/] [{pnl_color}]+${pnl_dollars:.2f} ({pnl_pct:+.2f}%)[/{pnl_color}]
[bold]Entry/Exit:[/] {d.get('entry_order_status', 'IDLE')} / {d.get('exit_order_status', 'IDLE')}
[bold]Outcome:[/] [bold {'green' if d.get('outcome') == 'YES' else 'red' if d.get('outcome') == 'NO' else 'white'}]{d.get('outcome', 'PENDING')}[/]"""
            ),
            title=f"{d.get('asset', 'UNKNOWN')} · {time_window}",
            border_style=card_color, box=box.HEAVY, padding=(1, 2),
        )
        if i == 0:
            layout["main"]["col1"].update(card)
        elif i == 1:
            layout["main"]["col2"].update(card)
        elif i == 2:
            if layout["main"]["col1"].renderable is None:
                layout["main"]["col1"].update(card)
            else:
                layout["main"]["col1"].split_column(
                    layout["main"]["col1"].renderable, Layout(card, ratio=1))
        elif i == 3:
            if layout["main"]["col2"].renderable is None:
                layout["main"]["col2"].update(card)
            else:
                layout["main"]["col2"].split_column(
                    layout["main"]["col2"].renderable, Layout(card, ratio=1))
    return layout


async def dashboard_loop(bots):
    with Live(create_dashboard(bots), console=console,
              refresh_per_second=2, screen=True) as live:
        while True:
            try:
                live.update(create_dashboard(bots))
            except Exception as e:
                console.print(f"[red]Dashboard error: {e}[/red]")
            await asyncio.sleep(0.8)


# ═════════════════════════════════════════════════════════════════════════════
# PNL MERGE + TELEGRAM
# ═════════════════════════════════════════════════════════════════════════════

# PNL merge reads per-asset local JSON files for configured TRADING_ASSETS only.
OUTPUT_FILE            = "bot_pnl.json"
MERGE_INTERVAL_SECONDS = 300
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID")

completed_markets      = 0
last_notification_time = 0


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials not set.")
        return
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "parse_mode": "HTML", "text": message}
        requests.post(url, json=payload, timeout=10)
        print("📨 Telegram notification sent.")
    except Exception as e:
        print(f"❌ Failed to send Telegram: {e}")


def get_pnl_emoji(pnl: float) -> str:
    return "🟢" if pnl >= 0 else "🔴"


def merge_all_pnl(send_telegram_notify: bool = False):
    global completed_markets, last_notification_time
    all_trades: List[Dict[str, Any]] = []
    total_pnl    = 0.0
    total_wins   = 0
    total_losses = 0
    asset_stats: Dict[str, Dict] = {}
    print(f"\n🔄 [{datetime.now().strftime('%H:%M:%S')}] Merging PNL files...")
    for file_path in PNL_FILES:
        if not os.path.exists(file_path):
            continue
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data   = json.load(f)
            trades     = data.get("trades", [])
            file_pnl   = data.get("total_pnl", 0.0)
            wins       = data.get("wins", 0)
            losses     = data.get("losses", 0)
            asset_name = file_path.replace("_pnl_history.json", "").upper()
            asset_stats[asset_name] = {
                "wins":         wins,
                "losses":       losses,
                "total_trades": wins + losses,
                "pnl":          file_pnl,
                "win_rate":     (round((wins / (wins + losses) * 100), 2)
                                 if (wins + losses) > 0 else 0.0),
            }
            all_trades.extend(trades)
            total_pnl    += file_pnl
            total_wins   += wins
            total_losses += losses
        except Exception as e:
            print(f"❌ Error reading {file_path}: {e}")

    total_trades     = total_wins + total_losses
    overall_win_rate = (round((total_wins / total_trades) * 100, 2)
                        if total_trades > 0 else 0.0)
    combined_data = {
        "total_pnl":    round(total_pnl, 4),
        "wins":         total_wins,
        "losses":       total_losses,
        "win_rate":     f"{overall_win_rate}%",
        "total_trades": total_trades,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "assets":       asset_stats,
    }
    try:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(combined_data, f, indent=2)
    except Exception:
        pass

    ranked = sorted(asset_stats.items(), key=lambda x: x[1]["pnl"], reverse=True)
    print("\n" + "=" * 80)
    print(f"{BOLD}{CYAN}💰 PNL SUMMARY{RESET}")
    print("=" * 80)
    print(f"Total PNL       : ${total_pnl:.2f}")
    print(f"Total Trades    : {total_trades} ({total_wins}W | {total_losses}L)")
    print(f"Overall Win Rate: {overall_win_rate:.1f}%\n")
    print(f"{BOLD}Asset Ranking:{RESET}")
    for i, (asset, stats) in enumerate(ranked, 1):
        color = GREEN if stats["pnl"] >= 0 else RED
        print(f"{i}. {asset:<6} | {stats['total_trades']:>2} trades | "
              f"{stats['wins']:>3}W {stats['losses']:>3}L | "
              f"{stats['win_rate']:>5.1f}% | {color}${stats['pnl']:.2f}{RESET}")

    if send_telegram_notify:
        current_time = t.time()
        if current_time - last_notification_time > 60:
            telegram_msg = (
                f"<b>💰 EMILIANO PNL Summary</b>\n\n"
                f"<b>Total PNL:</b> {get_pnl_emoji(total_pnl)} "
                f"<b>${total_pnl:.2f}</b>\n"
                f"<b>Total Trades:</b> {total_trades} "
                f"(<b>{total_wins}W</b> - <b>{total_losses}L</b>)\n"
                f"<b>Overall Win Rate:</b> {overall_win_rate:.1f}%\n\n"
                f"<b>Ranking:</b>\n"
            )
            for i, (asset, stats) in enumerate(ranked, 1):
                emoji = get_pnl_emoji(stats["pnl"])
                telegram_msg += (
                    f"{i}. <b>{asset}</b>: {emoji} ${stats['pnl']:.2f} | "
                    f"{stats['total_trades']} trades "
                    f"(<b>{stats['wins']}W</b> - {stats['losses']}L) | "
                    f"{stats['win_rate']:.1f}%\n"
                )
            telegram_msg += f"\nLast Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            send_telegram(telegram_msg)
            last_notification_time = current_time


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════
#
# Startup order (this is the fix for the duplicate-global-task issue):
#   1. Construct ONE AccountService — single Web3 connection, single ClobClient,
#      auth derived/created exactly once for the whole process.
#   2. Run the wallet audit ONCE, here, before any market worker exists.
#   3. Start the global PnL-merge scheduler ONCE.
#   4. Construct N MarketWorker instances (one per tracked asset), each
#      sharing the same AccountService by reference. Adding more assets here
#      never creates a second wallet audit, a second ClobClient, or a second
#      PnL-merge scheduler — those only ever exist once per process.
# ═════════════════════════════════════════════════════════════════════════════

_BINANCE_FEED_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"})


async def ensure_binance_feeds_for_workers(
    worker_configs: Optional[Tuple[WorkerConfig, ...]] = None,
) -> None:
    """Start shared Binance spot WS feeds for configured trading assets."""
    configs = worker_configs or WORKER_CONFIGS
    for asset in dict.fromkeys(wc.asset for wc in configs):
        sym = asset.upper()
        if sym in _BINANCE_FEED_ASSETS:
            await BinanceDepthSignal.get_or_create(sym)
            print(f"📡 [Binance] External feed starting for {sym}USDT")


async def main():
    from utils.graceful_shutdown import (
        get_shutdown_event,
        graceful_shutdown,
        install_signal_handlers,
        register_shutdown_hook,
        track_background_task,
    )

    install_signal_handlers()
    get_shutdown_event()

    account = AccountService()

    if not account.run_wallet_audit():
        console.print("[bold red]Wallet audit failed — aborting startup.[/bold red]")
        return
    account.start_pnl_merge_scheduler()

    await ensure_binance_feeds_for_workers()

    bots = [MarketWorker(wc, account) for wc in WORKER_CONFIGS]

    async def _stop_workers() -> None:
        await asyncio.gather(*(b.graceful_stop() for b in bots), return_exceptions=True)

    register_shutdown_hook(_stop_workers)

    async def _run() -> None:
        await asyncio.gather(
            *[bot.start() for bot in bots],
            dashboard_loop(bots),
        )

    task = asyncio.create_task(_run(), name="bot-main")
    track_background_task(task)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await graceful_shutdown(reason="Ctrl+C")


if __name__ == "__main__":
    try:
        print("Starting EmilianoBot — momentum + delayed Polymarket adjustment")
        for wc in WORKER_CONFIGS:
            print(
                f"   {wc.asset.upper()} {wc.window}: edge>={wc.min_net_edge:.2%} "
                f"| max entry {wc.momentum_max_entry_price:.2f} "
                f"| stop {wc.stop_loss_pct:.0%} | dry_run={wc.dry_run} | live={wc.live_trading_enabled}"
            )
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(f"[bold red]Fatal Error: {e}[/bold red]")
        raise