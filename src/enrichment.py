"""
enrichment.py  –  NER company extraction + sector classification (Supabase edition)
===================================================================================

Two features, zero external API calls:

  1. classify_sector(text)
     Keyword-based sector tagging.  Returns one of:
       'TMT'     – software, SaaS, cybersecurity, semiconductor, cloud, …
       'Mining'  – gold, silver, copper, lithium, mineral, …
       'Unknown' – no strong signal found

  2. extract_companies(text)
     Uses spaCy's en_core_web_sm model to pull ORG named entities from a
     headline or description, then filters out obvious false positives
     (stock exchanges, regulators, common nouns).

  3. enrich_filing(row: dict) -> dict
     Adds 'sector' and 'companies' keys to a filing dict before insert.

  4. enrich_supabase()
     Back-fills sector + companies for every existing row that is still NULL.
     Safe to run multiple times (skips already-enriched rows).

Usage:
    python enrichment.py                  # back-fill existing Supabase data
    python enrichment.py --dry-run        # preview changes without updating
    python enrichment.py --batch 100      # process 100 rows at a time
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# ── Load environment variables ────────────────────────────────────────────────

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Supabase client ───────────────────────────────────────────────────────────

class SupabaseClient:
    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        self.key = os.environ["SUPABASE_KEY"]
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def get_unenriched(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch filings where sector is NULL."""
        resp = requests.get(
            f"{self.url}/rest/v1/filings",
            params={
                "select": "*",
                "sector": "is.null",
                "limit": str(limit),
                "offset": str(offset),
                "order": "filing_date.asc",
            },
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def update_filing(self, filing_id: int, sector: str, companies: str) -> bool:
        """Update a filing with sector and companies."""
        resp = requests.patch(
            f"{self.url}/rest/v1/filings",
            params={"id": f"eq.{filing_id}"},
            json={"sector": sector, "companies": companies},
            headers=self.headers,
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            log.warning("Update failed for ID %s: %s", filing_id, resp.status_code)
            return False
        return True

    def get_count_unenriched(self) -> int:
        """Get count of filings needing enrichment."""
        resp = requests.get(
            f"{self.url}/rest/v1/filings",
            params={
                "select": "id",
                "sector": "is.null",
                "limit": 0,
            },
            headers=self.headers,
            timeout=30,
        )
        # Supabase returns a count header
        return int(resp.headers.get("Content-Range", "0/0").split("/")[-1] or 0)


def get_supabase() -> SupabaseClient:
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set as environment variables.\n"
            "Create a .env file in the project root with:\n"
            "    SUPABASE_URL=https://your-project-id.supabase.co\n"
            "    SUPABASE_KEY=your-service-role-key"
        )
    return SupabaseClient()


# ── Sector classifier (unchanged) ────────────────────────────────────────────

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "TMT": [
        "software",
        "saas",
        "cybersecurity",
        "cyber security",
        "technology",
        "tech",
        "semiconductor",
        "artificial intelligence",
        " ai ",
        "machine learning",
        "cloud",
        "internet",
        "telecom",
        "telecommunications",
        "wireless",
        "broadband",
        "fintech",
        "edtech",
        "healthtech",
        "medtech",
        "e-commerce",
        "ecommerce",
        "platform",
        "digital",
        "data analytics",
        "blockchain",
        "cryptocurrency",
        "crypto",
    ],
    "Mining": [
        "gold",
        "silver",
        "copper",
        "zinc",
        "nickel",
        "cobalt",
        "lithium",
        "uranium",
        "iron ore",
        "rare earth",
        "mineral",
        "mine",
        "mining",
        "exploration",
        "drill",
        "resource",
        "deposit",
        "tailings",
        "smelter",
        "ore",
        "precious metal",
        "base metal",
        "potash",
        "phosphate",
    ],
}


def classify_sector(text: str) -> str:
    """Return the sector label for *text*."""
    if not text:
        return "Unknown"

    lower = text.lower()
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                log.debug("classify_sector: matched %r → %s", kw, sector)
                return sector
    return "Unknown"


# ── spaCy NER company extractor (unchanged) ──────────────────────────────────

_NER_BLOCKLIST: set[str] = {
    "TSX", "TSXV", "CSE", "NYSE", "NASDAQ", "LSE", "ASX",
    "TSX Venture Exchange", "Toronto Stock Exchange",
    "SEC", "OSC", "SEDAR", "SEDAR+", "EDGAR", "CRA", "IIROC", "CIRO",
    "Securities Commission",
    "Reuters", "Bloomberg", "Globe and Mail", "Financial Post",
    "Newsfile", "Cision", "PR Newswire", "GlobeNewswire", "Marketwired",
    "Q1", "Q2", "Q3", "Q4", "Inc", "Corp", "Ltd", "LLC",
}

