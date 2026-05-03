"""
COIN.KILLER — Multi-Coin Dashboard Backend
Supports BTC, ETH, SOL, XRP, DOGE, BNB, HYPE running simultaneously.
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
from collections import deque, defaultdict
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()

app     = Flask(__name__)
BOT_DIR = Path(__file__).parent

# Optional imports (graceful fallback if file not present)
try:
    import telegram_bot as _tgbot
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False

_last_tg_token = ""

def _maybe_restart_telegram(cfg: dict):
    """Start or restart the Telegram bot when config changes."""
    global _last_tg_token
    if not _TG_AVAILABLE:
        return
    token   = cfg.get("telegram_token", "")
    enabled = cfg.get("telegram_enabled", False)
    chat_id = cfg.get("telegram_chat_id", "")
    if enabled and token and token != _last_tg_token:
        _last_tg_token = token
        allowed = [chat_id] if chat_id else []
        print(f"[Telegram] Starting bot (token …{token[-6:]})")
        _tgbot.start(token, allowed)
    elif not enabled and _last_tg_token:
        _last_tg_token = ""
        _tgbot.stop()
        print("[Telegram] Bot stopped (disabled in config)")

API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
KALSHI_BASE      = "https://api.elections.kalshi.com/trade-api/v2"

with open(PRIVATE_KEY_PATH, "rb") as _f:
    PRIVATE_KEY = serialization.load_pem_private_key(_f.read(), password=None)

# ── Coin configuration ─────────────────────────────────────────────────────────
COINS = ["BTC"]

COIN_SERIES = {
    "BTC":  "KXBTC15M",
    "ETH":  "KXETH15M",
    "SOL":  "KXSOL15M",
    "XRP":  "KXXRP15M",
    "DOGE": "KXDOGE15M",
    "BNB":  "KXBNB15M",
    "HYPE": "KXHYPE15M",
}

COINBASE_IDS = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "XRP":  "XRP-USD",
    "DOGE": "DOGE-USD",
    "BNB":  "BNB-USD",
    "HYPE": "HYPE-USD",
}

# ── Per-coin live price state ─────────────────────────────────────────────────
live_prices    = {coin: None for coin in COINS}
price_histories = {coin: [] for coin in COINS}   # list of (ts, price)
prices_lock    = threading.Lock()

# ── Per-coin settings default ─────────────────────────────────────────────────
def _default_settings():
    return {
        "mode":                   "balanced",
        "daily_loss_limit":       50.0,
        "max_session_wager":      5.0,
        "wager_mode":             "dollar",
        "wager_pct":              10.0,
        "trigger_time":           5.0,
        "trigger_method":         "ev",
        "allow_early_buy":        True,
        "early_max_price":        0.75,
        "telegram_enabled":       False,
        "telegram_token":         "",
        "telegram_allowed_users": [],
        "min_ev_edge":            0.05,
        "momentum_gate":          False,
        "momentum_window":        30,
        "safety_margin":          0.0,
        "max_trades_session":     0,
        "take_profit_cents":      0.0,
        "kelly_enabled":          False,
        "kelly_fraction":         0.25,
        "wallet_floor":           0.0,
        "penny_enabled":          False,
        "penny_stop_mins":        3.0,
        "penny_wager":            1.0,
        "penny_wager_mode":       "dollar",
        "penny_flip":             True,
        "close_window":           1.0,
        "exit_at_cents":          97.0,
        "time_exit_mins":         3.0,
        "time_exit_enabled":      False,
    }

# ── Per-coin runtime state ────────────────────────────────────────────────────
def _make_coin_state():
    return {
        "market_ticker":          None,
        "market_close_time":      None,   # ISO timestamp — secs_remaining computed live from this
        "secs_remaining":         None,
        "yes_ask":                None,
        "no_ask":                 None,
        "target_price":           None,
        "target_dir":             None,
        "pnl_today":              0.0,
        "trades_today":           0,
        "wins_today":             0,
        "losses_today":           0,
        "avg_win":                0.0,
        "avg_loss":               0.0,
        "profit_factor":          0.0,
        "expected_val":           0.0,
        "current_position":       None,
        "pnl_period":             "day",
        "killed":                 False,
        "bot_running":            False,
        "bot_status":             "idle",
        "bot_watching_direction": None,
        "bot_watching_max_price": None,
        "signals":                None,
        "conviction":             None,
        "kalshi_pnl_dollars":     None,
        "kalshi_pnl_pct":         None,
        "kalshi_win_rate":        None,
        "kalshi_wins":            0,
        "kalshi_losses":          0,
        "kalshi_total_cost":      0.0,
        "kalshi_stats_updated":   0,
        "settings":               _default_settings(),
    }

coin_states      = {coin: _make_coin_state() for coin in COINS}
coin_locks       = {coin: threading.Lock()   for coin in COINS}
bot_processes    = {coin: None               for coin in COINS}
signal_processes = {coin: None               for coin in COINS}
bot_log_buffers  = {coin: deque(maxlen=200)  for coin in COINS}

# Shared balance (single Kalshi account)
balance_val   = {"balance": None, "session_start": None}
balance_lock  = threading.Lock()

# ── Global config (hard stop, telegram) ──────────────────────────────────────
GLOBAL_CONFIG_PATH  = BOT_DIR / "global_config.json"
SYSTEM_CONFIG_PATH  = BOT_DIR / "system_config.json"

def _default_global_config():
    return {
        "hard_stop_enabled":   False,
        "hard_stop_floor":     0.0,
        "daily_loss_limit":    50.0,
        "telegram_enabled":    False,
        "telegram_token":      "",
        "telegram_chat_id":    "",
        "inverse_bet_enabled": False,
    }

def _default_system_config():
    return {
        "kalshi_api_key_id":      os.getenv("KALSHI_API_KEY_ID", ""),
        "kalshi_private_key_path": os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        "llm_provider":   "ollama",
        "llm_model":      "llama3",
        "llm_api_key":    "",
        "llm_url":        "http://localhost:11434",
        "llm_perm_history":       True,
        "llm_perm_signals":       True,
        "llm_perm_settings_read": True,
        "llm_perm_settings_write": False,
        "llm_perm_websearch":     True,
    }

global_config       = _default_global_config()
global_config_lock  = threading.Lock()

# ── Ollama auto-start ─────────────────────────────────────────────────────────
_ollama_proc = None   # subprocess we launched (None if user's own instance)

def _ensure_ollama(base_url: str) -> str | None:
    """
    Make sure Ollama is listening at base_url.
    If not, spawn 'ollama serve' and wait up to 8s for it to come up.
    Returns None on success, or an error string.
    """
    global _ollama_proc
    health = base_url.rstrip("/") + "/api/tags"
    # Already running?
    try:
        if requests.get(health, timeout=2).status_code == 200:
            return None
    except Exception:
        pass
    # Try to start it
    try:
        if _ollama_proc is None or _ollama_proc.poll() is not None:
            print("[Ollama] Not detected — launching 'ollama serve'…")
            _ollama_proc = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        # Poll up to 8 seconds
        for _ in range(16):
            time.sleep(0.5)
            try:
                if requests.get(health, timeout=1).status_code == 200:
                    print("[Ollama] Ready.")
                    return None
            except Exception:
                pass
        return "Ollama started but not responding yet — try again in a few seconds"
    except FileNotFoundError:
        return "Ollama not installed. Download it at ollama.com"
    except Exception as e:
        return f"Could not start Ollama: {e}"
system_config       = _default_system_config()
system_config_lock  = threading.Lock()

# Load system config from disk
if SYSTEM_CONFIG_PATH.exists():
    try:
        with open(SYSTEM_CONFIG_PATH) as f:
            system_config.update(json.load(f))
    except Exception:
        pass

# ── Kalshi API helpers ────────────────────────────────────────────────────────
def sign_request(method, path):
    timestamp = str(int(time.time() * 1000))
    message   = f"{timestamp}{method.upper()}{path}".encode()
    sig = PRIVATE_KEY.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }

def kalshi_get_balance():
    path = "/trade-api/v2/portfolio/balance"
    r = requests.get(KALSHI_BASE + "/portfolio/balance",
                     headers=sign_request("GET", path), timeout=5)
    return r.json().get("balance", 0) / 100

def kalshi_get_open_positions(coin_prefix=None):
    """Return open positions, optionally filtered by coin series prefix."""
    try:
        path = "/trade-api/v2/portfolio/positions"
        r    = requests.get(KALSHI_BASE + "/portfolio/positions",
                            headers=sign_request("GET", path), timeout=5)
        all_pos = r.json().get("market_positions", [])
        result  = []
        for pos in all_pos:
            pos_fp = float(pos.get("position_fp", 0))
            if abs(pos_fp) < 1:
                continue
            ticker = pos.get("ticker", "")
            if coin_prefix and not ticker.startswith(coin_prefix):
                continue
            side      = "no" if pos_fp < 0 else "yes"
            contracts = int(abs(pos_fp))
            exposure  = float(pos.get("market_exposure_dollars", 0))
            entry_px  = round(exposure / contracts, 4) if contracts > 0 else 0
            result.append({
                "ticker":    ticker,
                "side":      side,
                "contracts": contracts,
                "entry":     entry_px,
                "exposure":  exposure,
            })
        return result
    except Exception as e:
        print(f"Position fetch error: {e}")
        return []

def kalshi_get_market_for_coin(coin):
    """Fetch the soonest-closing open market for a given coin."""
    series = COIN_SERIES.get(coin, f"KX{coin}15M")
    path   = "/trade-api/v2/markets"
    params = {"series_ticker": series, "status": "open", "limit": 5}
    r      = requests.get(KALSHI_BASE + "/markets",
                          headers=sign_request("GET", path), params=params, timeout=5)
    markets   = r.json().get("markets", [])
    now_utc   = datetime.now(timezone.utc)
    best, best_secs = None, float("inf")
    for m in markets:
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        diff  = (close - now_utc).total_seconds()
        if 0 < diff < best_secs:
            best_secs = diff
            best      = m
    return best, best_secs

def kalshi_compute_stats(coin, period="day"):
    """Compute P&L stats from Kalshi fills for a specific coin."""
    series   = COIN_SERIES.get(coin, f"KX{coin}15M")
    now_utc  = datetime.now(timezone.utc)

    if period == "day":
        min_date = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        min_date = now_utc - timedelta(days=7)
    elif period == "month":
        min_date = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        min_date = now_utc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        min_date = None

    try:
        path = "/trade-api/v2/portfolio/fills"
        r    = requests.get(KALSHI_BASE + "/portfolio/fills",
                            headers=sign_request("GET", path),
                            params={"limit": 200}, timeout=10)
        all_fills = r.json().get("fills", [])
    except Exception as e:
        print(f"Fills fetch error ({coin}): {e}")
        return {"pnl": 0, "pnl_pct": 0, "win_rate": 0, "wins": 0, "losses": 0,
                "total_trades": 0, "total_cost": 0}

    fills = []
    for f in all_fills:
        if not f.get("ticker", "").startswith(series):
            continue
        ts = f.get("created_time", "")
        if min_date and ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt < min_date:
                    continue
            except Exception:
                pass
        fills.append(f)

    by_ticker = defaultdict(list)
    for f in fills:
        by_ticker[f["ticker"]].append(f)

    total_pnl, total_cost = 0.0, 0.0
    n_wins, n_losses = 0, 0

    for ticker, tfills in by_ticker.items():
        buy_cost      = 0.0
        sell_rev      = 0.0
        net_contracts = 0
        our_side      = None

        for f in tfills:
            side   = f.get("side", "yes")
            action = f.get("action", "buy")
            count  = int(f.get("count", 0))
            price  = float(f.get("yes_price_dollars") or f.get("no_price_dollars")
                           or f.get("yes_price", 0) or f.get("no_price", 0) or 0)
            if price > 1.0:
                price /= 100.0
            our_side = side
            if action == "buy":
                buy_cost      += count * price
                net_contracts += count
            elif action == "sell":
                sell_rev      += count * price
                net_contracts -= count

        if net_contracts > 0 and our_side:
            try:
                mpath = f"/trade-api/v2/markets/{ticker}"
                mr    = requests.get(KALSHI_BASE + f"/markets/{ticker}",
                                     headers=sign_request("GET", mpath), timeout=5)
                mkt    = mr.json().get("market", {})
                result = mkt.get("result", "")
                if result and result.lower() == our_side.lower():
                    sell_rev += net_contracts * 1.0
            except Exception:
                pass

        pnl        = sell_rev - buy_cost
        total_pnl  += pnl
        total_cost += buy_cost
        if pnl > 0:
            n_wins += 1
        elif buy_cost > 0:
            n_losses += 1

    total_trades = n_wins + n_losses
    win_rate     = (n_wins / total_trades * 100) if total_trades > 0 else None
    pnl_pct      = (total_pnl / total_cost * 100) if total_cost > 0 else None

    return {
        "pnl":          total_pnl,
        "pnl_pct":      pnl_pct,
        "win_rate":     win_rate,
        "wins":         n_wins,
        "losses":       n_losses,
        "total_trades": total_trades,
        "total_cost":   total_cost,
    }

# ── Bot subprocess management ─────────────────────────────────────────────────
def _stream_bot_output(proc, coin):
    try:
        for line in iter(proc.stdout.readline, b''):
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                bot_log_buffers[coin].append(text)
    except Exception:
        pass

def _start_bot(coin):
    global bot_processes
    bot_log_buffers[coin].clear()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["BOT_COIN"]         = coin
    proc = subprocess.Popen(
        [sys.executable, "-u", str(BOT_DIR / "bot.py"), coin],
        cwd=str(BOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    bot_processes[coin] = proc
    threading.Thread(target=_stream_bot_output, args=(proc, coin), daemon=True).start()
    with coin_locks[coin]:
        coin_states[coin]["bot_running"] = True
        coin_states[coin]["killed"]      = False

def _stop_bot(coin):
    proc = bot_processes.get(coin)
    if proc and proc.poll() is None:
        proc.terminate()
    bot_processes[coin] = None
    with coin_locks[coin]:
        coin_states[coin]["bot_running"] = False

# ── Background loops ──────────────────────────────────────────────────────────

def coinbase_ws_loop():
    """Single WebSocket connection subscribing to all coin tickers."""
    try:
        import websocket as _websocket
    except ImportError:
        print("websocket-client not installed — pip install websocket-client")
        return

    def _store_price(coin, price):
        if coin not in COINS or price <= 0:
            return
        ts = time.time()
        with prices_lock:
            live_prices[coin] = price
            hist = price_histories[coin]
            hist.append((ts, price))
            if len(hist) > 300:
                price_histories[coin] = hist[-300:]

    def on_message(ws, message):
        try:
            data = json.loads(message)
            # Coinbase Advanced Trade WS wraps updates in an events array
            for event in data.get("events", []):
                for tkr in event.get("tickers", []):
                    coin = tkr.get("product_id", "").replace("-USD", "")
                    try:
                        _store_price(coin, float(tkr.get("price", 0)))
                    except (ValueError, TypeError):
                        pass
            # Also handle legacy flat ticker messages
            if data.get("type") == "ticker":
                coin = data.get("product_id", "").replace("-USD", "")
                try:
                    _store_price(coin, float(data.get("price", 0)))
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    def on_open(ws):
        # Advanced Trade WS uses "channel" (singular), not "channels"
        ws.send(json.dumps({
            "type":        "subscribe",
            "product_ids": list(COINBASE_IDS.values()),
            "channel":     "ticker",
        }))
        print("[WS] Subscribed to Coinbase Advanced Trade ticker.")

    def on_error(ws, error):
        print(f"[WS] Coinbase error: {error}")

    while True:
        try:
            ws_app = _websocket.WebSocketApp(
                "wss://advanced-trade-ws.coinbase.com/",
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print(f"[WS] Coinbase WS crashed: {e}")
        time.sleep(5)


def coinbase_rest_price_loop():
    """REST fallback — polls Coinbase public spot price for any coin whose WS
    price is stale (> 25 s old).  No auth needed."""
    last_ts = {coin: 0.0 for coin in COINS}
    while True:
        for coin, product_id in COINBASE_IDS.items():
            with prices_lock:
                age = time.time() - last_ts.get(coin, 0)
                has_price = live_prices.get(coin) is not None
            if has_price and age < 25:
                continue  # WS is working fine for this coin
            try:
                r = requests.get(
                    f"https://api.coinbase.com/v2/prices/{product_id}/spot",
                    timeout=4,
                )
                price = float(r.json().get("data", {}).get("amount", 0))
                if price > 0:
                    ts = time.time()
                    with prices_lock:
                        live_prices[coin] = price
                        last_ts[coin] = ts
                        hist = price_histories[coin]
                        hist.append((ts, price))
                        if len(hist) > 300:
                            price_histories[coin] = hist[-300:]
            except Exception:
                pass
            time.sleep(0.4)
        time.sleep(2)


def market_price_loop():
    """Poll Kalshi market data for all coins, one coin every 0.4s → full cycle ~3s."""
    coin_idx = 0
    while True:
        coin = COINS[coin_idx % len(COINS)]
        try:
            best, secs = kalshi_get_market_for_coin(coin)
            if best:
                strike      = best.get("floor_strike") or best.get("cap_strike")
                strike_type = best.get("strike_type", "")
                tdir        = ("above" if "greater" in strike_type
                               else "below" if "less" in strike_type
                               else strike_type)
                with coin_locks[coin]:
                    coin_states[coin]["market_ticker"]   = best["ticker"]
                    coin_states[coin]["market_close_time"] = best.get("close_time")
                    coin_states[coin]["secs_remaining"]  = secs
                    coin_states[coin]["yes_ask"]         = float(best.get("yes_ask_dollars", 0))
                    coin_states[coin]["no_ask"]          = float(best.get("no_ask_dollars",  0))
                    coin_states[coin]["target_price"]    = strike
                    coin_states[coin]["target_dir"]      = tdir
        except Exception:
            pass
        coin_idx += 1
        time.sleep(0.4)


def balance_loop():
    """Refresh Kalshi balance every 30s. Enforces hard-stop floor."""
    while True:
        try:
            bal = kalshi_get_balance()
            with balance_lock:
                balance_val["balance"] = bal
                if balance_val["session_start"] is None:
                    balance_val["session_start"] = bal

            # ── Hard stop floor check ────────────────────────────────────────
            with global_config_lock:
                hs_enabled = global_config.get("hard_stop_enabled", False)
                hs_floor   = float(global_config.get("hard_stop_floor", 0.0))
            if hs_enabled and hs_floor > 0 and bal <= hs_floor:
                print(f"[HARD STOP] Balance ${bal:.2f} <= floor ${hs_floor:.2f} — stopping ALL bots")
                for coin in COINS:
                    proc = bot_processes.get(coin)
                    if proc and proc.poll() is None:
                        _stop_bot(coin)
                        bot_log_buffers[coin].append(
                            f"[HARD STOP] Killed — balance ${bal:.2f} <= floor ${hs_floor:.2f}"
                        )
        except Exception:
            pass
        time.sleep(5)


def status_reader_loop():
    """
    Every 1s: read per-coin bot_status_*.json, current_position_*.json, CSV trades.
    This is all local disk reads — no API calls.
    """
    last_csv = {coin: 0.0 for coin in COINS}
    last_pos = {coin: 0.0 for coin in COINS}

    while True:
        now = time.time()
        for coin in COINS:
            # ── Bot status + signals file ────────────────────────────────────
            bot_has_fresh_signals = False
            try:
                sf = BOT_DIR / f"bot_status_{coin}.json"
                if sf.exists():
                    bs = json.load(open(sf))
                    if now - bs.get("updated_at", 0) < 30:
                        with coin_locks[coin]:
                            coin_states[coin]["bot_status"]             = bs.get("status", "idle")
                            coin_states[coin]["bot_watching_direction"] = bs.get("direction")
                            coin_states[coin]["bot_watching_max_price"] = bs.get("max_price")
                            if bs.get("signals"):
                                coin_states[coin]["signals"] = bs["signals"]
                                bot_has_fresh_signals = True
                            if bs.get("conviction") is not None:
                                coin_states[coin]["conviction"] = bs["conviction"]
            except Exception:
                pass

            # ── Signal monitor state file (used when bot is off) ─────────────
            if not bot_has_fresh_signals:
                try:
                    ms = BOT_DIR / f"signal_state_{coin}.json"
                    if ms.exists():
                        md = json.load(open(ms))
                        if now - md.get("updated_at", 0) < 60:
                            with coin_locks[coin]:
                                if md.get("signals"):
                                    coin_states[coin]["signals"]    = md["signals"]
                                if md.get("conviction") is not None:
                                    coin_states[coin]["conviction"] = md["conviction"]
                except Exception:
                    pass

            # ── Sync bot_running with actual process state ───────────────────
            proc = bot_processes.get(coin)
            running = proc is not None and proc.poll() is None
            with coin_locks[coin]:
                coin_states[coin]["bot_running"] = running

            # ── Current position (every 5s — local file only, no API call) ──────
            if now - last_pos[coin] > 5:
                last_pos[coin] = now
                current_pos    = None
                pf             = BOT_DIR / f"current_position_{coin}.json"
                # Only show position if bot is actually in "traded" state —
                # avoids showing stale files from crashed/stopped bots
                with coin_locks[coin]:
                    bot_st = coin_states[coin].get("bot_status", "idle")
                if pf.exists() and bot_st == "traded":
                    try:
                        pd = json.load(open(pf))
                        current_pos = {
                            "ticker":    pd.get("ticker", ""),
                            "side":      pd.get("side", ""),
                            "price":     float(pd.get("entry", 0)),
                            "contracts": pd.get("contracts", 0),
                            "cost":      round(pd.get("cost", 0), 2),
                        }
                    except Exception:
                        pass
                elif pf.exists() and bot_st not in ("traded",):
                    # Stale file — bot is not in a traded state, remove it
                    try:
                        pf.unlink()
                    except Exception:
                        pass

                with coin_locks[coin]:
                    coin_states[coin]["current_position"] = current_pos

            # ── CSV trade stats (every 5s) ────────────────────────────────────
            if now - last_csv[coin] > 5:
                last_csv[coin] = now
                try:
                    trades_folder = BOT_DIR / "trades" / coin
                    trades_folder.mkdir(parents=True, exist_ok=True)

                    def safe_pnl(r):
                        val = r.get("pnl", "")
                        if not val or val in ("pending", "unfilled", "expired"):
                            return None
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return None

                    with coin_locks[coin]:
                        period = coin_states[coin]["pnl_period"]

                    now_dt = datetime.now()
                    rows   = []
                    for fpath in sorted(trades_folder.glob("trades_*.csv")):
                        try:
                            with open(fpath) as fh:
                                for r in csv.DictReader(fh):
                                    ts = r.get("timestamp", "")
                                    try:
                                        dt = datetime.fromisoformat(ts)
                                        if period == "day"   and dt.date() != now_dt.date(): continue
                                        if period == "week"  and (now_dt.date()-dt.date()).days > 7: continue
                                        if period == "month" and (now_dt.year != dt.year or now_dt.month != dt.month): continue
                                        if period == "year"  and now_dt.year != dt.year: continue
                                        rows.append(r)
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                    today_rows = []
                    for fpath in sorted(trades_folder.glob("trades_*.csv")):
                        try:
                            with open(fpath) as fh:
                                for r in csv.DictReader(fh):
                                    ts = r.get("timestamp", "")
                                    try:
                                        dt = datetime.fromisoformat(ts)
                                        if dt.date() == now_dt.date():
                                            today_rows.append(r)
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                    settled  = [(r, safe_pnl(r)) for r in rows]
                    settled  = [(r, p) for r, p in settled if p is not None]
                    pnl      = sum(p for _, p in settled)
                    wins     = [p for _, p in settled if p > 0]
                    losses   = [p for _, p in settled if p <= 0]
                    nw, nl   = len(wins), len(losses)
                    avg_win  = sum(wins)   / nw if nw   else 0.0
                    avg_loss = sum(losses) / nl if nl   else 0.0
                    tot_won  = sum(wins)
                    tot_lost = abs(sum(losses))
                    pf_val   = tot_won / tot_lost if tot_lost > 0 else 0.0
                    ns       = nw + nl
                    ev       = pnl / ns if ns else 0.0

                    with coin_locks[coin]:
                        coin_states[coin]["trades_today"]  = len(today_rows)
                        coin_states[coin]["pnl_today"]     = pnl
                        coin_states[coin]["wins_today"]    = nw
                        coin_states[coin]["losses_today"]  = nl
                        coin_states[coin]["avg_win"]       = avg_win
                        coin_states[coin]["avg_loss"]      = avg_loss
                        coin_states[coin]["profit_factor"] = pf_val
                        coin_states[coin]["expected_val"]  = ev

                except Exception as e:
                    print(f"CSV trades error ({coin}): {e}")

        time.sleep(1)


def kalshi_stats_loop():
    """Refresh Kalshi P&L stats for all coins every 120s."""
    time.sleep(15)  # give everything else time to start
    while True:
        for coin in COINS:
            try:
                with coin_locks[coin]:
                    period = coin_states[coin]["pnl_period"]
                stats = kalshi_compute_stats(coin, period)
                now   = time.time()
                with coin_locks[coin]:
                    coin_states[coin]["kalshi_pnl_dollars"]  = stats["pnl"]
                    coin_states[coin]["kalshi_pnl_pct"]      = stats["pnl_pct"]
                    coin_states[coin]["kalshi_win_rate"]     = stats["win_rate"]
                    coin_states[coin]["kalshi_wins"]         = stats["wins"]
                    coin_states[coin]["kalshi_losses"]       = stats["losses"]
                    coin_states[coin]["kalshi_total_cost"]   = stats["total_cost"]
                    coin_states[coin]["kalshi_stats_updated"] = now
            except Exception as e:
                print(f"Kalshi stats error ({coin}): {e}")
            time.sleep(2)  # small gap between coins to avoid rate limit burst
        time.sleep(30)

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    from flask import make_response
    resp = make_response(send_from_directory(BOT_DIR, "dashboard.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


def _live_secs(cs):
    """Compute seconds remaining from stored close_time — always accurate to the second."""
    ct = cs.get("market_close_time")
    if ct:
        try:
            close = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            return max(0, (close - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            pass
    return cs.get("secs_remaining")   # fallback to cached value


@app.route("/api/state")
def api_state():
    coin = request.args.get("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"error": "unknown coin"}), 400
    with coin_locks[coin]:
        out = dict(coin_states[coin])
    with prices_lock:
        out["coin_price"]    = live_prices[coin]
        out["price_history"] = [
            {"time": t, "price": p}
            for t, p in price_histories[coin][-200:]
        ]
    with balance_lock:
        out["balance"]               = balance_val["balance"]
        out["session_start_balance"] = balance_val["session_start"]
    out["coin"]           = coin
    out["secs_remaining"] = _live_secs(out)   # always fresh, never stale
    return jsonify(out)


@app.route("/api/claude/snapshot")
def api_claude_snapshot():
    """
    Rich snapshot endpoint optimized for Claude to read.
    Returns all coin states, signals, positions, settings, recent trades,
    global config, and balance in a single JSON payload.
    """
    import csv as _csv
    from datetime import date as _date

    gcfg = {}
    if GLOBAL_CONFIG_PATH.exists():
        try:
            gcfg = json.load(open(GLOBAL_CONFIG_PATH))
        except Exception:
            pass

    coins_out = []
    for coin in COINS:
        with coin_locks[coin]:
            cs = dict(coin_states[coin])

        cfg_path = BOT_DIR / f"bot_config_{coin}.json"
        cfg = {}
        if cfg_path.exists():
            try:
                cfg = json.load(open(cfg_path))
            except Exception:
                pass

        # Today's trades
        today_csv = BOT_DIR / "trades" / coin / f"trades_{_date.today().isoformat()}.csv"
        trades = []
        wins = losses = 0
        total_pnl = 0.0
        if today_csv.exists():
            try:
                with open(today_csv, newline="") as fh:
                    for row in _csv.DictReader(fh):
                        pnl_raw = row.get("pnl", "pending")
                        try:
                            pnl = float(pnl_raw)
                            total_pnl += pnl
                            if pnl > 0:   wins   += 1
                            elif pnl < 0: losses += 1
                            trades.append({
                                "time":      row.get("timestamp", "")[:19],
                                "side":      row.get("side"),
                                "price":     row.get("price"),
                                "contracts": row.get("contracts"),
                                "pnl":       pnl,
                                "note":      row.get("note"),
                            })
                        except (ValueError, TypeError):
                            trades.append({"time": row.get("timestamp","")[:19],
                                           "side": row.get("side"), "pnl": "pending"})
            except Exception:
                pass

        coins_out.append({
            "coin":           coin,
            "bot_running":    cs.get("bot_running", False),
            "bot_status":     cs.get("bot_status", "idle"),
            "signals":        cs.get("signals"),
            "conviction":     cs.get("conviction"),
            "current_position": cs.get("current_position"),
            "pnl":            cs.get("pnl", 0.0),
            "price":          cs.get("price"),
            "wins_today":     wins,
            "losses_today":   losses,
            "pnl_today":      round(total_pnl, 2),
            "recent_trades":  trades[-10:],  # last 10
            "settings": {
                "mode":               cfg.get("mode"),
                "wager_mode":         cfg.get("wager_mode"),
                "wager":              cfg.get("wager"),
                "max_wager":          cfg.get("max_wager"),
                "kelly_fraction":     cfg.get("kelly_fraction"),
                "kelly_hard_cap":     cfg.get("kelly_hard_cap"),
                "kelly_bankroll_pct_cap": cfg.get("kelly_bankroll_pct_cap"),
                "buy_window_open":    cfg.get("buy_window_open"),
                "min_signals":        cfg.get("min_signals"),
                "min_edge_threshold": cfg.get("min_edge_threshold"),
                "min_volume":         cfg.get("min_volume"),
                "max_spread_cents":   cfg.get("max_spread_cents"),
                "inverse_bet_enabled": cfg.get("inverse_bet_enabled"),
                "enabled":            cfg.get("enabled", True),
            },
        })

    bal = None
    with balance_lock:
        bal = balance_val.get("balance")

    return jsonify({
        "timestamp":     time.time(),
        "balance":       bal,
        "global_config": gcfg,
        "coins":         coins_out,
    })


@app.route("/api/claude/chat", methods=["POST"])
def api_claude_chat():
    """
    Chat endpoint wired directly to Claude (claude-sonnet-4-6).
    Auto-loads full live bot context so Claude always knows what's happening.
    Requires an Anthropic API key stored in system_config['llm_api_key'].
    """
    import csv as _csv
    from datetime import date as _date

    data     = request.get_json() or {}
    messages = data.get("messages", [])

    with system_config_lock:
        api_key = system_config.get("llm_api_key", "")

    if not api_key:
        return jsonify({"reply": "⚠ No Anthropic API key set. Go to Settings → AI and paste your key.", "ok": False})

    # ── Build live context ───────────────────────────────────────────────────
    try:
        with balance_lock:
            bal = balance_val.get("balance")
        bal_str = f"${bal:.2f}" if bal is not None else "unknown"

        gcfg = {}
        if GLOBAL_CONFIG_PATH.exists():
            try: gcfg = json.load(open(GLOBAL_CONFIG_PATH))
            except: pass

        running = [c for c in COINS if coin_states[c].get("bot_running")]
        coin_lines = []
        for coin in COINS:
            cs   = coin_states[coin]
            pnl  = cs.get("pnl_today",    0) or 0
            wins = cs.get("wins_today",   0) or 0
            loss = cs.get("losses_today", 0) or 0
            stat = cs.get("bot_status",  "idle")
            conv = cs.get("conviction")
            sigs = cs.get("signals") or {}
            sig_summary = " ".join(
                f"{k[0].upper()}:{v[0].upper()}"
                for k, v in sigs.items()
                if k not in ("yes_votes","no_votes","conviction_streak","conviction_dir")
            )
            conv_str = f"conv={conv*100:.0f}%" if conv else ""
            coin_lines.append(
                f"  {coin}: {stat}  P&L${pnl:+.2f}  W{wins}/L{loss}  {conv_str}  [{sig_summary}]"
            )

        # Today's trade summary per coin
        trade_lines = []
        for coin in COINS:
            today_csv = BOT_DIR / "trades" / coin / f"trades_{_date.today().isoformat()}.csv"
            if today_csv.exists():
                try:
                    rows = list(_csv.DictReader(open(today_csv, newline="")))
                    last = rows[-3:] if rows else []
                    for r in last:
                        pnl_raw = r.get("pnl","pending")
                        try:
                            pnl_val = float(pnl_raw)
                            pnl_str = f"${pnl_val:+.2f}"
                        except: pnl_str = "pending"
                        trade_lines.append(
                            f"  {coin} {r.get('timestamp','')[:16]} {r.get('side','?')}"
                            f" ×{r.get('contracts','?')} @{r.get('price','?')} → {pnl_str} [{r.get('note','')}]"
                        )
                except: pass

        live_context = f"""
