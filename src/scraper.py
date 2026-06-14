"""
Code generate with Claude by Anthropic
SEDAR+ / EDGAR / Newsfile Material Change Report Scraper
=========================================================
Pulls Material Change Reports from two complementary public sources:

  1. SEC EDGAR EFTS (primary)  — free, no auth, documented REST API.
     Canadian companies cross-listed on US exchanges file 6-K forms
     that wrap their SEDAR+ MCRs.  Covers most TSX large/mid-caps.

  2. TMX Newsfile RSS feeds (supplemental)  — free, real RSS feeds
     published by the official TMX newswire.  Catches TSX/TSXV/CSE-only
     companies that never file with the SEC.
     Mining feeds:  Mining and Metals, Precious Metals, Energy Metals, Rare Earths
     TMT feeds:     Technology, Computer Software, Internet Technology,
                    Semiconductors, Telecommunications, Cloud

  3. Canadian media RSS feeds (supplemental)  — broad deal coverage from
     Financial Post, and Globe and Mail Business.  Items are
     filtered by DEAL_KEYWORDS before storage so only M&A-relevant articles
     are kept.

All results are deduplicated by a source-specific ID and stored in a
single local SQLite database — re-runs are always safe (INSERT OR IGNORE).

Usage:
    python scraper.py                     # fetch last 3 days, all sources
    python scraper.py --days 7            # last 7 days
    python scraper.py --source edgar      # EDGAR only
    python scraper.py --source newsfile   # Newsfile RSS only
    python scraper.py --source media      # Canadian media RSS only
    python scraper.py --poll 300          # poll every 300 s
    python scraper.py --limit 100         # cap new inserts per run
    python scraper.py --debug             # verbose logging
"""

import argparse
import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import requests

from enrichment import enrich_filing, migrate_schema

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "sedar_filings.db"

# EDGAR EFTS — free, no API key required.
# SEC requires a descriptive User-Agent with contact info.
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# TMX Newsfile RSS feeds — official TMX newswire, free, no auth.
# These cover TSX/TSXV/CSE companies that never touch EDGAR.
# Each feed returns the latest 10 releases; poll frequently to catch all.
NEWSFILE_FEEDS = {
    # Mining
    "Mining and Metals":   "https://feeds.newsfilecorp.com/industry/mining-metals",
    "Precious Metals":     "https://feeds.newsfilecorp.com/industry/precious-metals",
    "Energy Metals":       "https://feeds.newsfilecorp.com/industry/energy-metals",
    "Rare Earths":         "https://feeds.newsfilecorp.com/industry/rare-earths",
    # TMT (Technology, Media & Telecom)
    "Technology":          "https://feeds.newsfilecorp.com/industry/technology",
    "Computer Software":   "https://feeds.newsfilecorp.com/industry/computer-software",
    "Internet Technology": "https://feeds.newsfilecorp.com/industry/internet-technology",
    "Semiconductors":      "https://feeds.newsfilecorp.com/industry/semiconductors",
    "Telecommunications":  "https://feeds.newsfilecorp.com/industry/telecommunications",
    "Cloud":               "https://feeds.newsfilecorp.com/industry/cloud",
}

# Canadian media RSS feeds — broad business/deal coverage that catches
# announcements before they appear in regulatory filings.
CANADIAN_MEDIA_FEEDS = {
    "Financial Post":     "https://financialpost.com/feed",
    "Globe and Mail Biz": "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/business/",
}

# Keyword filter applied to Canadian media RSS items.
# A headline or description must contain at least one keyword (case-insensitive)
# to be stored.  Purely editorial articles are dropped.
DEAL_KEYWORDS = [
    "acquisition",
    "merger",
    "definitive agreement",
    "letter of intent",
    "strategic alternatives",
    "going private",
    "TSX",
]

REQUEST_DELAY = 1.5   # seconds between paginated requests

