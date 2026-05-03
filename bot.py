"""
KILLER — Multi-coin Kalshi 15M Trading Bot
Timing-aware conviction engine with price threshold gating.
Pass coin as first argument: python bot.py BTC  (default: BTC)
"""

import sys
import os

# Set coin BEFORE importing signals so the feed subscribes to the right pair
COIN = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("BOT_COIN", "BTC")).upper()
os.environ["BOT_COIN"] = COIN

COIN_SERIES = {
    "BTC":  "KXBTC15M",
    "ETH":  "KXETH15M",
    "SOL":  "KXSOL15M",
    "XRP":  "KXXRP15M",
    "DOGE": "KXDOGE15M",
    "BNB":  "KXBNB15M",
    "HYPE": "KXHYPE15M",
}
SERIES_TICKER = COIN_SERIES.get(COIN, f"KX{COIN}15M")

import time
import json
import base64
import requests
import csv
import threading

try:
    import trade_code as _tc
    _TC_AVAILABLE = True
except ImportError:
    _TC_AVAILABLE = False
import math
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
from signals import start_feed_thread, get_signal, coin_state as btc_state

load_dotenv()

API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
BASE_URL         = "https://api.elections.kalshi.com/trade-api/v2"
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", 50))
MAX_CONTRACTS    = int(os.getenv("MAX_CONTRACTS_PER_TRADE", 200))

with open(PRIVATE_KEY_PATH, "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)

session = {
    "trades_today":        0,
    "trades_this_market":  0,   # resets every new market ticker
    "pnl_today":           0.0,
    "consec_losses":       0,    # consecutive losing markets (resets on any win)
    "cooldown_active":     False,
    "cooldown_remaining":  0,
    "cooldown_last_ticker": None, # last ticker we counted against cooldown
    "market_wagered":      0.0,
    "killed":              False,
    "last_market_ticker":  None,
    "_last_eval_ticker":   None,
}

MODE_THRESHOLDS = {
    "selective":  0.65,
    "balanced":   0.50,
    "aggressive": 0.35,
    "always":     0.00,
}

# ── Time-based price thresholds ───────────────────────────────────────────
# Tighter thresholds early (don't buy expensive with lots of time left)
# Format: (min_minutes_remaining, max_price_cents/100)
PRICE_THRESHOLDS = [
    (10.0, 0.30),   # 10+ min left:  only buy if price < 30¢
    (7.0,  0.45),   # 7-10 min left: only buy if price < 45¢
    (5.0,  0.60),   # 5-7 min left:  only buy if price < 60¢
    (3.0,  0.75),   # 3-5 min left:  only buy if price < 75¢
    (1.0,  0.88),   # 1-3 min left:  only buy if price < 88¢
    (0.0,  0.95),   # <1 min left:   basically anything worth it
]

def get_price_threshold(mins_remaining):
    """Return max acceptable buy price given time remaining."""
    for min_mins, max_price in PRICE_THRESHOLDS:
        if mins_remaining >= min_mins:
            return max_price
    return 0.95


# ── Auth ──────────────────────────────────────────────────────────────────

def load_config():
    # Coin-specific config takes priority, fallback to shared
    p = Path(__file__).parent / f"bot_config_{COIN}.json"
    if not p.exists():
        p = Path(__file__).parent / "bot_config.json"
    return json.load(open(p)) if p.exists() else {}

def load_global_config():
    p = Path(__file__).parent / "global_config.json"
    return json.load(open(p)) if p.exists() else {}

def sign_request(method, path):
    ts  = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}{path}".encode()
    sig = PRIVATE_KEY.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }

# ── Kalshi API ────────────────────────────────────────────────────────────

def get_balance():
    """Fetch current Kalshi balance in dollars. Raises on failure so callers can skip safely."""
    path = "/trade-api/v2/portfolio/balance"
    r = requests.get(BASE_URL + "/portfolio/balance",
                     headers=sign_request("GET", path), timeout=5)
    data = r.json()
    bal = data.get("balance")
    if bal is None:
        raise ValueError(f"Unexpected balance response: {data}")
    return bal / 100

def find_current_market():
    path = "/trade-api/v2/markets"
    r = requests.get(
        BASE_URL + "/markets",
        headers=sign_request("GET", path),
        params={"series_ticker": SERIES_TICKER, "status": "open", "limit": 5},
    )
    markets  = r.json().get("markets", [])
    now      = datetime.now(timezone.utc)
    best, best_diff = None, float("inf")
    for m in markets:
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        diff  = (close - now).total_seconds()
        if 0 < diff < best_diff:
            best_diff, best = diff, m
    return best, best_diff

def get_position_for_ticker(ticker):
    """
    Fetch Kalshi portfolio position for a specific ticker.
    Returns the position dict or None.
    """
    try:
        path = "/trade-api/v2/portfolio/positions"
        r = requests.get(
            BASE_URL + "/portfolio/positions",
            headers=sign_request("GET", path),
            params={"ticker": ticker},
            timeout=5,
        )
        positions = r.json().get("market_positions", [])
        for pos in positions:
            if pos.get("ticker") == ticker:
                return pos
    except Exception as e:
        print(f"  Position lookup error: {e}")
    return None

def get_recent_fills(ticker):
    """Fetch recent fills for a ticker to verify order filled."""
    try:
        path = "/trade-api/v2/portfolio/fills"
        r = requests.get(
            BASE_URL + "/portfolio/fills",
            headers=sign_request("GET", path),
            params={"ticker": ticker, "limit": 10},
            timeout=5,
        )
        return r.json().get("fills", [])
    except Exception:
        return []

def sell_position(ticker, side, num_contracts, price_dollars):
    """Sell (close) an existing position."""
    path = "/trade-api/v2/portfolio/orders"
    price_str = f"{float(price_dollars):.4f}"
    key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    body = json.dumps({
        "ticker": ticker, "action": "sell", "side": side,
        "type": "limit", "count": num_contracts, key: price_str,
    })
    r = requests.post(BASE_URL + "/portfolio/orders",
                     headers=sign_request("POST", path), data=body)
    return r.json()

def place_order(ticker, side, price_dollars, num_contracts):
    """Place a limit order slightly above ask to guarantee fill."""
    path = "/trade-api/v2/portfolio/orders"
    price = float(price_dollars)
    price = min(price + 0.05, 0.99)  # 5-cent buffer to absorb price slippage
    price_str = f"{price:.4f}"
    key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    body = json.dumps({
        "ticker": ticker, "action": "buy", "side": side,
        "type": "limit", "count": num_contracts, key: price_str,
    })
    r = requests.post(
        BASE_URL + "/portfolio/orders",
        headers=sign_request("POST", path), data=body,
    )
    return r.json()

def get_order_status(order_id):
    """
    Poll a specific order by ID and return (filled, contracts, cost_dollars).
    Logs the raw response once so we can see actual Kalshi field names.
    """
    try:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        r = requests.get(
            BASE_URL + f"/portfolio/orders/{order_id}",
            headers=sign_request("GET", path),
            timeout=5,
        )
        raw = r.json()
        order = raw.get("order", raw)  # some endpoints return order at top level

        # Log raw order once for debugging field names
        print(f"  [order raw] {order}")

        status = order.get("status", "")

        # Kalshi uses different field names across API versions — check all of them
        filled_count = int(
            order.get("filled_count") or
            order.get("fill_count") or
            order.get("quantity_matched") or
            0
        )
        total_ordered = int(order.get("count") or order.get("quantity") or 0)
        remaining     = int(order.get("remaining_count") or order.get("quantity_remaining") or 0)

        # If remaining < total, some filled
        if remaining > 0 and total_ordered > remaining:
            filled_count = max(filled_count, total_ordered - remaining)

        # Cost — Kalshi returns cents
        total_cost_c = float(order.get("total_cost") or order.get("cost") or 0)
        cost_dollars = total_cost_c / 100 if total_cost_c > 1 else total_cost_c

        is_filled = (
            filled_count > 0 or
            status in ("filled", "executed", "resting_cancelled") or
            (remaining == 0 and total_ordered > 0)
        )
        return is_filled, filled_count or total_ordered, cost_dollars
    except Exception as e:
        print(f"  get_order_status error: {e}")
        return False, 0, 0.0

def get_market_prices(ticker):
    """Fetch current YES/NO ask prices for a live market."""
    path = "/trade-api/v2/markets/" + ticker
    r = requests.get(BASE_URL + "/markets/" + ticker,
                     headers=sign_request("GET", path), timeout=5)
    m = r.json().get("market", r.json())
    return float(m.get("yes_ask_dollars", 0.5)), float(m.get("no_ask_dollars", 0.5))

