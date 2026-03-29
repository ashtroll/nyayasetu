# LSSE - Legal Strategy Search Engine

A comprehensive data collection and indexing system for Indian Section 138 Negotiable Instruments Act judgments.

## Overview

LSSE aggregates judgments from multiple sources, deduplicates records, and indexes them for semantic + lexical search using MeiliSearch and ChromaDB.

### Features

- **Multi-source data collection** (4 pipelines):
  - Indian High Court AWS bulk downloads (TAR streaming)
  - Supreme Court web scraping
  - Indian Law portal async scraper with date slicing
  - Indian Law API fetcher with rate-limit handling

- **Persistent deduplication** across all pipelines
- **Semantic indexing** with BAAI/bge-m3 embeddings (ChromaDB)
- **Lexical indexing** with MeiliSearch
- **Streamlit UI** for search and document retrieval
- **Automated setup** via PowerShell scripts

## Tech Stack

- **Python 3.10+** — data collection, processing
- **rclone + Google Drive** — remote storage (2TB)
- **MeiliSearch** — lexical search engine
- **ChromaDB** — semantic vector database
- **Streamlit** — web frontend
- **OpenNyAI** — rhetorical role labeling, NER
- **AWS S3** — High Court judgment source
- **Tor** — anonymized scraping (optional)

## Quick Start

### 1. Initial Setup (Windows)

```bash
# One-time setup
setup.bat

# Or manually:
pip install -r requirements.txt
```

### 2. Start Services (Each Session)

```powershell
.\start_services.ps1
# Starts MeiliSearch + mounts Google Drive to X:
```

### 3. Run Data Collection Pipelines

```bash
# HC bulk downloader (AWS TAR streaming)
python hc_bulk_downloader.py

# SC bulk downloader (web scraping)
python sc_bulk_downloader.py

# Async scraper (Indian Law portal with date slicing)
python scraper_async.py

# IK API fetcher (requires .env IK_API_TOKEN)
python api_fetcher.py

# Monitor progress
python watch_progress.py
```

All pipelines save to `data/raw_html/` locally, sync to `lsse_gdrive:LSSE/raw_html/` (Google Drive).

### 4. After 100k Documents Collected

```bash
# Parse with OpenNyAI
python process_judgments.py

# Index semantically
python index_chroma.py

# Index lexically
python index_meili.py

# Launch UI
streamlit run app.py
# Opens at http://localhost:8501
```

## Configuration

### Environment Variables (`.env`)

```env
IK_API_TOKEN=your_token_here
```

Get IK API token from Indian Law portal (₹0.20/doc, 50k free/month non-commercial).

### Storage Architecture

- **Local staging**: `data/raw_html/` (temporary, synced then deleted)
- **Permanent**: `lsse_gdrive:LSSE/raw_html/` (Google Drive, 2TB student pack)
- **Dedup index**: `data/seen_ids.txt` (persistent, shared across pipelines)
- **Processed JSON**: `data/processed_json/` (indexed documents)
- **Search indices**: `database/chroma/`, `database/meili/`

## Pipeline Details

### HC Bulk Downloader
- Source: `s3://indian-high-court-judgments/` (AWS, free)
- Method: TAR streaming + CNR matching
- Format: PDFs → text via pdfplumber
- Dedup: Persistent `data/seen_ids.txt` index
- Status: Processes year-by-year starting from 1988

### SC Bulk Downloader
- Source: Supreme Court website
- Method: HTTP download + HTML parsing
- Format: HTML → text
- Status: ~207 documents

### Async Scraper
- Source: Indian Law portal
- Method: httpx + Tor rotation + monthly date slicing (468 slices)
- Discovery: Up to 46.8k documents (468 months × 100 results/month)
- Dedup: Persistent index
- Status: Running with date slicing

### IK API Fetcher
- Source: Indian Law API
- Cost: ₹0.20/doc, 50k free/month non-commercial
- Method: POST requests with date-range filtering
- Dedup: Persistent index
- Rate limiting: 403 on token exhaustion (waits for reset)

## Deduplication

All pipelines share a persistent deduplication system:

- **File**: `data/seen_ids.txt` (append-only, one doc_id per line)
- **Usage**: Loaded at startup into memory set, O(1) lookup
- **Scope**: Cross-pipeline, survives restarts
- **Module**: `dedup.py` (shared by all pipelines)

## Known Issues & Fixes

| Issue | Fix |
|-------|-----|
| HC bulk 0 docs | Switched from `data/pdf/` (0% available) to `data/tar/` (TAR streaming) |
| Async scraper stops early | Added 468 monthly date slices instead of flat paginated search |
| API fetcher 403s | Added circuit breaker + 60s backoff on rate limit |
| Dedup fails on restart | Created persistent `seen_ids.txt` index |
| Windows encoding errors | Use `sys.stdout.buffer.write()` with UTF-8 encoding |

## Project Status

- **Documents collected**: ~1,417
- **Target**: 100,000
- **Remaining**: ~98,600
- **Milestone alert**: `data/MILESTONE_100K.txt` created when reached

## Architecture & Next Steps

See [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) for detailed architecture, file paths, and post-100k milestones.

## License

[Specify your license here]

## Contact

Built as part of the LSSE (Legal Strategy Search Engine) initiative.
