import time
import threading
import json
import websocket

# ── BTC STATE ─────────────────────────────────────
btc_state = {
    "price": None,
    "price_history": [],
}

# ── PRICE FEED ────────────────────────────────────

def on_message(ws, message):
    data = json.loads(message)

    if data.get("type") == "ticker" and "price" in data:
        price = float(data["price"])
        now = time.time()

        btc_state["price"] = price
        btc_state["price_history"].append((now, price))

        # keep last 5 min
        cutoff = now - 300
        btc_state["price_history"] = [
            (t, p) for t, p in btc_state["price_history"] if t > cutoff
        ]


def on_error(ws, error):
    print("WebSocket error:", error)


def on_close(ws, *args):
    print("WebSocket closed — reconnecting...")
    time.sleep(3)
    start_price_feed()


def on_open(ws):
    print("Connected to Coinbase BTC feed")

    ws.send(json.dumps({
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channels": ["ticker"]
    }))


def start_price_feed():
    ws = websocket.WebSocketApp(
        "wss://ws-feed.exchange.coinbase.com",
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    ws.run_forever()


def start_feed_thread():
    threading.Thread(target=start_price_feed, daemon=True).start()


# ── BASIC SIGNALS ─────────────────────────────────

def _momentum_signal():
    history = btc_state["price_history"]
    price = btc_state["price"]

    if not price or len(history) < 10:
        return 0.5, "no data", 0.0

    now = time.time()
    older = [p for t, p in history if t <= now - 120]

    if not older:
        return 0.5, "no history", 0.0

    move = (price - older[-1]) / older[-1]

    if move > 0.001:
        return 0.65, "up momentum", 0.8
    elif move < -0.001:
        return 0.35, "down momentum", 0.8
    else:
        return 0.5, "flat", 0.2


def _position_safety(strike_price, strike_type, mins_remaining):
    price = btc_state["price"]

    if not price or not strike_price:
        return 0.0, None, 0.0

    strike = float(strike_price)
    distance = price - strike

    safe_side = "yes" if distance > 0 else "no"

    return abs(distance), safe_side, distance


# ── MAIN SIGNAL ───────────────────────────────────

def get_signal(strike_price=None, strike_type=None, mins_remaining=None,
               yes_ask=None, no_ask=None):

    price = btc_state["price"]

    mom_p, mom_r, mom_s = _momentum_signal()

    signals_list = [
        {
            "name": "Momentum",
            "yes_prob": mom_p,
            "reason": mom_r,
            "weight": 1.0,
            "strength": mom_s
        }
    ]

    # weighted avg
    total_weight = sum(s["weight"] * s["strength"] for s in signals_list)

    if total_weight > 0:
        our_yes_prob = sum(
            s["yes_prob"] * s["weight"] * s["strength"]
            for s in signals_list
        ) / total_weight
    else:
        our_yes_prob = 0.5

    # agreement
    signal_agreement = 1.0 if mom_s > 0.5 else 0.0
    signal_direction = "yes" if our_yes_prob > 0.5 else "no"

    # safety
    pos_safety, safe_side, distance = _position_safety(
        strike_price, strike_type, mins_remaining
    )

    # time factor
    if mins_remaining is None:
        time_factor = 0.5
    else:
        time_factor = max(0.1, min(1.0, (15 - mins_remaining) / 15))

    return {
        "price": price,
        "our_yes_prob": our_yes_prob,
        "our_no_prob": 1 - our_yes_prob,
        "confidence": total_weight,

        # REQUIRED KEYS (this fixes your errors)
        "signals": signals_list,
        "pos_safety": pos_safety,
        "safe_side": safe_side,
        "distance": distance,
        "signal_agreement": signal_agreement,
        "signal_direction": signal_direction,
        "time_factor": time_factor,
    }