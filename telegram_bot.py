"""
BTC.KILLER — Telegram Bot
Controls the trading bot from your phone via Telegram.

Uses pure requests (no python-telegram-bot library needed).
Reads token and allowed users from bot_config.json.

Commands / inline buttons:
  /start        — Show main menu
  /status       — Bot status, market, conviction
  /position     — Current open position
  /pnl          — P&L and win rate from Kalshi
  /startbot     — Start the trading bot
  /stopbot      — Stop the trading bot
  /abort        — Sell current position
  /mode         — Change trading mode
  /settings     — Show current settings
  /help         — List all commands
"""

import time
import json
import requests
import threading
from pathlib import Path


# ── Telegram API helpers ──────────────────────────────────────────────────

class TelegramBot:
    def __init__(self, token, allowed_users, shared_state, state_lock):
        self.token     = token
        self.allowed   = set(str(u) for u in allowed_users)
        self.state     = shared_state
        self.lock      = state_lock
        self.base_url  = f"https://api.telegram.org/bot{token}"
        self.offset    = 0
        self.running   = True
        self.dashboard = "http://localhost:5050"

    def _send(self, chat_id, text, reply_markup=None, parse_mode="Markdown"):
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json=payload, timeout=10
            )
        except Exception as e:
            print(f"[Telegram] Send error: {e}")

    def _answer_callback(self, callback_query_id, text=""):
        try:
            requests.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=5
            )
        except Exception:
            pass

    def _get_updates(self):
        try:
            r = requests.get(
                f"{self.base_url}/getUpdates",
                params={"timeout": 25, "offset": self.offset},
                timeout=30
            )
            return r.json().get("result", [])
        except Exception as e:
            print(f"[Telegram] Poll error: {e}")
            return []

    def _allowed_check(self, user_id):
        """Return True if user is in allowlist. Empty allowlist = allow all (not recommended)."""
        if not self.allowed:
            return True
        return str(user_id) in self.allowed

    # ── Dashboard API calls ────────────────────────────────────────────
    def _api(self, endpoint, method="GET", data=None):
        try:
            url = f"{self.dashboard}{endpoint}"
            if method == "POST":
                r = requests.post(url, json=data or {}, timeout=5)
            else:
                r = requests.get(url, timeout=5)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    # ── Menus ──────────────────────────────────────────────────────────
    def _main_menu(self):
        return {
            "inline_keyboard": [
                [{"text": "📊 Status",       "callback_data": "status"},
                 {"text": "📍 Position",      "callback_data": "position"}],
                [{"text": "💰 P&L",           "callback_data": "pnl"},
                 {"text": "📈 Market",         "callback_data": "market"}],
                [{"text": "▶ Start Bot",       "callback_data": "startbot"},
                 {"text": "⏹ Stop Bot",        "callback_data": "stopbot"}],
                [{"text": "🎯 Change Mode",    "callback_data": "mode_menu"},
                 {"text": "⚙️ Settings",       "callback_data": "settings"}],
                [{"text": "🚨 ABORT Position", "callback_data": "abort_confirm"}],
            ]
        }

    def _mode_menu(self):
        return {
            "inline_keyboard": [
                [{"text": "🧠 Smart (Selective)",   "callback_data": "set_mode_selective"},
                 {"text": "⚖️ Smart (Balanced)",    "callback_data": "set_mode_balanced"}],
                [{"text": "🔥 Smart (Aggressive)",  "callback_data": "set_mode_aggressive"},
                 {"text": "💥 Always Buy",           "callback_data": "set_mode_always"}],
                [{"text": "« Back",                  "callback_data": "main_menu"}],
            ]
        }

    def _abort_confirm_menu(self):
        return {
            "inline_keyboard": [
                [{"text": "✅ YES — Sell it now", "callback_data": "abort_execute"},
                 {"text": "❌ Cancel",             "callback_data": "main_menu"}],
            ]
        }

    # ── Response builders ──────────────────────────────────────────────
    def _status_text(self):
        with self.lock:
            s = dict(self.state)
        bot_running = s.get("bot_running", False)
        bot_status  = s.get("bot_status",  "idle")
        market      = s.get("market_ticker", "—")
        secs        = s.get("secs_remaining") or 0
        mins        = secs / 60 if secs else 0
        yes_ask     = s.get("yes_ask") or 0
        no_ask      = s.get("no_ask") or 0
        conv        = s.get("conviction") or 0
        conv_dir    = s.get("conviction_direction") or "—"
        balance     = s.get("balance")
        btc         = s.get("btc_price")
        watching_dir = s.get("bot_watching_direction")
        watching_max = s.get("bot_watching_max_price")

        status_icon = "🟢" if bot_running else "🔴"
        status_text = "RUNNING" if bot_running else "STOPPED"
        if s.get("killed"):
            status_icon = "⛔"
            status_text = "KILLED (loss limit)"

        btc_str = f"${btc:,.2f}" if btc else "—"
        bal_str = f"${balance:.2f}" if balance else "—"

        lines = [
            f"*BTC.KILLER Status*",
            f"",
            f"{status_icon} Bot: *{status_text}*",
            f"💵 BTC: `{btc_str}`",
            f"💰 Balance: `{bal_str}`",
            f"",
            f"📈 Market: `{market}`",
            f"⏱ Time left: `{mins:.1f} min`",
            f"",
            f"YES ask: `{round(yes_ask*100)}¢`  |  NO ask: `{round(no_ask*100)}¢`",
            f"🎯 Conviction: `{round(conv*100)}% {conv_dir.upper()}`",
            f"🤖 Bot state: `{bot_status.upper()}`",
        ]
        if bot_status == "watching" and watching_dir:
            max_c = round((watching_max or 0) * 100)
            lines.append(f"⏳ Watching: `{watching_dir.upper()} < {max_c}¢`")
        return "\n".join(lines)

    def _position_text(self):
        with self.lock:
            s = dict(self.state)
        pos = s.get("current_position")
        market = s.get("market_ticker")
        secs   = s.get("secs_remaining") or 0

        if not pos or pos.get("ticker") != market or secs <= 0:
            return "📍 *No open position for current market.*"

        side      = (pos.get("side") or "").upper()
        price_c   = round((pos.get("price") or 0) * 100)
        contracts = pos.get("contracts") or 0
        cost      = pos.get("cost") or 0
        mins      = secs / 60

        icon = "🟢" if pos.get("side") == "yes" else "🔴"
        return (
            f"📍 *Active Position*\n\n"
            f"{icon} Side: `{side}`\n"
            f"📦 Contracts: `{contracts}`\n"
            f"💲 Buy price: `{price_c}¢`\n"
            f"💸 Total cost: `${cost:.2f}`\n"
            f"⏱ Time left: `{mins:.1f} min`\n\n"
            f"Potential win: `${contracts * (1 - (pos.get('price') or 0)):.2f}`"
        )

    def _pnl_text(self):
        with self.lock:
            s = dict(self.state)
        pnl     = s.get("kalshi_pnl_dollars")
        pnl_pct = s.get("kalshi_pnl_pct")
        wr      = s.get("kalshi_win_rate")
        wins    = s.get("kalshi_wins", 0)
        losses  = s.get("kalshi_losses", 0)
        period  = s.get("pnl_period", "day")

        if pnl is None:
            pnl = s.get("pnl_today", 0)
            wins    = s.get("wins_today", 0)
            losses  = s.get("losses_today", 0)
            source = "_(local estimate)_"
        else:
            source = "_(from Kalshi)_"

        pnl_sign = "+" if (pnl or 0) >= 0 else ""
        pct_str  = f" ({pnl_sign}{pnl_pct:.1f}%)" if pnl_pct is not None else ""
        wr_str   = f"{wr:.0f}%" if wr is not None else "—"
        total    = wins + losses
        pnl_icon = "💚" if (pnl or 0) > 0 else "🔴" if (pnl or 0) < 0 else "⬜"

        return (
            f"💰 *P&L — {period.upper()}* {source}\n\n"
            f"{pnl_icon} P&L: `{pnl_sign}${abs(pnl or 0):.2f}{pct_str}`\n"
            f"🏆 Win rate: `{wr_str}` ({wins}W / {losses}L / {total} total)\n"
        )

    def _market_text(self):
        with self.lock:
            s = dict(self.state)
        market   = s.get("market_ticker", "—")
        secs     = s.get("secs_remaining") or 0
        yes_ask  = s.get("yes_ask") or 0
        no_ask   = s.get("no_ask") or 0
        yes_ev   = s.get("yes_ev") or 0
        no_ev    = s.get("no_ev") or 0
        target   = s.get("target_price")
        tdir     = s.get("target_dir", "—")
        btc      = s.get("btc_price")
        safety   = s.get("pos_safety") or 0
        safe_side = s.get("safe_side") or "—"

        btc_str    = f"${btc:,.2f}" if btc else "—"
        target_str = f"${int(target):,}" if target else "—"
        mins       = secs / 60

        return (
            f"📈 *Current Market*\n\n"
            f"Ticker: `{market}`\n"
            f"⏱ Time left: `{mins:.1f} min`\n"
            f"🎯 Target: `{target_str}` ({tdir})\n"
            f"💵 BTC: `{btc_str}`\n\n"
            f"YES ask: `{round(yes_ask*100)}¢`  EV: `{yes_ev*100:+.1f}¢`\n"
            f"NO ask:  `{round(no_ask*100)}¢`  EV: `{no_ev*100:+.1f}¢`\n\n"
            f"Position safety: `{safety:.2f}x`  Safe side: `{safe_side.upper()}`"
        )

    def _settings_text(self):
        with self.lock:
            cfg = dict(self.state.get("settings", {}))
        return (
            f"⚙️ *Current Settings*\n\n"
            f"Mode: `{cfg.get('mode','—').upper()}`\n"
            f"Wager: `{cfg.get('wager_mode','—')}` — max `${cfg.get('max_session_wager',0):.2f}` / market\n"
            f"Min bet: `${cfg.get('min_bet',0):.2f}`\n"
            f"Daily loss limit: `${cfg.get('daily_loss_limit',0):.2f}`\n"
            f"Trigger time: `{cfg.get('trigger_time',5.0):.1f} min`\n"
            f"Early buy: `{'ON' if cfg.get('allow_early_buy') else 'OFF'}`\n"
        )

    # ── Command handlers ───────────────────────────────────────────────
    def handle_command(self, chat_id, cmd):
        cmd = cmd.lower().split()[0].lstrip('/')

        if cmd == "start" or cmd == "menu":
            self._send(chat_id, "🎰 *BTC.KILLER* — Choose an action:", self._main_menu())

        elif cmd == "status":
            self._send(chat_id, self._status_text(), self._main_menu())

        elif cmd == "position":
            self._send(chat_id, self._position_text(), self._main_menu())

        elif cmd == "pnl":
            self._send(chat_id, self._pnl_text(), self._main_menu())

        elif cmd == "market":
            self._send(chat_id, self._market_text(), self._main_menu())

        elif cmd == "startbot":
            self.state["_bot_control"] = "start"
            self._send(chat_id, "▶️ *Bot started!*", self._main_menu())

        elif cmd == "stopbot":
            self.state["_bot_control"] = "stop"
            self._send(chat_id, "⏹ *Bot stopped.*", self._main_menu())

        elif cmd == "abort":
            self._send(chat_id, "⚠️ *Are you sure you want to sell your current position?*",
                       self._abort_confirm_menu())

        elif cmd == "mode":
            self._send(chat_id, "🎯 *Choose trading mode:*", self._mode_menu())

        elif cmd == "settings":
            self._send(chat_id, self._settings_text(), self._main_menu())

        elif cmd == "help":
            help_text = (
                "*BTC.KILLER Bot Commands*\n\n"
                "/status — Bot status overview\n"
                "/position — Current position\n"
                "/pnl — P&L and win rate\n"
                "/market — Current market info\n"
                "/startbot — Start trading bot\n"
                "/stopbot — Stop trading bot\n"
                "/abort — Sell current position\n"
                "/mode — Change trading mode\n"
                "/settings — View settings\n"
                "/help — This message"
            )
            self._send(chat_id, help_text)
        else:
            self._send(chat_id, "Unknown command. Try /help or /start")

    def handle_callback(self, chat_id, callback_query_id, data):
        self._answer_callback(callback_query_id)

        if data == "main_menu":
            self._send(chat_id, "🎰 *BTC.KILLER* — Choose an action:", self._main_menu())

        elif data == "status":
            self._send(chat_id, self._status_text(), self._main_menu())

        elif data == "position":
            self._send(chat_id, self._position_text(), self._main_menu())

        elif data == "pnl":
            self._send(chat_id, self._pnl_text(), self._main_menu())

        elif data == "market":
            self._send(chat_id, self._market_text(), self._main_menu())

        elif data == "settings":
            self._send(chat_id, self._settings_text(), self._main_menu())

        elif data == "startbot":
            self.state["_bot_control"] = "start"
            self._send(chat_id, "▶️ *Bot started!*", self._main_menu())

        elif data == "stopbot":
            self.state["_bot_control"] = "stop"
            self._send(chat_id, "⏹ *Bot stopped.*", self._main_menu())

        elif data == "mode_menu":
            self._send(chat_id, "🎯 *Choose trading mode:*", self._mode_menu())

        elif data.startswith("set_mode_"):
            new_mode = data.replace("set_mode_", "")
            result = self._api("/api/settings", "POST", {"mode": new_mode})
            self._send(chat_id, f"✅ Mode set to *{new_mode.upper()}*", self._main_menu())

        elif data == "abort_confirm":
            self._send(chat_id, "⚠️ *Confirm: Sell your current position?*",
                       self._abort_confirm_menu())

        elif data == "abort_execute":
            result = self._api("/api/abort", "POST")
            if result.get("ok"):
                self._send(chat_id, "🚨 *Position sold!*", self._main_menu())
            else:
                err = result.get("error", "unknown error")
                self._send(chat_id, f"❌ Abort failed: {err}", self._main_menu())

    # ── Main polling loop ──────────────────────────────────────────────
    def run(self):
        print(f"[Telegram] Bot starting — polling for updates...")
        while self.running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self.offset = update["update_id"] + 1

                    # Text message / command
                    if "message" in update:
                        msg     = update["message"]
                        user_id = msg.get("from", {}).get("id")
                        chat_id = msg.get("chat", {}).get("id")
                        text    = msg.get("text", "")

                        if not self._allowed_check(user_id):
                            self._send(chat_id, "⛔ You are not authorized to use this bot.")
                            continue

                        if text.startswith("/"):
                            self.handle_command(chat_id, text[1:])
                        else:
                            # Treat any message as a menu request
                            self._send(chat_id, "🎰 *BTC.KILLER* — Choose an action:", self._main_menu())

                    # Inline button press
                    elif "callback_query" in update:
                        cq      = update["callback_query"]
                        user_id = cq.get("from", {}).get("id")
                        chat_id = cq.get("message", {}).get("chat", {}).get("id")
                        data    = cq.get("data", "")
                        cq_id   = cq.get("id")

                        if not self._allowed_check(user_id):
                            self._answer_callback(cq_id, "⛔ Not authorized")
                            continue

                        self.handle_callback(chat_id, cq_id, data)

            except Exception as e:
                print(f"[Telegram] Loop error: {e}")
                time.sleep(5)

            time.sleep(0.5)


def run_telegram_bot(token, allowed_users, shared_state, state_lock):
    """Entry point — called from dashboard.py as a daemon thread."""
    bot = TelegramBot(token, allowed_users, shared_state, state_lock)
    bot.run()


if __name__ == "__main__":
    # Standalone test mode
    import sys
    from pathlib import Path
    cfg_path = Path(__file__).parent / "bot_config.json"
    if not cfg_path.exists():
        print("No bot_config.json found. Run the dashboard first.")
        sys.exit(1)
    cfg = json.load(open(cfg_path))
    token = cfg.get("telegram_token")
    users = cfg.get("telegram_allowed_users", [])
    if not token:
        print("No telegram_token in bot_config.json")
        sys.exit(1)
    state = {}
    lock  = threading.Lock()
    run_telegram_bot(token, users, state, lock)
