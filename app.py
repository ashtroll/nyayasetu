"""
app.py
------
Step 5: Streamlit dashboard for LSSE.

Features:
  - Lexical search via MeiliSearch (fast, typo-tolerant)
  - Semantic search via ChromaDB + BAAI/bge-m3 embeddings
  - Faceted filters: court, outcome
  - Result cards with expandable facts / arguments / ratio sections

Usage:
    streamlit run app.py
"""

import os
from pathlib import Path

import meilisearch
import streamlit as st
from dotenv import load_dotenv

try:
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
except ImportError:
    chromadb = None
    SentenceTransformerEmbeddingFunction = None

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
MEILI_HOST       = os.getenv("MEILI_HOST", "http://127.0.0.1:7700")
MEILI_MASTER_KEY = os.getenv("MEILI_MASTER_KEY", "")
CHROMA_DB_DIR    = Path(os.getenv("CHROMA_DB_DIR", "./database/chroma"))
EMBED_MODEL      = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
INDEX_NAME       = "lsse_judgments"
COLLECTION_NAME  = "lsse_judgments"

# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="LSSE — Legal Strategy Search",
    page_icon="⚖",
    layout="wide",
)

# ── Cached resources ───────────────────────────────────────────────────────────

@st.cache_resource(show_spinner="Connecting to MeiliSearch...")
def get_meili_index():
    try:
        client = meilisearch.Client(MEILI_HOST, MEILI_MASTER_KEY or None)
        client.health()
        return client.index(INDEX_NAME)
    except Exception:
        return None


@st.cache_resource(show_spinner="Loading embedding model (BAAI/bge-m3)...")
def get_chroma_collection():
    if chromadb is None or SentenceTransformerEmbeddingFunction is None:
        return None
    try:
        ef = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
        client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
        return client.get_collection(name=COLLECTION_NAME, embedding_function=ef)
    except Exception:
        return None


# ── Facet helpers ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def get_all_courts() -> list[str]:
    index = get_meili_index()
    if not index:
        return []
    try:
        result = index.search("", {"limit": 5000, "attributesToRetrieve": ["court"]})
        courts = sorted({h["court"] for h in result.get("hits", []) if h.get("court")})
        return courts
    except Exception:
        return []


# ── Search functions ───────────────────────────────────────────────────────────

def build_meili_filter(courts: list[str], decisions: list[str]) -> str | None:
    parts = []
    if courts:
        escaped = [f'court = "{c}"' for c in courts]
        parts.append("(" + " OR ".join(escaped) + ")")
    if decisions:
        escaped = [f'decision = "{d}"' for d in decisions]
        parts.append("(" + " OR ".join(escaped) + ")")
    return " AND ".join(parts) if parts else None


def search_meili(query: str, courts: list, decisions: list, limit: int) -> list[dict]:
    index = get_meili_index()
    if not index:
        return []
    params = {
        "limit": limit,
        "attributesToRetrieve": [
            "id", "title", "court", "date", "decision",
            "judge_names", "statutes", "facts_text", "arguments_text", "ratio_text",
        ],
    }
    f = build_meili_filter(courts, decisions)
    if f:
        params["filter"] = f
    try:
        return index.search(query or "", params).get("hits", [])
    except Exception as exc:
        st.error(f"MeiliSearch error: {exc}")
        return []


def build_chroma_where(courts: list, decisions: list) -> dict | None:
    """Build ChromaDB $and/$in filter from selected values."""
    clauses = []
    if courts:
        clauses.append({"court": {"$in": courts}} if len(courts) > 1 else {"court": courts[0]})
    if decisions:
        clauses.append({"decision": {"$in": decisions}} if len(decisions) > 1 else {"decision": decisions[0]})
    if not clauses:
        return None
    return {"$and": clauses} if len(clauses) > 1 else clauses[0]


def search_chroma(query: str, courts: list, decisions: list, limit: int) -> list[dict]:
    if not query:
        return []
    col = get_chroma_collection()
    if not col or col.count() == 0:
        return []
    kwargs: dict = {"query_texts": [query], "n_results": min(limit, col.count())}
    where = build_chroma_where(courts, decisions)
    if where:
        kwargs["where"] = where
    try:
        res = col.query(**kwargs)
        hits = []
        for doc_id, meta, dist in zip(res["ids"][0], res["metadatas"][0], res["distances"][0]):
            hits.append({
                "id":               doc_id,
                "title":            meta.get("title", ""),
                "court":            meta.get("court", ""),
                "date":             meta.get("date", ""),
                "decision":         meta.get("decision", "Unknown"),
                "_semantic_score":  round(1 - dist, 3),
            })
        return hits
    except Exception as exc:
        st.error(f"ChromaDB error: {exc}")
        return []


# ── Card renderer ──────────────────────────────────────────────────────────────

DECISION_ICON = {"Allowed": "🟢", "Dismissed": "🔴", "Unknown": "⚪"}


