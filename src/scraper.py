"""
scraper.py  –  Deal Sourcing Scraper (Supabase edition)
=========================================================
Pulls Material Change Reports from three sources:

  1. SEC EDGAR EFTS (primary)
  2. TMX Newsfile RSS feeds (supplemental)
  3. Canadian media RSS feeds (supplemental)

All results are upserted into Supabase (PostgreSQL) via the REST API.
Deduplication is handled by a UNIQUE constraint on source_id.
Rows older than 14 days are deleted after each run.

Required env vars:
    SUPABASE_URL   – e.g. https://xxxx.supabase.co
    SUPABASE_KEY   – service_role key (not anon key — needs DELETE access)

Usage:
    python scraper.py                     # fetch last 3 days, all sources
    python scraper.py --days 7            # last 7 days
    python scraper.py --source edgar      # EDGAR only
    python scraper.py --source newsfile   # Newsfile RSS only
    python scraper.py --source media      # Canadian media RSS only
    python scraper.py --debug             # verbose logging
"""

import argparse
import logging
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from dotenv import load_dotenv

import requests

from enrichment import enrich_filing

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

NEWSFILE_FEEDS = {
    "Mining and Metals":   "https://feeds.newsfilecorp.com/industry/mining-metals",
    "Precious Metals":     "https://feeds.newsfilecorp.com/industry/precious-metals",
    "Energy Metals":       "https://feeds.newsfilecorp.com/industry/energy-metals",
    "Rare Earths":         "https://feeds.newsfilecorp.com/industry/rare-earths",
    "Technology":          "https://feeds.newsfilecorp.com/industry/technology",
    "Computer Software":   "https://feeds.newsfilecorp.com/industry/computer-software",
    "Internet Technology": "https://feeds.newsfilecorp.com/industry/internet-technology",
    "Semiconductors":      "https://feeds.newsfilecorp.com/industry/semiconductors",
    "Telecommunications":  "https://feeds.newsfilecorp.com/industry/telecommunications",
    "Cloud":               "https://feeds.newsfilecorp.com/industry/cloud",
}

CANADIAN_MEDIA_FEEDS = {
    "Financial Post":     "https://financialpost.com/feed",
    "Globe and Mail Biz": "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/business/",
}

DEAL_KEYWORDS = [
    "acquisition",
    "merger",
    "definitive agreement",
    "letter of intent",
    "strategic alternatives",
    "going private",
    "TSX",
]

REQUEST_DELAY = 1.5

