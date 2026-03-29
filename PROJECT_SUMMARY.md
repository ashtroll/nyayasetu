# LSSE (Legal Strategy Search Engine) — Project Summary & Status

**Last Updated:** 2026-03-26
**Project Goal:** Build a semantic + lexical search engine for Indian Section 138 NI Act judgments
**Target:** 100k+ documents by end of collection phase

---

## 1. ARCHITECTURE OVERVIEW

### Tech Stack
| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Data Collection** | Python 3.10+, requests, beautifulsoup4, stem, httpx, asyncio | Web scraping, Tor rotation, API fetching |
| **Remote Storage** | rclone + Google Drive (2TB student pack) | Permanent document storage |
| **Indexing** | MeiliSearch (lexical), ChromaDB (semantic BAAI/bge-m3) | Search & retrieval |
| **Frontend** | Streamlit | Web UI for end users |
| **PDF Processing** | pdfplumber, AWS S3 (TAR streaming) | Extract text from PDFs without disk overhead |

### Data Flow
```
Scrapers (3 sources) → data/raw_html/ (local temp)
                     → lsse_gdrive:LSSE/raw_html/ (permanent GDrive)
                     ↓
                  data/seen_ids.txt (dedup index)
                     ↓
process_judgments.py (OpenNyAI parse) → data/processed_json/
                     ↓
index_chroma.py (semantic) + index_meili.py (lexical)
                     ↓
app.py (Streamlit UI)
```

---

## 2. DATA COLLECTION PIPELINES

### 2.1 HC Bulk Downloader (`hc_bulk_downloader.py`)
**Status:** ✅ Running (PID 1107)
**Source:** AWS S3 Indian High Court dataset (16.7M judgments)
**Method:** TAR streaming + CNR matching (no disk overhead)

- **Location:** `s3://indian-high-court-judgments/data/tar/year=YYYY/court=X/bench=Y/data.tar`
- **Key:** TAR files contain PDFs named `{CNR}_{order}_{date}.pdf`
- **Extraction:** `pdfplumber.open(io.BytesIO(pdf_bytes))` → text
- **Storage:** `data/raw_html/hc_{CNR}.html` (locally) → `lsse_gdrive:LSSE/raw_html/` (permanent)
- **Current:** Processing year ~2011 (started from 1988, found matches in recent years first)
- **Dedup:** All doc IDs checked against `data/seen_ids.txt` before save

**Known Issue (FIXED):** Initial approach tried `data/pdf/` S3 path (0% PDF availability). Switched to `data/tar/` with TAR streaming.

---

### 2.2 SC Bulk Downloader (`sc_bulk_downloader.py`)
**Status:** ✅ Complete (207 docs saved)
**Source:** Supreme Court judgments via web scraping
**Method:** HTTP download + HTML → text via BeautifulSoup

- **Storage:** `data/raw_html/sc_{path}.html` (e.g., `sc_2007_12_1134_1143.html`)
- **Format:** Wrapped in `<html><body><pre>text</pre></body></html>`
- **Metadata:** Each doc has `.meta.json` sidecar

---

### 2.3 Async Scraper (`scraper_async.py`)
**Status:** ✅ Running (restarted ~21:28)
**Source:** Indian Law portal (IK scraped results)
**Method:** Async httpx + Tor rotation + **monthly date slicing**

- **Discovery:** 468 monthly slices (Jan 1988 – present) × 100 results/slice = **up to 46.8k discoverable docs**
- **Previous issue:** Flat search capped at ~1000 results. Fixed by adding date slice producer.
- **Storage:** `data/raw_html/{doc_id}.html` (IK numeric IDs)
- **Dedup:** Checked against `data/seen_ids.txt`
- **Tor:** Enabled (stem module)

---

### 2.4 IK API Fetcher (`api_fetcher.py`)
**Status:** ⏸️ Blocked (403 rate-limited)
**Source:** Indian Law API (₹0.20/doc, 50k free/month non-commercial)
**Method:** POST requests with date-range slicing

