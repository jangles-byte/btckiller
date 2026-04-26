"""
BTC.KILLER — Dashboard Backend
Serves the web UI and talks to Kalshi API for live data.
"""

import os
import sys
import json
import time
import threading
import webbrowser
import csv
import base64
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from signals import start_feed_thread, get_signal, btc_state

load_dotenv()

app = Flask(__name__)
BOT_DIR = Path(__file__).parent

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

with open(PRIVATE_KEY_PATH, "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)

# ── Shared state ───────────────────────────────────────
state = {
    "btc_price": None,
    "price_history": [],
    "balance": None,
    "market_ticker": None,
    "secs_remaining": None,
    "yes_ask": None,
    "no_ask": None,
    "our_yes_prob": 0.5,
    "our_no_prob": 0.5,
    "yes_ev": 0.0,
    "no_ev": 0.0,
    "conviction": 0.0,
    "conviction_direction": None,
    "streak_count": 0,
    "pos_safety": 0.0,
    "safe_side": None,
    "trend_1h": 0.0,
    "trend_6h": 0.0,
    "trend_24h": 0.0,
    "range_pct": 0.5,
    "volatility": 1.0,
    "candles_loaded": False,
    "confidence": 0.0,
    "signals": [],
    # P&L from Kalshi API (primary source)
    "kalshi_pnl_dollars": None,    # dollar P&L from settlements
    "kalshi_pnl_pct": None,        # percentage P&L
    "kalshi_win_rate": None,        # % win rate from settlements
    "kalshi_wins": 0,
    "kalshi_losses": 0,
    "kalshi_total_cost": 0.0,
    "kalshi_stats_updated": 0,
    # Fallback local P&L
    "pnl_today": 0.0,
    "trades_today": 0,
    "wins_today": 0,
    "losses_today": 0,
    "avg_win": 0.0,
    "avg_loss": 0.0,
    "profit_factor": 0.0,
    "expected_val": 0.0,
    "current_position": None,
    "pnl_period": "day",
    "killed": False,
    "bot_running": False,
    "bot_status": "idle",          # from bot_status.json
    "bot_watching_direction": None,
    "bot_watching_max_price": None,
    "target_price": None,
    "target_dir": None,
    "settings": {
        "mode": "balanced",
        "daily_loss_limit": 50.0,
        "max_session_wager": 5.0,
        "wager_mode": "dollar",
        "wager_pct": 10.0,
        "trigger_time": 5.0,
        "trigger_method": "ev",
        "allow_early_buy": True,
        "early_max_price": 0.75,
        "telegram_enabled": False,
        "telegram_token": "",
        "telegram_allowed_users": [],
        # Advanced filters
        "min_ev_edge":        0.05,
        "momentum_gate":      False,
        "momentum_window":    30,
        "safety_margin":      0.0,
        "max_trades_session": 0,
        "take_profit_cents":  0.0,
        "kelly_enabled":      False,
        "kelly_fraction":     0.25,
        "wallet_floor":       0.0,
    }
}
state_lock = threading.Lock()
bot_process = None
telegram_thread = None

# Rolling buffer of bot terminal output (last 200 lines)
from collections import deque
bot_log_buffer = deque(maxlen=200)

def _stream_bot_output(proc):
    """Read bot subprocess stdout/stderr into bot_log_buffer."""
    try:
        for line in iter(proc.stdout.readline, b''):
            text = line.decode('utf-8', errors='replace').rstrip()
            if text:
                bot_log_buffer.append(text)
    except Exception:
        pass

# ── Kalshi API helpers ─────────────────────────────────
def sign_request(method, path):
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode()
    signature = PRIVATE_KEY.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }

def kalshi_get_balance():
    path = "/trade-api/v2/portfolio/balance"
    r = requests.get(KALSHI_BASE + "/portfolio/balance",
                     headers=sign_request("GET", path), timeout=5)
    return r.json().get("balance", 0) / 100

def kalshi_get_market():
    path = "/trade-api/v2/markets"
    params = {"series_ticker": "KXBTC15M", "status": "open", "limit": 5}
    r = requests.get(KALSHI_BASE + "/markets",
                     headers=sign_request("GET", path), params=params, timeout=5)
    markets = r.json().get("markets", [])
    now = datetime.now(timezone.utc)
    best, best_diff = None, float("inf")
    for m in markets:
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        diff = (close - now).total_seconds()
        if 0 < diff < best_diff:
            best_diff = diff
            best = m
    return best, best_diff

