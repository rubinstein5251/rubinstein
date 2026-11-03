import asyncio
import json
import os
import time as _time
import traceback
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal
import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from dotenv import load_dotenv
from utils.network_retry import create_with_network_retries, is_transient_connectivity_error

load_dotenv()

from config import (  # noqa: E402 — env must be loaded first
    TRADING_ASSETS,
    TRADING_ASSETS_UPPER,
    WORKER_CONFIGS,
    trading_assets_label,
    validate_trading_assets,
)

validate_trading_assets()

try:
    from utils.inventory_strategy_store import inventory_strategy_store  # noqa: E402
except ImportError:
    inventory_strategy_store = None  # type: ignore

if TYPE_CHECKING:
    from bot import AccountService, MarketWorker

# ── Core bot classes (always required) ───────────────────────────────────────
try:
    from bot import AccountService as _AccountService  # type: ignore
    from bot import MarketWorker   as _MarketWorker    # type: ignore
except Exception as _bot_import_err:
    print(f"❌ Failed to import AccountService/MarketWorker: {_bot_import_err}")
    traceback.print_exc()
    _AccountService = None  # type: ignore
    _MarketWorker   = None  # type: ignore

# ── Portfolio history helpers (graceful fallback for older bot.py) ────────────
try:
    from bot import (                              # type: ignore
        portfolio_history_backfill,
        portfolio_history_snapshot,
        portfolio_history_get,
        prepare_chart_history,
        sanitize_portfolio_history,
        PORTFOLIO_FLAT_EPSILON,
        is_trading_allowed,
        _TRADING_TZ_NAME,
        TRADING_DAYS,
        _DAY_NAMES,
        MARKET_INTERVAL_SLUG,
    )
    _portfolio_available = True
except ImportError:
    print("⚠️  Portfolio history / schedule functions not found in bot.py — "
          "persistent history and weekend gating disabled. Deploy the updated bot.py.")
    portfolio_history_backfill   = None   # type: ignore
    portfolio_history_snapshot   = None   # type: ignore
    portfolio_history_get        = None   # type: ignore
    prepare_chart_history        = None   # type: ignore
    sanitize_portfolio_history   = None   # type: ignore
    PORTFOLIO_FLAT_EPSILON       = 0.005
    _portfolio_available         = False

# ── Positions / trade-history helpers (graceful fallback) ────────────────────
try:
    from bot import (                              # type: ignore
        collect_open_positions,
        get_trade_history,
        trade_history_backfill,
        compute_biggest_realized_win,
        compute_daily_pnl_calendar,
        find_worker,
        find_worker_by_asset,
        TRADING_TZ,
    )
    _positions_available = True
except ImportError:
    print("⚠️  Positions/trade-history functions not found in bot.py.")
    _positions_available = False

    def collect_open_positions(workers):  # type: ignore
        return []

    def get_trade_history(limit: int = 10):  # type: ignore
        return []

    def trade_history_backfill() -> int:  # type: ignore
        return 0

    def compute_biggest_realized_win() -> float:  # type: ignore
        return 0.0

    def compute_daily_pnl_calendar(  # type: ignore
        year: int, month: int, **kwargs: Any,
    ) -> dict:
        return {
            "year": year,
            "month": month,
            "timezone": "UTC",
            "week_start": 0,
            "scale_max": 100.0,
            "month_summary": {
                "total_pnl": 0.0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
            },
            "days": {},
        }

    TRADING_TZ = None  # type: ignore

    def find_worker(workers, asset, window):  # type: ignore
        return None

    def find_worker_by_asset(workers, asset) -> "MarketWorker | None":  # type: ignore
        return None

# MARKET_INTERVAL_SLUG comes from bot when available; config holds asset list only.
try:
    from bot import MARKET_INTERVAL_SLUG  # type: ignore
except Exception:
    if "MARKET_INTERVAL_SLUG" not in globals():
        MARKET_INTERVAL_SLUG = os.getenv("MARKET_INTERVAL", "5m")  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Display name derived from the Render URL
# ─────────────────────────────────────────────────────────────────────────────

def _bot_name_from_url(url: str) -> str:
    if not url:
        return "Emiliano"
    try:
        host = url.replace("https://", "").replace("http://", "").split(".")[0]
        return host.replace("-", " ").title()
    except Exception:
        return "Emiliano"


def _app_config_payload() -> dict:
    wc0 = WORKER_CONFIGS[0] if WORKER_CONFIGS else None
    payload = {
        "trading_assets":           list(TRADING_ASSETS_UPPER),
        "workers":                  [
            {"asset": w.asset.upper(), "window": w.window} for w in WORKER_CONFIGS
        ],
        "market_interval":          "5m",
        "trading_assets_label":     trading_assets_label(),
        "strategy":                 "momentum_directional",
        "strategy_label":           "BTC/ETH Momentum",
    }
    if wc0 is not None:
        payload.update({
            "max_entry_price_c": round(wc0.momentum_max_entry_price * 100, 2),
            "min_net_edge_pct": round(wc0.min_net_edge * 100, 2),
            "stop_loss_pct": wc0.stop_loss_pct,
            "order_size": wc0.order_size_max,
            "execution_type": "FOK",
            "dry_run": wc0.dry_run,
            "live_trading_enabled": wc0.live_trading_enabled,
        })
    return payload


BOT_DISPLAY_NAME = _bot_name_from_url(os.getenv("RENDER_EXTERNAL_URL", ""))
TRADING_ASSETS_SUBTITLE = trading_assets_label(" · ")

CALENDAR_PNL_SCALE_MAX = max(float(os.getenv("CALENDAR_PNL_SCALE_MAX", "100")), 0.01)
CALENDAR_WEEK_START = int(os.getenv("CALENDAR_WEEK_START", "0"))
if CALENDAR_WEEK_START not in (0, 1):
    CALENDAR_WEEK_START = 0


from utils.graceful_shutdown import (  # noqa: E402
    get_shutdown_event,
    graceful_shutdown,
    install_signal_handlers,
    register_shutdown_hook,
    shutdown_requested,
    track_background_task,
)


async def _close_dashboard_websockets() -> None:
    import contextlib

    for ws in active_connections[:]:
        with contextlib.suppress(Exception):
            await ws.close()
    active_connections.clear()


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    install_signal_handlers()
    get_shutdown_event()
    register_shutdown_hook(_close_dashboard_websockets)
    print("Emiliano Dashboard — HTTP server up, initializing bots in background...")
    init_task = asyncio.create_task(_initialize_bots(), name="bot-init")
    track_background_task(init_task)
    yield
    await graceful_shutdown(reason="application shutdown")


app = FastAPI(title=f"{BOT_DISPLAY_NAME} Dashboard", lifespan=_app_lifespan)

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass

bots:               list[Any]      = []
account:            Any            = None
active_connections: list[WebSocket] = []

# ── In-memory snapshot buffer ─────────────────────────────────────────────────
# Trailing in-memory point for live flat extension (not persisted every 60s).
_snapshot_buffer:    list[dict] = []
_last_redis_flush:   float      = 0.0
_SNAPSHOT_EVERY_SEC: int        = 60   # record a portfolio snapshot this often
_FLUSH_EVERY_SEC:    int        = 60   # flush buffer → Redis this often

_biggest_win_cache: dict[str, float | int] = {'value': 0.0, 'last_total_trades': -1}


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _create_account_with_network_retries(max_attempts: int = 5):
    """Construct AccountService off-loop and retry transient API/DNS failures only."""
    return await create_with_network_retries(_AccountService, max_attempts=max_attempts)


