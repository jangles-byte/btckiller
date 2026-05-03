"""
signals.py — Coin-agnostic signal engine.
Set BOT_COIN env var (or call init_coin()) before importing.
"""
import json
import math
import time
import threading
import requests
import websocket
from dotenv import load_dotenv
import os

load_dotenv()

COINALYZE_KEY  = os.getenv("COINALYZE_API_KEY")
COINALYZE_BASE = "https://api.coinalyze.net/v1"

# ── Coin config ───────────────────────────────────────────────────────────
COIN_CONFIGS = {
    # expected_move_pct   : % of price expected to move per sqrt(minute)
    # momentum_mild/strong: % move in 30-120s to register directional momentum
    # binance_futures     : USDS-margined futures symbol for order book + order flow
    "BTC":  {"coinbase_id": "BTC-USD",  "coinalyze": "BTCUSDT_PERP.A",  "expected_move_pct": 0.15, "momentum_mild": 0.02, "momentum_strong": 0.08, "binance_futures": "BTCUSDT"},
    "ETH":  {"coinbase_id": "ETH-USD",  "coinalyze": "ETHUSDT_PERP.A",  "expected_move_pct": 0.20, "momentum_mild": 0.03, "momentum_strong": 0.10, "binance_futures": "ETHUSDT"},
    "SOL":  {"coinbase_id": "SOL-USD",  "coinalyze": "SOLUSDT_PERP.A",  "expected_move_pct": 0.35, "momentum_mild": 0.05, "momentum_strong": 0.15, "binance_futures": "SOLUSDT"},
    "XRP":  {"coinbase_id": "XRP-USD",  "coinalyze": "XRPUSDT_PERP.A",  "expected_move_pct": 0.30, "momentum_mild": 0.04, "momentum_strong": 0.12, "binance_futures": "XRPUSDT"},
    "DOGE": {"coinbase_id": "DOGE-USD", "coinalyze": "DOGEUSDT_PERP.A", "expected_move_pct": 0.40, "momentum_mild": 0.06, "momentum_strong": 0.18, "binance_futures": "DOGEUSDT"},
    "BNB":  {"coinbase_id": "BNB-USD",  "coinalyze": "BNBUSDT_PERP.A",  "expected_move_pct": 0.25, "momentum_mild": 0.03, "momentum_strong": 0.10, "binance_futures": "BNBUSDT"},
    "HYPE": {"coinbase_id": "HYPE-USD", "coinalyze": "HYPEUSDT_PERP.A", "expected_move_pct": 0.50, "momentum_mild": 0.08, "momentum_strong": 0.25, "binance_futures": "HYPEUSDT"},
}

COIN = os.getenv("BOT_COIN", "BTC").upper()
_cfg = COIN_CONFIGS.get(COIN, COIN_CONFIGS["BTC"])
COINBASE_ID      = _cfg["coinbase_id"]
COINALYZE_SYMBOL = _cfg["coinalyze"]
EXPECTED_MOVE    = _cfg["expected_move_pct"]
MOM_MILD         = _cfg.get("momentum_mild",   0.05)
MOM_STRONG       = _cfg.get("momentum_strong", 0.15)
BINANCE_SYMBOL   = (_cfg.get("binance_futures") or "").lower()

# ── Shared state ──────────────────────────────────────────────────────────
coin_state = {
    "price": None,
    "price_history": [],
}

candles_cache = {
    "candles": [],
    "last_update": 0,
    "trend_1h": 0.0,
    "trend_6h": 0.0,
    "trend_24h": 0.0,
    "weekly_range_pct": 0.5,
    "hourly_bias": 0.0,
    "volatility": 1.0,
}

coinalyze_cache = {
    "funding_rate": None,
    "liq_history": [],
    "last_update": 0,
}

# ── Binance futures order book + order flow ───────────────────────────────
orderbook_cache = {
    "bid_vol":    0.0,   # total qty in top-10 bid levels
    "ask_vol":    0.0,   # total qty in top-10 ask levels
    "imbalance":  0.0,   # (bid - ask) / (bid + ask)  — positive = buy pressure
    "last_update": 0,
}

orderflow_cache = {
    "trades":      [],   # list of (timestamp, qty, is_buy_taker)
    "last_update": 0,
}

