"""
app.py  –  Deal Sourcing Dashboard (Supabase edition)
======================================================

Reads live from Supabase — always fresh, no cache staleness.

Required secrets (set in Streamlit Cloud → App Settings → Secrets):
    SUPABASE_URL = "https://xxxx.supabase.co"
    SUPABASE_KEY = "your-anon-key"   ← use anon key here (read-only is fine)
"""

import os
from datetime import date, timedelta

import requests
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Deal Flow",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap');

html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }

section[data-testid="stSidebar"] {
    background: #0d0d0d;
    border-right: 1px solid #1f1f1f;
}
section[data-testid="stSidebar"] * { color: #c8c8c8 !important; }
section[data-testid="stSidebar"] .stSelectbox label,
section[data-testid="stSidebar"] .stDateInput label,
section[data-testid="stSidebar"] .stTextInput label {
    color: #666 !important;
    font-size: 0.7rem !important;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-family: 'IBM Plex Mono', monospace;
}

.main .block-container { background: #f7f6f3; padding-top: 2rem; }

.deal-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.05rem; font-weight: 600; color: #111;
    line-height: 1.4; margin-bottom: 0.2rem;
}
.deal-meta {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.7rem; color: #888; letter-spacing: 0.04em;
}
.deal-card {
    background: #fff; border: 1px solid #e5e3de;
    border-left: 3px solid #111; border-radius: 2px;
    padding: 1rem 1.2rem; margin-bottom: 0.6rem;
}
.detail-panel {
    background: #fff; border: 1px solid #e5e3de;
    border-left: 4px solid #2563eb; border-radius: 2px;
    padding: 1.4rem 1.6rem; margin-bottom: 1rem;
}
.detail-headline {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.1rem; font-weight: 600; color: #111;
    line-height: 1.45; margin-bottom: 0.8rem;
}
.detail-row {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.72rem; color: #555; margin-bottom: 0.35rem;
    display: flex; gap: 0.6rem;
}
.detail-label { color: #aaa; text-transform: uppercase; letter-spacing: 0.07em; min-width: 6rem; }
.detail-companies { margin-top: 0.8rem; padding-top: 0.8rem; border-top: 1px solid #f0ede8; }
.company-chip {
    display: inline-block; font-family: 'IBM Plex Mono', monospace;
    font-size: 0.65rem; background: #f3f4f6; color: #374151;
    border: 1px solid #e5e7eb; border-radius: 2px;
    padding: 0.2rem 0.55rem; margin: 0.2rem 0.3rem 0.2rem 0;
}
.badge {
    display: inline-block; font-family: 'IBM Plex Mono', monospace;
    font-size: 0.65rem; font-weight: 600; letter-spacing: 0.06em;
    padding: 0.15rem 0.5rem; border-radius: 2px; margin-right: 0.4rem;
    text-transform: uppercase;
}
.badge-TMT     { background: #dbeafe; color: #1d4ed8; }
.badge-Mining  { background: #fef3c7; color: #92400e; }
.badge-Unknown { background: #f3f4f6; color: #6b7280; }
.source-tag {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.65rem;
    color: #aaa; text-transform: uppercase; letter-spacing: 0.06em;
}
.stat-block {
    background: #fff; border: 1px solid #e5e3de; border-radius: 2px;
    padding: 0.8rem 1.2rem; text-align: center;
}
.stat-number { font-family: 'IBM Plex Mono', monospace; font-size: 1.6rem; font-weight: 600; color: #111; line-height: 1; }
.stat-label  { font-family: 'IBM Plex Mono', monospace; font-size: 0.65rem; color: #888; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 0.25rem; }
.section-header {
    font-family: 'IBM Plex Mono', monospace; font-size: 0.7rem; color: #999;
    text-transform: uppercase; letter-spacing: 0.1em;
    border-bottom: 1px solid #e5e3de; padding-bottom: 0.4rem;
    margin-bottom: 1rem; margin-top: 1.5rem;
}
.empty-state { text-align: center; padding: 3rem; color: #aaa; font-family: 'IBM Plex Mono', monospace; font-size: 0.85rem; }
</style>
""", unsafe_allow_html=True)

# ── Supabase config ───────────────────────────────────────────────────────────

SUPABASE_URL = st.secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY") or os.environ.get("SUPABASE_KEY", "")

SECTORS = ["All", "TMT", "Mining", "Unknown"]
SOURCES = ["All", "edgar", "newsfile", "media"]

SOURCE_LABELS = {
    "edgar":    "EDGAR",
    "newsfile": "Newsfile",
    "media":    "Media",
}

# ── Supabase query helpers ────────────────────────────────────────────────────

def _sb_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def _build_params(sector, source, since: str, until: str, search: str, limit: int) -> dict:
    """Build Supabase REST query params."""
    params = {
        "filing_date": f"gte.{since}",
        "order":       "filing_date.desc",
        "limit":       str(limit),
        "select":      "*",
    }
    # filing_date upper bound
    params["filing_date"] = f"gte.{since}"

    if sector and sector != "All":
        params["sector"] = f"eq.{sector}"
    if source and source != "All":
        params["source"] = f"eq.{source}"

    # Supabase doesn't support AND on same column via simple params —
    # we pass both bounds using the range filter header trick
    return params


@st.cache_data(ttl=60)
def fetch_filings(sector, source, since: str, until: str, search: str, limit: int) -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []

    params = {
        "select":       "*",
        "filing_date":  f"gte.{since}",
        "order":        "filing_date.desc",
        "limit":        str(limit),
    }
    if sector and sector != "All":
        params["sector"] = f"eq.{sector}"
    if source and source != "All":
        params["source"] = f"eq.{source}"

    headers = {**_sb_headers(), "Range-Unit": "items"}

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/filings",
            params=params,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        st.error(f"Supabase error: {e}")
        return []

    # Apply date upper bound + search client-side (simple, avoids complex param encoding)
    results = []
    for r in rows:
        fd = r.get("filing_date", "") or ""
        if fd > until:
            continue
        if search:
            haystack = " ".join(filter(None, [r.get("headline"), r.get("issuer_name")])).lower()
            if search.lower() not in haystack:
                continue
        results.append(r)

    return results


@st.cache_data(ttl=60)
def fetch_stats(since: str, until: str, sector, source, search: str) -> dict:
    rows = fetch_filings(sector, source, since, until, search, limit=1000)
    if not rows:
        return {}

    total = len(rows)

    edgar_n    = sum(1 for r in rows if r.get("source") == "edgar")
    newsfile_n = sum(1 for r in rows if r.get("source") == "newsfile")

    from collections import Counter
    sector_counts = Counter(r.get("sector") or "Unknown" for r in rows)
    top_sector = sector_counts.most_common(1)[0][0] if sector_counts else "—"

    return {
        "total":      total,
        "edgar_n":    edgar_n,
        "newsfile_n": newsfile_n,
        "top_sector": top_sector,
    }


def fetch_filing_by_id(filing_id) -> dict | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/filings",
            params={"id": f"eq.{filing_id}", "select": "*", "limit": "1"},
            headers=_sb_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else None
    except Exception:
        return None


# ── Sidebar ───────────────────────────────────────────────────────────────────

today     = date.today()
yesterday = today - timedelta(days=1)

with st.sidebar:
    st.markdown("### 📡 Deal Flow")
    st.markdown("<div style='height:1px;background:#1f1f1f;margin:0.5rem 0 1.2rem'></div>", unsafe_allow_html=True)

    search = st.text_input("Search", placeholder="keyword or issuer…")

    sector_filter = st.selectbox("Sector", SECTORS)
    source_filter = st.selectbox("Source", SOURCES)

    st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)

    # ── Quick-range buttons ───────────────────────────────────────────────────
    col_a, col_b = st.columns(2)

    with col_a:
        if st.button("Last 7 days", use_container_width=True):
            st.session_state.date_from = today - timedelta(days=7)
            st.session_state.date_to   = today
            st.cache_data.clear()
            st.rerun()

    with col_b:
        if st.button("Last 14 days", use_container_width=True):
            st.session_state.date_from = today - timedelta(days=14)
            st.session_state.date_to   = today
            st.cache_data.clear()
            st.rerun()

    # Default: yesterday's deals on first load
    if "date_from" not in st.session_state:
        st.session_state.date_from = yesterday
    if "date_to" not in st.session_state:
        st.session_state.date_to = yesterday

    date_from = st.date_input("From", value=st.session_state.date_from, key="date_from")
    date_to   = st.date_input("To",   value=st.session_state.date_to,   key="date_to")

    limit = st.select_slider("Max results", options=[25, 50, 100, 250, 500], value=50)

    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)

    # Manual refresh — busts the 60-second cache instantly
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    if not SUPABASE_URL or not SUPABASE_KEY:
        st.error("Supabase credentials missing.\nSet SUPABASE_URL and SUPABASE_KEY in app secrets.")

# ── Session state for detail view ─────────────────────────────────────────────

if "selected_filing_id" not in st.session_state:
    st.session_state.selected_filing_id = None

# ── Main ──────────────────────────────────────────────────────────────────────

st.markdown("## Deal Sourcing")
st.markdown(
    "<div style='font-family:IBM Plex Mono,monospace;font-size:0.75rem;color:#888;margin-bottom:1.5rem'>"
    "SEDAR+ · EDGAR · Newsfile · Canadian Media  ·  Live via Supabase"
    "</div>",
    unsafe_allow_html=True,
)

since_str = date_from.isoformat()
until_str = date_to.isoformat()

stats = fetch_stats(since_str, until_str, sector_filter, source_filter, search)

if stats:
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(
            f"<div class='stat-block'><div class='stat-number'>{stats['total']:,}</div>"
            f"<div class='stat-label'>Filings in range</div></div>",
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"<div class='stat-block'><div class='stat-number'>{stats['edgar_n']:,}</div>"
            f"<div class='stat-label'>EDGAR</div></div>",
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"<div class='stat-block'><div class='stat-number'>{stats['newsfile_n']:,}</div>"
            f"<div class='stat-label'>Newsfile</div></div>",
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f"<div class='stat-block'><div class='stat-number'>{stats['top_sector']}</div>"
            f"<div class='stat-label'>Top sector</div></div>",
            unsafe_allow_html=True,
        )

# ── Deal detail panel ─────────────────────────────────────────────────────────

if st.session_state.selected_filing_id is not None:
    filing = fetch_filing_by_id(st.session_state.selected_filing_id)
    if filing:
        st.markdown("<div class='section-header'>Filing Detail</div>", unsafe_allow_html=True)

        sector       = filing.get("sector") or "Unknown"
        source       = filing.get("source") or ""
        issuer       = filing.get("issuer_name") or "—"
        headline     = filing.get("headline") or "—"
        fdate        = filing.get("filing_date") or "—"
        url          = filing.get("url") or ""
        companies    = filing.get("companies") or ""
        jurisdiction = filing.get("jurisdiction") or "—"
        doc_type     = filing.get("doc_type") or "—"
        source_label = SOURCE_LABELS.get(source, source.upper())
        badge_class  = f"badge-{sector.replace(' ', '-')}"

        link_html = (
            f'<a href="{url}" target="_blank" '
            f'style="color:#2563eb;text-decoration:none;font-size:0.72rem;'
            f'font-family:IBM Plex Mono,monospace">↗ View source document</a>'
            if url else ""
        )

        company_chips = ""
        if companies:
            names = [c.strip() for c in companies.split("|") if c.strip()]
            if names:
                company_chips = (
                    "<div class='detail-companies'>"
                    "<div style='font-family:IBM Plex Mono,monospace;font-size:0.65rem;"
                    "color:#aaa;text-transform:uppercase;letter-spacing:0.07em;"
                    "margin-bottom:0.45rem'>Extracted companies</div>"
                    + "".join(f"<span class='company-chip'>🏢 {n}</span>" for n in names)
                    + "</div>"
                )

        st.markdown(f"""
        <div class="detail-panel">
            <div class="detail-headline">{headline}</div>
            <div class="detail-row"><span class="detail-label">Sector</span><span class="badge {badge_class}">{sector}</span></div>
            <div class="detail-row"><span class="detail-label">Issuer</span><span>{issuer}</span></div>
            <div class="detail-row"><span class="detail-label">Date</span><span>{fdate}</span></div>
            <div class="detail-row"><span class="detail-label">Source</span><span>{source_label} &nbsp;·&nbsp; {doc_type}</span></div>
            <div class="detail-row"><span class="detail-label">Jurisdiction</span><span>{jurisdiction}</span></div>
            <div class="detail-row" style="margin-top:0.5rem"><span class="detail-label"></span>{link_html}</div>
            {company_chips}
        </div>
        """, unsafe_allow_html=True)

        if st.button("✕ Close detail", key="close_detail"):
            st.session_state.selected_filing_id = None
            st.rerun()

# ── Deal list ─────────────────────────────────────────────────────────────────

st.markdown("<div class='section-header'>Filings</div>", unsafe_allow_html=True)

filings = fetch_filings(sector_filter, source_filter, since_str, until_str, search, limit)

if not filings:
    st.markdown(
        "<div class='empty-state'>No filings match your filters.<br>"
        "Try widening the date range or clearing the search.</div>",
        unsafe_allow_html=True,
    )
else:
    for f in filings:
        filing_id = f.get("id")
        sector    = f.get("sector") or "Unknown"
        source    = f.get("source") or ""
        issuer    = f.get("issuer_name") or "—"
        headline  = f.get("headline") or "—"
        fdate     = f.get("filing_date") or ""
        url       = f.get("url") or ""
        companies = f.get("companies") or ""

        badge_class  = f"badge-{sector.replace(' ', '-')}"
        source_label = SOURCE_LABELS.get(source, source.upper())

        headline_html = (
            f'<a href="{url}" target="_blank" style="color:#111;text-decoration:none">{headline}</a>'
            if url else headline
        )

        companies_html = ""
        if companies:
            names = [c.strip() for c in companies.split("|") if c.strip()]
            companies_html = (
                "<div style='margin-top:0.4rem'>"
                + "".join(
                    f"<span style='font-family:IBM Plex Mono,monospace;font-size:0.65rem;"
                    f"color:#555;margin-right:0.8rem'>🏢 {n}</span>"
                    for n in names
                )
                + "</div>"
            )

        is_selected = st.session_state.selected_filing_id == filing_id
        card_border = "#2563eb" if is_selected else "#111"

        col_card, col_btn = st.columns([11, 1])
        with col_card:
            st.markdown(f"""
            <div class="deal-card" style="border-left-color:{card_border}">
                <div class="deal-title">{headline_html}</div>
                <div class="deal-meta" style="margin-top:0.35rem">
                    <span class="badge {badge_class}">{sector}</span>
                    <span class="source-tag">{source_label}</span>
                    &nbsp;·&nbsp;
                    <span style="font-family:'IBM Plex Mono',monospace;font-size:0.7rem;color:#aaa">{issuer}</span>
                    &nbsp;·&nbsp;
                    <span style="font-family:'IBM Plex Mono',monospace;font-size:0.7rem;color:#aaa">{fdate}</span>
                </div>
                {companies_html}
            </div>
            """, unsafe_allow_html=True)
        with col_btn:
            btn_label = "▲" if is_selected else "↗"
            btn_help  = "Close detail" if is_selected else "View detail"
            if st.button(btn_label, key=f"detail_{filing_id}", help=btn_help):
                if is_selected:
                    st.session_state.selected_filing_id = None
                else:
                    st.session_state.selected_filing_id = filing_id
                st.rerun()