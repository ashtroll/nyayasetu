"""
api_fetcher.py
--------------
Indian Kanoon API-based document fetcher (runs in parallel with scraper_async.py).

Non-commercial free quota: Rs 10,000/month
  - /search/       Rs 0.50/call  (amortised ~Rs 0.05/doc across 10 results per page)
  - /doc/<id>/     Rs 0.20/call  → 50,000 full docs/month free
  - /docmeta/<id>/ Rs 0.02/call  → 500,000 meta lookups/month free

Strategy to bypass the ~100-result-per-query cap:
  Slice queries by date range (year → quarter → month) so each slice
  returns <100 results, then paginate each slice to exhaustion.
  This gives access to all 45,036+ Section 138 documents.

Outputs raw HTML to data/raw_html/ — identical layout to scraper_async.py,
so process_judgments.py picks them up unchanged.

Usage:
    python api_fetcher.py                          # Section 138 NI Act, full sweep
    python api_fetcher.py --test 20                # first 20 docs only
    python api_fetcher.py --query "cheque bounce"  # custom query
    python api_fetcher.py --all-courts             # all 8 broad NI Act queries
    python api_fetcher.py --start-year 2015        # sweep from 2015 onwards
"""

import argparse
import json
import os
import random
import subprocess
import time
import urllib.parse
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv
from loguru import logger

import dedup as _dedup

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
RAW_HTML_DIR  = Path(os.getenv("RAW_HTML_DIR", "./data/raw_html"))
IK_API_BASE   = "https://api.indiankanoon.org"
IK_API_TOKEN  = os.getenv("IK_API_TOKEN", "")
RCLONE        = Path("./bin/rclone.exe") if os.name == "nt" else Path("rclone")
GDRIVE_RAW    = "lsse_gdrive:LSSE/raw_html"
BATCH_UPLOAD_EVERY = 10      # sync to GDrive every N new docs
SLICE_THRESHOLD    = 90      # if found > this, sub-slice the date range
MAX_PAGES_PER_SLICE = 100    # IK API allows up to 100 pages (1,000 docs) per query

# NI Act enacted 1881, substantive case law from ~1988 amendment
DEFAULT_START_YEAR = 1988

# Broad NI Act queries (used with --all-courts)
BROAD_QUERIES = [
    "Section 138 Negotiable Instruments Act",
    "Section 141 Negotiable Instruments Act",
    "cheque dishonour conviction",
    "cheque bounce complaint dismissed",
    "NI Act acquittal",
    "negotiable instruments cheque drawee bank",
    "138 NI Act appeal High Court",
    "138 NI Act Supreme Court",
]


# ── Session ────────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Token {IK_API_TOKEN}",
        "Accept": "application/json",
    })
    return s


def jitter(lo: float = 0.3, hi: float = 1.0) -> None:
    time.sleep(round(random.uniform(lo, hi), 2))


# ── Deduplication ──────────────────────────────────────────────────────────────

def already_fetched(doc_id: str) -> bool:
    return _dedup.get().contains(doc_id) or (RAW_HTML_DIR / f"{doc_id}.html").exists()


# ── API calls with retry ───────────────────────────────────────────────────────

_FORBIDDEN = {"__forbidden__": True}   # sentinel returned on 403


def _post_with_retry(session: requests.Session, url: str, timeout: int = 20) -> dict:
    """POST url with 3-attempt exponential backoff.
    Returns _FORBIDDEN on 403, {} on other total failure."""
    for attempt in range(3):
        try:
            r = session.post(url, timeout=timeout)
            if r.status_code == 403:
                logger.warning(f"403 Forbidden — token rate-limited. Waiting 60s...")
                time.sleep(60)
                return _FORBIDDEN
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            wait = (2 ** attempt) * 5
            logger.warning(f"API call failed (attempt {attempt+1}/3): {exc}. Retrying in {wait}s...")
            time.sleep(wait)
    logger.error(f"All retries exhausted for {url}")
    return {}


def fetch_search_page(
    session: requests.Session,
    query: str,
    pagenum: int,
    fromdate: str | None = None,
    todate: str | None = None,
) -> dict:
    """
    POST to IK search API.
    fromdate / todate format: DD-MM-YYYY  (e.g. "01-01-2020")
    Dates are appended inside formInput per the official IKAPI convention:
      formInput = "query fromdate: DD-MM-YYYY todate: DD-MM-YYYY"
    """
    q = query
    if fromdate:
        q += f" fromdate: {fromdate} todate: {todate}"
    url = (
        f"{IK_API_BASE}/search/"
        f"?formInput={urllib.parse.quote(q)}"
        f"&pagenum={pagenum}"
        f"&maxpages={MAX_PAGES_PER_SLICE}"
    )
    return _post_with_retry(session, url)