HEADERS = {
    "User-Agent": "DealSourcingBot/1.0 (research tool; contact@yourdomain.com)",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/html, */*",
}

# ── Supabase client ───────────────────────────────────────────────────────────

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    log.info(f"Loaded environment from {env_path}")
else:
    # Try to load from current working directory
    load_dotenv()
    log.info("Loaded environment from current directory (or no .env found)")

class SupabaseClient:
    """
    Thin wrapper around the Supabase REST API.
    Uses only requests — no supabase-py SDK needed in CI.
    """

    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        self.key = os.environ["SUPABASE_KEY"]
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=ignore-duplicates",  # upsert: skip on conflict
        }

    def upsert(self, table: str, row: dict) -> bool:
        """
        Insert a row; silently skip if source_id already exists.
        Returns True if a new row was inserted.
        """
        resp = requests.post(
            f"{self.url}/rest/v1/{table}",
            json=row,
            headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=representation"},
        )
        if resp.status_code not in (200, 201):
            log.warning("Supabase upsert failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        # 201 = inserted, 200 with empty body = ignored duplicate
        return resp.status_code == 201 and bool(resp.json())

    def delete_old(self, table: str, date_col: str, cutoff: str) -> int:
        """Delete rows where date_col < cutoff (ISO date string)."""
        resp = requests.delete(
            f"{self.url}/rest/v1/{table}",
            params={date_col: f"lt.{cutoff}"},
            headers={**self.headers, "Prefer": "return=representation"},
        )
        if resp.status_code not in (200, 204):
            log.warning("Supabase delete failed (%s): %s", resp.status_code, resp.text[:200])
            return 0
        try:
            deleted = len(resp.json())
        except Exception:
            deleted = 0
        return deleted


def get_supabase() -> SupabaseClient:
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set as environment variables.\n"
            "In GitHub Actions: add them as repository secrets.\n"
            "Locally: export them in your shell or add to a .env file."
        )
    return SupabaseClient()


# ── Filing upsert ─────────────────────────────────────────────────────────────

def upsert_filing(db: SupabaseClient, row: dict) -> bool:
    """Enrich then upsert a filing. Returns True if newly inserted."""
    enrich_filing(row)
    return db.upsert("filings", row)


# ── Helpers (shared with original scraper) ────────────────────────────────────

def _parse_rss_date(raw: str) -> str:
    """Parse RFC 2822 or ISO date string → YYYY-MM-DD. Falls back to today."""
    if not raw:
        return date.today().isoformat()
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()


def _extract_ticker(text: str):
    """
    Pull the first (EXCHANGE: TICKER) pattern from text.
    Returns (ticker, jurisdiction) or (None, None).
    """
    import re
    m = re.search(r"\((?P<ex>TSX(?:V)?|CSE|NYSE|NASDAQ):\s*(?P<tk>[A-Z0-9.]+)\)", text)
    if not m:
        return None, None
    ex = m.group("ex")
    tk = m.group("tk")
    jurisdiction = "CA" if ex in ("TSX", "TSXV", "CSE") else "US"
    return tk, jurisdiction


# ── Source 1: EDGAR EFTS ──────────────────────────────────────────────────────

def scrape_edgar(db: SupabaseClient, session: requests.Session, date_from: str, date_to: str) -> int:
    log.info("[EDGAR] Scraping %s → %s", date_from, date_to)
    new_count = 0
    now = datetime.now(timezone.utc).isoformat()

    params = {
        "q":        '"material change"',
        "dateRange": "custom",
        "startdt":  date_from,
        "enddt":    date_to,
        "forms":    "6-K",
        "_source":  "hits.hits._source,hits.hits._id",
        "from":     0,
        "size":     40,
    }

    while True:
        try:
            resp = session.get(EDGAR_SEARCH_URL, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error("[EDGAR] Request failed: %s", e)
            break

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            src = hit.get("_source", {})
            hit_id = hit.get("_id", "")

            accession = hit_id.split(":")[0] if ":" in hit_id else hit_id
            accession_path = accession.replace("-", "")
            cik = accession_path[:10]
            filename = hit_id.split(":", 1)[1] if ":" in hit_id else ""
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{filename}"
                if filename else
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=6-K"
            )

            filing = {
                "source_id":    f"edgar:{accession}",
                "source":       "edgar",
                "issuer_name":  src.get("entity_name", src.get("display_names", [""])[0] if src.get("display_names") else ""),
                "doc_type":     src.get("file_type", "6-K"),
                "headline":     src.get("period_of_report", src.get("file_date", "")),
                "filing_date":  src.get("file_date", date_from)[:10],
                "period_date":  src.get("period_of_report", ""),
                "jurisdiction": "US",
                "form_type":    "6-K",
                "url":          url,
                "fetched_at":   now,
            }

            inserted = upsert_filing(db, filing)
            if inserted:
                new_count += 1
                log.info("[EDGAR][NEW] %s  %s", filing["issuer_name"][:50], filing["filing_date"])

        total = data.get("hits", {}).get("total", {}).get("value", 0)
        params["from"] += len(hits)
        if params["from"] >= total or params["from"] >= 200:
            break

        time.sleep(REQUEST_DELAY)

    log.info("[EDGAR] Done. %d new filings stored.", new_count)
    return new_count


# ── Source 2: Newsfile RSS ────────────────────────────────────────────────────

def scrape_newsfile(db: SupabaseClient, session: requests.Session, date_from: str, date_to: str) -> int:
    log.info("[Newsfile] Scraping feeds %s → %s", date_from, date_to)
    new_count = 0
    cutoff_from = datetime.strptime(date_from, "%Y-%m-%d").date()
    cutoff_to   = datetime.strptime(date_to,   "%Y-%m-%d").date()
    now = datetime.now(timezone.utc).isoformat()

    for feed_name, feed_url in NEWSFILE_FEEDS.items():
        log.info("[Newsfile] Fetching '%s'", feed_name)
        try:
            resp = session.get(feed_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            log.warning("[Newsfile] Failed '%s': %s", feed_name, e)
            continue

        for item in root.findall(".//item"):
            title   = (item.findtext("title")   or "").strip()
            link    = (item.findtext("link")     or "").strip()
            pub_raw = (item.findtext("pubDate")  or "").strip()
            guid    = (item.findtext("guid")     or link).strip()

            filed_date = _parse_rss_date(pub_raw)
            try:
                item_date = datetime.strptime(filed_date, "%Y-%m-%d").date()
                if item_date < cutoff_from or item_date > cutoff_to:
                    continue
            except ValueError:
                pass

            ticker, jurisdiction = _extract_ticker(title)

            # Detect sector from feed name
            sector = "Mining" if any(k in feed_name for k in ("Mining", "Metals", "Rare", "Energy Metals")) else "TMT"

            filing = {
                "source_id":    f"newsfile:{guid}",
                "source":       "newsfile",
                "issuer_name":  ticker or "",
                "doc_type":     feed_name,
                "headline":     title,
                "filing_date":  filed_date,
                "period_date":  "",
                "jurisdiction": jurisdiction or "CA",
                "form_type":    feed_name,
                "url":          link,
                "fetched_at":   now,
            }

            inserted = upsert_filing(db, filing)
            if inserted:
                new_count += 1
                log.info("[Newsfile][NEW] %-50s  %s", title[:50], filed_date)

        time.sleep(REQUEST_DELAY)

    log.info("[Newsfile] Done. %d new items stored.", new_count)
    return new_count


# ── Source 3: Canadian media RSS ──────────────────────────────────────────────

def _keyword_match(text: str) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in DEAL_KEYWORDS)


def scrape_media(db: SupabaseClient, session: requests.Session, date_from: str, date_to: str) -> int:
    log.info("[Media] Scraping feeds %s → %s", date_from, date_to)
    new_count = 0
    cutoff_from = datetime.strptime(date_from, "%Y-%m-%d").date()
    cutoff_to   = datetime.strptime(date_to,   "%Y-%m-%d").date()
    now = datetime.now(timezone.utc).isoformat()

    import re

    for feed_name, feed_url in CANADIAN_MEDIA_FEEDS.items():
        log.info("[Media] Fetching '%s'", feed_name)
        try:
            resp = session.get(feed_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            log.warning("[Media] Failed '%s': %s", feed_name, e)
            continue

        for item in root.findall(".//item"):
            title   = (item.findtext("title")       or "").strip()
            link    = (item.findtext("link")         or "").strip()
            pub_raw = (item.findtext("pubDate")      or "").strip()
            desc    = (item.findtext("description")  or "").strip()
            guid    = (item.findtext("guid")         or link).strip()

            clean_desc = re.sub(r"<[^>]+>", "", desc).strip()

            if not (_keyword_match(title) or _keyword_match(clean_desc)):
                continue

            filed_date = _parse_rss_date(pub_raw)
            try:
                item_date = datetime.strptime(filed_date, "%Y-%m-%d").date()
                if item_date < cutoff_from or item_date > cutoff_to:
                    continue
            except ValueError:
                pass

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

            inserted = upsert_filing(db, filing)
            if inserted:
                new_count += 1
                log.info("[Media][NEW] %-50s  %s", title[:50], filed_date)

        time.sleep(REQUEST_DELAY)

    log.info("[Media] Done. %d new articles stored.", new_count)
    return new_count


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_scrape(db: SupabaseClient, days_back: int = 3) -> int:
    today     = datetime.now(timezone.utc).date()
    date_to   = today.strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")

    session   = requests.Session()
    total_new = 0

    total_new += scrape_edgar(db, session, date_from, date_to)
    total_new += scrape_newsfile(db, session, date_from, date_to)
    total_new += scrape_media(db, session, date_from, date_to)

    log.info("── Run complete. %d new filings stored. ──", total_new)

    # Keep only last 14 days
    cutoff = (today - timedelta(days=14)).isoformat()
    deleted = db.delete_old("filings", "filing_date", cutoff)
    log.info("Cleanup: deleted %d filings older than %s", deleted, cutoff)

    return total_new


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape deal filings into Supabase.")
    parser.add_argument("--days",  type=int, default=3, help="Days back to search (default: 3)")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    db = get_supabase()
    run_scrape(db, args.days)


if __name__ == "__main__":
    main()