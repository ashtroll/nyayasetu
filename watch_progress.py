"""
watch_progress.py
-----------------
Monitors total docs collected across all pipelines.
Logs progress every 30 min. Writes a MILESTONE file when 100k is hit.

Run once:  python watch_progress.py
Check now: python watch_progress.py --check
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

LOGS = {
    "api_fetcher":    Path("data/api_fetch.log"),
    "sc_bulk":        Path("data/sc_bulk.log"),
    "hc_bulk":        Path("data/hc_bulk.log"),
    "async_scraper":  Path("data/async_scrape.log"),
}

PROGRESS_FILE = Path("data/progress.log")
MILESTONE     = 100_000
CHECK_EVERY   = 30 * 60   # seconds


def count_saved(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    return text.count("[SAVED]")


def snapshot() -> dict:
    counts = {name: count_saved(path) for name, path in LOGS.items()}
    counts["total"] = sum(counts.values())
    return counts


def report(counts: dict, verbose: bool = True) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"\n{'='*55}",
        f"  Progress snapshot - {ts}",
        f"{'='*55}",
        f"  API fetcher   : {counts['api_fetcher']:>8,}",
        f"  SC bulk       : {counts['sc_bulk']:>8,}",
        f"  HC bulk       : {counts['hc_bulk']:>8,}",
        f"  Async scraper : {counts['async_scraper']:>8,}",
        f"  {'-'*30}",
        f"  TOTAL         : {counts['total']:>8,}  /  {MILESTONE:,}",
        f"  Remaining     : {max(0, MILESTONE - counts['total']):>8,}",
        f"{'='*55}",
    ]
    text = "\n".join(lines)
    if verbose:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    return text


def watch():
    print(f"Watching progress. Checking every {CHECK_EVERY//60} min. "
          f"Will alert at {MILESTONE:,} docs.\n"
          f"Progress log: {PROGRESS_FILE}\n"
          f"Press Ctrl+C to stop.\n")

    notified = False
    while True:
        counts = snapshot()
        entry = report(counts)

        # Append to progress log
        with PROGRESS_FILE.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")

        if counts["total"] >= MILESTONE and not notified:
            milestone_msg = (
                f"\n{'*'*55}\n"
                f"  *** 100,000 DOCUMENTS REACHED! ***\n"
                f"  Total: {counts['total']:,}\n"
                f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"  Next step: python process_judgments.py\n"
                f"{'*'*55}\n"
            )
            sys.stdout.buffer.write(milestone_msg.encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
            Path("data/MILESTONE_100K.txt").write_text(milestone_msg, encoding="utf-8")
            notified = True

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Print current count and exit (no loop)")
    args = parser.parse_args()

    if args.check:
        report(snapshot())
    else:
        watch()