_TICKER_RE = re.compile(r"\s*\([A-Z]{2,6}(?:V)?:\s*[A-Z0-9.]+\)")
_nlp = None


def _load_spacy():
    global _nlp
    if _nlp is not None:
        return _nlp
    try:
        import spacy
        try:
            _nlp = spacy.load("en_core_web_sm")
        except OSError:
            raise RuntimeError(
                "spaCy model 'en_core_web_sm' not found.\n"
                "Install it with:\n"
                "    pip install spacy\n"
                "    python -m spacy download en_core_web_sm"
            ) from None
    except ImportError:
        raise RuntimeError(
            "spaCy is not installed.\n"
            "Install it with:\n"
            "    pip install spacy\n"
            "    python -m spacy download en_core_web_sm"
        ) from None
    return _nlp


def extract_companies(text: str) -> list[str]:
    """Use spaCy NER to extract organisation names from *text*."""
    if not text or not text.strip():
        return []

    nlp = _load_spacy()
    with nlp.select_pipes(enable=["tok2vec", "ner"]):
        doc = nlp(text[:1_000])

    seen: set[str] = set()
    results: list[str] = []

    for ent in doc.ents:
        if ent.label_ != "ORG":
            continue

        name = _TICKER_RE.sub("", ent.text).strip()
        if not name or len(name) < 3:
            continue

        upper = name.upper()
        blocked = any(
            upper == bl.upper() or upper.startswith(bl.upper())
            for bl in _NER_BLOCKLIST
        )
        if blocked:
            continue

        if name not in seen:
            seen.add(name)
            results.append(name)

    return results


# ── Per-filing enrichment ─────────────────────────────────────────────────────

def enrich_filing(row: dict) -> dict:
    """Add 'sector' and 'companies' keys to a filing dict."""
    combined = " ".join(filter(None, [
        row.get("headline", ""),
        row.get("doc_type", ""),
        row.get("issuer_name", ""),
    ]))

    row["sector"] = classify_sector(combined)

    try:
        companies = extract_companies(combined)
    except RuntimeError as exc:
        log.warning("NER unavailable — skipping company extraction: %s", exc)
        companies = []

    row["companies"] = " | ".join(companies) if companies else ""
    return row


# ── Supabase enrichment ──────────────────────────────────────────────────────

def enrich_supabase(batch_size: int = 100, dry_run: bool = False) -> int:
    """
    Back-fill 'sector' and 'companies' for every existing row where sector is NULL.
    
    Args:
        batch_size: Number of rows to process per batch
        dry_run: If True, only preview changes without updating
    
    Returns:
        Number of rows updated
    """
    db = get_supabase()
    
    # Get total count
    total_pending = db.get_count_unenriched()
    if total_pending == 0:
        log.info("All filings already enriched!")
        return 0
    
    log.info("Back-fill: %d rows need enrichment.", total_pending)
    
    if dry_run:
        log.info("DRY RUN: Would process %d rows", total_pending)
        # Fetch and display a sample
        sample = db.get_unenriched(limit=5)
        log.info("Sample of rows to be enriched:")
        for row in sample:
            combined = " ".join(filter(None, [
                row.get("headline", ""),
                row.get("doc_type", ""),
                row.get("issuer_name", ""),
            ]))
            sector = classify_sector(combined)
            companies = extract_companies(combined) if combined else []
            log.info("  ID %s: %s -> %s", row["id"], row["headline"][:50], sector)
        return 0
    
    updated = 0
    offset = 0
    
    while True:
        rows = db.get_unenriched(limit=batch_size, offset=offset)
        if not rows:
            break
        
        for row in rows:
            # Enrich the row
            enriched = enrich_filing(row)
            
            # Update in Supabase
            success = db.update_filing(
                row["id"], 
                enriched["sector"], 
                enriched["companies"]
            )
            if success:
                updated += 1
                log.info("[%d/%d] Updated ID %s: %s", 
                        updated, total_pending, row["id"], enriched["sector"])
        
        offset += len(rows)
        log.info("Progress: %d / %d", updated, total_pending)
    
    log.info("Back-fill complete. %d rows updated.", updated)
    return updated


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back-fill sector & NER company data into Supabase."
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=100,
        metavar="N",
        help="Rows per batch (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without updating",
    )
    args = parser.parse_args()

    enrich_supabase(batch_size=args.batch, dry_run=args.dry_run)


if __name__ == "__main__":
    main()