# ── Coinalyze ─────────────────────────────────────────────────────────────
def refresh_coinalyze():
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/funding-rate",
            params={"api_key": COINALYZE_KEY, "symbols": COINALYZE_SYMBOL},
            timeout=5
        )
        data = r.json()
        if data and isinstance(data, list):
            coinalyze_cache["funding_rate"] = data[0].get("value", 0)
    except Exception as e:
        print(f"[{COIN}] Funding fetch error: {e}")

    time.sleep(1)

    try:
        now = int(time.time())
        r = requests.get(
            f"{COINALYZE_BASE}/liquidation-history",
            params={
                "api_key": COINALYZE_KEY,
                "symbols": COINALYZE_SYMBOL,
                "interval": "1min",
                "from": now - 300,
                "to": now,
            },
            timeout=5
        )
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            coinalyze_cache["liq_history"] = data[0].get("history", [])
        else:
            coinalyze_cache["liq_history"] = []
    except Exception as e:
        print(f"[{COIN}] Liquidation fetch error: {e}")

    coinalyze_cache["last_update"] = time.time()

def coinalyze_refresh_loop():
    # Stagger startup across coins so all 7 bots don't hit the API simultaneously.
    # BTC=0s, ETH=8s, SOL=16s, XRP=24s, DOGE=32s, BNB=40s, HYPE=48s
    coin_order = list(COIN_CONFIGS.keys())
    stagger = coin_order.index(COIN) * 8 if COIN in coin_order else 0
    if stagger:
        time.sleep(stagger)
    while True:
        refresh_coinalyze()
        time.sleep(60)

# ── Candles ───────────────────────────────────────────────────────────────
def fetch_candles():
    try:
        import datetime as dt
        end   = dt.datetime.utcnow()
        start = end - dt.timedelta(days=7)
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{COINBASE_ID}/candles",
            params={"granularity": 3600, "start": start.isoformat(), "end": end.isoformat()},
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list) or len(data) < 10:
            return

        candles = sorted([{
            "t": c[0], "low": c[1], "high": c[2],
            "open": c[3], "close": c[4], "volume": c[5]
        } for c in data], key=lambda x: x["t"])

        candles_cache["candles"] = candles
        closes  = [c["close"] for c in candles]
        current = closes[-1]

        if len(closes) >= 2:  candles_cache["trend_1h"]  = ((current - closes[-2])  / closes[-2])  * 100
        if len(closes) >= 7:  candles_cache["trend_6h"]  = ((current - closes[-7])  / closes[-7])  * 100
        if len(closes) >= 25: candles_cache["trend_24h"] = ((current - closes[-25]) / closes[-25]) * 100

        week_low  = min(c["low"]  for c in candles)
        week_high = max(c["high"] for c in candles)
        if week_high > week_low:
            candles_cache["weekly_range_pct"] = (current - week_low) / (week_high - week_low)

        hourly_ranges = [(c["high"] - c["low"]) / c["close"] for c in candles]
        avg_range     = sum(hourly_ranges) / len(hourly_ranges) if hourly_ranges else 0.001
        candles_cache["volatility"] = (hourly_ranges[-1] / avg_range) if avg_range > 0 else 1.0

        from collections import defaultdict
        import datetime as _dt
        hour_returns = defaultdict(list)
        for i in range(1, len(candles)):
            h   = _dt.datetime.utcfromtimestamp(candles[i]["t"]).hour
            ret = (candles[i]["close"] - candles[i]["open"]) / candles[i]["open"]
            hour_returns[h].append(ret)
        current_hour = _dt.datetime.utcnow().hour
        if current_hour in hour_returns:
            candles_cache["hourly_bias"] = sum(hour_returns[current_hour]) / len(hour_returns[current_hour])

        candles_cache["last_update"] = time.time()
        print(f"[{COIN}] Candles updated: 1h={candles_cache['trend_1h']:+.2f}% "
              f"24h={candles_cache['trend_24h']:+.2f}%")
    except Exception as e:
        print(f"[{COIN}] Candles fetch error: {e}")

def candles_refresh_loop():
    while True:
        fetch_candles()
        time.sleep(3600)

# ── WebSocket price feed ──────────────────────────────────────────────────
def on_message(ws, message):
    data = json.loads(message)
    if data.get("type") == "ticker" and "price" in data:
        price = float(data["price"])
        now   = time.time()
        coin_state["price"] = price
        history = coin_state["price_history"]
        history.append((now, price))
        # Trim old entries (>5 min) — only scan from front, not rebuild entire list
        cutoff = now - 300
        while history and history[0][0] < cutoff:
            history.pop(0)

def on_error(ws, error):
    print(f"[{COIN}] WebSocket error: {error}")

