"""
scraper.py
----------
Tor-powered stealth scraper for Section 138 NI Act judgments
from Indian Kanoon (indiankanoon.org).

Features:
  - IP rotation via Tor stem (renew_tor_ip)
  - SOCKS5h proxy so DNS also goes through Tor
  - Duplicate-safe: skips files already saved to data/raw_html/
  - Rate-limit handling: on 429/403 renew IP + exponential backoff
  - Deterministic filenames based on case doc-id (no duplicates ever)
  - rclone sync to Google Drive after each batch (optional)

Usage:
    python scraper.py              # full run (all pages)
    python scraper.py --test 5     # test run: scrape first 5 cases only
    python scraper.py --no-tor     # skip Tor (use direct connection for testing)
"""

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from loguru import logger

# ── Tor imports (graceful fallback if stem not installed) ────────────────────
try:
    import socks
    from stem import Signal
    from stem.control import Controller
    TOR_AVAILABLE = True
except ImportError:
    TOR_AVAILABLE = False
    logger.warning("stem/PySocks not installed. Run with --no-tor.")

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()

RAW_HTML_DIR     = Path(os.getenv("RAW_HTML_DIR", "./data/raw_html"))
TOR_SOCKS_PORT   = int(os.getenv("TOR_SOCKS_PORT", 9050))
TOR_CONTROL_PORT = int(os.getenv("TOR_CONTROL_PORT", 9051))
RCLONE           = Path("./bin/rclone.exe") if os.name == "nt" else Path("rclone")

BASE_URL    = "https://indiankanoon.org"
SEARCH_PATH = "/search/"
QUERY       = "Section 138 Negotiable Instruments Act"

# Realistic browser headers — no origin-leaking fields
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://indiankanoon.org/",
    # Explicitly absent: X-Forwarded-For, X-Real-IP, Via, True-Client-IP
}

MAX_RETRIES   = 5
BACKOFF_BASE  = 10      # seconds; doubles on each retry
PAGE_DELAY    = (3, 7)  # random jitter range in seconds

# ── Tor helpers ───────────────────────────────────────────────────────────────

def renew_tor_ip() -> None:
    """Send NEWNYM signal to Tor to get a fresh exit node."""
    if not TOR_AVAILABLE:
        logger.warning("stem not available — cannot rotate IP")
        return
    try:
        with Controller.from_port(port=TOR_CONTROL_PORT) as ctrl:
            ctrl.authenticate()          # uses CookieAuthentication automatically
            ctrl.signal(Signal.NEWNYM)
        logger.info("Tor IP renewed (NEWNYM sent)")
        time.sleep(5)                    # Tor needs a moment to build a new circuit
    except Exception as exc:
        logger.error(f"renew_tor_ip failed: {exc}")


def jitter(lo: float = PAGE_DELAY[0], hi: float = PAGE_DELAY[1]) -> None:
    """Sleep for a random duration in [lo, hi] seconds."""
    duration = round(random.uniform(lo, hi), 2)
    logger.debug(f"Sleeping {duration}s")
    time.sleep(duration)


def get_real_ip(session: requests.Session) -> str:
    """Ask an external service what IP it sees for this session."""
    try:
        r = session.get("https://api.ipify.org?format=json", timeout=15)
        return r.json().get("ip", "unknown")
    except Exception:
        return "unknown"


def verify_tor_anonymity() -> None:
    """
    Confirm that Tor is actually routing traffic before any scraping begins.
    Compares the direct IP with the Tor-proxied IP.
    Aborts if they match (Tor not working) or if direct IP lookup fails.
    """
    logger.info("Verifying Tor anonymity...")

    direct_session = requests.Session()
    direct_session.headers.update(HEADERS)
    real_ip = get_real_ip(direct_session)

    proxy = f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}"
    tor_session = requests.Session()
    tor_session.headers.update(HEADERS)
    tor_session.proxies = {"http": proxy, "https": proxy}
    tor_ip = get_real_ip(tor_session)

    logger.info(f"Real IP   : {real_ip}")
    logger.info(f"Tor IP    : {tor_ip}")

    if real_ip == "unknown" or tor_ip == "unknown":
        logger.error("Could not determine IPs. Check internet + Tor connectivity.")
        raise SystemExit(1)

    if real_ip == tor_ip:
        logger.error(
            "Real IP and Tor IP are the same — Tor is NOT routing your traffic. "
            "Ensure Tor is running on port 9050 and retry."
        )
        raise SystemExit(1)

    logger.success(f"Anonymity confirmed. Scraping via Tor exit node {tor_ip}.")