def render_card(hit: dict, show_score: bool = False) -> None:
    decision = hit.get("decision", "Unknown")
    icon = DECISION_ICON.get(decision, "⚪")

    with st.container(border=True):
        col_title, col_badge = st.columns([5, 1])
        with col_title:
            st.markdown(f"**{hit.get('title', 'Untitled')}**")
        with col_badge:
            st.markdown(f"{icon} {decision}")

        meta = []
        if hit.get("court"):
            meta.append(f"🏛 {hit['court']}")
        if hit.get("date"):
            meta.append(f"📅 {hit['date']}")
        if show_score and "_semantic_score" in hit:
            meta.append(f"🎯 {hit['_semantic_score']:.0%} match")
        if meta:
            st.caption("   ·   ".join(meta))

        # Expandable judgment detail
        has_detail = any(hit.get(k) for k in ("facts_text", "arguments_text", "ratio_text"))
        if has_detail:
            with st.expander("View judgment details"):
                for label, key, cap in [
                    ("Facts", "facts_text", 1500),
                    ("Arguments", "arguments_text", 1000),
                    ("Ratio Decidendi", "ratio_text", 1000),
                ]:
                    text = hit.get(key, "")
                    if text:
                        st.markdown(f"**{label}**")
                        st.write(text[:cap] + ("…" if len(text) > cap else ""))
                statutes = hit.get("statutes", [])
                if statutes:
                    st.markdown("**Statutes cited**")
                    st.caption(", ".join(statutes))


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚖ LSSE")
    st.caption("Legal Strategy Search Engine")
    st.divider()

    search_mode = st.radio(
        "Search mode",
        ["Lexical (fast)", "Semantic (AI)", "Both"],
        help=(
            "**Lexical** — keyword + typo-tolerant (MeiliSearch)\n\n"
            "**Semantic** — meaning-based via BAAI/bge-m3 embeddings (ChromaDB)\n\n"
            "**Both** — side-by-side comparison"
        ),
    )

    st.divider()
    st.subheader("Filters")
    courts = get_all_courts()
    selected_courts    = st.multiselect("Court", options=courts, placeholder="All courts")
    selected_decisions = st.multiselect(
        "Outcome", options=["Allowed", "Dismissed", "Unknown"], placeholder="All outcomes"
    )
    result_limit = st.slider("Max results", 5, 50, 20)

    st.divider()
    meili_ok  = get_meili_index() is not None
    chroma_ok = get_chroma_collection() is not None
    st.caption(f"MeiliSearch: {'🟢 online' if meili_ok else '🔴 offline'}")
    st.caption(f"ChromaDB:    {'🟢 ready'  if chroma_ok else '🔴 not found'}")


# ── Main content ───────────────────────────────────────────────────────────────

st.title("Legal Strategy Search Engine")
st.caption("Search Section 138 NI Act judgments · facts · arguments · ratio · outcomes")

query = st.text_input(
    "search",
    placeholder="e.g.  cheque dishonour conviction   ·   limitation period s.138   ·   acquittal benefit of doubt",
    label_visibility="collapsed",
)

active = bool(query or selected_courts or selected_decisions)

if not active:
    # ── Landing page ──────────────────────────────────────────────────────────
    st.divider()
    c1, c2, c3 = st.columns(3)
    with c1:
        idx = get_meili_index()
        n = 0
        if idx:
            try:
                n = idx.get_stats().number_of_documents
            except Exception:
                pass
        st.metric("Judgments indexed", n)
    with c2:
        col = get_chroma_collection()
        st.metric("Embeddings stored", col.count() if col else 0)
    with c3:
        st.metric("Search backends", "2  (lexical + semantic)")

    st.divider()
    st.markdown("""
**How to use**

1. Type a query in the search bar above.
2. Choose **Lexical** for keyword/phrase search (instant, typo-tolerant).
3. Choose **Semantic** for concept/meaning search (AI-powered, ~1–2 s).
4. Use **Both** for a side-by-side comparison.
5. Narrow results with the **Court** and **Outcome** filters in the sidebar.

**Example queries**
- `cheque dishonour not admitted by accused`
- `Section 138 limitation period complaint`
- `conviction set aside insufficient funds`
- `plea of settlement compromise cheque case`
    """)

else:
    # ── Results ───────────────────────────────────────────────────────────────
    use_lexical  = search_mode in ("Lexical (fast)", "Both")
    use_semantic = search_mode in ("Semantic (AI)",  "Both")

    if search_mode == "Both":
        col_lex, col_sem = st.columns(2)

    if use_lexical:
        lex_hits = search_meili(query, selected_courts, selected_decisions, result_limit)
        target = col_lex if search_mode == "Both" else st
        with target:
            st.subheader(f"Lexical — {len(lex_hits)} result{'s' if len(lex_hits) != 1 else ''}")
            if not lex_hits:
                st.info("No results. Try a broader query or remove filters.")
            for h in lex_hits:
                render_card(h, show_score=False)

    if use_semantic:
        if not query:
            sem_hits = []
        else:
            sem_hits = search_chroma(query, selected_courts, selected_decisions, result_limit)
        target = col_sem if search_mode == "Both" else st
        with target:
            st.subheader(f"Semantic — {len(sem_hits)} result{'s' if len(sem_hits) != 1 else ''}")
            if not sem_hits:
                st.info("No results." if query else "Enter a query for semantic search.")
            for h in sem_hits:
                render_card(h, show_score=True)
