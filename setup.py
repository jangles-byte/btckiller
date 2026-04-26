"""
BTC.KILLER — Setup Wizard (terminal-based, no browser needed)
Run via: 1_SETUP.command
"""

import sys
import os
import json
import subprocess
import base64
import time
from pathlib import Path

BOT_DIR = Path(__file__).parent

# ── Terminal colors ───────────────────────────────────────────────────────

def c(text, code): return f"\033[{code}m{text}\033[0m"
def green(t):  return c(t, "92")
def yellow(t): return c(t, "93")
def red(t):    return c(t, "91")
def cyan(t):   return c(t, "96")
def bold(t):   return c(t, "1")
def dim(t):    return c(t, "2")

def header():
    print()
    print(bold(yellow("  ██████╗ ████████╗ ██████╗    ██╗  ██╗██╗██╗     ██╗     ███████╗██████╗ ")))
    print(bold(green( "  ██╔══██╗╚══██╔══╝██╔════╝    ██║ ██╔╝██║██║     ██║     ██╔════╝██╔══██╗")))
    print(bold(green( "  ██████╔╝   ██║   ██║         █████╔╝ ██║██║     ██║     █████╗  ██████╔╝")))
    print(bold(green( "  ██╔══██╗   ██║   ██║         ██╔═██╗ ██║██║     ██║     ██╔══╝  ██╔══██╗")))
    print(bold(green( "  ██████╔╝   ██║   ╚██████╗    ██║  ██╗██║███████╗███████╗███████╗██║  ██║")))
    print(bold(green( "  ╚═════╝    ╚═╝    ╚═════╝    ╚═╝  ╚═╝╚═╝╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝")))
    print()
    print(bold("  Setup Wizard") + dim("  —  takes about 2 minutes"))
    print(dim("  " + "─" * 60))
    print()

def step(n, title):
    print()
    print(bold(cyan(f"  Step {n}: {title}")))
    print(dim("  " + "─" * 40))

def ok(msg):   print(green(f"  ✓ {msg}"))
def warn(msg): print(yellow(f"  ⚠ {msg}"))
def err(msg):  print(red(f"  ✗ {msg}"))
def info(msg): print(dim(f"    {msg}"))

def ask(prompt, default=None, secret=False):
    """Prompt user for input. Returns stripped string."""
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            val = getpass.getpass(f"  → {prompt}{suffix}: ")
        else:
            val = input(f"  → {prompt}{suffix}: ").strip()
        if not val and default is not None:
            return default
        return val
    except (KeyboardInterrupt, EOFError):
        print()
        print(yellow("\n  Setup cancelled."))
        sys.exit(0)

def ask_yn(prompt, default="y"):
    val = ask(f"{prompt} (y/n)", default=default).lower()
    return val.startswith("y")

# ── Find .pem files ───────────────────────────────────────────────────────

def find_pem_files():
    """Look for .pem files in common locations."""
    search_dirs = [
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home(),
    ]
    found = []
    for d in search_dirs:
        if d.exists():
            for f in d.glob("*.pem"):
                found.append(f)
            for f in d.glob("*.key"):
                found.append(f)
    return found

def pick_pem_file():
    """Let user pick their .pem file. Returns Path or None."""
    pem_files = find_pem_files()

    if pem_files:
        print()
        print(f"  Found {len(pem_files)} key file(s):")
        for i, f in enumerate(pem_files):
            size = f.stat().st_size
            print(f"  {bold(str(i+1))}. {f.name}  {dim(str(f.parent))}  {dim(f'{size}B')}")
        print(f"  {bold(str(len(pem_files)+1))}. Enter path manually")
        print()
        while True:
            choice = ask(f"Which file? (1-{len(pem_files)+1})").strip()
            try:
                n = int(choice)
                if 1 <= n <= len(pem_files):
                    return pem_files[n - 1]
                elif n == len(pem_files) + 1:
                    break
            except ValueError:
                pass
            warn("Enter a number from the list.")
    else:
        info("No .pem files found in Downloads, Desktop, Documents, or home folder.")

    # Manual path entry
    print()
    info("Enter the full path to your .pem file.")
    info("Tip: drag the file from Finder into this Terminal window to get the path.")
    print()
    while True:
        raw = ask("Path to .pem file")
        # strip quotes that macOS sometimes adds when dragging into terminal
        raw = raw.strip().strip("'\"")
        p = Path(raw).expanduser()
        if p.exists():
            return p
        err(f"File not found: {p}")
        if not ask_yn("Try again?"):
            return None

# ── Python detection ──────────────────────────────────────────────────────

