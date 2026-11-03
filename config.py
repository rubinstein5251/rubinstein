"""
Centralized configuration: trading_config.json workers + .env secrets/globals.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

SUPPORTED_TRADING_ASSETS: frozenset[str] = frozenset(
    {"btc", "eth", "sol", "xrp", "doge", "hype", "bnb"}
)
SUPPORTED_WINDOWS: frozenset[str] = frozenset({"5m", "15m"})
WINDOW_SECONDS: dict[str, int] = {"5m": 300, "15m": 900}
MIN_SHARES: int = 5

_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "btc",
    "ethereum": "eth",
    "solana": "sol",
    "ripple": "xrp",
}


def _fatal(message: str) -> None:
    print(f"❌ [config] {message}", file=sys.stderr)
    sys.exit(1)


def normalize_asset_slug(raw: str) -> str:
    token = (raw or "").strip().lower()
    if not token:
        raise ValueError("empty asset token")
    return _ASSET_ALIASES.get(token, token)


def normalize_window(raw: str) -> str:
    w = (raw or "").strip().lower()
    if w not in SUPPORTED_WINDOWS:
        raise ValueError(f"unsupported window {raw!r}")
    return w


def worker_key(asset: str, window: str) -> str:
    return f"{normalize_asset_slug(asset)}:{normalize_window(window)}"


def _parse_unit_fraction(name: str, value: Any, default: float) -> float:
    """Parse a config fraction in (0, 1] — used for entry threshold and stop-loss pct."""
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v <= 0 or v > 1 or v != v:
        _fatal(f"{name} must be in (0, 1] (got {raw!r}).")
    return v


def _parse_positive_int(name: str, value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        _fatal(f"{name}={value!r} is not a valid integer.")
    if v < MIN_SHARES:
        _fatal(f"{name} must be >= {MIN_SHARES} (got {value!r}).")
    return v


def _parse_order_size(name: str, value: Any, default: float) -> float:
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v < MIN_SHARES or v != v or v in (float("inf"), float("-inf")):
        _fatal(f"{name} must be >= {MIN_SHARES} (got {raw!r}).")
    return v


def _parse_max_shares(name: str, value: Any, default: float) -> float:
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v < MIN_SHARES or v != v or v in (float("inf"), float("-inf")):
        _fatal(f"{name} must be >= {MIN_SHARES} (got {raw!r}).")
    return v


def _parse_nonnegative_float(name: str, value: Any, default: float) -> float:
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v < 0 or v != v or v in (float("inf"), float("-inf")):
        _fatal(f"{name} must be >= 0 (got {raw!r}).")
    return v


def _parse_bool_value(name: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_cooldown_ms(name: str, value: Any, default: int) -> int:
    try:
        v = int(value if value is not None else default)
    except (TypeError, ValueError):
        _fatal(f"{name}={value!r} is not a valid integer.")
    if v < 0:
        _fatal(f"{name} must be >= 0 (got {value!r}).")
    return v


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _load_env_sizing_overrides() -> Optional[dict[str, float]]:
    """Apply sizing overrides only when ORDER_SIZE_MIN, ORDER_SIZE_MAX, and MAX_SHARES are all set."""
    keys = ("ORDER_SIZE_MIN", "ORDER_SIZE_MAX", "MAX_SHARES")
    raw = {k: os.getenv(k, "").strip() for k in keys}
    set_keys = [k for k, v in raw.items() if v]
    if not set_keys:
        return None
    if len(set_keys) != len(keys):
        _fatal(
            "ORDER_SIZE_MIN, ORDER_SIZE_MAX, and MAX_SHARES must all be set together "
            f"to override sizing (found: {', '.join(set_keys)}). "
            "Omit all three to use trading_config.json defaults."
        )
    out: dict[str, float] = {
        "order_size_min": _parse_order_size("ORDER_SIZE_MIN", raw["ORDER_SIZE_MIN"], 0.0),
        "order_size_max": _parse_order_size("ORDER_SIZE_MAX", raw["ORDER_SIZE_MAX"], 0.0),
        "max_shares": _parse_max_shares("MAX_SHARES", raw["MAX_SHARES"], 0.0),
    }
    optional_max_order = os.getenv("MAX_ORDER_SIZE", "").strip()
    if optional_max_order:
        out["max_order_size"] = _parse_order_size("MAX_ORDER_SIZE", optional_max_order, 0.0)
    return out


ENV_SIZING_OVERRIDES: Optional[dict[str, float]] = _load_env_sizing_overrides()


DRY_RUN_DEFAULT: bool = _parse_bool_env("DRY_RUN_DEFAULT", _parse_bool_env("DRY_MODE", True))


def _cfg_get(raw: dict, defaults: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present value from worker entry or defaults."""
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
        if key in defaults and defaults[key] is not None:
            return defaults[key]
    return default


@dataclass(frozen=True)
class WorkerConfig:
    asset: str
    window: str
    # Legacy fields kept for dashboard/backward compatibility. Gabagool does not use them for entries/exits.
    momentum_entry_threshold: float = 0.90
    stop_loss_pct: float = 0.20
    price_99_entry_window_seconds: int = 60
    inventory_accumulation_enabled: bool = True
    cheap_side_min_price: float = 0.10
    cheap_side_max_price: float = 0.20
    target_combined_pair_cost: float = 0.98
    max_combined_pair_cost: float = 0.98
    max_entries_per_side_per_market: int = 1
    no_buys_last_minutes: int = 2
    cheap_entry_price: float = 0.20
    initial_hedge_delay_seconds: int = 0
    initial_hedge_timeout_seconds: float = 4.0
    combined_cost_target: float = 0.98
    max_combined_cost: float = 0.98
    entry_window_seconds: int = 60
    max_entry_price: float = 0.20
    order_type: str = "FOK"
    max_slippage: float = 0.005
    max_quote_age_seconds: float = 3.0
    max_hedge_retries: int = 3
    hedge_retry_delay_ms: int = 400
    max_market_exposure: float = 40.0
    max_position_size: float = 20.0
    max_unmatched_yes: float = 20.0
    max_unmatched_no: float = 20.0
    max_share_imbalance: float = 0.0
    max_unmatched_shares: float = 0.0
    balance_inventory_before_buy: bool = True
    account_for_pending_buy_orders: bool = True
    allow_unmatched_accumulation: bool = False
    min_order_size: float = float(MIN_SHARES)
    late_entry_enabled: bool = False
    entry_price: float = 0.95
    entry_price_threshold: float = 0.95
    late_entry_max_slippage: float = 0.005
    min_time_before_close_seconds: int = 0
    stop_loss_exit_max_retries: int = 5
    stop_loss_exit_retry_ms: int = 400
    gabagool_initial_entry_threshold: float = 0.90
    gabagool_max_pair_cost: float = 0.98
    gabagool_min_profit_margin: float = 0.02
    gabagool_max_unpaired_shares: float = 5.0
    gabagool_min_time_to_resolution: int = 60
    gabagool_price_buffer: float = 0.00
    gabagool_inventory_soft_limit: float = 2.0
    gabagool_endgame_seconds: int = 30
    gabagool_endgame_max_unpaired: float = 1.0
    gabagool_falling_knife_enabled: bool = True
    gabagool_falling_knife_window_seconds: float = 3.0
    gabagool_falling_knife_min_drop: float = 0.035
    gabagool_falling_knife_fast_window_seconds: float = 1.0
    gabagool_falling_knife_fast_drop: float = 0.015
    gabagool_falling_knife_recovery_cents: float = 1.5
    gabagool_falling_knife_cooldown_seconds: float = 5.0
    gabagool_falling_knife_binance_confirm: float = 0.10

    # Arbigab-style three-engine architecture
    momentum_engine_enabled: bool = True
    momentum_min_delta: float = 0.0015
    momentum_signal_max_age: float = 0.75
    momentum_execution_mode: str = "single_taker"
    momentum_max_position_delta: float = 5.0
    market_making_enabled: bool = True
    market_making_quote_size: float = 5.0
    market_making_min_spread: float = 0.01
    market_making_quote_offset: float = 0.01
    market_making_max_position_delta: float = 5.0
    market_making_requote_seconds: float = 0.40
    spread_capture_enabled: bool = True
    spread_capture_trigger_sum: float = 0.995
    spread_capture_max_entry_sum: float = 0.985
    spread_capture_max_unpaired: float = 1.0
    spread_capture_min_net_profit: float = 0.005
    spread_capture_fee_rate: float = 0.0
    spread_capture_slippage_buffer: float = 0.002
    spread_capture_min_book_liquidity: float = 5.0
    spread_capture_max_data_age_seconds: float = 3.0
    spread_capture_second_leg_timeout_ms: int = 10000
    spread_capture_max_recovery_loss: float = 2.0
    spread_capture_release_blocked_to_eligible: bool = False
    spread_capture_binance_volatility_block: bool = True
    inventory_max_pair_cost: float = 0.98
    inventory_min_net_edge: float = 0.02
    inventory_max_capital_per_market: float = 40.0
    inventory_max_dca_attempts: int = 8
    inventory_dca_cooldown_seconds: float = 60.0
    inventory_dca_min_price_move: float = 0.03
    inventory_max_imbalance_seconds: float = 7200.0
    inventory_max_active_orders: int = 4
    min_net_profit_per_pair: float = 0.02
    force_hedge_after_timeout: bool = False
    max_consecutive_buys_per_outcome: int = 3
    stabilization_window_seconds: float = 15.0
    stabilization_tolerance_percent: float = 0.02
    min_orderbook_depth: float = 5.0
    falling_knife_filter_enabled: bool = True
    price_drop_window_seconds: float = 15.0
    max_price_drop_percent: float = 3.5
    falling_knife_cooldown_seconds: float = 5.0
    binance_fok_enabled: bool = True
    binance_fok_signal_windows_ms: tuple = (1000.0, 3000.0, 5000.0)
    binance_fok_up_move_threshold_pct: float = 0.0008
    binance_fok_down_move_threshold_pct: float = 0.0008
    binance_fok_signal_cooldown_ms: float = 3000.0
    binance_fok_max_signal_latency_ms: float = 500.0
    binance_fok_max_binance_data_age_ms: float = 1500.0
    binance_fok_max_combined_pair_cost: float = 0.985
    binance_fok_min_net_pair_edge: float = 0.01
    binance_fok_max_entry_price: float = 0.85
    binance_fok_max_hedge_price: float = 0.85
    binance_fok_max_slippage: float = 0.005
    binance_fok_max_hedge_delay_ms: float = 2500.0
    binance_fok_max_unhedged_exposure_usd: float = 15.0
    binance_fok_max_capital_per_trade: float = 10.0
    preemptive_cancel_enabled: bool = True
    preemptive_cancel_imbalance: float = 0.18
    preemptive_cancel_momentum: float = 0.10
    preemptive_cancel_price_delta: float = 0.0015
    trade_cooldown_ms: int = 3000
    order_size_min: float = 5.0
    order_size_max: float = 5.0
    max_order_size: float = 5.0
    max_shares: float = 20.0
    dry_run: bool = DRY_RUN_DEFAULT
    dry_run_fill_delay_min_ms: int = 200
    dry_run_fill_delay_max_ms: int = 2500
    listener_activate_secs: int = 300
    entry_seconds_left: int = 300
    min_entry_seconds_left: int = 0
    fill_timeout_ms: int = 10000
    fill_poll_ms: int = 400
    momentum_strategy_enabled: bool = True
    live_trading_enabled: bool = False
    momentum_max_entry_price: float = 0.92
    min_net_edge: float = 0.05
    momentum_windows_seconds: tuple = (5, 15, 30, 60, 120)
    min_momentum_strength: float = 0.75
    momentum_volatility_cap: float = 0.08
    volume_confirmation_threshold: float = 0.05
    momentum_reversal_exit_enabled: bool = True
    momentum_reversal_confirm_strength: float = 0.6
    momentum_reversal_confirmations: int = 2
    max_holding_time_seconds: float = 240.0
    estimated_fee_rate: float = 0.0
    execution_slippage_buffer: float = 0.02
    external_max_data_age_seconds: float = 3.0
    take_profit_pct: float = 0.10
    take_profit_delta: float = 0.08  # legacy; ignored when take_profit_pct is set in config
    min_signal_quality_score: float = 60.0
    signal_gate_windows_seconds: tuple = (5, 15, 30)
    signal_require_timeframe_agreement: bool = False
    momentum_trigger_window_seconds: int = 5
    momentum_confirm_window_seconds: int = 15
    momentum_support_window_seconds: int = 30
    momentum_context_window_seconds: int = 60
    momentum_trigger_min_abs_return: float = 0.00005
    momentum_confirm_tolerance: float = 0.0001
    momentum_support_opposing_tolerance: float = 0.002
    momentum_context_reversal_min_abs_return: float = 0.004
    momentum_require_volume_confirmation: bool = False
    matched_pair_accumulation_enabled: bool = False
    signal_max_spread: float = 0.06
    signal_max_slippage: float = 0.03
    signal_max_token_move_since_open: float = 0.12
    signal_min_persistence_ratio: float = 0.55
    signal_reversal_acceleration_threshold: float = -0.0005
    signal_quality_weights: dict[str, float] = field(
        default_factory=lambda: {
            "momentum_alignment": 25.0,
            "persistence_acceleration": 20.0,
            "net_edge": 25.0,
            "liquidity_spread": 15.0,
            "reference_time": 15.0,
        }
    )
    enabled: bool = True

    @property
    def interval_seconds(self) -> int:
        return WINDOW_SECONDS[self.window]

    @property
    def key(self) -> str:
        return worker_key(self.asset, self.window)

    def market_slug(self, start_ts: int) -> str:
        return f"{self.asset}-updown-{self.window}-{start_ts}"

    @property
    def order_size(self) -> float:
        return self.order_size_max

    @property
    def random_order_size(self) -> bool:
        return self.order_size_min < self.order_size_max - 1e-9


