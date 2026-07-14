# CHANGE_SUMMARY
# 2026-07-10  kilo
#   - Created daily_orb_v5 signal.
#   - Anchors opening range to 9:30 AM ET (US equity market open) and keeps it
#     fixed for the entire trading day, matching the YM ORB v5 backtest anchor.
#   - Supports per-timeframe OR windows (1m, 3m, 5m, 15m, 30m, 1h) plus an "any"
#     mode that trades the first breakout from any timeframe.
#   - Re-entry logic requires spot to pull back inside the daily OR range before
#     re-breaking in the same direction.
# 2026-07-10  kilo (audit fixes)
#   - Fixed OR window boundary: now 09:30:00 inclusive through 09:30+or_seconds
#     exclusive (was including one extra second).
#   - Fixed "any" mode re-entry pullback zone to use the timeframe that actually
#     triggered the last entry, instead of hard-coding the 5m range.
#   - Added _prune_state to drop daily state older than 2 days and prevent
#     unbounded memory growth.
# 2026-07-10  kilo (restart resilience)
#   - Persist per-(asset,date,tf_mode,max_reentries) state to disk so a restart
#     restores entry/re-entry counts, cooldown, last_trigger_tf and the finalized
#     opening range instead of starting fresh.
#   - Added warm_start(): on runner startup, reload persisted state; if the OR is
#     missing or not finalized and we are past 9:30 ET, backfill the range from
#     the Hyperliquid tick archive (live CSV + zstd-rotated files) over the exact
#     window [09:30:00, 09:30+or_sec). Because the OR is a pure function of that
#     price window, backfill reproduces the same range a continuous run would
#     have produced.
#   - Entries that fired while the process was down are NOT backfilled/fabricated
#     (a restart is a real operational event and the paper log must reflect it);
#     only the OR and the last-flushed re-entry state are recovered.
# WHY: User wants the ProjectX-style daily ORB ported to Polymarket UP/DOWN
#      markets for BTC, ETH, SOL, BNB, XRP, and HYPE, and a restart must leave
#      the strategy trading exactly as if it had been alive since the 9:30 open.

"""
Daily Opening Range Breakout v5 for Polymarket UP/DOWN markets.

This signal mirrors the YM ORB v5 logic from the ProjectX live bot:
  * The opening range is anchored to 9:30 AM ET and stays fixed all day.
  * Each timeframe has its own OR window length and breakout buffer.
  * Long breakout  -> buy YES on the active contract.
  * Short breakout -> buy NO  on the active contract.

State is keyed by (asset, date, tf_mode, max_reentries) so each asset and
variant has an independent daily state. State is persisted to disk and the
opening range is recoverable from the Hyperliquid tick archive so a process
restart does not reset the day.
"""

import csv
import glob
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("daily_orb_v5")

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# US equity market open in ET minute-of-day (9 * 60 + 30 = 570).
SESSION_OPEN_MIN = 570

# OR windows and buffers per timeframe.  Window length is the number of seconds
# after 9:30 ET used to establish the daily high/low.  Buffers widen with window
# length because a longer OR naturally captures more range and needs a slightly
# wider confirmation band.
TF_PARAMS = {
    "1m":  {"or_seconds": 60,   "buffer": 0.0005},
    "3m":  {"or_seconds": 180,  "buffer": 0.0006},
    "5m":  {"or_seconds": 300,  "buffer": 0.0007},
    "15m": {"or_seconds": 900,  "buffer": 0.0012},
    "30m": {"or_seconds": 1800, "buffer": 0.0018},
    "1h":  {"or_seconds": 3600, "buffer": 0.0025},
}

MIN_COOLDOWN_TICKS = 3
REENTRY_SIZE_SCALE = [1.0, 0.75, 0.50, 0.33]

# Global mutable state keyed by (asset, date, tf_mode, max_reentries).
_STATE: dict = {}

# Keep only the last N days of state to prevent unbounded memory growth.
_STATE_RETENTION_DAYS = 2

