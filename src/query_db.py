"""
query_db.py  –  Query Supabase filings database
================================================

Usage:
    python query_db.py                       # show last 20 filings
    python query_db.py --limit 50            # show last 50 filings
    python query_db.py --issuer "Barrick"    # filter by issuer name
    python query_db.py --since 2024-01-01    # filings on or after a date
    python query_db.py --sector Mining       # filter by sector label
    python query_db.py --sector TMT          # TMT deals only
    python query_db.py --stats               # show summary statistics
    python query_db.py --export filings.csv

Sector labels:
    TMT, Mining, Unknown
"""

import argparse
import csv
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Load environment variables ────────────────────────────────────────────────

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


class SupabaseClient:
    def __init__(self):
        self.url = SUPABASE_URL
        self.key = SUPABASE_KEY
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def query(self, params: dict) -> list[dict]:
        """Execute a query against the filings table."""
        if not self.url or not self.key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env or environment")
        
        resp = requests.get(
            f"{self.url}/rest/v1/filings",
            params=params,
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_stats(self) -> dict:
        """Get summary statistics."""
        # Total count
        total = self.query({"select": "id", "limit": 0})
        # We need to get the count from headers
        resp = requests.get(
            f"{self.url}/rest/v1/filings",
            params={"select": "id", "limit": 0},
            headers=self.headers,
        )
        total_count = int(resp.headers.get("Content-Range", "0/0").split("/")[-1] or 0)
        
        # Min/max dates
        date_stats = self.query({
            "select": "filing_date",
            "order": "filing_date.asc",
            "limit": 1,
        })
        oldest = date_stats[0]["filing_date"] if date_stats else None
        
        newest_stats = self.query({
            "select": "filing_date",
            "order": "filing_date.desc",
            "limit": 1,
        })
        newest = newest_stats[0]["filing_date"] if newest_stats else None
        
        # Top issuers
        # Since we can't do GROUP BY directly, we'll get all and count locally
        all_rows = self.query({
            "select": "issuer_name",
            "limit": 1000,  # Reasonable limit
        })
        issuer_counts = {}
        for row in all_rows:
            issuer = row.get("issuer_name", "Unknown")
            issuer_counts[issuer] = issuer_counts.get(issuer, 0) + 1
        top_issuers = sorted(issuer_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        
        # Sector counts
        sector_counts = {}
        sector_rows = self.query({
            "select": "sector",
            "limit": 1000,
        })
        for row in sector_rows:
            sector = row.get("sector", "Unknown")
            if sector is None:
                sector = "Unknown"
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        
        return {
            "total": total_count,
            "oldest": oldest,
            "newest": newest,
            "top_issuers": top_issuers,
            "sector_counts": sector_counts,
        }


def get_client() -> SupabaseClient:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[error] SUPABASE_URL and SUPABASE_KEY must be set")
        print("        Create a .env file in the project root with:")
        print("        SUPABASE_URL=https://your-project-id.supabase.co")
        print("        SUPABASE_KEY=your-service-role-key")
        exit(1)
    return SupabaseClient()


def show_stats(client: SupabaseClient) -> None:
    stats = client.get_stats()
    
    print(f"\n── Database Statistics ─────────────────────────────────────")
    print(f"  Total filings : {stats['total']:,}")
    print(f"  Oldest filing : {stats['oldest']}")
    print(f"  Newest filing : {stats['newest']}")

    print(f"\n── Top 10 Issuers ──────────────────────────────────────────")
    for issuer, count in stats['top_issuers']:
        issuer_display = issuer if issuer else "(unknown)"
        print(f"  {count:>4}  {issuer_display}")

    print(f"\n── Filings by Sector ───────────────────────────────────────")
    for sector, count in sorted(stats['sector_counts'].items(), key=lambda x: x[1], reverse=True):
        print(f"  {count:>4}  {sector}")
    print()


def list_filings(
    client: SupabaseClient,
    issuer: str | None,
    since: str | None,
    sector: str | None,
    limit: int,
) -> list[dict]:
    """Query and return filings matching the filters."""
    params = {
        "select": "*",
        "order": "filing_date.desc",
        "limit": str(limit),
    }
    
    if issuer:
        # Use text search for issuer_name
        params["issuer_name"] = f"ilike.%{issuer}%"
    
    if since:
        params["filing_date"] = f"gte.{since}"
    
    if sector:
        params["sector"] = f"eq.{sector}"
    
    return client.query(params)


def print_results(rows: list[dict]) -> None:
    """Print filings in a formatted table."""
    if not rows:
        print("No filings matched your query.")
        return

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
        f"{'Jur.':<{col_widths['jurisdiction']}}  "
        f"{'Sector':<{col_widths['sector']}}"
    )

    print(f"\n{header}")
    print("─" * len(header))

    for r in rows:
        issuer   = (r.get("issuer_name") or "")[:col_widths["issuer_name"]]
        headline = (r.get("headline")    or "")[:col_widths["headline"]]
        jur      = (r.get("jurisdiction")or "")[:col_widths["jurisdiction"]]
        sector   = (r.get("sector") or "Unknown")[:col_widths["sector"]]

        line = (
            f"{r['filing_date']:<{col_widths['filing_date']}}  "
            f"{issuer:<{col_widths['issuer_name']}}  "
            f"{headline:<{col_widths['headline']}}  "
            f"{jur:<{col_widths['jurisdiction']}}  "
            f"{sector:<{col_widths['sector']}}"
        )
        print(line)

        if r.get("url"):
            print(f"   └─ {r['url']}")
        if r.get("companies") and r["companies"].strip():
            companies_list = r["companies"].split(" | ")
            for company in companies_list:
                if company and company.strip():
                    print(f"   ├─ 🏢 {company.strip()}")

    print(f"\n{len(rows)} filing(s) shown.\n")


def export_csv(rows: list[dict], path: str) -> None:
    if not rows:
        print("Nothing to export.")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(rows)} rows to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the Supabase filings database.")
    parser.add_argument("--limit",  type=int,  default=20, help="Max rows to display (default: 20)")
    parser.add_argument("--issuer", type=str,  default=None, help="Filter by issuer name (partial match)")
    parser.add_argument("--since",  type=str,  default=None, help="Only show filings on/after YYYY-MM-DD")
    parser.add_argument("--sector", type=str,  default=None,
                        help="Filter by sector label: TMT, Mining, Unknown")
    parser.add_argument("--stats",  action="store_true", help="Show summary statistics instead of listing")
    parser.add_argument("--export", type=str,  default=None, metavar="FILE.csv", help="Export results to CSV")
    args = parser.parse_args()

    client = get_client()

    if args.stats:
        show_stats(client)
        return

    rows = list_filings(client, args.issuer, args.since, args.sector, args.limit)

    if args.export:
        export_csv(rows, args.export)
    else:
        print_results(rows)


if __name__ == "__main__":
    main()