def make_session(use_tor: bool) -> requests.Session:
    """Create a requests Session, optionally routed through Tor SOCKS5h."""
    session = requests.Session()
    session.headers.update(HEADERS)
    if use_tor:
        proxy = f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}"
        session.proxies = {"http": proxy, "https": proxy}
        logger.info(f"Session configured with Tor proxy ({proxy})")
    return session


# ── Deduplication helper ──────────────────────────────────────────────────────

def case_filepath(doc_id: str) -> Path:
    """Return the canonical save path for a given case doc_id."""
    return RAW_HTML_DIR / f"{doc_id}.html"


def already_scraped(doc_id: str) -> bool:
    return case_filepath(doc_id).exists()


# ── Fetch with retry ──────────────────────────────────────────────────────────

def fetch(session: requests.Session, url: str, use_tor: bool) -> requests.Response | None:
    """
    GET url with exponential backoff on 429/403.
    On rate-limit: renew Tor IP, wait, and retry up to MAX_RETRIES times.
    Returns None if all retries exhausted.
    """
    delay = BACKOFF_BASE
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp
            elif resp.status_code in (429, 403):
                logger.warning(
                    f"[{resp.status_code}] Rate-limited on {url} "
                    f"(attempt {attempt}/{MAX_RETRIES}). "
                    f"Renewing IP and waiting {delay}s..."
                )
                renew_tor_ip()
                time.sleep(delay)
                delay = min(delay * 2, 120)   # cap at 2 min
            elif resp.status_code == 404:
                logger.warning(f"[404] Not found: {url}")
                return None
            else:
                logger.warning(f"[{resp.status_code}] Unexpected status for {url}")
                time.sleep(delay)
                delay = min(delay * 2, 120)
        except requests.exceptions.RequestException as exc:
            logger.warning(f"Request error (attempt {attempt}): {exc}")
            time.sleep(delay)
            delay = min(delay * 2, 120)

    logger.error(f"All {MAX_RETRIES} retries exhausted for {url}")
    return None


# ── Search result parser ──────────────────────────────────────────────────────

def parse_search_results(html: str) -> list[dict]:
    """
    Extract case links and titles from an Indian Kanoon search results page.
    Returns list of dicts: [{"doc_id": "...", "title": "...", "url": "..."}]
    No duplicates — deduped by doc_id within this batch.
    """
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_ids = set()

    # Each result is wrapped in <article class="result">
    # Title:    h4.result_title > a  (text only; href goes to docfragment, not /doc/)
    # Full doc: div.hlbottom > a[href^='/doc/']
    for article in soup.select("article.result"):
        # Extract doc_id from the full-document link in hlbottom
        hlbottom = article.select_one("div.hlbottom a[href^='/doc/']")
        if not hlbottom:
            continue
        doc_id = re.search(r"/doc/(\d+)/", hlbottom["href"])
        if not doc_id:
            continue
        doc_id = doc_id.group(1)

        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)

        # Extract human-readable title
        title_tag = article.select_one("h4.result_title a")
        title = title_tag.get_text(strip=True) if title_tag else f"Case_{doc_id}"

        results.append({
            "doc_id": doc_id,
            "title":  title,
            "url":    BASE_URL + f"/doc/{doc_id}/",
        })

    return results


def get_next_page_url(html: str, current_pagenum: int) -> str | None:
    """Extract the URL of the next search results page, or None if last page."""
    soup = BeautifulSoup(html, "lxml")
    next_link = soup.find("a", string=re.compile(r"next", re.I))
    if next_link and next_link.get("href"):
        return BASE_URL + next_link["href"]
    # Fallback: construct next page URL manually
    next_pagenum = current_pagenum + 10     # Indian Kanoon uses &pagenum=0,10,20...
    return None   # caller handles manual pagination


# ── Save case HTML ────────────────────────────────────────────────────────────