--- LIVE BOT STATE ({datetime.now().strftime('%H:%M:%S')}) ---
Balance: {bal_str}
Inverse mode: {"ON" if gcfg.get("inverse_bet_enabled") else "OFF"}
Running bots: {", ".join(running) if running else "none"}
Hard stop floor: ${gcfg.get("hard_stop_floor","?")}  Daily loss limit: ${gcfg.get("daily_loss_limit","?")}

Coin status (status | P&L | W/L | conviction | signals):
{chr(10).join(coin_lines)}

Recent trades (last 3 per active coin):
{chr(10).join(trade_lines) if trade_lines else "  no trades today"}
"""
    except Exception as e:
        live_context = f"\n[Context error: {e}]"

    system_prompt = (
        "You are Claude, co-pilot of Jon's Kalshi 15-minute prediction-market trading bot. "
        "You have full access to all bot data and controls. Be concise and direct — Jon wants "
        "fast actionable answers. Here is the live system state:\n" + live_context
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": messages,
            },
            timeout=30,
        )
        d     = r.json()
        reply = (d.get("content") or [{}])[0].get("text") or d.get("error", {}).get("message", "No response")
        return jsonify({"reply": reply, "ok": True})
    except Exception as e:
        return jsonify({"reply": f"⚠ Claude API error: {e}", "ok": False})


@app.route("/api/claude/status")
def api_claude_status():
    """Returns MCP installation status and live signal snapshot for the panel."""
    import csv as _csv
    from datetime import date as _date

    # Check if MCP server file exists
    mcp_exists = (BOT_DIR / "kalshi_mcp_server.py").exists()

    # Check if Claude Desktop config has our server registered
    claude_cfg_path = os.path.expanduser(
        "~/Library/Application Support/Claude/claude_desktop_config.json"
    )
    mcp_registered = False
    try:
        with open(claude_cfg_path) as f:
            cfg = json.load(f)
        mcp_registered = "kalshi-bot" in cfg.get("mcpServers", {})
    except Exception:
        pass

    # Build compact signal snapshot for the panel
    coin_snap = []
    for coin in COINS:
        with coin_locks[coin]:
            cs = dict(coin_states[coin])
        sigs = cs.get("signals") or {}
        conv = cs.get("conviction")
        running = cs.get("bot_running", False)
        sig_vals = {k: v for k, v in sigs.items()
                    if k not in ("yes_votes","no_votes","conviction_streak","conviction_dir")}
        bull = sum(1 for v in sig_vals.values() if v == "bull")
        bear = sum(1 for v in sig_vals.values() if v == "bear")
        coin_snap.append({
            "coin":       coin,
            "running":    running,
            "signals":    sig_vals,
            "conviction": round(conv, 3) if conv else None,
            "bull":       bull,
            "bear":       bear,
        })

    # Today's total P&L across all coins
    total_pnl = 0.0
    for coin in COINS:
        today_csv = BOT_DIR / "trades" / coin / f"trades_{_date.today().isoformat()}.csv"
        if today_csv.exists():
            try:
                with open(today_csv, newline="") as fh:
                    for row in _csv.DictReader(fh):
                        try: total_pnl += float(row.get("pnl", 0))
                        except: pass
            except: pass

    with balance_lock:
        bal = balance_val.get("balance")

    gcfg = {}
    if GLOBAL_CONFIG_PATH.exists():
        try: gcfg = json.load(open(GLOBAL_CONFIG_PATH))
        except: pass

    return jsonify({
        "mcp_exists":     mcp_exists,
        "mcp_registered": mcp_registered,
        "ready":          mcp_registered,
        "coins":          coin_snap,
        "balance":        bal,
        "pnl_today":      round(total_pnl, 2),
        "inverse":        gcfg.get("inverse_bet_enabled", False),
        "running_count":  sum(1 for c in COINS if coin_states[c].get("bot_running")),
    })


@app.route("/api/claude/open", methods=["POST"])
def api_claude_open():
    """Build a live-context prompt and open Claude Desktop app."""
    import csv as _csv
    from datetime import date as _date

    data        = request.get_json() or {}
    prompt_type = data.get("type", "snapshot")  # snapshot | analyze | advise

    # Build live context
    try:
        with balance_lock:
            bal = balance_val.get("balance")
        bal_str = f"${bal:.2f}" if bal is not None else "unknown"

        gcfg = {}
        if GLOBAL_CONFIG_PATH.exists():
            try: gcfg = json.load(open(GLOBAL_CONFIG_PATH))
            except: pass

        running = [c for c in COINS if coin_states[c].get("bot_running")]
        coin_lines = []
        for coin in COINS:
            cs   = coin_states[coin]
            sigs = cs.get("signals") or {}
            conv = cs.get("conviction")
            pnl  = cs.get("pnl_today", 0) or 0
            wins = cs.get("wins_today", 0) or 0
            loss = cs.get("losses_today", 0) or 0
            sig_str = " ".join(
                f"{k[0].upper()}:{'🟢' if v=='bull' else '🔴' if v=='bear' else '⚪'}"
                for k, v in sigs.items()
                if k not in ("yes_votes","no_votes","conviction_streak","conviction_dir")
            )
            conv_str = f"conv={conv*100:.0f}%" if conv else "conv=--"
            coin_lines.append(f"  {coin}: {conv_str}  {sig_str}  P&L${pnl:+.2f} W{wins}/L{loss}")

        context = (
            f"[Live bot state — {datetime.now().strftime('%H:%M:%S')}]\n"
            f"Balance: {bal_str}  |  Inverse mode: {'ON' if gcfg.get('inverse_bet_enabled') else 'OFF'}\n"
            f"Running: {', '.join(running) if running else 'none'}\n"
            + "\n".join(coin_lines)
        )
    except Exception as e:
        context = f"[Context unavailable: {e}]"

    prompts = {
        "snapshot": f"{context}\n\nShow me the full bot snapshot and tell me which coins have the strongest signals right now.",
        "analyze":  f"{context}\n\nAnalyze my recent performance across all coins. What patterns do you see and what should I change?",
        "advise":   f"{context}\n\nGive me a full briefing — what's happening right now, which coins should I run, and what (if anything) should I adjust?",
        "signals":  f"{context}\n\nBreak down the current signals for each coin in detail. Flag anything unusual.",
    }
    prompt = prompts.get(prompt_type, prompts["snapshot"])

    # Open Claude Desktop app
    opened = False
    try:
        subprocess.Popen(["open", "-a", "Claude"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opened = True
    except Exception:
        pass

    return jsonify({"prompt": prompt, "opened": opened})


@app.route("/api/home")
def api_home():
    """Aggregate stats for the Home tab."""
    # Non-blocking snapshot — read directly without waiting on locks.
    # CPython dict reads are safe under the GIL; worst case we see 1 stale value.
    prices_snap = dict(live_prices)

    coin_summary = []
    total_pnl    = 0.0
    total_wins   = 0
    total_losses = 0

    for coin in COINS:
        cs = dict(coin_states[coin])  # lockless snapshot
        running = (bot_processes.get(coin) is not None and
                   bot_processes[coin].poll() is None)

        # Use CSV stats as the authoritative source for the home display.
        # CSV is always current (updated every 5s) and never returns false zeros.
        # Kalshi API stats can be 0 due to API errors and should not override good CSV data.
        wins   = int(cs.get("wins_today",   0) or 0)
        losses = int(cs.get("losses_today", 0) or 0)
        pnl    = float(cs.get("pnl_today",  0.0) or 0.0)
        total  = wins + losses
        wr     = round(wins / total * 100, 1) if total > 0 else None

        total_pnl    += pnl
        total_wins   += wins
        total_losses += losses
        coin_summary.append({
            "coin":             coin,
            "running":          running,
            "price":            prices_snap.get(coin),
            "pnl":              round(pnl, 2),
            "wins":             wins,
            "losses":           losses,
            "win_rate":         wr,
            "trades_today":     cs.get("trades_today", 0),
            "yes_ask":          cs.get("yes_ask"),
            "no_ask":           cs.get("no_ask"),
            "secs_remaining":   _live_secs(cs),
            "bot_status":       cs.get("bot_status", "idle"),
            "current_position": cs.get("current_position"),
            "signals":          cs.get("signals"),
            "conviction":       cs.get("conviction"),
        })

    total_trades = total_wins + total_losses
    agg_win_rate = (total_wins / total_trades * 100) if total_trades > 0 else None

    with balance_lock:
        bal         = balance_val["balance"]
        sess_start  = balance_val["session_start"]
    sess_pnl = round((bal - sess_start), 2) if bal and sess_start else None

    return jsonify({
        "coins":       coin_summary,
        "total_pnl":   round(total_pnl, 2),
        "total_wins":  total_wins,
        "total_losses": total_losses,
        "win_rate":    agg_win_rate,
        "balance":     bal,
        "session_pnl": sess_pnl,
    })


@app.route("/api/history")
def api_history():
    """Return daily P&L series per coin for the home-screen chart."""
    period = request.args.get("period", "day")
    now_dt = datetime.now()
    result = {}

    for coin in COINS:
        trades_folder = BOT_DIR / "trades" / coin
        if not trades_folder.exists():
            result[coin] = []
            continue

        # Build a time-bucketed P&L series
        buckets = defaultdict(float)
        for fpath in sorted(trades_folder.glob("trades_*.csv")):
            try:
                with open(fpath) as fh:
                    for r in csv.DictReader(fh):
                        ts_str = r.get("timestamp", "")
                        pnl_str = r.get("pnl", "")
                        if not pnl_str or pnl_str in ("pending", "unfilled", "expired"):
                            continue
                        try:
                            dt  = datetime.fromisoformat(ts_str)
                            pnl = float(pnl_str)
                            if period == "day"   and dt.date() != now_dt.date(): continue
                            if period == "week"  and (now_dt.date()-dt.date()).days > 7: continue
                            if period == "month" and (now_dt.year!=dt.year or now_dt.month!=dt.month): continue
                            if period == "year"  and now_dt.year != dt.year: continue
                            # Bucket by hour for day, by day for longer periods
                            if period == "day":
                                key = dt.strftime("%H:00")
                            else:
                                key = dt.strftime("%Y-%m-%d")
                            buckets[key] += pnl
                        except Exception:
                            pass
            except Exception:
                pass

        # Convert to sorted list
        series = [{"time": k, "pnl": round(v, 2)}
                  for k, v in sorted(buckets.items())]
        result[coin] = series

    return jsonify(result)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    coin = request.args.get("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"error": "unknown coin"}), 400

    if request.method == "GET":
        with coin_locks[coin]:
            return jsonify(dict(coin_states[coin]["settings"]))

    settings = request.get_json() or {}
    with coin_locks[coin]:
        coin_states[coin]["settings"].update(settings)
    cfg_path = BOT_DIR / f"bot_config_{coin}.json"
    existing = {}
    if cfg_path.exists():
        try:
            existing = json.load(open(cfg_path))
        except Exception:
            pass
    with coin_locks[coin]:
        existing.update(coin_states[coin]["settings"])
    with open(cfg_path, "w") as fh:
        json.dump(existing, fh, indent=2)
    return jsonify({"ok": True})


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    body = request.get_json() or {}
    coin = body.pop("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"error": "unknown coin"}), 400

    # Save settings if provided
    if body:
        with coin_locks[coin]:
            coin_states[coin]["settings"].update(body)
        cfg_path = BOT_DIR / f"bot_config_{coin}.json"
        existing = {}
        if cfg_path.exists():
            try:
                existing = json.load(open(cfg_path))
            except Exception:
                pass
        with coin_locks[coin]:
            existing.update(coin_states[coin]["settings"])
        with open(cfg_path, "w") as fh:
            json.dump(existing, fh, indent=2)

    proc = bot_processes.get(coin)
    if proc is None or proc.poll() is not None:
        _start_bot(coin)

    return jsonify({"ok": True, "coin": coin})


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    body = request.get_json() or {}
    coin = body.get("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"error": "unknown coin"}), 400
    _stop_bot(coin)
    return jsonify({"ok": True, "coin": coin})


@app.route("/api/bot/log")
def api_bot_log():
    coin = request.args.get("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"lines": [], "running": False})
    lines   = list(bot_log_buffers[coin])
    proc    = bot_processes.get(coin)
    running = proc is not None and proc.poll() is None
    return jsonify({"lines": lines, "running": running})


@app.route("/api/abort", methods=["POST"])
def api_abort():
    body = request.get_json() or {}
    coin = body.get("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"ok": False, "error": "unknown coin"})

    pf = BOT_DIR / f"current_position_{coin}.json"
    if not pf.exists():
        return jsonify({"ok": False, "error": "No open position"})
    try:
        pos       = json.load(open(pf))
        ticker    = pos["ticker"]
        side      = pos["side"]
        contracts = pos["contracts"]

        path = "/trade-api/v2/portfolio/orders"
        try:
            mpath = "/trade-api/v2/markets/" + ticker
            mr    = requests.get(KALSHI_BASE + "/markets/" + ticker,
                                 headers=sign_request("GET", mpath), timeout=5)
            mdata      = mr.json().get("market", mr.json())
            sell_price = (float(mdata.get("yes_bid_dollars", 0.01)) if side == "yes"
                          else float(mdata.get("no_bid_dollars", 0.01)))
            sell_price = max(sell_price, 0.01)
        except Exception:
            sell_price = 0.01

        price_str = f"{sell_price:.4f}"
        key       = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        body_str  = json.dumps({
            "ticker": ticker, "action": "sell", "side": side,
            "type": "limit", "count": contracts, key: price_str,
        })
        r = requests.post(KALSHI_BASE + "/portfolio/orders",
                          headers=sign_request("POST", path), data=body_str)
        pf.unlink(missing_ok=True)
        return jsonify({"ok": True, "result": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/trades")
def api_trades():
    coin          = request.args.get("coin", "BTC").upper()
    trades_folder = BOT_DIR / "trades" / coin
    if not trades_folder.exists():
        return jsonify([])
    try:
        all_rows = []
        for fpath in sorted(trades_folder.glob("trades_*.csv")):
            try:
                with open(fpath, newline="") as fh:
                    reader = csv.DictReader(fh)
                    if not reader.fieldnames:
                        continue
                    for row in reader:
                        clean = {str(k): str(v) if v is not None else ""
                                 for k, v in row.items() if k is not None}
                        all_rows.append(clean)
            except Exception:
                continue
        return jsonify(all_rows[-100:])
    except Exception:
        return jsonify([])


@app.route("/api/set_period", methods=["POST"])
def api_set_period():
    body   = request.get_json() or {}
    coin   = body.get("coin", "BTC").upper()
    period = body.get("period", "day")
    if coin in COINS:
        with coin_locks[coin]:
            coin_states[coin]["pnl_period"]             = period
            coin_states[coin]["kalshi_stats_updated"]   = 0
    return jsonify({"ok": True})


@app.route("/api/kalshi_stats")
def api_kalshi_stats():
    coin = request.args.get("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"ok": False, "error": "unknown coin"}), 400
    with coin_locks[coin]:
        period = coin_states[coin]["pnl_period"]
    try:
        stats = kalshi_compute_stats(coin, period)
        now   = time.time()
        with coin_locks[coin]:
            coin_states[coin]["kalshi_pnl_dollars"]   = stats["pnl"]
            coin_states[coin]["kalshi_pnl_pct"]       = stats["pnl_pct"]
            coin_states[coin]["kalshi_win_rate"]      = stats["win_rate"]
            coin_states[coin]["kalshi_wins"]          = stats["wins"]
            coin_states[coin]["kalshi_losses"]        = stats["losses"]
            coin_states[coin]["kalshi_total_cost"]    = stats["total_cost"]
            coin_states[coin]["kalshi_stats_updated"] = now
        return jsonify({"ok": True, **stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/llm", methods=["POST"])
def api_llm():
    """Proxy LLM requests server-side so API keys never touch the browser."""
    data      = request.get_json() or {}
    messages  = data.get("messages", [])
    sys_ctx   = data.get("system", "")

    with system_config_lock:
        scfg = dict(system_config)

    provider = scfg.get("llm_provider", "ollama")
    model    = scfg.get("llm_model",    "llama3")
    api_key  = scfg.get("llm_api_key",  "")
    base_url = scfg.get("llm_url",      "http://localhost:11434")

    # ── Build a rich live-context header from current bot state ─────────────
    try:
        with balance_lock:
            bal = balance_val.get("balance")
        bal_str = f"${bal:.2f}" if bal is not None else "unknown"
        running = [c for c in COINS if coin_states[c].get("bot_running")]
        coin_lines = []
        for coin in COINS:
            cs   = coin_states[coin]
            pnl  = cs.get("pnl_today",    0) or 0
            wins = cs.get("wins_today",   0) or 0
            loss = cs.get("losses_today", 0) or 0
            stat = cs.get("bot_status",  "idle")
            coin_lines.append(f"  {coin}: {stat}  P&L ${pnl:+.2f}  W{wins}/L{loss}")
        live_ctx = (
            f"\n\n--- LIVE BOT STATE ({datetime.now().strftime('%H:%M:%S')}) ---\n"
            f"Balance: {bal_str}  |  Running bots: {', '.join(running) or 'none'}\n"
            + "\n".join(coin_lines)
        )
        sys_ctx = sys_ctx + live_ctx
    except Exception:
        pass

    try:
        if provider == "anthropic":
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json={"model": model, "max_tokens": 1024,
                      "system": sys_ctx, "messages": messages},
                timeout=30,
            )
            d     = r.json()
            reply = (d.get("content") or [{}])[0].get("text") or d.get("error", {}).get("message", "No response")

        elif provider in ("openai", "grok"):
            url = ("https://api.x.ai/v1/chat/completions" if provider == "grok"
                   else "https://api.openai.com/v1/chat/completions")
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "system", "content": sys_ctx}] + messages},
                timeout=30,
            )
            d     = r.json()
            reply = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") \
                    or d.get("error", {}).get("message", "No response")

        else:  # ollama / hermes (both use Ollama backend)
            err = _ensure_ollama(base_url)
            if err:
                return jsonify({"reply": f"⚠ {err}", "ok": False})
            # Use streaming so tokens arrive continuously — avoids read timeout
            # when the model is loading cold (can take 60-180s on first call).
            r = requests.post(
                base_url.rstrip("/") + "/api/chat",
                json={"model": model,
                      "messages": [{"role": "system", "content": sys_ctx}] + messages,
                      "stream": True},
                stream=True,
                timeout=(30, 300),   # (connect, read) — 5 min read for cold model load
            )
            chunks = []
            for raw_line in r.iter_lines():
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                    token = (obj.get("message") or {}).get("content", "")
                    if token:
                        chunks.append(token)
                    if obj.get("done"):
                        break
                except Exception:
                    continue
            reply = "".join(chunks) or "No response"

        return jsonify({"reply": reply, "ok": True})

    except Exception as e:
        return jsonify({"reply": f"⚠ LLM error: {e}", "ok": False}), 200


@app.route("/api/global_settings", methods=["GET", "POST"])
def api_global_settings():
    global global_config
    if request.method == "GET":
        with global_config_lock:
            return jsonify(dict(global_config))
    data = request.get_json() or {}
    with global_config_lock:
        global_config.update(data)
        cfg = dict(global_config)
    with open(GLOBAL_CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
    # Restart telegram bot if token/enable changed
    _maybe_restart_telegram(cfg)
    # Propagate hard_stop_floor → wallet_floor in every per-coin config file
    # Propagate inverse_bet_enabled → inverse_bet_enabled in every per-coin config file
    _sync_keys = {}
    if "hard_stop_floor" in data:
        _sync_keys["wallet_floor"] = float(data["hard_stop_floor"])
    if "inverse_bet_enabled" in data:
        _sync_keys["inverse_bet_enabled"] = bool(data["inverse_bet_enabled"])
    if _sync_keys:
        for _coin in COINS:
            _cfg_path = BOT_DIR / f"bot_config_{_coin}.json"
            try:
                _coin_cfg = json.load(open(_cfg_path)) if _cfg_path.exists() else {}
                _coin_cfg.update(_sync_keys)
                with coin_locks[_coin]:
                    coin_states[_coin]["settings"].update(_sync_keys)
                with open(_cfg_path, "w") as _fh:
                    json.dump(_coin_cfg, _fh, indent=2)
            except Exception as _e:
                print(f"  global sync failed for {_coin}: {_e}")
    return jsonify({"ok": True})


@app.route("/api/system_settings", methods=["GET", "POST"])
def api_system_settings():
    global system_config
    if request.method == "GET":
        with system_config_lock:
            # Never expose raw API key — mask it
            out = dict(system_config)
            if out.get("llm_api_key"):
                out["llm_api_key_set"] = True
                out["llm_api_key"] = ""   # don't send key to browser
            return jsonify(out)
    data = request.get_json() or {}
    with system_config_lock:
        # Don't overwrite key if browser sent empty (masked)
        if not data.get("llm_api_key") and system_config.get("llm_api_key"):
            data.pop("llm_api_key", None)
        system_config.update(data)
        cfg = dict(system_config)
    with open(SYSTEM_CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
    return jsonify({"ok": True})


@app.route("/api/balance_history")
def api_balance_history():
    """Return cumulative P&L over time per coin for charting."""
    period = request.args.get("period", "day")   # day | week | month | all
    now    = datetime.now()
    if period == "day":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        cutoff = now - timedelta(days=7)
    elif period == "month":
        cutoff = now - timedelta(days=30)
    else:
        cutoff = datetime(2000, 1, 1)

    COIN_COLORS = {
        "BTC": "#f7931a", "ETH": "#627eea", "SOL": "#9945ff",
        "XRP": "#346aa9", "DOGE": "#c3a634", "BNB": "#f3ba2f", "HYPE": "#00ff88"
    }

    result = {}
    for coin in COINS:
        folder = BOT_DIR / "trades" / coin
        if not folder.exists():
            result[coin] = {"points": [], "color": COIN_COLORS.get(coin, "#aaa")}
            continue
        events = []
        for fpath in sorted(folder.glob("trades_*.csv")):
            try:
                with open(fpath, newline="") as fh:
                    for row in csv.DictReader(fh):
                        pnl_raw = row.get("pnl", "")
                        if not pnl_raw or pnl_raw in ("pending","unfilled","expired",""):
                            continue
                        try:
                            pnl = float(pnl_raw)
                            ts  = datetime.fromisoformat(row.get("timestamp",""))
                        except Exception:
                            continue
                        if ts >= cutoff:
                            events.append((ts, pnl))
            except Exception:
                pass
        events.sort(key=lambda x: x[0])
        cumulative, points = 0.0, []
        for ts, pnl in events:
            cumulative += pnl
            points.append({"t": ts.isoformat(), "v": round(cumulative, 4)})
        result[coin] = {"points": points, "color": COIN_COLORS.get(coin, "#aaa")}

    return jsonify(result)


@app.route("/api/analytics")
def api_analytics():
    """Aggregate trade analytics parsed from CSV + trade_code fields."""
    from collections import defaultdict
    sess_st  = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    hour_st  = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    coin_st  = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
    total, pending = 0, 0

    for coin in COINS:
        folder = BOT_DIR / "trades" / coin
        if not folder.exists():
            continue
        for fpath in sorted(folder.glob("trades_*.csv")):
            try:
                with open(fpath, newline="") as fh:
                    for row in csv.DictReader(fh):
                        pnl_raw = row.get("pnl", "")
                        if not pnl_raw or pnl_raw in ("pending", "unfilled", "expired", ""):
                            pending += 1
                            continue
                        try:
                            pnl = float(pnl_raw)
                        except Exception:
                            continue
                        total += 1
                        code  = row.get("trade_code", "")
                        parts = code.split("|") if code else []
                        sess  = parts[1] if len(parts) > 1 else "US"
                        hour  = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1

                        coin_st[coin]["trades"] += 1
                        coin_st[coin]["pnl"]    += pnl
                        if pnl > 0:
                            sess_st[sess]["wins"]  += 1
                            coin_st[coin]["wins"]  += 1
                        elif pnl < 0:
                            sess_st[sess]["losses"]  += 1
                            coin_st[coin]["losses"]  += 1
                        sess_st[sess]["pnl"] += pnl
                        if hour >= 0:
                            hour_st[hour]["trades"] += 1
                            hour_st[hour]["pnl"]    += pnl
                            if pnl > 0:
                                hour_st[hour]["wins"] += 1
            except Exception:
                pass

    return jsonify({
        "session": {k: v for k, v in sess_st.items()},
        "hourly":  {str(k): v for k, v in hour_st.items()},
        "coin":    {k: v for k, v in coin_st.items()},
        "total_settled": total,
        "total_pending": pending,
    })


@app.route("/api/kalshi_chart")
def api_kalshi_chart():
    """Return Kalshi market candlestick data for the current active market."""
    coin = request.args.get("coin", "BTC").upper()
    if coin not in COINS:
        return jsonify({"ok": False, "error": "unknown coin"}), 400

    with coin_locks[coin]:
        ticker = coin_states[coin].get("market_ticker")
    if not ticker:
        return jsonify({"ok": False, "error": "No active market"})

    series = COIN_SERIES.get(coin, f"KX{coin}15M")
    try:
        now_ts   = int(time.time())
        start_ts = now_ts - 1800  # last 30 min
        path = f"/trade-api/v2/series/{series}/markets/{ticker}/candlesticks"
        r = requests.get(
            KALSHI_BASE + f"/series/{series}/markets/{ticker}/candlesticks",
            headers=sign_request("GET", path),
            params={"start_ts": start_ts, "end_ts": now_ts, "period_interval": 1},
            timeout=6,
        )
        data    = r.json()
        candles = data.get("candlesticks", [])
        return jsonify({"ok": True, "ticker": ticker, "candles": candles})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/current_position")
def api_current_position():
    coin   = request.args.get("coin", "BTC").upper()
    series = COIN_SERIES.get(coin, f"KX{coin}")
    try:
        positions = kalshi_get_open_positions(series)
        if positions:
            return jsonify(positions[0])
        return jsonify(None)
    except Exception:
        pf = BOT_DIR / f"current_position_{coin}.json"
        if pf.exists():
            try:
                return jsonify(json.load(open(pf)))
            except Exception:
                pass
        return jsonify(None)


# ── Startup ───────────────────────────────────────────────────────────────────
def open_browser():
    time.sleep(2)
    webbrowser.open("http://localhost:5050")


if __name__ == "__main__":
    print("=" * 60)
    print("  COIN.KILLER DASHBOARD  —  Multi-Coin Edition")
    print("  http://localhost:5050")
    print("=" * 60)

    # Load saved settings for each coin
    for coin in COINS:
        cfg_path = BOT_DIR / f"bot_config_{coin}.json"
        if cfg_path.exists():
            try:
                saved = json.load(open(cfg_path))
                with coin_locks[coin]:
                    coin_states[coin]["settings"].update(saved)
            except Exception:
                pass
        # Legacy fallback: single bot_config.json → BTC only
        elif coin == "BTC":
            legacy = BOT_DIR / "bot_config.json"
            if legacy.exists():
                try:
                    saved = json.load(open(legacy))
                    with coin_locks[coin]:
                        coin_states[coin]["settings"].update(saved)
                except Exception:
                    pass

    # Load global config
    if GLOBAL_CONFIG_PATH.exists():
        try:
            saved_global = json.load(open(GLOBAL_CONFIG_PATH))
            with global_config_lock:
                global_config.update(saved_global)
            print("Loaded global_config.json")
        except Exception as e:
            print(f"Could not load global_config.json: {e}")

    # Start telegram bot if configured
    with global_config_lock:
        _gc = dict(global_config)
    _maybe_restart_telegram(_gc)

    # Start background threads
    print("Starting Coinbase price feed...")
    threading.Thread(target=coinbase_ws_loop,       daemon=True).start()
    threading.Thread(target=coinbase_rest_price_loop, daemon=True).start()

    print("Starting Kalshi market price poller...")
    threading.Thread(target=market_price_loop,   daemon=True).start()

    print("Starting balance loop...")
    threading.Thread(target=balance_loop,        daemon=True).start()

    print("Starting status/CSV reader...")
    threading.Thread(target=status_reader_loop,  daemon=True).start()

    print("Starting Kalshi stats loop...")
    threading.Thread(target=kalshi_stats_loop,   daemon=True).start()

    # Start signal monitors for all coins (so signals show even when bots are off)
    print("Starting signal monitors...")
    _sig_monitor_script = BOT_DIR / "signal_monitor.py"
    if _sig_monitor_script.exists():
        import subprocess
        for _coin in COINS:
            try:
                _env = os.environ.copy()
                _env["BOT_COIN"] = _coin
                _proc = subprocess.Popen(
                    [sys.executable, str(_sig_monitor_script), _coin],
                    env=_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                signal_processes[_coin] = _proc
                print(f"  Signal monitor started: {_coin} (pid {_proc.pid})")
            except Exception as _e:
                print(f"  Could not start signal monitor for {_coin}: {_e}")
    else:
        print("  signal_monitor.py not found — signals only available while bots run")

    threading.Thread(target=open_browser,        daemon=True).start()

    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
