"""
index_chroma.py
---------------
Step 4b: Embed processed judgments into ChromaDB for semantic search.

Embeds: title + facts_text + arguments_text + ratio_text
Model:  BAAI/bge-m3 (configurable via EMBED_MODEL env var)
Store:  ChromaDB at CHROMA_DB_DIR (./database/chroma by default)

Note: First run downloads the embedding model (~2 GB for bge-m3).
      Set EMBED_MODEL=all-MiniLM-L6-v2 for a lightweight 90 MB alternative.

Usage:
    python index_chroma.py          # embed all unembedded docs
    python index_chroma.py --reset  # drop collection and re-embed everything
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from dotenv import load_dotenv
from loguru import logger
from tqdm import tqdm

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
PROCESSED_JSON_DIR = Path(os.getenv("PROCESSED_JSON_DIR", "./data/processed_json"))
CHROMA_DB_DIR      = Path(os.getenv("CHROMA_DB_DIR", "./database/chroma"))
GDRIVE_PROCESSED   = "lsse_gdrive:LSSE/processed_json"
RCLONE             = Path("./bin/rclone.exe") if os.name == "nt" else Path("rclone")
COLLECTION_NAME    = "lsse_judgments"
EMBED_MODEL        = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
BATCH_SIZE         = 8    # keep small: bge-m3 is memory-intensive on CPU


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


# ── Text helpers ───────────────────────────────────────────────────────────────

def build_embed_text(doc: dict) -> str:
    """Build the text to embed from a judgment document."""
    parts = [
        doc.get("title", ""),
        doc.get("facts_text", ""),
        doc.get("arguments_text", ""),
        doc.get("ratio_text", ""),
    ]
    return " ".join(p for p in parts if p).strip()[:8000]   # cap at ~8k chars


# ── Main ───────────────────────────────────────────────────────────────────────

def run(reset: bool = False) -> None:
    PROCESSED_JSON_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)

    # Load embedding model (downloads on first use)
    logger.info(f"Loading embedding model: {EMBED_MODEL}")
    logger.info("(First run: this may download ~2 GB for bge-m3, ~90 MB for MiniLM)")
    ef = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)

    # ChromaDB persistent client
    client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))

    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
            logger.info(f"Collection '{COLLECTION_NAME}' dropped.")
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    # Already-embedded IDs
    existing_ids: set[str] = set()
    if not reset:
        try:
            existing = collection.get(include=[])
            existing_ids = set(existing["ids"])
            logger.info(f"Already embedded: {len(existing_ids)} documents")
        except Exception:
            pass

    # List JSON files on GDrive
    logger.info("Listing processed JSON files on Google Drive...")
    remote_files = rclone_list_remote(GDRIVE_PROCESSED)
    json_files = [f for f in remote_files if f.endswith(".json")]
    if not json_files:
        logger.warning("No processed JSON files found. Run process_judgments.py first.")
        return
    logger.info(f"Found {len(json_files)} JSON files on GDrive")

    ids, documents, metadatas = [], [], []
    embedded = 0

    for filename in tqdm(json_files, desc="Embedding into ChromaDB"):
        doc_id = filename.replace(".json", "")
        if doc_id in existing_ids:
            logger.debug(f"[SKIP] {doc_id} already embedded")
            continue

        local_path = PROCESSED_JSON_DIR / filename
        if not rclone_download(f"{GDRIVE_PROCESSED}/{filename}", local_path):
            logger.warning(f"Could not download {filename}")
            continue

        doc = json.loads(local_path.read_text(encoding="utf-8"))
        local_path.unlink(missing_ok=True)

        text = build_embed_text(doc)
        if not text:
            logger.debug(f"[SKIP] {doc_id} has no embeddable text")
            continue

        ids.append(doc_id)
        documents.append(text)
        metadatas.append({
            "title":    doc.get("title", "")[:200],
            "court":    doc.get("court", ""),
            "date":     doc.get("date", ""),
            "decision": doc.get("decision", "Unknown"),
        })

        if len(ids) >= BATCH_SIZE:
            collection.add(ids=ids, documents=documents, metadatas=metadatas)
            embedded += len(ids)
            logger.info(f"Embedded batch of {len(ids)} ({embedded} total)")
            ids, documents, metadatas = [], [], []

    # Flush remaining
    if ids:
        collection.add(ids=ids, documents=documents, metadatas=metadatas)
        embedded += len(ids)

    logger.success(
        f"ChromaDB embedding complete. "
        f"New documents embedded: {embedded} | Total in collection: {collection.count()}"
    )


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSSE ChromaDB Embedder")
    parser.add_argument("--reset", action="store_true",
                        help="Drop collection and re-embed everything from scratch")
    args = parser.parse_args()
    run(reset=args.reset)