def _merge_worker_entry(raw: dict, defaults: dict) -> WorkerConfig:
    asset = normalize_asset_slug(str(raw.get("asset", "")))
    if asset not in SUPPORTED_TRADING_ASSETS:
        _fatal(f"Invalid asset {raw.get('asset')!r}. Supported: {sorted(SUPPORTED_TRADING_ASSETS)}")

    try:
        window = normalize_window(str(raw.get("window", "")))
    except ValueError:
        _fatal(
            f"Invalid window {raw.get('window')!r} for {asset}. "
            f"Supported: {sorted(SUPPORTED_WINDOWS)}"
        )

    momentum_entry_threshold = _parse_unit_fraction(
        "momentum_entry_threshold",
        _cfg_get(raw, defaults, "momentum_entry_threshold"),
        float(defaults.get("momentum_entry_threshold", 0.90)),
    )
    env_momentum = os.getenv("MOMENTUM_ENTRY_THRESHOLD", "").strip()
    if env_momentum:
        momentum_entry_threshold = _parse_unit_fraction(
            "MOMENTUM_ENTRY_THRESHOLD", env_momentum, momentum_entry_threshold,
        )
    stop_loss_raw = _cfg_get(raw, defaults, "stop_loss_pct", default=0.20)
    env_stop = os.getenv("STOP_LOSS_PERCENT", os.getenv("STOP_LOSS_PCT", "")).strip()
    if env_stop:
        stop_loss_raw = env_stop
    try:
        stop_loss_pct = float(stop_loss_raw)
    except (TypeError, ValueError):
        _fatal(f"stop_loss_pct={stop_loss_raw!r} is not a valid number")
    if stop_loss_pct > 1.0:
        stop_loss_pct /= 100.0
    if stop_loss_pct <= 0 or stop_loss_pct >= 1 or stop_loss_pct != stop_loss_pct:
        _fatal(f"stop_loss_pct must be in (0, 1) (got {stop_loss_raw!r})")

    entry_price_raw = _cfg_get(
        raw, defaults, "entry_price", "entry_price_threshold", "momentum_entry_threshold",
        default=0.95,
    )
    env_entry_price = os.getenv("ENTRY_PRICE", "").strip()
    if env_entry_price:
        entry_price_raw = env_entry_price
    try:
        entry_price = float(entry_price_raw)
    except (TypeError, ValueError):
        _fatal(f"entry_price={entry_price_raw!r} is not a valid number")
    if entry_price > 1.0:
        entry_price /= 100.0
    if entry_price <= 0 or entry_price >= 1 or entry_price != entry_price:
        _fatal(f"entry_price must be in (0, 1) (got {entry_price_raw!r})")
    from utils.entry_limit import resolve_entry_fok_limit

    entry_limit = resolve_entry_fok_limit(entry_price)
    entry_price = entry_limit.limit_price

    entry_window_raw = _cfg_get(
        raw, defaults, "entry_window_seconds", "price_99_entry_window_seconds", default=60,
    )
    env_entry_window = os.getenv("ENTRY_WINDOW_SECONDS", "").strip()
    if not env_entry_window:
        env_entry_window = os.getenv("PRICE_99_ENTRY_WINDOW_SECONDS", "").strip()
    if env_entry_window:
        entry_window_raw = env_entry_window
    try:
        entry_window_seconds = int(entry_window_raw)
        price_99_entry_window_seconds = entry_window_seconds
    except (TypeError, ValueError):
        _fatal(f"entry_window_seconds={entry_window_raw!r} is not a valid integer")
    if entry_window_seconds < 0 or entry_window_seconds > 900:
        _fatal("entry_window_seconds must be between 0 and 900 (0 = no window limit)")

    cheap_min_raw = _cfg_get(raw, defaults, "cheap_side_min_price", default=0.10)
    env_cheap_min = os.getenv("CHEAP_SIDE_MIN_PRICE", "").strip()
    if env_cheap_min:
        cheap_min_raw = env_cheap_min
    cheap_side_min_price = _parse_unit_fraction("cheap_side_min_price", cheap_min_raw, 0.10)

    cheap_max_raw = _cfg_get(raw, defaults, "cheap_side_max_price", default=0.20)
    env_cheap_max = os.getenv("CHEAP_SIDE_MAX_PRICE", "").strip()
    if env_cheap_max:
        cheap_max_raw = env_cheap_max
    cheap_side_max_price = _parse_unit_fraction("cheap_side_max_price", cheap_max_raw, 0.20)
    if cheap_side_min_price > cheap_side_max_price + 1e-9:
        _fatal(
            f"{asset}:{window}: cheap_side_min_price={cheap_side_min_price} "
            f"cannot exceed cheap_side_max_price={cheap_side_max_price}"
        )

    combined_target_raw = _cfg_get(
        raw, defaults, "target_combined_pair_cost", "combined_cost_target", default=0.98,
    )
    env_target = os.getenv("TARGET_COMBINED_PAIR_COST", "").strip()
    if not env_target:
        env_target = os.getenv("COMBINED_COST_TARGET", "").strip()
    if env_target:
        combined_target_raw = env_target
    target_combined_pair_cost = _parse_unit_fraction(
        "target_combined_pair_cost", combined_target_raw, 0.98,
    )
    combined_cost_target = target_combined_pair_cost

    max_combined_raw = _cfg_get(
        raw, defaults, "max_combined_pair_cost", "max_combined_cost", default=0.98,
    )
    env_max_combined = os.getenv("MAX_COMBINED_PAIR_COST", "").strip()
    if not env_max_combined:
        env_max_combined = os.getenv("MAX_COMBINED_COST", "").strip()
    if env_max_combined:
        max_combined_raw = env_max_combined
    max_combined_pair_cost = _parse_unit_fraction("max_combined_pair_cost", max_combined_raw, 0.98)
    max_combined_cost = max_combined_pair_cost
    if combined_cost_target > max_combined_cost + 1e-9:
        _fatal(
            f"{asset}:{window}: target_combined_pair_cost={combined_cost_target} "
            f"cannot exceed max_combined_pair_cost={max_combined_cost}"
        )

    cheap_entry_price = cheap_side_max_price
    max_entry_price = cheap_side_max_price

    max_entries_raw = _cfg_get(raw, defaults, "max_entries_per_side_per_market", default=1)
    try:
        max_entries_per_side_per_market = int(max_entries_raw)
    except (TypeError, ValueError):
        _fatal(f"max_entries_per_side_per_market={max_entries_raw!r} is not a valid integer")
    if max_entries_per_side_per_market < 1:
        _fatal(f"{asset}:{window}: max_entries_per_side_per_market must be >= 1")

    no_buys_raw = _cfg_get(raw, defaults, "no_buys_last_minutes", default=2)
    env_no_buys = os.getenv("NO_BUYS_LAST_MINUTES", "").strip()
    if env_no_buys:
        no_buys_raw = env_no_buys
    try:
        no_buys_last_minutes = int(no_buys_raw)
    except (TypeError, ValueError):
        _fatal(f"no_buys_last_minutes={no_buys_raw!r} is not a valid integer")
    if no_buys_last_minutes < 0:
        _fatal(f"{asset}:{window}: no_buys_last_minutes must be >= 0")

    hedge_delay_raw = _cfg_get(raw, defaults, "initial_hedge_delay_seconds", default=0)
    env_hedge_delay = os.getenv("INITIAL_HEDGE_DELAY_SECONDS", "").strip()
    if env_hedge_delay:
        hedge_delay_raw = env_hedge_delay
    try:
        initial_hedge_delay_seconds = int(hedge_delay_raw)
    except (TypeError, ValueError):
        _fatal(f"initial_hedge_delay_seconds={hedge_delay_raw!r} is not a valid integer")
    if initial_hedge_delay_seconds < 0:
        _fatal(f"{asset}:{window}: initial_hedge_delay_seconds must be >= 0")

    initial_hedge_timeout_seconds = _parse_nonnegative_float(
        "initial_hedge_timeout_seconds",
        os.getenv(
            "INITIAL_HEDGE_TIMEOUT_SECONDS",
            _cfg_get(raw, defaults, "initial_hedge_timeout_seconds", default=4.0),
        ),
        4.0,
    )

    order_type = str(
        os.getenv("ORDER_TYPE", _cfg_get(raw, defaults, "order_type", default="FOK"))
    ).strip().upper()
    if order_type != "FOK":
        _fatal(f"ORDER_TYPE must be FOK (got {order_type!r})")

    inventory_accumulation_enabled = _parse_bool_value(
        "inventory_accumulation_enabled",
        _cfg_get(raw, defaults, "inventory_accumulation_enabled", default=True),
        True,
    )
    late_entry_enabled = False
    entry_price_threshold = cheap_entry_price
    max_slippage = _parse_nonnegative_float(
        "max_slippage",
        os.getenv("MAX_SLIPPAGE", _cfg_get(raw, defaults, "max_slippage", "late_entry_max_slippage", default=0.005)),
        0.005,
    )
    late_entry_max_slippage = max_slippage
    max_hedge_retries = int(_cfg_get(raw, defaults, "max_hedge_retries", "MAX_HEDGE_RETRIES", default=3) or 3)
    env_retries = os.getenv("MAX_HEDGE_RETRIES", "").strip()
    if env_retries:
        max_hedge_retries = int(env_retries)
    if max_hedge_retries < 0:
        _fatal(f"{asset}:{window}: max_hedge_retries must be >= 0")
    hedge_retry_delay_ms = _parse_cooldown_ms(
        "hedge_retry_delay_ms",
        os.getenv("HEDGE_RETRY_DELAY_MS", _cfg_get(raw, defaults, "hedge_retry_delay_ms", default=400)),
        400,
    )
    max_market_exposure = _parse_nonnegative_float(
        "max_market_exposure",
        os.getenv("MAX_MARKET_EXPOSURE", _cfg_get(raw, defaults, "max_market_exposure", "inventory_max_capital_per_market", default=40.0)),
        40.0,
    )
    max_position_size = _parse_max_shares(
        "max_position_size",
        os.getenv("MAX_POSITION_SIZE", _cfg_get(raw, defaults, "max_position_size", "max_shares", default=20.0)),
        20.0,
    )
    max_unmatched_yes = _parse_max_shares(
        "max_unmatched_yes",
        _cfg_get(raw, defaults, "max_unmatched_yes", "gabagool_max_unpaired_shares", default=max_position_size),
        max_position_size,
    )
    max_unmatched_no = _parse_max_shares(
        "max_unmatched_no",
        _cfg_get(raw, defaults, "max_unmatched_no", "gabagool_max_unpaired_shares", default=max_position_size),
        max_position_size,
    )
    max_share_imbalance = _parse_nonnegative_float(
        "max_share_imbalance",
        os.getenv("MAX_SHARE_IMBALANCE", _cfg_get(raw, defaults, "max_share_imbalance", default=0.0)),
        0.0,
    )
    max_unmatched_shares = _parse_nonnegative_float(
        "max_unmatched_shares",
        os.getenv("MAX_UNMATCHED_SHARES", _cfg_get(raw, defaults, "max_unmatched_shares", default=0.0)),
        0.0,
    )
    balance_inventory_before_buy = _parse_bool_value(
        "balance_inventory_before_buy",
        os.getenv(
            "BALANCE_INVENTORY_BEFORE_BUY",
            _cfg_get(raw, defaults, "balance_inventory_before_buy", default=True),
        ),
        True,
    )
    account_for_pending_buy_orders = _parse_bool_value(
        "account_for_pending_buy_orders",
        os.getenv(
            "ACCOUNT_FOR_PENDING_BUY_ORDERS",
            _cfg_get(raw, defaults, "account_for_pending_buy_orders", default=True),
        ),
        True,
    )
    allow_unmatched_accumulation = _parse_bool_value(
        "allow_unmatched_accumulation",
        os.getenv(
            "ALLOW_UNMATCHED_ACCUMULATION",
            _cfg_get(raw, defaults, "allow_unmatched_accumulation", default=False),
        ),
        False,
    )
    if max_unmatched_shares > 0:
        max_unmatched_yes = min(max_unmatched_yes, max_unmatched_shares)
        max_unmatched_no = min(max_unmatched_no, max_unmatched_shares)
    min_order_size = _parse_order_size(
        "min_order_size",
        os.getenv("MIN_ORDER_SIZE", _cfg_get(raw, defaults, "min_order_size", default=MIN_SHARES)),
        float(MIN_SHARES),
    )
    if min_order_size < MIN_SHARES:
        _fatal(f"{asset}:{window}: min_order_size must be >= exchange minimum {MIN_SHARES}")
    max_quote_age_seconds = _parse_nonnegative_float(
        "max_quote_age_seconds",
        _cfg_get(
            raw, defaults, "max_quote_age_seconds",
            "spread_capture_max_data_age_seconds", default=3.0,
        ),
        3.0,
    )
    min_time_before_close_seconds = int(
        _cfg_get(
            raw, defaults, "min_time_before_close_seconds",
            "gabagool_min_time_to_resolution", default=0,
        )
    )
    if min_time_before_close_seconds < 0:
        _fatal(f"{asset}:{window}: min_time_before_close_seconds must be >= 0")

    stop_loss_exit_max_retries = int(
        _cfg_get(raw, defaults, "stop_loss_exit_max_retries", default=5) or 5
    )
    stop_loss_exit_retry_ms = _parse_cooldown_ms(
        "stop_loss_exit_retry_ms",
        _cfg_get(raw, defaults, "stop_loss_exit_retry_ms", default=400),
        400,
    )
    if stop_loss_exit_max_retries < 0:
        _fatal(f"{asset}:{window}: stop_loss_exit_max_retries must be >= 0")
    gabagool_initial_entry_threshold = _parse_unit_fraction(
        "gabagool_initial_entry_threshold",
        _cfg_get(raw, defaults, "gabagool_initial_entry_threshold"),
        float(defaults.get("gabagool_initial_entry_threshold", 0.90)),
    )
    gabagool_max_pair_cost = _parse_unit_fraction(
        "gabagool_max_pair_cost",
        _cfg_get(raw, defaults, "gabagool_max_pair_cost"),
        float(defaults.get("gabagool_max_pair_cost", 0.98)),
    )
    gabagool_min_profit_margin = _parse_unit_fraction(
        "gabagool_min_profit_margin",
        _cfg_get(raw, defaults, "gabagool_min_profit_margin"),
        float(defaults.get("gabagool_min_profit_margin", 0.02)),
    )
    gabagool_max_unpaired_shares = _parse_max_shares(
        "gabagool_max_unpaired_shares",
        _cfg_get(raw, defaults, "gabagool_max_unpaired_shares"),
        float(defaults.get("gabagool_max_unpaired_shares", 5.0)),
    )
    gabagool_min_time_to_resolution = int(
        _cfg_get(raw, defaults, "gabagool_min_time_to_resolution", default=60)
    )
    gabagool_price_buffer = float(
        _cfg_get(raw, defaults, "gabagool_price_buffer", default=0.0)
    )
    gabagool_inventory_soft_limit = _parse_nonnegative_float(
        "gabagool_inventory_soft_limit",
        _cfg_get(raw, defaults, "gabagool_inventory_soft_limit"),
        float(defaults.get("gabagool_inventory_soft_limit", 2.0)),
    )
    gabagool_endgame_seconds = int(
        _cfg_get(raw, defaults, "gabagool_endgame_seconds", default=30)
    )
    gabagool_endgame_max_unpaired = _parse_nonnegative_float(
        "gabagool_endgame_max_unpaired",
        _cfg_get(raw, defaults, "gabagool_endgame_max_unpaired"),
        float(defaults.get("gabagool_endgame_max_unpaired", 1.0)),
    )
    if gabagool_min_time_to_resolution < 0:
        _fatal(f"{asset}:{window}: gabagool_min_time_to_resolution must be >= 0")
    if gabagool_price_buffer < 0 or gabagool_price_buffer > 0.05:
        _fatal(f"{asset}:{window}: gabagool_price_buffer must be between 0 and 0.05")
    if gabagool_min_profit_margin >= 1:
        _fatal(f"{asset}:{window}: gabagool_min_profit_margin must be < 1")
    if gabagool_inventory_soft_limit > gabagool_max_unpaired_shares + 1e-9:
        _fatal(
            f"{asset}:{window}: gabagool_inventory_soft_limit={gabagool_inventory_soft_limit} "
            f"cannot exceed gabagool_max_unpaired_shares={gabagool_max_unpaired_shares}"
        )
    if gabagool_endgame_seconds < 0:
        _fatal(f"{asset}:{window}: gabagool_endgame_seconds must be >= 0")
    if gabagool_endgame_max_unpaired > gabagool_max_unpaired_shares + 1e-9:
        _fatal(
            f"{asset}:{window}: gabagool_endgame_max_unpaired={gabagool_endgame_max_unpaired} "
            f"cannot exceed gabagool_max_unpaired_shares={gabagool_max_unpaired_shares}"
        )
    gabagool_falling_knife_enabled = _parse_bool_value(
        "gabagool_falling_knife_enabled",
        _cfg_get(raw, defaults, "gabagool_falling_knife_enabled", default=True),
        True,
    )
    gabagool_falling_knife_window_seconds = _parse_nonnegative_float(
        "gabagool_falling_knife_window_seconds",
        _cfg_get(raw, defaults, "gabagool_falling_knife_window_seconds", default=3.0),
        3.0,
    )
    gabagool_falling_knife_min_drop = _parse_nonnegative_float(
        "gabagool_falling_knife_min_drop",
        _cfg_get(raw, defaults, "gabagool_falling_knife_min_drop", default=0.035),
        0.035,
    )
    gabagool_falling_knife_fast_window_seconds = _parse_nonnegative_float(
        "gabagool_falling_knife_fast_window_seconds",
        _cfg_get(raw, defaults, "gabagool_falling_knife_fast_window_seconds", default=1.0),
        1.0,
    )
    gabagool_falling_knife_fast_drop = _parse_nonnegative_float(
        "gabagool_falling_knife_fast_drop",
        _cfg_get(raw, defaults, "gabagool_falling_knife_fast_drop", default=0.015),
        0.015,
    )
    gabagool_falling_knife_recovery_cents = _parse_nonnegative_float(
        "gabagool_falling_knife_recovery_cents",
        _cfg_get(raw, defaults, "gabagool_falling_knife_recovery_cents", default=1.5),
        1.5,
    )
    gabagool_falling_knife_cooldown_seconds = _parse_nonnegative_float(
        "gabagool_falling_knife_cooldown_seconds",
        _cfg_get(raw, defaults, "gabagool_falling_knife_cooldown_seconds", default=5.0),
        5.0,
    )
    gabagool_falling_knife_binance_confirm = _parse_nonnegative_float(
        "gabagool_falling_knife_binance_confirm",
        _cfg_get(raw, defaults, "gabagool_falling_knife_binance_confirm", default=0.10),
        0.10,
    )
    if gabagool_falling_knife_window_seconds <= 0:
        _fatal(f"{asset}:{window}: gabagool_falling_knife_window_seconds must be > 0")
    if gabagool_falling_knife_fast_window_seconds <= 0:
        _fatal(f"{asset}:{window}: gabagool_falling_knife_fast_window_seconds must be > 0")
    if gabagool_falling_knife_fast_window_seconds > gabagool_falling_knife_window_seconds:
        _fatal(f"{asset}:{window}: falling-knife fast window cannot exceed main window")
    if gabagool_falling_knife_min_drop <= 0 and gabagool_falling_knife_fast_drop <= 0:
        _fatal(f"{asset}:{window}: at least one falling-knife drop threshold must be > 0")
    if gabagool_max_pair_cost > 1.0 - gabagool_min_profit_margin + 1e-9:
        _fatal(
            f"{asset}:{window}: gabagool_max_pair_cost={gabagool_max_pair_cost} "
            f"must be <= 1 - gabagool_min_profit_margin={1.0 - gabagool_min_profit_margin:.4f}"
        )

    momentum_engine_enabled = _parse_bool_value("momentum_engine_enabled", _cfg_get(raw, defaults, "momentum_engine_enabled", default=True), True)
    momentum_min_delta = _parse_nonnegative_float("momentum_min_delta", _cfg_get(raw, defaults, "momentum_min_delta", default=0.0015), 0.0015)
    momentum_signal_max_age = _parse_nonnegative_float("momentum_signal_max_age", _cfg_get(raw, defaults, "momentum_signal_max_age", default=0.75), 0.75)
    momentum_execution_mode = str(_cfg_get(raw, defaults, "momentum_execution_mode", default="single_taker")).strip().lower()
    if momentum_execution_mode not in {"single_taker", "gtc_at_ask", "single_maker", "dual_hybrid"}:
        _fatal(f"{asset}:{window}: unsupported momentum_execution_mode={momentum_execution_mode!r}")
    momentum_max_position_delta = _parse_nonnegative_float("momentum_max_position_delta", _cfg_get(raw, defaults, "momentum_max_position_delta", default=5.0), 5.0)
    market_making_enabled = _parse_bool_value("market_making_enabled", _cfg_get(raw, defaults, "market_making_enabled", default=True), True)
    market_making_quote_size = _parse_order_size("market_making_quote_size", _cfg_get(raw, defaults, "market_making_quote_size", default=5.0), 5.0)
    market_making_min_spread = _parse_nonnegative_float("market_making_min_spread", _cfg_get(raw, defaults, "market_making_min_spread", default=0.01), 0.01)
    market_making_quote_offset = _parse_nonnegative_float("market_making_quote_offset", _cfg_get(raw, defaults, "market_making_quote_offset", default=0.01), 0.01)
    market_making_max_position_delta = _parse_nonnegative_float("market_making_max_position_delta", _cfg_get(raw, defaults, "market_making_max_position_delta", default=5.0), 5.0)
    market_making_requote_seconds = _parse_nonnegative_float("market_making_requote_seconds", _cfg_get(raw, defaults, "market_making_requote_seconds", default=0.40), 0.40)
    spread_capture_enabled = _parse_bool_value("spread_capture_enabled", _cfg_get(raw, defaults, "spread_capture_enabled", default=True), True)
    spread_capture_trigger_sum = _parse_nonnegative_float("spread_capture_trigger_sum", _cfg_get(raw, defaults, "spread_capture_trigger_sum", default=0.995), 0.995)
    spread_capture_max_entry_sum = _parse_nonnegative_float("spread_capture_max_entry_sum", _cfg_get(raw, defaults, "spread_capture_max_entry_sum", default=0.985), 0.985)
    spread_capture_max_unpaired = _parse_nonnegative_float("spread_capture_max_unpaired", _cfg_get(raw, defaults, "spread_capture_max_unpaired", default=1.0), 1.0)
    spread_capture_min_net_profit = _parse_nonnegative_float(
        "spread_capture_min_net_profit",
        _cfg_get(raw, defaults, "spread_capture_min_net_profit", default=0.005),
        0.005,
    )
    spread_capture_fee_rate = _parse_nonnegative_float(
        "spread_capture_fee_rate",
        _cfg_get(raw, defaults, "spread_capture_fee_rate", default=0.0),
        0.0,
    )
    spread_capture_slippage_buffer = _parse_nonnegative_float(
        "spread_capture_slippage_buffer",
        _cfg_get(raw, defaults, "spread_capture_slippage_buffer", default=0.002),
        0.002,
    )
    spread_capture_min_book_liquidity = _parse_order_size(
        "spread_capture_min_book_liquidity",
        _cfg_get(raw, defaults, "spread_capture_min_book_liquidity", default=5.0),
        5.0,
    )
    spread_capture_max_data_age_seconds = _parse_nonnegative_float(
        "spread_capture_max_data_age_seconds",
        _cfg_get(raw, defaults, "spread_capture_max_data_age_seconds", default=3.0),
        3.0,
    )
    spread_capture_second_leg_timeout_ms = _parse_cooldown_ms(
        "spread_capture_second_leg_timeout_ms",
        _cfg_get(raw, defaults, "spread_capture_second_leg_timeout_ms", default=10000),
        10000,
    )
    spread_capture_max_recovery_loss = _parse_nonnegative_float(
        "spread_capture_max_recovery_loss",
        _cfg_get(raw, defaults, "spread_capture_max_recovery_loss", default=2.0),
        2.0,
    )
    spread_capture_release_blocked_to_eligible = _parse_bool_value(
        "spread_capture_release_blocked_to_eligible",
        _cfg_get(raw, defaults, "spread_capture_release_blocked_to_eligible", default=False),
        False,
    )
    spread_capture_binance_volatility_block = _parse_bool_value(
        "spread_capture_binance_volatility_block",
        _cfg_get(raw, defaults, "spread_capture_binance_volatility_block", default=True),
        True,
    )
    inventory_accumulation_enabled = _parse_bool_value(
        "inventory_accumulation_enabled",
        _cfg_get(raw, defaults, "inventory_accumulation_enabled", default=True),
        True,
    )
    inventory_max_pair_cost = _parse_unit_fraction(
        "inventory_max_pair_cost",
        _cfg_get(raw, defaults, "inventory_max_pair_cost", default=gabagool_max_pair_cost),
        gabagool_max_pair_cost,
    )
    inventory_min_net_edge = _parse_nonnegative_float(
        "inventory_min_net_edge",
        _cfg_get(raw, defaults, "inventory_min_net_edge", default=gabagool_min_profit_margin),
        gabagool_min_profit_margin,
    )
    inventory_max_capital_per_market = _parse_nonnegative_float(
        "inventory_max_capital_per_market",
        _cfg_get(raw, defaults, "inventory_max_capital_per_market", default=40.0),
        40.0,
    )
    inventory_max_dca_attempts = int(
        _cfg_get(raw, defaults, "inventory_max_dca_attempts", default=8) or 8
    )
    inventory_dca_cooldown_seconds = _parse_nonnegative_float(
        "inventory_dca_cooldown_seconds",
        _cfg_get(raw, defaults, "inventory_dca_cooldown_seconds", default=60.0),
        60.0,
    )
    inventory_dca_min_price_move = _parse_nonnegative_float(
        "inventory_dca_min_price_move",
        _cfg_get(raw, defaults, "inventory_dca_min_price_move", default=0.03),
        0.03,
    )
    inventory_max_imbalance_seconds = _parse_nonnegative_float(
        "inventory_max_imbalance_seconds",
        _cfg_get(raw, defaults, "inventory_max_imbalance_seconds", default=7200.0),
        7200.0,
    )
    inventory_max_active_orders = int(
        _cfg_get(raw, defaults, "inventory_max_active_orders", default=4) or 4
    )
    min_net_profit_per_pair = _parse_nonnegative_float(
        "min_net_profit_per_pair",
        os.getenv(
            "MIN_NET_PROFIT_PER_PAIR",
            _cfg_get(
                raw,
                defaults,
                "min_net_profit_per_pair",
                "inventory_min_net_edge",
                "gabagool_min_profit_margin",
                default=0.02,
            ),
        ),
        0.02,
    )
    force_hedge_after_timeout = _parse_bool_value(
        "force_hedge_after_timeout",
        os.getenv(
            "FORCE_HEDGE_AFTER_TIMEOUT",
            _cfg_get(raw, defaults, "force_hedge_after_timeout", default=False),
        ),
        False,
    )
    max_consecutive_buys_raw = _cfg_get(
        raw, defaults, "max_consecutive_buys_per_outcome", default=3,
    )
    try:
        max_consecutive_buys_per_outcome = int(max_consecutive_buys_raw)
    except (TypeError, ValueError):
        _fatal(f"max_consecutive_buys_per_outcome={max_consecutive_buys_raw!r} is not a valid integer")
    if max_consecutive_buys_per_outcome < 0:
        _fatal(f"{asset}:{window}: max_consecutive_buys_per_outcome must be >= 0")
    stabilization_window_seconds = _parse_nonnegative_float(
        "stabilization_window_seconds",
        _cfg_get(raw, defaults, "stabilization_window_seconds", default=15.0),
        15.0,
    )
    stabilization_tolerance_percent = _parse_nonnegative_float(
        "stabilization_tolerance_percent",
        _cfg_get(raw, defaults, "stabilization_tolerance_percent", default=0.02),
        0.02,
    )
    min_orderbook_depth = _parse_order_size(
        "min_orderbook_depth",
        os.getenv(
            "MIN_ORDERBOOK_DEPTH",
            _cfg_get(
                raw,
                defaults,
                "min_orderbook_depth",
                "spread_capture_min_book_liquidity",
                default=5.0,
            ),
        ),
        5.0,
    )
    falling_knife_filter_enabled = _parse_bool_value(
        "falling_knife_filter_enabled",
        os.getenv(
            "FALLING_KNIFE_FILTER_ENABLED",
            _cfg_get(
                raw,
                defaults,
                "falling_knife_filter_enabled",
                "gabagool_falling_knife_enabled",
                default=True,
            ),
        ),
        True,
    )
    price_drop_window_seconds = _parse_nonnegative_float(
        "price_drop_window_seconds",
        os.getenv(
            "PRICE_DROP_WINDOW_SECONDS",
            _cfg_get(
                raw,
                defaults,
                "price_drop_window_seconds",
                "gabagool_falling_knife_window_seconds",
                default=15.0,
            ),
        ),
        15.0,
    )
    max_price_drop_percent = _parse_nonnegative_float(
        "max_price_drop_percent",
        os.getenv(
            "MAX_PRICE_DROP_PERCENT",
            _cfg_get(raw, defaults, "max_price_drop_percent", default=3.5),
        ),
        3.5,
    )
    falling_knife_cooldown_seconds = _parse_nonnegative_float(
        "falling_knife_cooldown_seconds",
        os.getenv(
            "FALLING_KNIFE_COOLDOWN_SECONDS",
            _cfg_get(
                raw,
                defaults,
                "falling_knife_cooldown_seconds",
                "gabagool_falling_knife_cooldown_seconds",
                default=5.0,
            ),
        ),
        5.0,
    )
    if price_drop_window_seconds <= 0:
        _fatal(f"{asset}:{window}: price_drop_window_seconds must be > 0")
    if stabilization_window_seconds <= 0:
        _fatal(f"{asset}:{window}: stabilization_window_seconds must be > 0")
    from utils.btc_signal import parse_signal_windows_ms

    binance_fok_enabled = _parse_bool_value(
        "binance_fok_enabled",
        _cfg_get(raw, defaults, "binance_fok_enabled", default=True),
        True,
    )
    windows_raw = _cfg_get(
        raw, defaults, "binance_fok_signal_windows_ms", "btc_signal_window_ms",
        default=[1000, 3000, 5000],
    )
    binance_fok_signal_windows_ms = tuple(parse_signal_windows_ms(windows_raw))
    binance_fok_up_move_threshold_pct = _parse_nonnegative_float(
        "binance_fok_up_move_threshold_pct",
        _cfg_get(raw, defaults, "binance_fok_up_move_threshold_pct", "btc_up_move_threshold_pct", default=0.0008),
        0.0008,
    )
    binance_fok_down_move_threshold_pct = _parse_nonnegative_float(
        "binance_fok_down_move_threshold_pct",
        _cfg_get(raw, defaults, "binance_fok_down_move_threshold_pct", "btc_down_move_threshold_pct", default=0.0008),
        0.0008,
    )
    binance_fok_signal_cooldown_ms = _parse_nonnegative_float(
        "binance_fok_signal_cooldown_ms",
        _cfg_get(raw, defaults, "binance_fok_signal_cooldown_ms", "signal_cooldown_ms", default=3000.0),
        3000.0,
    )
    binance_fok_max_signal_latency_ms = _parse_nonnegative_float(
        "binance_fok_max_signal_latency_ms",
        _cfg_get(raw, defaults, "binance_fok_max_signal_latency_ms", "max_signal_latency_ms", default=500.0),
        500.0,
    )
    binance_fok_max_binance_data_age_ms = _parse_nonnegative_float(
        "binance_fok_max_binance_data_age_ms",
        _cfg_get(raw, defaults, "binance_fok_max_binance_data_age_ms", "max_binance_data_age_ms", default=1500.0),
        1500.0,
    )
    binance_fok_max_combined_pair_cost = _parse_unit_fraction(
        "binance_fok_max_combined_pair_cost",
        _cfg_get(raw, defaults, "binance_fok_max_combined_pair_cost", "max_combined_pair_cost", default=0.985),
        0.985,
    )
    binance_fok_min_net_pair_edge = _parse_nonnegative_float(
        "binance_fok_min_net_pair_edge",
        _cfg_get(raw, defaults, "binance_fok_min_net_pair_edge", "min_net_pair_edge", default=0.01),
        0.01,
    )
    binance_fok_max_entry_price = _parse_unit_fraction(
        "binance_fok_max_entry_price",
        _cfg_get(raw, defaults, "binance_fok_max_entry_price", default=0.85),
        0.85,
    )
    binance_fok_max_hedge_price = _parse_unit_fraction(
        "binance_fok_max_hedge_price",
        _cfg_get(raw, defaults, "binance_fok_max_hedge_price", "max_hedge_price", default=0.85),
        0.85,
    )
    binance_fok_max_slippage = _parse_nonnegative_float(
        "binance_fok_max_slippage",
        _cfg_get(raw, defaults, "binance_fok_max_slippage", "max_slippage", default=0.005),
        0.005,
    )
    binance_fok_max_hedge_delay_ms = _parse_nonnegative_float(
        "binance_fok_max_hedge_delay_ms",
        _cfg_get(raw, defaults, "binance_fok_max_hedge_delay_ms", "max_hedge_delay_ms", default=2500.0),
        2500.0,
    )
    binance_fok_max_unhedged_exposure_usd = _parse_nonnegative_float(
        "binance_fok_max_unhedged_exposure_usd",
        _cfg_get(raw, defaults, "binance_fok_max_unhedged_exposure_usd", default=15.0),
        15.0,
    )
    binance_fok_max_capital_per_trade = _parse_nonnegative_float(
        "binance_fok_max_capital_per_trade",
        _cfg_get(raw, defaults, "binance_fok_max_capital_per_trade", default=10.0),
        10.0,
    )
    preemptive_cancel_enabled = _parse_bool_value("preemptive_cancel_enabled", _cfg_get(raw, defaults, "preemptive_cancel_enabled", default=True), True)
    preemptive_cancel_imbalance = _parse_nonnegative_float("preemptive_cancel_imbalance", _cfg_get(raw, defaults, "preemptive_cancel_imbalance", default=0.18), 0.18)
    preemptive_cancel_momentum = _parse_nonnegative_float("preemptive_cancel_momentum", _cfg_get(raw, defaults, "preemptive_cancel_momentum", default=0.10), 0.10)
    preemptive_cancel_price_delta = _parse_nonnegative_float("preemptive_cancel_price_delta", _cfg_get(raw, defaults, "preemptive_cancel_price_delta", default=0.0015), 0.0015)

    trade_cooldown_ms = _parse_cooldown_ms(
        "trade_cooldown_ms",
        _cfg_get(raw, defaults, "trade_cooldown_ms"),
        int(defaults.get("trade_cooldown_ms", 3000)),
    )

    order_size_fixed = _parse_order_size(
        "order_size",
        _cfg_get(raw, defaults, "order_size", "spread_size"),
        float(_cfg_get(defaults, {}, "order_size", "spread_size", default=5.0)),
    )
    size_min_raw = _cfg_get(raw, defaults, "order_size_min", "spread_size_min")
    size_max_raw = _cfg_get(raw, defaults, "order_size_max", "spread_size_max")
    if size_min_raw is None and size_max_raw is None:
        order_size_min = order_size_max = order_size_fixed
    else:
        order_size_min = _parse_order_size(
            "order_size_min",
            size_min_raw if size_min_raw is not None else order_size_fixed,
            order_size_fixed,
        )
        order_size_max = _parse_order_size(
            "order_size_max",
            size_max_raw if size_max_raw is not None else order_size_fixed,
            order_size_fixed,
        )
    max_order = _parse_order_size(
        "max_order_size",
        _cfg_get(raw, defaults, "max_order_size"),
        float(defaults.get("max_order_size", 5.0)),
    )
    max_shares = _parse_max_shares(
        "max_shares",
        _cfg_get(raw, defaults, "max_shares"),
        float(defaults.get("max_shares", 20.0)),
    )

    if ENV_SIZING_OVERRIDES:
        order_size_min = ENV_SIZING_OVERRIDES["order_size_min"]
        order_size_max = ENV_SIZING_OVERRIDES["order_size_max"]
        max_shares = ENV_SIZING_OVERRIDES["max_shares"]
        if "max_order_size" in ENV_SIZING_OVERRIDES:
            max_order = ENV_SIZING_OVERRIDES["max_order_size"]

    if order_size_min > order_size_max:
        _fatal(
            f"{asset}:{window}: order_size_min ({order_size_min}) "
            f"cannot exceed order_size_max ({order_size_max})"
        )
    max_order = max(max_order, order_size_max)
    if max_order > max_shares:
        _fatal(
            f"{asset}:{window}: max_order_size ({max_order}) "
            f"cannot exceed max_shares ({max_shares})"
        )

    dr_raw = _cfg_get(raw, defaults, "dry_run")
    if dr_raw is None:
        dry_run = DRY_RUN_DEFAULT
    else:
        dry_run = bool(dr_raw)

    dry_min = _parse_cooldown_ms(
        "dry_run_fill_delay_min_ms",
        _cfg_get(raw, defaults, "dry_run_fill_delay_min_ms"),
        int(defaults.get("dry_run_fill_delay_min_ms", 200)),
    )
    dry_max = _parse_cooldown_ms(
        "dry_run_fill_delay_max_ms",
        _cfg_get(raw, defaults, "dry_run_fill_delay_max_ms"),
        int(defaults.get("dry_run_fill_delay_max_ms", 2500)),
    )
    if dry_max < dry_min:
        _fatal(f"{asset}:{window}: dry_run_fill_delay_max_ms must be >= dry_run_fill_delay_min_ms")

    interval = WINDOW_SECONDS[window]
    listener_raw = _cfg_get(raw, defaults, "listener_activate_secs")
    entry_raw = _cfg_get(raw, defaults, "entry_seconds_left")
    env_listener = os.getenv("LISTENER_ACTIVATE_SECONDS", "").strip()
    env_entry = os.getenv("ENTRY_SECONDS_LEFT", "").strip()
    if listener_raw is not None:
        listener_secs = int(listener_raw)
    elif env_listener:
        listener_secs = int(env_listener)
    else:
        listener_secs = interval
    if entry_raw is not None:
        entry_secs = int(entry_raw)
    elif env_entry:
        entry_secs = int(env_entry)
    else:
        entry_secs = interval

    min_entry_raw = _cfg_get(raw, defaults, "min_entry_seconds_left")
    fill_timeout_raw = _cfg_get(
        raw, defaults, "fill_timeout_ms", "spread_fill_timeout_ms",
    )
    fill_poll_raw = _cfg_get(raw, defaults, "fill_poll_ms", "spread_fill_poll_ms")

    min_entry_secs = int(min_entry_raw if min_entry_raw is not None else 0)
    fill_timeout_ms = _parse_cooldown_ms(
        "fill_timeout_ms",
        fill_timeout_raw,
        int(_cfg_get(defaults, {}, "fill_timeout_ms", "spread_fill_timeout_ms", default=10000)),
    )
    fill_poll_ms = _parse_cooldown_ms(
        "fill_poll_ms",
        fill_poll_raw,
        int(_cfg_get(defaults, {}, "fill_poll_ms", "spread_fill_poll_ms", default=400)),
    )

    if min_entry_secs < 0:
        _fatal(f"{asset}:{window}: min_entry_seconds_left must be >= 0")
    if fill_poll_ms > fill_timeout_ms:
        _fatal(f"{asset}:{window}: fill_poll_ms must be <= fill_timeout_ms")

    momentum_strategy_enabled = _parse_bool_value(
        "momentum_strategy_enabled",
        _cfg_get(raw, defaults, "momentum_strategy_enabled", default=True),
        True,
    )
    live_trading_enabled = _parse_bool_value(
        "live_trading_enabled",
        os.getenv(
            "LIVE_TRADING_ENABLED",
            _cfg_get(raw, defaults, "live_trading_enabled", default=False),
        ),
        False,
    )
    momentum_max_entry_price = _parse_unit_fraction(
        "momentum_max_entry_price",
        _cfg_get(raw, defaults, "momentum_max_entry_price", "max_entry_price", default=0.92),
        0.92,
    )
    min_net_edge = _parse_nonnegative_float(
        "min_net_edge",
        _cfg_get(raw, defaults, "min_net_edge", "inventory_min_net_edge", default=0.05),
        0.05,
    )
    if min_net_edge >= 1:
        _fatal(f"{asset}:{window}: min_net_edge must be < 1")
    windows_raw = _cfg_get(raw, defaults, "momentum_windows_seconds", default=[5, 15, 30, 60, 120])
    if isinstance(windows_raw, str):
        windows_raw = [p.strip() for p in windows_raw.split(",") if p.strip()]
    momentum_windows_seconds = tuple(int(float(w)) for w in (windows_raw or []))
    if not momentum_windows_seconds or any(w <= 0 for w in momentum_windows_seconds):
        _fatal(f"{asset}:{window}: momentum_windows_seconds must be positive integers")
    min_momentum_strength = _parse_nonnegative_float(
        "min_momentum_strength",
        _cfg_get(raw, defaults, "min_momentum_strength", default=0.75),
        0.75,
    )
    momentum_volatility_cap = _parse_nonnegative_float(
        "momentum_volatility_cap",
        _cfg_get(raw, defaults, "momentum_volatility_cap", default=0.08),
        0.08,
    )
    volume_confirmation_threshold = _parse_nonnegative_float(
        "volume_confirmation_threshold",
        _cfg_get(raw, defaults, "volume_confirmation_threshold", default=0.05),
        0.05,
    )
    momentum_reversal_exit_enabled = _parse_bool_value(
        "momentum_reversal_exit_enabled",
        _cfg_get(raw, defaults, "momentum_reversal_exit_enabled", default=True),
        True,
    )
    momentum_reversal_confirm_strength = _parse_nonnegative_float(
        "momentum_reversal_confirm_strength",
        _cfg_get(raw, defaults, "momentum_reversal_confirm_strength", default=0.6),
        0.6,
    )
    momentum_reversal_confirmations = int(
        _cfg_get(raw, defaults, "momentum_reversal_confirmations", default=2) or 2
    )
    if momentum_reversal_confirmations < 1:
        _fatal(f"{asset}:{window}: momentum_reversal_confirmations must be >= 1")
    max_holding_time_seconds = _parse_nonnegative_float(
        "max_holding_time_seconds",
        _cfg_get(raw, defaults, "max_holding_time_seconds", default=240.0),
        240.0,
    )
    estimated_fee_rate = _parse_nonnegative_float(
        "estimated_fee_rate",
        _cfg_get(raw, defaults, "estimated_fee_rate", "spread_capture_fee_rate", default=0.0),
        0.0,
    )
    execution_slippage_buffer = _parse_nonnegative_float(
        "execution_slippage_buffer",
        _cfg_get(
            raw,
            defaults,
            "execution_slippage_buffer",
            "spread_capture_slippage_buffer",
            default=0.02,
        ),
        0.02,
    )
    external_max_data_age_seconds = _parse_nonnegative_float(
        "external_max_data_age_seconds",
        _cfg_get(
            raw,
            defaults,
            "external_max_data_age_seconds",
            "binance_fok_max_binance_data_age_ms",
            default=3.0,
        ),
        3.0,
    )
    if external_max_data_age_seconds > 120:
        external_max_data_age_seconds /= 1000.0

    take_profit_pct_raw = _cfg_get(raw, defaults, "take_profit_pct", default=None)
    if take_profit_pct_raw is None:
        take_profit_pct = 0.10
    else:
        take_profit_pct = _parse_nonnegative_float(
            "take_profit_pct",
            take_profit_pct_raw,
            0.10,
        )
    if take_profit_pct > 1.0:
        take_profit_pct /= 100.0
    if take_profit_pct <= 0 or take_profit_pct >= 1 or take_profit_pct != take_profit_pct:
        _fatal(f"{asset}:{window}: take_profit_pct must be in (0, 1) (got {take_profit_pct_raw!r})")

    take_profit_delta = _parse_nonnegative_float(
        "take_profit_delta",
        _cfg_get(raw, defaults, "take_profit_delta", default=0.08),
        0.08,
    )

    min_signal_quality_score = _parse_nonnegative_float(
        "min_signal_quality_score",
        _cfg_get(raw, defaults, "min_signal_quality_score", default=60.0),
        60.0,
    )
    if min_signal_quality_score > 100:
        _fatal(f"{asset}:{window}: min_signal_quality_score must be <= 100")
    gate_windows_raw = _cfg_get(raw, defaults, "signal_gate_windows_seconds", default=[5, 15, 30])
    if isinstance(gate_windows_raw, str):
        gate_windows_raw = [p.strip() for p in gate_windows_raw.split(",") if p.strip()]
    signal_gate_windows_seconds = tuple(int(float(w)) for w in (gate_windows_raw or [5, 15, 30]))
    signal_require_timeframe_agreement = _parse_bool_value(
        "signal_require_timeframe_agreement",
        _cfg_get(raw, defaults, "signal_require_timeframe_agreement", default=False),
        False,
    )
    momentum_trigger_window_seconds = int(
        _cfg_get(raw, defaults, "momentum_trigger_window_seconds", default=5) or 5
    )
    momentum_confirm_window_seconds = int(
        _cfg_get(raw, defaults, "momentum_confirm_window_seconds", default=15) or 15
    )
    momentum_support_window_seconds = int(
        _cfg_get(raw, defaults, "momentum_support_window_seconds", default=30) or 30
    )
    momentum_context_window_seconds = int(
        _cfg_get(raw, defaults, "momentum_context_window_seconds", default=60) or 60
    )
    momentum_trigger_min_abs_return = _parse_nonnegative_float(
        "momentum_trigger_min_abs_return",
        _cfg_get(raw, defaults, "momentum_trigger_min_abs_return", default=0.00005),
        0.00005,
    )
    momentum_confirm_tolerance = _parse_nonnegative_float(
        "momentum_confirm_tolerance",
        _cfg_get(raw, defaults, "momentum_confirm_tolerance", default=0.0001),
        0.0001,
    )
    momentum_support_opposing_tolerance = _parse_nonnegative_float(
        "momentum_support_opposing_tolerance",
        _cfg_get(raw, defaults, "momentum_support_opposing_tolerance", default=0.002),
        0.002,
    )
    momentum_context_reversal_min_abs_return = _parse_nonnegative_float(
        "momentum_context_reversal_min_abs_return",
        _cfg_get(raw, defaults, "momentum_context_reversal_min_abs_return", default=0.004),
        0.004,
    )
    momentum_require_volume_confirmation = _parse_bool_value(
        "momentum_require_volume_confirmation",
        _cfg_get(raw, defaults, "momentum_require_volume_confirmation", default=False),
        False,
    )
    matched_pair_accumulation_enabled = _parse_bool_value(
        "matched_pair_accumulation_enabled",
        _cfg_get(raw, defaults, "matched_pair_accumulation_enabled", default=False),
        False,
    )
    signal_max_spread = _parse_nonnegative_float(
        "signal_max_spread",
        _cfg_get(raw, defaults, "signal_max_spread", default=0.06),
        0.06,
    )
    signal_max_slippage = _parse_nonnegative_float(
        "signal_max_slippage",
        _cfg_get(raw, defaults, "signal_max_slippage", default=0.03),
        0.03,
    )
    signal_max_token_move_since_open = _parse_nonnegative_float(
        "signal_max_token_move_since_open",
        _cfg_get(raw, defaults, "signal_max_token_move_since_open", default=0.12),
        0.12,
    )
    signal_min_persistence_ratio = _parse_nonnegative_float(
        "signal_min_persistence_ratio",
        _cfg_get(raw, defaults, "signal_min_persistence_ratio", default=0.55),
        0.55,
    )
    if signal_min_persistence_ratio > 1:
        _fatal(f"{asset}:{window}: signal_min_persistence_ratio must be <= 1")
    signal_reversal_acceleration_threshold = float(
        _cfg_get(raw, defaults, "signal_reversal_acceleration_threshold", default=-0.0005)
    )
    weights_raw = _cfg_get(raw, defaults, "signal_quality_weights", default={}) or {}
    default_weights = {
        "momentum_alignment": 25.0,
        "persistence_acceleration": 20.0,
        "net_edge": 25.0,
        "liquidity_spread": 15.0,
        "reference_time": 15.0,
    }
    signal_quality_weights = {
        k: float(weights_raw.get(k, default_weights[k])) for k in default_weights
    }

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).lower() in ("1", "true", "yes", "on")

    return WorkerConfig(
        asset=asset,
        window=window,
        momentum_entry_threshold=momentum_entry_threshold,
        stop_loss_pct=stop_loss_pct,
        entry_price=entry_price,
        price_99_entry_window_seconds=price_99_entry_window_seconds,
        inventory_accumulation_enabled=inventory_accumulation_enabled,
        cheap_side_min_price=cheap_side_min_price,
        cheap_side_max_price=cheap_side_max_price,
        target_combined_pair_cost=target_combined_pair_cost,
        max_combined_pair_cost=max_combined_pair_cost,
        max_entries_per_side_per_market=max_entries_per_side_per_market,
        no_buys_last_minutes=no_buys_last_minutes,
        cheap_entry_price=cheap_entry_price,
        initial_hedge_delay_seconds=initial_hedge_delay_seconds,
        initial_hedge_timeout_seconds=initial_hedge_timeout_seconds,
        combined_cost_target=combined_cost_target,
        max_combined_cost=max_combined_cost,
        entry_window_seconds=entry_window_seconds,
        max_entry_price=max_entry_price,
        order_type=order_type,
        max_slippage=max_slippage,
        max_hedge_retries=max_hedge_retries,
        hedge_retry_delay_ms=hedge_retry_delay_ms,
        max_market_exposure=max_market_exposure,
        max_position_size=max_position_size,
        max_unmatched_yes=max_unmatched_yes,
        max_unmatched_no=max_unmatched_no,
        max_share_imbalance=max_share_imbalance,
        max_unmatched_shares=max_unmatched_shares,
        balance_inventory_before_buy=balance_inventory_before_buy,
        account_for_pending_buy_orders=account_for_pending_buy_orders,
        allow_unmatched_accumulation=allow_unmatched_accumulation,
        min_order_size=min_order_size,
        late_entry_enabled=late_entry_enabled,
        entry_price_threshold=entry_price,
        late_entry_max_slippage=late_entry_max_slippage,
        max_quote_age_seconds=max_quote_age_seconds,
        min_time_before_close_seconds=min_time_before_close_seconds,
        stop_loss_exit_max_retries=stop_loss_exit_max_retries,
        stop_loss_exit_retry_ms=stop_loss_exit_retry_ms,
        gabagool_initial_entry_threshold=gabagool_initial_entry_threshold,
        gabagool_max_pair_cost=gabagool_max_pair_cost,
        gabagool_min_profit_margin=gabagool_min_profit_margin,
        gabagool_max_unpaired_shares=gabagool_max_unpaired_shares,
        gabagool_min_time_to_resolution=gabagool_min_time_to_resolution,
        gabagool_price_buffer=gabagool_price_buffer,
        gabagool_inventory_soft_limit=gabagool_inventory_soft_limit,
        gabagool_endgame_seconds=gabagool_endgame_seconds,
        gabagool_endgame_max_unpaired=gabagool_endgame_max_unpaired,
        gabagool_falling_knife_enabled=gabagool_falling_knife_enabled,
        gabagool_falling_knife_window_seconds=gabagool_falling_knife_window_seconds,
        gabagool_falling_knife_min_drop=gabagool_falling_knife_min_drop,
        gabagool_falling_knife_fast_window_seconds=gabagool_falling_knife_fast_window_seconds,
        gabagool_falling_knife_fast_drop=gabagool_falling_knife_fast_drop,
        gabagool_falling_knife_recovery_cents=gabagool_falling_knife_recovery_cents,
        gabagool_falling_knife_cooldown_seconds=gabagool_falling_knife_cooldown_seconds,
        gabagool_falling_knife_binance_confirm=gabagool_falling_knife_binance_confirm,
        momentum_engine_enabled=momentum_engine_enabled,
        momentum_min_delta=momentum_min_delta,
        momentum_signal_max_age=momentum_signal_max_age,
        momentum_execution_mode=momentum_execution_mode,
        momentum_max_position_delta=momentum_max_position_delta,
        market_making_enabled=market_making_enabled,
        market_making_quote_size=market_making_quote_size,
        market_making_min_spread=market_making_min_spread,
        market_making_quote_offset=market_making_quote_offset,
        market_making_max_position_delta=market_making_max_position_delta,
        market_making_requote_seconds=market_making_requote_seconds,
        spread_capture_enabled=spread_capture_enabled,
        spread_capture_trigger_sum=spread_capture_trigger_sum,
        spread_capture_max_entry_sum=spread_capture_max_entry_sum,
        spread_capture_max_unpaired=spread_capture_max_unpaired,
        spread_capture_min_net_profit=spread_capture_min_net_profit,
        spread_capture_fee_rate=spread_capture_fee_rate,
        spread_capture_slippage_buffer=spread_capture_slippage_buffer,
        spread_capture_min_book_liquidity=spread_capture_min_book_liquidity,
        spread_capture_max_data_age_seconds=spread_capture_max_data_age_seconds,
        spread_capture_second_leg_timeout_ms=spread_capture_second_leg_timeout_ms,
        spread_capture_max_recovery_loss=spread_capture_max_recovery_loss,
        spread_capture_release_blocked_to_eligible=spread_capture_release_blocked_to_eligible,
        spread_capture_binance_volatility_block=spread_capture_binance_volatility_block,
        inventory_max_pair_cost=inventory_max_pair_cost,
        inventory_min_net_edge=inventory_min_net_edge,
        inventory_max_capital_per_market=inventory_max_capital_per_market,
        inventory_max_dca_attempts=inventory_max_dca_attempts,
        inventory_dca_cooldown_seconds=inventory_dca_cooldown_seconds,
        inventory_dca_min_price_move=inventory_dca_min_price_move,
        inventory_max_imbalance_seconds=inventory_max_imbalance_seconds,
        inventory_max_active_orders=inventory_max_active_orders,
        min_net_profit_per_pair=min_net_profit_per_pair,
        force_hedge_after_timeout=force_hedge_after_timeout,
        max_consecutive_buys_per_outcome=max_consecutive_buys_per_outcome,
        stabilization_window_seconds=stabilization_window_seconds,
        stabilization_tolerance_percent=stabilization_tolerance_percent,
        min_orderbook_depth=min_orderbook_depth,
        falling_knife_filter_enabled=falling_knife_filter_enabled,
        price_drop_window_seconds=price_drop_window_seconds,
        max_price_drop_percent=max_price_drop_percent,
        falling_knife_cooldown_seconds=falling_knife_cooldown_seconds,
        binance_fok_enabled=binance_fok_enabled,
        binance_fok_signal_windows_ms=binance_fok_signal_windows_ms,
        binance_fok_up_move_threshold_pct=binance_fok_up_move_threshold_pct,
        binance_fok_down_move_threshold_pct=binance_fok_down_move_threshold_pct,
        binance_fok_signal_cooldown_ms=binance_fok_signal_cooldown_ms,
        binance_fok_max_signal_latency_ms=binance_fok_max_signal_latency_ms,
        binance_fok_max_binance_data_age_ms=binance_fok_max_binance_data_age_ms,
        binance_fok_max_combined_pair_cost=binance_fok_max_combined_pair_cost,
        binance_fok_min_net_pair_edge=binance_fok_min_net_pair_edge,
        binance_fok_max_entry_price=binance_fok_max_entry_price,
        binance_fok_max_hedge_price=binance_fok_max_hedge_price,
        binance_fok_max_slippage=binance_fok_max_slippage,
        binance_fok_max_hedge_delay_ms=binance_fok_max_hedge_delay_ms,
        binance_fok_max_unhedged_exposure_usd=binance_fok_max_unhedged_exposure_usd,
        binance_fok_max_capital_per_trade=binance_fok_max_capital_per_trade,
        preemptive_cancel_enabled=preemptive_cancel_enabled,
        preemptive_cancel_imbalance=preemptive_cancel_imbalance,
        preemptive_cancel_momentum=preemptive_cancel_momentum,
        preemptive_cancel_price_delta=preemptive_cancel_price_delta,
        trade_cooldown_ms=trade_cooldown_ms,
        order_size_min=order_size_min,
        order_size_max=order_size_max,
        max_order_size=max_order,
        max_shares=max_shares,
        dry_run=dry_run,
        dry_run_fill_delay_min_ms=dry_min,
        dry_run_fill_delay_max_ms=dry_max,
        listener_activate_secs=listener_secs,
        entry_seconds_left=entry_secs,
        min_entry_seconds_left=min_entry_secs,
        fill_timeout_ms=fill_timeout_ms,
        fill_poll_ms=fill_poll_ms,
        momentum_strategy_enabled=momentum_strategy_enabled,
        live_trading_enabled=live_trading_enabled,
        momentum_max_entry_price=momentum_max_entry_price,
        min_net_edge=min_net_edge,
        momentum_windows_seconds=momentum_windows_seconds,
        min_momentum_strength=min_momentum_strength,
        momentum_volatility_cap=momentum_volatility_cap,
        volume_confirmation_threshold=volume_confirmation_threshold,
        momentum_reversal_exit_enabled=momentum_reversal_exit_enabled,
        momentum_reversal_confirm_strength=momentum_reversal_confirm_strength,
        momentum_reversal_confirmations=momentum_reversal_confirmations,
        max_holding_time_seconds=max_holding_time_seconds,
        estimated_fee_rate=estimated_fee_rate,
        execution_slippage_buffer=execution_slippage_buffer,
        external_max_data_age_seconds=external_max_data_age_seconds,
        take_profit_pct=take_profit_pct,
        take_profit_delta=take_profit_delta,
        min_signal_quality_score=min_signal_quality_score,
        signal_gate_windows_seconds=signal_gate_windows_seconds,
        signal_require_timeframe_agreement=signal_require_timeframe_agreement,
        signal_max_spread=signal_max_spread,
        signal_max_slippage=signal_max_slippage,
        signal_max_token_move_since_open=signal_max_token_move_since_open,
        signal_min_persistence_ratio=signal_min_persistence_ratio,
        signal_reversal_acceleration_threshold=signal_reversal_acceleration_threshold,
        signal_quality_weights=signal_quality_weights,
        momentum_trigger_window_seconds=momentum_trigger_window_seconds,
        momentum_confirm_window_seconds=momentum_confirm_window_seconds,
        momentum_support_window_seconds=momentum_support_window_seconds,
        momentum_context_window_seconds=momentum_context_window_seconds,
        momentum_trigger_min_abs_return=momentum_trigger_min_abs_return,
        momentum_confirm_tolerance=momentum_confirm_tolerance,
        momentum_support_opposing_tolerance=momentum_support_opposing_tolerance,
        momentum_context_reversal_min_abs_return=momentum_context_reversal_min_abs_return,
        momentum_require_volume_confirmation=momentum_require_volume_confirmation,
        matched_pair_accumulation_enabled=matched_pair_accumulation_enabled,
        enabled=enabled,
    )