# On-disk persistence for restart resilience.  Lives next to the portfolio state
# files under the project root so all per-strategy state is in one place.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STATE_DIR = _PROJECT_ROOT / "state" / "daily_orb_v5"

# Hyperliquid tick archive used to reconstruct the opening range after a restart.
# Override with HL_TRADES_DIR if the collector lives elsewhere.
_HL_TRADES_DIR = Path(
    os.environ.get(
        "HL_TRADES_DIR",
        "/root/projects/trading/data/poly-data/collectors/hyperliquid",
    )
)
# Only scan rotated files modified within this window when backfilling, to bound
# startup IO.  Two days comfortably covers the UTC/ET date boundary.
_BACKFILL_LOOKBACK_SECONDS = 48 * 3600


def _make_state():
    return {
        "today": None,
        "or_high": {},
        "or_low": {},
        "or_closed": {},
        "entry_count": {"YES": 0, "NO": 0},
        "inside_after_break": {"YES": False, "NO": False},
        "cooldown": {"YES": 0, "NO": 0},
        "last_trigger_tf": None,
    }


def _no_signal(reason):
    return {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "entry_price": 0.0,
        "signal_price": 0.0,
        "source": "DAILY_ORB_V5",
        "reason": reason,
    }


def _now_et():
    return datetime.now(ET)


def _seconds_since_open(dt: datetime) -> int:
    """Whole seconds elapsed since 09:30:00 ET today."""
    et = dt.astimezone(ET)
    open_dt = et.replace(hour=9, minute=30, second=0, microsecond=0)
    delta = et - open_dt
    return int(delta.total_seconds())


def _active_tfs(tf_mode: str) -> list[str]:
    """Return the list of timeframes to monitor for this mode."""
    if tf_mode in ("any", "anyscale"):
        return list(TF_PARAMS.keys())
    if tf_mode in TF_PARAMS:
        return [tf_mode]
    # Legacy fallback: treat unknown tf_hint as 5m.
    return ["5m"]


def _prune_state(asset: str, today):
    """Drop state entries older than _STATE_RETENTION_DAYS for this asset."""
    global _STATE
    cutoff_date = today - timedelta(days=_STATE_RETENTION_DAYS)
    stale = [key for key in _STATE if key[0] == asset and key[1] < cutoff_date]
    for key in stale:
        del _STATE[key]


# ---------------------------------------------------------------------------
# Restart resilience: persist state + recover the opening range from archive.
# ---------------------------------------------------------------------------

def _state_path(key) -> Path:
    """Filesystem path for a (asset, date, tf_mode, max_reentries) state file."""
    asset, today, tf_mode, max_reentries = key
    return _STATE_DIR / f"{asset}_{today.isoformat()}_{tf_mode}_{max_reentries}.json"


def _serialize_state(state: dict) -> dict:
    """Convert in-memory state to a JSON-safe dict (inf -> None sentinels)."""
    or_high = {tf: (None if v == float("-inf") else v) for tf, v in state["or_high"].items()}
    or_low = {tf: (None if v == float("inf") else v) for tf, v in state["or_low"].items()}
    return {
        "or_high": or_high,
        "or_low": or_low,
        "or_closed": dict(state["or_closed"]),
        "entry_count": dict(state["entry_count"]),
        "inside_after_break": dict(state["inside_after_break"]),
        "cooldown": dict(state["cooldown"]),
        "last_trigger_tf": state["last_trigger_tf"],
    }