- **Current Issue:** Token rate-limited after ~1,210 docs in 32 min (2026-03-26 ~21:00)
- **Recovery:** Expected ~4-6 hours from block time
- **Circuit Breaker:** Added — aborts query after 3 consecutive 403s
- **Backoff:** 60s wait on 403 error
- **Storage:** `data/raw_html/{doc_id}.html` (IK numeric IDs)
- **Dedup:** Checked against `data/seen_ids.txt`

---

## 3. PERSISTENT DEDUPLICATION SYSTEM

**File:** `dedup.py` (new module, used by all 4 pipelines)

### How It Works
```python
class SeenIDs:
    - Loads data/seen_ids.txt at startup into an in-memory set
    - contains(doc_id) → O(1) lookup
    - add(doc_id) → appends to file + updates set
    - Module-level singleton via get()
```

### Initial State
- **1,594 doc IDs** backfilled from GDrive file listing (HC + SC bulk from previous runs)
- **Persistent:** Never deleted, survives restarts
- **Shared:** All 4 pipelines check/append to same `data/seen_ids.txt`

### Why It's Needed
- **Local files deleted after GDrive sync** → `already_fetched()` checks only local disk (unreliable)
- **Solution:** Persistent index across all pipelines + storage backend

---

## 4. STORAGE ARCHITECTURE

### Local Staging (`data/raw_html/`)
- **Temporary:** Files deleted after rclone sync
- **Purpose:** Buffer for active downloads
- **Dedup sync:** Every 10 docs, push to GDrive via rclone

### Permanent (`lsse_gdrive:LSSE/raw_html/`)
- **Google Drive** (2TB student pack)
- **File naming:**
  - HC: `hc_{CNR}.html` + `hc_{CNR}.meta.json`
    Example: `hc_GAHC010160222012.html`
  - SC: `sc_{path}.html` + `sc_{path}.meta.json`
    Example: `sc_2007_12_1134_1143.html`
  - API/Scraper: `{numeric_id}.html` + `{numeric_id}.meta.json`
    Example: `1234567.html`
- **Capacity:** 2TB ≈ 170M docs @ ~11.7KB avg per doc → **100k target = ~1.2GB (0.6% utilization)**

### Deduplication Index (`data/seen_ids.txt`)
- **Persistent:** Never synced away
- **Format:** One doc_id per line (append-only)
- **Current:** 1,594 entries
- **Used by:** All 4 pipelines for `already_fetched()` check

---

## 5. NEXT STEPS (POST-100K MILESTONE)

### Phase 2: Processing
```
1. run process_judgments.py
   ↓ Parses HTML → OpenNyAI (rhetorical roles, NER, facts/arguments/ratio)
   ↓ Outputs: data/processed_json/ (LSSE schema)

2. run index_chroma.py
   ↓ Embeds JSON with BAAI/bge-m3
   ↓ Stores: database/chroma/

3. run index_meili.py
   ↓ Indexes JSON (lexical search)
   ↓ Stores: database/meili/

4. streamlit run app.py
   ↓ Launch UI at localhost:8501
```

### Phase 3: Model Training (User's Idea)
- **Architecture:** RAG (Retrieval-Augmented Generation)
- **Flow:** User uploads query → find relevant cases via semantic search (ChromaDB) + lexical filter (MeiliSearch) → send context + query to Claude/GPT fine-tuned model
- **Advantage:** No model retraining needed; uses existing judgments as context

---

## 6. CURRENT PROGRESS

| Pipeline | Docs | Status | Last Update |
|----------|------|--------|-------------|
| HC bulk | TBD | ✅ Running | 2026-03-26 20:57 |
| SC bulk | 207 | ✅ Complete | 2026-03-26 |
| Async scraper | TBD | ✅ Running | 2026-03-26 21:28 |
| API fetcher | ~1,210 | ⏸️ 403 blocked | 2026-03-26 ~21:00 |
| **Total** | **~1,417+** | — | — |
| **Target** | **100,000** | — | **~98.6k remaining** |

**Milestone Watch:** `watch_progress.py` (updates `data/progress.log` every 30 min, creates `data/MILESTONE_100K.txt` at 100k)

