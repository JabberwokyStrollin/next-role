"""
discover_boards_from_careers.py — find ATS boards by scraping company careers pages.

For each company in company_registry.json with a job_portal_url, fetch the
page and look for embedded ATS markers. Add any newly-detected boards to
target_boards.json via auto_add_board.

Detection strategies (first hit wins, cheapest first):
  1. Final URL after redirects (e.g. shopify.com/careers → boards.greenhouse.io/shopify)
  2. <a href> and <iframe src> in HTML — most enterprise careers pages
     embed Greenhouse as an iframe or link out to Lever/Ashby
  3. <script src> — for widget-based ATS embeds
  4. raw-HTML regex — catches ATS URLs in inline JSON / data-attributes

Writes per-scan diagnostic records to data/board_discovery_log.jsonl so
"why didn't X get discovered" questions are answerable after the fact
without re-running the scrape.

Usage:
    python scripts/discover_boards_from_careers.py --dry-run --limit 5 --verbose
    python scripts/discover_boards_from_careers.py --company Shopify --verbose
    python scripts/discover_boards_from_careers.py --dry-run
    python scripts/discover_boards_from_careers.py
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    COMPANY_REGISTRY_PATH,
    DATA_DIR,
    TARGET_BOARDS_PATH,
    load_json,
    save_json,
    today,
)
from crawl import detect_ats

BOARD_DISCOVERY_LOG = DATA_DIR / "board_discovery_log.jsonl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

REQUEST_DELAY_S = 0.8  # polite pacing between careers-page fetches (different host each time)
API_DELAY_S     = 0.3  # between ATS API validation calls (3 shared hosts)

# Companies that proxy an ATS through their own domain emit a recognizable
# query param. The slug isn't in the URL — we guess it from the company name
# and validate via the ATS public API before recording.
PROXY_PARAMS = {
    "gh_jid":    "greenhouse",
    "ashby_jid": "ashby",
    "lever_jid": "lever",
}
PROXY_PARAM_RE = re.compile(r"[?&](" + "|".join(PROXY_PARAMS) + r")=", re.I)


def slug_from_name(name: str) -> str:
    """Best-guess ATS slug from company name: lowercased, alphanumeric only.
    Catches the common case where slug == lowercased name (stripe, databricks,
    lyft). Misses rebrand/legal-name cases (e.g. DoorDash's `doordashusa`)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def validate_ats_slug(ats: str, slug: str) -> bool:
    """Hit the ATS public API; return True iff slug yields a non-empty board."""
    if not slug:
        return False
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    elif ats == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}"
    elif ats == "ashby":
        url = (
            "https://jobs.ashbyhq.com/api/non-admin/organization/job-board"
            f"?organizationHostedJobsPageName={slug}"
        )
    else:
        return False
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
    except Exception:
        return False
    time.sleep(API_DELAY_S)
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except Exception:
        return False
    if ats == "greenhouse":   return bool(data.get("jobs"))
    if ats == "lever":        return isinstance(data, list) and len(data) > 0
    if ats == "ashby":        return bool(data.get("jobPostings"))
    return False


