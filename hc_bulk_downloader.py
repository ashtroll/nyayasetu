"""
hc_bulk_downloader.py
---------------------
Download and filter judgments from the AWS Open Data Registry (Open Justice India).

Two datasets supported:
  --source hc   Indian High Court Judgments  (s3://indian-high-court-judgments)
                ~1.11 TB, 16.7M judgments, 25 high courts, 1950-2025
                Format: Parquet metadata + TAR archives of judgment HTML per bench
                Strategy: filter parquet, then stream TAR archives to extract matching files

  --source sc   Indian Supreme Court Judgments (s3://indian-supreme-court-judgments)
                Smaller (~43k judgments), 1950-2025
                Format: Parquet metadata (includes raw_html text) + year-level tar archives
                Strategy: use raw_html from parquet (no tar download needed)

Both are free, no AWS account needed.

Usage:
    python hc_bulk_downloader.py                        # HC sweep 1988-present
    python hc_bulk_downloader.py --source sc            # SC sweep 1950-present
    python hc_bulk_downloader.py --source sc --test 10  # SC: first 10 matching docs
    python hc_bulk_downloader.py --start-year 2015      # HC from 2015 onwards
    python hc_bulk_downloader.py --scan-only            # count matches, no downloads
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pandas as pd
import pdfplumber
from dotenv import load_dotenv
from loguru import logger

import dedup as _dedup

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
RAW_HTML_DIR   = Path(os.getenv("RAW_HTML_DIR", "./data/raw_html"))
PARQUET_DIR    = Path(os.getenv("PARQUET_CACHE_DIR", "./data/hc_parquet"))
HC_S3_BUCKET   = "s3://indian-high-court-judgments"
SC_S3_BUCKET   = "s3://indian-supreme-court-judgments"
S3_BUCKET      = HC_S3_BUCKET   # default; overridden by --source sc
AWS_REGION     = "ap-south-1"
RCLONE         = Path("./bin/rclone.exe") if os.name == "nt" else Path("rclone")
GDRIVE_RAW     = "lsse_gdrive:LSSE/raw_html"
BATCH_UPLOAD_EVERY = 10
DEFAULT_START_YEAR = 1988

# AWS CLI (installed in venv)
import shutil as _shutil
AWS = _shutil.which("aws") or ("venv/Scripts/aws.CMD" if os.name == "nt" else "venv/bin/aws")

# Keywords to match Section 138 NI Act / cheque dishonour cases
FILTER_PATTERNS = [
    re.compile(r'\b138\b', re.I),
    re.compile(r'negotiable\s+instruments?\s+act', re.I),
    re.compile(r'cheque\s+(dishon|bounc|return)', re.I),
    re.compile(r'cheque\s+was\s+(return|dishon)', re.I),
]


# ── AWS helpers ────────────────────────────────────────────────────────────────

def aws_ls(s3_path: str) -> list[str]:
    """List S3 prefixes (directories) at path. Returns list of prefix names."""
    result = subprocess.run(
        [AWS, "s3", "ls", "--no-sign-request", "--region", AWS_REGION, s3_path + "/"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    prefixes = []
    for line in result.stdout.splitlines():
        if "PRE" in line:
            part = line.strip().split()[-1].rstrip("/")
            prefixes.append(part)
    return prefixes


def aws_ls_files(s3_path: str) -> list[str]:
    """List actual files (not subdirectories) at an S3 path."""
    result = subprocess.run(
        [AWS, "s3", "ls", "--no-sign-request", "--region", AWS_REGION, s3_path + "/"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    files = []
    for line in result.stdout.splitlines():
        if "PRE" not in line:
            parts = line.strip().split()
            if len(parts) >= 4:
                files.append(parts[-1])
    return files


def download_parquet(year: str, court: str, bench: str, local_dir: Path) -> Path | None:
    """Download metadata.parquet for a specific year/court/bench."""
    s3_key = f"metadata/parquet/{year}/{court}/{bench}/metadata.parquet"
    local_path = local_dir / f"{year}_{court}_{bench}.parquet"
    if local_path.exists():
        return local_path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [AWS, "s3", "cp", "--no-sign-request", "--region", AWS_REGION,
         f"{HC_S3_BUCKET}/{s3_key}", str(local_path)],
        capture_output=True, text=True
    )
    return local_path if result.returncode == 0 else None


# ── Text extraction ────────────────────────────────────────────────────────────

def html_to_text(raw_html: str) -> str:
    """Strip HTML tags and return plain text."""
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(raw_html, "lxml").get_text(separator="\n").strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw_html).strip()


# ── Filtering ──────────────────────────────────────────────────────────────────

def is_section138_case(row: pd.Series) -> bool:
    """Return True if the row looks like a Section 138 NI Act case."""
    text = " ".join([
        str(row.get("title", "") or ""),
        str(row.get("description", "") or ""),
    ])
    # Must match the "138" pattern AND at least one supporting keyword
    has_138 = bool(FILTER_PATTERNS[0].search(text))
    has_ni  = any(p.search(text) for p in FILTER_PATTERNS[1:])
    return has_138 and has_ni


# ── TAR streaming ──────────────────────────────────────────────────────────────

def stream_tar_for_bench(
    year_prefix: str,
    court: str,
    bench: str,
    cnr_to_row: dict,
    total_saved: list,
    limit: int | None,
) -> int:
    """
    Stream TAR archives from S3 for one bench, extracting files whose stem
    matches a CNR in cnr_to_row. Pipes aws s3 cp stdout directly into tarfile
    so the full archive is never written to disk.

    Returns number of new docs saved.
    """
    tar_prefix = f"{HC_S3_BUCKET}/data/tar/{year_prefix}/{court}/{bench}"
    tar_files = [f for f in aws_ls_files(tar_prefix) if f.endswith(".tar") or f.endswith(".tar.gz")]
    if not tar_files:
        logger.debug(f"      No TAR files at data/tar/{year_prefix}/{court}/{bench}")
        return 0

    saved = 0
    remaining = set(cnr_to_row.keys())

    for tar_filename in tar_files:
        if not remaining or (limit and total_saved[0] >= limit):
            break

        s3_tar_uri = f"{tar_prefix}/{tar_filename}"
        logger.debug(f"      Streaming {tar_filename} ({len(remaining)} CNRs to find)")

        proc = subprocess.Popen(
            [AWS, "s3", "cp", "--no-sign-request", "--region", AWS_REGION,
             s3_tar_uri, "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            # r|* = streaming mode, auto-detect compression
            with tarfile.open(fileobj=proc.stdout, mode="r|*") as tar:
                for member in tar:
                    if not remaining or (limit and total_saved[0] >= limit):
                        break
                    # Filename format: {CNR}_{order}_{date}.pdf
                    # Match by checking which CNR is a prefix of the filename
                    fname = Path(member.name).name
                    matched_cnr = None
                    for cnr in remaining:
                        if fname.startswith(cnr):
                            matched_cnr = cnr
                            break
                    if not matched_cnr:
                        continue
                    f = tar.extractfile(member)
                    if not f:
                        continue
                    # Extract text from PDF bytes
                    try:
                        pdf_bytes = f.read()
                        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                            text = "\n\n".join(p.extract_text() or "" for p in pdf.pages).strip()
                    except Exception as pdf_exc:
                        logger.debug(f"      PDF extract failed ({fname}): {pdf_exc}")
                        continue
                    if not text:
                        continue
                    row = cnr_to_row[matched_cnr]
                    doc_id = doc_id_from_cnr(matched_cnr)
                    if already_fetched(doc_id):
                        remaining.discard(matched_cnr)
                        continue
                    meta = {
                        "doc_id": doc_id,
                        "title":  str(row.get("title", "") or ""),
                        "court":  str(row.get("court", "") or ""),
                        "date":   str(row.get("decision_date", "") or ""),
                        "judge":  str(row.get("judge", "") or ""),
                        "cnr":    matched_cnr,
                        "source": "aws_hc_bulk",
                        "url":    "",
                    }
                    save_doc(doc_id, text, meta)
                    total_saved[0] += 1
                    saved += 1
                    remaining.discard(matched_cnr)
        except Exception as exc:
            logger.warning(f"      TAR stream error ({tar_filename}): {exc}")
        finally:
            proc.terminate()

    return saved


# ── Dedup + save ───────────────────────────────────────────────────────────────

def doc_id_from_cnr(cnr: str) -> str:
    """Use CNR as doc_id (stable, unique). Fall back to hash if empty."""
    cnr = str(cnr or "").strip()
    if cnr:
        return f"hc_{cnr}"
    return "hc_" + hashlib.md5(cnr.encode()).hexdigest()[:12]


def already_fetched(doc_id: str) -> bool:
    return _dedup.get().contains(doc_id) or (RAW_HTML_DIR / f"{doc_id}.html").exists()


def save_doc(doc_id: str, text: str, meta: dict) -> None:
    if _dedup.get().contains(doc_id):
        return
    # Wrap plain text in <pre> so process_judgments.py extracts it unchanged
    html = f"<html><body><pre>{text}</pre></body></html>"
    html_path = RAW_HTML_DIR / f"{doc_id}.html"
    meta_path = RAW_HTML_DIR / f"{doc_id}.meta.json"
    html_path.write_text(html, encoding="utf-8")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _dedup.get().add(doc_id)
    logger.success(f"[SAVED] {doc_id} — {meta.get('title', '')[:60]}")


# ── GDrive sync ────────────────────────────────────────────────────────────────

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


# ── Main sweep ─────────────────────────────────────────────────────────────────

def sweep(start_year: int, limit: int | None = None, scan_only: bool = False) -> None:
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)

    end_year = 2025   # dataset goes to 2025
    total_saved = 0
    total_matched = 0

    logger.info(f"Sweeping High Court judgments {start_year}–{end_year}")

    for year in range(start_year, end_year + 1):
        if limit and total_saved >= limit:
            break

        year_prefix = f"year={year}"
        courts = aws_ls(f"{S3_BUCKET}/metadata/parquet/{year_prefix}")
        if not courts:
            logger.debug(f"  {year}: no courts found — skipping")
            continue

        logger.info(f"  {year}: {len(courts)} courts")

        for court in courts:
            if limit and total_saved >= limit:
                break

            benches = aws_ls(f"{S3_BUCKET}/metadata/parquet/{year_prefix}/{court}")
            for bench in benches:
                if limit and total_saved >= limit:
                    break

                # Download and filter Parquet
                parquet_path = download_parquet(year_prefix, court, bench, PARQUET_DIR)
                if parquet_path is None:
                    logger.warning(f"    Could not download parquet: {year}/{court}/{bench}")
                    continue

                try:
                    df = pd.read_parquet(parquet_path)
                except Exception as exc:
                    logger.warning(f"    Parquet read failed: {exc}")
                    continue

                matches = df[df.apply(is_section138_case, axis=1)]
                if matches.empty:
                    continue

                total_matched += len(matches)
                logger.info(f"    {year}/{court}/{bench}: {len(matches)} Section 138 matches")

                if scan_only:
                    continue

                # Build CNR lookup for fast matching inside TAR
                cnr_to_row = {
                    str(row.get("cnr", "") or "").strip(): row
                    for _, row in matches.iterrows()
                    if str(row.get("cnr", "") or "").strip()
                }

                total_saved_list = [total_saved]
                n = stream_tar_for_bench(
                    year_prefix, court, bench,
                    cnr_to_row, total_saved_list, limit
                )
                total_saved = total_saved_list[0]

                if n > 0 and total_saved % BATCH_UPLOAD_EVERY < n:
                    sync_to_gdrive()

    sync_to_gdrive()

    if scan_only:
        logger.success(f"Scan complete. Section 138 matches found: {total_matched}")
    else:
        logger.success(f"Done. Total new docs saved: {total_saved} | Total matches found: {total_matched}")


# ── Supreme Court sweep ────────────────────────────────────────────────────────

def is_section138_case_sc(row: pd.Series) -> bool:
    """SC filter — also checks raw_html since description snippets can be short."""
    text = " ".join([
        str(row.get("title", "") or ""),
        str(row.get("description", "") or ""),
        str(row.get("raw_html", "") or ""),
    ])
    has_138 = bool(FILTER_PATTERNS[0].search(text))
    has_ni  = any(p.search(text) for p in FILTER_PATTERNS[1:])
    return has_138 and has_ni


def sweep_sc(start_year: int, limit: int | None = None, scan_only: bool = False) -> None:
    """
    Sweep Supreme Court judgments from s3://indian-supreme-court-judgments.
    Uses raw_html from Parquet for text (no tar download needed).
    SC parquet structure: metadata/parquet/year=YYYY/metadata.parquet  (flat, no court/bench)
    """
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    sc_parquet_dir = Path("./data/sc_parquet")
    sc_parquet_dir.mkdir(parents=True, exist_ok=True)

    end_year = 2025
    total_saved = 0
    total_matched = 0

    logger.info(f"Sweeping Supreme Court judgments {start_year}–{end_year}")

    for year in range(start_year, end_year + 1):
        if limit and total_saved >= limit:
            break

        year_prefix = f"year={year}"
        s3_key = f"metadata/parquet/{year_prefix}/metadata.parquet"
        local_path = sc_parquet_dir / f"sc_{year}.parquet"

        if not local_path.exists():
            result = subprocess.run(
                [AWS, "s3", "cp", "--no-sign-request", "--region", AWS_REGION,
                 f"{SC_S3_BUCKET}/{s3_key}", str(local_path)],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                logger.debug(f"  {year}: no SC parquet — skipping")
                continue

        try:
            df = pd.read_parquet(local_path)
        except Exception as exc:
            logger.warning(f"  {year}: parquet read failed: {exc}")
            continue

        matches = df[df.apply(is_section138_case_sc, axis=1)]
        total_matched += len(matches)

        if matches.empty:
            logger.debug(f"  {year}: {len(df)} SC judgments, 0 Section 138 matches")
            continue

        logger.info(f"  {year}: {len(df)} SC judgments, {len(matches)} Section 138 matches")

        if scan_only:
            continue

        for _, row in matches.iterrows():
            if limit and total_saved >= limit:
                break

            path_id = str(row.get("path", "") or "").strip()
            doc_id  = f"sc_{path_id}" if path_id else f"sc_{str(row.get('cnr',''))}"

            if already_fetched(doc_id):
                logger.debug(f"    [SKIP] {doc_id}")
                continue

            raw_html = str(row.get("raw_html", "") or "")
            text = html_to_text(raw_html) if raw_html else ""
            if not text:
                logger.debug(f"    [SKIP] {doc_id} — no text in raw_html")
                continue

            meta = {
                "doc_id":   doc_id,
                "title":    str(row.get("title", "") or ""),
                "court":    "Supreme Court of India",
                "date":     str(row.get("decision_date", "") or ""),
                "judge":    str(row.get("judge", "") or ""),
                "citation": str(row.get("citation", "") or ""),
                "source":   "aws_sc_bulk",
                "url":      "",
            }
            save_doc(doc_id, text, meta)
            total_saved += 1

            if total_saved % BATCH_UPLOAD_EVERY == 0:
                sync_to_gdrive()

    sync_to_gdrive()

    if scan_only:
        logger.success(f"SC scan complete. Section 138 matches: {total_matched}")
    else:
        logger.success(f"SC done. Saved: {total_saved} | Matches found: {total_matched}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LSSE Bulk Downloader — High Court + Supreme Court (AWS Open Data)"
    )
    parser.add_argument("--source", choices=["hc", "sc"], default="hc",
                        help="Dataset: hc = High Courts (default), sc = Supreme Court")
    parser.add_argument("--test", type=int, metavar="N", help="Download only N matching docs")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR,
                        help=f"Start year (default: {DEFAULT_START_YEAR})")
    parser.add_argument("--scan-only", action="store_true",
                        help="Count Section 138 matches without downloading")
    args = parser.parse_args()

    if args.source == "sc":
        sweep_sc(start_year=args.start_year, limit=args.test, scan_only=args.scan_only)
    else:
        sweep(start_year=args.start_year, limit=args.test, scan_only=args.scan_only)
