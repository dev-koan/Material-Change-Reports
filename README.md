# 📡 Deal Flow — Canadian M&A Filing Monitor

A self-hosted pipeline that scrapes Material Change Reports from SEDAR+, EDGAR, TMX Newsfile, and Canadian business media, classifies them by sector, extracts company names via NER, and surfaces everything through a searchable Streamlit dashboard — updated automatically every morning by GitHub Actions.

Built with the help of Claude
```
https://dealtool.streamlit.app/
```
---

## Why I built this

Deal sourcing in Canadian markets means juggling multiple regulatory and media feeds that don't talk to each other. SEDAR+ has no public API. EDGAR covers cross-listed companies but misses TSX/TSXV-only issuers. Newsfile catches the small-caps but has no search. The Financial Post and Globe and Mail cover the same deals, often hours earlier, but bury them in general business noise.

This tool pulls all four sources into a single SQLite database, automatically tags each filing as **TMT** or **Mining**, and extracts named companies from headlines using spaCy. The result is a morning briefing you can query, filter, and export — without a Bloomberg terminal.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Data Sources                        │
│                                                         │
│  ┌──────────┐  ┌─────────────┐  ┌───────────────────┐  │
│  │  EDGAR   │  │TMX Newsfile │  │  Canadian Media   │  │
│  │  EFTS    │  │  RSS feeds  │  │  (FP, Globe RSS)  │  │
│  │  (6-K)   │  │  (10 feeds) │  │  keyword-filtered │  │
│  └────┬─────┘  └──────┬──────┘  └────────┬──────────┘  │
└───────┼───────────────┼─────────────────┼──────────────┘
        │               │                 │
        └───────────────▼─────────────────┘
                   scraper.py
                  (orchestrates
                   all sources)
                        │
                        ▼
               enrichment.py
          ┌────────────────────────┐
          │  classify_sector()     │  keyword → TMT / Mining / Unknown
          │  extract_companies()   │  spaCy NER → org names
          └────────────────────────┘
                        │
                        ▼
              sedar_filings.db
               (SQLite, WAL mode,
                INSERT OR IGNORE)
                        │
             ┌──────────┴──────────┐
             │                     │
         app.py               query_db.py
      (Streamlit UI)          (CLI query /
      filter, search,          CSV export /
      detail panel)            stats)
             │
             ▼
    ┌─────────────────┐
    │  GitHub Actions │  scrape.yml
    │  Daily @ 08:00  │  → commits updated DB
    │  UTC (4am ET)   │
    └─────────────────┘
```

**Data flow in brief:**

1. `scraper.py` fetches filings from all three source types, deduplicates by `source_id`, and calls `enrichment.py` inline before each insert.
2. `enrichment.py` classifies sector via keyword matching and extracts org names via spaCy's `en_core_web_sm` model. Results are stored as columns on the same row.
3. `sedar_filings.db` is a plain SQLite file. `INSERT OR IGNORE` on `source_id` means re-runs never create duplicates. The scraper auto-deletes filings older than 7 days to keep the DB lean.
4. `app.py` reads the DB directly with `@st.cache_resource` — no separate server process needed.
5. `query_db.py` provides a CLI for one-off queries, stats, and CSV exports.
6. `scrape.yml` runs the scraper nightly via GitHub Actions and commits the updated `.db` file back to the repo.

---

## Running locally

### 1. Prerequisites

- Python 3.11+
- Git

### 2. Clone and install

```bash
git clone https://github.com/your-org/deal-flow.git
cd deal-flow

# spaCy model for NER (company name extraction)
python -m spacy download en_core_web_sm
```

### 3. Run the scraper

```bash
# Fetch the last 3 days from all sources (default)
python scraper.py

# Or specify a window
python scraper.py --days 7

# Single source only
python scraper.py --source edgar
python scraper.py --source newsfile
python scraper.py --source media

# Keep polling (useful for monitoring)
python scraper.py --poll 300   # re-scrape every 5 minutes

# Verbose output
python scraper.py --debug
```

This creates `sedar_filings.db` in the project directory. Re-running is safe — duplicates are silently skipped.

### 4. (Optional) Back-fill enrichment on an existing DB

If you have a DB from before the sector/NER columns were added:

```bash
python enrichment.py
# or
python enrichment.py --db path/to/custom.db
```

### 5. Launch the dashboard

```bash
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501). Use the sidebar to filter by sector, source, date range, or keyword.

### 6. Query from the CLI

```bash
# Last 20 filings
python query_db.py

# Filter by issuer (partial match)
python query_db.py --issuer "Barrick"

# Filter by sector
python query_db.py --sector Mining
python query_db.py --sector TMT

# Filings since a date
python query_db.py --since 2024-06-01

# Summary statistics
python query_db.py --stats

# Export to CSV
python query_db.py --export filings.csv --limit 500
```

---

## Automated daily scrape (GitHub Actions)

`scrape.yml` runs at 08:00 UTC (04:00 ET) every day:

1. Checks out the repo
2. Installs dependencies
3. Runs `python scraper.py --days 2`
4. Vacuums the SQLite DB
5. Commits and pushes the updated `sedar_filings.db`

To trigger manually: go to **Actions → Daily scrape → Run workflow** in GitHub.

> **Note:** Remember to update the `User-Agent` string in `scraper.py` with your actual contact email — the SEC requires this for EDGAR access.

---

## Sector labels

| Label     | Signal keywords (sample)                                          |
|-----------|-------------------------------------------------------------------|
| `TMT`     | software, SaaS, cybersecurity, AI, semiconductor, cloud, fintech |
| `Mining`  | gold, silver, copper, lithium, uranium, mineral, exploration      |
| `Unknown` | No strong signal found in headline + doc_type + issuer name      |

Classification uses the first-match rule: TMT is checked before Mining. Adjust `SECTOR_KEYWORDS` in `enrichment.py` to add sectors or tune precision.

---

## Limitations

- **Newsfile feeds return the latest 10 items per feed.** If you skip more than a day or two, items can fall off the feed before being captured. The GitHub Actions cron (`--days 2`) is sized to prevent gaps.
- **spaCy NER on short headlines is noisy.** The blocklist in `enrichment.py` filters common false positives (exchanges, regulators, wire services), but edge cases slip through. Treat `companies` as a starting point for research, not a clean entity list.
- **Canadian media articles are keyword-filtered** before storage. Articles that don't contain any `DEAL_KEYWORDS` are dropped, so editorial commentary without deal terminology won't appear.
- **The DB is committed to git.** This is convenient for a single-user research tool but doesn't scale. For team use, point `DB_PATH` at a shared volume or swap SQLite for a hosted Postgres instance.