def on_close(ws, *args):
    print(f"[{COIN}] WebSocket closed — reconnecting in 3s...")
    time.sleep(3)
    start_price_feed()

def on_open(ws):
    print(f"[{COIN}] Connected to Coinbase {COINBASE_ID} feed")
    ws.send(json.dumps({
        "type": "subscribe",
        "product_ids": [COINBASE_ID],
        "channels": ["ticker"]
    }))

def start_price_feed():
    ws = websocket.WebSocketApp(
        "wss://ws-feed.exchange.coinbase.com",
        on_message=on_message, on_error=on_error,
        on_close=on_close, on_open=on_open,
    )
    ws.run_forever()

# ── Binance futures WebSocket (order book + order flow) ──────────────────
def _binance_on_message(ws, message):
    try:
        data   = json.loads(message)
        stream = data.get("stream", "")
        d      = data.get("data", data)

        if "depth" in stream:
            # Partial book — top-10 bids and asks
            bids = d.get("b", [])[:10]
            asks = d.get("a", [])[:10]
            bid_vol = sum(float(qty) for _, qty in bids)
            ask_vol = sum(float(qty) for _, qty in asks)
            total   = bid_vol + ask_vol
            orderbook_cache["bid_vol"]    = bid_vol
            orderbook_cache["ask_vol"]    = ask_vol
            orderbook_cache["imbalance"]  = (bid_vol - ask_vol) / total if total else 0.0
            orderbook_cache["last_update"] = time.time()

        elif "aggTrade" in stream:
            # m=True → buyer is maker → this was a SELL (taker sold)
            # m=False → buyer is taker → this was a BUY (taker bought)
            is_buy = not d.get("m", True)
            qty    = float(d.get("q", 0))
            ts     = d.get("T", 0) / 1000.0
            orderflow_cache["trades"].append((ts, qty, is_buy))
            # Trim to 5-minute rolling window
            cutoff = time.time() - 300
            while orderflow_cache["trades"] and orderflow_cache["trades"][0][0] < cutoff:
                orderflow_cache["trades"].pop(0)
            orderflow_cache["last_update"] = time.time()
    except Exception as e:
        print(f"[{COIN}] Binance msg error: {e}")

def _binance_feed_loop():
    if not BINANCE_SYMBOL:
        return
    sym = BINANCE_SYMBOL
    url = (f"wss://fstream.binance.com/stream?streams="
           f"{sym}@depth20@500ms/{sym}@aggTrade")
    while True:
        try:
            ws = websocket.WebSocketApp(
                url,
                on_message=_binance_on_message,
                on_error=lambda ws, e: print(f"[{COIN}] Binance WS error: {e}"),
                on_open=lambda ws: print(f"[{COIN}] Connected to Binance {sym.upper()} futures feed"),
            )
            ws.run_forever()
        except Exception as e:
            print(f"[{COIN}] Binance feed exception: {e}")
        print(f"[{COIN}] Binance feed disconnected — reconnecting in 5s...")
        time.sleep(5)

def start_feed_thread():
    threading.Thread(target=start_price_feed,        daemon=True).start()
    threading.Thread(target=coinalyze_refresh_loop,  daemon=True).start()
    threading.Thread(target=candles_refresh_loop,    daemon=True).start()
    if BINANCE_SYMBOL:
        threading.Thread(target=_binance_feed_loop,  daemon=True).start()

# ── Signal calculators ────────────────────────────────────────────────────

def _liquidation_signal():
    history = coinalyze_cache.get("liq_history", [])
    if not history:
        return 0.5, "No liquidation data", 0.0

    now = time.time()
    wl = ws_ = 0.0
    for h in history:
        age = now - h["t"]
        w   = 3.0 if age < 60 else (1.0 if age < 180 else 0.3)
        wl  += h.get("l", 0) * w
        ws_ += h.get("s", 0) * w

    total = wl + ws_
    if total < 0.3:
        return 0.5, "Low liquidation activity", 0.1

    short_ratio = ws_ / total
    yes_prob    = 0.35 + (short_ratio * 0.3)
    strength    = min(total / 5.0, 1.0)

    if short_ratio > 0.7:    reason = f"Short cascade {ws_:.1f} → UP"
    elif short_ratio > 0.55: reason = f"Short-leaning liqs → mild UP"
    elif short_ratio < 0.3:  reason = f"Long cascade {wl:.1f} → DOWN"
    elif short_ratio < 0.45: reason = f"Long-leaning liqs → mild DOWN"
    else:                    reason = f"Mixed liqs L:{wl:.1f} S:{ws_:.1f}"
    return yes_prob, reason, strength