def get_market_full(ticker):
    """Fetch ask + bid prices + volume for a live market."""
    path = "/trade-api/v2/markets/" + ticker
    r = requests.get(BASE_URL + "/markets/" + ticker,
                     headers=sign_request("GET", path), timeout=5)
    m = r.json().get("market", r.json())
    return {
        "yes_ask":    float(m.get("yes_ask_dollars", 0.5)),
        "no_ask":     float(m.get("no_ask_dollars",  0.5)),
        "yes_bid":    float(m.get("yes_bid_dollars",  0.0)),
        "no_bid":     float(m.get("no_bid_dollars",   0.0)),
        "volume":     float(m.get("volume",            0.0)),   # total contracts traded
        "open_interest": float(m.get("open_interest", 0.0)),
    }

def check_liquidity(ticker, direction, ask, min_volume=500, max_spread_cents=5):
    """
    Returns (passes, reason, haircut_factor).
    Checks spread and volume before committing capital.
    haircut_factor: 1.0 = full size, 0.5 = half size, 0.0 = skip
    """
    try:
        mkt = get_market_full(ticker)
        if direction == "yes":
            bid = mkt["yes_bid"]
        else:
            bid = mkt["no_bid"]

        spread_cents = round((ask - bid) * 100, 1)
        volume       = mkt["volume"]

        if spread_cents > max_spread_cents and volume < min_volume:
            return False, f"Spread {spread_cents}¢ > {max_spread_cents}¢ AND vol {volume:.0f} < {min_volume} — skip", 0.0
        if spread_cents > max_spread_cents:
            return True,  f"Spread {spread_cents}¢ > {max_spread_cents}¢ — half size", 0.5
        if volume < min_volume:
            return True,  f"Volume {volume:.0f} < {min_volume} — half size", 0.5
        return True, f"Liquidity OK (spread={spread_cents}¢ vol={volume:.0f})", 1.0
    except Exception as e:
        return True, f"Liquidity check failed ({e}) — allowing", 1.0

def check_momentum(direction, window_secs=30):
    """
    Returns (passes, rate_pct_per_sec).
    Passes = True if BTC momentum does NOT strongly contradict the bet direction.
    direction: "yes" (want BTC up) or "no" (want BTC down)
    """
    history = btc_state.get("price_history", [])
    if not history or len(history) < 2:
        return True, 0.0  # no data → allow entry
    now = time.time()
    recent = [(t, p) for t, p in history if t >= now - window_secs]
    if len(recent) < 2:
        return True, 0.0  # not enough points in window → allow
    oldest_t, oldest_p = recent[0]
    newest_t, newest_p = recent[-1]
    elapsed = newest_t - oldest_t
    if elapsed < 1 or oldest_p == 0:
        return True, 0.0
    # Fractional rate of change per second
    rate = (newest_p - oldest_p) / (oldest_p * elapsed)
    # Block threshold: 0.01%/sec (~0.6%/min) moving against the bet
    THRESHOLD = 0.0001
    if direction == "yes" and rate < -THRESHOLD:
        return False, rate   # BTC falling → bad for YES
    if direction == "no"  and rate >  THRESHOLD:
        return False, rate   # BTC rising  → bad for NO
    return True, rate

def get_settled_pnl(ticker, side, contracts, entry_price):
    try:
        path = "/trade-api/v2/markets/" + ticker
        r    = requests.get(BASE_URL + "/markets/" + ticker,
                            headers=sign_request("GET", path), timeout=5)
        market = r.json().get("market", r.json())
        result = market.get("result", "")
        if not result:
            return None
        won = (result == side)
        return contracts * (1.0 - entry_price) if won else -(contracts * entry_price)
    except Exception as e:
        print(f"  Settlement error: {e}")
        return None

# ── Trade logging ─────────────────────────────────────────────────────────

def get_trades_file():
    folder = Path(__file__).parent / "trades" / COIN
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"trades_{datetime.now().strftime('%Y-%m-%d')}.csv"

def log_trade(ticker, side, price, contracts, note="", trade_code=""):
    f   = get_trades_file()
    new = not f.exists()
    with open(f, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp", "ticker", "side", "price",
                        "contracts", "pnl", "note", "trade_code"])
        w.writerow([datetime.now().isoformat(), ticker, side,
                    price, contracts, "pending", note, trade_code])

