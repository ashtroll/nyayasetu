"""
index_meili.py
--------------
Step 4a: Index processed JSON judgments into MeiliSearch.

For each processed_json on GDrive:
  1. Download JSON (via rclone)
  2. Upsert into MeiliSearch index 'lsse_judgments'
  3. Delete local copy

Usage:
    python index_meili.py          # index all unindexed docs
    python index_meili.py --reset  # drop index and re-index everything
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import meilisearch
from dotenv import load_dotenv
from loguru import logger
from tqdm import tqdm

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
PROCESSED_JSON_DIR = Path(os.getenv("PROCESSED_JSON_DIR", "./data/processed_json"))
MEILI_HOST         = os.getenv("MEILI_HOST", "http://127.0.0.1:7700")
MEILI_MASTER_KEY   = os.getenv("MEILI_MASTER_KEY", "")
GDRIVE_PROCESSED   = "lsse_gdrive:LSSE/processed_json"
RCLONE             = Path("./bin/rclone.exe") if os.name == "nt" else Path("rclone")
INDEX_NAME         = "lsse_judgments"
BATCH_SIZE         = 50


# ── rclone helpers ─────────────────────────────────────────────────────────────

def rclone_list_remote(remote_path: str) -> list[str]:
    result = subprocess.run(
        [str(RCLONE), "lsf", remote_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def rclone_download(remote_path: str, local_path: Path) -> bool:
    result = subprocess.run(
        [str(RCLONE), "copyto", remote_path, str(local_path)],
        capture_output=True, text=True
    )
    return result.returncode == 0


# ── MeiliSearch helpers ────────────────────────────────────────────────────────

def get_indexed_ids(index) -> set[str]:
    """Return set of doc IDs already present in the MeiliSearch index."""
    try:
        stats = index.get_stats()
        if stats.number_of_documents == 0:
            return set()
        results = index.search("", {"limit": 10000, "attributesToRetrieve": ["id"]})
        return {hit["id"] for hit in results["hits"]}
    except Exception:
        return set()


# ── Main ───────────────────────────────────────────────────────────────────────

def run(reset: bool = False) -> None:
    PROCESSED_JSON_DIR.mkdir(parents=True, exist_ok=True)

    # Connect to MeiliSearch
    client = meilisearch.Client(MEILI_HOST, MEILI_MASTER_KEY or None)
    try:
        client.health()
        logger.info(f"MeiliSearch reachable at {MEILI_HOST}")
    except Exception as exc:
        logger.error(f"MeiliSearch not reachable at {MEILI_HOST}: {exc}")
        logger.info("Run start_services.ps1 first.")
        return

    # Reset index if requested
    if reset:
        try:
            task = client.delete_index(INDEX_NAME)
            client.wait_for_task(task.task_uid)
            logger.info(f"Index '{INDEX_NAME}' dropped.")
        except Exception:
            pass

    # Create index (no-op if already exists)
    try:
        task = client.create_index(INDEX_NAME, {"primaryKey": "id"})
        client.wait_for_task(task.task_uid)
    except meilisearch.errors.MeilisearchApiError as e:
        if "already exists" not in str(e):
            raise

    index = client.index(INDEX_NAME)

    # Schema configuration
    index.update_filterable_attributes(["court", "decision", "date", "statutes", "judge_names"])
    index.update_sortable_attributes(["date"])
    index.update_searchable_attributes([
        "title", "court", "facts_text", "arguments_text",
        "ratio_text", "judge_names", "statutes"
    ])

    # Already-indexed IDs
    indexed_ids: set[str] = set()
    if not reset:
        indexed_ids = get_indexed_ids(index)
        logger.info(f"Already indexed: {len(indexed_ids)} documents")

    # List JSON files on GDrive
    logger.info("Listing processed JSON files on Google Drive...")
    remote_files = rclone_list_remote(GDRIVE_PROCESSED)
    json_files = [f for f in remote_files if f.endswith(".json")]
    if not json_files:
        logger.warning("No processed JSON files found. Run process_judgments.py first.")
        return
    logger.info(f"Found {len(json_files)} JSON files on GDrive")

    batch: list[dict] = []
    indexed = 0

    for filename in tqdm(json_files, desc="Indexing into MeiliSearch"):
        doc_id = filename.replace(".json", "")
        if doc_id in indexed_ids:
            logger.debug(f"[SKIP] {doc_id} already indexed")
            continue

        local_path = PROCESSED_JSON_DIR / filename
        if not rclone_download(f"{GDRIVE_PROCESSED}/{filename}", local_path):
            logger.warning(f"Could not download {filename}")
            continue

        doc = json.loads(local_path.read_text(encoding="utf-8"))
        local_path.unlink(missing_ok=True)
        batch.append(doc)

        if len(batch) >= BATCH_SIZE:
            task = index.add_documents(batch)
            client.wait_for_task(task.task_uid)
            indexed += len(batch)
            logger.info(f"Indexed batch of {len(batch)} ({indexed} total)")
            batch = []

    # Flush remaining
    if batch:
        task = index.add_documents(batch)
        client.wait_for_task(task.task_uid)
        indexed += len(batch)

    final_count = index.get_stats().number_of_documents
    logger.success(
        f"MeiliSearch indexing complete. "
        f"New documents added: {indexed} | Total in index: {final_count}"
    )


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSSE MeiliSearch Indexer")
    parser.add_argument("--reset", action="store_true",
                        help="Drop and re-index everything from scratch")
    args = parser.parse_args()
    run(reset=args.reset)