def save_case(doc_id: str, html: str, meta: dict) -> None:
    """
    Write HTML + meta to local staging directory.
    sync_to_gdrive() handles batch upload at the end of each page.
    """
    html_path = case_filepath(doc_id)
    meta_path = RAW_HTML_DIR / f"{doc_id}.meta.json"

    if html_path.exists():
        logger.debug(f"[SKIP] {doc_id} already saved locally")
        return

    html_path.write_text(html, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.success(f"[SAVED] {doc_id} — {meta.get('title', '')[:60]}")


# ── rclone batch sync ──────────────────────────────────────────────────────────

def sync_to_gdrive() -> None:
    """
    Batch-upload all local HTML/JSON files to GDrive in one rclone call,
    then delete the local copies. Called once per search page (10 cases).
    This is far faster than one rclone process per file.
    """
    files = list(RAW_HTML_DIR.glob("*.html")) + list(RAW_HTML_DIR.glob("*.json"))
    if not files:
        return

    logger.info(f"Batch uploading {len(files)} files to GDrive...")
    result = subprocess.run(
        [str(RCLONE), "copy", str(RAW_HTML_DIR), "lsse_gdrive:LSSE/raw_html",
         "--tpslimit", "10", "--drive-chunk-size", "64M",
         "--include", "*.html", "--include", "*.json"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        for f in files:
            f.unlink(missing_ok=True)
        logger.success(f"Batch uploaded {len(files)} files — local copies cleared")
    else:
        logger.warning(f"Batch upload failed: {result.stderr.strip()}")


# ── Main scrape loop ──────────────────────────────────────────────────────────

def scrape(limit: int | None = None, use_tor: bool = True) -> None:
    """
    Main scraping loop.

    Args:
        limit:   Max number of cases to scrape. None = unlimited.
        use_tor: Route traffic through Tor SOCKS proxy.
    """
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)

    if use_tor:
        verify_tor_anonymity()          # hard-stop if Tor not actually routing

    session     = make_session(use_tor)
    total_saved = 0
    pagenum     = 0         # Indian Kanoon pagination offset

    logger.info(f"Starting scrape | query='{QUERY}' | limit={limit} | tor={use_tor}")

    while True:
        # ── 1. Fetch search results page ─────────────────────────────────────
        search_url = (
            f"{BASE_URL}{SEARCH_PATH}"
            f"?formInput={urllib.parse.quote(QUERY)}"
            f"&type=judgments"
            f"&pagenum={pagenum}"
        )
        logger.info(f"Fetching search page {pagenum // 10 + 1}: {search_url}")

        resp = fetch(session, search_url, use_tor)
        if resp is None:
            logger.error("Search page fetch failed. Stopping.")
            break

        cases = parse_search_results(resp.text)
        if not cases:
            logger.info("No more results — scrape complete.")
            break

        # ── 2. Scrape each case on this page ──────────────────────────────────
        for case in cases:
            doc_id = case["doc_id"]

            # Deduplication: skip if already on disk
            if already_scraped(doc_id):
                logger.debug(f"[SKIP] {doc_id} already exists on disk")
                continue

            jitter()                    # randomised polite delay

            case_resp = fetch(session, case["url"], use_tor)
            if case_resp is None:
                logger.warning(f"[FAILED] Could not fetch {doc_id}")
                continue

            save_case(doc_id, case_resp.text, {
                "doc_id": doc_id,
                "title":  case["title"],
                "url":    case["url"],
            })
            total_saved += 1

            if limit and total_saved >= limit:
                logger.info(f"Limit of {limit} cases reached. Stopping.")
                sync_to_gdrive()
                return

        # ── 3. Sync batch to Google Drive ─────────────────────────────────────
        sync_to_gdrive()

        # ── 4. Advance to next search page ───────────────────────────────────
        pagenum += 10
        jitter(8, 15)                   # longer randomised pause between pages

    logger.info(f"Done. Total new cases saved: {total_saved}")
    sync_to_gdrive()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSSE Stealth Scraper")
    parser.add_argument(
        "--test", type=int, metavar="N",
        help="Test mode: scrape only N cases then stop"
    )
    parser.add_argument(
        "--no-tor", action="store_true",
        help="Disable Tor proxy (direct connection)"
    )
    args = parser.parse_args()

    use_tor = not args.no_tor
    if use_tor and not TOR_AVAILABLE:
        logger.error("Tor requested but stem/PySocks not installed. Use --no-tor.")
        raise SystemExit(1)

    scrape(limit=args.test, use_tor=use_tor)
