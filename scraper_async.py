"""
scraper_async.py
----------------
Async multi-Tor scraper for Indian Kanoon (runs alongside api_fetcher.py).

Architecture:
  - asyncio event loop with N worker coroutines (one per Tor instance)
  - Each worker has its own httpx.AsyncClient tunnelled through a different
    SOCKS5h port, so requests are distributed across N independent Tor circuits
  - On 429/403: send NEWNYM to that instance's control port, backoff, retry
  - Producer coroutine discovers doc IDs from search pages and fills a queue
  - Upload coroutine batch-syncs to GDrive every BATCH_UPLOAD_EVERY new docs

Multi-Tor setup (configure in .env):
  TOR_SOCKS_PORTS=9050,9052,9054      (one per Tor process)
  TOR_CONTROL_PORTS=9051,9053,9055

Start additional Tor instances (run once as admin):
  tor --SocksPort 9052 --ControlPort 9053 --DataDirectory C:/tor_data/2
  tor --SocksPort 9054 --ControlPort 9055 --DataDirectory C:/tor_data/3

Usage:
    python scraper_async.py                   # async scrape, all Tor ports in .env
    python scraper_async.py --test 30         # stop after 30 docs
    python scraper_async.py --workers 5       # use 5 concurrent workers
    python scraper_async.py --no-tor          # direct async (no Tor, for testing)
    python scraper_async.py --query "cheque dishonour"
"""

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from loguru import logger

import dedup as _dedup

# Tor imports (graceful fallback)
try:
    from stem import Signal
    from stem.control import Controller
    TOR_AVAILABLE = True
except ImportError:
    TOR_AVAILABLE = False
    logger.warning("stem not installed — Tor IP rotation disabled")

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
RAW_HTML_DIR  = Path(os.getenv("RAW_HTML_DIR", "./data/raw_html"))
RCLONE        = Path("./bin/rclone.exe") if os.name == "nt" else Path("rclone")
GDRIVE_RAW    = "lsse_gdrive:LSSE/raw_html"

BASE_URL    = "https://indiankanoon.org"
SEARCH_PATH = "/search/"
QUERY       = "Section 138 Negotiable Instruments Act"

# Tor ports — read from .env, comma-separated
_socks_env   = os.getenv("TOR_SOCKS_PORTS",   "9050")
_control_env = os.getenv("TOR_CONTROL_PORTS",  "9051")
DEFAULT_SOCKS_PORTS   = [int(p) for p in _socks_env.split(",")]
DEFAULT_CONTROL_PORTS = [int(p) for p in _control_env.split(",")]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://indiankanoon.org/",
}

MAX_RETRIES        = 5
BACKOFF_BASE       = 10      # seconds; doubles each retry
BATCH_UPLOAD_EVERY = 10      # sync GDrive every N new docs
QUEUE_MAX          = 200     # max items in URL queue


# ── Deduplication ──────────────────────────────────────────────────────────────

def already_scraped(doc_id: str) -> bool:
    return _dedup.get().contains(doc_id) or (RAW_HTML_DIR / f"{doc_id}.html").exists()


# ── HTML parsing ───────────────────────────────────────────────────────────────

def parse_search_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_ids: set[str] = set()
    for article in soup.select("article.result"):
        hlbottom = article.select_one("div.hlbottom a[href^='/doc/']")
        if not hlbottom:
            continue
        m = re.search(r"/doc/(\d+)/", hlbottom["href"])
        if not m:
            continue
        doc_id = m.group(1)
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        title_tag = article.select_one("h4.result_title a")
        title = title_tag.get_text(strip=True) if title_tag else f"Case_{doc_id}"
        results.append({
            "doc_id": doc_id,
            "title":  title,
            "url":    BASE_URL + f"/doc/{doc_id}/",
        })
    return results


# ── Tor IP rotation (sync — called from worker thread via run_in_executor) ─────

def _renew_tor_ip_sync(ctrl_port: int) -> None:
    if not TOR_AVAILABLE:
        return
    try:
        with Controller.from_port(port=ctrl_port) as ctrl:
            ctrl.authenticate()
            ctrl.signal(Signal.NEWNYM)
        logger.info(f"  [Tor:{ctrl_port}] NEWNYM sent — rotating exit node")
        time.sleep(5)
    except Exception as exc:
        logger.warning(f"  [Tor:{ctrl_port}] NEWNYM failed: {exc}")