def _momentum_signal():
    history = coin_state["price_history"]
    price   = coin_state["price"]
    if not price or len(history) < 3:
        return 0.5, "Building history...", 0.0

    now    = time.time()
    older  = None
    window_used = 30
    for window in (120, 60, 30):
        candidates = [(t, p) for t, p in history if t <= now - window]
        if candidates:
            older       = candidates
            window_used = window
            break
    if not older:
        return 0.5, "Not enough history", 0.0

    move_pct = ((price - older[-1][1]) / older[-1][1]) * 100
    lbl = f"{window_used}s"

    if move_pct > MOM_STRONG:    return 0.70, f"Strong UP +{move_pct:.3f}% ({lbl})",   1.0
    elif move_pct > MOM_MILD:    return 0.58, f"Mild UP +{move_pct:.3f}% ({lbl})",     0.6
    elif move_pct < -MOM_STRONG: return 0.30, f"Strong DOWN {move_pct:.3f}% ({lbl})",  1.0
    elif move_pct < -MOM_MILD:   return 0.42, f"Mild DOWN {move_pct:.3f}% ({lbl})",    0.6
    else:                        return 0.50, f"Flat {move_pct:.3f}% ({lbl})",          0.1

def _kalshi_distance_signal(strike_price, strike_type, mins_remaining):
    price = coin_state["price"]
    if not price or not strike_price or mins_remaining is None:
        return 0.5, "No market data", 0.0
    try:
        strike = float(strike_price)
    except (TypeError, ValueError):
        return 0.5, "Invalid strike", 0.0

    distance     = price - strike
    distance_pct = (distance / strike) * 100
    exp_move_pct = EXPECTED_MOVE * (max(mins_remaining, 0.1) ** 0.5)
    z            = distance_pct / exp_move_pct if exp_move_pct else 0
    yes_prob_above = 1 / (1 + math.exp(-z * 1.2))

    if strike_type and "less" in str(strike_type).lower():
        yes_prob = 1 - yes_prob_above
    else:
        yes_prob = yes_prob_above

    strength  = min(1.0, (15 - mins_remaining) / 15 + 0.3)
    direction = "UP" if yes_prob > 0.55 else ("DOWN" if yes_prob < 0.45 else "NEUTRAL")
    reason    = f"{COIN} ${distance:+.2f} vs strike, {mins_remaining:.1f}min → {direction}"
    return yes_prob, reason, strength

def _multi_timeframe_signal():
    candles = candles_cache.get("candles", [])
    if not candles or candles_cache["last_update"] == 0:
        return 0.5, "Historical data loading...", 0.0

    trend_1h    = candles_cache["trend_1h"]
    trend_6h    = candles_cache["trend_6h"]
    trend_24h   = candles_cache["trend_24h"]
    range_pct   = candles_cache["weekly_range_pct"]
    hourly_bias = candles_cache["hourly_bias"]
    volatility  = candles_cache["volatility"]

    score = 0.0
    if trend_1h  > 0.1:  score += 0.3
    elif trend_1h  < -0.1: score -= 0.3
    if trend_6h  > 0.3:  score += 0.4
    elif trend_6h  < -0.3: score -= 0.4
    if trend_24h > 0.5:  score += 0.3
    elif trend_24h < -0.5: score -= 0.3
    if range_pct > 0.85:   score -= 0.3
    elif range_pct > 0.70: score -= 0.15
    elif range_pct < 0.15: score += 0.3
    elif range_pct < 0.30: score += 0.15
    score += hourly_bias * 20
    if volatility > 2.0:
        score *= 0.5

    yes_prob = max(0.25, min(0.75, 0.5 + score * 0.1))
    trend_agreement = abs(
        (1 if trend_1h > 0 else -1) +
        (1 if trend_6h > 0 else -1) +
        (1 if trend_24h > 0 else -1)
    ) / 3.0
    strength  = trend_agreement * 0.8
    direction = "UP" if yes_prob > 0.52 else ("DOWN" if yes_prob < 0.48 else "NEUTRAL")
    reason    = (f"1h:{trend_1h:+.1f}% 6h:{trend_6h:+.1f}% 24h:{trend_24h:+.1f}% → {direction}")
    return yes_prob, reason, strength