def load_worker_configs(path: Optional[str] = None) -> Tuple[WorkerConfig, ...]:
    cfg_path = path or os.getenv("TRADING_CONFIG_PATH", "trading_config.json")
    if not os.path.isfile(cfg_path):
        _fatal(f"Trading config not found: {cfg_path}")

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        _fatal(f"Invalid JSON in {cfg_path}: {e}")
    except OSError as e:
        _fatal(f"Cannot read {cfg_path}: {e}")

    if not isinstance(data, dict):
        _fatal(f"{cfg_path} must be a JSON object.")

    defaults = data.get("defaults") or {}
    workers_raw = data.get("workers")
    if not isinstance(workers_raw, list) or not workers_raw:
        _fatal(f"{cfg_path} must contain a non-empty 'workers' array.")

    seen: set[str] = set()
    out: list[WorkerConfig] = []
    for entry in workers_raw:
        if not isinstance(entry, dict):
            _fatal("Each worker entry must be a JSON object.")
        wc = _merge_worker_entry(entry, defaults)
        if not wc.enabled:
            continue
        if wc.key in seen:
            _fatal(f"Duplicate worker config: {wc.key}")
        seen.add(wc.key)
        out.append(wc)

    if not out:
        _fatal("No enabled workers in trading config.")

    return tuple(out)


