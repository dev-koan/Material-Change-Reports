"""
enrichment.py  –  NER company extraction + sector classification
================================================================

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

  4. enrich_db(db_path)
     Back-fills sector + companies for every existing row that is still NULL.
     Safe to run multiple times (skips already-enriched rows).

Usage:
    python enrichment.py                  # back-fill existing DB
    python enrichment.py --db custom.db   # specify a different DB path
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Sector classifier ─────────────────────────────────────────────────────────

# Each sector maps to a list of lower-case keyword fragments.
# The FIRST matching sector wins (order matters for ambiguous cases).
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
    """
    Return the sector label for *text* (headline + description combined).

    Strategy
    --------
    - Lowercase the text once.
    - Walk sectors in definition order; first sector whose keyword list
      contains any substring match wins.
    - Return 'Unknown' if nothing matches.

    Parameters
    ----------
    text : str
        The combined text to classify (headline, description, or both).

    Returns
    -------
    str
        One of: 'TMT', 'Mining', or 'Unknown'.
    """
    if not text:
        return "Unknown"

    lower = text.lower()

    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                log.debug("classify_sector: matched %r → %s", kw, sector)
                return sector

    return "Unknown"


# ── spaCy NER company extractor ───────────────────────────────────────────────

# Tokens we want to strip out even if spaCy tags them as ORG.
# Includes exchanges, regulators, common noun phrases, and media outlets.
_NER_BLOCKLIST: set[str] = {
    # Exchanges & venues
    "TSX", "TSXV", "CSE", "NYSE", "NASDAQ", "LSE", "ASX",
    "TSX Venture Exchange", "Toronto Stock Exchange",
    # Regulators / government
    "SEC", "OSC", "SEDAR", "SEDAR+", "EDGAR", "CRA", "IIROC", "CIRO",
    "Securities Commission",
    # Media / wires
    "Reuters", "Bloomberg", "Globe and Mail", "Financial Post",
    "Newsfile", "Cision", "PR Newswire", "GlobeNewswire", "Marketwired",
    # Generic noise
    "Q1", "Q2", "Q3", "Q4", "Inc", "Corp", "Ltd", "LLC",
}

# Compiled pattern to strip trailing legal suffixes from extracted names
# so "Barrick Gold Corporation" → "Barrick Gold Corporation" stays intact
# but ticker tags like "(TSX: ABX)" are removed.
_TICKER_RE = re.compile(r"\s*\([A-Z]{2,6}(?:V)?:\s*[A-Z0-9.]+\)")

_nlp = None  # lazy-loaded to avoid slow import at module level


def _load_spacy():
    """Lazy-load spaCy model; raise a clear error if not installed."""
    global _nlp
    if _nlp is not None:
        return _nlp
    try:
        import spacy  # noqa: PLC0415
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
    """
    Use spaCy NER to extract organisation names from *text*.

    Steps
    -----
    1. Run the en_core_web_sm pipeline (tok2vec + NER only — no parser).
    2. Collect all spans labelled 'ORG'.
    3. Strip ticker tags and leading/trailing whitespace.
    4. Remove entries in the blocklist (case-insensitive prefix match).
    5. De-duplicate while preserving order.

    Parameters
    ----------
    text : str
        Raw headline or concatenated headline+description.

    Returns
    -------
    list[str]
        Deduplicated list of organisation names found; empty list if none.
    """
    if not text or not text.strip():
        return []

    nlp = _load_spacy()

    # Disable everything except NER to keep it fast
    with nlp.select_pipes(enable=["tok2vec", "ner"]):
        doc = nlp(text[:1_000])  # cap at 1 000 chars to stay snappy

    seen: set[str] = set()
    results: list[str] = []

    for ent in doc.ents:
        if ent.label_ != "ORG":
            continue

        name = _TICKER_RE.sub("", ent.text).strip()

        if not name or len(name) < 3:
            continue

        # Blocklist check — exact or prefix match
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
    """
    Add 'sector' and 'companies' keys to a filing dict.

    Combines headline and doc_type for better signal.  The 'companies'
    value is stored as a pipe-delimited string so it fits cleanly in SQLite
    without needing a second table.

    Parameters
    ----------
    row : dict
        A filing dict (same structure as passed to upsert_filing).

    Returns
    -------
    dict
        The same dict, mutated in place, with 'sector' and 'companies' set.
    """
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


# ── DB schema migration + back-fill ──────────────────────────────────────────

DEFAULT_DB = Path(__file__).parent / "sedar_filings.db"


def migrate_schema(conn: sqlite3.Connection) -> None:
    """
    Add 'sector' and 'companies' columns to the filings table if they don't
    already exist.  Safe to call on both old and new databases.
    """
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(filings)").fetchall()
    }
    for col, col_def in [("sector", "TEXT"), ("companies", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE filings ADD COLUMN {col} {col_def}")
            log.info("Schema: added column '%s'", col)
    # Index for fast sector filtering
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sector ON filings (sector);"
    )
    conn.commit()


def enrich_db(db_path: Path = DEFAULT_DB, batch_size: int = 200) -> int:
    """
    Back-fill 'sector' and 'companies' for every existing row where both
    columns are still NULL.

    Processes rows in batches of *batch_size* to keep memory usage low.
    Commits after each batch.  Safe to interrupt and restart — already-
    enriched rows (sector IS NOT NULL) are skipped.

    Returns the total number of rows updated.
    """
    if not db_path.exists():
        log.error("Database not found at %s — run scraper.py first.", db_path)
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    migrate_schema(conn)

    total_pending = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE sector IS NULL"
    ).fetchone()[0]
    log.info("Back-fill: %d rows need enrichment.", total_pending)

    updated = 0
    offset  = 0

    while True:
        rows = conn.execute(
            "SELECT id, headline, doc_type, issuer_name "
            "FROM filings WHERE sector IS NULL "
            "LIMIT ? OFFSET ?",
            (batch_size, offset),
        ).fetchall()

        if not rows:
            break

        for row in rows:
            d = dict(row)
            enrich_filing(d)
            conn.execute(
                "UPDATE filings SET sector=?, companies=? WHERE id=?",
                (d["sector"], d["companies"], d["id"]),
            )
            updated += 1

        conn.commit()
        offset  += len(rows)
        log.info("Back-fill progress: %d / %d", updated, total_pending)

    log.info("Back-fill complete. %d rows updated.", updated)
    conn.close()
    return updated


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Back-fill sector & NER company data into the filings DB."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        metavar="PATH",
        help=f"Path to SQLite database (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=200,
        metavar="N",
        help="Rows per commit batch (default: 200)",
    )
    args = parser.parse_args()

    enrich_db(args.db, args.batch)


if __name__ == "__main__":
    main()