# ao3-metadata-scraper

A Python tool that scrapes **metadata only** (no fic body text) from [Archive of Our Own (AO3)](https://archiveofourown.org) and saves results to a CSV file.

Inspired by and compatible with [radiolarian/AO3Scraper](https://github.com/radiolarian/AO3Scraper). This tool collapses the original three-script workflow into a single file with a more explicit, debuggable pipeline and adds a Jupyter notebook version.

---

## Features

| Feature | Detail |
|---|---|
| **Metadata only** | Never downloads fic body text — faster and lighter |
| **Three explicit steps** | Build page list → collect IDs → collect metadata. Each step saves its output to a plain text file you can inspect before the next step runs |
| **Resumable** | Every output file is written incrementally; interrupted runs pick up where they left off with `--resume` |
| **Robust error handling** | Retries timeouts, HTTP 525 (Cloudflare SSL), 429 (rate-limit), and 5xx server errors with exponential back-off (15 s → 30 s → 60 s → 120 s → 240 s) |
| **AO3 ToS compliant** | Enforces a minimum 5-second delay between all requests |
| **Notebook included** | `ao3_metadata_scraper.ipynb` is a fully documented Jupyter notebook version of the same pipeline |

---

## Output columns

| Column | Description |
|---|---|
| `work_id` | Numeric AO3 work identifier |
| `title` | Work title |
| `author` | Author name(s), comma-separated if multiple |
| `rating` | Content rating |
| `warnings` | Archive warnings |
| `category` | Relationship category (F/F, F/M, Gen, M/M, etc.) |
| `fandom` | Fandom tag(s) |
| `relationship` | Relationship/pairing tag(s) |
| `character` | Character tag(s) |
| `additional_tags` | Freeform tags |
| `language` | Language the work is written in |
| `series` | Series membership (if any) |
| `published` | Original publication date |
| `status` | `Completed` or `Updated` |
| `status_date` | Date of completion or last update |
| `words` | Total word count |
| `chapters` | Chapter count in `current/total` format |
| `comments` | Comment count |
| `kudos` | Kudos count |
| `bookmarks` | Bookmark count |
| `hits` | Hit count |
| `summary` | Full work summary |

---

## Requirements

- Python 3.10 or newer
- See `requirements.txt`

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-username/ao3-metadata-scraper.git
cd ao3-metadata-scraper

# 2. (Recommended) Create a virtual environment
python -m venv .venv

# macOS / Linux:
source .venv/bin/activate

# Windows:
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Usage

### How to find your search URL

1. Go to [archiveofourown.org](https://archiveofourown.org)
2. Browse to a fandom/tag page or run a search
3. Apply any filters — language, rating, word count, completion status, etc.
4. Copy the full URL from your browser's address bar

### How many pages are there?

Scroll to the bottom of the search results page in your browser and read the last page number from the pagination bar. AO3 shows 20 works per listing page, so 50 pages = ~1,000 works.

---

### Full pipeline

```bash
python ao3_metadata_scraper.py \
    "https://archiveofourown.org/works?work_search[sort_column]=kudos_count&tag_id=Sherlock+%28TV%29" \
    --start-page 1 --end-page 62
```

This runs all three steps and writes three output files:

```
ao3_pages.txt        ← Step 1: one listing-page URL per line
ao3_work_ids.txt     ← Step 2: one numeric work ID per line
ao3_metadata.csv     ← Step 3: one metadata row per work
```

### Custom output filenames

```bash
python ao3_metadata_scraper.py \
    "https://archiveofourown.org/works?tag_id=Sherlock+%28TV%29" \
    --start-page 1 --end-page 62 \
    --pages-out sherlock_pages.txt \
    --ids-out   sherlock_ids.txt \
    --out       sherlock_metadata.csv
```

### Scrape a specific page range

```bash
# Pages 10–20 only
python ao3_metadata_scraper.py \
    "https://archiveofourown.org/works?tag_id=Sherlock+%28TV%29" \
    --start-page 10 --end-page 20
```

### Run steps individually

```bash
# Step 1 only — build the page list, then stop and review it
python ao3_metadata_scraper.py \
    "https://archiveofourown.org/works?tag_id=Sherlock+%28TV%29" \
    --start-page 1 --end-page 62 --step1-only

# Step 2 only — collect IDs from an existing page list
python ao3_metadata_scraper.py --step2 ao3_pages.txt

# Step 3 only — collect metadata from an existing IDs file
python ao3_metadata_scraper.py --step3 ao3_work_ids.txt --out ao3_metadata.csv
```

### Resume an interrupted Step 3

```bash
python ao3_metadata_scraper.py \
    --step3 ao3_work_ids.txt \
    --out ao3_metadata.csv \
    --resume
```

`--resume` reads the existing CSV, skips IDs already present, and appends only new rows.

### All options

```
positional / source (mutually exclusive):
  URL                 AO3 search/tag/fandom URL — runs all 3 steps
  --step2 FILE        Run Step 2 only from an existing page-URLs file
  --step3 FILE        Run Step 3 only from an existing work-IDs file

output:
  --out FILE          Metadata CSV (default: ao3_metadata.csv)
  --pages-out FILE    Step 1 output  (default: ao3_pages.txt)
  --ids-out FILE      Step 2 output  (default: ao3_work_ids.txt)

step control:
  --start-page N      First listing page, inclusive (default: 1)
  --end-page N        Last listing page, inclusive (required with a URL)
  --step1-only        Build page list then stop
  --resume            Step 3: append to existing CSV, skip already-done IDs

other:
  --header AGENT      User-Agent suffix e.g. "MyProject/1.0; me@email.com"
  -h, --help          Show help
```

---

## Notebook

`ao3_metadata_scraper.ipynb` is a Jupyter notebook version of the same pipeline. Open it in VS Code, JupyterLab, or Classic Jupyter. Edit the configuration cell (Step 2) to set your URL, start/end page, and output filenames, then run all cells in order.

> **Kernel restart required after updating:** If you have previously run an older version of the notebook in the same session, do **Kernel → Restart Kernel and Run All Cells**. Stale function definitions from a previous run persist in memory until the kernel is restarted.

---

## How it works

### Three-step pipeline

```
Step 1 — Build page URL list            (no network requests)
│
│   _page_url(base_url, n) injects page=n into the query string.
│   Increments n by 1 from start_page to end_page (inclusive).
│   Writes all URLs to ao3_pages.txt.
│   No HTTP requests — runs instantly.
│
Step 2 — Collect work IDs               (1 request per listing page)
│
│   Fetches each URL from ao3_pages.txt.
│   AO3 renders each work as <li id="work_NNNNNNN" class="work blurb group">.
│   Extracts the numeric ID from that attribute.
│   Deduplicates across pages, writes to ao3_work_ids.txt.
│
Step 3 — Collect metadata               (1 request per work)
│
│   Fetches /works/{id}?view_adult=true for each ID.
│   (?view_adult=true bypasses the mature-content click-through gate.)
│   Parses <dl class="work meta group"> for tags and stats.
│   Parses title, author, and summary from the page header.
│   Streams one CSV row per work to disk immediately after each fetch.
│   Works that fail are logged to errors_ao3_metadata.csv.
```

### Why explicit page numbers instead of following next-page links?

Earlier versions tried to follow the "Next →" pagination link from each HTML response. This silently stopped after one page because AO3 emits different href formats depending on the URL type:

| URL type | Href AO3 emits |
|---|---|
| Tag/fandom pages | `?page=2` (query-string only) |
| Search pages | `/works/search?...&page=2` (absolute path) |
| Already-paginated | `https://archiveofourown.org/...?page=2` (full URL) |

A query-string-only href like `?page=2`, naively prepended to `https://archiveofourown.org`, produces `https://archiveofourown.org?page=2` — no path, so it fetches the homepage. No works found → loop stops after page 1.

Constructing the URL for each page number directly (`?page=n`) requires no HTML parsing, works for all AO3 URL types, and the full list can be reviewed before any ID collection starts.

### Error handling

`fetch()` retries with exponential back-off on transient errors:

| Error | Behaviour |
|---|---|
| Read timeout | Retry — 60 s timeout per request |
| HTTP 429 | Retry — rate-limited by AO3 |
| HTTP 525 | Retry — Cloudflare SSL handshake error (transient) |
| HTTP 500/502/503/504 | Retry — server errors |
| HTTP 403 | Log and skip — deliberate block; immediate retry won't help |
| HTTP 404 | Log and skip — work doesn't exist |

Back-off schedule: 15 s → 30 s → 60 s → 120 s → 240 s, then give up after 5 attempts.

---

## Differences from radiolarian/AO3Scraper

| Original | This tool |
|---|---|
| Three separate scripts | One script + one notebook |
| `ao3_work_ids.py` followed HTML next-page links | Constructs page URLs directly from start/end numbers |
| `ao3_get_fanfics.py` downloaded full fic body text | Never downloads body text |
| `extract_metadata.py` stripped body text as post-processing | No post-processing needed |
| No resume support for the metadata step | `--resume` skips already-scraped IDs |
| `warnings` and `series` fields not collected | Both fields included |

---

## AO3 terms of service

AO3 asks that scraping tools wait between requests to avoid overloading their servers. This tool enforces a **minimum 5-second delay** (`REQUEST_DELAY`) between every request. Please do not reduce this value.

See: [archiveofourown.org/TOS](https://archiveofourown.org/TOS)

---

## License

[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) — matching the original AO3Scraper by radiolarian.