WORKER_CONFIGS: Tuple[WorkerConfig, ...] = load_worker_configs()
TRADING_ASSETS: Tuple[str, ...] = tuple(dict.fromkeys(w.asset for w in WORKER_CONFIGS))
TRADING_ASSETS_UPPER: Tuple[str, ...] = tuple(a.upper() for a in TRADING_ASSETS)
ALL_TRACKED_ASSETS = TRADING_ASSETS
TOTAL_BOTS: int = len(WORKER_CONFIGS)


def asset_pnl_filename(asset: str, window: str = "5m") -> str:
    a = normalize_asset_slug(asset)
    w = normalize_window(window)
    return f"{a}_{w}_pnl_history.json"


PNL_FILES: list[str] = [asset_pnl_filename(w.asset, w.window) for w in WORKER_CONFIGS]


def validate_trading_assets() -> Tuple[str, ...]:
    if not TRADING_ASSETS:
        _fatal("No trading assets resolved from worker config.")
    return TRADING_ASSETS


def trading_assets_label(separator: str = " · ") -> str:
    labels = [f"{w.asset.upper()} {w.window}" for w in WORKER_CONFIGS]
    return separator.join(labels)


def _parse_positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _fatal(f"{name}={raw!r} is not a valid number.")
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        _fatal(f"{name} must be a positive number (got {raw!r}).")
    return value


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _fatal(f"{name}={raw!r} is not a valid integer.")
    if value <= 0:
        _fatal(f"{name} must be a positive integer (got {raw!r}).")
    return value