def _deserialize_state(payload: dict) -> dict:
    """Inverse of _serialize_state; restores inf sentinels."""
    state = _make_state()
    state["or_high"] = {
        tf: (float("-inf") if v is None else float(v))
        for tf, v in payload.get("or_high", {}).items()
    }
    state["or_low"] = {
        tf: (float("inf") if v is None else float(v))
        for tf, v in payload.get("or_low", {}).items()
    }
    state["or_closed"] = {tf: bool(v) for tf, v in payload.get("or_closed", {}).items()}
    ec = payload.get("entry_count", {})
    state["entry_count"] = {
        "YES": int(ec.get("YES", 0)),
        "NO": int(ec.get("NO", 0)),
    }
    iab = payload.get("inside_after_break", {})
    state["inside_after_break"] = {
        "YES": bool(iab.get("YES", False)),
        "NO": bool(iab.get("NO", False)),
    }
    cd = payload.get("cooldown", {})
    state["cooldown"] = {"YES": int(cd.get("YES", 0)), "NO": int(cd.get("NO", 0))}
    state["last_trigger_tf"] = payload.get("last_trigger_tf")
    return state


def _save_key(key) -> None:
    """Atomically persist a state key to disk. Never raises."""
    try:
        state = _STATE.get(key)
        if not state or state.get("today") is None:
            return
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _state_path(key)
        payload = {
            "asset": key[0],
            "date": key[1].isoformat(),
            "tf_mode": key[2],
            "max_reentries": key[3],
            "state": _serialize_state(state),
        }
        fd, tmp = tempfile.mkstemp(dir=str(_STATE_DIR), prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception as exc:  # persistence must never break trading
        log.warning("daily_orb_v5: failed to save state %s: %s", key, exc)


def _load_key(key):
    """Load a persisted state key into _STATE. Returns the state or None."""
    path = _state_path(key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        state = _deserialize_state(payload.get("state", {}))
        state["today"] = key[1]
        _STATE[key] = state
        return state
    except Exception as exc:
        log.warning("daily_orb_v5: failed to load state %s: %s", key, exc)
        return None


def _window_ms(date, or_seconds: int):
    """Return (open_ms, close_ms) UTC epoch millis for the OR window of `date`.

    Window is [09:30:00 ET, 09:30:00 + or_seconds) -- inclusive start, exclusive
    end, matching the live build loop.
    """
    open_et = datetime(
        date.year, date.month, date.day, 9, 30, 0, tzinfo=ET
    )
    close_et = open_et + timedelta(seconds=or_seconds)
    open_ms = int(open_et.astimezone(UTC).timestamp() * 1000)
    close_ms = int(close_et.astimezone(UTC).timestamp() * 1000)
    return open_ms, close_ms


def _candidate_archives(date):
    """Yield (path, is_zst) for Hyperliquid trade files that may hold `date`."""
    if not _HL_TRADES_DIR.exists():
        return
    now_ts = datetime.now(UTC).timestamp()
    cutoff = now_ts - _BACKFILL_LOOKBACK_SECONDS
    live = _HL_TRADES_DIR / "hyperliquid_trades.csv"
    if live.exists() and live.stat().st_mtime >= cutoff:
        yield live, False
    pattern = str(_HL_TRADES_DIR / "hyperliquid_trades_*.csv.zst")
    for p in glob.glob(pattern):
        try:
            if os.path.getmtime(p) >= cutoff:
                yield Path(p), True
        except OSError:
            continue


def _iter_trade_rows(path: Path, is_zst: bool):
    """Stream rows from a Hyperliquid trades CSV (plain or zstd-compressed)."""
    if is_zst:
        proc = subprocess.Popen(
            ["zstdcat", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        reader = csv.DictReader(proc.stdout)
        for row in reader:
            yield row
        proc.stdout.close()
        proc.wait()
    else:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                yield row


def _backfill_or(asset: str, date, or_seconds: int, now_ms: int | None = None):
    """Reconstruct one timeframe's opening range from the Hyperliquid archive.

    Returns (high, low, finalized, n_rows) or (None, None, False, 0) if no data.
    `finalized` is True only when `now_ms` is past the window close, i.e. the
    range is complete.  A partial range (still inside the window) is returned
    with finalized=False so live ticks keep building it.

    `now_ms` is the UTC epoch millis of "now" (passed by warm_start so the clock
    is consistent and testable); defaults to the wall clock when None.
    """
    open_ms, close_ms = _window_ms(date, or_seconds)
    if now_ms is None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
    # The upper bound of what we can read right now: the window close or now,
    # whichever is earlier (we cannot read ticks from the future).
    upper_ms = min(close_ms, now_ms)
    if upper_ms <= open_ms:
        return None, None, False, 0

    high = float("-inf")
    low = float("inf")
    n = 0
    for path, is_zst in _candidate_archives(date):
        try:
            for row in _iter_trade_rows(path, is_zst):
                if row.get("coin") != asset:
                    continue
                try:
                    tms = int(row.get("trade_time_ms") or 0)
                except (TypeError, ValueError):
                    continue
                if tms < open_ms:
                    continue
                if tms >= upper_ms:
                    # Files are time-ordered; once we pass the window we can stop
                    # reading this file (but other files may still hold rows).
                    break
                try:
                    price = float(row["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if price > high:
                    high = price
                if price < low:
                    low = price
                n += 1
        except Exception as exc:
            log.warning("daily_orb_v5: backfill read error %s: %s", path, exc)
            continue

    if n == 0 or high == float("-inf") or low == float("inf"):
        return None, None, False, 0
    finalized = now_ms >= close_ms
    return high, low, finalized, n


def warm_start(asset: str, tf_mode: str, max_reentries, now=None) -> dict:
    """Restore today's state for (asset, tf_mode, max_reentries) after a restart.

    Call once per asset at runner startup (before the tick loop).  It:
      1. Reloads any persisted state file for today.
      2. For each active timeframe, if the OR is not finalized and we are past
         9:30 ET, reconstructs it from the Hyperliquid archive so a restart mid-
         or post-window still trades the exact same range a continuous run would.
      3. Persists the reconstructed state.

    Re-entry counts / cooldown / last_trigger_tf are taken from the persisted
    file when present.  Entries that fired while the process was down are NOT
    recreated -- a restart is a real gap and the paper log reflects it.

    Returns a small dict describing what happened (for logging).
    """
    asset = asset.upper()
    tf_mode = str(tf_mode).lower() if tf_mode else "any"
    active_tfs = _active_tfs(tf_mode)
    now = now or _now_et()
    today = now.date()
    sec_since_open = _seconds_since_open(now)
    key = (asset, today, tf_mode, max_reentries)

    report = {"asset": asset, "tf_mode": tf_mode, "loaded": False, "backfilled": {}}

    # 1. Reload persisted state (carries entry_count / cooldown / last_trigger_tf
    #    and any already-finalized OR from an earlier run today).
    persisted = _load_key(key)
    if persisted is not None:
        report["loaded"] = True

    state = _STATE.setdefault(key, _make_state())
    state["today"] = today

    if sec_since_open < 0:
        # Pre-market: nothing to recover; the live loop will build the OR at 9:30.
        report["pre_market"] = True
        return report

    # 2. Reconstruct any non-finalized OR from the archive.
    changed = False
    now_ms = int(now.astimezone(UTC).timestamp() * 1000)
    for tf in active_tfs:
        if state["or_closed"].get(tf):
            # Already finalized (from persisted state); trust it.
            continue
        or_sec = TF_PARAMS[tf]["or_seconds"]
        high, low, finalized, n = _backfill_or(asset, today, or_sec, now_ms=now_ms)
        if high is None:
            report["backfilled"][tf] = {"rows": 0, "note": "no archive data"}
            continue
        # Merge with whatever live ticks have already been observed this process
        # (in case warm_start runs after a few live ticks arrived).
        state["or_high"][tf] = max(state["or_high"].get(tf, float("-inf")), high)
        state["or_low"][tf] = min(state["or_low"].get(tf, float("inf")), low)
        state["or_closed"][tf] = bool(finalized)
        report["backfilled"][tf] = {
            "rows": n,
            "high": high,
            "low": low,
            "finalized": finalized,
        }
        changed = True

    # 3. Persist the reconstructed state so a subsequent restart reloads it
    #    without re-scanning the archive.
    if changed or persisted is None:
        _save_key(key)
    return report


def daily_orb_v5_signal(
    spot_price=None,
    asset="BTC",
    rem_sec=0,
    yp=None,
    np_val=None,
    yes_ask=None,
    no_ask=None,
    tf_hint="any",
    market_id=None,
    max_reentries=3,
    max_entry_price=0.85,
    time_gate_seconds=30,
    **kwargs,
):
    """Generate a daily-ORB breakout signal for the given asset.

    Args:
        spot_price: Current Hyperliquid spot price for the asset.
        asset: Underlying asset (BTC, ETH, SOL, BNB, XRP, HYPE).
        rem_sec: Seconds remaining in the Polymarket contract.
        yp/no_val: Best bid for YES/NO contracts.
        yes_ask/no_ask: Best ask for YES/NO contracts.
        tf_hint: Timeframe mode ("any", "1m", "3m", "5m", "15m", "30m", "1h").
        market_id: Polymarket condition_id; used only for logging.
        max_reentries: Max re-entries per direction per day.
        max_entry_price: Do not enter if the ask is above this price.
        time_gate_seconds: Do not enter if the contract expires within this many
            seconds (theta-decay guard).
    """
    if spot_price is None or spot_price <= 0:
        return _no_signal("no spot")

    tf_mode = str(tf_hint).lower() if tf_hint else "any"
    active_tfs = _active_tfs(tf_mode)

    now = _now_et()
    today = now.date()
    sec_since_open = _seconds_since_open(now)

    # Before market open there is no daily range yet.
    if sec_since_open < 0:
        return _no_signal("pre-market")

    state_key = (asset.upper(), today, tf_mode, max_reentries)
    if state_key not in _STATE:
        # Defensive reload: warm_start should have populated this, but if the
        # module was reimported or warm_start was skipped, recover from disk.
        if _load_key(state_key) is None:
            _STATE[state_key] = _make_state()
    state = _STATE[state_key]
    state["today"] = today

    # Decrement cooldown counters each tick.
    for d in ("YES", "NO"):
        if state["cooldown"][d] > 0:
            state["cooldown"][d] -= 1

    # Prune stale daily state before creating today's state.
    _prune_state(asset.upper(), today)

    # Update opening ranges for every active timeframe.
    # The OR window is 09:30:00 inclusive through 09:30:00 + or_seconds exclusive,
    # matching the v5 YM convention (09:30 <= time_min < or_end_min).
    newly_finalized = False
    for tf in active_tfs:
        params = TF_PARAMS[tf]
        or_sec = params["or_seconds"]
        if sec_since_open < or_sec:
            state["or_high"][tf] = max(state["or_high"].get(tf, float("-inf")), spot_price)
            state["or_low"][tf] = min(state["or_low"].get(tf, float("inf")), spot_price)
            state["or_closed"][tf] = False
        else:
            if not state["or_closed"].get(tf):
                newly_finalized = True
            state["or_closed"][tf] = True

    # Persist the moment any timeframe's range finalizes so a restart reloads the
    # exact range instead of recomputing it from live ticks.
    if newly_finalized:
        _save_key(state_key)

    # If none of the active OR windows have closed yet, wait.
    closed_tfs = [tf for tf in active_tfs if state["or_closed"].get(tf)]
    if not closed_tfs:
        return _no_signal("OR window active")

    # Expiry / time-decay guard.
    if rem_sec < time_gate_seconds:
        return _no_signal("time_gate")

    # Build triggers for closed timeframes.
    triggers = {}
    for tf in closed_tfs:
        or_high = state["or_high"].get(tf)
        or_low = state["or_low"].get(tf)
        if or_high is None or or_low is None:
            continue
        if not (or_high > 0 and or_low < float("inf")):
            continue
        buf = TF_PARAMS[tf]["buffer"]
        triggers[tf] = {
            "buy": or_high * (1.0 + buf),
            "sell": or_low * (1.0 - buf),
            "or_high": or_high,
            "or_low": or_low,
        }

    if not triggers:
        return _no_signal("no triggers")

    # Detect pullback inside the OR range (enables re-entry).
    # For "any"/"anyscale" modes use the OR range of the timeframe that last
    # triggered; for a specific tf use that tf's own range. This ensures re-entry
    # requires a pullback into the same range that produced the original breakout.
    reentry_tf = state["last_trigger_tf"] if tf_mode in ("any", "anyscale") else tf_mode
    reentry_range = triggers.get(reentry_tf) if reentry_tf else None
    if reentry_range:
        if spot_price < reentry_range["or_high"] and state["entry_count"]["YES"] > 0:
            state["inside_after_break"]["YES"] = True
        if spot_price > reentry_range["or_low"] and state["entry_count"]["NO"] > 0:
            state["inside_after_break"]["NO"] = True

    # Find the first timeframe that is currently breaking out.
    # For specific-tf modes there is only one trigger; for "any" mode we check
    # in ascending timeframe order so shorter windows fire first.
    triggered_tf = None
    direction = None
    trigger_price = None

    for tf in active_tfs:
        if tf not in triggers:
            continue
        t = triggers[tf]
        if spot_price >= t["buy"]:
            triggered_tf = tf
            direction = "YES"
            trigger_price = t["buy"]
            break
        if spot_price <= t["sell"]:
            triggered_tf = tf
            direction = "NO"
            trigger_price = t["sell"]
            break

    if triggered_tf is None:
        return _no_signal("inside range")

    # Entry / re-entry validation.
    n = state["entry_count"][direction]
    is_first = n == 0
    is_reentry = n > 0 and n <= max_reentries and state["inside_after_break"][direction]

    if not (is_first or is_reentry):
        return _no_signal("max_reentries_or_no_pullback")
    if state["cooldown"][direction] > 0:
        return _no_signal("cooldown")

    # Price cap.
    entry_price = yes_ask if direction == "YES" else no_ask
    if entry_price is None:
        entry_price = yp if direction == "YES" else np_val
    if entry_price is None or entry_price <= 0:
        return _no_signal("no price")
    if entry_price > max_entry_price:
        return _no_signal("price_cap")

    # Commit the entry.
    entry_index = n
    state["entry_count"][direction] += 1
    state["inside_after_break"][direction] = False
    state["cooldown"][direction] = MIN_COOLDOWN_TICKS
    state["last_trigger_tf"] = triggered_tf

    # Persist re-entry state immediately so a restart resumes the exact count and
    # does not grant extra entries (or forget ones already taken).
    _save_key(state_key)

    size_scale = REENTRY_SIZE_SCALE[min(entry_index, len(REENTRY_SIZE_SCALE) - 1)]

    return {
        "triggered": True,
        "direction": direction,
        "confidence": size_scale,
        "entry_price": float(entry_price),
        "signal_price": float(entry_price),
        "source": "DAILY_ORB_V5",
        "entry_index": entry_index,
        "size_scale": size_scale,
        "is_reentry": entry_index > 0,
        "tf": triggered_tf,
        "tf_mode": tf_mode,
        "asset": asset.upper(),
        "or_high": triggers[triggered_tf]["or_high"],
        "or_low": triggers[triggered_tf]["or_low"],
        "trigger_price": trigger_price,
        "reason": (
            "%s #%s dir=%s tf=%s asset=%s spot=%.2f trigger=%s price=%.3f"
            % (
                "RE-ENTRY" if entry_index > 0 else "FIRST",
                entry_index,
                direction,
                triggered_tf,
                asset.upper(),
                spot_price,
                ">=%.2f" % trigger_price if direction == "YES" else "<=%.2f" % trigger_price,
                entry_price,
            )
        ),
    }
