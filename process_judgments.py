"""
process_judgments.py
--------------------
Step 3: Cognitive pipeline for LSSE.

For each scraped HTML judgment:
  1. Download from Google Drive (rclone copy)
  2. Clean text with BeautifulSoup
  3. Extract metadata (court, date, judges, statutes)
  4. Run OpenNyAI Rhetorical Role Labeling
     - Falls back to rule-based classifier if OpenNyAI unavailable
  5. Build structured JSON matching the LSSE schema
  6. Upload JSON to Google Drive → delete local copies

Usage:
    python process_judgments.py          # process all unprocessed cases
    python process_judgments.py --test 1 # process 1 case and print output
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from loguru import logger
from tqdm import tqdm

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
RAW_HTML_DIR      = Path(os.getenv("RAW_HTML_DIR",      "./data/raw_html"))
PROCESSED_JSON_DIR = Path(os.getenv("PROCESSED_JSON_DIR", "./data/processed_json"))
RCLONE            = Path("./bin/rclone.exe") if os.name == "nt" else Path("rclone")

GDRIVE_RAW       = "lsse_gdrive:LSSE/raw_html"
GDRIVE_PROCESSED = "lsse_gdrive:LSSE/processed_json"

# ── OpenNyAI (graceful import) ────────────────────────────────────────────────
OPENNYAI_OK = False
_rr_pipeline = None   # lazy-initialized on first use
try:
    from opennyai import Pipeline
    from opennyai.utils import Data
    OPENNYAI_OK = True
    logger.info("OpenNyAI loaded successfully")
except Exception as exc:
    logger.warning(f"OpenNyAI not available ({exc}). Using rule-based fallback.")

# Rhetorical role label sets from OpenNyAI
RR_FACTS     = {"Facts"}
RR_ARGS      = {"Argument by Petitioner", "Argument by Respondent",
                "Arguments", "Argument"}
RR_RATIO     = {"Ratio of the Decision", "Ratio"}
RR_DECISION  = {"Decision", "Ruling by Present Court"}
RR_STATUTE   = {"Statute"}

# ── rclone helpers ────────────────────────────────────────────────────────────

def rclone_list_remote(remote_path: str) -> list[str]:
    """Return list of filenames in a remote rclone path."""
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


def rclone_upload_delete(local_path: Path, remote_dir: str) -> bool:
    remote = f"{remote_dir}/{local_path.name}"
    result = subprocess.run(
        [str(RCLONE), "copyto", str(local_path), remote,
         "--tpslimit", "10", "--drive-chunk-size", "64M"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        local_path.unlink(missing_ok=True)
        return True
    logger.warning(f"Upload failed: {local_path.name} — {result.stderr.strip()}")
    return False

# ── HTML cleaning ─────────────────────────────────────────────────────────────

def extract_from_html(html: str) -> dict:
    """
    Parse Indian Kanoon HTML and return:
      {text, title, court, date, judge_names, statutes_raw}
    """
    soup = BeautifulSoup(html, "lxml")

    # Title — from <title> tag e.g. "X vs Y on 12 January, 2024"
    raw_title = soup.title.get_text(strip=True) if soup.title else ""
    raw_title = re.sub(r"\s*[-|]\s*Indian Kanoon.*$", "", raw_title).strip()

    # Date — last "on DD Month, YYYY" segment from title
    date_match = re.search(r"on\s+(\d{1,2}\s+\w+,?\s+\d{4})", raw_title, re.I)
    date = date_match.group(1).strip() if date_match else ""

    # Court — the h3 that looks like "X High Court" / "Supreme Court" / "Tribunal"
    court = ""
    for h3 in soup.find_all("h3"):
        t = h3.get_text(strip=True)
        if re.search(r"(High Court|Supreme Court|Tribunal|District Court|Sessions Court)", t, re.I):
            court = t
            break

    # Full judgment text — in <pre> tag on Indian Kanoon
    pre = soup.find("pre")
    full_text = pre.get_text("\n") if pre else soup.get_text("\n")
    full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()

    # Judge names — first few lines of judgment often "Name, J." or "Name, CJ"
    judge_names = []
    for line in full_text.splitlines()[:20]:
        line = line.strip()
        if re.search(r",\s*J\.?$|,\s*C\.?J\.?$|,\s*Judge$", line, re.I):
            name = re.sub(r",\s*(C\.?J\.?|J\.?)$", "", line, flags=re.I).strip()
            if name and len(name) < 60:
                judge_names.append(name)

    # Statutes — "Section X of/under the Y Act, YYYY" patterns
    statute_pattern = re.compile(
        r"(?:Section|Sec\.?)\s+\d+[\w\(\)]*(?:\s+(?:of|under)\s+(?:the\s+)?[\w\s]+Act(?:,\s*\d{4})?)?",
        re.I
    )
    statutes_raw = list(dict.fromkeys(
        m.group().strip() for m in statute_pattern.finditer(full_text)
    ))[:20]

    return {
        "text":         full_text,
        "title":        raw_title,
        "court":        court,
        "date":         date,
        "judge_names":  judge_names,
        "statutes_raw": statutes_raw,
    }

# ── Rhetorical Role Labeling ──────────────────────────────────────────────────

def label_with_opennyai(text: str) -> dict[str, list[str]]:
    """Run OpenNyAI rhetorical role labeling. Returns label→sentences mapping."""
    global _rr_pipeline
    try:
        if _rr_pipeline is None:
            _rr_pipeline = Pipeline(["Rhetorical_Role"], use_gpu=False, verbose=False)
        data = Data(text, preprocessing_nlp_model="en_core_web_sm", use_gpu=False)
        results = _rr_pipeline(data)
        if not results:
            return {}
        buckets = {k: [] for k in ("facts", "arguments", "ratio", "decision")}
        for sent in results[0].get("annotations", []):
            labels = sent.get("labels") or []
            label = labels[0] if labels else ""
            sentence = sent.get("text", "")
            if not sentence:
                continue
            if label in RR_FACTS:
                buckets["facts"].append(sentence)
            elif label in RR_ARGS:
                buckets["arguments"].append(sentence)
            elif label in RR_RATIO:
                buckets["ratio"].append(sentence)
            elif label in RR_DECISION:
                buckets["decision"].append(sentence)
        return buckets
    except Exception as exc:
        logger.warning(f"OpenNyAI labeling failed: {exc}. Falling back.")
        return {}


def label_rule_based(text: str) -> dict[str, list[str]]:
    """
    Rule-based rhetorical role classifier.
    Segments judgment text by keyword patterns.
    """
    # Split into paragraphs
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]

    facts_kw     = re.compile(r"\b(facts|background|brief facts|case|appellant|respondent|petitioner|complainant|accused)\b", re.I)
    args_kw      = re.compile(r"\b(submitted|argued|contended|pleaded|urged|counsel|advocate|learned)\b", re.I)
    ratio_kw     = re.compile(r"\b(held|therefore|thus|accordingly|in our view|we are of the opinion|it is settled|the law is)\b", re.I)
    decision_kw  = re.compile(r"\b(allowed|dismissed|quashed|set aside|disposed|order|result|accordingly)\b", re.I)

    buckets = {"facts": [], "arguments": [], "ratio": [], "decision": []}

    for para in paragraphs:
        facts_s = bool(facts_kw.search(para))
        args_s  = bool(args_kw.search(para))
        ratio_s = bool(ratio_kw.search(para))
        dec_s   = bool(decision_kw.search(para))

        # Simple priority: ratio > decision > arguments > facts
        if ratio_s:
            buckets["ratio"].append(para)
        elif dec_s and len(para) < 300:
            buckets["decision"].append(para)
        elif args_s:
            buckets["arguments"].append(para)
        elif facts_s:
            buckets["facts"].append(para)

    return buckets


def extract_decision_label(decision_sentences: list[str]) -> str:
    """Return 'Allowed', 'Dismissed', or 'Unknown' from decision text."""
    combined = " ".join(decision_sentences).lower()
    if re.search(r"\ballowed\b", combined):
        return "Allowed"
    if re.search(r"\bdismissed\b|\brejected\b|\bquashed\b", combined):
        return "Dismissed"
    return "Unknown"

# ── Main processing ───────────────────────────────────────────────────────────

def process_one(doc_id: str, html: str) -> dict:
    """Full pipeline for a single document. Returns the LSSE JSON schema dict."""
    meta = extract_from_html(html)
    text = meta["text"]

    # Rhetorical role labeling
    if OPENNYAI_OK:
        buckets = label_with_opennyai(text)
    else:
        buckets = {}

    if not any(buckets.values()):      # OpenNyAI unavailable or empty
        buckets = label_rule_based(text)

    return {
        "id":             doc_id,
        "title":          meta["title"],
        "court":          meta["court"],
        "date":           meta["date"],
        "judge_names":    meta["judge_names"],
        "statutes":       meta["statutes_raw"],
        "facts_text":     " ".join(buckets.get("facts",     [])),
        "arguments_text": " ".join(buckets.get("arguments", [])),
        "ratio_text":     " ".join(buckets.get("ratio",     [])),
        "decision":       extract_decision_label(buckets.get("decision", [])),
    }


def already_processed(doc_id: str) -> bool:
    """Check if JSON already exists locally or on GDrive."""
    local = PROCESSED_JSON_DIR / f"{doc_id}.json"
    if local.exists():
        return True
    # Check remote
    remote_files = rclone_list_remote(GDRIVE_PROCESSED)
    return f"{doc_id}.json" in remote_files


def run(limit: int | None = None, verbose: bool = False) -> None:
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_JSON_DIR.mkdir(parents=True, exist_ok=True)

    # Get list of scraped HTML files from GDrive
    logger.info("Listing scraped files on Google Drive...")
    remote_files = rclone_list_remote(GDRIVE_RAW)
    html_files   = [f for f in remote_files if f.endswith(".html")]

    if not html_files:
        logger.warning("No HTML files found on GDrive at lsse_gdrive:LSSE/raw_html/")
        logger.info("Run scraper.py first to populate the dataset.")
        return

    logger.info(f"Found {len(html_files)} HTML files to process")

    processed = 0
    for filename in tqdm(html_files, desc="Processing judgments"):
        doc_id = filename.replace(".html", "")

        if already_processed(doc_id):
            logger.debug(f"[SKIP] {doc_id} already processed")
            continue

        # Download HTML from GDrive
        local_html = RAW_HTML_DIR / filename
        logger.info(f"Downloading {filename} from GDrive...")
        if not rclone_download(f"{GDRIVE_RAW}/{filename}", local_html):
            logger.warning(f"Could not download {filename}")
            continue

        # Process
        html = local_html.read_text(encoding="utf-8", errors="ignore")
        result = process_one(doc_id, html)

        # Save JSON locally
        json_path = PROCESSED_JSON_DIR / f"{doc_id}.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        if verbose:
            print(json.dumps(result, ensure_ascii=False, indent=2))

        logger.success(f"[DONE] {doc_id} — {result['title'][:55]} | {result['decision']}")

        # Upload JSON to GDrive, delete local JSON + HTML
        rclone_upload_delete(json_path,  GDRIVE_PROCESSED)
        local_html.unlink(missing_ok=True)

        processed += 1
        if limit and processed >= limit:
            logger.info(f"Limit of {limit} reached.")
            break

    logger.info(f"Pipeline complete. Processed: {processed} new documents.")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSSE Cognitive Pipeline")
    parser.add_argument("--test", type=int, metavar="N",
                        help="Process only N documents (test mode)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print each JSON result to terminal")
    args = parser.parse_args()
    run(limit=args.test, verbose=args.verbose)
