"""
ꓘALSHIKILLER — Telegram Bot  (multi-coin edition)
====================================================
Full dashboard mirror via Telegram inline keyboards.
All changes call the dashboard REST API → reflected live on the web dashboard.

No external library needed — uses only `requests`.

Run via: launched as a daemon thread from dashboard.py
Commands: /start  /status  /cancel  /help
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("tg_bot")

DASHBOARD = "http://localhost:5050"
COINS     = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
MODES     = ["balanced", "selective", "aggressive", "always"]

# ── Settings metadata: key → (label, type, min, max, unit) ───────────────────
# Keys ending in _pct or _fp are virtual — converted to/from fractions on save.
COIN_EDIT_KEYS = {
    # Dollar mode wager
    "wager":               ("Min Wager $",        "float", 0.01, 500,   "$"),
    "max_wager":           ("Max Wager $",         "float", 0.01, 1000,  "$"),
    # Pct mode wager  (virtual: stored as wager_pct_min / wager_pct)
    "wager_pct_min":       ("Min Wager %",         "float", 0.01, 100,   "%"),
    "wager_pct":           ("Max Wager %",         "float", 0.01, 100,   "%"),
    # Price / timing
    "always_max_price_pct":("Max Buy Price",       "int",   1,    99,    "¢"),  # virtual → always_max_price /100
    "buy_window_open":     ("Buy Window Open",     "float", 0,    15,    "m"),
    "buy_window_close":    ("Buy Window Close",    "float", 0,    14,    "m"),
    # Signal / edge
    "min_ev_edge_pct":     ("Min Edge",            "int",   0,    30,    "%"),  # virtual → min_ev_edge /100
    "signal_agreement":    ("Signal Agreement",    "int",   1,    8,     ""),
    # TP / SL
    "take_profit_cents":   ("Take Profit at",      "int",   50,   99,    "¢"),
    "stop_loss_cents":     ("Stop Loss at",        "int",   1,    49,    "¢"),
    # Longshot
    "penny_wager":         ("Longshot Wager",      "float", 0.01, 50,    "$"),
    "penny_stop_mins":     ("Longshot Stop",       "float", 0,    14,    "m"),
    # Kelly
    "kelly_fraction_pct":  ("Kelly Fraction",      "int",   1,    100,   "%"),  # virtual → kelly_fraction /100
    # Loss cooldown
    "consec_loss_trigger": ("Loss Trigger",        "int",   1,    20,    " L"),
    "cooldown_markets":    ("Cooldown Markets",    "int",   1,    20,    " mkts"),
}
COIN_TOGGLE_KEYS = {
    "take_profit_on":    "Take Profit",
    "stop_loss_on":      "Stop Loss",
    "penny_enabled":     "Longshot",
    "kelly_enabled":     "Kelly Sizing",
    "hourly_loss_enabled": "Loss Cooldown",
}
GLOBAL_EDIT_KEYS = {
    "hard_stop_floor":  ("Hard Stop Floor",  "float", 0,    100000, "$"),
    "daily_loss_limit": ("Daily Loss Limit", "float", 1,    9999,   "$"),
}
GLOBAL_TOGGLE_KEYS = {
    "hard_stop_enabled":   "Hard Stop",
    "inverse_bet_enabled": "Inverse Bet",
    "telegram_enabled":    "TG Alerts",
}

# ── Per-chat user state ───────────────────────────────────────────────────────
# {chat_id: {"mode":"idle"|"edit", "coin":str|None, "key":str,
#            "scope":"coin"|"global", "prompt_msg_id":int|None}}
_state: dict = {}
_state_lock  = threading.Lock()

# Accumulate edits per (chat_id, coin) until "Save & Apply" pressed
_pending: dict = {}   # (chat_id, coin) → {key: value}

# Thread control
_stop_event = threading.Event()
_token      = ""
_allowed_ids: list = []   # empty = allow all chat IDs


# ── Telegram API ──────────────────────────────────────────────────────────────

_LONG_POLL_SECS = 20   # Telegram server-side wait
_REQ_TIMEOUT    = (10, _LONG_POLL_SECS + 10)  # (connect, read) — read > long-poll

def _tg(method: str, _read_timeout: int = 12, **kw) -> dict:
    """POST to Telegram Bot API.  Use _read_timeout=35 for getUpdates."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_token}/{method}",
            json=kw, timeout=(10, _read_timeout),
        )
        return r.json()
    except Exception as e:
        log.warning("TG %s error: %s", method, e)
        return {}