def fetch_doc(session: requests.Session, doc_id: str) -> dict:
    url = f"{IK_API_BASE}/doc/{doc_id}/"
    return _post_with_retry(session, url, timeout=30)


# ── Save ───────────────────────────────────────────────────────────────────────

def save_doc(doc_id: str, html: str, meta: dict) -> None:
    if _dedup.get().contains(doc_id):
        logger.debug(f"[SKIP] {doc_id} already in dedup index")
        return
    html_path = RAW_HTML_DIR / f"{doc_id}.html"
    meta_path = RAW_HTML_DIR / f"{doc_id}.meta.json"
    if html_path.exists():
        logger.debug(f"[SKIP] {doc_id} already on disk")
        return
    html_path.write_text(html, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _dedup.get().add(doc_id)
    logger.success(f"[SAVED] {doc_id} — {meta.get('title', '')[:60]}")


# ── rclone batch sync ──────────────────────────────────────────────────────────

def sync_to_gdrive() -> None:
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
        logger.success(f"Batch uploaded {len(files)} files — local copies cleared")
    else:
        logger.warning(f"rclone failed: {result.stderr.strip()}")


# ── Date-range helpers ─────────────────────────────────────────────────────────

def _fmt(d: date) -> str:
    """Format date as DD-MM-YYYY for IK API."""
    return d.strftime("%d-%m-%Y")


def _year_range(year: int) -> tuple[str, str]:
    return _fmt(date(year, 1, 1)), _fmt(date(year, 12, 31))


def _quarter_ranges(year: int) -> list[tuple[str, str]]:
    quarters = [
        (date(year, 1, 1),  date(year, 3, 31)),
        (date(year, 4, 1),  date(year, 6, 30)),
        (date(year, 7, 1),  date(year, 9, 30)),
        (date(year, 10, 1), date(year, 12, 31)),
    ]
    return [(_fmt(s), _fmt(e)) for s, e in quarters]


def _month_ranges(year: int, quarter: int) -> list[tuple[str, str]]:
    """Return 3 monthly ranges for a given quarter (1-4)."""
    import calendar
    first_month = (quarter - 1) * 3 + 1
    months = range(first_month, first_month + 3)
    ranges = []
    for m in months:
        last_day = calendar.monthrange(year, m)[1]
        ranges.append((_fmt(date(year, m, 1)), _fmt(date(year, m, last_day))))
    return ranges


def probe_count(session: requests.Session, query: str, fromdate: str, todate: str) -> int:
    """Return total found count for a date-sliced query (costs 1 search call).
    Returns _FORBIDDEN dict if 403 received."""
    result = fetch_search_page(session, query, pagenum=0, fromdate=fromdate, todate=todate)
    if result.get("__forbidden__"):
        return _FORBIDDEN
    found = result.get("found", "0")
    # "found" is a string like "1 - 10 of 4523" or plain "0"
    try:
        # Parse "X - Y of N" format
        if "of" in str(found):
            return int(str(found).split("of")[-1].strip().replace(",", ""))
        return int(str(found).strip().replace(",", ""))
    except ValueError:
        return 0


# ── Core fetch loop for a single date slice ────────────────────────────────────

def fetch_slice(
    session: requests.Session,
    query: str,
    fromdate: str,
    todate: str,
    limit: int | None,
    total_saved: list,   # mutable counter passed by reference
) -> str:
    """
    Fetch all docs in one date slice, paginating up to MAX_PAGES_PER_SLICE.
    Modifies total_saved[0] in place.
    Returns "forbidden" if a 403 is hit, "ok" otherwise.
    """
    for page in range(MAX_PAGES_PER_SLICE):
        pagenum = page * 10
        result = fetch_search_page(session, query, pagenum, fromdate, todate)
        if result.get("__forbidden__"):
            return "forbidden"
        docs = result.get("docs", [])
        if not docs:
            break

        for doc in docs:
            doc_id = str(doc.get("tid", ""))
            if not doc_id:
                continue
            if already_fetched(doc_id):
                logger.debug(f"    [SKIP] {doc_id}")
                continue

            jitter(0.3, 1.0)
            full = fetch_doc(session, doc_id)
            html = full.get("doc", "")
            if not html:
                logger.debug(f"    [SKIP] {doc_id} — empty response")
                continue

            meta = {
                "doc_id":    doc_id,
                "title":     doc.get("title", ""),
                "docsource": doc.get("docsource", ""),
                "url":       f"https://indiankanoon.org/doc/{doc_id}/",
            }
            save_doc(doc_id, html, meta)
            total_saved[0] += 1

            if total_saved[0] % BATCH_UPLOAD_EVERY == 0:
                sync_to_gdrive()

            if limit and total_saved[0] >= limit:
                return

        jitter(0.5, 1.5)

    return "ok"


# ── Adaptive date-sliced sweep ─────────────────────────────────────────────────

def sweep_query(
    session: requests.Session,
    query: str,
    start_year: int,
    limit: int | None,
) -> int:
    """
    Sweep all docs for `query` from start_year to current year
    using adaptive date slicing (year → quarter → month).
    Returns total new docs saved.
    """
    end_year = date.today().year
    total_saved = [0]   # list so fetch_slice can mutate it

    logger.info(f"Sweeping '{query}' from {start_year} to {end_year}")
    consecutive_403s = 0
    MAX_CONSECUTIVE_403S = 3

    for year in range(start_year, end_year + 1):
        if limit and total_saved[0] >= limit:
            break
        if consecutive_403s >= MAX_CONSECUTIVE_403S:
            logger.warning(f"  {consecutive_403s} consecutive 403s — skipping rest of query")
            break

        fd_year, td_year = _year_range(year)
        count = probe_count(session, query, fd_year, td_year)
        if isinstance(count, dict) and count.get("__forbidden__"):
            consecutive_403s += 1
            continue
        logger.info(f"  {year}: {count} docs found")

        if count == 0:
            continue

        consecutive_403s = 0  # reset on success

        if count <= SLICE_THRESHOLD:
            status = fetch_slice(session, query, fd_year, td_year, limit, total_saved)
            if status == "forbidden":
                consecutive_403s += 1
        else:
            # Split into quarters
            for q_idx, (fd_q, td_q) in enumerate(_quarter_ranges(year), 1):
                if limit and total_saved[0] >= limit:
                    break
                if consecutive_403s >= MAX_CONSECUTIVE_403S:
                    break
                q_count = probe_count(session, query, fd_q, td_q)
                logger.info(f"    Q{q_idx} {year}: {q_count} docs")

                if q_count == 0:
                    continue
                elif q_count <= SLICE_THRESHOLD:
                    status = fetch_slice(session, query, fd_q, td_q, limit, total_saved)
                    if status == "forbidden":
                        consecutive_403s += 1
                else:
                    # Split into months
                    for fd_m, td_m in _month_ranges(year, q_idx):
                        if limit and total_saved[0] >= limit:
                            break
                        m_count = probe_count(session, query, fd_m, td_m)
                        logger.info(f"      {fd_m}–{td_m}: {m_count} docs")
                        if m_count > 0:
                            fetch_slice(session, query, fd_m, td_m, limit, total_saved)

        jitter(1, 2)

    sync_to_gdrive()
    return total_saved[0]


# ── Entry point ────────────────────────────────────────────────────────────────

def run(queries: list[str], limit: int | None = None, start_year: int = DEFAULT_START_YEAR) -> None:
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)

    if not IK_API_TOKEN:
        logger.error("IK_API_TOKEN not set. Add it to .env")
        raise SystemExit(1)

    session = make_session()

    # Connectivity check
    try:
        r = session.post(f"{IK_API_BASE}/search/?formInput=test&pagenum=0", timeout=10)
        r.raise_for_status()
        logger.info("Indian Kanoon API reachable.")
    except Exception as exc:
        logger.error(f"Cannot reach IK API: {exc}")
        raise SystemExit(1)

    grand_total = 0
    seen_queries: set[str] = set()

    for query in queries:
        if query in seen_queries:
            continue
        seen_queries.add(query)

        remaining = (limit - grand_total) if limit else None
        saved = sweep_query(session, query, start_year, remaining)
        grand_total += saved
        logger.info(f"Query done. Saved {saved} new docs (grand total: {grand_total})")

        if limit and grand_total >= limit:
            break

    logger.success(f"API fetch complete. Total new docs saved: {grand_total}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSSE Indian Kanoon API Fetcher")
    parser.add_argument("--test", type=int, metavar="N", help="Fetch only N docs then stop")
    parser.add_argument("--query", type=str, default=None, help="Custom search query")
    parser.add_argument("--all-courts", action="store_true", help="Run all 8 broad NI Act queries")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR,
                        help=f"Start year for date sweep (default: {DEFAULT_START_YEAR})")
    args = parser.parse_args()

    if args.all_courts:
        queries = BROAD_QUERIES
    elif args.query:
        queries = [args.query]
    else:
        queries = [BROAD_QUERIES[0]]

    run(queries=queries, limit=args.test, start_year=args.start_year)