def kalshi_get_settlements(limit=200, min_date=None):
    """
    Fetch settled markets from Kalshi portfolio.
    Returns list of settlement dicts with calculated pnl fields.
    """
    try:
        path = "/trade-api/v2/portfolio/settlements"
        params = {"limit": limit}
        r = requests.get(
            KALSHI_BASE + "/portfolio/settlements",
            headers=sign_request("GET", path),
            params=params,
            timeout=10,
        )
        data = r.json()
        all_settlements = data.get("settlements", [])
        # Filter client-side to BTC 15M markets only
        settlements = [s for s in all_settlements
                       if s.get("market_ticker", "").startswith("KXBTC15M")]

        if not settlements:
            return []

        result = []
        for s in settlements:
            settled_time_str = s.get("settled_time", "")
            # Filter by date if requested
            if min_date and settled_time_str:
                try:
                    settled_dt = datetime.fromisoformat(settled_time_str.replace("Z", "+00:00"))
                    if settled_dt < min_date:
                        continue
                except Exception:
                    pass

            revenue       = s.get("revenue", 0)             # cents
            yes_cost      = s.get("yes_total_cost", 0)       # cents
            no_cost       = s.get("no_total_cost", 0)        # cents
            total_cost    = yes_cost + no_cost
            pnl_cents     = revenue - total_cost
            pnl_dollars   = pnl_cents / 100

            pnl_pct = None
            if total_cost > 0:
                pnl_pct = (pnl_cents / total_cost) * 100

            result.append({
                "ticker":       s.get("market_ticker", ""),
                "settled_time": settled_time_str,
                "revenue":      revenue / 100,
                "cost":         total_cost / 100,
                "pnl":          pnl_dollars,
                "pnl_pct":      pnl_pct,
                "won":          pnl_cents > 0,
            })
        return result
    except Exception as e:
        print(f"Settlements fetch error: {e}")
        return []

def kalshi_compute_stats(period="day"):
    """
    Fetch settlements from Kalshi and compute P&L stats for the given period.
    Returns dict with pnl, win_rate, wins, losses, total_cost, pnl_pct.
    """
    now = datetime.now(timezone.utc)

    if period == "day":
        min_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        min_date = now - timedelta(days=7)
    elif period == "month":
        min_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        min_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        min_date = None  # all time

    settlements = kalshi_get_settlements(limit=500, min_date=min_date)

    total_pnl   = sum(s["pnl"] for s in settlements)
    total_cost  = sum(s["cost"] for s in settlements)
    wins        = [s for s in settlements if s["won"]]
    losses      = [s for s in settlements if not s["won"]]
    n_wins      = len(wins)
    n_losses    = len(losses)
    total_trades = n_wins + n_losses

    win_rate = (n_wins / total_trades * 100) if total_trades > 0 else None
    pnl_pct  = (total_pnl / total_cost * 100) if total_cost > 0 else None

    return {
        "pnl":          total_pnl,
        "pnl_pct":      pnl_pct,
        "win_rate":     win_rate,
        "wins":         n_wins,
        "losses":       n_losses,
        "total_trades": total_trades,
        "total_cost":   total_cost,
    }

def kalshi_get_open_position(ticker):
    """
    Check Kalshi portfolio positions for an open position on this ticker.
    Returns position dict or None.
    """
    try:
        path = "/trade-api/v2/portfolio/positions"
        r = requests.get(
            KALSHI_BASE + "/portfolio/positions",
            headers=sign_request("GET", path),
            params={"ticker": ticker},
            timeout=5,
        )
        positions = r.json().get("market_positions", [])
        for pos in positions:
            if pos.get("ticker") == ticker and abs(pos.get("position", 0)) > 0:
                return pos
    except Exception as e:
        print(f"Position fetch error: {e}")
    return None