---

## 7. AUTOMATION SCRIPTS

| Script | Purpose | Run |
|--------|---------|-----|
| `setup.bat` | One-click initialization (deps, rclone, MeiliSearch, Tor) | Double-click once |
| `start_services.ps1` | Start MeiliSearch + remount GDrive (run each session) | Each session |
| `mount_gdrive.sh` / `.bat` | Mount Google Drive to `X:` | Called by `start_services.ps1` |
| `bin/rclone.exe`, `meilisearch.exe` | Auto-downloaded by setup.bat | Auto |

---

## 8. KEY FILES & PATHS

```
c:/Users/as157/OneDrive/Desktop/Legallity/
├── requirements.txt                      ← Install: pip install -r requirements.txt
├── .env.example                           ← Copy to .env, fill IK_API_TOKEN
├── setup.bat                              ← One-time setup
├── setup.ps1                              ← PowerShell logic
├── start_services.ps1                     ← Start MeiliSearch + GDrive
├── bin/                                   ← rclone.exe, meilisearch.exe
│
├── Data Collection
│   ├── scraper.py                         ← Sync Tor scraper
│   ├── scraper_async.py                   ← Async multi-Tor (w/ date slicing)
│   ├── api_fetcher.py                     ← IK API (currently 403 blocked)
│   ├── hc_bulk_downloader.py              ← HC AWS TAR streaming
│   ├── sc_bulk_downloader.py              ← SC web scraping
│   ├── dedup.py                           ← Persistent dedup module
│
├── Processing & Indexing
│   ├── process_judgments.py               ← OpenNyAI parse → JSON
│   ├── index_chroma.py                    ← Semantic indexing
│   ├── index_meili.py                     ← Lexical indexing
│   ├── watch_progress.py                  ← Monitor collection progress
│
├── Frontend
│   ├── app.py                             ← Streamlit UI
│
└── data/
    ├── raw_html/                          ← Local staging (temp)
    ├── processed_json/                    ← Parsed JSON output
    ├── seen_ids.txt                       ← Persistent dedup index
    ├── progress.log                       ← Collection metrics
    │
    └── database/
        ├── chroma/                        ← ChromaDB semantic index
        └── meili/                         ← MeiliSearch data dir
```

---

## 9. KNOWN ISSUES & SOLUTIONS

| Issue | Root Cause | Solution | Status |
|-------|-----------|----------|--------|
| HC bulk 0 docs | PDFs at `data/pdf/` S3 path unavailable | Switch to `data/tar/` with TAR streaming | ✅ Fixed |
| Async scraper stops early | Flat search caps at ~1000 results | Add 468 monthly date slices | ✅ Fixed |
| API fetcher 403s | Token rate-limited after ~1,210 docs in 32 min | Circuit breaker + backoff + wait for unblock | ✅ Fixed (waiting) |
| Dedup broken on restart | `already_fetched()` checks local disk only (deleted after sync) | Created `dedup.py` + `seen_ids.txt` persistent index | ✅ Fixed |
| Windows encoding errors | `print()` fails on special chars (→, ─) | Use `sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))` | ✅ Fixed |

---

## 10. QUICK START

```bash
# 1. Initialize (one-time)
setup.bat

# 2. Each session
start_services.ps1

# 3. Start data collection
python scraper_async.py &
python hc_bulk_downloader.py &
# API fetcher starts when token unblocks (wait ~4-6 hours from 21:00)

# 4. Monitor progress
python watch_progress.py

# 5. Once you see "MILESTONE_100K.txt" created
python process_judgments.py
python index_chroma.py
python index_meili.py
streamlit run app.py
```

---

## 11. QUESTIONS FOR NEXT SESSION

1. **When API token unblocks:** Restart `api_fetcher.py`
2. **At 100k documents:** Confirm you want to proceed to Phase 2 (processing)
3. **Model training:** Ready to discuss RAG architecture + Claude/GPT integration
4. **UI/UX:** Any specific search features or filters for the Streamlit app?