async def _initialize_bots():
    global bots, account
    print("🚀 Emiliano Dashboard Starting on Render...")
    bots = []

    if _AccountService is None or _MarketWorker is None:
        print("❌ Cannot start bots — AccountService/MarketWorker failed to import.")
        return

    try:
        # ── One-time account-level init ───────────────────────────────────
        print("Initializing AccountService (wallet audit runs once here)...")
        account = await _create_account_with_network_retries()
        if not account.run_wallet_audit():
            print("❌ Wallet audit failed — aborting bot startup.")
            return
        account.start_pnl_merge_scheduler()
        print("✅ AccountService ready.")

        from bot import ensure_binance_feeds_for_workers

        await ensure_binance_feeds_for_workers()
        print("✅ Binance external market feeds requested.")

        # ── Per-asset market workers ──────────────────────────────────────
        for wc in WORKER_CONFIGS:
            print(f"Initializing {wc.asset.upper()} {wc.window} Bot...")
            bots.append(_MarketWorker(wc, account))
            print(f"✅ {wc.asset.upper()} {wc.window} Bot initialized")

        # ── Portfolio history backfill ────────────────────────────────────
        # Runs exactly once at startup.  Reads emiliano:{asset}:trades from
        # Redis for every asset, reconstructs the full historical equity curve,
        # and writes it to emiliano:portfolio:history so the chart immediately
        # shows all past performance — not just data from today.
        #
        # The backfill is idempotent: if history already covers every trade
        # record it does nothing.  It is safe to redeploy repeatedly.
        if _portfolio_available and portfolio_history_backfill:
            try:
                n = portfolio_history_backfill()
                if n > 0:
                    print(f"📈 [backfill] {n} historical portfolio points reconstructed "
                          "from existing Redis trade records.")
                else:
                    print("ℹ️  [backfill] History already up-to-date (or no trade records found).")
            except Exception as bf_err:
                print(f"⚠️  [backfill] Non-fatal error: {bf_err}")
                traceback.print_exc()

        if _positions_available and trade_history_backfill:
            try:
                n_th = trade_history_backfill()
                if n_th > 0:
                    print(f"📜 [trade-history] {n_th} execution records backfilled.")
            except Exception as th_err:
                print(f"⚠️  [trade-history] Non-fatal backfill error: {th_err}")
                traceback.print_exc()

        global _biggest_win_cache
        _biggest_win_cache['value'] = _compute_biggest_win()
        _biggest_win_cache['last_total_trades'] = (
            sum(getattr(b, "wins", 0) + getattr(b, "losses", 0) for b in bots)
        )


        async def _stop_all_bots() -> None:
            if bots:
                await asyncio.gather(
                    *(b.graceful_stop() for b in bots),
                    return_exceptions=True,
                )

        register_shutdown_hook(_stop_all_bots)
        run_task = asyncio.create_task(run_all_bots(), name="run-all-bots")
        track_background_task(run_task)
        print("All bots started successfully!")

    except Exception as e:
        if is_transient_connectivity_error(e):
            print(
                "❌ Bot initialization stopped after repeated network failures. "
                "The dashboard HTTP server remains available, but bots are NOT running. "
                "Check DNS/internet access and that https://clob.polymarket.com resolves "
                "from this machine/container, then restart the service. No live orders were submitted."
            )
        else:
            print(f"❌ Bot init failed: {e}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND TASKS
# ─────────────────────────────────────────────────────────────────────────────

async def run_all_bots():
    if not bots:
        return
    try:
        await asyncio.gather(
            *[bot.start() for bot in bots],
            broadcast_loop(),
            keep_alive_heartbeat(),
            portfolio_snapshot_loop(),
        )
    except asyncio.CancelledError:
        if not shutdown_requested():
            raise
    except Exception as e:
        if shutdown_requested():
            return
        print(f"Background tasks error: {e}")
        traceback.print_exc()


async def keep_alive_heartbeat():
    render_url         = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    ping_interval      = 10 * 60
    heartbeat_interval = 25
    seconds_since_ping = 0

    async with aiohttp.ClientSession() as session:
        while not shutdown_requested():
            try:
                loop_time = asyncio.get_running_loop().time()
                print(f"💓 Heartbeat [{loop_time:.0f}] — {len(bots)} bots active")
                seconds_since_ping += heartbeat_interval
                if render_url and seconds_since_ping >= ping_interval:
                    seconds_since_ping = 0
                    try:
                        async with session.get(
                            f"{render_url}/api/status",
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            print(f"🌐 Self-ping → {resp.status}")
                    except Exception as ping_err:
                        print(f"⚠️  Self-ping failed: {ping_err}")
            except Exception:
                pass
            await asyncio.sleep(heartbeat_interval)


async def portfolio_snapshot_loop():
    """Sync portfolio PnL to Redis only when value changes; buffer trailing t in memory."""
    global _last_redis_flush, _snapshot_buffer
    _flat_eps = PORTFOLIO_FLAT_EPSILON

    while not shutdown_requested():
        await asyncio.sleep(_SNAPSHOT_EVERY_SEC)
        try:
            stats     = get_global_stats()
            total_pnl = stats.get("total_pnl", 0.0)
            try:
                total_pnl = round(float(total_pnl), 4)
                if total_pnl != total_pnl or total_pnl in (float("inf"), float("-inf")):
                    raise ValueError("non-finite total_pnl")
            except (TypeError, ValueError):
                print(f"⚠️ portfolio_snapshot_loop: rejected non-finite total_pnl={total_pnl!r}")
                continue

            now_ms = int(_time.time() * 1000)
            if _snapshot_buffer and abs(_snapshot_buffer[-1]["v"] - total_pnl) <= _flat_eps:
                _snapshot_buffer[-1]["t"] = now_ms
            else:
                _snapshot_buffer = [{"t": now_ms, "v": total_pnl}]

            now = _time.time()
            if _portfolio_available and portfolio_history_snapshot and (
                    now - _last_redis_flush >= _FLUSH_EVERY_SEC):
                _last_redis_flush = now
                portfolio_history_snapshot(total_pnl)
                print(f"💾 Portfolio history synced. Total PnL=${total_pnl:.2f}")

        except Exception as e:
            print(f"⚠️ portfolio_snapshot_loop error: {e}")


async def broadcast_loop():
    while not shutdown_requested():
        try:
            bot_data = []
            for bot in bots:
                try:
                    bot_data.append(bot.get_dashboard_data())
                except Exception as e:
                    bot_data.append({
                        "asset":    getattr(bot, "asset_type", "ERROR").upper(),
                        "status":   "ERROR",
                        "position": str(e)[:80],
                    })

            gs = get_global_stats()
            data = {
                "bots":          bot_data,
                "global_stats":  gs,
                "positions":     collect_open_positions(bots),
                "trade_history": get_trade_history(10),
                "config":        _app_config_payload(),
                "timestamp":     asyncio.get_running_loop().time(),
            }
            message = json.dumps(data)
            for ws in active_connections[:]:
                try:
                    await ws.send_text(message)
                except Exception:
                    if ws in active_connections:
                        active_connections.remove(ws)

            await asyncio.sleep(1.0)
        except Exception as e:
            print(f"Broadcast error: {e}")
            await asyncio.sleep(3)


def _compute_biggest_win() -> float:
    return compute_biggest_realized_win()


def get_global_stats() -> dict:
    global _biggest_win_cache
    # Compute trading schedule status once so it's consistent across the dict.
    _trading_ok = is_trading_allowed()

    if not bots:
        return {
            "total_bots": 0, "active_bots": 0, "total_pnl": 0.0,
            "in_profit": 0, "in_loss": 0, "total_trades": 0,
            "total_wins": 0, "total_losses": 0, "win_rate": 0.0,
            "trading_allowed": _trading_ok,
            "trading_tz":      _TRADING_TZ_NAME,
            "biggest_win": round(_biggest_win_cache['value'], 2),
        }

    active_count    = 0
    in_profit_count = 0
    in_loss_count   = 0

    from utils.accounting_definitions import portfolio_realized_and_overall
    from utils.canonical_ledger import canonical_ledger

    totals = portfolio_realized_and_overall(bots)
    total_pnl = totals["overall_net_pnl"]
    cumulative_realized = totals["cumulative_realized_pnl"]
    total_unrealized = totals["unrealized_pnl"]
    total_wins, total_losses = 0, 0
    _, total_wins, total_losses, _ = canonical_ledger.portfolio_totals()

    for bot in bots:
        try:
            in_position = (
                getattr(bot, "inventory", None) is not None
                and (
                    bot.inventory.yes_shares > 0
                    or bot.inventory.no_shares > 0
                )
            )
            if bot.active_market or in_position:
                active_count += 1
            if in_position:
                pnl_dollars, _, _ = bot.get_current_pnl()
                if pnl_dollars > 0:
                    in_profit_count += 1
                elif pnl_dollars < 0:
                    in_loss_count += 1
        except Exception:
            continue

    total_trades = total_wins + total_losses
    win_rate     = round((total_wins / total_trades) * 100, 1) if total_trades > 0 else 0.0

    if total_trades != _biggest_win_cache['last_total_trades']:
        _biggest_win_cache['value'] = _compute_biggest_win()
        _biggest_win_cache['last_total_trades'] = total_trades

    closed_trades = 0
    if bots:
        for bot in bots:
            try:
                closed_trades += int(getattr(bot, "trade_count", 0) or 0)
            except Exception:
                continue

    return {
        "total_bots":           len(bots),
        "active_bots":          active_count,
        "total_pnl":            round(total_pnl, 2),
        "overall_net_pnl":      round(total_pnl, 4),
        "cumulative_realized_pnl": round(cumulative_realized, 4),
        "unrealized_pnl":       round(total_unrealized, 4),
        "closed_trades":        closed_trades,
        "in_profit":            in_profit_count,
        "in_loss":              in_loss_count,
        "total_trades":         total_trades,
        "total_wins":           total_wins,
        "total_losses":         total_losses,
        "win_rate":             win_rate,
        "trading_allowed":      _trading_ok,
        "trading_tz":           _TRADING_TZ_NAME,
        "biggest_win":          round(_biggest_win_cache['value'], 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# /api/history  — portfolio equity-curve endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/history")
async def api_history(period: str = "1D"):
    """
    Returns the portfolio equity curve for the requested period.

    Query params
    ────────────
    period : '1D' | '1W' | '1M' | '1Y' | 'ALL'   (default '1D')

    Response
    ────────
    { "points": [ {"t": unix_ms, "v": cumulative_pnl_dollars}, ... ] }

    Data sources: Redis history + in-memory trailing buffer.
    Period filter keeps points within the lookback window but x-axis domain is
    data-driven (first→last timestamp), never padded with empty pre-data time.
    """
    now_ms = int(_time.time() * 1000)
    persisted: list = []
    if _portfolio_available and portfolio_history_get:
        try:
            persisted = portfolio_history_get("ALL")
        except Exception as e:
            print(f"⚠️ /api/history Redis read error: {e}")

    merged = list(persisted) + list(_snapshot_buffer)

    if _portfolio_available and prepare_chart_history:
        merged = prepare_chart_history(merged, period, now_ms=now_ms)
    elif _portfolio_available and sanitize_portfolio_history:
        merged = sanitize_portfolio_history(merged, drop_isolated_spikes=True, collapse_flat_runs=True)  # type: ignore

    return JSONResponse({"points": merged})


# ─────────────────────────────────────────────────────────────────────────────
# /api/pnl/calendar  — daily realized PnL buckets for heatmap
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/pnl/calendar")
async def api_pnl_calendar(
    year: int | None = None,
    month: int | None = None,
    tz: str | None = None,
    week_start: int | None = None,
    scale_max: float | None = None,
):
    from datetime import datetime, timezone as _utc

    cal_tz = TRADING_TZ if TRADING_TZ is not None else _utc.utc
    now = datetime.now(cal_tz)
    y = year if year is not None else now.year
    m = month if month is not None else now.month
    ws = week_start if week_start is not None else CALENDAR_WEEK_START
    sm = scale_max if scale_max is not None else CALENDAR_PNL_SCALE_MAX

    try:
        data = compute_daily_pnl_calendar(
            y, m, tz_name=tz, week_start=ws, scale_max=sm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(data)


# ─────────────────────────────────────────────────────────────────────────────
# HTML DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__BOT_NAME__ • Live</title>

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">

  <style>
    :root {
      /* default: green */
      --accent:        #22c55e;
      --accent-dark:   #16a34a;
      --accent-dim:    rgba(34,197,94,0.15);
      --accent-dimmer: rgba(34,197,94,0.12);
      --accent-border: rgba(34,197,94,0.25);
      --accent-glow:   rgba(34,197,94,0.08);
      --accent-muted:  color-mix(in srgb, var(--accent) 62%, #a1a1aa);
      --accent-subtle: color-mix(in srgb, var(--accent) 38%, #71717a);
      --accent-border-soft: color-mix(in srgb, var(--accent) 22%, transparent);
    }

    [data-theme="btc"] {
      --accent:        #f7931a;
      --accent-dark:   #c97314;
      --accent-dim:    rgba(247,147,26,0.15);
      --accent-dimmer: rgba(247,147,26,0.12);
      --accent-border: rgba(247,147,26,0.25);
      --accent-glow:   rgba(247,147,26,0.08);
    }

    [data-theme="eth"] {
      --accent:        #627eea;
      --accent-dark:   #4a62c4;
      --accent-dim:    rgba(98,126,234,0.15);
      --accent-dimmer: rgba(98,126,234,0.12);
      --accent-border: rgba(98,126,234,0.25);
      --accent-glow:   rgba(98,126,234,0.08);
    }

    [data-theme="sol"] {
      --accent:        #9945ff;
      --accent-dark:   #7a33d4;
      --accent-dim:    rgba(153,69,255,0.15);
      --accent-dimmer: rgba(153,69,255,0.12);
      --accent-border: rgba(153,69,255,0.25);
      --accent-glow:   rgba(153,69,255,0.08);
    }

    [data-theme="xrp"] {
      --accent:        #346aa9;
      --accent-dark:   #275280;
      --accent-dim:    rgba(52,106,169,0.15);
      --accent-dimmer: rgba(52,106,169,0.12);
      --accent-border: rgba(52,106,169,0.25);
      --accent-glow:   rgba(52,106,169,0.08);
    }

    .theme-dot {
      width:18px; height:18px; border-radius:50%; border:2px solid transparent;
      cursor:pointer; transition:border-color .15s, transform .15s;
      padding:0;
    }
    .theme-dot.active { border-color:#fff; transform:scale(1.2); }

    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: 'Inter', system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
    .mono, .font-mono, [class*="font-mono"] {
      font-family: 'JetBrains Mono','Consolas','Menlo',monospace;
      font-variant-numeric: tabular-nums;
    }
    .font-mono { font-family: 'JetBrains Mono','Consolas','Menlo',monospace !important; font-variant-numeric: tabular-nums; }
    .card { transition: all 0.3s cubic-bezier(0.4,0,0.2,1); }
    .card:hover { transform: translateY(-3px); }
    .sig-strong-bull  { color: var(--accent-muted); font-weight: 700; }
    .sig-strong-bear  { color: #ef4444; font-weight: 700; }
    .sig-mild-bull    { color: #86efac; }
    .sig-mild-bear    { color: #fca5a5; }
    .sig-neutral      { color: #71717a; }
    .pill-strong-bull { background: var(--accent-dim);   color: var(--accent-muted); }
    .pill-strong-bear { background: rgba(239,68,68,.15);   color: #ef4444; }
    .pill-mild-bull   { background: rgba(134,239,172,.12); color: #86efac; }
    .pill-mild-bear   { background: rgba(252,165,165,.12); color: #fca5a5; }
    .pill-neutral     { background: rgba(113,113,122,.15); color: #71717a; }

    .pm-card { background:#18181b; border-radius:24px; overflow:hidden; margin-bottom:24px; }
    .pm-card-hdr { display:flex; justify-content:space-between; align-items:center; padding:18px 20px 4px; }
    .pm-label-grp { display:flex; align-items:center; gap:7px; }
    .pm-tri { font-size:11px; font-weight:700; line-height:1; }
    .pm-tri.pos { color:var(--accent-muted); } .pm-tri.neg { color:#ef4444; }
    .pm-lbl-txt { font-size:12px; font-weight:500; color:#71717a; letter-spacing:.04em; text-transform:uppercase; }
    .pm-per-tabs { display:flex; gap:1px; }
    .pm-per-btn {
      background:none; border:none; color:#52525b;
      font-family:'Inter',system-ui,sans-serif;
      font-size:11px; font-weight:600; padding:5px 8px; border-radius:6px;
      cursor:pointer; transition:all .15s; letter-spacing:.03em;
    }
    .pm-per-btn.act { background:color-mix(in srgb, var(--accent) 88%, #0a0a0a); color:#0a0a0a; }
    .pm-val-block { padding:6px 20px 0; display:flex; flex-direction:column; align-items:flex-start; gap:4px; }
    .pm-balance-row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .pm-balance-toggle {
      background:none; border:none; color:#71717a; cursor:pointer;
      padding:8px; border-radius:10px; transition:color .15s,background .15s;
      font-size:17px; line-height:1; flex-shrink:0;
    }
    .pm-balance-toggle:hover { color:#a1a1aa; background:rgba(255,255,255,0.06); }
    .pm-balance-toggle:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
    .pm-big-val { font-family:'Inter',system-ui,sans-serif; font-size:40px; font-weight:800; line-height:1; letter-spacing:-.04em; }
    .pm-big-val.pos { color:var(--accent); } .pm-big-val.neg { color:#ef4444; } .pm-big-val.neu { color:#e4e4e7; }
    .ps-stat-val { font-family:'Inter',system-ui,sans-serif; font-size:20px; font-weight:700; line-height:1.2; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
    .ps-stat-val.pos { color:var(--accent-muted); }
    .ps-stat-val.win { color:color-mix(in srgb, var(--accent) 75%, #d4d4d8); }
    .ps-stat-val.neg { color:#ef4444; }
    .ps-stat-val.neu { color:#e4e4e7; }
    .pm-stat-num.pos { color:var(--accent-muted); }
    .text-accent { color:var(--accent-muted); }
    .text-accent-bright { color:var(--accent); }
    .border-l-accent { border-left-color:var(--accent-border-soft); }
    .badge-accent {
      background:var(--accent-dim); color:var(--accent-muted);
      border:1px solid var(--accent-border-soft);
    }
    .conn-dot-on {
      background:var(--accent-muted);
      box-shadow:0 0 0 2px var(--accent-border-soft);
    }
    .conn-status {
      display:flex; align-items:center; gap:6px;
      font-family:'JetBrains Mono','Consolas',monospace;
      font-size:11px; font-weight:600; letter-spacing:.03em;
      color:#71717a; white-space:nowrap;
    }
    .conn-status.live { color:var(--accent-muted); }
    .conn-status.off { color:#f87171; }
    .pm-change-lbl { font-family:'JetBrains Mono','Consolas',monospace; font-variant-numeric:tabular-nums; font-size:12px; font-weight:600; color:#71717a; letter-spacing:.01em; }
    .pm-change-lbl.pos { color:var(--accent-muted); } .pm-change-lbl.neg { color:#ef4444; }
    .pm-chart-box { height:160px; position:relative; overflow:hidden; margin-top:12px; }
    .pm-chart-box svg { display:block; width:100%; height:100%; pointer-events:none; }
    .pm-no-data { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:12px; color:#52525b; white-space:nowrap; pointer-events:none; }
    #pm-chart-overlay { position:absolute; inset:0; z-index:10; cursor:crosshair; touch-action:pan-y; }
    .pm-stats-row { display:flex; border-top:1px solid rgba(255,255,255,0.06); padding:12px 20px; align-items:center; justify-content:space-between; }
    .pm-stat-item { display:flex; flex-direction:column; align-items:center; }
    .pm-stat-num { font-family:'JetBrains Mono','Consolas',monospace; font-variant-numeric:tabular-nums; font-size:15px; font-weight:700; line-height:1.2; }
    .pm-stat-lbl { font-size:9px; color:#52525b; text-transform:uppercase; letter-spacing:.1em; margin-top:3px; }

    /* Portfolio section tabs — Trades / Positions / History */
    .pm-section-tabs { display:flex; gap:0; border-bottom:1px solid rgba(255,255,255,0.06); padding:0 20px; }
    .pm-section-tab {
      background:none; border:none; border-bottom:2px solid transparent;
      color:#71717a; font-family:'Inter',system-ui,sans-serif;
      font-size:13px; font-weight:600; padding:14px 16px 12px; margin-bottom:-1px;
      cursor:pointer; transition:color .15s,border-color .15s;
    }
    .pm-section-tab:hover { color:#a1a1aa; }
    .pm-section-tab.act { color:#fafafa; border-bottom-color:var(--accent-muted); }
    .pm-section-panel { padding:0; min-height:120px; }
    .pm-section-panel.hidden { display:none; }
    .pm-trades-panel { padding:16px 20px 20px; }
    .pm-trades-grid {
      display:grid; grid-template-columns:1fr; gap:16px;
    }
    @media (min-width:768px) {
      .pm-trades-grid { grid-template-columns:repeat(2,1fr); gap:24px; }
    }
    .pm-empty { padding:32px 20px; text-align:center; color:#52525b; font-size:13px; }
    .pm-row {
      display:flex; align-items:center; justify-content:space-between; gap:12px;
      padding:14px 20px; border-bottom:1px solid rgba(255,255,255,0.04);
      transition:background .12s;
    }
    .pm-row:last-child { border-bottom:none; }
    .pm-row:hover { background:rgba(255,255,255,0.02); }
    .pm-row-main { flex:1; min-width:0; }
    .pm-row-title { font-size:13px; font-weight:600; color:#e4e4e7; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .pm-row-sub { font-size:11px; color:#71717a; margin-top:3px; display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
    .pm-side-yes { color:var(--accent-muted); font-weight:600; font-size:11px; }
    .pm-side-no  { color:#ef4444; font-weight:600; font-size:11px; }
    .pm-side-spread { color:#38bdf8; font-weight:600; font-size:11px; }
    .pm-action-buy  { color:var(--accent-muted); font-weight:700; font-size:11px; letter-spacing:.04em; }
    .pm-action-sell { color:#f97316; font-weight:700; font-size:11px; letter-spacing:.04em; }
    .pm-action-redeem { color:#38bdf8; font-weight:700; font-size:11px; letter-spacing:.04em; }
    .pm-row-stats { display:flex; flex-direction:column; align-items:flex-end; gap:2px; flex-shrink:0; }
    .pm-row-val { font-family:'JetBrains Mono','Consolas',monospace; font-variant-numeric:tabular-nums; font-size:13px; font-weight:600; color:#e4e4e7; }
    .pm-row-val.pos { color:var(--accent-muted); } .pm-row-val.neg { color:#ef4444; }
    .pm-row-meta { font-size:10px; color:#52525b; font-family:'JetBrains Mono','Consolas',monospace; }
    .pm-row-meta.pos { color:var(--accent-subtle); } .pm-row-meta.neg { color:#ef4444; }
    .pm-cashout-btn {
      background:var(--accent-dimmer); color:var(--accent-muted); border:1px solid var(--accent-border-soft);
      font-family:'Inter',system-ui,sans-serif; font-size:11px; font-weight:700;
      padding:6px 12px; border-radius:8px; cursor:pointer; flex-shrink:0;
      transition:background .15s,opacity .15s; letter-spacing:.02em;
    }
    .pm-cashout-btn:hover:not(:disabled) { background:var(--accent-dim); color:var(--accent); }
    .pm-cashout-btn:disabled { opacity:.45; cursor:not-allowed; }
    @media (max-width:640px) {
      .pm-row { flex-wrap:wrap; }
      .pm-row-stats { align-items:flex-start; width:100%; flex-direction:row; justify-content:space-between; }
      .pm-cashout-btn { width:100%; text-align:center; margin-top:4px; }
    }

    /* PnL calendar heatmap */
    .cal-nav { display:flex; align-items:center; gap:10px; }
    .cal-nav-btn {
      background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.08);
      color:#a1a1aa; width:32px; height:32px; border-radius:8px; cursor:pointer;
      font-size:18px; line-height:1; display:flex; align-items:center; justify-content:center;
      transition:background .15s,color .15s;
    }
    .cal-nav-btn:hover { background:rgba(255,255,255,0.1); color:#fafafa; }
    .cal-month-label {
      font-family:'Inter',system-ui,sans-serif; font-size:14px; font-weight:600;
      color:#e4e4e7; min-width:140px; text-align:center;
    }
    .cal-summary {
      display:flex; flex-wrap:wrap; align-items:baseline; gap:16px 24px;
      padding:8px 20px 16px; border-bottom:1px solid rgba(255,255,255,0.06);
    }
    .cal-summary-pnl {
      font-family:'Inter',system-ui,sans-serif; font-size:22px; font-weight:800;
      letter-spacing:-.02em; font-variant-numeric:tabular-nums;
    }
    .cal-summary-pnl.pos { color:var(--accent-muted); }
    .cal-summary-pnl.neg { color:#ef4444; }
    .cal-summary-pnl.neu { color:#e4e4e7; }
    .cal-summary-meta {
      font-family:'JetBrains Mono','Consolas',monospace; font-size:12px;
      color:#71717a; font-variant-numeric:tabular-nums;
    }
    .cal-scale-note { font-size:10px; color:#52525b; margin-left:auto; }
    .cal-weekdays {
      display:grid; grid-template-columns:repeat(7,1fr); gap:4px;
      padding:0 20px 6px;
    }
    .cal-wday {
      text-align:center; font-size:10px; font-weight:600; color:#52525b;
      text-transform:uppercase; letter-spacing:.08em;
    }
    .cal-grid-wrap { padding:0 20px 20px; position:relative; }
    .cal-grid {
      display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:5px;
    }
    .cal-cell {
      position:relative;
      aspect-ratio:1;
      min-height:52px;
      border-radius:8px;
      padding:7px 5px;
      display:flex; flex-direction:column;
      align-items:center; justify-content:center;
      gap:3px;
      border:1px solid rgba(255,255,255,0.04);
      background:rgba(113,113,122,0.06);
      cursor:default;
      transition:border-color .15s, background .15s;
      overflow:hidden;
    }
    .cal-cell--pad {
      background:transparent;
      border-color:transparent;
      opacity:.28;
      pointer-events:none;
    }
    .cal-cell--pad .cal-day-num { color:#3f3f46; }
    .cal-cell--empty {
      background:rgba(113,113,122,0.05);
      border-color:rgba(255,255,255,0.03);
    }
    .cal-cell--future {
      background:rgba(113,113,122,0.03);
      border-color:rgba(255,255,255,0.02);
      opacity:.45;
    }
    .cal-cell--pos {
      background:color-mix(
        in srgb,
        var(--accent) calc(6% + var(--cal-i, 0) * 54%),
        rgba(24,24,27,0.92)
      );
      border-color:color-mix(
        in srgb,
        var(--accent-border-soft) calc(18% + var(--cal-i, 0) * 82%),
        transparent
      );
    }
    .cal-cell--neg {
      background:color-mix(
        in srgb,
        #ef4444 calc(6% + var(--cal-i, 0) * 54%),
        rgba(24,24,27,0.92)
      );
      border-color:color-mix(
        in srgb,
        #ef4444 calc(14% + var(--cal-i, 0) * 46%),
        transparent
      );
    }
    .cal-cell--active { cursor:pointer; }
    .cal-cell--active:hover {
      border-color:rgba(255,255,255,0.18);
      box-shadow:0 0 0 1px rgba(255,255,255,0.04);
    }
    .cal-day-num {
      font-size:10px; font-weight:600; color:#52525b; line-height:1;
      font-variant-numeric:tabular-nums;
    }
    .cal-cell--empty .cal-day-num,
    .cal-cell--future .cal-day-num { color:#52525b; }
    .cal-cell--pos .cal-day-num {
      color:color-mix(in srgb, var(--accent-subtle) calc(40% + var(--cal-i, 0) * 60%), #71717a);
    }
    .cal-cell--neg .cal-day-num {
      color:color-mix(in srgb, #ef4444 calc(30% + var(--cal-i, 0) * 50%), #71717a);
    }
    .cal-day-pnl {
      font-family:'JetBrains Mono','Consolas',monospace;
      font-size:10px; font-weight:700; line-height:1.15;
      font-variant-numeric:tabular-nums;
      text-align:center; max-width:100%;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }
    .cal-cell--pos .cal-day-pnl {
      color:color-mix(in srgb, var(--accent-muted) calc(55% + var(--cal-i, 0) * 45%), #e4e4e7);
    }
    .cal-cell--neg .cal-day-pnl {
      color:color-mix(in srgb, #ef4444 calc(55% + var(--cal-i, 0) * 45%), #fca5a5);
    }
    .cal-tooltip {
      position:fixed; z-index:100; pointer-events:none;
      background:#27272a; border:1px solid rgba(255,255,255,0.1);
      border-radius:10px; padding:10px 12px; min-width:160px;
      box-shadow:0 8px 24px rgba(0,0,0,0.45);
      font-size:12px; line-height:1.5;
    }
    .cal-tooltip.hidden { display:none; }
    .cal-tooltip-title {
      font-weight:700; color:#fafafa; margin-bottom:6px;
      font-family:'Inter',system-ui,sans-serif;
    }
    .cal-tooltip-row {
      display:flex; justify-content:space-between; gap:12px;
      font-family:'JetBrains Mono','Consolas',monospace; font-size:11px;
      color:#a1a1aa;
    }
    .cal-tooltip-row span:last-child { color:#e4e4e7; font-variant-numeric:tabular-nums; }
    .cal-tooltip-row.pos span:last-child { color:var(--accent-muted); }
    .cal-tooltip-row.neg span:last-child { color:#ef4444; }
    .pm-calendar-panel { padding:0; min-height:200px; }
    @media (max-width:640px) {
      .cal-cell { min-height:44px; padding:5px 3px; gap:2px; }
      .cal-day-pnl { font-size:9px; }
      .cal-day-num { font-size:9px; }
      .cal-scale-note { width:100%; margin-left:0; margin-top:4px; }
    }

    /* Mobile bottom navigation — desktop keeps top tabs */
    .pm-bottom-nav { display:none; }
    @media (max-width:768px) {
      .pm-section-tabs { display:none; }
      .pm-bottom-nav {
        display:flex;
        position:fixed;
        left:0; right:0; bottom:0;
        z-index:50;
        justify-content:space-around;
        align-items:stretch;
        background:#18181b;
        border-top:1px solid rgba(255,255,255,0.08);
        padding:6px 0 calc(6px + env(safe-area-inset-bottom, 0px));
        box-shadow:0 -4px 24px rgba(0,0,0,0.35);
      }
      .pm-bottom-nav-item {
        flex:1;
        display:flex;
        flex-direction:column;
        align-items:center;
        justify-content:center;
        gap:3px;
        background:none;
        border:none;
        color:#71717a;
        font-family:'Inter',system-ui,sans-serif;
        font-size:10px;
        font-weight:600;
        padding:6px 4px;
        cursor:pointer;
        transition:color .15s;
        min-height:52px;
      }
      .pm-bottom-nav-item i { font-size:18px; line-height:1; }
      .pm-bottom-nav-item:hover { color:#a1a1aa; }
      .pm-bottom-nav-item.act { color:var(--accent); }
      .pm-bottom-nav-item.act i { color:var(--accent); }
      body { padding-bottom:calc(72px + env(safe-area-inset-bottom, 0px)) !important; }
    }
  </style>
</head>

<body class="bg-zinc-950 text-zinc-100 min-h-screen p-4 md:p-6">
<div class="max-w-7xl mx-auto">

  <!-- ── Header ──────────────────────────────────────────────── -->
  <div class="flex items-center justify-between mb-5">
    <div>
      <h1 class="text-4xl font-bold"
          style="font-family:'JetBrains Mono','Consolas',monospace;letter-spacing:-.03em;line-height:1;">
        __BOT_NAME__
      </h1>
      <p id="assets-subtitle" class="text-zinc-500 text-sm mt-1 font-mono">__TRADING_ASSETS__</p>
    </div>
    <div class="flex items-center gap-3">
      <div id="theme-picker" style="display:flex;gap:6px;align-items:center;">
        <button type="button" onclick="setTheme('')"    data-t=""    class="theme-dot" style="background:#22c55e" title="Green (default)"></button>
        <button type="button" onclick="setTheme('btc')" data-t="btc" class="theme-dot" style="background:#f7931a" title="BTC"></button>
        <button type="button" onclick="setTheme('eth')" data-t="eth" class="theme-dot" style="background:#627eea" title="ETH"></button>
        <button type="button" onclick="setTheme('sol')" data-t="sol" class="theme-dot" style="background:#9945ff" title="SOL"></button>
        <button type="button" onclick="setTheme('xrp')" data-t="xrp" class="theme-dot" style="background:#346aa9" title="XRP"></button>
      </div>
      <div id="conn-status" class="conn-status" title="WebSocket status">
        <span id="conn-dot" class="w-2.5 h-2.5 rounded-full bg-zinc-600 inline-block flex-shrink-0"></span>
        <span id="conn-label">Connecting…</span>
      </div>
    </div>
  </div>

  <!-- ── Trading schedule banner ─────────────────────────────────── -->
  <div id="schedule-banner"
       class="mb-4 px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border"
       style="font-family:'JetBrains Mono','Consolas',monospace;letter-spacing:.02em;
              background:var(--accent-glow);color:var(--accent-muted);border-color:var(--accent-border-soft);
              transition:background .4s,color .4s,border-color .4s;">
    <span id="schedule-dot" class="w-2 h-2 rounded-full inline-block flex-shrink-0"
          style="background:var(--accent-muted)"></span>
    <span id="schedule-text">Checking trading schedule…</span>
  </div>

  <!-- ── Profile stats strip (Polymarket-style) ─────────────────── -->
  <div id="profile-stats-strip" class="grid grid-cols-3 w-full mb-5">
    <div class="flex flex-col gap-1">
      <span class="text-[10px] font-semibold uppercase tracking-widest text-zinc-500"
            style="font-family:'Inter',system-ui,sans-serif;">Positions Value</span>
      <span class="ps-stat-val neu" id="ps-positions-value">$0.00</span>
    </div>
    <div class="flex flex-col gap-1">
      <span class="text-[10px] font-semibold uppercase tracking-widest text-zinc-500"
            style="font-family:'Inter',system-ui,sans-serif;">Biggest Win</span>
      <span class="ps-stat-val neu" id="ps-biggest-win">$0.00</span>
    </div>
    <div class="flex flex-col gap-1">
      <span class="text-[10px] font-semibold uppercase tracking-widest text-zinc-500"
            style="font-family:'Inter',system-ui,sans-serif;">Closed Trades</span>
      <span class="ps-stat-val text-zinc-100" id="ps-predictions">0</span>
    </div>
  </div>

  <p id="math-quote" class="mb-5 text-sm font-bold"
     style="color:var(--accent-muted);font-family:'JetBrains Mono','Consolas',monospace;letter-spacing:-.03em;line-height:1;transition:opacity .6s ease;"></p>
  <script>
  (function(){
    var q=[
      '"The only way to learn mathematics is to do mathematics." — Paul Halmos',
      '"Mathematics is the language in which God has written the universe." — Galileo Galilei',
      '"In mathematics you don\'t understand things. You just get used to them." — John von Neumann',
      '"God made the integers; all else is the work of man." — Leopold Kronecker',
      '"Pure mathematics is, in its way, the poetry of logical ideas." — Albert Einstein',
      '"Mathematics is not about numbers, equations, computations, or algorithms: it is about understanding." — William Paul Thurston',
      '"Do not worry about your difficulties in mathematics. I can assure you mine are still greater." — Albert Einstein',
      '"A mathematician is a machine for turning coffee into theorems." — Paul Erdős',
      '"Without mathematics, there\'s nothing you can do. Everything around you is mathematics." — Shakuntala Devi',
      '"The essence of mathematics lies in its freedom." — Georg Cantor',
      '"If people do not believe that mathematics is simple, it is only because they do not realize how complicated life is." — John von Neumann',
      '"An equation for me has no meaning unless it represents a thought of God." — Srinivasa Ramanujan',
      '"It is not knowledge, but the act of learning, not possession but the act of getting there, which grants the greatest enjoyment." — Carl Friedrich Gauss',
      '"Mathematics is the queen of the sciences and number theory is the queen of mathematics." — Carl Friedrich Gauss',
      '"No human investigation can be called real science if it cannot be demonstrated mathematically." — Leonardo da Vinci',
    ];
    var el=document.getElementById('math-quote');
    var idx=Math.floor(Math.random()*q.length);
    function next(){el.style.opacity='0';setTimeout(function(){idx=(idx+1)%q.length;el.textContent=q[idx];el.style.opacity='1';},600);}
    el.textContent=q[idx]; el.style.opacity='1';
    setInterval(next,12000);
  })();
  </script>

  <!-- ── P&L Chart Card ──────────────────────────────────────── -->
  <div class="pm-card">
    <div class="pm-card-hdr">
      <div class="pm-label-grp">
        <span class="pm-tri neg" id="pm-tri">▼</span>
        <span class="pm-lbl-txt">Portfolio P&amp;L</span>
      </div>
      <div class="pm-per-tabs">
        <button class="pm-per-btn act" id="ppb-1D"  onclick="setPeriod('1D')">1D</button>
        <button class="pm-per-btn"     id="ppb-1W"  onclick="setPeriod('1W')">1W</button>
        <button class="pm-per-btn"     id="ppb-1M"  onclick="setPeriod('1M')">1M</button>
        <button class="pm-per-btn"     id="ppb-1Y"  onclick="setPeriod('1Y')">1Y</button>
        <button class="pm-per-btn"     id="ppb-ALL" onclick="setPeriod('ALL')">ALL</button>
      </div>
    </div>
    <div class="pm-val-block">
      <div class="pm-balance-row">
        <div class="pm-big-val neu" id="pm-bigval">$0.00</div>
        <button class="pm-balance-toggle" id="pm-balance-toggle" type="button"
                aria-label="Hide balances" aria-pressed="true" onclick="toggleBalanceVisibility()">
          <i class="fa-solid fa-eye" id="pm-balance-toggle-icon"></i>
        </button>
      </div>
      <span class="pm-change-lbl" id="pm-change-lbl">Loading history…</span>
    </div>
    <div class="pm-chart-box" id="pm-chart-box">
      <div class="pm-no-data" id="pm-no-data">Loading history…</div>
      <div id="pm-chart-overlay"></div>
    </div>
    <div class="pm-stats-row">
      <div class="pm-stat-item">
        <span class="pm-stat-num text-zinc-200" id="pm-st-trades">0</span>
        <span class="pm-stat-lbl">Trades</span>
      </div>
      <div class="pm-stat-item">
        <span class="pm-stat-num pos" id="pm-st-wins">0</span>
        <span class="pm-stat-lbl">Wins</span>
      </div>
      <div class="pm-stat-item">
        <span class="pm-stat-num text-red-400" id="pm-st-losses">0</span>
        <span class="pm-stat-lbl">Losses</span>
      </div>
      <div class="pm-stat-item">
        <span class="pm-stat-num text-yellow-400" id="pm-st-wr">--%</span>
        <span class="pm-stat-lbl">Win Rate</span>
      </div>
    </div>
  </div>

  <!-- ── Trades / Positions / History / Calendar ───────────────── -->
  <div class="pm-card" id="portfolio-tabs-card">
    <div class="pm-section-tabs">
      <button class="pm-section-tab act" id="tab-btn-trades" onclick="setPortfolioTab('trades')">Trades</button>
      <button class="pm-section-tab" id="tab-btn-positions" onclick="setPortfolioTab('positions')">Positions (0)</button>
      <button class="pm-section-tab" id="tab-btn-history" onclick="setPortfolioTab('history')">History</button>
      <button class="pm-section-tab" id="tab-btn-calendar" onclick="setPortfolioTab('calendar')">Calendar</button>
    </div>
    <div id="panel-trades" class="pm-section-panel pm-trades-panel">
      <div id="bots-container" class="pm-trades-grid"></div>
    </div>
    <div id="panel-positions" class="pm-section-panel hidden">
      <div id="positions-list"></div>
    </div>
    <div id="panel-history" class="pm-section-panel hidden">
      <div id="history-list"></div>
    </div>
    <div id="panel-calendar" class="pm-section-panel pm-calendar-panel hidden">
      <div class="pm-card-hdr" style="padding-top:14px;">
        <div class="pm-label-grp">
          <span class="pm-lbl-txt">PnL Calendar</span>
        </div>
        <div class="cal-nav">
          <button type="button" class="cal-nav-btn" id="cal-prev" aria-label="Previous month">&#8249;</button>
          <span class="cal-month-label" id="cal-month-label">—</span>
          <button type="button" class="cal-nav-btn" id="cal-next" aria-label="Next month">&#8250;</button>
        </div>
      </div>
      <div class="cal-summary">
        <span class="cal-summary-pnl neu" id="cal-month-pnl">$0.00</span>
        <span class="cal-summary-meta" id="cal-month-wr">Win rate —</span>
        <span class="cal-summary-meta" id="cal-month-trades">0 trades</span>
        <span class="cal-scale-note" id="cal-scale-note">Scale ±$100</span>
      </div>
      <div class="cal-weekdays" id="cal-weekdays"></div>
      <div class="cal-grid-wrap">
        <div class="cal-grid" id="cal-grid"></div>
      </div>
      <div id="cal-tooltip" class="cal-tooltip hidden" role="tooltip"></div>
    </div>
  </div>

</div>

<!-- Mobile bottom nav (hidden on desktop via CSS) -->
<nav class="pm-bottom-nav" id="pm-bottom-nav" aria-label="Portfolio navigation">
  <button type="button" class="pm-bottom-nav-item act" id="bottom-tab-trades" onclick="setPortfolioTab('trades')">
    <i class="fa-solid fa-chart-line" aria-hidden="true"></i>
    <span>Trades</span>
  </button>
  <button type="button" class="pm-bottom-nav-item" id="bottom-tab-positions" onclick="setPortfolioTab('positions')">
    <i class="fa-solid fa-briefcase" aria-hidden="true"></i>
    <span>Positions</span>
  </button>
  <button type="button" class="pm-bottom-nav-item" id="bottom-tab-history" onclick="setPortfolioTab('history')">
    <i class="fa-solid fa-clock-rotate-left" aria-hidden="true"></i>
    <span>History</span>
  </button>
  <button type="button" class="pm-bottom-nav-item" id="bottom-tab-calendar" onclick="setPortfolioTab('calendar')">
    <i class="fa-solid fa-calendar-days" aria-hidden="true"></i>
    <span>Calendar</span>
  </button>
</nav>

<script>
// ═══════════════════════════════════════════════════════════════════
// PORTFOLIO HISTORY CHART ENGINE
//
// • Points only on PnL changes (no heartbeat spam).
// • X-axis domain = [first data timestamp, last data timestamp] — never
//   padded with empty pre-data time (e.g. new session at 8:04pm starts there).
// • Period tabs filter the lookback window server-side; scaling is always
//   data-driven on the client.
// ═══════════════════════════════════════════════════════════════════

const _PERIOD_MS = {
  '1D':  24 * 60 * 60 * 1000,
  '1W':  7  * 24 * 60 * 60 * 1000,
  '1M':  30 * 24 * 60 * 60 * 1000,
  '1Y':  365 * 24 * 60 * 60 * 1000,
};
const _FLAT_EPS = 0.005;
const _BALANCE_LS_KEY = 'pm_balance_visible';

let _balanceVisible = true;
let _profileStats = { positionsValue: 0, biggestWin: 0, predictions: 0 };
let _historyPts    = [];
let _livePts       = [];
let _pnlPeriod     = '1D';
let _historyLoaded = false;
let _loadingHist   = false;
let _lastLivePnl   = null;
let _lastLiveTs    = 0;
let _chartRaf      = 0;

function _isFiniteNum(v) {
  return typeof v === 'number' && isFinite(v);
}

function _loadBalancePref() {
  try {
    const v = localStorage.getItem(_BALANCE_LS_KEY);
    if (v === '0' || v === 'false') _balanceVisible = false;
  } catch (e) { /* private browsing */ }
}

function _saveBalancePref() {
  try {
    localStorage.setItem(_BALANCE_LS_KEY, _balanceVisible ? '1' : '0');
  } catch (e) { /* private browsing */ }
}

function _maskUsd() { return '••••'; }
function _maskCents() { return '•••'; }

function _accentColor() {
  return getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
}
function _accentDark() {
  return getComputedStyle(document.documentElement).getPropertyValue('--accent-dark').trim();
}

const _THEME_LS = 'pm_theme';
function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem(_THEME_LS, t); } catch(e){}
  document.querySelectorAll('.theme-dot').forEach(b => {
    b.classList.toggle('active', b.dataset.t === t);
  });
  schedulePnlChartUpdate(true);
  refreshBalanceSensitiveUI();
}
function _loadTheme() {
  try {
    const t = localStorage.getItem(_THEME_LS) || '';
    setTheme(t);
  } catch(e) { setTheme(''); }
}

function _fmtMoney(abs) {
  const [int, dec] = abs.toFixed(2).split('.');
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + dec;
}
function _displayBigVal(val) {
  if (!_balanceVisible) return _maskUsd();
  return (val >= 0 ? '' : '-') + '$' + _fmtMoney(Math.abs(val));
}

function _displayPeriodChange(change, pct, arrow, periodLabel) {
  const pctStr = pct !== null
    ? ` (${change >= 0 ? '+' : ''}${pct.toFixed(1)}%)`
    : '';
  if (!_balanceVisible) {
    const maskedPart = pct !== null ? pctStr : _maskUsd();
    return `${maskedPart}  ${arrow}  ${periodLabel}`;
  }
  const sign = change >= 0 ? '+' : '';
  return `${sign}$${_fmtMoney(Math.abs(change))}${pctStr}  ${arrow}  ${periodLabel}`;
}

function _updateBalanceToggleUI() {
  const btn = document.getElementById('pm-balance-toggle');
  const icon = document.getElementById('pm-balance-toggle-icon');
  if (!btn || !icon) return;
  btn.setAttribute('aria-pressed', _balanceVisible ? 'true' : 'false');
  btn.setAttribute('aria-label', _balanceVisible ? 'Hide balances' : 'Show balances');
  icon.className = _balanceVisible ? 'fa-solid fa-eye' : 'fa-solid fa-eye-slash';
}

function toggleBalanceVisibility() {
  _balanceVisible = !_balanceVisible;
  _saveBalancePref();
  _updateBalanceToggleUI();
  refreshBalanceSensitiveUI();
}

function refreshBalanceSensitiveUI() {
  updatePnlChart();
  renderPositions(window._lastPositions || []);
  if (window._lastBots) renderBots(window._lastBots);
  if (window._lastTradeHistory) renderTradeHistory(window._lastTradeHistory);
  renderProfileStats(window._lastBots || [], window._lastGlobalStats || {});
  if (window._calData) renderCalendar(window._calData);
}

function _updatePositionsTabCount(count) {
  const n = count || 0;
  const btn = document.getElementById('tab-btn-positions');
  if (btn) btn.textContent = 'Positions (' + n + ')';
  const bottomBtn = document.getElementById('bottom-tab-positions');
  if (bottomBtn) {
    const label = bottomBtn.querySelector('span');
    if (label) label.textContent = n > 0 ? ('Positions (' + n + ')') : 'Positions';
  }
}

function _periodLabel(p, tMin, tMax) {
  const labels = {'1D':'Past 24h','1W':'Past 7 Days','1M':'Past 30 Days',
                  '1Y':'Past Year','ALL':'All Time'};
  const full = _PERIOD_MS[p];
  const span = (tMax && tMin) ? (tMax - tMin) : 0;
  if (!full || p === 'ALL' || span >= full * 0.9) return labels[p] || p;
  if (span < 24 * 60 * 60 * 1000) {
    return 'Since ' + new Date(tMin).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  }
  return 'Since ' + new Date(tMin).toLocaleDateString([], {month:'short', day:'numeric'});
}

function _mergedPts() {
  const byT = new Map();
  for (const p of _historyPts) {
    if (p && Number.isFinite(p.t) && _isFiniteNum(p.v)) byT.set(p.t, p.v);
  }
  for (const p of _livePts) {
    if (p && Number.isFinite(p.t) && _isFiniteNum(p.v)) byT.set(p.t, p.v);
  }
  return Array.from(byT.entries())
    .map(([t, v]) => ({ t, v }))
    .sort((a, b) => a.t - b.t);
}

function _collapseFlatRuns(pts) {
  if (pts.length < 2) return pts.slice();
  const out = [{ t: pts[0].t, v: pts[0].v }];
  for (let i = 1; i < pts.length; i++) {
    const prev = out[out.length - 1];
    if (Math.abs(pts[i].v - prev.v) <= _FLAT_EPS) {
      prev.t = pts[i].t;
    } else {
      out.push({ t: pts[i].t, v: pts[i].v });
    }
  }
  return out;
}

/** Data-driven x domain: first real point → last real point (no fixed 24h padding). */
function _chartTimeDomain(pts) {
  if (!pts.length) {
    const now = Date.now();
    return { tMin: now, tMax: now, span: 1 };
  }
  const tMin = pts[0].t;
  const tMax = pts[pts.length - 1].t;
  return { tMin, tMax, span: Math.max(tMax - tMin, 1) };
}

function _chartRenderPts(basePts) {
  const collapsed = _collapseFlatRuns(basePts);
  if (!collapsed.length) return [];
  const last = collapsed[collapsed.length - 1];
  const trailV = (_lastLivePnl != null && _isFiniteNum(_lastLivePnl))
    ? _lastLivePnl : last.v;
  const trailT = _lastLiveTs || Date.now();
  if (trailT > last.t && Math.abs(trailV - last.v) <= _FLAT_EPS) {
    return [...collapsed, { t: trailT, v: trailV, trailing: true }];
  }
  return collapsed;
}

function _xForTime(t, tMin, span, PX, drawW) {
  return PX + ((t - tMin) / span) * drawW;
}

function _nearestPtByTime(pts, targetT) {
  if (!pts.length) return null;
  let lo = 0, hi = pts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].t < targetT) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(pts[lo - 1].t - targetT) < Math.abs(pts[lo].t - targetT)) {
    return pts[lo - 1];
  }
  return pts[lo];
}

async function loadHistory(period) {
  if (_loadingHist) return;
  _loadingHist = true;
  try {
    const res = await fetch('/api/history?period=' + period);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data   = await res.json();
    _historyPts  = (data.points || []).filter(p => !p.synthetic);
    _historyLoaded = true;
    _livePts = [];
  } catch(e) {
    console.warn('loadHistory error:', e);
    _historyLoaded = true;
  } finally {
    _loadingHist = false;
  }
  updatePnlChart();
}

function setPeriod(p) {
  _pnlPeriod = p;
  document.querySelectorAll('.pm-per-btn').forEach(b => {
    b.classList.toggle('act', b.id === 'ppb-' + p);
  });
  loadHistory(p);
}

function pushLivePnlPoint(totalPnl) {
  if (!_isFiniteNum(totalPnl)) return;
  const now = Date.now();
  _lastLivePnl = totalPnl;
  _lastLiveTs  = now;

  const lastLive = _livePts.length ? _livePts[_livePts.length - 1] : null;
  const histLast = _historyPts.length ? _historyPts[_historyPts.length - 1] : null;
  const ref = lastLive || histLast;

  if (ref && Math.abs(ref.v - totalPnl) <= _FLAT_EPS) {
    schedulePnlChartUpdate();
    return;
  }

  _livePts.push({ t: now, v: totalPnl });
  if (_livePts.length > 500) _livePts.splice(0, _livePts.length - 500);
  schedulePnlChartUpdate(true);
}

function schedulePnlChartUpdate(force) {
  if (_chartRaf && !force) return;
  if (_chartRaf) cancelAnimationFrame(_chartRaf);
  _chartRaf = requestAnimationFrame(() => {
    _chartRaf = 0;
    updatePnlChart();
  });
}

function updatePnlChart() {
  const basePts = _mergedPts();
  const pts = _chartRenderPts(basePts);
  const { tMin, tMax, span } = _chartTimeDomain(pts);

  const totalPnl       = basePts.length > 0 ? basePts[basePts.length - 1].v : (_lastLivePnl || 0);
  const periodStartVal = basePts.length > 0 ? basePts[0].v : totalPnl;
  const periodEndVal   = totalPnl;
  const periodChange   = periodEndVal - periodStartVal;
  const periodChangePct = periodStartVal !== 0
    ? (periodChange / Math.abs(periodStartVal)) * 100
    : null;

  const bigEl = document.getElementById('pm-bigval');
  if (bigEl) {
    bigEl.textContent = _displayBigVal(totalPnl);
    bigEl.className   = 'pm-big-val ' + (!_balanceVisible ? 'neu'
      : (totalPnl > 0 ? 'pos' : totalPnl < 0 ? 'neg' : 'neu'));
  }

  const triEl = document.getElementById('pm-tri');
  if (triEl) {
    triEl.textContent = periodChange >= 0 ? '▲' : '▼';
    triEl.className   = 'pm-tri ' + (periodChange >= 0 ? 'pos' : 'neg');
  }

  const chEl = document.getElementById('pm-change-lbl');
  if (chEl) {
    if (basePts.length > 1) {
      const arrow = periodChange >= 0 ? '▲' : '▼';
      chEl.textContent = _displayPeriodChange(
        periodChange, periodChangePct, arrow, _periodLabel(_pnlPeriod, tMin, tMax));
      chEl.className   = 'pm-change-lbl ' + (periodChange >= 0 ? 'pos' : 'neg');
    } else {
      chEl.textContent = _historyLoaded ? _periodLabel(_pnlPeriod, tMin, tMax) : 'Loading…';
      chEl.className   = 'pm-change-lbl';
    }
  }

  const chartEl = document.getElementById('pm-chart-box');
  if (!chartEl) return;

  if (pts.length < 2) {
    const old = chartEl.querySelector('svg');
    if (old) old.remove();
    const nd = chartEl.querySelector('#pm-no-data');
    if (nd) {
      nd.textContent   = _historyLoaded ? 'No data for this period yet' : 'Loading history…';
      nd.style.display = '';
    }
    window._pmChart = null;
    return;
  }

  const W = 400, H = 160, PX = 20, PY = 10;
  const vals  = pts.map(p => p.v);
  const minV  = Math.min(...vals);
  const maxV  = Math.max(...vals);
  const padV  = (maxV - minV) * 0.15 || 0.05;
  const lo = minV - padV, hi = maxV + padV;
  const vRange = hi - lo;
  const drawW  = W - PX * 2;
  const drawH  = H - PY * 2;

  const mx = t => _xForTime(t, tMin, span, PX, drawW).toFixed(2);
  const my = v  => (PY + drawH - ((v - lo) / vRange * drawH)).toFixed(2);

  const lineColor  = periodChange >= 0 ? _accentColor() : '#ef4444';
  const lineColor2 = periodChange >= 0 ? _accentDark() : '#dc2626';

  const dLine = pts.map((p, i) => `${i===0?'M':'L'}${mx(p.t)},${my(p.v)}`).join(' ');
  const zeroY = parseFloat(my(0));
  const czY   = Math.max(PY, Math.min(H - PY, zeroY));
  const lastX = parseFloat(mx(pts[pts.length - 1].t));
  const dArea = dLine
    + ` L${lastX.toFixed(2)},${czY.toFixed(2)}`
    + ` L${parseFloat(mx(pts[0].t)).toFixed(2)},${czY.toFixed(2)} Z`;

  const labelCount = 5;
  const xLabels = [];
  for (let i = 0; i < labelCount; i++) {
    const frac = labelCount === 1 ? 0 : i / (labelCount - 1);
    const ts = tMin + frac * span;
    const x = parseFloat(mx(ts));
    const tsStr = _pnlPeriod === '1D'
      ? new Date(ts).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
      : (_pnlPeriod === '1W'
        ? new Date(ts).toLocaleString([], {weekday:'short', hour:'2-digit', minute:'2-digit'})
        : new Date(ts).toLocaleDateString([], {month:'short', day:'numeric'}));
    const anchor = i === 0 ? 'start' : (i === labelCount - 1 ? 'end' : 'middle');
    xLabels.push(`<text x="${x}" y="${H-3}" text-anchor="${anchor}" font-size="8"
      fill="rgba(255,255,255,0.28)"
      font-family="'JetBrains Mono','Consolas',monospace">${tsStr}</text>`);
  }

  let zeroLine = '';
  if (minV < 0 && maxV > 0) {
    zeroLine = `<line x1="${PX}" y1="${czY.toFixed(2)}" x2="${W-PX}" y2="${czY.toFixed(2)}"
      stroke="rgba(255,255,255,0.12)" stroke-width="1" stroke-dasharray="3,3"/>`;
  }

  const lastY = parseFloat(my(pts[pts.length - 1].v));

  window._pmChart = {
    pts, PX, PY, drawW, drawH, W, H,
    tMin, tMax, span,
    period: _pnlPeriod, lo, vRange, lineColor,
    origVal: totalPnl,
    origPeriodChange: periodChange,
    origChangeTxt: chEl ? chEl.textContent : '',
    origChangeCls: chEl ? chEl.className   : '',
  };

  const ndEl = chartEl.querySelector('#pm-no-data');
  if (ndEl) ndEl.style.display = 'none';
  const oldSvg = chartEl.querySelector('svg');
  if (oldSvg) oldSvg.remove();

  const svgEl = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svgEl.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svgEl.setAttribute('preserveAspectRatio','none');
  svgEl.style.cssText = 'display:block;width:100%;height:100%;pointer-events:none';
  svgEl.innerHTML = `
    <defs>
      <linearGradient id="pnlGrd" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%"   stop-color="${lineColor}" stop-opacity="0.38"/>
        <stop offset="65%"  stop-color="${lineColor}" stop-opacity="0.07"/>
        <stop offset="100%" stop-color="${lineColor}" stop-opacity="0"/>
      </linearGradient>
      <linearGradient id="lineGrd" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%"   stop-color="${lineColor2}"/>
        <stop offset="100%" stop-color="${lineColor}"/>
      </linearGradient>
      <filter id="glow">
        <feGaussianBlur stdDeviation="1.5" result="coloredBlur"/>
        <feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <clipPath id="chartClip">
        <rect x="${PX}" y="${PY}" width="${drawW}" height="${drawH+PY}"/>
      </clipPath>
    </defs>
    ${zeroLine}
    <g clip-path="url(#chartClip)">
      <path d="${dArea}" fill="url(#pnlGrd)"/>
      <path d="${dLine}" fill="none" stroke="url(#lineGrd)"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
            filter="url(#glow)"/>
    </g>
    ${xLabels.join('')}
    <circle cx="${lastX}" cy="${lastY}" r="3.5" fill="${lineColor}" filter="url(#glow)"/>
    <line id="xhair-line" x1="${PX}" y1="${PY}" x2="${PX}" y2="${H-14}"
          stroke="rgba(255,255,255,0.6)" stroke-width="1.2"
          stroke-dasharray="3,3" display="none"/>
    <circle id="xhair-dot" cx="${PX}" cy="${PY}" r="4.5"
            fill="${lineColor}" stroke="#111" stroke-width="1.5"
            display="none" filter="url(#glow)"/>`;

  const overlay = chartEl.querySelector('#pm-chart-overlay');
  chartEl.insertBefore(svgEl, overlay);
}

// ── Crosshair ────────────────────────────────────────────────
function _initChartOverlay() {
  const overlay = document.getElementById('pm-chart-overlay');
  if (!overlay) return;

  function _move(clientX) {
    const c = window._pmChart;
    if (!c || !c.pts || c.pts.length < 2) return;
    const box  = document.getElementById('pm-chart-box');
    if (!box) return;
    const rect = box.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const targetT = c.tMin + frac * c.span;
    const pt   = _nearestPtByTime(c.pts, targetT);
    const svgX = c.PX + ((targetT - c.tMin) / c.span) * c.drawW;
    const svgY = c.PY + c.drawH - ((pt.v - c.lo) / c.vRange * c.drawH);
    const line = document.getElementById('xhair-line');
    const dot  = document.getElementById('xhair-dot');
    if (line) { line.setAttribute('x1',svgX.toFixed(2)); line.setAttribute('x2',svgX.toFixed(2));
                line.setAttribute('y1',c.PY); line.setAttribute('y2',c.H-14);
                line.removeAttribute('display'); }
    if (dot)  { dot.setAttribute('cx',svgX.toFixed(2)); dot.setAttribute('cy',svgY.toFixed(2));
                dot.removeAttribute('display'); }
    const bigEl = document.getElementById('pm-bigval');
    if (bigEl) {
      bigEl.textContent = _displayBigVal(pt.v);
      bigEl.className   = 'pm-big-val ' + (!_balanceVisible ? 'neu'
        : (pt.v > 0 ? 'pos' : pt.v < 0 ? 'neg' : 'neu'));
    }
    const triEl = document.getElementById('pm-tri');
    if (triEl) { triEl.textContent=pt.v>=0?'▲':'▼'; triEl.className='pm-tri '+(pt.v>=0?'pos':'neg'); }
    const chEl = document.getElementById('pm-change-lbl');
    if (chEl && pt.t) {
      chEl.textContent = c.period==='1D'
        ? new Date(pt.t).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})
        : new Date(pt.t).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      chEl.className = 'pm-change-lbl';
    }
  }
  function _leave() {
    const c   = window._pmChart;
    const line = document.getElementById('xhair-line');
    const dot  = document.getElementById('xhair-dot');
    if (line) line.setAttribute('display','none');
    if (dot)  dot.setAttribute('display','none');
    if (!c) return;
    const bigEl = document.getElementById('pm-bigval');
    if (bigEl) {
      bigEl.textContent = _displayBigVal(c.origVal);
      bigEl.className   = 'pm-big-val ' + (!_balanceVisible ? 'neu'
        : (c.origVal > 0 ? 'pos' : c.origVal < 0 ? 'neg' : 'neu'));
    }
    const triEl = document.getElementById('pm-tri');
    if (triEl) {
      triEl.textContent = c.origPeriodChange>=0?'▲':'▼';
      triEl.className   = 'pm-tri '+(c.origPeriodChange>=0?'pos':'neg');
    }
    const chEl = document.getElementById('pm-change-lbl');
    if (chEl && c.origChangeTxt) { chEl.textContent=c.origChangeTxt; chEl.className=c.origChangeCls; }
  }
  overlay.addEventListener('mousemove',   e => _move(e.clientX));
  overlay.addEventListener('mouseleave',  _leave);
  overlay.addEventListener('touchstart',  e => _move(e.touches[0].clientX), {passive:true});
  overlay.addEventListener('touchmove',   e => _move(e.touches[0].clientX), {passive:true});
  overlay.addEventListener('touchend',    _leave);
  overlay.addEventListener('touchcancel', _leave);
}

// Refresh history from backend every 2 minutes to pick up any new Redis flushes.
setInterval(() => loadHistory(_pnlPeriod), 2 * 60 * 1000);
setInterval(_refreshMarketCountdowns, 250);
_refreshMarketCountdowns();
setInterval(() => {
  if (_lastLivePnl != null) {
    _lastLiveTs = Date.now();
    schedulePnlChartUpdate();
  }
}, 30 * 1000);

// ═══════════════════════════════════════════════════════════════════
// WEBSOCKET
// ═══════════════════════════════════════════════════════════════════
const connDot = document.getElementById('conn-dot');
const connLabel = document.getElementById('conn-label');
const connStatus = document.getElementById('conn-status');
const _CONN_DOT_BASE = 'w-2.5 h-2.5 rounded-full inline-block flex-shrink-0';

function _setConnStatus(state) {
  if (!connDot || !connLabel) return;
  if (state === 'live') {
    connDot.className = _CONN_DOT_BASE + ' conn-dot-on';
    connLabel.textContent = 'Live';
    if (connStatus) connStatus.className = 'conn-status live';
  } else if (state === 'reconnecting') {
    connDot.className = _CONN_DOT_BASE + ' bg-red-500';
    connLabel.textContent = 'Reconnecting…';
    if (connStatus) connStatus.className = 'conn-status off';
  } else {
    connDot.className = _CONN_DOT_BASE + ' bg-zinc-600';
    connLabel.textContent = 'Connecting…';
    if (connStatus) connStatus.className = 'conn-status';
  }
}

let ws;
function connect() {
  _setConnStatus('connecting');
  const proto = location.protocol==='https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen  = () => _setConnStatus('live');
  ws.onclose = () => {
    _setConnStatus('reconnecting');
    setTimeout(connect, 2500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = e => {
    const d = JSON.parse(e.data);
    window._lastBots = d.bots || [];
    window._lastTradeHistory = d.trade_history || [];
    window._lastGlobalStats = d.global_stats || {};
    renderGlobalStats(d.global_stats);
    renderProfileStats(window._lastBots, window._lastGlobalStats);
    pushLivePnlPoint(d.global_stats.total_pnl ?? 0);
    renderBots(window._lastBots);
    renderPositions(d.positions || []);
    renderTradeHistory(window._lastTradeHistory);
    if (d.config) renderAppConfig(d.config);
  };
}
function renderAppConfig(cfg) {
  const el = document.getElementById('assets-subtitle');
  if (!el || !cfg) return;
  const assets = (cfg.trading_assets || []).join(' · ');
  const interval = cfg.market_interval || '';
  const strat = cfg.strategy_label || cfg.strategy || '';
  const parts = [assets, interval ? `${interval} markets` : '', strat].filter(Boolean);
  el.textContent = parts.join(' · ');
}
connect();

async function loadPositionsAndHistory() {
  try {
    const [pRes, hRes] = await Promise.all([
      fetch('/api/positions'),
      fetch('/api/trades/history?limit=10'),
    ]);
    if (pRes.ok) renderPositions((await pRes.json()).positions || []);
    if (hRes.ok) {
      window._lastTradeHistory = (await hRes.json()).trades || [];
      renderTradeHistory(window._lastTradeHistory);
    }
  } catch (e) { console.warn('loadPositionsAndHistory:', e); }
}
loadPositionsAndHistory();

let _activePortfolioTab = 'trades';
const _cashoutPending = new Set();
const _PORTFOLIO_TABS = ['trades', 'positions', 'history', 'calendar'];

function setPortfolioTab(tab) {
  if (!_PORTFOLIO_TABS.includes(tab)) return;
  _activePortfolioTab = tab;
  _PORTFOLIO_TABS.forEach(t => {
    const btn = document.getElementById('tab-btn-' + t);
    const bottomBtn = document.getElementById('bottom-tab-' + t);
    const panel = document.getElementById('panel-' + t);
    if (btn) btn.classList.toggle('act', tab === t);
    if (bottomBtn) bottomBtn.classList.toggle('act', tab === t);
    if (panel) panel.classList.toggle('hidden', tab !== t);
  });
  if (tab === 'calendar') {
    _calHideTooltip();
    if (!_calData && !_calLoading) loadCalendar(null, null);
  }
}

function _fmtUsd(v, signed) {
  if (!_balanceVisible) return _maskUsd();
  const n = Number(v) || 0;
  const sign = signed && n > 0 ? '+' : (signed && n < 0 ? '' : '');
  return sign + '$' + _fmtMoney(Math.abs(n));
}

function _fmtCents(c) {
  if (!_balanceVisible) return _maskCents();
  return (Number(c) || 0).toFixed(1) + '¢';
}

function _displayTradePnl(dollars, pct) {
  const pctPart = ` (${(Number(pct) || 0).toFixed(1)}%)`;
  if (!_balanceVisible) return _maskUsd() + pctPart;
  const n = Number(dollars) || 0;
  const pnlPos = n >= 0;
  return `${pnlPos ? '+' : ''}$${_fmtMoney(Math.abs(n))}${pctPart}`;
}

function _displayCumulativePnl(val) {
  if (!_balanceVisible) return _maskUsd();
  const n = Number(val) || 0;
  const cumPos = n >= 0;
  return `${cumPos ? '+' : ''}$${_fmtMoney(Math.abs(n))}`;
}

function _displayYesNoPrice(val) {
  if (!_balanceVisible) return _maskCents();
  return (Number(val) || 0) + '¢';
}

function _shortMarket(name) {
  if (!name) return '—';
  return name.length > 48 ? name.slice(0, 46) + '…' : name;
}

function renderPositions(positions) {
  window._lastPositions = positions || [];
  _updatePositionsTabCount(window._lastPositions.length);
  const el = document.getElementById('positions-list');
  if (!el) return;
  if (window._lastPositions.length === 0) {
    el.innerHTML = '<div class="pm-empty">No open positions</div>';
    return;
  }
  if (window._positionsDebugOnce !== true) {
    console.log('[positions] sample snapshot:', JSON.parse(JSON.stringify(window._lastPositions[0])));
    window._positionsDebugOnce = true;
  }
  el.innerHTML = window._lastPositions.map(p => {
    const roi = Number(p.roi_pct) || 0;
    const pnl = Number(p.unrealized_pnl) || 0;
    const roiCls = roi >= 0 ? 'pos' : 'neg';
    const pending = _cashoutPending.has(`${p.asset}:${p.window}`);
    const cashoutKey = `${p.asset}:${p.window}`;
    const disabled = pending || !p.cashout_available;
    const btnLabel = pending ? 'Selling…' : 'Sell';
    const sideCls = p.side === 'YES' ? 'pm-side-yes' : p.side === 'NO' ? 'pm-side-no' : 'pm-side-spread';
    const hasBothLegs = Number(p.yes_shares || 0) > 0 && Number(p.no_shares || 0) > 0;
    const isSpread = p.side === 'SPREAD' || hasBothLegs;
    const entryDetail = isSpread
      ? `<span>Y ${Number(p.yes_shares||0).toFixed(1)}@${_fmtCents(p.yes_avg_price_c)} → ${_fmtCents(p.yes_bid_c)} · N ${Number(p.no_shares||0).toFixed(1)}@${_fmtCents(p.no_avg_price_c)} → ${_fmtCents(p.no_bid_c)}${p.pair_avg_price_c ? ' · pair '+_fmtCents(p.pair_avg_price_c)+' avg' : ''}</span>`
      : `<span class="${sideCls}">${p.side}</span><span>${_fmtCents(p.entry_price_cents)} → ${_fmtCents(p.current_price_cents)}</span>`;
    return `
      <div class="pm-row" data-asset="${p.asset}" data-window="${p.window || '5m'}">
        <div class="pm-row-main">
          <div class="pm-row-title">${_shortMarket(p.market)}</div>
          <div class="pm-row-sub">
            <span>${p.asset} ${p.window || '5m'}</span>
            ${entryDetail}
            <span>${Number(p.size).toFixed(2)} sh</span>
          </div>
        </div>
        <div class="pm-row-stats">
          <span class="pm-row-val ${roiCls}">${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%</span>
          <span class="pm-row-meta ${roiCls}">${_fmtUsd(pnl, true)}</span>
        </div>
        ${isSpread ? '' : `<button class="pm-cashout-btn" ${disabled ? 'disabled' : ''}
                onclick="cashoutPosition('${p.asset}','${p.window || '5m'}')">${btnLabel}</button>`}
      </div>`;
  }).join('');
}

function _historyActionStyle(action) {
  const a = (action || 'buy').toLowerCase();
  if (a === 'redeem') return { label: 'redeem', cls: 'pm-action-redeem' };
  if (a === 'sell')   return { label: 'sell',   cls: 'pm-action-sell' };
  return { label: 'buy', cls: 'pm-action-buy' };
}

function renderTradeHistory(trades) {
  window._lastTradeHistory = trades || [];
  const el = document.getElementById('history-list');
  if (!el) return;
  if (!trades || trades.length === 0) {
    el.innerHTML = '<div class="pm-empty">No trade history yet</div>';
    return;
  }
  el.innerHTML = trades.map(t => {
    const { label, cls } = _historyActionStyle(t.action);
    const isSpreadTrade = (t.side || '') === 'SPREAD';
    const sideCls = isSpreadTrade ? 'pm-side-spread' : (t.side === 'YES' ? 'pm-side-yes' : 'pm-side-no');
    const ts = t.timestamp_ms
      ? new Date(t.timestamp_ms).toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true})
      : (t.timestamp || '—');
    const detail = isSpreadTrade
      ? `<span class="${sideCls}">SPREAD</span><span>Y ${Number(t.yes_size||0).toFixed(1)}@${_fmtCents((Number(t.yes_price)||0)*100)} · N ${Number(t.no_size||0).toFixed(1)}@${_fmtCents((Number(t.no_price)||0)*100)}</span>`
      : `<span class="${sideCls}">${t.side || ''}</span><span>${_fmtCents((Number(t.price) || 0) * 100)}</span>`;
    const sizeLabel = isSpreadTrade
      ? `${Number(t.size||0).toFixed(2)} pair sh`
      : `${Number(t.size).toFixed(2)} sh`;
    return `
      <div class="pm-row">
        <div class="pm-row-main">
          <div class="pm-row-title">${_shortMarket(t.market)}</div>
          <div class="pm-row-sub">
            <span>${t.asset || ''}</span>
            <span class="${cls}">${label}</span>
            ${detail}
            <span>${sizeLabel}</span>
          </div>
        </div>
        <div class="pm-row-stats">
          <span class="pm-row-meta">${ts}</span>
        </div>
      </div>`;
  }).join('');
}

async function cashoutPosition(asset, window) {
  const key = `${asset}:${window || '5m'}`;
  if (_cashoutPending.has(key)) return;
  _cashoutPending.add(key);
  renderPositions(window._lastPositions || []);
  try {
    const res = await fetch('/api/positions/' + encodeURIComponent(asset.toLowerCase()) + '/'
      + encodeURIComponent((window || '5m').toLowerCase()) + '/cashout', {
      method: 'POST',
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.error || 'Cashout failed');
    }
  } catch (e) {
    alert('Cashout request failed');
    console.warn(e);
  } finally {
    _cashoutPending.delete(key);
    loadPositionsAndHistory();
  }
}

// ═══════════════════════════════════════════════════════════════════
// GLOBAL STATS
// ═══════════════════════════════════════════════════════════════════
function renderProfileStats(bots, g) {
  let positionsValue = 0;
  for (const bot of (bots || [])) {
    const ySh = Number(bot.yes_shares) || 0;
    const nSh = Number(bot.no_shares) || 0;
    positionsValue += ySh * (Number(bot.yes_avg_price_c) || 0) / 100;
    positionsValue += nSh * (Number(bot.no_avg_price_c) || 0) / 100;
  }
  const biggestWin = Number(g?.biggest_win) || 0;
  let predictions = Number(g?.closed_trades) || 0;
  if (!predictions) {
    for (const bot of (bots || [])) {
      predictions += Number(bot.trade_count) || 0;
    }
  }
  _profileStats = { positionsValue, biggestWin, predictions };

  const posEl = document.getElementById('ps-positions-value');
  const winEl = document.getElementById('ps-biggest-win');
  const predEl = document.getElementById('ps-predictions');

  if (posEl) {
    if (!_balanceVisible) {
      posEl.textContent = _maskUsd();
      posEl.className = 'ps-stat-val neu';
    } else {
      const n = positionsValue;
      posEl.textContent = (n >= 0 ? '' : '-') + '$' + _fmtMoney(Math.abs(n));
      posEl.className = 'ps-stat-val ' + (n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu');
    }
  }

  if (winEl) {
    if (!_balanceVisible) {
      winEl.textContent = _maskUsd();
      winEl.className = 'ps-stat-val neu';
    } else if (biggestWin === 0) {
      winEl.textContent = '$0.00';
      winEl.className = 'ps-stat-val neu';
    } else {
      winEl.textContent = '+$' + _fmtMoney(biggestWin);
      winEl.className = 'ps-stat-val win';
    }
  }

  if (predEl) {
    predEl.textContent = String(predictions);
    predEl.className = 'ps-stat-val text-zinc-100';
  }
}

function renderGlobalStats(g) {
  const el = id => document.getElementById(id);
  if (el('pm-st-trades')) el('pm-st-trades').textContent = g.total_trades ?? 0;
  if (el('pm-st-wins'))   el('pm-st-wins').textContent   = g.total_wins   ?? 0;
  if (el('pm-st-losses')) el('pm-st-losses').textContent = g.total_losses ?? 0;
  if (el('pm-st-wr'))     el('pm-st-wr').textContent     = (g.win_rate??0).toFixed(1)+'%';

  // ── Trading schedule banner ──────────────────────────────────────
  // Reflects the server-side is_trading_allowed() result broadcast
  // through global_stats.trading_allowed on every WebSocket tick.
  // The banner updates automatically the moment the day changes —
  // no page refresh needed.
  const banner   = el('schedule-banner');
  const dot      = el('schedule-dot');
  const schedTxt = el('schedule-text');
  const BANNER_BASE = 'mb-4 px-4 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border';
  const DOT_BASE = 'w-2 h-2 rounded-full inline-block flex-shrink-0';
  if (banner && dot && schedTxt) {
    const allowed  = g.trading_allowed ?? true;
    const tz       = g.trading_tz || 'UTC';
    if (allowed) {
      banner.className = BANNER_BASE;
      banner.style.background = 'var(--accent-glow)';
      banner.style.color = 'var(--accent-muted)';
      banner.style.borderColor = 'var(--accent-border-soft)';
      dot.className = DOT_BASE;
      dot.style.background = 'var(--accent-muted)';
      schedTxt.textContent =
        `✅ Trading ACTIVE — new entries permitted (${tz})`;
    } else {
      banner.className = BANNER_BASE;
      banner.style.background = 'rgba(234,179,8,0.12)';
      banner.style.color = '#facc15';
      banner.style.borderColor = '#713f12';
      dot.className = DOT_BASE;
      dot.style.background = '#facc15';
      schedTxt.textContent =
        `🚫 Weekend — new entries BLOCKED (${tz}). Stop-loss and portfolio tracking continue.`;
    }
  }
}

// ═══════════════════════════════════════════════════════════════════
// SIGNAL HELPERS
// ═══════════════════════════════════════════════════════════════════
function sigClass(s){
  if(!s)return'sig-neutral';const u=s.toUpperCase();
  if(u.includes('STALE'))return'sig-neutral';
  if(u.includes('BULL'))return'sig-strong-bull';
  if(u.includes('BEAR'))return'sig-strong-bear';
  if(u.includes('STRONGLY')&&u.includes('BULL'))return'sig-strong-bull';
  if(u.includes('STRONGLY')&&u.includes('BEAR'))return'sig-strong-bear';
  if(u.includes('MILDLY')&&u.includes('BULL'))return'sig-mild-bull';
  if(u.includes('MILDLY')&&u.includes('BEAR'))return'sig-mild-bear';
  return'sig-neutral';
}
function pillClass(s){
  if(!s)return'pill-neutral';const u=s.toUpperCase();
  if(u.includes('STALE'))return'pill-neutral';
  if(u.includes('BULL'))return'pill-strong-bull';
  if(u.includes('BEAR'))return'pill-strong-bear';
  if(u.includes('STRONGLY')&&u.includes('BULL'))return'pill-strong-bull';
  if(u.includes('STRONGLY')&&u.includes('BEAR'))return'pill-strong-bear';
  if(u.includes('MILDLY')&&u.includes('BULL'))return'pill-mild-bull';
  if(u.includes('MILDLY')&&u.includes('BEAR'))return'pill-mild-bear';
  return'pill-neutral';
}
function sigIcon(s){
  if(!s)return'—';const u=s.toUpperCase();
  if(u.includes('STALE'))return'⚠️';
  if(u.includes('BULL'))return'🚀';
  if(u.includes('BEAR'))return'🔻';
  if(u.includes('STRONGLY')&&u.includes('BULL'))return'🚀';
  if(u.includes('STRONGLY')&&u.includes('BEAR'))return'🔻';
  if(u.includes('MILDLY')&&u.includes('BULL'))return'📈';
  if(u.includes('MILDLY')&&u.includes('BEAR'))return'📉';
  return'➖';
}
function formatMarketWindow(s,e){
  if(!s||!e)return'Waiting for market…';
  try{
    const o={timeZone:'America/New_York'};
    const sd=new Date(s).toLocaleString('en-US',{...o,month:'short',day:'numeric'});
    const st=new Date(s).toLocaleString('en-US',{...o,hour:'numeric',minute:'2-digit',hour12:true});
    const et=new Date(e).toLocaleString('en-US',{...o,hour:'numeric',minute:'2-digit',hour12:true});
    return`${sd}, ${st} – ${et} ET`;
  }catch(err){return'Waiting for market…';}
}

// ═══════════════════════════════════════════════════════════════════
// BOT CARDS
// ═══════════════════════════════════════════════════════════════════
function renderBots(bots){
  window._lastBots = bots || [];
  const c=document.getElementById('bots-container');
  if(!c) return;
  window._lastBots.forEach((bot,i)=>{
    let card=document.getElementById(`bot-card-${i}`);
    if(!card){
      card=document.createElement('div');
      card.id=`bot-card-${i}`;
      c.appendChild(card);
    }
    const pnlBorder = _botCardPnlBorderClass(bot);
    card.className = ['card', 'bg-zinc-900', 'rounded-3xl', 'p-5', 'md:p-6', pnlBorder].filter(Boolean).join(' ');
    card.innerHTML=renderCard(bot);
  });
  // Remove stale cards when asset count decreases (e.g. config change).
  let i=window._lastBots.length;
  while(document.getElementById(`bot-card-${i}`)){
    document.getElementById(`bot-card-${i}`).remove();
    i++;
  }
}
function _formatMarketCountdown(iso) {
  if (!iso) return '--:--';
  const end = new Date(iso).getTime();
  if (!Number.isFinite(end)) return '--:--';
  const seconds = Math.max(0, Math.ceil((end - Date.now()) / 1000));
  const m = Math.floor(seconds / 60);
  const sec = seconds % 60;
  return `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
}

function _refreshMarketCountdowns() {
  document.querySelectorAll('[data-market-countdown]').forEach(el => {
    el.textContent = _formatMarketCountdown(el.getAttribute('data-market-countdown'));
  });
}

function _fmtCooldownRemaining(sec) {
  const s = Math.max(0, Number(sec) || 0);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${String(r).padStart(2, '0')}s`;
}

function _botHasInventory(bot) {
  const ySh = Number(bot.yes_shares) || 0;
  const nSh = Number(bot.no_shares) || 0;
  if (ySh > 0 || nSh > 0) return true;
  const side = String(bot.position_side || '').trim();
  const sz = Number(bot.position_size) || 0;
  return side.length > 0 && sz > 0;
}

function _botCardPnlBorderClass(bot) {
  const hasInv = _botHasInventory(bot);
  const positionRow = _fmtPositionRow(bot);
  const hasPos = positionRow !== '-' || hasInv;
  const unrealized = Number(bot.pnl_dollars) || 0;
  const inProfit = hasInv && unrealized > 0;
  const inLoss = hasInv && unrealized < 0;
  if (inProfit) return 'border-l-4 border-l-accent';
  if (inLoss) return 'border-l-4 border-red-500';
  if (hasPos) return 'border-l-4 border-sky-500';
  return '';
}

function _legAvgCents(bot, leg) {
  const isYes = leg === 'YES';
  const sh = Number(isYes ? bot.yes_shares : bot.no_shares) || 0;
  if (sh <= 0) return 0;
  let c = Number(isYes ? bot.yes_avg_price_c : bot.no_avg_price_c) || 0;
  if (c <= 0) {
    const p = Number(isYes ? bot.yes_avg_price : bot.no_avg_price) || 0;
    if (p > 0) c = p <= 1 ? p * 100 : p;
  }
  if (c <= 0) {
    const bought = String(bot.bought_side || bot.initial_entry_side || bot.position_side || '').toUpperCase();
    if (bought === leg) {
      c = Number(bot.entry_fill_price_c || bot.initial_entry_fill_c) || 0;
    }
  }
  return c;
}

function _fmtPositionRow(bot) {
  const ySh = Number(bot.yes_shares) || 0;
  const nSh = Number(bot.no_shares) || 0;
  if (ySh <= 0 && nSh <= 0) {
    const side = String(bot.position_side || '').toUpperCase();
    const sz = Number(bot.position_size) || 0;
    if (side && sz > 0) {
      const fillC = Number(bot.entry_fill_price_c || bot.initial_entry_fill_c) || 0;
      if (fillC > 0) return `${side} ${sz.toFixed(1)} sh @ ${fillC.toFixed(1)}c`;
      return `${side} ${sz.toFixed(1)} sh`;
    }
    const pos = String(bot.position || '').trim();
    if (pos && pos !== '-') return pos;
    return '-';
  }
  const parts = [];
  if (ySh > 0) {
    const yC = _legAvgCents(bot, 'YES');
    parts.push(yC > 0 ? `YES ${ySh.toFixed(1)} sh @ ${yC.toFixed(1)}c` : `YES ${ySh.toFixed(1)} sh`);
  }
  if (nSh > 0) {
    const nC = _legAvgCents(bot, 'NO');
    parts.push(nC > 0 ? `NO ${nSh.toFixed(1)} sh @ ${nC.toFixed(1)}c` : `NO ${nSh.toFixed(1)} sh`);
  }
  let text = parts.join(' · ');
  if (ySh > 0 && nSh > 0) {
    const pairC = Number(bot.pair_avg_price_c) || (_legAvgCents(bot, 'YES') + _legAvgCents(bot, 'NO'));
    if (pairC > 0) text += ` (pair ${pairC.toFixed(1)}c)`;
  }
  return text;
}

function _isInventoryAccumulation(bot) {
  return !!bot.inventory_accumulation_enabled;
}

function _fmtLegSpend(bot, leg) {
  const isYes = leg === 'YES';
  const sh = Number(isYes ? bot.yes_shares : bot.no_shares) || 0;
  const spent = Number(isYes ? bot.yes_spent_usd : bot.no_spent_usd) || 0;
  const avgC = _legAvgCents(bot, leg);
  const label = isYes ? 'YES (UP)' : 'NO (DOWN)';
  if (sh <= 0) {
    return `<div class="flex items-center justify-between gap-2 py-1">
      <span class="text-zinc-400">${label}</span>
      <span class="font-mono text-zinc-500">—</span>
    </div>`;
  }
  const avgTxt = avgC > 0 ? `@ ${avgC.toFixed(1)}¢` : '';
  return `<div class="flex items-center justify-between gap-2 py-1">
    <span class="text-zinc-400">${label}</span>
    <span class="font-mono text-zinc-100 text-right">${sh.toFixed(1)} sh ${avgTxt}<br><span class="text-zinc-400 text-xs">${_fmtUsd(spent)} spent</span></span>
  </div>`;
}

function _fmtUsdCompact(v) {
  const n = Number(v) || 0;
  return '$' + n.toFixed(2);
}

function _renderInventoryStatusBar(bot) {
  const targetC = Number(bot.target_combined_pair_cost_c) || 98;
  const combinedC = Number(bot.matched_combined_cost_c || bot.pair_avg_price_c) || 0;
  const phase = String(bot.accumulation_phase || bot.strategy_state || '—').replace(/_/g, ' ');
  const next = String(bot.inventory_next_action || '—').replace(/_/g, ' ');
  const combinedCls = combinedC > 0 && combinedC <= targetC + 0.5 ? 'text-accent' : combinedC > targetC ? 'text-yellow-400' : 'text-zinc-300';
  return `<div class="text-xs text-zinc-400 font-mono flex flex-wrap gap-x-3 gap-y-1">
    <span>Target pair <span class="text-orange-400">${targetC.toFixed(0)}¢</span></span>
    <span>Combined <span class="${combinedCls}">${combinedC > 0 ? combinedC.toFixed(1) + '¢' : '—'}</span></span>
    <span>Phase <span class="text-zinc-200">${phase}</span></span>
    <span>Next <span class="text-sky-300">${next}</span></span>
  </div>`;
}

function _normalizeLegFill(fill) {
  if (!fill || typeof fill !== 'object') return null;
  if (fill.YES || fill.NO) return fill;
  const side = String(fill.side || '').toUpperCase();
  if (!side) return null;
  const qty = Number(fill.qty) || 0;
  let c = Number(fill.price_c) || 0;
  if (c <= 0 && fill.price) c = Number(fill.price) * 100;
  const out = {
    YES: { qty: 0, price_c: 0 },
    NO: { qty: 0, price_c: 0 },
    kind: fill.kind || '',
  };
  out[side] = { qty, price_c: c };
  return out;
}

function _fmtLegFillSide(slot) {
  if (!slot) return '—';
  const qty = Number(slot.qty) || 0;
  if (qty <= 0) return '—';
  let c = Number(slot.price_c) || 0;
  if (c <= 0 && slot.price) c = Number(slot.price) * 100;
  return `${qty.toFixed(1)} sh @ ${c.toFixed(1)}¢`;
}

function _renderPairLegBlock(label, rawFill) {
  const fill = _normalizeLegFill(rawFill);
  const kind = fill && fill.kind === 'emergency_hedge'
    ? ' <span class="text-yellow-500/90">emergency</span>'
    : fill && (fill.kind === 'ideal_hedge' || fill.kind === 'initial_hedge_allowed')
      ? ' <span class="text-sky-400/90">hedge</span>'
      : '';
  return `<div class="mb-2.5">
    <div class="text-zinc-500 text-xs mb-1">${label}${kind}</div>
    <div class="flex justify-between gap-2 text-xs py-0.5">
      <span class="text-accent font-medium">YES</span>
      <span class="font-mono text-zinc-100">${_fmtLegFillSide(fill && fill.YES)}</span>
    </div>
    <div class="flex justify-between gap-2 text-xs py-0.5">
      <span class="text-red-400 font-medium">NO</span>
      <span class="font-mono text-zinc-100">${_fmtLegFillSide(fill && fill.NO)}</span>
    </div>
  </div>`;
}

function _renderInventoryDetailGrid(bot) {
  const total = Number(bot.total_invested_usd || bot.invested) || 0;
  const matched = Number(bot.matched_pairs) || 0;
  const reason = String(bot.inventory_action_reason || bot.entry_block_reason || '').trim();
  return `<div class="bg-zinc-800/60 rounded-xl px-3 py-2 mb-3 text-sm font-mono">
    <div class="text-xs text-zinc-500 uppercase tracking-wide mb-2">Pair entry fills</div>
    ${_renderPairLegBlock('First leg fill', bot.first_leg_fill)}
    ${_renderPairLegBlock('Second leg fill', bot.second_leg_fill)}
    <div class="text-xs text-zinc-500 uppercase tracking-wide mt-3 mb-2">Inventory legs (total)</div>
    ${_fmtLegSpend(bot, 'YES')}
    ${_fmtLegSpend(bot, 'NO')}
    <div class="border-t border-zinc-700/80 mt-2 pt-2 space-y-1 text-xs">
      <div class="flex justify-between"><span class="text-zinc-400">Combined pair cost</span><span class="text-zinc-100">${Number(bot.matched_combined_cost_c || bot.pair_avg_price_c) > 0 ? Number(bot.matched_combined_cost_c || bot.pair_avg_price_c).toFixed(1) + '¢' : '—'}</span></div>
      <div class="flex justify-between"><span class="text-zinc-400">Total spent</span><span class="text-zinc-100">${_fmtUsdCompact(total)}</span></div>
      <div class="flex justify-between"><span class="text-zinc-400">Matched pairs</span><span class="text-zinc-100">${matched > 0 ? matched.toFixed(1) : '—'}</span></div>
      ${reason ? `<div class="col-span-2 text-zinc-500 pt-1">${reason}</div>` : ''}
    </div>
  </div>`;
}

function renderCard(bot){
  const hasInv = _botHasInventory(bot);
  const positionRow = _fmtPositionRow(bot);
  const hasPos = positionRow !== '-' || hasInv;
  const pnlPos=(bot.pnl_dollars||0)>=0;
  const cumPos=(bot.cumulative_pnl||0)>=0;
  const inProfit=hasInv&&(bot.pnl_dollars||0)>0;
  const inLoss=hasInv&&(bot.pnl_dollars||0)<0;
  const stratLabel=bot.strategy_name||bot.strategy_label||'BTC/ETH Momentum';
  const entryC=Number(bot.max_entry_price_c||bot.minimum_entry_price_c||92);
  const minEdge=Number(bot.min_net_edge_pct||5);
  const momDir=String(bot.momentum_direction||'—');
  const momSignal=String(bot.momentum_signal_display||'—');
  const yesEdge=Number(bot.yes_net_edge||0);
  const noEdge=Number(bot.no_net_edge||0);
  const limitC=Number(bot.buy_limit_price_c||0);
  const stopPct=Number(bot.stop_loss_pct||0.20);
  const tpPct=Number(bot.take_profit_pct||0.10);
  const stopTrigC=Number(bot.stop_loss_trigger_c||0);
  const tpTrigC=Number(bot.take_profit_trigger_c||0);
  const stopStatus=String(bot.stop_loss_status||'Monitoring');
  const initSide=String(bot.initial_entry_side||bot.position_side||'').toUpperCase();
  const initFillC=Number(bot.initial_entry_fill_c||bot.entry_fill_price_c||0);
  const initQty=Number(bot.initial_entry_quantity||bot.filled_size||0);
  const sigScore=Number(bot.signal_quality_score||0);
  const sigThreshold=Number(bot.signal_quality_threshold||60);
  const netEdge=Number(bot.entry_net_edge||0);
  const tpExits=Number(bot.take_profit_exits||0);
  const slExits=Number(bot.stop_loss_exits||0);
  const totalFees=Number(bot.fees_paid||0);
  const exitFillC=Number(bot.exit_fill_price_c||0);
  const exitReason=String(bot.exit_reason||'');
  const displayState=String(bot.strategy_display_state||bot.status||'WAITING FOR ENTRY');
  const posSide=String(bot.position_side||'').toUpperCase();
  const entryFillC=Number(bot.entry_fill_price_c||0);
  const bidC=Number(bot.executable_bid_c||0);
  const stoppedOut=!!bot.market_stopped_out;
  const entryOrd=String(bot.entry_order_status||'IDLE');
  const exitOrd=String(bot.exit_order_status||'IDLE');
  let signal=displayState;
  if(stoppedOut&&!hasPos) signal='MARKET BLOCKED AFTER STOP LOSS';
  const yesAskC=Number(bot.yes_ask_c||bot.yes||0);
  const noAskC=Number(bot.no_ask_c||bot.no||0);
  const wins=bot.wins??0;const losses=bot.losses??0;
  const trades=bot.trade_count??0;const wr=bot.win_rate??0;
  const mw=formatMarketWindow(bot.market_start_iso,bot.market_end_iso);
  const label=`${bot.asset||'BOT'}`;
  const badge=inProfit
    ?`<span class="text-xs badge-accent px-2 py-0.5 rounded-full font-semibold">🟢 IN PROFIT</span>`
    :inLoss
      ?`<span class="text-xs bg-red-950 text-red-400 px-2 py-0.5 rounded-full font-semibold">🔴 IN LOSS</span>`
      :hasPos
        ?`<span class="text-xs bg-sky-900 text-sky-300 px-2 py-0.5 rounded-full">⚡ IN POSITION</span>`
        :`<span class="text-xs bg-zinc-800 text-zinc-400 px-2 py-0.5 rounded-full">WAITING</span>`;
  const invMode = _isInventoryAccumulation(bot);
  const blockedBanner=!invMode&&stoppedOut?`
      <div class="bg-red-950/40 border border-red-900/60 rounded-xl px-3 py-2 mb-3 text-xs text-red-300">
        <div class="font-semibold">Market blocked after stop loss — no re-entry</div>
      </div>`:'';
  const statusBarInner = invMode
    ? _renderInventoryStatusBar(bot)
    : `<span class="inline-flex items-center gap-1.5 text-sm font-semibold px-2.5 py-1 rounded-full ${pillClass(signal)}">
          ${sigIcon(signal)} ${signal}
        </span>
        <div class="text-xs text-zinc-400 font-mono">
          edge ≥ <span class="text-orange-400">${minEdge.toFixed(1)}pp</span>
          &nbsp;· max <span class="text-orange-400">${entryC.toFixed(1)}¢</span>
          &nbsp;· ${momDir}
          &nbsp;· TP <span class="text-accent">+${(tpPct*100).toFixed(0)}%</span>
          &nbsp;· stop <span class="text-red-400">-${(stopPct*100).toFixed(0)}%</span>
        </div>`;
  const detailGrid = invMode
    ? _renderInventoryDetailGrid(bot)
    : `<div class="bg-zinc-800/60 rounded-xl px-3 py-2 mb-3 text-xs font-mono text-zinc-300 grid grid-cols-2 gap-x-3 gap-y-1">
        <span class="col-span-2">${stratLabel} · ${displayState}${bot.entry_block_reason?(' · '+bot.entry_block_reason):''}</span>
        <span>Momentum: ${momDir} · ${momSignal}</span>
        <span>Signal quality: ${sigScore?sigScore.toFixed(0)+'/'+sigThreshold:'—'} · net edge ${(netEdge*100).toFixed(1)}pp</span>
        <span>Position: ${posSide||initSide||'—'} ${initQty?initQty.toFixed(1)+' sh @ '+initFillC.toFixed(1)+'c':'—'}</span>
        <span>Take-profit: +${(tpPct*100).toFixed(0)}% @ ${tpTrigC?tpTrigC.toFixed(1)+'c':'—'}</span>
        <span>Stop-loss: -${(stopPct*100).toFixed(0)}% @ ${stopTrigC?stopTrigC.toFixed(1)+'c':'—'} · ${stopStatus}</span>
        <span>YES ask: ${yesAskC?yesAskC.toFixed(1)+'c':'—'}</span>
        <span>NO ask: ${noAskC?noAskC.toFixed(1)+'c':'—'}</span>
        <span>Max entry: ${entryC.toFixed(1)}c</span>
        <span>YES edge: ${(yesEdge*100).toFixed(1)}pp</span>
        <span>NO edge: ${(noEdge*100).toFixed(1)}pp</span>
        <span>Buy limit: ${limitC?limitC.toFixed(1)+'c':'—'}</span>
        <span>Bid: ${bidC?bidC.toFixed(1)+'c':'—'}</span>
        <span>TP @: ${tpTrigC?tpTrigC.toFixed(1)+'c':'—'}</span>
        <span>Stop @: ${stopTrigC?stopTrigC.toFixed(1)+'c':'—'}</span>
        <span>Exit fill: ${exitFillC?exitFillC.toFixed(1)+'c':'—'} · ${exitReason||'—'}</span>
        <span>TP exits: ${tpExits} · SL exits: ${slExits}</span>
        <span>Fees: $${totalFees.toFixed(2)}</span>
        <span>Entry ord: ${entryOrd}</span>
        <span>Exit ord: ${exitOrd}</span>
      </div>`;
  const statusBarWrapClass = invMode
    ? 'bg-zinc-800 rounded-xl px-3 py-2 mb-3 flex flex-col gap-1'
    : 'bg-zinc-800 rounded-xl px-3 py-2 mb-3 flex items-center justify-between gap-2 flex-wrap';
  return`
    <div class="rounded-2xl">
      <div class="flex items-center justify-between mb-1">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-xl font-black tracking-tight" style="font-family:'Inter',system-ui,sans-serif;letter-spacing:-.02em;">${label}</span>
          ${badge}
        </div>
        <span class="text-zinc-500 text-xs font-mono" data-market-countdown="${bot.market_end_iso||''}">${bot.timer||'--:--'}</span>
      </div>
      <div class="text-zinc-500 text-xs mb-3 font-mono">${mw}</div>
      ${blockedBanner}
      <div class="flex gap-3 mb-3">
        <div class="flex-1 bg-zinc-800 rounded-xl p-2 text-center">
          <div class="text-zinc-400 text-xs mb-0.5">YES</div>
          <div class="text-lg font-bold text-accent font-mono" style="font-variant-numeric:tabular-nums;">${_displayYesNoPrice(bot.yes||0)}</div>
        </div>
        <div class="flex-1 bg-zinc-800 rounded-xl p-2 text-center">
          <div class="text-zinc-400 text-xs mb-0.5">NO</div>
          <div class="text-lg font-bold text-red-400 font-mono" style="font-variant-numeric:tabular-nums;">${_displayYesNoPrice(bot.no||0)}</div>
        </div>
      </div>
      <div class="${statusBarWrapClass}">
        ${invMode ? `<div class="text-xs font-semibold text-zinc-300">${stratLabel}</div>` : ''}
        ${statusBarInner}
      </div>
      ${detailGrid}
      <div class="space-y-1.5 text-sm mb-3">
        ${invMode ? '' : `<div class="flex items-center justify-between">
          <span class="text-zinc-400">Position</span>
          <span class="font-mono text-zinc-200">${positionRow}</span>
        </div>`}
        <div class="flex items-center justify-between">
          <span class="text-zinc-400">Unrealized P&amp;L</span>
          <span class="font-mono ${pnlPos?'text-accent':'text-red-400'}" style="font-variant-numeric:tabular-nums;">
            ${_displayTradePnl(bot.unrealized_pnl ?? bot.pnl_dollars, bot.pnl_pct)}
          </span>
        </div>
        <div class="flex items-center justify-between">
          <span class="text-zinc-400">Realized P&amp;L</span>
          <span class="font-mono text-zinc-300" style="font-variant-numeric:tabular-nums;">
            ${_displayCumulativePnl(bot.realized_pnl ?? bot.cumulative_realized_pnl)}
          </span>
        </div>
        <div class="flex items-center justify-between">
          <span class="text-zinc-400">Cumulative realized</span>
          <span class="font-mono text-zinc-300" style="font-variant-numeric:tabular-nums;">
            ${_displayCumulativePnl(bot.cumulative_realized_pnl ?? bot.cumulative_pnl)}
          </span>
        </div>
        <div class="flex items-center justify-between">
          <span class="text-zinc-400">Overall net P&amp;L</span>
          <span class="font-mono font-semibold ${(Number(bot.overall_net_pnl||bot.total_net_pnl||0)>=0)?'text-accent':'text-red-300'}" style="font-variant-numeric:tabular-nums;">
            ${_displayCumulativePnl(bot.overall_net_pnl ?? bot.total_net_pnl)}
          </span>
        </div>
        <div class="flex items-center justify-between">
          <span class="text-zinc-400">Outcome</span>
          <span class="font-mono ${bot.outcome==='YES'?'text-accent':bot.outcome==='NO'?'text-red-400':'text-zinc-500'}">
            ${bot.outcome||'PENDING'}
          </span>
        </div>
      </div>
      <div class="bg-zinc-800/60 rounded-xl px-3 py-2 mb-3 flex items-center justify-between text-xs">
        <div class="flex flex-col items-center"><span class="font-bold text-zinc-200 font-mono">${trades}</span><span class="text-zinc-500 uppercase" style="font-size:9px;letter-spacing:.08em">Trades</span></div>
        <div class="flex flex-col items-center"><span class="font-bold text-accent font-mono">${wins}</span><span class="text-zinc-500 uppercase" style="font-size:9px;letter-spacing:.08em">Wins</span></div>
        <div class="flex flex-col items-center"><span class="font-bold text-red-400 font-mono">${losses}</span><span class="text-zinc-500 uppercase" style="font-size:9px;letter-spacing:.08em">Losses</span></div>
        <div class="flex flex-col items-center"><span class="font-bold text-yellow-400 font-mono">${wr.toFixed(1)}%</span><span class="text-zinc-500 uppercase" style="font-size:9px;letter-spacing:.08em">Win Rate</span></div>
      </div>
      <div class="pt-2 border-t border-zinc-800 flex items-center justify-between text-xs text-zinc-500">
        <span>Listener in</span>
        <span class="font-mono">${bot.listener||'--:--'}</span>
      </div>
    </div>`;
}

// ═══════════════════════════════════════════════════════════════════
// PNL CALENDAR HEATMAP
// ═══════════════════════════════════════════════════════════════════

let _calYear = null;
let _calMonth = null;
let _calData = null;
let _calLoading = false;

const _CAL_WEEKDAYS_SUN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const _CAL_WEEKDAYS_MON = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _CAL_MONTHS = ['January','February','March','April','May','June',
                     'July','August','September','October','November','December'];

function _calDaysInMonth(year, month) {
  return new Date(year, month, 0).getDate();
}

function _calPadOffset(year, month, weekStart) {
  const dow = new Date(year, month - 1, 1).getDay();
  return weekStart === 1 ? (dow + 6) % 7 : dow;
}

function _calFmtSignedUsd(val) {
  if (!_balanceVisible) return _maskUsd();
  const n = Number(val) || 0;
  const sign = n >= 0 ? '+' : '-';
  return sign + '$' + _fmtMoney(Math.abs(n));
}

function _calIntensity(pnl, scaleMax) {
  const sm = Math.max(Number(scaleMax) || 100, 0.01);
  return Math.min(Math.abs(Number(pnl) || 0) / sm, 1);
}

function _calCellStyle(pnl, scaleMax) {
  if (!pnl) return '';
  return '--cal-i:' + _calIntensity(pnl, scaleMax).toFixed(3) + ';';
}

function _calIsFutureDay(year, month, day) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const cell = new Date(year, month - 1, day);
  return cell > today;
}

function _calDayKey(year, month, day) {
  const m = String(month).padStart(2, '0');
  const d = String(day).padStart(2, '0');
  return year + '-' + m + '-' + d;
}

function _calShowTooltip(ev, dayKey, dayData, label) {
  const tip = document.getElementById('cal-tooltip');
  if (!tip) return;
  const pnl = dayData ? Number(dayData.pnl) || 0 : 0;
  const trades = dayData ? Number(dayData.trades) || 0 : 0;
  const wins = dayData ? Number(dayData.wins) || 0 : 0;
  const losses = dayData ? Number(dayData.losses) || 0 : 0;
  const best = dayData && dayData.best_trade != null ? Number(dayData.best_trade) : null;
  const worst = dayData && dayData.worst_trade != null ? Number(dayData.worst_trade) : null;
  const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';

  let html = '<div class="cal-tooltip-title">' + label + '</div>';
  html += '<div class="cal-tooltip-row ' + pnlCls + '"><span>PnL</span><span>' + _calFmtSignedUsd(pnl) + '</span></div>';
  html += '<div class="cal-tooltip-row"><span>Trades</span><span>' + trades + '</span></div>';
  if (trades > 0) {
    html += '<div class="cal-tooltip-row"><span>W / L</span><span>' + wins + ' / ' + losses + '</span></div>';
    if (best != null) {
      html += '<div class="cal-tooltip-row pos"><span>Best</span><span>' + _calFmtSignedUsd(best) + '</span></div>';
    }
    if (worst != null) {
      html += '<div class="cal-tooltip-row neg"><span>Worst</span><span>' + _calFmtSignedUsd(worst) + '</span></div>';
    }
  }
  tip.innerHTML = html;
  tip.classList.remove('hidden');

  const pad = 12;
  const rect = tip.getBoundingClientRect();
  let x = ev.clientX + pad;
  let y = ev.clientY + pad;
  if (x + rect.width > window.innerWidth - pad) x = ev.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - pad) y = ev.clientY - rect.height - pad;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

function _calHideTooltip() {
  const tip = document.getElementById('cal-tooltip');
  if (tip) tip.classList.add('hidden');
}

function renderCalendar(data) {
  if (!data) return;
  window._calData = data;
  _calYear = data.year;
  _calMonth = data.month;

  const weekStart = data.week_start === 1 ? 1 : 0;
  const labels = weekStart === 1 ? _CAL_WEEKDAYS_MON : _CAL_WEEKDAYS_SUN;
  const wdaysEl = document.getElementById('cal-weekdays');
  if (wdaysEl) {
    wdaysEl.innerHTML = labels.map(function(l) {
      return '<div class="cal-wday">' + l + '</div>';
    }).join('');
  }

  const monthLabel = document.getElementById('cal-month-label');
  if (monthLabel) {
    monthLabel.textContent = _CAL_MONTHS[data.month - 1] + ' ' + data.year;
  }

  const summary = data.month_summary || {};
  const totalPnl = Number(summary.total_pnl) || 0;
  const pnlEl = document.getElementById('cal-month-pnl');
  if (pnlEl) {
    if (!_balanceVisible) {
      pnlEl.textContent = _maskUsd();
      pnlEl.className = 'cal-summary-pnl neu';
    } else {
      pnlEl.textContent = (totalPnl >= 0 ? '+' : '-') + '$' + _fmtMoney(Math.abs(totalPnl));
      pnlEl.className = 'cal-summary-pnl ' + (totalPnl > 0 ? 'pos' : totalPnl < 0 ? 'neg' : 'neu');
    }
  }

  const wrEl = document.getElementById('cal-month-wr');
  if (wrEl) {
    const wr = Number(summary.win_rate) || 0;
    wrEl.textContent = summary.trades ? ('Win rate ' + wr.toFixed(1) + '%') : 'Win rate —';
  }

  const trEl = document.getElementById('cal-month-trades');
  if (trEl) {
    const n = Number(summary.trades) || 0;
    trEl.textContent = n + ' trade' + (n === 1 ? '' : 's');
  }

  const scaleEl = document.getElementById('cal-scale-note');
  if (scaleEl) {
    const sm = Number(data.scale_max) || 100;
    scaleEl.textContent = 'Scale \u00b1$' + _fmtMoney(sm);
  }

  const gridEl = document.getElementById('cal-grid');
  if (!gridEl) return;

  const daysInMonth = _calDaysInMonth(data.year, data.month);
  const padStart = _calPadOffset(data.year, data.month, weekStart);
  const daysMap = data.days || {};
  const scaleMax = Number(data.scale_max) || 100;

  const cells = [];

  if (padStart > 0) {
    const prevMonth = data.month === 1 ? 12 : data.month - 1;
    const prevYear = data.month === 1 ? data.year - 1 : data.year;
    const prevDays = _calDaysInMonth(prevYear, prevMonth);
    for (let i = padStart - 1; i >= 0; i--) {
      const d = prevDays - i;
      cells.push({ pad: true, day: d });
    }
  }

  for (let d = 1; d <= daysInMonth; d++) {
    const key = _calDayKey(data.year, data.month, d);
    cells.push({ pad: false, day: d, key: key, data: daysMap[key] || null });
  }

  const remainder = cells.length % 7;
  if (remainder !== 0) {
    for (let d = 1; d <= 7 - remainder; d++) {
      cells.push({ pad: true, day: d });
    }
  }

  gridEl.innerHTML = cells.map(function(cell) {
    if (cell.pad) {
      return '<div class="cal-cell cal-cell--pad"><span class="cal-day-num">' + cell.day + '</span></div>';
    }

    const dayData = cell.data;
    const hasTrades = dayData && Number(dayData.trades) > 0;
    const pnl = hasTrades ? Number(dayData.pnl) || 0 : 0;
    const isFuture = _calIsFutureDay(data.year, data.month, cell.day);
    let cls = 'cal-cell';
    if (isFuture && !hasTrades) cls += ' cal-cell--future';
    else if (hasTrades) cls += ' cal-cell--active cal-cell--' + (pnl >= 0 ? 'pos' : 'neg');
    else cls += ' cal-cell--empty';

    const style = hasTrades ? (' style="' + _calCellStyle(pnl, scaleMax) + '"') : '';
    const pnlHtml = hasTrades
      ? '<span class="cal-day-pnl">' + _calFmtSignedUsd(pnl) + '</span>'
      : '';

    return '<div class="' + cls + '" data-day-key="' + cell.key + '"' + style
      + ' onmouseenter="window._calTip(event,\'' + cell.key + '\')"'
      + ' onmousemove="window._calTip(event,\'' + cell.key + '\')"'
      + ' onmouseleave="_calHideTooltip()">'
      + '<span class="cal-day-num">' + cell.day + '</span>'
      + pnlHtml
      + '</div>';
  }).join('');
}

window._calTip = function(ev, dayKey) {
  if (!window._calData) return;
  const dayData = (window._calData.days || {})[dayKey] || null;
  const parts = dayKey.split('-');
  const label = _CAL_MONTHS[parseInt(parts[1], 10) - 1] + ' ' + parseInt(parts[2], 10) + ', ' + parts[0];
  _calShowTooltip(ev, dayKey, dayData, label);
};

async function loadCalendar(year, month) {
  if (_calLoading) return;
  _calLoading = true;
  try {
    const params = new URLSearchParams();
    if (year != null) params.set('year', String(year));
    if (month != null) params.set('month', String(month));
    const res = await fetch('/api/pnl/calendar?' + params.toString());
    if (!res.ok) throw new Error('calendar fetch failed');
    const data = await res.json();
    renderCalendar(data);
  } catch (e) {
    console.warn('PnL calendar load failed', e);
    const gridEl = document.getElementById('cal-grid');
    if (gridEl) gridEl.innerHTML = '<div class="pm-empty" style="grid-column:1/-1;">Could not load calendar</div>';
  } finally {
    _calLoading = false;
  }
}

function _calNav(delta) {
  if (_calYear == null || _calMonth == null) return;
  let y = _calYear;
  let m = _calMonth + delta;
  if (m < 1) { m = 12; y -= 1; }
  else if (m > 12) { m = 1; y += 1; }
  loadCalendar(y, m);
}

function _initCalendar() {
  const prev = document.getElementById('cal-prev');
  const next = document.getElementById('cal-next');
  if (prev) prev.addEventListener('click', function() { _calNav(-1); });
  if (next) next.addEventListener('click', function() { _calNav(1); });
  setInterval(function() {
    if (_activePortfolioTab === 'calendar' && _calYear != null && _calMonth != null) {
      loadCalendar(_calYear, _calMonth);
    }
  }, 2 * 60 * 1000);
}

// Boot: init crosshair, then load history immediately from the backend.
// The backend will return backfilled historical data so the chart shows
// past performance right away — not an empty chart from today.
_loadBalancePref();
_loadTheme();
_updateBalanceToggleUI();
_initChartOverlay();
loadHistory(_pnlPeriod);
_initCalendar();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    content = HTML_CONTENT.replace("__BOT_NAME__", BOT_DISPLAY_NAME)
    subtitle = f"{TRADING_ASSETS_SUBTITLE} · {MARKET_INTERVAL_SLUG} markets"
    return content.replace("__TRADING_ASSETS__", subtitle)


@app.get("/api/config")
async def api_config():
    return JSONResponse(_app_config_payload())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        if websocket in active_connections:
            active_connections.remove(websocket)


@app.get("/health")
async def health():
    """Lightweight probe for Render / load balancers."""
    return {"ok": True, "bots": len(bots)}


@app.get("/api/status")
async def api_status():
    return {
        "bots":          [bot.get_dashboard_data() for bot in bots] if bots else [],
        "global_stats":  get_global_stats(),
        "positions":     collect_open_positions(bots) if bots else [],
        "trade_history": get_trade_history(10),
        "config":        _app_config_payload(),
    }


@app.get("/api/positions")
async def api_positions():
    """Live open positions — one row per FILLED worker position."""
    return JSONResponse({"positions": collect_open_positions(bots) if bots else []})


@app.get("/api/trades/history")
async def api_trades_history(limit: int = 10):
    """Most recent executed BUY/SELL records, newest first."""
    lim = max(1, min(limit, 50))
    return JSONResponse({"trades": get_trade_history(lim)})


@app.post("/api/positions/{asset}/{window}/cashout")
async def api_cashout(asset: str, window: str):
    raise HTTPException(
        status_code=410,
        detail="Manual cashout removed — inventory strategy holds matched pairs to settlement",
    )


@app.post("/api/positions/{asset}/cashout")
async def api_cashout_legacy(asset: str):
    raise HTTPException(
        status_code=410,
        detail="Manual cashout removed — inventory strategy holds matched pairs to settlement",
    )


if __name__ == "__main__":
    install_signal_handlers()
    _port = int(os.getenv("PORT", "5000"))
    try:
        uvicorn.run("main:app", host="0.0.0.0", port=_port, log_level="info")
    except KeyboardInterrupt:
        pass