def find_python():
    for cmd in ["python3", "python3.12", "python3.11", "python3.10", "python3.9"]:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                ver = (r.stdout.strip() or r.stderr.strip()).split()[-1].split(".")
                if len(ver) >= 2 and int(ver[0]) == 3 and int(ver[1]) >= 9:
                    return cmd
        except Exception:
            continue
    return None

def run_cmd(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           cwd=cwd or str(BOT_DIR), timeout=180)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)

# ── Kalshi connection test ────────────────────────────────────────────────

def test_kalshi(api_key, pem_bytes):
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import urllib.request

        pk  = serialization.load_pem_private_key(pem_bytes, password=None)
        ts  = str(int(time.time() * 1000))
        msg = f"{ts}GET/trade-api/v2/portfolio/balance".encode()
        sig = pk.sign(msg,
                      padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                  salt_length=padding.PSS.MAX_LENGTH),
                      hashes.SHA256())
        headers = {
            "KALSHI-ACCESS-KEY":       api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }
        req = urllib.request.Request(
            "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance",
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            bal  = data.get("balance", 0) / 100
            return True, f"Connected! Balance: ${bal:.2f}"
    except Exception as e:
        return False, str(e)

# ── Main wizard ───────────────────────────────────────────────────────────

def main():
    header()

    # ── Step 1: Python ────────────────────────────────────────────────────
    step(1, "Checking Python")
    py = find_python()
    if not py:
        err("Python 3.9 or later is required.")
        info("Download from: https://www.python.org/downloads/")
        info("After installing, run this setup again.")
        sys.exit(1)
    ok(f"Found {py}")

    # ── Step 2: Kalshi API Key ID ─────────────────────────────────────────
    step(2, "Kalshi API Key ID")
    info("Get this from: kalshi.com → Profile → API Keys → Create Key")
    print()
    api_key = ""
    while not api_key:
        api_key = ask("Kalshi API Key ID").strip()
        if not api_key:
            warn("API Key ID is required.")

    # ── Step 3: Private key (.pem) ────────────────────────────────────────
    step(3, "Kalshi Private Key (.pem file)")
    info("This is the .pem file you downloaded when creating your API key.")
    info("It's usually in your Downloads folder.")

    pem_path = pick_pem_file()
    if not pem_path:
        err("No .pem file selected. Cannot continue.")
        sys.exit(1)

    try:
        pem_bytes = pem_path.read_bytes()
        ok(f"Loaded: {pem_path.name}")
    except Exception as e:
        err(f"Could not read file: {e}")
        sys.exit(1)

    # ── Step 4: Coinalyze ─────────────────────────────────────────────────
    step(4, "Coinalyze API Key")
    info("Free account at coinalyze.net → API Keys")
    info("Used for liquidation signals.")
    print()
    coinalyze = ""
    while not coinalyze:
        coinalyze = ask("Coinalyze API Key").strip()
        if not coinalyze:
            warn("Coinalyze key is required.")

    # ── Step 5: Telegram (optional) ───────────────────────────────────────
    step(5, "Telegram Bot (optional — press Enter to skip)")
    info("Message @BotFather on Telegram → /newbot → get your token.")
    info("Get your User ID from @userinfobot.")
    print()
    tg_token = ask("Telegram Bot Token", default="").strip()
    tg_users = []
    if tg_token:
        raw_users = ask("Your Telegram User ID(s), comma-separated", default="").strip()
        tg_users = [u.strip() for u in raw_users.split(",") if u.strip()]

    # ── Step 6: Install packages ──────────────────────────────────────────
    step(6, "Installing packages")

    venv_path = BOT_DIR / "venv"
    if not venv_path.exists():
        info("Creating virtual environment...")
        ok_flag, out = run_cmd(f"{py} -m venv venv")
        if ok_flag:
            ok("Virtual environment created.")
        else:
            warn(f"venv failed, using system Python. ({out[:60]})")

    venv_py  = venv_path / "bin" / "python3"
    venv_pip = venv_path / "bin" / "pip"
    if venv_py.exists():
        pip_cmd = str(venv_pip)
        py_cmd  = str(venv_py)
    else:
        pip_cmd = f"{py} -m pip"
        py_cmd  = py

    req_file = BOT_DIR / "requirements.txt"
    info("Installing dependencies...")
    if req_file.exists():
        ok_flag, out = run_cmd(f"{pip_cmd} install -r \"{req_file}\" --quiet")
    else:
        ok_flag, out = run_cmd(f"{pip_cmd} install flask python-dotenv requests websocket-client cryptography --quiet")

    if ok_flag:
        ok("Packages installed.")
    else:
        warn(f"Some packages may have issues: {out[:80]}")

    # ── Step 7: Test Kalshi connection ────────────────────────────────────
    step(7, "Testing Kalshi connection")
    info("Connecting to Kalshi API...")

    # Need cryptography installed first — use venv python for the test
    conn_ok, conn_msg = False, "cryptography not yet importable"
    try:
        conn_ok, conn_msg = test_kalshi(api_key, pem_bytes)
    except ImportError:
        # cryptography was just installed — run test in a subprocess with venv
        test_script = (
            "import sys, json, base64, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).parent))\n"
            "from setup import test_kalshi\n"
            f"pem = open({str(pem_path)!r},'rb').read()\n"
            f"ok, msg = test_kalshi({api_key!r}, pem)\n"
            "print('OK:' + msg if ok else 'ERR:' + msg)\n"
        )
        tmp = BOT_DIR / ".test_conn.py"
        tmp.write_text(test_script)
        r = subprocess.run([py_cmd, str(tmp)], capture_output=True, text=True, timeout=15)
        tmp.unlink(missing_ok=True)
        out = (r.stdout + r.stderr).strip()
        if out.startswith("OK:"):
            conn_ok, conn_msg = True, out[3:]
        elif out.startswith("ERR:"):
            conn_ok, conn_msg = False, out[4:]
        else:
            conn_ok, conn_msg = False, out or "No response"

    if conn_ok:
        ok(conn_msg)
    else:
        warn(f"Connection test: {conn_msg[:100]}")
        warn("Continuing setup — double-check your API Key ID if trading fails.")

    # ── Step 8: Write config files ────────────────────────────────────────
    step(8, "Saving configuration")

    # Copy .pem to bot directory
    dest_pem = BOT_DIR / "kalshi_private_key.pem"
    dest_pem.write_bytes(pem_bytes)
    ok(f"Private key saved → {dest_pem.name}")

    # Write .env
    env_content = (
        f"KALSHI_API_KEY_ID={api_key}\n"
        f"KALSHI_PRIVATE_KEY_PATH={dest_pem}\n"
        f"COINALYZE_API_KEY={coinalyze}\n"
        f"DAILY_LOSS_LIMIT=50\n"
        f"MAX_CONTRACTS_PER_TRADE=200\n"
    )
    (BOT_DIR / ".env").write_text(env_content)
    ok(".env file written.")

    # Write bot_config.json
    cfg = {
        "mode": "balanced",
        "daily_loss_limit": 50.0,
        "max_session_wager": 5.0,
        "wager_mode": "dollar",
        "wager_pct": 10.0,
        "trigger_time": 5.0,
        "trigger_method": "ev",
        "allow_early_buy": True,
        "early_max_price": 0.75,
        "min_bet": 0.50,
        "min_bet_pct": 1.0,
        "loss_period": "daily",
        "telegram_enabled": bool(tg_token),
        "telegram_token": tg_token,
        "telegram_allowed_users": tg_users,
    }
    (BOT_DIR / "bot_config.json").write_text(json.dumps(cfg, indent=2))
    ok("Settings saved.")

    # Update the dashboard launcher with correct python path
    launcher = BOT_DIR / "2_START_DASHBOARD.command"
    launcher.write_text(
        "#!/bin/bash\n"
        'cd "$(dirname "$0")"\n'
        'echo ""\n'
        'echo "  Starting BTC.KILLER dashboard..."\n'
        'echo "  Open http://localhost:5050 in your browser"\n'
        'echo ""\n'
        f'"{py_cmd}" dashboard.py\n'
    )
    try:
        os.chmod(launcher, 0o755)
    except Exception:
        pass
    ok("Launcher updated.")

    # ── Done ──────────────────────────────────────────────────────────────
    print()
    print(dim("  " + "─" * 60))
    print()
    print(bold(green("  ✅  SETUP COMPLETE!")))
    print()
    print(f"  To start trading:")
    print(f"  {bold('Double-click  2_START_DASHBOARD.command')}")
    print(f"  Then open  {cyan('http://localhost:5050')}  in your browser.")
    print()
    print(dim("  You can adjust bet sizes and loss limits in the dashboard."))
    print()

    # Offer to launch now
    if ask_yn("Launch the dashboard now?"):
        print()
        info("Starting dashboard...")
        subprocess.Popen([py_cmd, str(BOT_DIR / "dashboard.py")], cwd=str(BOT_DIR))
        time.sleep(2)
        import webbrowser
        webbrowser.open("http://localhost:5050")
        print()
        ok("Dashboard launched → http://localhost:5050")
        print()
        print(dim("  (Keep this terminal window open while the bot is running)"))

    print()


if __name__ == "__main__":
    main()