def _crowd_confidence_signal(yes_ask, no_ask, mins_remaining):
    if yes_ask is None or no_ask is None or mins_remaining is None:
        return 0.5, "No market data", 0.0

    crowd_yes        = yes_ask
    crowd_confidence = abs(crowd_yes - 0.5) * 2

    if mins_remaining >= 10:   time_factor = 0.0
    elif mins_remaining >= 5:  time_factor = (10 - mins_remaining) / 5
    else:                      time_factor = 1.0

    strength = crowd_confidence * 0.7

    if time_factor < 0.4:
        if crowd_yes > 0.65:
            yes_prob = max(0.25, 0.5 - (crowd_yes - 0.65) * 0.8)
            reason   = f"Crowd {round(crowd_yes*100)}% YES early → contrarian NO"
        elif crowd_yes < 0.35:
            yes_prob = min(0.75, 0.5 + (0.35 - crowd_yes) * 0.8)
            reason   = f"Crowd {round(crowd_yes*100)}% YES early → contrarian YES"
        else:
            yes_prob = 0.5
            reason   = f"Crowd neutral early ({round(crowd_yes*100)}%)"
            strength = 0.1
    else:
        yes_prob  = max(0.1, min(0.9, 0.5 + (crowd_yes - 0.5) * time_factor))
        direction = "YES" if crowd_yes > 0.5 else "NO"
        reason    = f"Crowd {round(crowd_yes*100)}% YES late → confirming {direction}"

    return yes_prob, reason, strength

def _orderbook_signal():
    """Bid vs ask wall imbalance from Binance futures top-10 depth."""
    if not BINANCE_SYMBOL or time.time() - orderbook_cache["last_update"] > 15:
        return 0.5, "No orderbook data", 0.0

    imb     = orderbook_cache["imbalance"]   # -1.0 to +1.0
    abs_imb = abs(imb)

    if abs_imb < 0.10:
        return 0.5, f"Balanced book {imb:+.2f}", 0.1

    if imb > 0.35:
        yes_prob, reason = 0.73, f"Strong bid wall {imb:+.2f} → UP"
    elif imb > 0.10:
        yes_prob, reason = 0.61, f"Bid-heavy {imb:+.2f} → mild UP"
    elif imb < -0.35:
        yes_prob, reason = 0.27, f"Strong ask wall {imb:+.2f} → DOWN"
    else:
        yes_prob, reason = 0.39, f"Ask-heavy {imb:+.2f} → mild DOWN"

    strength = min(abs_imb / 0.35, 1.0)
    return yes_prob, reason, strength


def _orderflow_signal():
    """Taker buy vs sell volume over the last 2 minutes from Binance futures aggTrades."""
    if not BINANCE_SYMBOL:
        return 0.5, "No orderflow data", 0.0

    now    = time.time()
    cutoff = now - 120  # rolling 2-minute window
    recent = [(ts, qty, ib) for ts, qty, ib in orderflow_cache["trades"] if ts > cutoff]

    if not recent:
        return 0.5, "No recent trades", 0.0

    buy_vol  = sum(qty for _, qty, ib in recent if ib)
    sell_vol = sum(qty for _, qty, ib in recent if not ib)
    total    = buy_vol + sell_vol

    if total < 0.001:
        return 0.5, "Low volume", 0.0

    ratio = (buy_vol - sell_vol) / total   # -1.0 to +1.0

    if ratio > 0.30:
        yes_prob, reason = 0.72, f"Buy flow dominant {ratio:+.2f}"
    elif ratio > 0.10:
        yes_prob, reason = 0.60, f"Mild buy pressure {ratio:+.2f}"
    elif ratio < -0.30:
        yes_prob, reason = 0.28, f"Sell flow dominant {ratio:+.2f}"
    elif ratio < -0.10:
        yes_prob, reason = 0.40, f"Mild sell pressure {ratio:+.2f}"
    else:
        return 0.5, f"Balanced flow {ratio:+.2f}", 0.1

    strength = min(abs(ratio) / 0.30, 1.0)
    return yes_prob, reason, strength


def _position_safety(strike_price, strike_type, mins_remaining):
    price = coin_state["price"]
    if not price or not strike_price:
        return 0.0, None, 0.0
    try:
        strike = float(strike_price)
    except (TypeError, ValueError):
        return 0.0, None, 0.0

    distance      = abs(price - strike)
    expected_move = strike * (EXPECTED_MOVE / 100) * (max(mins_remaining, 0.1) ** 0.5)
    safety        = distance / expected_move if expected_move > 0 else 0.0
    btc_above     = price > strike

    if strike_type and "less" in str(strike_type).lower():
        safe_side = "yes" if not btc_above else "no"
    else:
        safe_side = "yes" if btc_above else "no"

    return safety, safe_side, price - strike

