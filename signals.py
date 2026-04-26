import json
import math
import time
import threading
import requests
import websocket
from dotenv import load_dotenv
import os

load_dotenv()

COINALYZE_KEY = os.getenv("COINALYZE_API_KEY")
COINALYZE_BASE = "https://api.coinalyze.net/v1"

btc_state = {
    "price": None,
    "price_history": [],
}

# Historical candles cache — hourly BTC for last 7 days
candles_cache = {
    "candles": [],       # list of {t, open, high, low, close, volume}
    "last_update": 0,
    "trend_1h": 0.0,     # % change last hour
    "trend_6h": 0.0,     # % change last 6 hours
    "trend_24h": 0.0,    # % change last 24 hours
    "weekly_range_pct": 0.5,  # where price sits in 7-day range (0=low, 1=high)
    "hourly_bias": 0.0,  # time-of-day historical bias (-1 to +1)
    "volatility": 1.0,   # current vol vs weekly avg (1=normal, 2=high)
}

coinalyze_cache = {
    "funding_rate": None,
    "liq_history": [],
    "last_update": 0,
}

def refresh_coinalyze():
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/funding-rate",
            params={"api_key": COINALYZE_KEY, "symbols": "BTCUSDT_PERP.A"},
            timeout=5
        )
        data = r.json()
        if data and isinstance(data, list):
            coinalyze_cache["funding_rate"] = data[0].get("value", 0)
    except Exception as e:
        print(f"Funding fetch error: {e}")

    time.sleep(1)

    try:
        now = int(time.time())
        r = requests.get(
            f"{COINALYZE_BASE}/liquidation-history",
            params={
                "api_key": COINALYZE_KEY,
                "symbols": "BTCUSDT_PERP.A",
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
        print(f"Liquidation fetch error: {e}")

    coinalyze_cache["last_update"] = time.time()

def coinalyze_refresh_loop():
    while True:
        refresh_coinalyze()
        time.sleep(30)

def fetch_candles():
    """Fetch hourly BTC candles for last 7 days from Coinbase."""
    try:
        import datetime as dt
        end = dt.datetime.utcnow()
        start = end - dt.timedelta(days=7)
        r = requests.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles",
            params={
                "granularity": 3600,  # 1 hour
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list) or len(data) < 10:
            return

        # Format: [timestamp, low, high, open, close, volume]
        candles = sorted([{
            "t": c[0], "low": c[1], "high": c[2],
            "open": c[3], "close": c[4], "volume": c[5]
        } for c in data], key=lambda x: x["t"])

        candles_cache["candles"] = candles

        closes = [c["close"] for c in candles]
        current = closes[-1]

        # Trend calculations
        if len(closes) >= 2:
            candles_cache["trend_1h"] = ((current - closes[-2]) / closes[-2]) * 100
        if len(closes) >= 7:
            candles_cache["trend_6h"] = ((current - closes[-7]) / closes[-7]) * 100
        if len(closes) >= 25:
            candles_cache["trend_24h"] = ((current - closes[-25]) / closes[-25]) * 100

        # Weekly range percentile
        week_low = min(c["low"] for c in candles)
        week_high = max(c["high"] for c in candles)
        if week_high > week_low:
            candles_cache["weekly_range_pct"] = (current - week_low) / (week_high - week_low)

        # Volatility: avg hourly range vs current
        hourly_ranges = [(c["high"] - c["low"]) / c["close"] for c in candles]
        avg_range = sum(hourly_ranges) / len(hourly_ranges) if hourly_ranges else 0.001
        current_range = hourly_ranges[-1] if hourly_ranges else avg_range
        candles_cache["volatility"] = current_range / avg_range if avg_range > 0 else 1.0

        # Time-of-day bias: group by hour, avg return
        from collections import defaultdict
        import datetime as _dt
        hour_returns = defaultdict(list)
        for i in range(1, len(candles)):
            h = _dt.datetime.utcfromtimestamp(candles[i]["t"]).hour
            ret = (candles[i]["close"] - candles[i]["open"]) / candles[i]["open"]
            hour_returns[h].append(ret)
        current_hour = _dt.datetime.utcnow().hour
        if current_hour in hour_returns:
            candles_cache["hourly_bias"] = sum(hour_returns[current_hour]) / len(hour_returns[current_hour])

        candles_cache["last_update"] = time.time()
        print(f"Candles updated: trend_1h={candles_cache['trend_1h']:+.2f}% "
              f"trend_24h={candles_cache['trend_24h']:+.2f}% "
              f"range_pct={candles_cache['weekly_range_pct']:.2f} "
              f"vol={candles_cache['volatility']:.2f}x")

    except Exception as e:
        print(f"Candles fetch error: {e}")

def candles_refresh_loop():
    """Refresh candles every hour."""
    while True:
        fetch_candles()
        time.sleep(3600)

def on_message(ws, message):
    data = json.loads(message)
    if data.get("type") == "ticker" and "price" in data:
        price = float(data["price"])
        now = time.time()
        btc_state["price"] = price
        btc_state["price_history"].append((now, price))
        cutoff = now - 300
        btc_state["price_history"] = [
            (t, p) for t, p in btc_state["price_history"] if t > cutoff
        ]

def on_error(ws, error):
    print(f"WebSocket error: {error}")

def on_close(ws, *args):
    print("WebSocket closed — reconnecting in 3s...")
    time.sleep(3)
    start_price_feed()

def on_open(ws):
    print("Connected to Coinbase BTC price feed")
    ws.send(json.dumps({
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channels": ["ticker"]
    }))

def start_price_feed():
    ws = websocket.WebSocketApp(
        "wss://ws-feed.exchange.coinbase.com",
        on_message=on_message, on_error=on_error,
        on_close=on_close, on_open=on_open,
    )
    ws.run_forever()

def start_feed_thread():
    threading.Thread(target=start_price_feed, daemon=True).start()
    threading.Thread(target=coinalyze_refresh_loop, daemon=True).start()
    threading.Thread(target=candles_refresh_loop, daemon=True).start()

# ── Signal calculators ────────────────────────────────────────────────────

def _liquidation_signal():
    """Recency-weighted liquidation cascade."""
    history = coinalyze_cache.get("liq_history", [])
    if not history:
        return 0.5, "No liquidation data", 0.0

    now = time.time()
    wl = ws_ = 0.0
    for h in history:
        age = now - h["t"]
        w = 3.0 if age < 60 else (1.0 if age < 180 else 0.3)
        wl += h.get("l", 0) * w
        ws_ += h.get("s", 0) * w

    total = wl + ws_
    if total < 0.3:
        return 0.5, "Low liquidation activity", 0.1

    short_ratio = ws_ / total
    yes_prob = 0.35 + (short_ratio * 0.3)
    strength = min(total / 5.0, 1.0)

    if short_ratio > 0.7:    reason = f"Short cascade {ws_:.1f} BTC → UP"
    elif short_ratio > 0.55: reason = f"Short-leaning liqs → mild UP"
    elif short_ratio < 0.3:  reason = f"Long cascade {wl:.1f} BTC → DOWN"
    elif short_ratio < 0.45: reason = f"Long-leaning liqs → mild DOWN"
    else:                    reason = f"Mixed liqs L:{wl:.1f} S:{ws_:.1f}"

    return yes_prob, reason, strength

def _momentum_signal():
    """2-minute BTC momentum."""
    history = btc_state["price_history"]
    price = btc_state["price"]
    if not price or len(history) < 10:
        return 0.5, "Building history...", 0.0

    now = time.time()
    older = [(t, p) for t, p in history if t <= now - 120]
    if not older:
        return 0.5, "Not enough history", 0.0

    move_pct = ((price - older[-1][1]) / older[-1][1]) * 100

    if move_pct > 0.15:    return 0.70, f"Strong UP +{move_pct:.3f}%", 1.0
    elif move_pct > 0.05:  return 0.58, f"Mild UP +{move_pct:.3f}%", 0.6
    elif move_pct < -0.15: return 0.30, f"Strong DOWN {move_pct:.3f}%", 1.0
    elif move_pct < -0.05: return 0.42, f"Mild DOWN {move_pct:.3f}%", 0.6
    else:                  return 0.50, f"Flat {move_pct:.3f}%", 0.1

def _kalshi_distance_signal(strike_price, strike_type, mins_remaining):
    """BTC position vs strike — strongest late-market signal."""
    price = btc_state["price"]
    if not price or not strike_price or mins_remaining is None:
        return 0.5, "No market data", 0.0

    try:
        strike = float(strike_price)
    except (TypeError, ValueError):
        return 0.5, "Invalid strike", 0.0

    distance = price - strike
    distance_pct = (distance / strike) * 100
    expected_move_pct = 0.15 * (max(mins_remaining, 0.1) ** 0.5)
    z = distance_pct / expected_move_pct if expected_move_pct else 0

    yes_prob_above = 1 / (1 + math.exp(-z * 1.2))

    if strike_type and "less" in str(strike_type).lower():
        yes_prob = 1 - yes_prob_above
    else:
        yes_prob = yes_prob_above

    strength = min(1.0, (15 - mins_remaining) / 15 + 0.3)
    direction = "UP" if yes_prob > 0.55 else ("DOWN" if yes_prob < 0.45 else "NEUTRAL")
    reason = f"BTC ${distance:+.0f} vs strike, {mins_remaining:.1f}min → {direction}"

    return yes_prob, reason, strength

def _multi_timeframe_signal():
    """
    Multi-timeframe trend context.
    Uses hourly candles to determine if we're in a bullish or bearish regime.
    Returns a YES probability bias based on macro trend.
    """
    candles = candles_cache.get("candles", [])
    if not candles or candles_cache["last_update"] == 0:
        return 0.5, "Historical data loading...", 0.0

    trend_1h   = candles_cache["trend_1h"]
    trend_6h   = candles_cache["trend_6h"]
    trend_24h  = candles_cache["trend_24h"]
    range_pct  = candles_cache["weekly_range_pct"]
    hourly_bias = candles_cache["hourly_bias"]
    volatility = candles_cache["volatility"]

    # Score: positive = bullish, negative = bearish
    score = 0.0

    # Trend alignment — all timeframes agreeing is stronger signal
    if trend_1h > 0.1:   score += 0.3
    elif trend_1h < -0.1: score -= 0.3

    if trend_6h > 0.3:   score += 0.4
    elif trend_6h < -0.3: score -= 0.4

    if trend_24h > 0.5:  score += 0.3
    elif trend_24h < -0.5: score -= 0.3

    # Weekly range position
    # Near 7-day high → potential resistance/reversal
    # Near 7-day low → potential support/bounce
    if range_pct > 0.85:   score -= 0.3  # near weekly high, lean bearish
    elif range_pct > 0.70:  score -= 0.15
    elif range_pct < 0.15:  score += 0.3  # near weekly low, lean bullish
    elif range_pct < 0.30:  score += 0.15

    # Time-of-day historical bias
    score += hourly_bias * 20  # scale small % to score

    # High volatility = less reliable signals = pull toward neutral
    if volatility > 2.0:
        score *= 0.5

    # Map score to probability
    yes_prob = 0.5 + (score * 0.1)
    yes_prob = max(0.25, min(0.75, yes_prob))

    # Strength: how confident are we in this context
    trend_agreement = abs(
        (1 if trend_1h > 0 else -1) +
        (1 if trend_6h > 0 else -1) +
        (1 if trend_24h > 0 else -1)
    ) / 3.0
    strength = trend_agreement * 0.8

    direction = "UP" if yes_prob > 0.52 else ("DOWN" if yes_prob < 0.48 else "NEUTRAL")
    reason = (f"Trend 1h:{trend_1h:+.1f}% 6h:{trend_6h:+.1f}% 24h:{trend_24h:+.1f}% "
              f"range:{range_pct:.0%} → {direction}")

    return yes_prob, reason, strength

def _crowd_confidence_signal(yes_ask, no_ask, mins_remaining):
    """
    Time-weighted crowd contrarian/confirming signal.

    Early market: crowd overconfidence is a contrarian signal
      - crowd says 80% YES early → they're probably wrong → lean NO
    
    Late market: crowd is usually right, follow them
      - crowd says 80% YES late → BTC clearly above strike → confirm YES

    The crossover happens around 5-6 minutes remaining.
    """
    if yes_ask is None or no_ask is None or mins_remaining is None:
        return 0.5, "No market data", 0.0

    # Crowd implied probability for YES
    crowd_yes = yes_ask  # e.g. 0.75 means crowd thinks 75% chance YES

    # How extreme is the crowd's position? (0 = neutral, 1 = fully confident)
    crowd_confidence = abs(crowd_yes - 0.5) * 2  # 0 to 1

    # Time factor: 0 = early (contrarian), 1 = late (confirming)
    # Crossover at ~5 minutes
    if mins_remaining >= 10:
        time_factor = 0.0   # fully contrarian
    elif mins_remaining >= 5:
        time_factor = (10 - mins_remaining) / 5  # 0 → 1 over 5-10 min window
    else:
        time_factor = 1.0   # fully confirming

    # Strength scales with crowd confidence AND time relevance
    # Early: strong contrarian signal when crowd is very confident
    # Late: strong confirming signal when crowd is very confident
    strength = crowd_confidence * 0.7  # max 0.7 strength

    if time_factor < 0.4:
        # Early — contrarian
        if crowd_yes > 0.65:
            # Crowd heavily YES early → lean NO
            yes_prob = 0.5 - (crowd_yes - 0.65) * 0.8
            yes_prob = max(yes_prob, 0.25)
            reason = f"Crowd {round(crowd_yes*100)}% YES early → contrarian NO lean"
        elif crowd_yes < 0.35:
            # Crowd heavily NO early → lean YES
            yes_prob = 0.5 + (0.35 - crowd_yes) * 0.8
            yes_prob = min(yes_prob, 0.75)
            reason = f"Crowd {round(crowd_yes*100)}% YES early → contrarian YES lean"
        else:
            yes_prob = 0.5
            reason = f"Crowd neutral early ({round(crowd_yes*100)}%)"
            strength = 0.1
    else:
        # Late — confirming
        # Follow the crowd: high crowd YES → our YES prob goes up
        yes_prob = 0.5 + (crowd_yes - 0.5) * time_factor
        yes_prob = max(0.1, min(0.9, yes_prob))
        direction = "YES" if crowd_yes > 0.5 else "NO"
        reason = f"Crowd {round(crowd_yes*100)}% YES late → confirming {direction}"

    return yes_prob, reason, strength

def _position_safety(strike_price, strike_type, mins_remaining):
    """How safe is BTC's current position vs the strike."""
    price = btc_state["price"]
    if not price or not strike_price:
        return 0.0, None, 0.0

    try:
        strike = float(strike_price)
    except (TypeError, ValueError):
        return 0.0, None, 0.0

    distance = abs(price - strike)
    expected_move = strike * 0.0015 * (max(mins_remaining, 0.1) ** 0.5)
    safety = distance / expected_move if expected_move > 0 else 0.0

    btc_above = price > strike
    if strike_type and "less" in str(strike_type).lower():
        safe_side = "yes" if not btc_above else "no"
    else:
        safe_side = "yes" if btc_above else "no"

    return safety, safe_side, price - strike

# ── Main signal aggregator ────────────────────────────────────────────────

def get_signal(strike_price=None, strike_type=None, mins_remaining=None,
               yes_ask=None, no_ask=None):
    """
    Returns full signal analysis.
    
    Signals:
      Liquidations (35%)      — independent derivatives data
      Momentum (25%)          — BTC price direction
      Kalshi Distance (25%)   — BTC vs strike with time
      Crowd Confidence (15%)  — time-weighted contrarian/confirming
    """
    price = btc_state["price"]

    liq_p, liq_r, liq_s   = _liquidation_signal()
    mom_p, mom_r, mom_s   = _momentum_signal()
    kal_p, kal_r, kal_s   = _kalshi_distance_signal(strike_price, strike_type, mins_remaining)
    crd_p, crd_r, crd_s   = _crowd_confidence_signal(yes_ask, no_ask, mins_remaining)

    mtf_p, mtf_r, mtf_s = _multi_timeframe_signal()

    signals_list = [
        {"name": "Liquidations", "yes_prob": liq_p, "reason": liq_r, "weight": 0.25, "strength": liq_s},
        {"name": "Momentum",     "yes_prob": mom_p, "reason": mom_r, "weight": 0.20, "strength": mom_s},
        {"name": "Kalshi Dist",  "yes_prob": kal_p, "reason": kal_r, "weight": 0.25, "strength": kal_s},
        {"name": "Crowd",        "yes_prob": crd_p, "reason": crd_r, "weight": 0.15, "strength": crd_s},
        {"name": "Multi-TF",     "yes_prob": mtf_p, "reason": mtf_r, "weight": 0.15, "strength": mtf_s},
    ]

    # Weighted average
    total_weight = sum(s["weight"] * s["strength"] for s in signals_list)
    if total_weight > 0:
        our_yes_prob = sum(s["yes_prob"] * s["weight"] * s["strength"]
                         for s in signals_list) / total_weight
    else:
        our_yes_prob = 0.5

    # Signal agreement
    yes_votes = sum(s["strength"] for s in signals_list if s["yes_prob"] > 0.55)
    no_votes  = sum(s["strength"] for s in signals_list if s["yes_prob"] < 0.45)
    total_votes = yes_votes + no_votes
    if total_votes > 0:
        signal_agreement = abs(yes_votes - no_votes) / total_votes
        signal_direction = "yes" if yes_votes > no_votes else "no"
    else:
        signal_agreement = 0.0
        signal_direction = None

    # Position safety
    pos_safety, safe_side, distance = _position_safety(
        strike_price, strike_type, mins_remaining)

    # Time factor
    if mins_remaining is None:
        time_factor = 0.5
    else:
        m = max(mins_remaining, 0)
        if m >= 11:    time_factor = 0.2
        elif m >= 8:   time_factor = 0.3 + (11 - m) * 0.033
        elif m >= 4:   time_factor = 0.4 + (8 - m) * 0.075
        elif m >= 2:   time_factor = 0.7 + (4 - m) * 0.1
        else:          time_factor = 1.0

    return {
        "price": price,
        "our_yes_prob": our_yes_prob,
        "our_no_prob": 1 - our_yes_prob,
        "confidence": total_weight,
        "signals": signals_list,
        "pos_safety": pos_safety,
        "safe_side": safe_side,
        "distance": distance,
        "signal_agreement": signal_agreement,
        "signal_direction": signal_direction,
        "time_factor": time_factor,
    }


def get_candles_context():
    """Return current multi-timeframe context for dashboard display."""
    return {
        "trend_1h":   candles_cache["trend_1h"],
        "trend_6h":   candles_cache["trend_6h"],
        "trend_24h":  candles_cache["trend_24h"],
        "range_pct":  candles_cache["weekly_range_pct"],
        "volatility": candles_cache["volatility"],
        "hourly_bias": candles_cache["hourly_bias"],
        "candles_loaded": len(candles_cache["candles"]) > 0,
    }