print(
    f"📌 Workers ({len(WORKER_CONFIGS)}): "
    + ", ".join(f"{w.asset.upper()} {w.window}" for w in WORKER_CONFIGS)
)
print(f"🧪 DRY_RUN_DEFAULT={DRY_RUN_DEFAULT}")
if WORKER_CONFIGS:
    wc0 = WORKER_CONFIGS[0]
    if ENV_SIZING_OVERRIDES:
        size_label = (
            f"{wc0.order_size_min}-{wc0.order_size_max} random"
            if wc0.random_order_size
            else str(wc0.order_size_max)
        )
        print(
            f"📐 Sizing (.env override): order={size_label} | "
            f"max_order={wc0.max_order_size} | max_shares={wc0.max_shares}"
        )
    else:
        print(
            f"📐 Sizing (trading_config.json): order={wc0.order_size_max} fixed | "
            f"max_order={wc0.max_order_size} | max_shares={wc0.max_shares}"
        )
    print(
        f"📈 Momentum strategy: min_edge={wc0.min_net_edge:.2%} | "
        f"max_entry={wc0.momentum_max_entry_price:.2f} | "
        f"take_profit=+{wc0.take_profit_pct:.0%} | "
        f"stop_loss={wc0.stop_loss_pct:.0%} | dry_run={wc0.dry_run} | "
        f"live={wc0.live_trading_enabled}"
    )