def update_trade_pnl(ticker, pnl):
    """Update the pnl column for the most recent pending trade for this ticker."""
    folder = Path(__file__).parent / "trades" / COIN
    if not folder.exists():
        return
    for f in sorted(folder.glob("trades_*.csv"), reverse=True):
        rows, updated = [], False
        with open(f, newline="") as fh:
            reader = csv.DictReader(fh)
            fields = list(reader.fieldnames or [])
            # Ensure new columns exist in old files
            for col in ("note", "trade_code"):
                if col not in fields:
                    fields.append(col)
            for row in reader:
                if row.get("ticker") == ticker and row.get("pnl") == "pending":
                    row["pnl"] = f"{pnl:.4f}" if pnl is not None else "expired"
                    updated = True
                rows.append(row)
        if updated:
            with open(f, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            return

# ── Bot status file (read by dashboard) ──────────────────────────────────

_last_signals    = None   # cache last computed signals so idle/watched states keep showing them
_last_conviction = None   # cache last computed conviction so traded/idle states show it too

def write_bot_status(status, direction=None, max_price=None, conviction=None,
                     mins_remaining=None, signals=None):
    """Write bot's current internal status so dashboard can display it."""
    global _last_signals, _last_conviction
    status_file = Path(__file__).parent / f"bot_status_{COIN}.json"
    try:
        if signals is not None:
            _last_signals = signals          # update cache whenever we have fresh signals
        if conviction is not None:
            _last_conviction = conviction    # update cache whenever we have a fresh conviction
        payload = {
            "status":         status,
            "direction":      direction,
            "max_price":      max_price,
            "conviction":     _last_conviction,  # always write last known conviction
            "mins_remaining": mins_remaining,
            "updated_at":     time.time(),
            "signals":        _last_signals,     # always include last known signals
        }
        with open(status_file, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass

# ── Conviction scoring ────────────────────────────────────────────────────

def calc_conviction(sig, yes_ask, no_ask):
    """
    Returns (conviction 0-1, direction, components dict)
    """
    pos_safety       = sig["pos_safety"]
    safe_side        = sig["safe_side"]
    signal_agreement = sig["signal_agreement"]
    signal_direction = sig["signal_direction"]
    time_factor      = sig["time_factor"]
    our_yes          = sig["our_yes_prob"]
    our_no           = sig["our_no_prob"]

    # Direction priority:
    # 1. Signals — when they clearly agree on a direction
    # 2. Safe side — where BTC currently sits relative to strike (always reliable)
    # 3. Nothing else — hold, don't guess
    if signal_direction and signal_agreement >= 0.25:
        direction  = signal_direction
        contrarian = False
    elif safe_side:
        direction  = safe_side
        contrarian = False
    else:
        direction  = "yes"   # absolute fallback, should rarely hit
        contrarian = False

    # Scoring
    safety_score = min(pos_safety / 2.0, 1.0)
    sig_score    = signal_agreement if direction == signal_direction \
                   else signal_agreement * 0.3
    t_score      = time_factor
    crowd_lean   = yes_ask if direction == "yes" else no_ask

    if crowd_lean > 0.6:
        price_conf = 0.5 + (crowd_lean - 0.5) * 0.5
    elif crowd_lean < 0.3:
        price_conf = safety_score * sig_score
    else:
        price_conf = 0.5

    conviction = (
        safety_score * 0.20 +   # was 0.40 — less dominant so near-strike ≠ no-trade
        sig_score    * 0.45 +   # was 0.30 — signals are the primary evidence
        t_score      * 0.25 +   # was 0.20 — time urgency matters more
        price_conf   * 0.10
    )

    if contrarian and pos_safety > 1.0:
        conviction *= 0.5

    conviction = min(conviction, 1.0)

    return conviction, direction, {
        "safety_score": safety_score,
        "sig_score":    sig_score,
        "t_score":      t_score,
        "price_conf":   price_conf,
        "contrarian":   contrarian,
        "pos_safety":   pos_safety,
        "safe_side":    safe_side,
    }

def required_readings(conviction):
    if conviction >= 0.85:   return 5
    elif conviction >= 0.70: return 10
    elif conviction >= 0.55: return 15
    elif conviction >= 0.40: return 25
    else:                    return 999

def calc_bet_size(conviction, risk_per_trade, min_bet):
    if conviction >= 0.75:   factor = 1.0
    elif conviction >= 0.55: factor = 0.5
    elif conviction >= 0.40: factor = 0.25
    else:                    factor = 0.1
    return max(risk_per_trade * factor, min_bet)

# ── Startup position recovery ──────────────────────────────────────────────

def recover_open_positions():
    """
    On startup, query Kalshi for any existing open positions and return them
    as open_trades entries so the bot can monitor and settle them properly.
    position_fp is negative for NO contracts, positive for YES contracts.
    """
    try:
        path = "/trade-api/v2/portfolio/positions"
        r = requests.get(
            BASE_URL + "/portfolio/positions",
            headers=sign_request("GET", path),
            timeout=5,
        )
        positions = r.json().get("market_positions", [])
        recovered = []
        for pos in positions:
            pos_fp = float(pos.get("position_fp", 0))
            if abs(pos_fp) < 1:
                continue
            ticker = pos.get("ticker", "")
            if not ticker.startswith(f"KX{COIN}"):
                continue
            side      = "no" if pos_fp < 0 else "yes"
            contracts = int(abs(pos_fp))
            exposure  = float(pos.get("market_exposure_dollars", 0))
            entry_px  = round(exposure / contracts, 4) if contracts > 0 else 0
            recovered.append({
                "ticker":      ticker,
                "side":        side,
                "contracts":   contracts,
                "entry_price": entry_px,
            })
            print(f"  🔄 RECOVERED position: {ticker} | {contracts} {side.upper()} @ ${entry_px:.3f}")
        return recovered
    except Exception as e:
        print(f"  Position recovery error: {e}")
        return []


# ── Background signal loop (runs always, independent of market state) ─────

def _signal_background_loop():
    """Compute and cache signals every 15s regardless of whether a market is active.
    Momentum, Liquidations, Multi-TF are macro signals that don't need a live market."""
    global _last_signals
    time.sleep(42)  # let WebSocket price data and candles load first
    while True:
        try:
            sig = get_signal()
            _payload = {
                "momentum": None, "liq": None, "kalshi": None,
                "crowd": None, "multi_tf": None,
                "orderbook": None, "orderflow": None,
                "yes_votes":  sig.get("yes_votes", 0),
                "no_votes":   sig.get("no_votes",  0),
                "conviction_streak": 0,
                "conviction_dir":    sig.get("signal_direction"),
            }
            for _s in sig.get("signals", []):
                _n = _s.get("name", "").lower()
                _v = ("bull" if _s.get("yes_prob", 0.5) > 0.55
                      else "bear" if _s.get("yes_prob", 0.5) < 0.45
                      else "neutral")
                if "moment" in _n:                     _payload["momentum"]  = _v
                elif "liq"    in _n:                   _payload["liq"]       = _v
                elif "kalshi" in _n or "dist" in _n:   _payload["kalshi"]    = _v
                elif "crowd"  in _n:                   _payload["crowd"]     = _v
                elif "multi"  in _n:                   _payload["multi_tf"]  = _v
                elif "book"   in _n:                   _payload["orderbook"] = _v
                elif "flow"   in _n:                   _payload["orderflow"] = _v
            _last_signals = _payload
            sigs_display = {k: v for k, v in _payload.items()
                            if k in ("momentum","liq","kalshi","crowd","multi_tf")}
            print(f"[{COIN}] SIGNALS: {sigs_display}")
        except Exception as e:
            print(f"[{COIN}] Signal loop error: {e}")
        time.sleep(15)


# ── Main loop ─────────────────────────────────────────────────────────────

def run_bot():
    print("=" * 60)
    print(f"{COIN}.KILLER — Conviction engine v3  [{SERIES_TICKER}]")
    print("Price thresholds: 30/45/60/75/88/95¢ by time window")
    print("=" * 60)

    print("\nStarting feeds...")
    start_feed_thread()
    threading.Thread(target=_signal_background_loop, daemon=True).start()
    print("Waiting 40 seconds for data to load...")
    time.sleep(40)

    last_market    = None
    last_mkt_time  = 0
    secs_remaining = 0
    streak = {"direction": None, "count": 0, "conviction": 0.0}

    market_state = {
        "ticker":         None,
        "traded":         False,
        "our_side":       None,
        "our_contracts":  0,
        "our_entry":      0.0,
        "topup_done":     False,
        "flip_done":      False,
    }

    print("Checking Kalshi for existing open positions...")
    open_trades = recover_open_positions()
    if open_trades:
        print(f"  Loaded {len(open_trades)} open position(s) from Kalshi — will monitor for settlement.")
        t = open_trades[0]
        # Lock the market so the bot doesn't try to trade again on the same ticker
        session["last_market_ticker"] = t["ticker"]
        market_state["traded"]        = True
        market_state["our_side"]      = t["side"]
        market_state["our_contracts"] = t["contracts"]
        market_state["our_entry"]     = t["entry_price"]
        # Write current_position_{COIN}.json so dashboard shows it immediately
        pos_file = Path(__file__).parent / f"current_position_{COIN}.json"
        with open(pos_file, "w") as pf:
            json.dump({
                "ticker":    t["ticker"],
                "side":      t["side"],
                "contracts": t["contracts"],
                "entry":     t["entry_price"],
                "cost":      round(t["entry_price"] * t["contracts"], 2),
            }, pf)
    else:
        print("  No open positions found — starting fresh.")
        # Clear any stale position file from a previous run
        stale_pos = Path(__file__).parent / f"current_position_{COIN}.json"
        if stale_pos.exists():
            stale_pos.unlink()
            print(f"  Cleared stale position file for {COIN}.")

    # Watching state — tracks when we have conviction but price is too high
    # Bot watches every 2s for price to drop into acceptable range before firing
    market_watching = {
        "active":       False,
        "direction":    None,
        "max_price":    0.0,
        "since":        0,
        "ticker":       None,
        "price_history": [],   # [(timestamp, ask_price), ...] for momentum detection
    }

    while True:
        if session["killed"]:
            print("KILL SWITCH. Stopping.")
            write_bot_status("killed")
            break

        try:
            cfg = load_config()
            mode             = cfg.get("mode", "balanced")
            gcfg             = load_global_config()
            daily_loss_lim   = float(gcfg.get("daily_loss_limit", DAILY_LOSS_LIMIT))
            wager_mode        = cfg.get("wager_mode", "dollar")
            # max_wager is the dashboard key; max_session_wager is legacy fallback
            _max_cfg = cfg.get("max_wager") or cfg.get("max_session_wager", 5.0)
            if wager_mode in ("percent", "pct"):
                wager_pct_val     = float(cfg.get("wager_pct",     10.0))  # max % (from slider max)
                wager_pct_min_val = float(cfg.get("wager_pct_min",  1.0))  # min % (from slider min)
                try:
                    current_bal      = get_balance()
                    pct_wager        = current_bal * wager_pct_val / 100
                    max_market_wager = pct_wager  # pure % sizing — no dollar cap
                    min_bet          = max(current_bal * wager_pct_min_val / 100, 0.01)
                except Exception:
                    max_market_wager = float(_max_cfg)
                    min_bet          = 0.25
            else:
                max_market_wager  = float(_max_cfg)
                # wager is the dashboard min/base key; min_bet is legacy fallback
                min_bet           = float(cfg.get("wager") or cfg.get("min_bet", 0.25))
                min_bet           = max(min_bet, 0.01)
            min_threshold     = MODE_THRESHOLDS.get(mode, 0.50)
            trigger_method    = cfg.get("trigger_method", "ev")
            # Always-buy time window: fire between always_open and always_close minutes left
            always_open       = float(cfg.get("always_open",  6.0))
            always_close      = float(cfg.get("always_close", 3.0))
            always_max_price  = float(cfg.get("always_max_price", 0.75))
            # ── Advanced filters ─────────────────────────────────────────
            min_ev_edge        = float(cfg.get("min_ev_edge",        0.05))
            momentum_gate_on   = bool( cfg.get("momentum_gate",      False))
            momentum_window_s  = int(  cfg.get("momentum_window",    30))
            safety_margin_usd  = float(cfg.get("safety_margin",      0.0))
            max_trades_session = int(  cfg.get("max_trades_session",  0))
            take_profit_cents  = float(cfg.get("take_profit_cents",   0.0))
            take_profit_mins   = float(cfg.get("take_profit_mins",    0.0))   # sell if ahead & X mins remain
            sell_at_price      = float(cfg.get("sell_at_price",       0.0))   # sell when bid hits this price
            sell_min_mins      = float(cfg.get("sell_min_mins",       2.0))   # only auto-sell if ≥ this many mins left
            kelly_enabled      = bool( cfg.get("kelly_enabled",       False))
            kelly_fraction     = float(cfg.get("kelly_fraction",      0.25))
            min_signals        = int(  cfg.get("signal_agreement", cfg.get("min_signals", 2)))
            wallet_floor       = float(cfg.get("wallet_floor",        0.0))
            inverse_bet_enabled= bool( cfg.get("inverse_bet_enabled", False))
            # ── 1¢ Longshot ─────────────────────────────────────────────
            penny_enabled      = bool( cfg.get("penny_enabled",       False))
            penny_stop_mins    = float(cfg.get("penny_stop_mins",     3.0))   # stop buying when < X mins left
            penny_wager        = float(cfg.get("penny_wager",         1.0))
            penny_wager_mode   = cfg.get("penny_wager_mode",          "dollar")
            penny_flip         = bool( cfg.get("penny_flip",          True))  # sell winning side to take penny

            # ── Settle open trades ───────────────────────────────────────
            still_open = []
            for t in open_trades:
                pnl = get_settled_pnl(t["ticker"], t["side"],
                                      t["contracts"], t["entry_price"])
                if pnl is not None:
                    session["pnl_today"] += pnl
                    if pnl > 0:
                        session["consec_losses"] = 0  # win resets streak
                    else:
                        session["consec_losses"] += 1
                    update_trade_pnl(t["ticker"], pnl)
                    print(f"  SETTLED: {t['ticker']} → "
                          f"{'WIN' if pnl>0 else 'LOSS'} ${pnl:+.2f} | "
                          f"Day P&L: ${session['pnl_today']:+.2f} | "
                          f"Consec losses: {session['consec_losses']}")
                    pos_file = Path(__file__).parent / f"current_position_{COIN}.json"
                    if pos_file.exists():
                        try:
                            pos = json.load(open(pos_file))
                            if pos.get("ticker") == t["ticker"]:
                                pos_file.unlink()
                        except Exception:
                            pass
                else:
                    still_open.append(t)
            open_trades = still_open

            if session["pnl_today"] <= -daily_loss_lim:
                session["killed"] = True
                print(f"DAILY LOSS LIMIT hit ${abs(session['pnl_today']):.2f} — stopping")
                write_bot_status("killed")
                break

            # ── Hard wallet floor ────────────────────────────────────────
            # Only check when no open positions — Kalshi deducts position cost
            # from available balance, so checking mid-trade gives false positives.
            if wallet_floor > 0 and not open_trades:
                try:
                    live_bal = get_balance()
                    if live_bal < wallet_floor:
                        session["killed"] = True
                        print(f"WALLET FLOOR HIT — balance ${live_bal:.2f} < floor ${wallet_floor:.2f} — stopping")
                        write_bot_status("killed")
                        break
                except Exception as e:
                    print(f"  wallet floor check failed: {e}")

            # ── Refresh market every 1s for fresh prices ─────────────────
            now = time.time()
            if not last_market or (now - last_mkt_time) > 1:
                last_market, secs_remaining = find_current_market()
                last_mkt_time = now
            elif last_market:
                close = datetime.fromisoformat(
                    last_market["close_time"].replace("Z", "+00:00"))
                secs_remaining = (close - datetime.now(timezone.utc)).total_seconds()

            if not last_market or secs_remaining <= 0:
                print("No active market — waiting 5s")
                streak = {"direction": None, "count": 0, "conviction": 0.0}
                write_bot_status("idle")
                time.sleep(5)
                continue

            ticker   = last_market["ticker"]
            mins_rem = secs_remaining / 60
            yes_ask  = float(last_market.get("yes_ask_dollars", 0.5))
            no_ask   = float(last_market.get("no_ask_dollars", 0.5))
            strike   = last_market.get("floor_strike") or last_market.get("cap_strike")
            s_type   = last_market.get("strike_type", "")

            # ── One trade per market ─────────────────────────────────────
            if ticker == session["last_market_ticker"]:
                # ── Exit monitors (take-profit, time-based, price-target) ─
                has_position = (
                    market_state.get("our_side")
                    and market_state.get("our_contracts", 0) > 0
                    and market_state.get("our_entry", 0) > 0
                    and mins_rem > 0.1
                )
                if has_position:
                    try:
                        mkt       = get_market_full(ticker)
                        our_side  = market_state["our_side"]
                        bid_price = mkt[f"{our_side}_bid"]
                        entry     = market_state["our_entry"]
                        gain_c    = (bid_price - entry) * 100   # cents gained

                        exit_reason = None

                        # 1. Gain-in-cents take-profit
                        if take_profit_cents > 0 and bid_price > 0.01 and gain_c >= take_profit_cents:
                            exit_reason = (f"TAKE PROFIT (+{gain_c:.1f}¢ ≥ {take_profit_cents:.0f}¢ target)")

                        # 2. Time-based take-profit: sell if ahead & enough time to re-enter
                        if not exit_reason and take_profit_mins > 0 and mins_rem >= take_profit_mins and gain_c > 0:
                            exit_reason = (f"TIME PROFIT ({mins_rem:.1f}min left, ahead +{gain_c:.1f}¢ — freeing capital)")

                        # 3. Price-target auto-sell: bid hit target with enough time to re-enter
                        if not exit_reason and sell_at_price > 0 and bid_price >= sell_at_price and mins_rem >= sell_min_mins:
                            exit_reason = (f"PRICE TARGET hit {bid_price:.2f} ≥ {sell_at_price:.2f} "
                                           f"({mins_rem:.1f}min left — re-entering)")

                        if exit_reason:
                            print(f"\n  💰 {exit_reason} — selling {market_state['our_contracts']} {our_side.upper()}")
                            sell_result = sell_position(
                                ticker, our_side, market_state["our_contracts"], bid_price)
                            if "error" not in sell_result:
                                actual_pnl = market_state["our_contracts"] * (bid_price - entry)
                                update_trade_pnl(ticker, actual_pnl)
                                session["pnl_today"] += actual_pnl
                                if actual_pnl > 0:
                                    session["consec_losses"] = 0
                                else:
                                    session["consec_losses"] += 1
                                open_trades[:] = [t for t in open_trades if t["ticker"] != ticker]
                                pos_file = Path(__file__).parent / f"current_position_{COIN}.json"
                                if pos_file.exists():
                                    pos_file.unlink()
                                # Reset market lock so bot can re-enter same market
                                market_state["our_contracts"] = 0
                                market_state["our_side"]      = None
                                market_state["our_entry"]     = 0
                                market_state["traded"]        = False
                                session["last_market_ticker"] = None
                                print(f"  ✅ SOLD for ${actual_pnl:+.2f} | Day P&L: ${session['pnl_today']:+.2f}")
                                print(f"  🔄 Market lock cleared — bot may re-enter {ticker}")
                                time.sleep(1)
                                continue   # immediately loop back to look for re-entry
                            else:
                                print(f"  ❌ Exit sell error: {sell_result.get('error','?')}")
                    except Exception as tp_err:
                        print(f"  Exit monitor error: {tp_err}")
                # ── 1¢ Longshot flip (while holding winning side) ────────
                if (penny_enabled and penny_flip
                        and market_state.get("our_side")
                        and market_state.get("our_contracts", 0) > 0
                        and mins_rem > penny_stop_mins):
                    try:
                        our_side = market_state["our_side"]
                        opp_side = "no" if our_side == "yes" else "yes"
                        opp_ask  = yes_ask if opp_side == "yes" else no_ask
                        if opp_ask <= 0.02:
                            mkt2     = get_market_full(ticker)
                            our_bid2 = mkt2.get(f"{our_side}_bid", 0)
                            if our_bid2 >= 0.97:
                                print(f"\n  🎰 PENNY FLIP! Selling {our_side.upper()} at "
                                      f"{our_bid2:.2f}, buying {opp_side.upper()} at {opp_ask:.2f} "
                                      f"({mins_rem:.1f}min left — lottery ticket!)")
                                sell_r = sell_position(ticker, our_side,
                                                       market_state["our_contracts"], our_bid2)
                                if "error" not in sell_r:
                                    entry2   = market_state["our_entry"]
                                    sell_pnl = market_state["our_contracts"] * (our_bid2 - entry2)
                                    update_trade_pnl(ticker, sell_pnl)
                                    session["pnl_today"] += sell_pnl
                                    if sell_pnl > 0:
                                        session["consec_losses"] = 0
                                    else:
                                        session["consec_losses"] += 1
                                    open_trades[:] = [t for t in open_trades if t["ticker"] != ticker]
                                    market_state["our_contracts"] = 0
                                    market_state["our_side"]      = None
                                    market_state["our_entry"]     = 0
                                    market_state["traded"]        = False
                                    session["last_market_ticker"] = None
                                    # Now buy the penny side
                                    if penny_wager_mode == "percent":
                                        try:
                                            pb = get_balance()
                                        except Exception:
                                            pb = penny_wager * 10
                                        p_wager = pb * penny_wager / 100
                                    else:
                                        p_wager = penny_wager
                                    p_contracts = max(1, int(p_wager / max(opp_ask, 0.01)))
                                    p_contracts = min(p_contracts, MAX_CONTRACTS)
                                    p_result = place_order(ticker, opp_side, opp_ask, p_contracts)
                                    if "error" not in p_result:
                                        log_trade(ticker, opp_side, opp_ask, p_contracts, "penny_flip")
                                        market_state["our_side"]      = opp_side
                                        market_state["our_contracts"] = p_contracts
                                        market_state["our_entry"]     = opp_ask
                                        market_state["traded"]        = True
                                        session["last_market_ticker"] = ticker
                                        print(f"  🎰 FLIP BOUGHT {p_contracts} {opp_side.upper()} @ {opp_ask:.2f}")
                                    else:
                                        print(f"  ❌ Penny flip buy error: {p_result.get('error','?')}")
                    except Exception as pf_err:
                        print(f"  Penny flip error: {pf_err}")

                write_bot_status("traded", mins_remaining=mins_rem)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] {ticker} | "
                      f"{mins_rem:.1f}min left | position held — waiting for market to close")
                time.sleep(0.5)
                continue

            # ── New market reset ─────────────────────────────────────────
            if ticker != session["_last_eval_ticker"]:
                streak = {"direction": None, "count": 0, "conviction": 0.0}
                session["market_wagered"]    = 0.0
                session["trades_this_market"] = 0
                session["_last_eval_ticker"] = ticker
                market_state = {
                    "ticker":        ticker,
                    "traded":        False,
                    "our_side":      None,
                    "our_contracts": 0,
                    "our_entry":     0.0,
                    "topup_done":    False,
                    "flip_done":     False,
                }
                market_watching = {
                    "active":    False,
                    "direction": None,
                    "max_price": 0.0,
                    "since":     0,
                    "ticker":    ticker,
                }
                print(f"\n  ── New market: {ticker} | budget + watching state reset ──")

            # ── 1¢ LONGSHOT — fires immediately, bypasses all mode gates ───
            # Checks as soon as any side hits 1¢ regardless of time or mode.
            if penny_enabled and not market_state.get("traded", False) and mins_rem > penny_stop_mins:
                for p_side, p_ask in [("yes", yes_ask), ("no", no_ask)]:
                    if p_ask <= 0.02:
                        if penny_wager_mode == "percent":
                            try:
                                pb = get_balance()
                            except Exception:
                                pb = penny_wager * 10
                            p_wager = pb * penny_wager / 100
                        else:
                            p_wager = penny_wager
                        p_contracts = max(1, int(p_wager / max(p_ask, 0.01)))
                        p_contracts = min(p_contracts, MAX_CONTRACTS)
                        print(f"\n  🎰 PENNY LONGSHOT: {p_side.upper()} @ {p_ask:.2f} "
                              f"| {p_contracts} contracts | {mins_rem:.1f}min left "
                              f"| potential payout ${p_contracts*(1-p_ask):.2f}")
                        p_result = place_order(ticker, p_side, p_ask, p_contracts)
                        if "error" not in p_result:
                            log_trade(ticker, p_side, p_ask, p_contracts, "penny")
                            market_state["our_side"]       = p_side
                            market_state["our_contracts"]  = p_contracts
                            market_state["our_entry"]      = p_ask
                            market_state["traded"]         = True
                            session["last_market_ticker"]  = ticker
                            session["pnl_today"]          -= p_wager
                            pos_file = Path(__file__).parent / f"current_position_{COIN}.json"
                            with open(pos_file, "w") as pf:
                                json.dump({
                                    "ticker": ticker, "side": p_side,
                                    "contracts": p_contracts, "entry": p_ask,
                                    "mins_remaining": mins_rem, "cost": p_wager,
                                }, pf)
                            write_bot_status("traded", direction=p_side, mins_remaining=mins_rem)
                            print(f"  🎰 PENNY BOUGHT {p_contracts} contracts for ${p_wager:.2f}")
                        else:
                            print(f"  ❌ Penny buy error: {p_result.get('error','?')}")
                        break
                if market_state.get("traded", False):
                    time.sleep(1)
                    continue  # skip all signal logic — position handled next iteration

            # ── Buy window: only trade within user-defined time window ────
            buy_window_open  = float(cfg.get("buy_window_open",  cfg.get("always_open",  14.0)))
            buy_window_close = float(cfg.get("buy_window_close", cfg.get("always_close",  0.5)))
            loss_cooldown_enabled = bool(cfg.get("hourly_loss_enabled", False))
            consec_loss_trigger   = int(cfg.get("consec_loss_trigger", 3))
            cooldown_markets      = int(cfg.get("cooldown_markets", 3))

            # ── Consecutive loss limit check (triggers cooldown) ────────
            if loss_cooldown_enabled and not session["cooldown_active"] \
                    and session["consec_losses"] >= consec_loss_trigger:
                session["cooldown_active"]      = True
                session["cooldown_remaining"]   = cooldown_markets
                session["cooldown_last_ticker"] = None  # fresh start
                print(f"  LOSS COOLDOWN — {session['consec_losses']} consecutive losses → "
                      f"sitting out {cooldown_markets} markets")

            # ── Cooldown: sit out N markets after consecutive losses ──────
            if session["cooldown_active"]:
                if session["cooldown_remaining"] > 0:
                    # Decrement once per NEW market — not every 2-second loop iteration
                    if ticker != session["cooldown_last_ticker"]:
                        session["cooldown_last_ticker"] = ticker
                        session["cooldown_remaining"] -= 1
                        remaining_after = session["cooldown_remaining"]
                        print(f"  COOLDOWN — new market, {remaining_after} market(s) left to sit out")
                    else:
                        print(f"  COOLDOWN — sitting out this market "
                              f"({session['cooldown_remaining']} remaining after)")
                    write_bot_status("building", mins_remaining=mins_rem)
                    time.sleep(5)
                    continue
                else:
                    session["cooldown_active"]      = False
                    session["cooldown_last_ticker"] = None
                    session["consec_losses"]        = 0  # reset streak after cooldown
                    print(f"  COOLDOWN OVER — resuming trading")

            # ── Buy window gate — enforced in ALL modes including "always" ─
            if not (buy_window_close <= mins_rem <= buy_window_open):
                if mins_rem > buy_window_open:
                    print(f"  WINDOW HOLD — {mins_rem:.1f}min left, buy window opens at {buy_window_open:.1f}min")
                else:
                    print(f"  WINDOW CLOSED — {mins_rem:.1f}min left (closes at {buy_window_close:.1f}min)")
                write_bot_status("building", mins_remaining=mins_rem)
                time.sleep(2)
                continue

            # ── Compute signals ──────────────────────────────────────────
            sig = get_signal(strike_price=strike, strike_type=s_type,
                           mins_remaining=mins_rem,
                           yes_ask=yes_ask, no_ask=no_ask)
            conviction, direction, components = calc_conviction(sig, yes_ask, no_ask)

            # ── Inverse mode: flip direction immediately so ALL downstream
            #    logic (gates, Kelly, sizing, price checks) uses the right side
            if inverse_bet_enabled:
                direction = "no" if direction == "yes" else "yes"

            ask = yes_ask if direction == "yes" else no_ask

            # ── Write signals to status file for dashboard ────────────────
            try:
                _sig_payload = {
                    "momentum": None, "liq": None, "kalshi": None,
                    "crowd": None, "multi_tf": None,
                    "orderbook": None, "orderflow": None,
                    "yes_votes": sig.get("yes_votes", 0),
                    "no_votes":  sig.get("no_votes",  0),
                    "conviction_streak": 0,
                    "conviction_dir":    direction,
                }
                for _s in sig.get("signals", []):
                    _n = _s.get("name","").lower()
                    _v = "bull" if _s.get("yes_prob", 0.5) > 0.55 else ("bear" if _s.get("yes_prob", 0.5) < 0.45 else "neutral")
                    if "moment" in _n:                    _sig_payload["momentum"]  = _v
                    elif "liq"  in _n:                   _sig_payload["liq"]       = _v
                    elif "kalshi" in _n or "dist" in _n: _sig_payload["kalshi"]    = _v
                    elif "crowd" in _n:                  _sig_payload["crowd"]     = _v
                    elif "multi" in _n:                  _sig_payload["multi_tf"]  = _v
                    elif "book" in _n:                   _sig_payload["orderbook"] = _v
                    elif "flow" in _n:                   _sig_payload["orderflow"] = _v
                write_bot_status("building", direction=direction,
                                 conviction=conviction, mins_remaining=mins_rem,
                                 signals=_sig_payload)
            except Exception:
                pass

            # ── Signal breakdown (printed every loop so you can see why) ─
            coin_now = btc_state.get("price", 0)
            dist_raw = coin_now - float(strike) if coin_now and strike else 0
            sig_lines = []
            for s in sig.get("signals", []):
                vote = "YES" if s["yes_prob"] > 0.55 else ("NO" if s["yes_prob"] < 0.45 else "---")
                sig_lines.append(f"{s['name'][:5]}:{vote}({s['yes_prob']:.2f})")
            print(f"  SIGNALS  {COIN}${coin_now:.0f}  strike${float(strike):.0f}  "
                  f"dist={dist_raw:+.0f}  type={s_type!r}  "
                  f"safe_side={components['safe_side']}  "
                  f"sig_dir={sig['signal_direction']}  "
                  f"→ {direction.upper()}  |  " + "  ".join(sig_lines))
            print(f"  CONV_DBG  safety={components['safety_score']:.3f}×0.20  "
                  f"sig={components['sig_score']:.3f}×0.45  "
                  f"time={components['t_score']:.3f}×0.25  "
                  f"price={components['price_conf']:.3f}×0.10  "
                  f"= {conviction:.3f}")

            # ── Budget check ─────────────────────────────────────────────
            remaining_budget = max_market_wager - session["market_wagered"]

            # ── Streak tracking ──────────────────────────────────────────
            if direction == streak["direction"]:
                streak["count"] += 1
                streak["conviction"] = max(streak["conviction"] * 0.6 + conviction * 0.4,
                                          conviction * 0.8)
            else:
                streak = {"direction": direction, "count": 1, "conviction": conviction}

            needed         = required_readings(conviction)
            avg_conviction = conviction
            already_traded = market_state["traded"]

            # ── Time-based price threshold for this window ───────────────
            max_price = get_price_threshold(mins_rem)
            # always_max_price is a hard user-set ceiling — never exceed it
            if always_max_price > 0:
                max_price = min(max_price, always_max_price)

            print(f"[{datetime.now().strftime('%H:%M:%S')}] {ticker} | "
                  f"{mins_rem:.1f}min ({secs_remaining:.0f}s) | "
                  f"{direction.upper()} conv={conviction:.2f} "
                  f"streak={streak['count']}/{needed} | "
                  f"ask={ask:.2f} threshold=<{max_price:.2f} "
                  f"safety={components['pos_safety']:.2f} "
                  f"budget=${remaining_budget:.2f} "
                  f"{'[TRADED]' if already_traded else ''}"
                  f"{'[WATCHING]' if market_watching['active'] else ''}")

            # ── Last 60s top-up ──────────────────────────────────────────
            if (already_traded
                    and secs_remaining < 60
                    and not market_state["topup_done"]
                    and remaining_budget >= min_bet
                    and components["pos_safety"] >= 2.0
                    and components["safe_side"] == market_state["our_side"]):

                try:
                    live_yes, live_no = get_market_prices(ticker)
                    topup_ask = live_yes if market_state["our_side"] == "yes" else live_no
                    if topup_ask > 0 and topup_ask < 0.95:
                        topup_contracts = int(remaining_budget / topup_ask)
                        topup_contracts = min(topup_contracts, MAX_CONTRACTS)
                        if topup_contracts >= 1:
                            topup_cost = topup_contracts * topup_ask
                            print(f"\n  >>> TOP-UP: BUY {market_state['our_side'].upper()} "
                                  f"| {topup_contracts} @ ${topup_ask:.3f} "
                                  f"| cost ${topup_cost:.2f} "
                                  f"| {secs_remaining:.0f}s left")
                            result = place_order(ticker, market_state["our_side"],
                                               topup_ask, topup_contracts)
                            log_trade(ticker, market_state["our_side"],
                                     topup_ask, topup_contracts, "topup")
                            session["market_wagered"]    += topup_cost
                            session["trades_today"]      += 1
                            session["trades_this_market"] += 1
                            market_state["topup_done"]   = True
                except Exception as e:
                    print(f"  Top-up error: {e}")

            # ── Loss mitigation flip ─────────────────────────────────────
            elif (already_traded
                    and not market_state["flip_done"]
                    and secs_remaining > 45
                    and remaining_budget >= min_bet
                    and components["pos_safety"] >= 2.0):

                our_side  = market_state["our_side"]
                flip_side = "no" if our_side == "yes" else "yes"
                safe_side = components["safe_side"]

                if safe_side == flip_side:
                    try:
                        live_yes, live_no = get_market_prices(ticker)
                        current_val = live_yes if our_side == "yes" else live_no
                        loss_pct    = current_val / market_state["our_entry"] \
                                      if market_state["our_entry"] > 0 else 1.0

                        if loss_pct < 0.20:
                            flip_ask = live_no if flip_side == "no" else live_yes
                            if flip_ask > 0:
                                flip_contracts = int(remaining_budget / flip_ask)
                                flip_contracts = min(flip_contracts, MAX_CONTRACTS)
                                if flip_contracts >= 1:
                                    flip_cost = flip_contracts * flip_ask
                                    print(f"\n  >>> FLIP: position worth {loss_pct:.0%} of entry. "
                                          f"BUY {flip_side.upper()} | "
                                          f"{flip_contracts} @ ${flip_ask:.3f}")
                                    result = place_order(ticker, flip_side,
                                                       flip_ask, flip_contracts)
                                    log_trade(ticker, flip_side,
                                             flip_ask, flip_contracts, "flip")
                                    session["market_wagered"]    += flip_cost
                                    session["trades_today"]      += 1
                                    session["trades_this_market"] += 1
                                    market_state["flip_done"]     = True
                    except Exception as e:
                        print(f"  Flip error: {e}")

            # ── Normal trade logic ────────────────────────────────────────
            if already_traded:
                write_bot_status("traded", direction=market_state["our_side"],
                                mins_remaining=mins_rem)
                time.sleep(2)
                continue

            if remaining_budget < min_bet:
                print(f"  MARKET BUDGET exhausted")
                time.sleep(2)
                continue

            # ── Minimum agreeing signals gate ────────────────────────────
            # Require at least N individual signals pointing the same direction.
            # Prevents trading when only 1 signal (e.g. Multi-TF) has an opinion.
            n_agreeing = sig.get("agreeing_count", 0)
            if n_agreeing < min_signals:
                print(f"  WEAK SIGNAL — only {n_agreeing}/{min_signals} signals agree "
                      f"(yes_ct={sum(1 for s in sig.get('signals',[]) if s['yes_prob']>0.55)} "
                      f"no_ct={sum(1 for s in sig.get('signals',[]) if s['yes_prob']<0.45)}) — skip")
                write_bot_status("building", direction=direction,
                                conviction=conviction, mins_remaining=mins_rem)
                time.sleep(2)
                continue

            # ── Mode-based conviction check ──────────────────────────────
            if mode == "always":
                # Always mode — no time window restriction, trade whenever conditions are met
                # Pick direction by trigger_method
                if trigger_method == "signal":
                    # Require minimum signal agreement — don't fire on noise
                    if sig["signal_agreement"] < 0.25 and not components.get("safe_side"):
                        print(f"  ALWAYS — weak signal agreement {sig['signal_agreement']:.2f} — waiting")
                        write_bot_status("building", direction=direction,
                                        conviction=conviction, mins_remaining=mins_rem)
                        time.sleep(2)
                        continue
                else:
                    # EV: use signal direction when signals agree, otherwise pick cheaper side
                    yes_ev  = sig["our_yes_prob"] - yes_ask
                    no_ev   = sig["our_no_prob"]  - no_ask
                    best_ev = max(yes_ev, no_ev)
                    if min_ev_edge > 0 and best_ev < min_ev_edge:
                        print(f"  ALWAYS — EV edge {best_ev*100:.1f}¢ < min {min_ev_edge*100:.0f}¢ — skipping")
                        write_bot_status("building", direction=direction,
                                        conviction=conviction, mins_remaining=mins_rem)
                        time.sleep(2)
                        continue
                    # Direction: signals first, safe_side second — never raw EV
                    # In inverse mode, flip whatever direction signals/safe_side pick
                    if sig["signal_direction"] and sig["signal_agreement"] >= 0.20:
                        _sd = sig["signal_direction"]
                        direction = ("no" if _sd == "yes" else "yes") if inverse_bet_enabled else _sd
                        print(f"  ALWAYS — {'INVERSE ' if inverse_bet_enabled else ''}signal: {direction.upper()} "
                              f"(agree={sig['signal_agreement']:.2f})")
                    elif components["safe_side"]:
                        _ss = components["safe_side"]
                        direction = ("no" if _ss == "yes" else "yes") if inverse_bet_enabled else _ss
                        print(f"  ALWAYS — {'INVERSE ' if inverse_bet_enabled else ''}safe_side: {direction.upper()}")
                ask = yes_ask if direction == "yes" else no_ask
                # Enforce max price for always-buy
                if ask > always_max_price:
                    write_bot_status("watching", direction=direction,
                                    max_price=always_max_price, mins_remaining=mins_rem)
                    print(f"  ALWAYS — in window but price {ask:.0%} > max {always_max_price:.0%}, waiting...")
                    time.sleep(2)
                    continue
            else:
                if avg_conviction < min_threshold:
                    write_bot_status("building", direction=direction,
                                    conviction=conviction, mins_remaining=mins_rem)
                    print(f"  HOLD — conviction {avg_conviction:.2f} < threshold {min_threshold:.2f}")
                    time.sleep(2)
                    continue
                if streak["count"] < needed:
                    write_bot_status("building", direction=direction,
                                    conviction=conviction, mins_remaining=mins_rem)
                    print(f"  BUILDING — {streak['count']}/{needed} (conv {avg_conviction:.2f})")
                    time.sleep(2)
                    continue

            # ── Price threshold gate (timing-based) ──────────────────────
            # We have conviction. Now check if price is acceptable for this time window.
            ask = yes_ask if direction == "yes" else no_ask  # re-fetch after direction finalized

            if ask <= 0:
                time.sleep(2)
                continue

            if ask >= 0.99:
                print(f"  SKIP — ask {ask:.2f} too expensive (<1¢ profit per contract)")
                time.sleep(2)
                continue

            if ask < 0.03:
                print(f"  SKIP — ask {ask:.2f} too cheap (near-certain loss)")
                time.sleep(2)
                continue

            # Time-window price check
            if ask > max_price:
                now_t = time.time()

                # Start or continue watching session
                if not market_watching["active"] or market_watching["ticker"] != ticker:
                    market_watching = {
                        "active":        True,
                        "direction":     direction,
                        "max_price":     max_price,
                        "since":         now_t,
                        "ticker":        ticker,
                        "price_history": [(now_t, ask)],
                    }
                    print(f"\n  ⏳ WATCHING {direction.upper()} | "
                          f"price {ask:.0%} > target <{max_price:.0%} for {mins_rem:.1f}min | "
                          f"waiting for price to drop...")
                    write_bot_status("watching", direction=direction,
                                    max_price=max_price, conviction=avg_conviction,
                                    mins_remaining=mins_rem)
                    time.sleep(2)
                    continue  # wait for price to actually drop before firing
                else:
                    # Append to price history, keep last 10 readings (~20s)
                    market_watching["price_history"].append((now_t, ask))
                    if len(market_watching["price_history"]) > 10:
                        market_watching["price_history"].pop(0)

                    ph = market_watching["price_history"]
                    watching_secs = now_t - market_watching["since"]

                    # ── Momentum flip: if we're watching YES (waiting for drop)
                    # but price is falling fast AND time is short, flip to NO ──
                    if len(ph) >= 4 and mins_rem < 7.0:
                        oldest_p = ph[0][1]
                        newest_p = ph[-1][1]
                        price_delta = newest_p - oldest_p  # negative = falling
                        elapsed = ph[-1][0] - ph[0][0]
                        rate = price_delta / elapsed if elapsed > 0 else 0  # per second

                        if direction == "yes" and rate < -0.003:
                            # YES falling fast — momentum favors NO
                            flip_ask = no_ask
                            if flip_ask <= max_price:
                                print(f"\n  🔄 MOMENTUM FLIP: YES dropping fast "
                                      f"({price_delta:.0%} in {elapsed:.0f}s) → switching to NO "
                                      f"@ {flip_ask:.0%}")
                                direction = "no"
                                ask = flip_ask
                                market_watching["active"] = False
                                # Fall through to buy below
                            else:
                                print(f"  ⏳ WATCHING {direction.upper()} | "
                                      f"{ask:.0%} > <{max_price:.0%} | "
                                      f"falling {price_delta:.0%} in {elapsed:.0f}s | {watching_secs:.0f}s")
                                write_bot_status("watching", direction=direction,
                                                max_price=max_price, conviction=avg_conviction,
                                                mins_remaining=mins_rem)
                                time.sleep(2)
                                continue

                        elif direction == "no" and rate > 0.003:
                            # NO falling fast (YES rising) — flip to YES
                            flip_ask = yes_ask
                            if flip_ask <= max_price:
                                print(f"\n  🔄 MOMENTUM FLIP: NO dropping fast → switching to YES "
                                      f"@ {flip_ask:.0%}")
                                direction = "yes"
                                ask = flip_ask
                                market_watching["active"] = False
                            else:
                                print(f"  ⏳ WATCHING {direction.upper()} | "
                                      f"{ask:.0%} > <{max_price:.0%} | {watching_secs:.0f}s")
                                write_bot_status("watching", direction=direction,
                                                max_price=max_price, conviction=avg_conviction,
                                                mins_remaining=mins_rem)
                                time.sleep(2)
                                continue
                        else:
                            print(f"  ⏳ WATCHING {direction.upper()} | "
                                  f"{ask:.0%} > <{max_price:.0%} | {watching_secs:.0f}s")
                            write_bot_status("watching", direction=direction,
                                            max_price=max_price, conviction=avg_conviction,
                                            mins_remaining=mins_rem)
                            time.sleep(2)
                            continue
                    else:
                        print(f"  ⏳ WATCHING {direction.upper()} | "
                              f"{ask:.0%} > <{max_price:.0%} | {watching_secs:.0f}s")
                        write_bot_status("watching", direction=direction,
                                        max_price=max_price, conviction=avg_conviction,
                                        mins_remaining=mins_rem)
                        time.sleep(2)
                        continue

            # Price just entered the acceptable range
            if market_watching["active"] and market_watching["ticker"] == ticker:
                ph = market_watching["price_history"]
                market_watching["active"] = False

                # ── Stabilization check: if price is STILL falling, wait for bottom ──
                # Only wait if we have enough readings and still have time
                if len(ph) >= 3 and mins_rem > 1.5:
                    oldest_p = ph[0][1]
                    newest_p = ph[-1][1]
                    price_delta = newest_p - oldest_p
                    elapsed = ph[-1][0] - ph[0][0] if ph[-1][0] != ph[0][0] else 1
                    rate = price_delta / elapsed  # per second

                    still_falling = (direction == "yes" and rate < -0.002) or \
                                    (direction == "no"  and rate > 0.002)

                    if still_falling:
                        print(f"  📉 PRICE IN RANGE but still moving "
                              f"({price_delta:.0%}/{elapsed:.0f}s) — holding for best price...")
                        market_watching["active"] = True   # keep watching
                        write_bot_status("watching", direction=direction,
                                        max_price=max_price, conviction=avg_conviction,
                                        mins_remaining=mins_rem)
                        time.sleep(2)
                        continue
                    else:
                        print(f"  ✅ PRICE STABILIZED at {ask:.0%} ≤ {max_price:.0%} — firing")
                else:
                    print(f"  ✅ PRICE IN RANGE: {ask:.0%} ≤ {max_price:.0%} — firing")

            # ── Safety margin: BTC must be WITHIN X dollars of strike ────
            if safety_margin_usd > 0:
                btc_now = btc_state.get("price")
                if btc_now and strike:
                    dist = abs(btc_now - float(strike))
                    if dist > safety_margin_usd:
                        print(f"  SKIP — Safety margin: BTC ${dist:.0f} from strike "
                              f"(must be within ${safety_margin_usd:.0f})")
                        time.sleep(2)
                        continue

            # ── Momentum gate: BTC must trend with the bet ───────────────
            if momentum_gate_on:
                passes, rate = check_momentum(direction, window_secs=momentum_window_s)
                if not passes:
                    print(f"  SKIP — Momentum gate: {rate*100:+.4f}%/s against {direction.upper()}")
                    time.sleep(2)
                    continue

            # ── Max trades per session cap ───────────────────────────────
            if max_trades_session > 0 and session["trades_this_market"] >= max_trades_session:
                print(f"  MAX TRADES/MARKET — {session['trades_this_market']}/{max_trades_session} this market")
                write_bot_status("idle")
                time.sleep(5)
                continue

            # ── Edge threshold gate ──────────────────────────────────────
            # Only trade when we have a quantified minimum edge over the market.
            # our_prob is what signals say; ask is what market prices in.
            # Edge = our belief minus market price. Below min_edge → skip.
            if inverse_bet_enabled:
                _edge_prob = sig["our_no_prob"] if direction == "yes" else sig["our_yes_prob"]
            else:
                _edge_prob = sig["our_yes_prob"] if direction == "yes" else sig["our_no_prob"]
            raw_edge = _edge_prob - ask
            min_edge_threshold = float(cfg.get("min_edge_threshold", 0.02))  # default 2%
            if raw_edge < min_edge_threshold:
                print(f"  SKIP — Edge {raw_edge*100:.1f}¢ < min {min_edge_threshold*100:.0f}¢ "
                      f"(our_prob={_edge_prob:.2f} ask={ask:.2f})")
                write_bot_status("building", direction=direction,
                                conviction=conviction, mins_remaining=mins_rem)
                time.sleep(2)
                continue

            # ── Liquidity gate ───────────────────────────────────────────
            # Skip or reduce size if spread is too wide or market is thin.
            _liq_ok, _liq_reason, _liq_haircut = check_liquidity(
                ticker, direction, ask,
                min_volume=float(cfg.get("min_volume", 500)),
                max_spread_cents=float(cfg.get("max_spread_cents", 5)),
            )
            print(f"  LIQUIDITY: {_liq_reason}")
            if not _liq_ok:
                write_bot_status("building", direction=direction,
                                conviction=conviction, mins_remaining=mins_rem)
                time.sleep(2)
                continue

            # ── Bet sizing ───────────────────────────────────────────────
            # Conviction factor: smoothly scales from 0→1 as conviction
            # goes from the fire threshold up to 1.0.
            # At minimum conviction (just crossed threshold) → smallest bet.
            # At peak conviction → full max wager.
            conv_low  = min_threshold   # conviction at which factor = 0
            conv_span = max(1.0 - conv_low, 0.01)
            conv_factor = max(0.0, min(1.0, (avg_conviction - conv_low) / conv_span))

            if kelly_enabled:
                # In inverse mode the signal probabilities are backwards — use the
                # opposite side's probability as our belief for the direction we're betting.
                if inverse_bet_enabled:
                    our_prob = sig["our_no_prob"] if direction == "yes" else sig["our_yes_prob"]
                else:
                    our_prob = sig["our_yes_prob"] if direction == "yes" else sig["our_no_prob"]
                edge       = max(our_prob - ask, 0.01)
                kelly_full = edge / max(1.0 - ask, 0.01)
                try:
                    current_bal_k = get_balance()
                except Exception:
                    current_bal_k = max_market_wager * 10
                # Conservative dual cap: 15% fractional Kelly, max 5% of bankroll, hard dollar cap
                # This matches the profitable weather bot's sizing discipline
                bankroll_pct_cap = current_bal_k * float(cfg.get("kelly_bankroll_pct_cap", 0.05))
                kelly_hard_cap   = float(cfg.get("kelly_hard_cap", 75.0))
                kelly_wager = current_bal_k * kelly_full * kelly_fraction * (0.25 + 0.75 * conv_factor)
                # Apply liquidity haircut
                kelly_wager *= _liq_haircut
                wager = min(kelly_wager, remaining_budget, max_market_wager,
                            bankroll_pct_cap, kelly_hard_cap)
                wager = max(wager, min_bet)
                print(f"  Kelly: edge={edge:.3f} K={kelly_full:.3f} "
                      f"frac={kelly_fraction} conv_factor={conv_factor:.2f} "
                      f"bal_cap=${bankroll_pct_cap:.2f} → ${wager:.2f}")
            else:
                # Linear ramp: min_bet at threshold, max_market_wager at full conviction
                raw_wager = min_bet + (max_market_wager - min_bet) * conv_factor
                raw_wager *= _liq_haircut   # apply liquidity haircut
                wager = min(raw_wager, remaining_budget, max_market_wager)
                wager = max(wager, min_bet)
                print(f"  Sizing: conv={avg_conviction:.3f} factor={conv_factor:.2f} "
                      f"haircut={_liq_haircut:.1f} → ${wager:.2f}")

            # Hard cap: never exceed max_market_wager regardless of rounding
            wager = min(wager, max_market_wager)

            contracts = int(wager / ask)
            contracts = min(contracts, MAX_CONTRACTS)

            if contracts < 1:
                # Kelly undersized the wager, but if we can afford 1 contract within
                # the hard cap, buy it — minimum viable trade is always 1 contract
                if ask <= max_market_wager and ask <= remaining_budget:
                    contracts = 1
                    print(f"  Kelly undersized → rounding up to 1 contract @ ${ask:.3f}")
                else:
                    print(f"  SKIP — not enough for 1 contract at ${ask:.3f} "
                          f"(budget=${remaining_budget:.2f} max=${max_market_wager:.2f})")
                    time.sleep(2)
                    continue

            actual_cost   = contracts * ask
            # Hard cap enforcement after rounding — trim contracts if over limit
            while actual_cost > max_market_wager and contracts > 1:
                contracts  -= 1
                actual_cost = contracts * ask
            potential_win = contracts * (1.0 - ask)

            # Enforce minimum bet — skip if sized trade is below threshold
            if actual_cost < min_bet:
                print(f"  SKIP — bet ${actual_cost:.2f} below min_bet ${min_bet:.2f} "
                      f"(low conviction factor)")
                time.sleep(2)
                continue

            # ── FINAL HARD PRICE GATE — last check before money leaves ─────
            if always_max_price > 0 and ask > always_max_price:
                print(f"  ❌ FINAL PRICE GATE — ask {ask:.0%} > max {always_max_price:.0%} — aborting")
                market_state["traded"] = False
                time.sleep(2)
                continue
            if actual_cost > max_market_wager:
                print(f"  ❌ FINAL SPEND GATE — cost ${actual_cost:.2f} > max ${max_market_wager:.2f} — aborting")
                market_state["traded"] = False
                time.sleep(2)
                continue

            print(f"\n  >>> FIRE: BUY {direction.upper()} | {contracts} @ ${ask:.3f} | "
                  f"conv={avg_conviction:.2f} streak={streak['count']} | "
                  f"cost=${actual_cost:.2f} win=${potential_win:.2f} | "
                  f"{'[CONTRARIAN]' if components['contrarian'] else ''}"
                  f"{'[INVERSE]' if inverse_bet_enabled else ''}")

            # Lock market before placing order
            session["last_market_ticker"] = ticker
            market_state["traded"] = True

            result = place_order(ticker, direction, ask, contracts)

            if "error" in result:
                print(f"  ORDER ERROR: {result['error'].get('details', result['error'])}")
                session["last_market_ticker"] = None
                market_state["traded"] = False
                continue

            # ── Confirm via positions endpoint — the only check that matters ──
            # If we bought something on Kalshi, a position appears. Simple as that.
            actual_contracts = contracts
            fill_cost        = actual_cost
            confirmed        = False

            # Poll every 200ms — limit orders at ask+5¢ fill in milliseconds
            deadline = time.time() + 5        # 5 second max wait
            while time.time() < deadline:
                time.sleep(0.2)
                kp = get_position_for_ticker(ticker)
                if kp:
                    pos_val = abs(float(kp.get("position_fp", kp.get("position", 0))))
                    if pos_val > 0:
                        actual_contracts = int(pos_val)
                        fill_cost        = round(float(kp.get("market_exposure_dollars", actual_cost)), 2)
                        confirmed        = True
                        elapsed          = round(time.time() - (deadline - 5), 1)
                        print(f"  ✅ CONFIRMED in {elapsed}s: {actual_contracts} contracts @ ${fill_cost:.2f}")
                        break

            if not confirmed:
                # 5 seconds with no position — order didn't fill, cancel it
                order_id = result.get("order", {}).get("order_id") or result.get("order_id")
                if order_id:
                    try:
                        cpath = f"/trade-api/v2/portfolio/orders/{order_id}"
                        requests.delete(BASE_URL + f"/portfolio/orders/{order_id}",
                                        headers=sign_request("DELETE", cpath), timeout=5)
                        print(f"  🚫 No fill after 5s — order cancelled. No position taken.")
                    except Exception as ce:
                        print(f"  Cancel error: {ce}")
                session["last_market_ticker"] = None
                market_state["traded"] = False
                continue

            open_trades.append({
                "ticker": ticker, "side": direction,
                "contracts": actual_contracts, "entry_price": ask,
            })

            # ── Generate trade code ──────────────────────────────────────
            tcode = ""
            if _TC_AVAILABLE:
                try:
                    tcode = _tc.generate(
                        coin              = COIN,
                        yes_ask           = round(ask * 100),
                        secs_remaining    = secs_rem,
                        wager             = actual_cost,
                        wager_mode        = cfg.get("wager_mode", "dollar"),
                        min_ev_edge       = min_ev_edge,
                        signal_agreement  = int(cfg.get("signal_agreement", 3)),
                        take_profit_cents = take_profit_cents,
                        stop_loss_cents   = float(cfg.get("stop_loss_cents", 0)),
                        penny_enabled     = bool(cfg.get("penny_enabled", False)),
                        signal_dir        = direction,
                        yes_votes         = sig.get("yes_votes", 0),
                        no_votes          = sig.get("no_votes", 0),
                        conviction_streak = streak.get("count", 0),
                        trade_type        = "MAIN",
                    )
                except Exception as _tce:
                    print(f"  trade_code gen error: {_tce}")

            log_trade(ticker, direction, ask, actual_contracts, "main", tcode)
            session["trades_today"]       += 1
            session["trades_this_market"] += 1
            session["market_wagered"]     += fill_cost

            market_state["our_side"]      = direction
            market_state["our_contracts"] = actual_contracts
            market_state["our_entry"]     = ask

            # Write position file for dashboard
            pos_file = Path(__file__).parent / f"current_position_{COIN}.json"
            with open(pos_file, "w") as pf:
                json.dump({
                    "ticker":        ticker,
                    "side":          direction,
                    "contracts":     actual_contracts,
                    "entry":         ask,
                    "mins_remaining": mins_rem,
                    "cost":          fill_cost,
                }, pf)

            write_bot_status("traded", direction=direction, mins_remaining=mins_rem)
            streak = {"direction": None, "count": 0, "conviction": 0.0}

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)

        time.sleep(0.5)


if __name__ == "__main__":
    run_bot()