# ── Background updater ─────────────────────────────────
def update_loop():
    global bot_process
    last_balance = 0
    last_market = 0
    last_kalshi_stats = 0
    last_telegram_check = 0
    last_trade_stats = 0   # throttle expensive CSV reads
    market_info = {"strike": None, "strike_type": None, "secs": None, "ticker": None}

    while True:
        try:
            now = time.time()

            # ── Telegram bot control (GIL-atomic dict pop, no lock needed) ──
            ctrl = state.pop("_bot_control", None)
            if ctrl == "start":
                if bot_process is None or bot_process.poll() is not None:
                    bot_log_buffer.clear()
                    env = os.environ.copy()
                    env["PYTHONUNBUFFERED"] = "1"
                    bot_process = subprocess.Popen(
                        [sys.executable, "-u", str(BOT_DIR / "bot.py")],
                        cwd=str(BOT_DIR),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        env=env,
                    )
                    threading.Thread(target=_stream_bot_output, args=(bot_process,), daemon=True).start()
                with state_lock:
                    state["bot_running"] = True
                    state["killed"] = False
                print("[Dashboard] Bot started via Telegram.")
            elif ctrl == "stop":
                if bot_process and bot_process.poll() is None:
                    bot_process.terminate()
                    bot_process = None
                with state_lock:
                    state["bot_running"] = False
                print("[Dashboard] Bot stopped via Telegram.")

            # Telegram watchdog — restart thread if it died
            if now - last_telegram_check > 30:
                _maybe_start_telegram()
                last_telegram_check = now

            # BTC price from signals
            with state_lock:
                state["btc_price"] = btc_state.get("price")
                state["price_history"] = [
                    {"time": t, "price": p}
                    for t, p in btc_state.get("price_history", [])[-200:]
                ]

            # Balance every 30s
            if now - last_balance > 30:
                try:
                    bal = kalshi_get_balance()
                    with state_lock:
                        state["balance"] = bal
                    last_balance = now
                except Exception as e:
                    print(f"Balance error: {e}")

            # Market every 3s
            if now - last_market > 1:
                try:
                    market, secs = kalshi_get_market()
                    if market:
                        strike = market.get("floor_strike") or market.get("cap_strike")
                        strike_type = market.get("strike_type", "")
                        tdir = ("above" if "greater" in strike_type
                                else "below" if "less" in strike_type
                                else strike_type)
                        with state_lock:
                            state["market_ticker"]   = market["ticker"]
                            state["secs_remaining"]  = secs
                            state["yes_ask"]         = float(market.get("yes_ask_dollars", 0))
                            state["no_ask"]          = float(market.get("no_ask_dollars", 0))
                            state["target_price"]    = strike
                            state["target_dir"]      = tdir
                        market_info = {
                            "strike": strike, "strike_type": strike_type,
                            "secs": secs, "ticker": market["ticker"]
                        }
                    last_market = now
                except Exception as e:
                    print(f"Market error: {e}")

            # Kalshi P&L stats every 60s
            if now - last_kalshi_stats > 60:
                try:
                    with state_lock:
                        period = state["pnl_period"]
                    stats = kalshi_compute_stats(period)
                    with state_lock:
                        state["kalshi_pnl_dollars"]  = stats["pnl"]
                        state["kalshi_pnl_pct"]      = stats["pnl_pct"]
                        state["kalshi_win_rate"]     = stats["win_rate"]
                        state["kalshi_wins"]         = stats["wins"]
                        state["kalshi_losses"]       = stats["losses"]
                        state["kalshi_total_cost"]   = stats["total_cost"]
                        state["kalshi_stats_updated"] = now
                    last_kalshi_stats = now
                except Exception as e:
                    print(f"Kalshi stats error: {e}")

            # Signal computation
            if market_info["strike"]:
                mins_left = market_info["secs"] / 60 if market_info["secs"] else None
                sig = get_signal(
                    strike_price=market_info["strike"],
                    strike_type=market_info["strike_type"],
                    mins_remaining=mins_left,
                    yes_ask=state.get("yes_ask"),
                    no_ask=state.get("no_ask"),
                )
                yes_ask = state.get("yes_ask") or 0.5
                no_ask  = state.get("no_ask") or 0.5
                yes_ev  = sig["our_yes_prob"] - yes_ask
                no_ev   = sig["our_no_prob"]  - no_ask

                try:
                    from bot import calc_conviction
                    conv, conv_dir, comp = calc_conviction(sig, yes_ask, no_ask)
                except Exception:
                    conv, conv_dir, comp = 0.0, None, {}

                try:
                    from signals import get_candles_context
                    ctx = get_candles_context()
                    with state_lock:
                        state.update(ctx)
                except Exception:
                    pass

                with state_lock:
                    state["our_yes_prob"]         = sig["our_yes_prob"]
                    state["our_no_prob"]          = sig["our_no_prob"]
                    state["confidence"]           = sig["confidence"]
                    state["signals"]              = sig["signals"]
                    state["yes_ev"]               = yes_ev
                    state["no_ev"]                = no_ev
                    state["conviction"]           = conv
                    state["conviction_direction"] = conv_dir
                    state["pos_safety"]           = sig.get("pos_safety", 0.0)
                    state["safe_side"]            = sig.get("safe_side")

            # Bot status file (written by bot.py)
            try:
                status_file = BOT_DIR / "bot_status.json"
                if status_file.exists():
                    bot_status = json.load(open(status_file))
                    age = now - bot_status.get("updated_at", 0)
                    if age < 30:  # only use if fresh
                        with state_lock:
                            state["bot_status"]             = bot_status.get("status", "idle")
                            state["bot_watching_direction"] = bot_status.get("direction")
                            state["bot_watching_max_price"] = bot_status.get("max_price")
            except Exception:
                pass

            # Local trade stats — throttled to every 5s (CSV reads were blocking market price updates)
            if now - last_trade_stats > 5:
                last_trade_stats = now
                try:
                    trades_folder = BOT_DIR / "trades"
                    trades_folder.mkdir(exist_ok=True)

                    def safe_pnl(r):
                        val = r.get("pnl", "")
                        if not val or val in ("pending", "unfilled", "expired"):
                            return None
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return None

                    def load_period_trades(period):
                        now_dt = datetime.now()
                        all_rows = []
                        for f in sorted(trades_folder.glob("trades_*.csv")):
                            try:
                                with open(f) as fh:
                                    rows = list(csv.DictReader(fh))
                                for r in rows:
                                    ts = r.get("timestamp", "")
                                    try:
                                        dt = datetime.fromisoformat(ts)
                                        if period == "day" and dt.date() != now_dt.date():
                                            continue
                                        elif period == "week" and (now_dt.date() - dt.date()).days > 7:
                                            continue
                                        elif period == "month" and (now_dt.year != dt.year or now_dt.month != dt.month):
                                            continue
                                        elif period == "year" and now_dt.year != dt.year:
                                            continue
                                        all_rows.append(r)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        return all_rows

                    with state_lock:
                        period = state["pnl_period"]

                    all_trades   = load_period_trades(period)
                    today_trades = load_period_trades("day")

                    settled  = [(r, safe_pnl(r)) for r in all_trades]
                    settled  = [(r, p) for r, p in settled if p is not None]
                    pnl      = sum(p for _, p in settled)
                    wins     = [p for _, p in settled if p > 0]
                    losses   = [p for _, p in settled if p <= 0]
                    n_wins   = len(wins)
                    n_losses = len(losses)
                    avg_win  = sum(wins)   / n_wins   if n_wins   else 0
                    avg_loss = sum(losses) / n_losses if n_losses else 0
                    total_won  = sum(wins)
                    total_lost = abs(sum(losses))
                    profit_factor = total_won / total_lost if total_lost > 0 else 0
                    n_settled     = n_wins + n_losses
                    expected_val  = pnl / n_settled if n_settled else 0

                    # Current open position from file
                    current_pos = None
                    pos_file = BOT_DIR / "current_position.json"
                    if pos_file.exists():
                        try:
                            pos_data = json.load(open(pos_file))
                            ticker_now = state.get("market_ticker")
                            if pos_data.get("ticker") == ticker_now:
                                kalshi_pos = kalshi_get_open_position(pos_data["ticker"])
                                if kalshi_pos:
                                    pos_side = "yes" if kalshi_pos.get("position", 0) > 0 else "no"
                                    current_pos = {
                                        "ticker":    pos_data["ticker"],
                                        "side":      pos_data.get("side", pos_side),
                                        "price":     float(pos_data.get("entry", 0)),
                                        "contracts": abs(kalshi_pos.get("position", pos_data.get("contracts", 0))),
                                        "cost":      round(kalshi_pos.get("total_cost", 0) / 100, 2),
                                    }
                                else:
                                    current_pos = {
                                        "ticker":    pos_data["ticker"],
                                        "side":      pos_data.get("side", ""),
                                        "price":     float(pos_data.get("entry", 0)),
                                        "contracts": pos_data.get("contracts", 0),
                                        "cost":      round(pos_data.get("cost", 0), 2),
                                    }
                        except Exception:
                            current_pos = None

                    with state_lock:
                        state["trades_today"]     = len(today_trades)
                        state["pnl_today"]        = pnl
                        state["wins_today"]       = n_wins
                        state["losses_today"]     = n_losses
                        state["avg_win"]          = avg_win
                        state["avg_loss"]         = avg_loss
                        state["profit_factor"]    = profit_factor
                        state["expected_val"]     = expected_val
                        state["current_position"] = current_pos
                except Exception as e:
                    print(f"Trades error: {e}")

        except Exception as e:
            print(f"Update loop error: {e}")

        time.sleep(0.5)