def _send(chat_id, text, kb=None) -> dict:
    kw = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb:
        kw["reply_markup"] = {"inline_keyboard": kb}
    return _tg("sendMessage", **kw)


def _edit_msg(chat_id, msg_id, text, kb=None) -> dict:
    kw = {"chat_id": chat_id, "message_id": msg_id,
          "text": text, "parse_mode": "HTML"}
    if kb:
        kw["reply_markup"] = {"inline_keyboard": kb}
    return _tg("editMessageText", **kw)


def _answer(cbq_id, text=""):
    _tg("answerCallbackQuery", callback_query_id=cbq_id, text=text)


def _delete_msg(chat_id, msg_id):
    _tg("deleteMessage", chat_id=chat_id, message_id=msg_id)


# ── Dashboard REST API ────────────────────────────────────────────────────────

def _api(path, method="GET", body=None) -> dict:
    try:
        if method == "GET":
            r = requests.get(DASHBOARD + path, timeout=7)
        else:
            r = requests.post(DASHBOARD + path, json=body or {}, timeout=7)
        return r.json()
    except Exception as e:
        log.warning("Dashboard %s error: %s", path, e)
        return {}


# ── Formatting ────────────────────────────────────────────────────────────────

def _pnl(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    return ("+$" if f >= 0 else "-$") + f"{abs(f):.2f}"


def _sess() -> str:
    h = datetime.now(timezone.utc).hour
    if h < 8:   return "🌏 ASIA"
    if h < 14:  return "🌍 EU"
    return "🌎 US"


def _icon(on) -> str:
    return "✅" if on else "⬜"


def _run_icon(running) -> str:
    return "🟢" if running else "⚫"


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _kb_main() -> list:
    row1 = [{"text": c, "callback_data": f"c:{c}"} for c in COINS[:4]]
    row2 = [{"text": c, "callback_data": f"c:{c}"} for c in COINS[4:]]
    return [
        [{"text": "🏠 Portfolio Status",   "callback_data": "home:status"}],
        row1, row2,
        [{"text": "🛑 Stop ALL Bots",      "callback_data": "home:stopall"},
         {"text": "⚙️ Global Settings",    "callback_data": "global:menu"}],
    ]


def _kb_coin(coin) -> list:
    return [
        [{"text": "📊 Status",            "callback_data": f"c:{coin}:status"},
         {"text": "📋 Trades",            "callback_data": f"c:{coin}:trades"}],
        [{"text": "▶ Start Bot",          "callback_data": f"c:{coin}:start"},
         {"text": "⏹ Stop Bot",           "callback_data": f"c:{coin}:stop"}],
        [{"text": "⚙️ Settings",           "callback_data": f"c:{coin}:settings"},
         {"text": "🚨 Abort Position",    "callback_data": f"c:{coin}:abort_confirm"}],
        [{"text": "← Main Menu",          "callback_data": "home:menu"}],
    ]


def _kb_abort_confirm(coin) -> list:
    return [
        [{"text": "✅ YES — Abort now", "callback_data": f"c:{coin}:abort_execute"},
         {"text": "❌ Cancel",          "callback_data": f"c:{coin}:back"}],
    ]


def _kb_settings(coin, cfg) -> list:
    """Settings keyboard — each row shows current value, tap to edit."""
    is_pct = cfg.get("wager_mode", "dollar") == "pct"

    def edit_row(key):
        label, typ, mn, mx, unit = COIN_EDIT_KEYS[key]
        # Virtual keys: convert from stored fraction → display integer
        if key == "min_ev_edge_pct":
            raw = round(float(cfg.get("min_ev_edge", 0.05)) * 100)
        elif key == "always_max_price_pct":
            raw = round(float(cfg.get("always_max_price", 0.75)) * 100)
        elif key == "kelly_fraction_pct":
            raw = round(float(cfg.get("kelly_fraction", 0.25)) * 100)
        else:
            raw = cfg.get(key, "—")
        if isinstance(raw, float):
            raw = f"{raw:.2f}"
        return [{"text": f"{label}: {raw}{unit}  ✏️",
                 "callback_data": f"c:{coin}:set:edit:{key}"}]

    def tog_row(key):
        on = cfg.get(key, False)
        if key == "take_profit_on" and key not in cfg:
            on = bool(cfg.get("take_profit_cents", 0))
        if key == "stop_loss_on" and key not in cfg:
            on = bool(cfg.get("stop_loss_cents", 0))
        label = COIN_TOGGLE_KEYS[key]
        return [{"text": f"{label}: {_icon(on)}  (tap to toggle)",
                 "callback_data": f"c:{coin}:set:tog:{key}"}]

    rows = []

    # Mode cycle button
    cur_mode = cfg.get("mode", "balanced")
    rows.append([{"text": f"🎯 Mode: {cur_mode.upper()}  (tap to cycle)",
                  "callback_data": f"c:{coin}:set:cycle:mode"}])

    # Wager — mode-aware
    if is_pct:
        rows.append(edit_row("wager_pct_min"))
        rows.append(edit_row("wager_pct"))
    else:
        rows.append(edit_row("wager"))
        rows.append(edit_row("max_wager"))

    rows += [
        edit_row("always_max_price_pct"),
        edit_row("buy_window_open"),
        edit_row("buy_window_close"),
        edit_row("min_ev_edge_pct"),
        edit_row("signal_agreement"),
    ]

    # Take profit
    rows.append(tog_row("take_profit_on"))
    if cfg.get("take_profit_on") or cfg.get("take_profit_cents", 0):
        rows.append(edit_row("take_profit_cents"))

    # Stop loss
    rows.append(tog_row("stop_loss_on"))
    if cfg.get("stop_loss_on") or cfg.get("stop_loss_cents", 0):
        rows.append(edit_row("stop_loss_cents"))

    # Longshot
    rows.append(tog_row("penny_enabled"))
    if cfg.get("penny_enabled"):
        rows.append(edit_row("penny_wager"))
        rows.append(edit_row("penny_stop_mins"))

    # Kelly
    rows.append(tog_row("kelly_enabled"))
    if cfg.get("kelly_enabled"):
        rows.append(edit_row("kelly_fraction_pct"))

    # Loss cooldown
    rows.append(tog_row("hourly_loss_enabled"))
    if cfg.get("hourly_loss_enabled"):
        rows.append(edit_row("consec_loss_trigger"))
        rows.append(edit_row("cooldown_markets"))

    rows.append([
        {"text": "💾 Save & Apply",       "callback_data": f"c:{coin}:set:save"},
        {"text": "📋 Apply to All Coins", "callback_data": f"c:{coin}:set:applyall"},
    ])
    rows.append([{"text": "← Back", "callback_data": f"c:{coin}:back"}])
    return rows


def _kb_global(gcfg) -> list:
    hs_on = gcfg.get("hard_stop_enabled", False)
    fl    = gcfg.get("hard_stop_floor", 0)
    dl    = gcfg.get("daily_loss_limit", 50)
    ib_on = gcfg.get("inverse_bet_enabled", False)
    tg_on = gcfg.get("telegram_enabled", False)
    return [
        [{"text": f"🛑 Hard Stop: {_icon(hs_on)}",
          "callback_data": "global:tog:hard_stop_enabled"}],
        [{"text": f"   Floor: ${float(fl):.2f}  ✏️",
          "callback_data": "global:edit:hard_stop_floor"}],
        [{"text": f"📉 Daily Loss Limit: ${float(dl):.0f}  ✏️",
          "callback_data": "global:edit:daily_loss_limit"}],
        [{"text": f"🔄 Inverse Bet: {_icon(ib_on)}",
          "callback_data": "global:tog:inverse_bet_enabled"}],
        [{"text": f"📢 TG Alerts: {_icon(tg_on)}",
          "callback_data": "global:tog:telegram_enabled"}],
        [{"text": "💾 Save",        "callback_data": "global:save"},
         {"text": "← Main Menu",   "callback_data": "home:menu"}],
    ]


# ── Content builders ──────────────────────────────────────────────────────────

def _text_home() -> str:
    d     = _api("/api/home")
    coins = d.get("coins", [])
    bal   = f"${d.get('balance') or 0:.2f}"
    sp    = _pnl(d.get("session_pnl"))
    tot_t = (d.get("total_wins") or 0) + (d.get("total_losses") or 0)
    active= sum(1 for c in coins if c.get("running"))

    lines = [
        f"<b>🔫 ꓘALSHIKILLER</b>  {_sess()}",
        f"💰 Balance: <b>{bal}</b>  |  Session: <b>{sp}</b>",
        f"🤖 Active: <b>{active}/{len(COINS)}</b>  |  Trades today: <b>{tot_t}</b>",
        "",
    ]
    for c in coins:
        pr  = f"${float(c.get('price') or 0):,.0f}" if c.get("price") else "—"
        wr  = f"{c.get('win_rate'):.0f}%" if c.get("win_rate") is not None else "—%"
        lines.append(
            f"{_run_icon(c.get('running'))} <b>{c['coin']}</b>  {pr}  "
            f"P&L:{_pnl(c.get('pnl'))}  "
            f"W/L:{c.get('wins',0)}/{c.get('losses',0)}  WR:{wr}"
        )
    return "\n".join(lines)


def _text_coin(coin) -> str:
    d       = _api(f"/api/state?coin={coin}")
    running = d.get("bot_running", False)
    price   = f"${float(d.get('coin_price') or 0):,.2f}"
    ya      = d.get("yes_ask")
    na      = d.get("no_ask")
    secs    = d.get("secs_remaining")
    pos     = d.get("current_position") or {}
    bal     = d.get("balance")

    tm = "—"
    if secs is not None:
        m, s = divmod(int(secs), 60)
        tm = f"{m}m {s:02d}s"

    pos_str = "FLAT"
    if pos and (pos.get("contracts", 0) or pos.get("position_fp", 0)):
        side = (pos.get("side") or "?").upper()
        qty  = abs(pos.get("contracts") or pos.get("position_fp") or 0)
        ep   = pos.get("price") or pos.get("entry") or 0
        pos_str = f"▲ {side} {qty} @ {ep}¢"

    wins   = d.get("wins_today", 0)
    losses = d.get("losses_today", 0)
    wr     = f"{wins/(wins+losses)*100:.0f}%" if wins+losses > 0 else "—"

    return "\n".join([
        f"<b>{_run_icon(running)} {coin} — {'RUNNING' if running else 'IDLE'}</b>",
        f"💰 Price: {price}  |  Balance: {'$'+f'{bal:.2f}' if bal else '—'}",
        f"📈 P&L: {_pnl(d.get('pnl_today'))}  |  W:{wins} L:{losses}  WR:{wr}",
        f"📋 Position: {pos_str}",
        f"⏱ Market: {d.get('market_ticker','—')}",
        f"   Rem: {tm}  |  YES:{ya}¢  NO:{na}¢" if ya else f"   Rem: {tm}",
    ])


def _text_settings(coin, cfg) -> str:
    is_pct = cfg.get("wager_mode", "dollar") == "pct"
    wmode  = "Percent (%)" if is_pct else "Dollar ($)"

    if is_pct:
        minw_s = f"{cfg.get('wager_pct_min', 1):.1f}%"
        maxw_s = f"{cfg.get('wager_pct', 10):.1f}%"
    else:
        minw_s = f"${float(cfg.get('wager', 1)):.2f}"
        maxw_s = f"${float(cfg.get('max_wager', 10)):.2f}"

    edge    = round(float(cfg.get("min_ev_edge") or 0) * 100)
    sa      = cfg.get("signal_agreement", 3)
    mp      = round(float(cfg.get("always_max_price") or 0.75) * 100)
    bw_o    = cfg.get("buy_window_open",  14)
    bw_c    = cfg.get("buy_window_close", 0.5)

    tp_on   = cfg.get("take_profit_on", bool(cfg.get("take_profit_cents", 0)))
    tp_v    = cfg.get("take_profit_cents", 85)
    sl_on   = cfg.get("stop_loss_on", bool(cfg.get("stop_loss_cents", 0)))
    sl_v    = cfg.get("stop_loss_cents", 15)

    ls_on   = cfg.get("penny_enabled", False)
    ls_w    = cfg.get("penny_wager", 1)
    ls_m    = cfg.get("penny_stop_mins", 2)

    kelly   = cfg.get("kelly_enabled", False)
    kf      = round(float(cfg.get("kelly_fraction", 0.25)) * 100)

    hl_on   = cfg.get("hourly_loss_enabled", False)
    cl_t    = cfg.get("consec_loss_trigger", 3)
    cd_m    = cfg.get("cooldown_markets", 3)

    bot_mode = cfg.get("mode", "balanced").upper()

    lines = [
        f"<b>⚙️ {coin} Settings</b>",
        f"  Bot mode: <b>{bot_mode}</b>",
        f"  Wager mode: <b>{wmode}</b>  |  Wager: <b>{minw_s} → {maxw_s}</b>",
        f"  Max buy: <b>{mp}¢</b>  |  Window: <b>{bw_o}m → {bw_c}m</b>",
        f"  Min edge: <b>{edge}%</b>  |  Signal agree: <b>{sa}</b>",
        f"  Take profit: <b>{'ON @ '+str(tp_v)+'¢' if tp_on else 'OFF'}</b>  "
        f"Stop loss: <b>{'ON @ '+str(sl_v)+'¢' if sl_on else 'OFF'}</b>",
        f"  Longshot: <b>{'ON ($'+str(ls_w)+', stop@'+str(ls_m)+'m)' if ls_on else 'OFF'}</b>",
        f"  Kelly: <b>{'ON '+str(kf)+'%' if kelly else 'OFF'}</b>  "
        f"Loss cooldown: <b>{'ON ('+str(cl_t)+'L → sit '+str(cd_m)+'mkts)' if hl_on else 'OFF'}</b>",
        "",
        "<i>Tap a row below to edit. Press 💾 to save.</i>",
    ]
    return "\n".join(lines)


def _text_trades(coin) -> str:
    rows = _api(f"/api/trades?coin={coin}")
    if not isinstance(rows, list) or not rows:
        return f"<b>{coin}</b> — No trades found."
    recent = rows[-10:][::-1]
    lines  = [f"<b>📋 {coin} — Last {len(recent)} trades</b>\n"]
    for t in recent:
        side = (t.get("action") or t.get("side") or "?").upper()
        ts   = (t.get("timestamp") or t.get("time") or "")[:16].replace("T", " ")
        pnl_raw = t.get("pnl", "")
        pnl_s = ""
        try:
            if pnl_raw and pnl_raw not in ("pending", "unfilled", "expired", ""):
                pv = float(pnl_raw)
                pnl_s = (" +$" if pv >= 0 else " -$") + f"{abs(pv):.2f}"
        except Exception:
            pass
        price = t.get("price") or t.get("cent") or "?"
        qty   = t.get("qty") or t.get("contracts") or "?"
        code  = t.get("trade_code", "")
        code_s = f"\n  <code>{code}</code>" if code else ""
        lines.append(f"  {ts}  {side}  {qty}@{price}¢{pnl_s}{code_s}")
    return "\n".join(lines)


# ── State helpers ─────────────────────────────────────────────────────────────

def _gs(chat_id) -> dict:
    with _state_lock:
        return dict(_state.get(chat_id, {"mode": "idle"}))


def _ss(chat_id, **kw):
    with _state_lock:
        _state[chat_id] = kw


def _cs(chat_id):
    with _state_lock:
        _state.pop(chat_id, None)


def _pend(chat_id, coin) -> dict:
    return _pending.setdefault((chat_id, coin), {})


def _flush(chat_id, coin):
    """Merge pending edits into live settings and save via API."""
    cfg     = _api(f"/api/settings?coin={coin}")
    patches = _pending.pop((chat_id, coin), {})
    cfg.update(patches)
    # Convert virtual fraction keys → real stored values
    if "min_ev_edge_pct" in patches:
        cfg["min_ev_edge"] = patches["min_ev_edge_pct"] / 100.0
        cfg.pop("min_ev_edge_pct", None)
    if "always_max_price_pct" in patches:
        cfg["always_max_price"] = patches["always_max_price_pct"] / 100.0
        cfg.pop("always_max_price_pct", None)
    if "kelly_fraction_pct" in patches:
        cfg["kelly_fraction"] = patches["kelly_fraction_pct"] / 100.0
        cfg.pop("kelly_fraction_pct", None)
    # Infer booleans from cents values
    if "take_profit_cents" in patches and patches["take_profit_cents"]:
        cfg["take_profit_on"] = True
    if "stop_loss_cents" in patches and patches["stop_loss_cents"]:
        cfg["stop_loss_on"] = True
    _api(f"/api/settings?coin={coin}", "POST", cfg)
    return cfg


# ── Handlers ─────────────────────────────────────────────────────────────────

def _on_command(chat_id, text):
    cmd = text.strip().split()[0].lstrip("/").lower()
    if cmd in ("start", "menu"):
        _send(chat_id, "<b>🔫 ꓘALSHIKILLER</b>\nSelect an option:", _kb_main())
    elif cmd == "status":
        _send(chat_id, _text_home(), _kb_main())
    elif cmd == "cancel":
        _cs(chat_id)
        _send(chat_id, "❌ Cancelled.", _kb_main())
    elif cmd == "help":
        _send(chat_id,
            "<b>Commands</b>\n"
            "/start  — Main menu\n"
            "/status — Portfolio snapshot\n"
            "/cancel — Cancel current input\n"
            "/help   — This message\n\n"
            "<i>Use the buttons in the menu for full control.</i>")
    else:
        _send(chat_id, "Try /start for the main menu.")


def _on_text_input(chat_id, text):
    """Handle free-text reply when user is in 'edit' mode."""
    st = _gs(chat_id)
    if st.get("mode") != "edit":
        return

    coin       = st.get("coin")
    key        = st.get("key")
    scope      = st.get("scope", "coin")
    pmid       = st.get("prompt_msg_id")

    if pmid:
        _delete_msg(chat_id, pmid)

    try:
        val_str = text.strip()

        if scope == "coin":
            meta = COIN_EDIT_KEYS.get(key)
            if not meta:
                _send(chat_id, "⚠️ Unknown setting. /cancel to abort.")
                return
            label, typ, mn, mx, unit = meta
            val = float(val_str) if typ == "float" else int(float(val_str))
            if not (mn <= val <= mx):
                _send(chat_id, f"⚠️ Must be between {mn} and {mx}{unit}. Try again or /cancel")
                return
            _pend(chat_id, coin)[key] = val
            cfg = _api(f"/api/settings?coin={coin}")
            cfg.update(_pend(chat_id, coin))
            _cs(chat_id)
            _send(chat_id,
                  f"✅ <b>{label}</b> → <b>{val}{unit}</b>\n"
                  f"<i>Not saved yet — press 💾 Save &amp; Apply to confirm.</i>",
                  _kb_settings(coin, cfg))

        elif scope == "global":
            meta = GLOBAL_EDIT_KEYS.get(key)
            if not meta:
                _send(chat_id, "⚠️ Unknown setting. /cancel to abort.")
                return
            label, typ, mn, mx, unit = meta
            val = float(val_str) if typ == "float" else int(float(val_str))
            if not (mn <= val <= mx):
                _send(chat_id, f"⚠️ Must be between {mn} and {mx}{unit}. Try again or /cancel")
                return
            gcfg = _api("/api/global_settings")
            gcfg[key] = val
            _api("/api/global_settings", "POST", gcfg)
            _cs(chat_id)
            _send(chat_id,
                  f"✅ <b>{label}</b> → <b>{val}{unit}</b> (saved & live on dashboard)",
                  _kb_global(gcfg))

    except (ValueError, TypeError):
        _send(chat_id, "⚠️ Invalid value. Enter a number, or /cancel")


def _on_callback(chat_id, msg_id, cbq_id, data):
    _answer(cbq_id)
    p = data.split(":")

    # ── HOME / PORTFOLIO ──────────────────────────────────────────────────────
    if p[0] == "home":
        action = p[1] if len(p) > 1 else "menu"
        if action == "menu":
            _edit_msg(chat_id, msg_id,
                      "<b>🔫 ꓘALSHIKILLER</b>\nSelect an option:", _kb_main())
        elif action == "status":
            _edit_msg(chat_id, msg_id, _text_home(), _kb_main())
        elif action == "stopall":
            for coin in COINS:
                _api("/api/bot/stop", "POST", {"coin": coin})
            _edit_msg(chat_id, msg_id,
                      "🛑 <b>All bots stopped.</b>",
                      [[{"text": "← Main Menu", "callback_data": "home:menu"}]])

    # ── GLOBAL SETTINGS ───────────────────────────────────────────────────────
    elif p[0] == "global":
        action = p[1] if len(p) > 1 else "menu"
        gcfg   = _api("/api/global_settings")

        if action == "menu":
            _edit_msg(chat_id, msg_id, "<b>⚙️ Global Settings</b>", _kb_global(gcfg))

        elif action == "tog":
            key = p[2]
            gcfg[key] = not gcfg.get(key, False)
            _api("/api/global_settings", "POST", gcfg)
            _edit_msg(chat_id, msg_id, "<b>⚙️ Global Settings</b>", _kb_global(gcfg))

        elif action == "edit":
            key  = p[2]
            meta = GLOBAL_EDIT_KEYS.get(key)
            if not meta:
                return
            label, typ, mn, mx, unit = meta
            cur = gcfg.get(key, "—")
            resp = _send(chat_id,
                         f"✏️ <b>Editing {label}</b>\n"
                         f"Current: <b>{cur}{unit}</b>  Range: {mn}–{mx}{unit}\n\n"
                         f"Reply with new value (or /cancel):")
            pmid = (resp.get("result") or {}).get("message_id")
            _ss(chat_id, mode="edit", coin=None, key=key, scope="global", prompt_msg_id=pmid)

        elif action == "save":
            _api("/api/global_settings", "POST", gcfg)
            _edit_msg(chat_id, msg_id, "💾 <b>Global settings saved.</b>", _kb_global(gcfg))

    # ── COIN ──────────────────────────────────────────────────────────────────
    elif p[0] == "c":
        if len(p) < 2:
            return
        coin = p[1]

        # coin button → coin menu
        if len(p) == 2:
            _edit_msg(chat_id, msg_id,
                      f"<b>{coin}</b> — choose action:", _kb_coin(coin))
            return

        action = p[2]

        if action == "back":
            _edit_msg(chat_id, msg_id,
                      f"<b>{coin}</b> — choose action:", _kb_coin(coin))

        elif action == "status":
            _edit_msg(chat_id, msg_id, _text_coin(coin), _kb_coin(coin))

        elif action == "trades":
            _edit_msg(chat_id, msg_id, _text_trades(coin),
                      [[{"text": "← Back", "callback_data": f"c:{coin}:back"}]])

        elif action == "start":
            _api("/api/bot/start", "POST", {"coin": coin})
            time.sleep(0.6)
            _edit_msg(chat_id, msg_id,
                      f"▶ <b>{coin}</b> bot started.", _kb_coin(coin))

        elif action == "stop":
            _api("/api/bot/stop", "POST", {"coin": coin})
            time.sleep(0.6)
            _edit_msg(chat_id, msg_id,
                      f"⏹ <b>{coin}</b> bot stopped.", _kb_coin(coin))

        elif action == "abort_confirm":
            _edit_msg(chat_id, msg_id,
                      f"⚠️ Abort position for <b>{coin}</b>?",
                      _kb_abort_confirm(coin))

        elif action == "abort_execute":
            result = _api("/api/abort", "POST", {"coin": coin})
            if result.get("ok"):
                _edit_msg(chat_id, msg_id,
                          f"🚨 <b>{coin}</b> position aborted.", _kb_coin(coin))
            else:
                err = result.get("error", "no open position")
                _edit_msg(chat_id, msg_id,
                          f"❌ Abort failed: {err}", _kb_coin(coin))

        elif action == "settings":
            cfg = _api(f"/api/settings?coin={coin}")
            _edit_msg(chat_id, msg_id,
                      _text_settings(coin, cfg), _kb_settings(coin, cfg))

        elif action == "set":
            if len(p) < 5:
                return
            sub = p[3]
            key = p[4]

            if sub == "tog":
                cfg = _api(f"/api/settings?coin={coin}")
                cur = cfg.get(key, False)
                # infer from cents if flag not explicit
                if key == "take_profit_on" and key not in cfg:
                    cur = bool(cfg.get("take_profit_cents", 0))
                if key == "stop_loss_on" and key not in cfg:
                    cur = bool(cfg.get("stop_loss_cents", 0))
                cfg[key] = not cur
                if key == "take_profit_on" and not cfg[key]:
                    cfg["take_profit_cents"] = 0
                if key == "stop_loss_on" and not cfg[key]:
                    cfg["stop_loss_cents"] = 0
                _api(f"/api/settings?coin={coin}", "POST", cfg)
                _edit_msg(chat_id, msg_id,
                          _text_settings(coin, cfg), _kb_settings(coin, cfg))

            elif sub == "cycle":
                # Cycle through a fixed option list (currently just mode)
                cfg = _api(f"/api/settings?coin={coin}")
                if key == "mode":
                    cur = cfg.get("mode", "balanced")
                    nxt = MODES[(MODES.index(cur) + 1) % len(MODES)] if cur in MODES else MODES[0]
                    cfg["mode"] = nxt
                    _pend(chat_id, coin)["mode"] = nxt
                _edit_msg(chat_id, msg_id,
                          _text_settings(coin, cfg), _kb_settings(coin, cfg))

            elif sub == "edit":
                meta = COIN_EDIT_KEYS.get(key)
                if not meta:
                    return
                label, typ, mn, mx, unit = meta
                cfg = _api(f"/api/settings?coin={coin}")
                # Virtual fraction keys → display as integer
                if key == "min_ev_edge_pct":
                    cur = round(float(cfg.get("min_ev_edge", 0.05)) * 100)
                elif key == "always_max_price_pct":
                    cur = round(float(cfg.get("always_max_price", 0.75)) * 100)
                elif key == "kelly_fraction_pct":
                    cur = round(float(cfg.get("kelly_fraction", 0.25)) * 100)
                else:
                    cur = cfg.get(key, "—")
                if isinstance(cur, float):
                    cur = f"{cur:.2f}"
                resp = _send(chat_id,
                             f"✏️ <b>Editing {label}</b> for <b>{coin}</b>\n"
                             f"Current: <b>{cur}{unit}</b>  Range: {mn}–{mx}{unit}\n\n"
                             f"Reply with the new value (or /cancel):")
                pmid = (resp.get("result") or {}).get("message_id")
                _ss(chat_id, mode="edit", coin=coin, key=key, scope="coin",
                    prompt_msg_id=pmid)

            elif sub == "save":
                cfg = _flush(chat_id, coin)
                _edit_msg(chat_id, msg_id,
                          f"💾 <b>{coin}</b> settings saved &amp; live on dashboard.",
                          _kb_coin(coin))

            elif sub == "applyall":
                # Save the current coin's pending edits first, then push to all others
                cfg = _flush(chat_id, coin)
                other_coins = [c for c in COINS if c != coin]
                failed = []
                for oc in other_coins:
                    try:
                        _api(f"/api/settings?coin={oc}", "POST", cfg)
                    except Exception:
                        failed.append(oc)
                if failed:
                    result_txt = (f"💾 <b>{coin}</b> saved. Applied to: "
                                  f"{', '.join(c for c in other_coins if c not in failed)}.\n"
                                  f"⚠️ Failed for: {', '.join(failed)}.")
                else:
                    result_txt = (f"📋 <b>{coin}</b> settings applied to all coins: "
                                  f"{', '.join(other_coins)}.")
                _edit_msg(chat_id, msg_id, result_txt, _kb_coin(coin))


# ── Polling loop ──────────────────────────────────────────────────────────────

def _dispatch(upd, allowed_ids):
    if "callback_query" in upd:
        cbq     = upd["callback_query"]
        chat_id = cbq["message"]["chat"]["id"]
        msg_id  = cbq["message"]["message_id"]
        cbq_id  = cbq["id"]
        data    = cbq.get("data", "")
        if allowed_ids and chat_id not in allowed_ids:
            _answer(cbq_id, "⛔ Unauthorised")
            return
        _on_callback(chat_id, msg_id, cbq_id, data)
        return

    if "message" in upd:
        msg     = upd["message"]
        chat_id = msg["chat"]["id"]
        text    = msg.get("text", "")
        if not text:
            return
        if allowed_ids and chat_id not in allowed_ids:
            _send(chat_id, "⛔ Unauthorised")
            return
        if text.startswith("/"):
            _on_command(chat_id, text)
        else:
            st = _gs(chat_id)
            if st.get("mode") == "edit":
                _on_text_input(chat_id, text)
            else:
                _send(chat_id, "Use /start for the menu.")


def _run_bot(allowed_ids):
    log.info("Telegram bot polling started.")
    offset = 0
    while not _stop_event.is_set():
        try:
            # timeout= is the Telegram server-side wait (seconds).
            # _read_timeout= must be LONGER so requests doesn't close first.
            resp    = _tg("getUpdates",
                          _read_timeout=_LONG_POLL_SECS + 10,
                          offset=offset,
                          timeout=_LONG_POLL_SECS,
                          limit=20)
            updates = resp.get("result", [])
        except Exception as e:
            log.warning("getUpdates error: %s", e)
            time.sleep(4)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                _dispatch(upd, allowed_ids)
            except Exception as e:
                log.exception("Dispatch error: %s", e)
    log.info("Telegram bot stopped.")


# ── Public API ────────────────────────────────────────────────────────────────

_bot_thread = None


def start(token: str, allowed_chat_ids=None):
    """Launch (or restart) the bot polling thread."""
    global _token, _allowed_ids, _bot_thread
    _stop_event.set()
    if _bot_thread and _bot_thread.is_alive():
        _bot_thread.join(timeout=3)
    _stop_event.clear()
    _token       = token
    _allowed_ids = [int(i) for i in (allowed_chat_ids or []) if str(i).strip()]
    _bot_thread  = threading.Thread(
        target=_run_bot, args=(_allowed_ids,), daemon=True, name="tg_bot")
    _bot_thread.start()
    log.info("Telegram bot started with token …%s", token[-6:] if token else "?")


def stop():
    """Stop the polling thread."""
    _stop_event.set()


def send_alert(token: str, chat_id, text: str):
    """One-off alert from bot.py (trade notifications, hard stop, etc.)."""
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log.warning("Alert send failed: %s", e)
