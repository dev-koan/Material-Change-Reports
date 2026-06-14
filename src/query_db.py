"""
query_db.py  –  Inspect & query the SEDAR+ filings database
============================================================

Usage:
    python query_db.py                       # show last 20 filings
    python query_db.py --limit 50            # show last 50 filings
    python query_db.py --issuer "Barrick"    # filter by issuer name
    python query_db.py --since 2024-01-01    # filings on or after a date
    python query_db.py --sector Mining       # filter by sector label
    python query_db.py --sector TMT          # TMT deals only
    python query_db.py --stats               # show summary statistics
    python query_db.py --export filings.csv

Sector labels (set automatically by enrichment.py):
    TMT, Mining, Unknown
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "sedar_filings.db"


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"[error] Database not found at {db_path}")
        print("        Run scraper.py first to create it.")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def show_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    oldest = conn.execute("SELECT MIN(filing_date) FROM filings").fetchone()[0]
    newest = conn.execute("SELECT MAX(filing_date) FROM filings").fetchone()[0]
    print(f"\n── Database Statistics ─────────────────────────────────────")
    print(f"  Total filings : {total:,}")
    print(f"  Oldest filing : {oldest}")
    print(f"  Newest filing : {newest}")

    print(f"\n── Top 10 Issuers ──────────────────────────────────────────")
    rows = conn.execute("""
        SELECT issuer_name, COUNT(*) AS cnt
        FROM filings
        GROUP BY issuer_name
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"  {r['cnt']:>4}  {r['issuer_name']}")

    print(f"\n── Filings by Jurisdiction ─────────────────────────────────")
    rows = conn.execute("""
        SELECT jurisdiction, COUNT(*) AS cnt
        FROM filings
        GROUP BY jurisdiction
        ORDER BY cnt DESC
    """).fetchall()
    for r in rows:
        j = r["jurisdiction"] or "(unknown)"
        print(f"  {r['cnt']:>4}  {j}")

    print(f"\n── Filings by Sector ───────────────────────────────────────")
    rows = conn.execute("""
        SELECT COALESCE(sector, 'Unknown') AS sector, COUNT(*) AS cnt
        FROM filings
        GROUP BY sector
        ORDER BY cnt DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['cnt']:>4}  {r['sector']}")
    print()


def list_filings(
    conn: sqlite3.Connection,
    issuer: str | None,
    since: str | None,
    sector: str | None,
    limit: int,
) -> tuple[list[sqlite3.Row], bool]:
    """
    Returns (rows, has_sector_column) where has_sector_column indicates
    if the sector column exists in the database.
    """
    # First, check if sector column exists
    has_sector = False
    try:
        # Try to select sector column - will error if it doesn't exist
        conn.execute("SELECT sector FROM filings LIMIT 1")
        has_sector = True
    except sqlite3.OperationalError:
        # Column doesn't exist yet - run enrichment first
        print("\n[WARNING] 'sector' column not found in database.")
        print("        Run 'python enrichment.py' first to add sector classification.\n")
    
    # Build query - if sector column doesn't exist and user filters by it, adjust
    query = "SELECT * FROM filings WHERE 1=1"
    params: list = []

    if issuer:
        query += " AND issuer_name LIKE ?"
        params.append(f"%{issuer}%")

    if since:
        query += " AND filing_date >= ?"
        params.append(since)

    if sector:
        if has_sector:
            query += " AND sector = ?"
            params.append(sector)
        else:
            print(f"[WARNING] Cannot filter by sector '{sector}' - column doesn't exist yet.")

    query += " ORDER BY filing_date DESC LIMIT ?"
    params.append(limit)

    return (conn.execute(query, params).fetchall(), has_sector)


def print_table(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("No filings matched your query.")
        return

    # Detect whether the enriched columns exist in this result set
    keys = rows[0].keys()
    has_sector    = "sector" in keys
    has_companies = "companies" in keys
    
    # If sector column exists but all values are NULL, show warning
    if has_sector:
        sample = conn.execute("SELECT COUNT(*) FROM filings WHERE sector IS NOT NULL").fetchone()[0]
        if sample == 0:
            print("\n[INFO] Sector column exists but no data yet. Run 'python enrichment.py' to classify.\n")

    col_widths = {
        "filing_date":  12,
        "issuer_name":  36,
        "headline":     50,
        "jurisdiction":  6,
        "sector":       12,
    }

    header = (
        f"{'Date':<{col_widths['filing_date']}}  "
        f"{'Issuer':<{col_widths['issuer_name']}}  "
        f"{'Headline':<{col_widths['headline']}}  "
        f"{'Jur.':<{col_widths['jurisdiction']}}"
    )
    if has_sector:
        header += f"  {'Sector':<{col_widths['sector']}}"

    print(f"\n{header}")
    print("─" * len(header))

    for r in rows:
        issuer   = (r["issuer_name"] or "")[:col_widths["issuer_name"]]
        headline = (r["headline"]    or "")[:col_widths["headline"]]
        jur      = (r["jurisdiction"]or "")[:col_widths["jurisdiction"]]

        line = (
            f"{r['filing_date']!s:<{col_widths['filing_date']}}  "
            f"{issuer:<{col_widths['issuer_name']}}  "
            f"{headline:<{col_widths['headline']}}  "
            f"{jur:<{col_widths['jurisdiction']}}"
        )
        if has_sector:
            sector = (r["sector"] or "Unknown")[:col_widths["sector"]]
            line += f"  {sector:<{col_widths['sector']}}"
        print(line)

        if r["url"]:
            print(f"   └─ {r['url']}")
        if has_companies and r["companies"] and r["companies"].strip():
            # Show each pipe-delimited company on its own indented line
            companies_list = r["companies"].split(" | ")
            for company in companies_list:
                if company and company.strip():
                    print(f"   ├─ 🏢 {company.strip()}")

    print(f"\n{len(rows)} filing(s) shown.\n")


def export_csv(rows: list[sqlite3.Row], path: str) -> None:
    if not rows:
        print("Nothing to export.")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])
    print(f"Exported {len(rows)} rows to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the SEDAR+ filings database.")
    parser.add_argument("--limit",  type=int,  default=20, help="Max rows to display (default: 20)")
    parser.add_argument("--issuer", type=str,  default=None, help="Filter by issuer name (partial match)")
    parser.add_argument("--since",  type=str,  default=None, help="Only show filings on/after YYYY-MM-DD")
    parser.add_argument("--sector", type=str,  default=None,
                        help="Filter by sector label: TMT, Mining, Unknown")
    parser.add_argument("--stats",  action="store_true", help="Show summary statistics instead of listing")
    parser.add_argument("--export", type=str,  default=None, metavar="FILE.csv", help="Export results to CSV")
    args = parser.parse_args()

    conn = get_conn()

    if args.stats:
        show_stats(conn)
        return

    rows, has_sector = list_filings(conn, args.issuer, args.since, args.sector, args.limit)

    if args.export:
        export_csv(rows, args.export)
    else:
        # Pass has_sector to print_table or handle in print_table
        print_table(conn, rows)


if __name__ == "__main__":
    main()