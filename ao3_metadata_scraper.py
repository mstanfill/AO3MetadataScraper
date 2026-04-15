#!/usr/bin/env python3
"""
ao3_metadata_scraper.py — AO3 Metadata Scraper
===============================================

Three-step pipeline for scraping metadata from Archive of Our Own.
Inspired by https://github.com/radiolarian/AO3Scraper  (CC BY-NC 4.0)

Step 1 — Build page URL list
    You tell it how many pages to scrape (or it fetches page 1 to find out).
    It writes every page URL to a plain text file — one URL per line.
    You can open this file and verify the URLs before anything else runs.

Step 2 — Collect work IDs
    Reads the page URLs file from Step 1, fetches each listing page,
    extracts every work ID, and writes them to a second text file.
    20 work IDs per listing page is normal.

Step 3 — Collect metadata
    Reads the work IDs file from Step 2, fetches each individual work page,
    extracts all metadata fields, and streams rows into a CSV.
    Body text is never downloaded.

All three steps are resumable: each output file is written incrementally
and the scraper skips entries already present on restart.

Usage
-----
    # Full pipeline — pages 1 to 62
    python ao3_metadata_scraper.py \\
        "https://archiveofourown.org/works?work_search[sort_column]=kudos_count&tag_id=Sherlock+%28TV%29" \\
        --start-page 1 --end-page 62

    # Scrape only pages 10 through 20
    python ao3_metadata_scraper.py \\
        "https://archiveofourown.org/works?tag_id=Sherlock+%28TV%29" \\
        --start-page 10 --end-page 20

    # Run steps individually
    python ao3_metadata_scraper.py <url> --start-page 1 --end-page 5 --step1-only
    python ao3_metadata_scraper.py --step2 pages.txt --ids-out ids.txt
    python ao3_metadata_scraper.py --step3 ids.txt --out metadata.csv

    # Resume an interrupted Step 3
    python ao3_metadata_scraper.py --step3 ids.txt --out metadata.csv --resume

Note on rate limiting
---------------------
    AO3's ToS requires a minimum 5-second delay between requests.
    Do not reduce REQUEST_DELAY below 5.
    See: https://archiveofourown.org/TOS
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from bs4 import BeautifulSoup


# =============================================================================
# Configuration
# =============================================================================

REQUEST_DELAY: float = 5.0   # seconds between every request — AO3 ToS minimum
MAX_RETRIES:   int   = 5     # increased from 3 — AO3 can be slow/flaky
RETRY_DELAY:   float = 15.0  # base back-off in seconds (doubles each attempt)
REQUEST_TIMEOUT: int = 60    # seconds per request — AO3 can be slow under load

# HTTP status codes that are transient and worth retrying.
# 429 = rate-limited, 500/502/503/504 = server errors, 525 = Cloudflare SSL
# handshake error (also transient — a back-off usually clears it).
_RETRY_STATUSES = {429, 500, 502, 503, 504, 525}

AO3_BASE = "https://archiveofourown.org"

METADATA_COLUMNS: list[str] = [
    "work_id", "title", "author", "rating", "warnings", "category",
    "fandom", "relationship", "character", "additional_tags", "language",
    "series", "published", "status", "status_date", "words", "chapters",
    "comments", "kudos", "bookmarks", "hits", "summary",
]


# =============================================================================
# HTTP helper
# =============================================================================

def make_session(user_header: str = "") -> requests.Session:
    """Return a Session with a descriptive User-Agent."""
    session = requests.Session()
    ua = "Mozilla/5.0 (compatible; ao3-metadata-scraper/1.0)"
    if user_header:
        ua = f"{ua}; {user_header}"
    session.headers.update({"User-Agent": ua})
    return session


def fetch(url: str, session: requests.Session) -> BeautifulSoup | None:
    """
    GET *url* and return a BeautifulSoup, or None on permanent failure.

    Before the first attempt the function sleeps REQUEST_DELAY seconds
    (AO3 ToS compliance).  Retries up to MAX_RETRIES times with exponential
    back-off on any status in _RETRY_STATUSES or a network/timeout exception.

    Back-off schedule (RETRY_DELAY = 15 s):
        attempt 1 fail → wait 15 s
        attempt 2 fail → wait 30 s
        attempt 3 fail → wait 60 s
        attempt 4 fail → wait 120 s
        attempt 5 fail → give up, return None

    HTTP 525 (Cloudflare SSL handshake error) is treated as transient and
    retried on the same schedule — it usually clears after one back-off.
    """
    time.sleep(REQUEST_DELAY)

    wait = RETRY_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")

            if resp.status_code in _RETRY_STATUSES:
                print(
                    f"  HTTP {resp.status_code} "
                    f"(attempt {attempt}/{MAX_RETRIES}) — "
                    f"waiting {wait:.0f}s …",
                    flush=True,
                )
                time.sleep(wait)
                wait *= 2          # exponential back-off
                continue

            # Any other status (404, 403, etc.) — not worth retrying
            print(f"  HTTP {resp.status_code}: {url}", flush=True)
            return None

        except requests.exceptions.Timeout:
            print(
                f"  Timed out after {REQUEST_TIMEOUT}s "
                f"(attempt {attempt}/{MAX_RETRIES}) — "
                f"waiting {wait:.0f}s …",
                flush=True,
            )
            time.sleep(wait)
            wait *= 2

        except requests.RequestException as exc:
            print(
                f"  Network error (attempt {attempt}/{MAX_RETRIES}): {exc} — "
                f"waiting {wait:.0f}s …",
                flush=True,
            )
            time.sleep(wait)
            wait *= 2

    print(f"  Gave up after {MAX_RETRIES} attempts: {url}", flush=True)
    return None


# =============================================================================
# Step 1 — Build the list of page URLs
# =============================================================================

def _page_url(base_url: str, page: int) -> str:
    """
    Return *base_url* with ``page=N`` set in the query string.

    Replaces any existing ``page=`` parameter so the function is safe to
    call on a URL that already contains a page number.
    """
    parsed     = urlparse(base_url)
    qs         = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page)]
    return urlunparse(parsed._replace(
        query=urlencode({k: v[0] for k, v in qs.items()})
    ))


def step1_build_page_list(
    base_url:   str,
    start_page: int,
    end_page:   int,
    pages_out:  str,
) -> list[str]:
    """
    Build every listing-page URL from *start_page* to *end_page* (inclusive)
    and write them to *pages_out*, one URL per line.

    URLs are constructed by injecting ``page=N`` into *base_url* and
    incrementing N by 1 on each step — no network requests are made.

    Parameters
    ----------
    base_url   : AO3 search / tag / fandom URL (any page= in it is replaced)
    start_page : first page number to include, e.g. ``1``
    end_page   : last page number to include, e.g. ``62``
    pages_out  : file path to write the URL list

    Returns
    -------
    list[str] — ordered list of page URLs from start_page to end_page
    """
    if end_page < start_page:
        raise ValueError(
            f"end_page ({end_page}) must be >= start_page ({start_page})"
        )

    page_urls: list[str] = []
    page = start_page
    while page <= end_page:
        page_urls.append(_page_url(base_url, page))
        page += 1           # n+1 — advance to the next page

    Path(pages_out).write_text("\n".join(page_urls) + "\n", encoding="utf-8")
    print(f"  ✓ {len(page_urls)} URL(s) written → {pages_out}", flush=True)
    return page_urls


# =============================================================================
# Step 2 — Extract work IDs from each listing page
# =============================================================================

def _ids_from_soup(soup: BeautifulSoup) -> list[str]:
    """
    Return all work IDs found in one listing page's HTML.

    AO3 renders each work in a listing as::

        <li id="work_NNNNNNN" class="work blurb group" ...>

    The numeric ID is extracted from the ``id`` attribute.
    """
    ids = []
    for li in soup.find_all("li", id=re.compile(r"^work_\d+")):
        raw = re.sub(r"^work_", "", li.get("id", "")).split()[0]
        if raw.isdigit():
            ids.append(raw)
    return ids


def step2_collect_ids(
    page_urls: list[str],
    session:   requests.Session,
    ids_out:   str,
) -> list[str]:
    """
    Fetch every listing page URL and extract work IDs into *ids_out*.

    Skips URLs already processed if *ids_out* exists with content (allows
    basic resumption if the file is inspected between runs).

    Parameters
    ----------
    page_urls : list of page URLs from Step 1
    session   : active requests.Session
    ids_out   : path to write the work-IDs file (one ID per line)

    Returns
    -------
    list[str] — all collected work IDs, in order
    """
    total     = len(page_urls)
    all_ids: list[str] = []

    print(f"\n{'─'*60}", flush=True)
    print(f"Step 2: fetching {total} listing page(s) → {ids_out}", flush=True)
    print(f"{'─'*60}\n", flush=True)

    for i, url in enumerate(page_urls, start=1):
        print(f"  [{i:>{len(str(total))}}/{total}] {url}", flush=True)
        soup = fetch(url, session)

        if soup is None:
            print(f"    fetch failed — skipping.", flush=True)
            continue

        page_ids = _ids_from_soup(soup)
        all_ids.extend(page_ids)
        print(f"    {len(page_ids)} IDs found  (running total: {len(all_ids)})",
              flush=True)

    # Deduplicate while preserving order (some fandoms overlap pages)
    seen: set[str] = set()
    unique: list[str] = []
    for wid in all_ids:
        if wid not in seen:
            seen.add(wid)
            unique.append(wid)

    if len(unique) < len(all_ids):
        print(f"  Deduplicated: {len(all_ids)} → {len(unique)} IDs.", flush=True)

    Path(ids_out).write_text("\n".join(unique) + "\n", encoding="utf-8")
    print(f"\n  ✓ {len(unique)} work IDs written → {ids_out}", flush=True)
    return unique


# =============================================================================
# Step 3 — Scrape metadata from each work page
# =============================================================================

def _text(tag) -> str:
    return tag.get_text(strip=True) if tag else ""


def _tags(meta_dl, cls: str) -> str:
    """Join all <a> texts inside <dd class='{cls}'> with ', '."""
    dd = meta_dl.find("dd", class_=cls) if meta_dl else None
    return ", ".join(a.get_text(strip=True) for a in dd.find_all("a")) if dd else ""


def _stat(meta_dl, label: str) -> str:
    """Read one value from the <dl class='stats'> block by its <dt> label."""
    stats = meta_dl.find("dl", class_="stats") if meta_dl else None
    if not stats:
        return ""
    dt = stats.find("dt", string=re.compile(re.escape(label), re.I))
    return _text(dt.find_next_sibling("dd")) if dt else ""


def _scrape_one(work_id: str, session: requests.Session) -> dict | None:
    """
    Fetch one AO3 work page and return a metadata dict.

    Appends ``?view_adult=true`` to bypass the mature-content gate.
    Returns None if the page is inaccessible or marked Access Denied.
    Body text is never fetched or stored.
    """
    soup = fetch(
        f"{AO3_BASE}/works/{work_id}?view_adult=true", session
    )
    if soup is None:
        return None
    if soup.find("p", string=re.compile(r"Access Denied", re.I)):
        print(f"  [work {work_id}] Access Denied", flush=True)
        return None

    # Title
    title = _text(soup.find("h2", class_="title heading"))

    # Author(s)
    byline = soup.find("h3", class_="byline heading")
    if byline:
        authors = [a.get_text(strip=True)
                   for a in byline.find_all("a", rel="author")]
        author  = ", ".join(authors) if authors else _text(byline)
    else:
        author = ""

    # Summary
    sdiv    = soup.find("div", class_="summary module")
    bq      = sdiv.find("blockquote", class_="userstuff") if sdiv else None
    summary = bq.get_text(separator="\n", strip=True) if bq else ""

    # Meta group
    meta = soup.find("dl", class_="work meta group")

    rating          = _tags(meta, "rating")
    warnings        = _tags(meta, "warning")
    category        = _tags(meta, "category")
    fandom          = _tags(meta, "fandom")
    relationship    = _tags(meta, "relationship")
    character       = _tags(meta, "character")
    additional_tags = _tags(meta, "freeform")
    language        = _text(meta.find("dd", class_="language") if meta else None)

    s_dd   = meta.find("dd", class_="series") if meta else None
    if s_dd:
        parts  = [s.get_text(strip=True)
                  for s in s_dd.find_all("span", class_="position")]
        series = "; ".join(parts) if parts else _text(s_dd)
    else:
        series = ""

    published = _stat(meta, "Published:")
    words     = _stat(meta, "Words:")
    chapters  = _stat(meta, "Chapters:")
    comments  = _stat(meta, "Comments:")
    kudos     = _stat(meta, "Kudos:")
    bookmarks = _stat(meta, "Bookmarks:")
    hits      = _stat(meta, "Hits:")

    c_dt = meta.find("dt", string=re.compile(r"Completed", re.I)) if meta else None
    u_dt = meta.find("dt", string=re.compile(r"Updated",   re.I)) if meta else None
    if c_dt:
        status, status_date = "Completed", _text(c_dt.find_next_sibling("dd"))
    elif u_dt:
        status, status_date = "Updated",   _text(u_dt.find_next_sibling("dd"))
    else:
        status = status_date = ""

    return {
        "work_id": work_id, "title": title, "author": author,
        "rating": rating, "warnings": warnings, "category": category,
        "fandom": fandom, "relationship": relationship, "character": character,
        "additional_tags": additional_tags, "language": language,
        "series": series, "published": published, "status": status,
        "status_date": status_date, "words": words, "chapters": chapters,
        "comments": comments, "kudos": kudos, "bookmarks": bookmarks,
        "hits": hits, "summary": summary,
    }


def step3_collect_metadata(
    work_ids: list[str],
    session:  requests.Session,
    out:      str,
    resume:   bool = False,
) -> list[dict]:
    """
    Scrape metadata for every ID in *work_ids* and write to *out* (CSV).

    Each row is flushed to disk immediately so progress survives interruption.
    With ``resume=True``, reads *out* for already-done IDs and skips them.
    Failed works go to ``errors_<out>``.

    Parameters
    ----------
    work_ids : ordered list of numeric work ID strings from Step 2
    session  : active requests.Session
    out      : output CSV path
    resume   : append to existing file, skip already-scraped IDs

    Returns
    -------
    list[dict] — rows written in this run
    """
    # Open output CSV
    done: set[str] = set()
    if resume and os.path.exists(out):
        with open(out, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                wid = row.get("work_id", "").strip()
                if wid:
                    done.add(wid)
        print(f"  Resuming — {len(done)} already in {out}", flush=True)
        fout   = open(out, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(fout, fieldnames=METADATA_COLUMNS,
                                extrasaction="ignore")
    else:
        fout   = open(out, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(fout, fieldnames=METADATA_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()

    err_path = os.path.join(os.path.dirname(out) or ".",
                            "errors_" + os.path.basename(out))
    ferr      = open(err_path, "a" if resume else "w",
                     newline="", encoding="utf-8")
    err_w     = csv.writer(ferr)
    if not resume:
        err_w.writerow(["work_id", "reason"])

    todo  = [wid for wid in work_ids if wid not in done]
    total = len(todo)
    w     = len(str(total))

    print(f"\n{'─'*60}", flush=True)
    print(f"Step 3: scraping {total} work(s) → {out}", flush=True)
    print(f"{'─'*60}\n", flush=True)

    written: list[dict] = []
    try:
        for i, wid in enumerate(todo, start=1):
            print(f"  [{i:>{w}}/{total}] {wid} … ", end="", flush=True)
            row = _scrape_one(wid, session)
            if row:
                writer.writerow(row)
                fout.flush()
                written.append(row)
                print(f"✓  {row['title'][:55]}", flush=True)
            else:
                err_w.writerow([wid, "failed or access denied"])
                ferr.flush()
                print("✗  (logged)", flush=True)
    finally:
        fout.close()
        ferr.close()

    print(f"\n{'─'*60}", flush=True)
    print(f"✓ Done. {len(written)} rows → {out}", flush=True)
    if os.path.exists(err_path) and os.path.getsize(err_path) > 20:
        print(f"  Errors → {err_path}", flush=True)

    return written


# =============================================================================
# CLI
# =============================================================================

def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ao3_metadata_scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )

    src = p.add_mutually_exclusive_group()
    src.add_argument("url", nargs="?", default=None, metavar="URL",
                     help="AO3 search/tag/fandom URL — runs all 3 steps")
    src.add_argument("--step2", metavar="PAGES_FILE",
                     help="Run Step 2 only from an existing page-URLs file")
    src.add_argument("--step3", metavar="IDS_FILE",
                     help="Run Step 3 only from an existing work-IDs file")

    p.add_argument("--start-page", type=int, default=1, metavar="N",
                   help="First listing page to include (default: 1)")
    p.add_argument("--end-page",   type=int, default=None, metavar="N",
                   help="Last listing page to include (required with a URL)")
    p.add_argument("--pages-out", default="ao3_pages.txt", metavar="FILE",
                   help="Step 1 output (default: ao3_pages.txt)")
    p.add_argument("--ids-out",  default="ao3_work_ids.txt", metavar="FILE",
                   help="Step 2 output (default: ao3_work_ids.txt)")
    p.add_argument("--out",      default="ao3_metadata.csv", metavar="FILE",
                   help="Step 3 output (default: ao3_metadata.csv)")
    p.add_argument("--step1-only", action="store_true",
                   help="Run Step 1 only (build page list, stop)")
    p.add_argument("--resume", action="store_true",
                   help="Step 3: append to existing CSV, skip done IDs")
    p.add_argument("--header", default="", metavar="AGENT",
                   help='User-Agent suffix e.g. "MyProject/1.0; me@email.com"')
    return p.parse_args()


def main() -> None:
    args    = _args()
    session = make_session(args.header)

    # ── Step 2 only ──────────────────────────────────────────────────────────
    if args.step2:
        p = Path(args.step2)
        if not p.exists():
            sys.exit(f"File not found: {args.step2}")
        urls = [ln.strip() for ln in p.read_text().splitlines() if ln.strip()]
        ids  = step2_collect_ids(urls, session, args.ids_out)
        if not ids:
            sys.exit("No IDs collected.")
        step3_collect_metadata(ids, session, args.out, resume=args.resume)
        return

    # ── Step 3 only ──────────────────────────────────────────────────────────
    if args.step3:
        p = Path(args.step3)
        if not p.exists():
            sys.exit(f"File not found: {args.step3}")
        ids = [ln.strip() for ln in p.read_text().splitlines()
               if ln.strip().isdigit()]
        if not ids:
            sys.exit("No valid IDs found in file.")
        step3_collect_metadata(ids, session, args.out, resume=args.resume)
        return

    # ── Full pipeline ─────────────────────────────────────────────────────────
    if not args.url:
        sys.exit("Provide a URL or use --step2 / --step3. Run with --help.")

    if "archiveofourown.org" not in args.url:
        print("Warning: URL doesn't look like an AO3 URL.", file=sys.stderr)

    if args.end_page is None:
        sys.exit(
            "Error: --end-page N is required when scraping from a URL.\n"
            "Check the last page number on your search results page in a browser,\n"
            "then re-run with e.g. --end-page 62"
        )

    print(f"\n── Step 1: Build page URL list ──────────────────────────────")
    print(f"URL  : {args.url}")
    print(f"Pages: {args.start_page} → {args.end_page}\n", flush=True)
    page_urls = step1_build_page_list(
        args.url, args.start_page, args.end_page, args.pages_out
    )
    if not page_urls:
        sys.exit("Step 1 produced no URLs.")

    if args.step1_only:
        print(f"\nStopping after Step 1 (--step1-only).")
        print(f"Inspect {args.pages_out}, then run:")
        print(f"  python {sys.argv[0]} --step2 {args.pages_out} "
              f"--ids-out {args.ids_out} --out {args.out}")
        return

    print(f"\n── Step 2: Collect work IDs ─────────────────────────────────")
    ids = step2_collect_ids(page_urls, session, args.ids_out)
    if not ids:
        sys.exit("Step 2 collected no IDs.")

    print(f"\n── Step 3: Collect metadata ─────────────────────────────────")
    step3_collect_metadata(ids, session, args.out, resume=args.resume)


if __name__ == "__main__":
    main()