# ── Main aggregator ───────────────────────────────────────────────────────
def get_signal(strike_price=None, strike_type=None, mins_remaining=None,
               yes_ask=None, no_ask=None):
    liq_p, liq_r, liq_s = _liquidation_signal()
    mom_p, mom_r, mom_s = _momentum_signal()
    kal_p, kal_r, kal_s = _kalshi_distance_signal(strike_price, strike_type, mins_remaining)
    crd_p, crd_r, crd_s = _crowd_confidence_signal(yes_ask, no_ask, mins_remaining)
    mtf_p, mtf_r, mtf_s = _multi_timeframe_signal()
    ob_p,  ob_r,  ob_s  = _orderbook_signal()
    of_p,  of_r,  of_s  = _orderflow_signal()

    signals_list = [
        {"name": "Liquidations", "yes_prob": liq_p, "reason": liq_r, "weight": 0.15, "strength": liq_s},
        {"name": "Momentum",     "yes_prob": mom_p, "reason": mom_r, "weight": 0.15, "strength": mom_s},
        {"name": "Kalshi Dist",  "yes_prob": kal_p, "reason": kal_r, "weight": 0.15, "strength": kal_s},
        {"name": "Crowd",        "yes_prob": crd_p, "reason": crd_r, "weight": 0.10, "strength": crd_s},
        {"name": "Order Book",   "yes_prob": ob_p,  "reason": ob_r,  "weight": 0.25, "strength": ob_s},
        {"name": "Order Flow",   "yes_prob": of_p,  "reason": of_r,  "weight": 0.20, "strength": of_s},
        {"name": "Multi-TF",     "yes_prob": mtf_p, "reason": mtf_r, "weight": 0.15, "strength": mtf_s},
    ]

    total_weight = sum(s["weight"] * s["strength"] for s in signals_list)
    our_yes_prob = (
        sum(s["yes_prob"] * s["weight"] * s["strength"] for s in signals_list) / total_weight
        if total_weight > 0 else 0.5
    )

    yes_votes   = sum(s["strength"] for s in signals_list if s["yes_prob"] > 0.55)
    no_votes    = sum(s["strength"] for s in signals_list if s["yes_prob"] < 0.45)
    total_votes = yes_votes + no_votes
    if total_votes > 0:
        signal_agreement = abs(yes_votes - no_votes) / total_votes
        signal_direction = "yes" if yes_votes > no_votes else "no"
    else:
        signal_agreement = 0.0
        signal_direction = None

    # Count how many individual signals are pointing the LEADING direction
    # (not just strength-weighted — each signal counts as 1 if it has a clear opinion)
    yes_count = sum(1 for s in signals_list if s["yes_prob"] > 0.55)
    no_count  = sum(1 for s in signals_list if s["yes_prob"] < 0.45)
    agreeing_count = yes_count if yes_votes >= no_votes else no_count

    pos_safety, safe_side, distance = _position_safety(strike_price, strike_type, mins_remaining)

    m = max(mins_remaining or 0, 0)
    if m >= 11:    time_factor = 0.2
    elif m >= 8:   time_factor = 0.3 + (11 - m) * 0.033
    elif m >= 4:   time_factor = 0.4 + (8 - m) * 0.075
    elif m >= 2:   time_factor = 0.7 + (4 - m) * 0.1
    else:          time_factor = 1.0

    return {
        "price":            coin_state["price"],
        "our_yes_prob":     our_yes_prob,
        "our_no_prob":      1 - our_yes_prob,
        "confidence":       total_weight,
        "signals":          signals_list,
        "yes_votes":        round(yes_votes, 2),
        "no_votes":         round(no_votes, 2),
        "pos_safety":       pos_safety,
        "safe_side":        safe_side,
        "distance":         distance,
        "signal_agreement": signal_agreement,
        "signal_direction": signal_direction,
        "time_factor":      time_factor,
        "agreeing_count":   agreeing_count,
    }

# kept for dashboard backward-compat
btc_state = coin_state

def get_candles_context():
    return {
        "trend_1h":       candles_cache["trend_1h"],
        "trend_6h":       candles_cache["trend_6h"],
        "trend_24h":      candles_cache["trend_24h"],
        "range_pct":      candles_cache["weekly_range_pct"],
        "volatility":     candles_cache["volatility"],
        "hourly_bias":    candles_cache["hourly_bias"],
        "candles_loaded": len(candles_cache["candles"]) > 0,
    }
