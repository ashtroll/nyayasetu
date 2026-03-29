"""
dedup.py — Shared persistent deduplication index for all LSSE pipelines.

All scrapers import this and call:
    seen = SeenIDs()
    seen.contains(doc_id)   -> bool
    seen.add(doc_id)        -> None   (also persists to disk)

Uses a flat text file (data/seen_ids.txt) — one doc_id per line.
Loaded into a memory set at startup for O(1) lookups.
Append-only writes are safe for concurrent processes on Windows.
"""
import os
from pathlib import Path

_SEEN_FILE = Path(os.getenv("RAW_HTML_DIR", "./data/raw_html")).parent / "seen_ids.txt"


class SeenIDs:
    """Process-local cache backed by a shared append-only file."""

    def __init__(self, path: Path = _SEEN_FILE):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Load existing IDs into memory
        if self._path.exists():
            self._ids: set[str] = set(
                line.strip() for line in
                self._path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        else:
            self._ids = set()

    def contains(self, doc_id: str) -> bool:
        return doc_id in self._ids

    def add(self, doc_id: str) -> None:
        if doc_id in self._ids:
            return
        self._ids.add(doc_id)
        # Append to file — safe for concurrent writers (OS-level atomic append)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(doc_id + "\n")

    def __len__(self) -> int:
        return len(self._ids)


# Module-level singleton — shared within a process
_singleton: SeenIDs | None = None


def get() -> SeenIDs:
    global _singleton
    if _singleton is None:
        _singleton = SeenIDs()
    return _singleton