def _log(record: dict) -> None:
    """Append a single scan record. Best-effort; never raises."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **record,
        }
        with BOARD_DISCOVERY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def api_probe_only(company_name: str) -> tuple[tuple[str, str] | None, dict]:
    """No careers page available — probe all 3 ATS APIs with slug=name guess."""
    diag = {"url_in": None}
    slug_guess = slug_from_name(company_name)
    for ats in ("greenhouse", "lever", "ashby"):
        if validate_ats_slug(ats, slug_guess):
            diag.update(reason="match_api_probe_no_page", ats=ats, slug=slug_guess)
            return (ats, slug_guess), diag
    diag["reason"] = "no_ats_found_no_page"
    return None, diag


def detect_in_careers_page(url: str, company_name: str) -> tuple[tuple[str, str] | None, dict]:
    """
    Fetch a careers page and try to detect the underlying ATS.

    Strategies, cheapest first:
      1. Redirect landed on a known ATS host
      2. <a>/<iframe>/<script> attribute matches detect_ats
      3. Raw HTML contains an ATS URL
      4. Proxy query param (gh_jid/ashby_jid/lever_jid) + validate guessed slug
      5. API probe of all 3 supported ATSes with slug guessed from company name

    Strategies 4 and 5 perform live API validation so we never store an
    unverified (ats, slug) pair. Returns (ats_info, diag).
    """
    diag: dict = {"url_in": url}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    except Exception as e:
        diag.update(reason="exception", exc_type=type(e).__name__, exc_msg=str(e)[:200])
        return None, diag

    diag.update(
        url_resolved = resp.url,
        status       = resp.status_code,
        raw_html_len = len(resp.text),
        content_type = resp.headers.get("Content-Type", ""),
    )

    if resp.status_code != 200:
        diag["reason"] = "http_error"
        return None, diag

    # Strategy 1
    info = detect_ats(resp.url)
    if info:
        diag.update(reason="match_redirect", ats=info[0], slug=info[1])
        return info, diag

    # Strategy 2
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag_name, attr in (("a", "href"), ("iframe", "src"), ("script", "src")):
        for el in soup.find_all(tag_name):
            val = el.get(attr)
            if not val:
                continue
            info = detect_ats(val)
            if info:
                diag.update(
                    reason  = f"match_{tag_name}_{attr}",
                    ats     = info[0],
                    slug    = info[1],
                    hit_url = val[:200],
                )
                return info, diag

    # Strategy 3
    info = detect_ats(resp.text)
    if info:
        diag.update(reason="match_raw_html", ats=info[0], slug=info[1])
        return info, diag

    # Strategy 4: proxy hint + validated slug guess
    slug_guess = slug_from_name(company_name)
    proxy_match = PROXY_PARAM_RE.search(resp.text)
    if proxy_match:
        ats_hint = PROXY_PARAMS[proxy_match.group(1).lower()]
        if validate_ats_slug(ats_hint, slug_guess):
            diag.update(reason="match_proxy_validated", ats=ats_hint, slug=slug_guess)
            return (ats_hint, slug_guess), diag
        diag["proxy_unverified"] = {"ats": ats_hint, "slug_tried": slug_guess}

    # Strategy 5: blind probe of supported ATS APIs with slug=name
    for ats in ("greenhouse", "lever", "ashby"):
        if validate_ats_slug(ats, slug_guess):
            diag.update(reason="match_api_probe", ats=ats, slug=slug_guess)
            return (ats, slug_guess), diag

    diag["reason"] = "no_ats_found"
    return None, diag


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover ATS boards by scraping company careers pages."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; do not write to target_boards.json.")
    parser.add_argument("--limit",   type=int, metavar="N",
                        help="Cap to first N companies (testing).")
    parser.add_argument("--company", metavar="NAME",
                        help="Scan only this company (case-insensitive exact match on registry name).")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-company decisions in stdout.")
    args = parser.parse_args()

    registry = load_json(COMPANY_REGISTRY_PATH)
    boards   = load_json(TARGET_BOARDS_PATH)
    already         = {(b.get("ats", "").lower(), b.get("slug", "")) for b in boards}
    already_companies = {b.get("company", "").lower() for b in boards if b.get("company")}

    # Every registry company is in play. Companies with a job_portal_url get
    # the full careers-page scrape; the rest skip straight to the API probe.
    candidates = list(registry)
    if args.company:
        candidates = [
            c for c in candidates if c.get("name", "").lower() == args.company.lower()
        ]
        if not candidates:
            print(f"No company named '{args.company}' in registry.")
            return
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"Scanning {len(candidates)} compan{'y' if len(candidates) == 1 else 'ies'}...")
    print()

    discovered:    list[tuple[str, str, str]] = []  # (company_name, ats, slug)
    skipped_known = 0
    no_match      = 0
    fetch_failed  = 0

    for i, c in enumerate(candidates, 1):
        name = c.get("name", "?")
        url  = c.get("job_portal_url", "")

        # Cheap early skip: company already has at least one board in the lane.
        if not args.company and name.lower() in already_companies:
            skipped_known += 1
            if args.verbose:
                print(f"  [{i:>3}/{len(candidates)}] . {name:<28} (company already onboarded)")
            continue

        if url:
            info, diag = detect_in_careers_page(url, name)
        else:
            info, diag = api_probe_only(name)
        diag["company"] = name
        _log(diag)

        if info is None:
            if diag.get("reason") in ("exception", "http_error"):
                fetch_failed += 1
                if args.verbose:
                    print(f"  [{i:>3}/{len(candidates)}] x {name:<28} fetch failed ({diag.get('reason')})")
            else:
                no_match += 1
                if args.verbose:
                    print(f"  [{i:>3}/{len(candidates)}] - {name:<28} no ATS detected")
        elif (info[0], info[1]) in already:
            skipped_known += 1
            if args.verbose:
                print(f"  [{i:>3}/{len(candidates)}] . {name:<28} {info[0]}/{info[1]} (already known)")
        else:
            discovered.append((name, info[0], info[1]))
            already.add((info[0], info[1]))           # dedup within this run
            already_companies.add(name.lower())
            print(f"  [{i:>3}/{len(candidates)}] + {name:<28} {info[0]}/{info[1]}  via {diag.get('reason')}")

        time.sleep(REQUEST_DELAY_S)

    print()
    print(f"Scanned:           {len(candidates)}")
    print(f"Discovered (new):  {len(discovered)}")
    print(f"Already known:     {skipped_known}")
    print(f"No ATS detected:   {no_match}")
    print(f"Fetch failed:      {fetch_failed}")

    if not discovered:
        return

    if args.dry_run:
        print("\nDry run — no changes written.")
        return

    iso = today()
    new_entries = [
        {
            "company":   name,
            "ats":       ats,
            "slug":      slug,
            "added":     iso,
            "added_via": "careers_page_scrape",
        }
        for name, ats, slug in discovered
    ]
    boards.extend(new_entries)
    save_json(TARGET_BOARDS_PATH, boards)
    print(f"\nWrote {len(discovered)} new board(s) to {TARGET_BOARDS_PATH.relative_to(ROOT)}.")


if __name__ == "__main__":
    main()