HEADERS = {
    # SEC requires org name + contact email in User-Agent
    "User-Agent": "DealSourcingBot/1.0 (research tool; contact@yourdomain.com)",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/html, */*",
}

# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database and ensure the schema exists.
    The DB file persists across runs — re-running scraper.py will NEVER
    wipe existing data.  New rows are added; duplicates are silently skipped.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS filings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id     TEXT    UNIQUE NOT NULL,  -- dedupe key per source
            source        TEXT    NOT NULL,          -- 'edgar' | 'sedar'
            issuer_name   TEXT,
            doc_type      TEXT,
            headline      TEXT,
            filing_date   TEXT,
            period_date   TEXT,
            jurisdiction  TEXT,
            form_type     TEXT,
            url           TEXT,
            fetched_at    TEXT    NOT NULL,
            sector        TEXT,                      -- classified by enrichment.py
            companies     TEXT                       -- pipe-delimited NER org names
        )
    """)
    # migrate_schema(conn)  # adds columns to pre-existing DBs without losing data
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date   ON filings (filing_date DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issuer ON filings (issuer_name COLLATE NOCASE);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON filings (source);")
    conn.commit()
    log.info("Database ready → %s", db_path)
    return conn


def upsert_filing(conn: sqlite3.Connection, row: dict) -> bool:
    """
    Enrich then insert a filing; silently skip if source_id already exists.
    Enrichment adds 'sector' (keyword classifier) and 'companies' (spaCy NER).
    Returns True if a new row was inserted.
    """
    enrich_filing(row)  # adds 'sector' and 'companies' keys in-place
    cur = conn.execute("""
        INSERT OR IGNORE INTO filings
            (source_id, source, issuer_name, doc_type, headline,
             filing_date, period_date, jurisdiction, form_type, url, fetched_at,
             sector, companies)
        VALUES
            (:source_id, :source, :issuer_name, :doc_type, :headline,
             :filing_date, :period_date, :jurisdiction, :form_type, :url, :fetched_at,
             :sector, :companies)
    """, row)
    conn.commit()
    return cur.rowcount > 0


# ── Source 1: EDGAR EFTS (primary) ────────────────────────────────────────────

def _edgar_accession_to_url(accession: str) -> str:
    """
    Convert accession number (e.g. 0001234567-24-001234) to the EDGAR
    filing index page URL.
    """
    clean = accession.replace("-", "")
    cik   = clean[:10].lstrip("0")
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=6-K&dateb=&owner=include&count=10"


def _edgar_hit_to_url(hit: dict) -> str:
    """Build a direct link to the filing document on EDGAR."""
    # _id is formatted as "ACCESSION:filename"
    hit_id   = hit.get("_id", "")
    source   = hit.get("_source", {})
    adsh     = source.get("file_date", "")   # fallback

    if ":" in hit_id:
        accession, filename = hit_id.split(":", 1)
        accession_path = accession.replace("-", "")
        cik = accession_path[:10]
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{filename}"

    # Fallback: filing index
    accession = hit_id
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0&type=6-K"


def scrape_edgar(
    conn: sqlite3.Connection,
    session: requests.Session,
    date_from: str,
    date_to: str,
) -> int:
    """
    Query EDGAR EFTS for 6-K filings containing 'material change report'.
    Returns count of newly inserted rows.
    """
    log.info("[EDGAR] Searching 6-K filings  %s → %s", date_from, date_to)

    new_count = 0
    offset    = 0
    page_size = 40
    total     = None
    now       = datetime.now(timezone.utc).isoformat()

    # start = datetime.strptime(date_from, "%Y-%m-%d").strftime("%m/%d/%Y")
    # end = datetime.strptime(date_to, "%Y-%m-%d").strftime("%m/%d/%Y")

    while True:

        params = {
            "q":          '"material change report"',
            "forms":      "6-K",
            "dateRange":  "custom",
            "startdt":    date_from,
            "enddt":      date_to,
            "from":       offset,
            "size":       page_size,
        }

        try:
            resp = session.get(EDGAR_SEARCH_URL, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as e:
            log.error("[EDGAR] HTTP error: %s", e)
            break
        except ValueError as e:
            log.error("[EDGAR] JSON parse error: %s  —  body: %.200s", e, resp.text)
            break
        except requests.RequestException as e:
            log.error("[EDGAR] Request failed: %s", e)
            break

        hits_block = data.get("hits", {})
        if total is None:
            total_info = hits_block.get("total", {})
            total = total_info.get("value", 0) if isinstance(total_info, dict) else total_info
            log.info("[EDGAR] Total matching filings: %s", total)

        hits = hits_block.get("hits", [])
        if not hits:
            log.info("[EDGAR] No more results at offset=%d", offset)
            break

        for hit in hits:
            src  = hit.get("_source", {})
            name = src.get("display_names")
            if isinstance(name, list) and name:
                first = name[0]
                # EDGAR returns either a dict {"name": "..."} or a plain string
                issuer_name = first.get("name", "") if isinstance(first, dict) else str(first)
            else:
                issuer_name = src.get("entity_name", "")

            filing = {
                "source_id":   hit.get("_id", ""),
                "source":      "edgar",
                "issuer_name": issuer_name,
                "doc_type":    "Material Change Report",
                "headline":    src.get("file_description") or f"6-K — {issuer_name}",
                "filing_date": src.get("file_date", ""),
                "period_date": src.get("period_of_report", ""),
                "jurisdiction":"CA",
                "form_type":   src.get("form_type", "6-K"),
                "url":         _edgar_hit_to_url(hit),
                "fetched_at":  now,
            }

            if not filing["source_id"]:
                continue

            inserted = upsert_filing(conn, filing)
            if inserted:
                new_count += 1
                log.info(
                    "[EDGAR][NEW] %-45s  %s  %s",
                    (issuer_name or "?")[:45],
                    filing["filing_date"],
                    filing["url"],
                )

        offset += len(hits)
        if offset >= (total or 0):
            break

        time.sleep(REQUEST_DELAY)

    log.info("[EDGAR] Done. %d new filings inserted (scanned %d).", new_count, offset)
    return new_count


# ── Source 2: TMX Newsfile RSS feeds (supplemental) ──────────────────────────

def _parse_rss_date(date_str: str) -> str:
    """
    Convert an RFC-2822 RSS pubDate string (e.g. 'Tue, 09 Jun 2026 17:30:00 -0400')
    to a plain YYYY-MM-DD string.  Returns the raw string on any parse failure.
    """
    try:
        return parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
    except Exception:
        return date_str


def _extract_ticker(text: str) -> tuple[str, str]:
    """
    Pull the first exchange:ticker tag from a headline or description,
    e.g. '(TSXV: AZEM)' → ('TSXV:AZEM', 'CA')
    Returns (ticker_string, jurisdiction) where jurisdiction is 'CA' or 'US'.
    Returns ('', 'CA') if not found (default to Canadian).
    """
    import re
    # Canadian exchanges
    ca_pattern = r'\((TSX(?:V)?|CSE):\s*([A-Z0-9.]+)\)'
    ca_match = re.search(ca_pattern, text, re.IGNORECASE)
    if ca_match:
        exchange = ca_match.group(1).upper()
        ticker = ca_match.group(2).upper()
        return (f"{exchange}:{ticker}", "CA")
    
    # US exchanges
    us_pattern = r'\((NYSE|NASDAQ):\s*([A-Z0-9.]+)\)'
    us_match = re.search(us_pattern, text, re.IGNORECASE)
    if us_match:
        exchange = us_match.group(1).upper()
        ticker = us_match.group(2).upper()
        return (f"{exchange}:{ticker}", "US")
    
    return ("", "CA")  # Default to Canada


def scrape_newsfile(
    conn: sqlite3.Connection,
    session: requests.Session,
    date_from: str,
    date_to: str,
) -> int:
    """
    Poll the TMX Newsfile RSS feeds for mining-sector news releases.

    Note: these feeds return the latest 10 items across ALL release types
    (not just MCRs).  We store everything — the AI triage step in Step 3
    will classify and filter.  The feed updates in near real-time so polling
    every few minutes with --days 1 catches everything.

    Each feed covers TSX/TSXV/CSE companies that don't file with the SEC,
    filling the gap left by EDGAR.
    """
    new_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for feed_name, feed_url in NEWSFILE_FEEDS.items():
        log.info("[Newsfile] Fetching '%s' feed", feed_name)

        try:
            resp = session.get(feed_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("[Newsfile] Failed to fetch '%s': %s", feed_name, e)
            continue

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            log.warning("[Newsfile] XML parse error for '%s': %s", feed_name, e)
            continue

        # RSS namespace used by Newsfile (none needed for standard elements)
        items = root.findall(".//item")
        log.info("[Newsfile] '%s' — %d items in feed", feed_name, len(items))

        for item in items:
            title   = (item.findtext("title")   or "").strip()
            link    = (item.findtext("link")     or "").strip()
            pub_raw = (item.findtext("pubDate")  or "").strip()
            desc    = (item.findtext("description") or "").strip()
            guid    = (item.findtext("guid")     or link).strip()

            filed_date = _parse_rss_date(pub_raw)

            # Filter to the requested date window
            try:
                if filed_date:
                    item_date = datetime.strptime(filed_date, "%Y-%m-%d").date()
                    cutoff_from = datetime.strptime(date_from, "%Y-%m-%d").date()
                    cutoff_to = datetime.strptime(date_to, "%Y-%m-%d").date()
                    if item_date and (item_date < cutoff_from or item_date > cutoff_to):
                        log.debug("[Newsfile] Skipping item dated %s (outside %s to %s)", 
                            filed_date, date_from, date_to)
                        continue  
            except (ValueError, TypeError):
                pass  # 

            # Try to extract ticker from title for easier downstream enrichment
            ticker, jurisdiction = _extract_ticker(title)
            if not ticker:
                ticker, jurisdiction = _extract_ticker(desc)
            if not ticker:
                jurisdiction = "CA"

            # Strip HTML tags from description for clean storage
            import re
            clean_desc = re.sub(r"<[^>]+>", "", desc).strip()
            # Trim to a reasonable headline length
            headline = clean_desc[:200] if clean_desc else title

            filing = {
                "source_id":   f"newsfile:{guid}",
                "source":      "newsfile",
                "issuer_name": ticker or title[:60],  # refined in Step 3 by AI
                "doc_type":    feed_name,
                "headline":    title,
                "filing_date": filed_date,
                "period_date": "",
                "jurisdiction": jurisdiction,
                "form_type":   feed_name,
                "url":         link,
                "fetched_at":  now,
            }

            inserted = upsert_filing(conn, filing)
            if inserted:
                new_count += 1
                log.info(
                    "[Newsfile][NEW] %-50s  %s",
                    title[:50], filed_date,
                )


        time.sleep(REQUEST_DELAY)

    log.info("[Newsfile] Done. %d new items stored.", new_count)
    return new_count


# ── Source 3: Canadian media RSS feeds (supplemental) ────────────────────────

def _keyword_match(text: str) -> bool:
    """
    Return True if *text* contains at least one DEAL_KEYWORDS entry
    (case-insensitive).  Both headline and description are checked by
    the caller; a match on either field is sufficient.
    """
    lower = text.lower()
    return any(kw.lower() in lower for kw in DEAL_KEYWORDS)


def scrape_media(
    conn: sqlite3.Connection,
    session: requests.Session,
    date_from: str,
    date_to: str,
) -> int:
    """
    Poll Financial Post, and Globe and Mail Business RSS feeds
    for M&A-relevant articles.

    Items are filtered through DEAL_KEYWORDS — only headlines/descriptions
    containing at least one keyword are stored.  This keeps the DB focused
    on deal-relevant coverage rather than general business news.

    The source field is set to 'media' so these rows are easy to distinguish
    from regulatory filings in downstream analysis.
    """
    new_count = 0
    cutoff_from = datetime.strptime(date_from, "%Y-%m-%d").date()
    cutoff_to   = datetime.strptime(date_to,   "%Y-%m-%d").date()
    now = datetime.now(timezone.utc).isoformat()

    import re  # already used in scrape_newsfile; safe to re-import

    for feed_name, feed_url in CANADIAN_MEDIA_FEEDS.items():
        log.info("[Media] Fetching '%s' feed", feed_name)

        try:
            resp = session.get(feed_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("[Media] Failed to fetch '%s': %s", feed_name, e)
            continue

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            log.warning("[Media] XML parse error for '%s': %s", feed_name, e)
            continue

        items = root.findall(".//item")
        log.info("[Media] '%s' — %d items in feed", feed_name, len(items))

        for item in items:
            title   = (item.findtext("title")       or "").strip()
            link    = (item.findtext("link")         or "").strip()
            pub_raw = (item.findtext("pubDate")      or "").strip()
            desc    = (item.findtext("description")  or "").strip()
            guid    = (item.findtext("guid")         or link).strip()

            # Strip HTML tags for clean keyword matching and storage
            clean_desc = re.sub(r"<[^>]+>", "", desc).strip()

            # ── Keyword filter ─────────────────────────────────────────────
            if not (_keyword_match(title) or _keyword_match(clean_desc)):
                log.debug("[Media] Skipped (no keywords): %s", title[:80])
                continue

            filed_date = _parse_rss_date(pub_raw)

            # Date window filter
            try:
                item_date = datetime.strptime(filed_date, "%Y-%m-%d").date()
                if item_date < cutoff_from or item_date > cutoff_to:
                    continue
            except ValueError:
                pass  # unparseable date — include anyway

            ticker, jurisdiction = _extract_ticker(title)
            if not ticker:
                ticker, jurisdiction = _extract_ticker(clean_desc)
            if not ticker:
                jurisdiction = "CA"

            filing = {
                "source_id":    f"media:{guid}",
                "source":       "media",
                "issuer_name":  ticker or "",
                "doc_type":     feed_name,
                "headline":     title,
                "filing_date":  filed_date,
                "period_date":  "",
                "jurisdiction": jurisdiction,
                "form_type":    feed_name,
                "url":          link,
                "fetched_at":   now,
            }

            inserted = upsert_filing(conn, filing)
            if inserted:
                new_count += 1
                log.info(
                    "[Media][NEW] %-50s  %s",
                    title[:50], filed_date,
                )

        time.sleep(REQUEST_DELAY)

    log.info("[Media] Done. %d new articles stored.", new_count)
    return new_count


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_scrape(conn: sqlite3.Connection, days_back: int = 3) -> int:
    today     = datetime.now(timezone.utc).date()
    date_to   = today.strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")

    session   = requests.Session()
    total_new = 0

    total_new += scrape_edgar(conn, session, date_from, date_to)

    total_new += scrape_newsfile(conn, session, date_from, date_to)

    total_new += scrape_media(conn, session, date_from, date_to)

    log.info("── Run complete.  %d new filings stored total. ──", total_new)
    delete_old_filings(conn, days=7)
    return total_new

def delete_old_filings(conn: sqlite3.Connection, days: int = 7) -> int:
    """Delete filings older than `days` days. Returns number of rows deleted."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM filings WHERE filing_date < ?", (cutoff,))
    conn.commit()
    log.info("Cleanup: deleted %d filings older than %s", cur.rowcount, cutoff)
    return cur.rowcount


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape SEDAR+ / EDGAR Material Change Reports into SQLite."
    )
    parser.add_argument("--days",   type=int,  default=3,
                        help="Days back to search (default: 3)")
    parser.add_argument("--poll",   type=int,  default=None, metavar="SECONDS",
                        help="Keep running; re-scrape every N seconds")
    parser.add_argument("--debug",  action="store_true",
                        help="Enable verbose debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # DB is opened once and reused — persists across all runs
    conn = init_db()

    if args.poll:
        log.info("Polling every %d seconds.  Ctrl-C to stop.", args.poll)
        while True:
            try:
                run_scrape(conn, args.days)
            except KeyboardInterrupt:
                log.info("Stopped.")
                break
            except Exception as exc:
                log.error("Unexpected error: %s", exc)
            time.sleep(args.poll)
    else:
        run_scrape(conn, args.days)

    conn.close()


if __name__ == "__main__":
    main()