async def renew_tor_ip(ctrl_port: int) -> None:
    """Async wrapper — runs blocking Tor control in thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _renew_tor_ip_sync, ctrl_port)


# ── GDrive sync (sync — called from executor) ──────────────────────────────────

def _sync_to_gdrive_sync() -> None:
    files = list(RAW_HTML_DIR.glob("*.html")) + list(RAW_HTML_DIR.glob("*.json"))
    if not files:
        return
    logger.info(f"Batch uploading {len(files)} files to GDrive...")
    result = subprocess.run(
        [str(RCLONE), "copy", str(RAW_HTML_DIR), GDRIVE_RAW,
         "--tpslimit", "10", "--drive-chunk-size", "64M",
         "--include", "*.html", "--include", "*.json"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        for f in files:
            f.unlink(missing_ok=True)
        logger.success(f"Uploaded {len(files)} files — local copies cleared")
    else:
        logger.warning(f"rclone failed: {result.stderr.strip()}")


async def sync_to_gdrive() -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_to_gdrive_sync)


# ── Save ───────────────────────────────────────────────────────────────────────

def save_case(doc_id: str, html: str, meta: dict) -> None:
    if _dedup.get().contains(doc_id):
        return
    html_path = RAW_HTML_DIR / f"{doc_id}.html"
    meta_path = RAW_HTML_DIR / f"{doc_id}.meta.json"
    if html_path.exists():
        return
    html_path.write_text(html, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _dedup.get().add(doc_id)
    logger.success(f"[SAVED] {doc_id} — {meta.get('title', '')[:60]}")


# ── httpx client factory ───────────────────────────────────────────────────────

def make_client(socks_port: int | None) -> httpx.AsyncClient:
    """Create an async httpx client, optionally via Tor SOCKS5h."""
    if socks_port:
        proxy_url = f"socks5h://127.0.0.1:{socks_port}"
        transport = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(proxy_url))
    else:
        transport = httpx.AsyncHTTPTransport()
    return httpx.AsyncClient(
        headers=HEADERS,
        transport=transport,
        timeout=30,
        follow_redirects=True,
    )


# ── Date-range slices for producer (mirrors api_fetcher strategy) ───────────────

def _date_slices(start_year: int = 1988) -> list[tuple[str, str]]:
    """Yield (fromdate, todate) monthly slices from start_year to today."""
    import calendar
    from datetime import date
    slices = []
    end_year = date.today().year
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            last = calendar.monthrange(year, month)[1]
            slices.append((
                f"{year}-{month:02d}-01",
                f"{year}-{month:02d}-{last:02d}",
            ))
    return slices


# ── Producer — discovers doc IDs from date-sliced search pages ─────────────────

async def producer(
    client: httpx.AsyncClient,
    query: str,
    url_queue: asyncio.Queue,
    stop_event: asyncio.Event,
) -> None:
    """
    Iterates monthly date slices (1988–today), fetches up to 10 search pages
    per slice, and puts undiscovered case URLs into url_queue.
    This bypasses the ~1000-result cap on a single flat search.
    """
    slices = _date_slices(start_year=1988)
    logger.info(f"[Producer] {len(slices)} monthly slices to sweep")

    for fromdate, todate in slices:
        if stop_event.is_set():
            break
        for page in range(10):   # up to 100 results per month slice
            if stop_event.is_set():
                break
            pagenum = page * 10
            search_url = (
                f"{BASE_URL}{SEARCH_PATH}"
                f"?formInput={urllib.parse.quote(query + f' fromdate:{fromdate} todate:{todate}')}"
                f"&type=judgments"
                f"&pagenum={pagenum}"
            )
            try:
                resp = await client.get(search_url)
                if resp.status_code == 429:
                    logger.warning(f"[Producer] 429 — rotating IP, sleeping 30s")
                    await asyncio.sleep(30)
                    break   # move to next slice
                if resp.status_code != 200:
                    logger.warning(f"[Producer] HTTP {resp.status_code} on {fromdate}")
                    await asyncio.sleep(5)
                    break
                cases = parse_search_results(resp.text)
                if not cases:
                    break   # no more results for this slice
                for case in cases:
                    if not already_scraped(case["doc_id"]):
                        await url_queue.put(case)
                await asyncio.sleep(random.uniform(1, 3))
            except Exception as exc:
                logger.warning(f"[Producer] Error ({fromdate} p{page}): {exc}")
                await asyncio.sleep(5)
                break

    logger.info("[Producer] All date slices exhausted.")
    stop_event.set()


# ── Worker — fetches individual case pages ─────────────────────────────────────

async def worker(
    worker_id: int,
    socks_port: int | None,
    ctrl_port: int | None,
    url_queue: asyncio.Queue,
    stop_event: asyncio.Event,
    saved_counter: list,    # [int] — mutable shared counter
    limit: int | None,
) -> None:
    """One async worker coroutine per Tor instance."""
    async with make_client(socks_port) as client:
        delay = BACKOFF_BASE
        while not stop_event.is_set():
            try:
                case = url_queue.get_nowait()
            except asyncio.QueueEmpty:
                if stop_event.is_set():
                    break
                await asyncio.sleep(1)
                continue

            doc_id = case["doc_id"]

            if already_scraped(doc_id):
                url_queue.task_done()
                continue

            await asyncio.sleep(random.uniform(0.5, 2.0))

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = await client.get(case["url"])

                    if resp.status_code == 200:
                        save_case(doc_id, resp.text, {
                            "doc_id": doc_id,
                            "title":  case["title"],
                            "url":    case["url"],
                        })
                        saved_counter[0] += 1
                        delay = BACKOFF_BASE   # reset on success

                        if limit and saved_counter[0] >= limit:
                            stop_event.set()

                        if saved_counter[0] % BATCH_UPLOAD_EVERY == 0:
                            await sync_to_gdrive()
                        break

                    elif resp.status_code in (429, 403):
                        logger.warning(
                            f"[W{worker_id}] {resp.status_code} on {doc_id} "
                            f"(attempt {attempt}/{MAX_RETRIES}) — rotating IP, waiting {delay}s"
                        )
                        if ctrl_port:
                            await renew_tor_ip(ctrl_port)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 120)

                    elif resp.status_code == 404:
                        logger.warning(f"[W{worker_id}] 404 — {doc_id}")
                        break

                    else:
                        logger.warning(f"[W{worker_id}] HTTP {resp.status_code} — {doc_id}")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 120)

                except Exception as exc:
                    logger.warning(f"[W{worker_id}] Attempt {attempt} error: {exc}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120)

            url_queue.task_done()


# ── Main ───────────────────────────────────────────────────────────────────────

async def scrape_async(
    query: str,
    socks_ports: list[int],
    ctrl_ports: list[int],
    n_workers: int,
    limit: int | None,
    use_tor: bool,
) -> None:
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)

    url_queue   = asyncio.Queue(maxsize=QUEUE_MAX)
    stop_event  = asyncio.Event()
    saved_counter = [0]

    # Pair up SOCKS and control ports; cycle if fewer than n_workers
    def get_port(idx: int, ports: list[int]) -> int | None:
        return ports[idx % len(ports)] if ports and use_tor else None

    logger.info(
        f"Starting async scrape | query='{query}' | "
        f"workers={n_workers} | tor={use_tor} | limit={limit}"
    )

    # Producer uses first Tor port (or direct)
    prod_client = make_client(get_port(0, socks_ports) if use_tor else None)

    tasks = [
        asyncio.create_task(
            producer(prod_client, query, url_queue, stop_event)
        )
    ]

    for i in range(n_workers):
        tasks.append(asyncio.create_task(
            worker(
                worker_id=i,
                socks_port=get_port(i, socks_ports) if use_tor else None,
                ctrl_port=get_port(i, ctrl_ports) if use_tor else None,
                url_queue=url_queue,
                stop_event=stop_event,
                saved_counter=saved_counter,
                limit=limit,
            )
        ))

    await asyncio.gather(*tasks)
    await prod_client.aclose()
    await sync_to_gdrive()
    logger.success(f"Async scrape done. Total new cases saved: {saved_counter[0]}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSSE Async Multi-Tor Scraper")
    parser.add_argument("--test",    type=int, metavar="N", help="Stop after N docs")
    parser.add_argument("--workers", type=int, default=len(DEFAULT_SOCKS_PORTS),
                        help="Number of concurrent worker coroutines")
    parser.add_argument("--query",   type=str, default=QUERY, help="Search query")
    parser.add_argument("--no-tor",  action="store_true", help="Direct connection (no Tor)")
    args = parser.parse_args()

    use_tor = not args.no_tor
    if use_tor and not TOR_AVAILABLE:
        logger.error("stem not installed. Use --no-tor or: pip install stem PySocks")
        raise SystemExit(1)

    asyncio.run(scrape_async(
        query=args.query,
        socks_ports=DEFAULT_SOCKS_PORTS,
        ctrl_ports=DEFAULT_CONTROL_PORTS,
        n_workers=args.workers,
        limit=args.test,
        use_tor=use_tor,
    ))