# ── Routes ────────────────────────────────────────────
@app.route("/")
def index():
    from flask import make_response
    resp = make_response(send_from_directory(BOT_DIR, "dashboard.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/api/state")
def api_state():
    with state_lock:
        out = dict(state)
    # Always serve fresh price data straight from the WebSocket feed
    # — don't wait for update_loop to copy it, which can lag by several seconds
    out["btc_price"] = btc_state.get("price")
    out["price_history"] = [
        {"time": t, "price": p}
        for t, p in btc_state.get("price_history", [])[-200:]
    ]
    return jsonify(out)

@app.route("/api/settings", methods=["POST"])
def api_settings():
    settings = request.get_json()
    with state_lock:
        state["settings"].update(settings)
    cfg_path = BOT_DIR / "bot_config.json"
    existing = {}
    if cfg_path.exists():
        try:
            existing = json.load(open(cfg_path))
        except Exception:
            pass
    existing.update(state["settings"])
    with open(cfg_path, "w") as f:
        json.dump(existing, f, indent=2)
    return jsonify({"ok": True})

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    global bot_process
    settings = request.get_json() or {}
    with state_lock:
        state["settings"].update(settings)
        state["bot_running"] = True
        state["killed"] = False
    cfg_path = BOT_DIR / "bot_config.json"
    existing = {}
    if cfg_path.exists():
        try:
            existing = json.load(open(cfg_path))
        except Exception:
            pass
    existing.update(state["settings"])
    with open(cfg_path, "w") as f:
        json.dump(existing, f, indent=2)
    if bot_process is None or bot_process.poll() is not None:
        bot_log_buffer.clear()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        bot_process = subprocess.Popen(
            [sys.executable, "-u", str(BOT_DIR / "bot.py")],
            cwd=str(BOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        threading.Thread(target=_stream_bot_output, args=(bot_process,), daemon=True).start()
    # Start telegram bot if enabled
    _maybe_start_telegram()
    return jsonify({"ok": True})

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    global bot_process
    if bot_process and bot_process.poll() is None:
        bot_process.terminate()
        bot_process = None
    with state_lock:
        state["bot_running"] = False
    return jsonify({"ok": True})

@app.route("/api/bot/log", methods=["GET"])
def api_bot_log():
    """Return recent bot terminal output lines."""
    lines = list(bot_log_buffer)
    return jsonify({"lines": lines, "running": bot_process is not None and bot_process.poll() is None})

@app.route("/api/abort", methods=["POST"])
def api_abort():
    """Sell current position at market price."""
    pos_file = BOT_DIR / "current_position.json"
    if not pos_file.exists():
        return jsonify({"ok": False, "error": "No open position"})
    try:
        pos = json.load(open(pos_file))
        ticker    = pos["ticker"]
        side      = pos["side"]
        contracts = pos["contracts"]

        path = "/trade-api/v2/portfolio/orders"
        try:
            mpath = "/trade-api/v2/markets/" + ticker
            mr = requests.get(KALSHI_BASE + "/markets/" + ticker,
                            headers=sign_request("GET", mpath), timeout=5)
            mdata = mr.json().get("market", mr.json())
            sell_price = (float(mdata.get("yes_bid_dollars", 0.01)) if side == "yes"
                         else float(mdata.get("no_bid_dollars", 0.01)))
            sell_price = max(sell_price, 0.01)
        except Exception:
            sell_price = 0.01

        price_str = f"{sell_price:.4f}"
        key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        body = json.dumps({
            "ticker": ticker, "action": "sell", "side": side,
            "type": "limit", "count": contracts, key: price_str,
        })
        r = requests.post(
            KALSHI_BASE + "/portfolio/orders",
            headers=sign_request("POST", path), data=body
        )
        result = r.json()
        pos_file.unlink()
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/current_position")
def api_current_position():
    pos_file = BOT_DIR / "current_position.json"
    if not pos_file.exists():
        return jsonify(None)
    try:
        return jsonify(json.load(open(pos_file)))
    except Exception:
        return jsonify(None)

@app.route("/api/trades")
def api_trades():
    trades_folder = BOT_DIR / "trades"
    if not trades_folder.exists():
        return jsonify([])
    try:
        all_rows = []
        for f in sorted(trades_folder.glob("trades_*.csv")):
            try:
                with open(f, newline="") as fh:
                    reader = csv.DictReader(fh)
                    if not reader.fieldnames:
                        continue
                    for row in reader:
                        clean_row = {str(k): str(v) if v is not None else ""
                                    for k, v in row.items() if k is not None}
                        all_rows.append(clean_row)
            except Exception as fe:
                print(f"Trade file error {f}: {fe}")
                continue
        return jsonify(all_rows[-100:])
    except Exception as e:
        return jsonify([])

@app.route("/api/set_period", methods=["POST"])
def api_set_period():
    period = request.get_json().get("period", "day")
    with state_lock:
        state["pnl_period"] = period
        state["kalshi_stats_updated"] = 0  # force refresh
    return jsonify({"ok": True})

@app.route("/api/kalshi_stats")
def api_kalshi_stats():
    """Force-refresh Kalshi stats and return them."""
    with state_lock:
        period = state["pnl_period"]
    try:
        stats = kalshi_compute_stats(period)
        now = time.time()
        with state_lock:
            state["kalshi_pnl_dollars"]  = stats["pnl"]
            state["kalshi_pnl_pct"]      = stats["pnl_pct"]
            state["kalshi_win_rate"]     = stats["win_rate"]
            state["kalshi_wins"]         = stats["wins"]
            state["kalshi_losses"]       = stats["losses"]
            state["kalshi_total_cost"]   = stats["total_cost"]
            state["kalshi_stats_updated"] = now
        return jsonify({"ok": True, **stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Telegram bot integration ──────────────────────────
def _maybe_start_telegram():
    """Start Telegram bot thread if enabled in config."""
    global telegram_thread
    cfg_path = BOT_DIR / "bot_config.json"
    try:
        cfg = json.load(open(cfg_path)) if cfg_path.exists() else {}
        if cfg.get("telegram_enabled") and cfg.get("telegram_token"):
            if telegram_thread is None or not telegram_thread.is_alive():
                from telegram_bot import run_telegram_bot
                telegram_thread = threading.Thread(
                    target=run_telegram_bot,
                    args=(cfg["telegram_token"], cfg.get("telegram_allowed_users", []), state, state_lock),
                    daemon=True,
                )
                telegram_thread.start()
                print("Telegram bot started.")
    except Exception as e:
        print(f"Telegram start error: {e}")

# ── Startup ────────────────────────────────────────────
def open_browser():
    time.sleep(2)
    webbrowser.open("http://localhost:5050")

if __name__ == "__main__":
    print("=" * 50)
    print("BTC.KILLER DASHBOARD")
    print("http://localhost:5050")
    print("=" * 50)

    print("Starting BTC price feed...")
    start_feed_thread()

    # Load saved settings
    cfg_path = BOT_DIR / "bot_config.json"
    if cfg_path.exists():
        try:
            saved = json.load(open(cfg_path))
            with state_lock:
                state["settings"].update(saved)
        except Exception:
            pass

    # Force initial Kalshi stats load
    threading.Thread(target=lambda: (time.sleep(5), api_kalshi_stats()), daemon=True).start()

    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=open_browser, daemon=True).start()

    # Start Telegram if configured
    _maybe_start_telegram()

    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
