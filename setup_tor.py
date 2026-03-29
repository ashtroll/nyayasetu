"""
setup_tor.py
------------
Prints step-by-step instructions to configure Tor for IP rotation,
then verifies the Tor SOCKS proxy is reachable before the scraper runs.

Usage:
    python setup_tor.py
"""

import sys
import socket

TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9050
TOR_CONTROL_PORT = 9051

TORRC_BLOCK = """
# ── Paste these lines into your torrc file ───────────────────────────────────
ControlPort 9051
CookieAuthentication 1
# (Optional) faster startup by caching the consensus
DataDirectory /var/lib/tor
# ─────────────────────────────────────────────────────────────────────────────
"""

WINDOWS_STEPS = """
HOW TO INSTALL & CONFIGURE TOR ON WINDOWS
==========================================

1. Download the Tor Expert Bundle (not Tor Browser) from:
   https://www.torproject.org/download/tor/

2. Extract it. Inside you will find:
     tor.exe      - the daemon
     torrc.sample - example config

3. Create (or edit) the torrc file next to tor.exe:
   - Copy torrc.sample -> torrc
   - Add the following lines:
{torrc}

4. Start Tor:
     tor.exe -f torrc

   OR install it as a Windows service:
     tor.exe --service install -options -f "C:\\path\\to\\torrc"
     net start tor

5. Verify Tor is running:
     netstat -ano | findstr 9050
     netstat -ano | findstr 9051
   Both ports should show LISTENING.

6. Come back here and re-run:
     python setup_tor.py
   It will confirm the connection.
""".format(torrc=TORRC_BLOCK)

LINUX_STEPS = """
HOW TO INSTALL & CONFIGURE TOR ON LINUX / WSL
==============================================

1. Install Tor:
     sudo apt update && sudo apt install -y tor

2. Edit /etc/tor/torrc  (requires sudo):
     sudo nano /etc/tor/torrc
   Add / uncomment these lines:
{torrc}

3. Restart Tor:
     sudo systemctl restart tor
     sudo systemctl enable tor

4. Verify:
     sudo systemctl status tor
     ss -tlnp | grep 9050
""".format(torrc=TORRC_BLOCK)


def check_tor_socks():
    """Return True if Tor SOCKS port is accepting connections."""
    try:
        s = socket.create_connection((TOR_SOCKS_HOST, TOR_SOCKS_PORT), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def check_tor_control():
    """Return True if Tor ControlPort is accepting connections."""
    try:
        s = socket.create_connection((TOR_SOCKS_HOST, TOR_CONTROL_PORT), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def main():
    print("=" * 60)
    print("  LSSE — Tor Setup Checker")
    print("=" * 60)

    socks_ok   = check_tor_socks()
    control_ok = check_tor_control()

    print(f"\n  SOCKS proxy  (port {TOR_SOCKS_PORT})  : {'OK' if socks_ok   else 'NOT REACHABLE'}")
    print(f"  Control port (port {TOR_CONTROL_PORT}) : {'OK' if control_ok else 'NOT REACHABLE'}")

    if socks_ok and control_ok:
        print("\n  [OK] Tor is running and ready. You can now run scraper.py.")
        return

    print("\n  Tor is not running or not configured correctly.")
    print("  Follow the instructions for your OS:\n")

    if sys.platform.startswith("win"):
        print(WINDOWS_STEPS)
    else:
        print(LINUX_STEPS)

    print("  After following the steps, re-run:  python setup_tor.py")


if __name__ == "__main__":
    main()
