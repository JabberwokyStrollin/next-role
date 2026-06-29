"""
crawl.py — Two-lane automated job board crawler.

Lane 1 (Aggregators): RemoteOK, Remotive — broad coverage, catches smaller/unknown companies.
Lane 2 (ATS direct): Greenhouse, Lever, Ashby — thorough coverage of known target companies.

Both lanes feed a cheap mechanical pre-filter (title + location + stack keywords) before
full ingest via ingest_job(). ATS boards discovered via aggregator apply URLs are
auto-added to data/target_boards.json so the curated list grows organically.

Usage:
    python scripts/crawl.py
    python scripts/crawl.py --dry-run          # show candidates, skip ingest
    python scripts/crawl.py --verbose          # show pre-filter decision for every listing
    python scripts/crawl.py --source remoteok  # run a single source only
    python scripts/crawl.py --limit 10         # cap ingest (useful for testing)
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import yaml
from bs4 import BeautifulSoup

from config import (
    CRAWL_LOG_PATH,
    JOB_PIPELINE_PATH,
    STACK_KEYWORDS_PATH,
    TARGET_BOARDS_PATH,
    load_json,
    save_json,
    today,
    compute_stack_score,
    location_passes,
)
from ingest import ingest_job

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; next-role job crawler)"}

# ── Crawl config ──────────────────────────────────────────────────────────────

CRAWL_CONFIG_DEFAULTS = {
    "seniority_titles":           ["staff", "principal", "senior staff", "lead engineer",
                                   "lead developer", "architect"],
    "title_exclude":              ["solutions architect", "delivery architect",
                                   "sales engineer", "customer success",
                                   "professional services"],
    "location_allow":             ["remote", "canada", "ireland"],
    "aggregator_tag_groups":      [["kafka", "flink", "java"]],
    "aggregator_keyword_groups":  ["kafka flink java"],
    "min_pre_filter_score":       3,
}


def load_crawl_config() -> dict:
    """Load crawl section from profile/stack_keywords.yaml."""
    if not STACK_KEYWORDS_PATH.exists():
        return dict(CRAWL_CONFIG_DEFAULTS)

    data  = yaml.safe_load(STACK_KEYWORDS_PATH.read_text(encoding="utf-8")) or {}
    crawl = data.get("crawl") or {}

    cfg = dict(CRAWL_CONFIG_DEFAULTS)
    if "seniority_titles" in crawl:
        # Preserve trailing whitespace — entries like 'sr ' rely on the
        # trailing space to avoid bare 'sr' matching inside 'disruptive',
        # 'israel-based', etc. via the substring check in pre_filter.
        cfg["seniority_titles"] = [str(t).lower() for t in crawl["seniority_titles"]]
    if "title_exclude" in crawl:
        cfg["title_exclude"] = [str(t).strip().lower() for t in crawl["title_exclude"]]
    if "location_allow" in crawl:
        cfg["location_allow"] = [str(l).strip().lower() for l in crawl["location_allow"]]
    if "aggregator_tags" in crawl:
        cfg["aggregator_tag_groups"] = [
            [str(t).strip() for t in group] for group in crawl["aggregator_tags"]
        ]
    if "aggregator_keywords" in crawl:
        cfg["aggregator_keyword_groups"] = [str(q).strip() for q in crawl["aggregator_keywords"]]
    if "min_pre_filter_score" in crawl:
        try:
            cfg["min_pre_filter_score"] = int(crawl["min_pre_filter_score"])
        except (TypeError, ValueError):
            pass
    return cfg


# ── HTML utilities ────────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup  = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip()]
    return "\n".join(lines)


# ── Pre-filter (no API cost) ──────────────────────────────────────────────────
#
# This pre-filter is INTENTIONALLY pre-LLM. It runs on every raw aggregator
# hit (~5000 listings per crawl) to cut down to the few worth scoring. The
# composite_score() in scripts/config.py requires Claude scoring + company
# research — calling it here would defeat the cost model entirely. Do NOT
# import composite_score in this file. The signals checked below
# (title allowlist/blocklist, location_allow, mechanical stack score on
# title + JD prefix) come from profile/stack_keywords.yaml, which is the
# canonical SSOT for pre-filter configuration.

def title_excluded(title_lower: str, terms: list[str]) -> str | None:
    """Return the first term in ``terms`` that appears as a whole word inside
    ``title_lower`` — or None if none match. Surrounds each term with
    non-letter boundaries so single-word terms like ``"intern"`` don't match
    inside longer words like ``"international"``. Multi-word terms
    (``"solutions architect"``) and terms ending in non-letter chars
    (``"jr."``, ``"entry-level"``) work too because the boundaries only
    forbid an adjacent ASCII letter.

    Both ``title_lower`` and entries in ``terms`` MUST already be lowercase
    — callers lowercase once at config-load time and per-row in pre_filter.
    """
    for bad in terms:
        if re.search(rf"(?<![a-z]){re.escape(bad)}(?![a-z])", title_lower):
            return bad
    return None


def pre_filter(title: str, location: str, text: str, cfg: dict,
               source: str | None = None) -> tuple[bool, str]:
    """Returns (passes, reason_string). ``source`` lets the US remote-only gate
    be source-aware (remote-only boards count region-only US locations as
    remote — see config.is_remote_role)."""
    t = title.lower()
    l = location.lower()

    if not any(kw in t for kw in cfg["seniority_titles"]):
        return False, f"title seniority miss ({title[:50]})"

    bad = title_excluded(t, cfg.get("title_exclude", []))
    if bad:
        return False, f"title excluded by '{bad}' ({title[:50]})"

    if not any(kw in l for kw in cfg["location_allow"]):
        return False, f"location miss ({location[:40]})"

    # Subtractive geography gate (config SSOT, not the YAML allowlist): drops
    # US rows the allowlist admits via bare "remote"/"americas" unless "US" is
    # an enabled target AND the role is remote. CA/IE/OTHER pass through.
    if not location_passes(location, source=source):
        return False, f"location US-gated (off / not remote) ({location[:40]})"

    score = compute_stack_score(f"{title} {text[:800]}")
    if score < cfg["min_pre_filter_score"]:
        return False, f"stack score {score} < {cfg['min_pre_filter_score']}"

    return True, f"stack {score}"


# ── ATS auto-discovery ────────────────────────────────────────────────────────

# ATSes we have a fetch_* implementation for. detect_ats may return other ATSes
# (workday, smartrecruiters) so target_boards.json captures discovered companies
# even when we can't yet crawl them; the crawl loop silently skips unsupported.
SUPPORTED_ATSES = {"greenhouse", "lever", "ashby"}


def detect_ats(url: str) -> tuple[str, str] | None:
    """Return (ats, slug) if apply URL reveals a known ATS, else None."""
    if not url:
        return None
    # Greenhouse: hosted boards + API
    m = re.search(r"boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([^/?#\s]+)", url)
    if m:
        return ("greenhouse", m.group(1))
    # Lever: US (.co) and EU (.eu.lever.co) hosted boards
    m = re.search(r"jobs(?:\.eu)?\.lever\.co/([^/?#\s]+)", url)
    if m:
        return ("lever", m.group(1))
    # Ashby
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#\s]+)", url)
    if m:
        return ("ashby", m.group(1))
    # Workday — slug is company subdomain (e.g. "stripe" from "stripe.wd1.myworkdayjobs.com").
    # Not yet crawlable; auto-add still records the company for visibility.
    m = re.search(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", url)
    if m:
        return ("workday", m.group(1))
    # SmartRecruiters. Same caveat as Workday — recorded but not fetched.
    m = re.search(r"(?:careers|jobs)\.smartrecruiters\.com/([^/?#\s]+)", url)
    if m:
        return ("smartrecruiters", m.group(1))
    return None


def auto_add_board(company: str, ats: str, slug: str,
                   added_via: str = "auto_discovery") -> bool:
    """Add board to target_boards.json if not already present. Returns True if newly added.

    added_via tags the provenance so a future audit can tell aggregator-driven
    discoveries from ingest-driven ones from backfill passes.
    """
    boards = load_json(TARGET_BOARDS_PATH)
    if any(b.get("ats") == ats and b.get("slug") == slug for b in boards):
        return False
    boards.append({"company": company, "ats": ats, "slug": slug,
                   "added": today(), "added_via": added_via})
    save_json(TARGET_BOARDS_PATH, boards)
    return True


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _get(url: str) -> requests.Response | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp
    except Exception as e:
        print(f"  [warn] {url} — {e}")
        return None


def _ts_to_date(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


# ── Lane 1: Aggregators ───────────────────────────────────────────────────────

def fetch_remoteok(cfg: dict) -> list[dict]:
    out:  list[dict] = []
    seen: set[str]   = set()
    for group in cfg["aggregator_tag_groups"]:
        tags = ",".join(group)
        print(f"  RemoteOK (tags: {tags})...")
        resp = _get(f"https://remoteok.io/api?tags={tags}")
        if not resp:
            continue
        try:
            raw = resp.json()
        except Exception:
            print("  [warn] RemoteOK: invalid JSON")
            continue

        added = 0
        for item in raw:
            if not isinstance(item, dict) or "position" not in item:
                continue
            apply_url = item.get("apply_url") or item.get("url", "")
            if not apply_url or apply_url in seen:
                continue
            seen.add(apply_url)
            out.append({
                "title":       item.get("position", ""),
                "company":     item.get("company", ""),
                "location":    item.get("location", "Worldwide"),
                "apply_url":   apply_url,
                "jd_text":     html_to_text(item.get("description", "")),
                "date_posted": (item.get("date") or "")[:10] or None,
                "source":      "remoteok",
            })
            added += 1
        print(f"    → {added} listings")
    return out


def fetch_remotive(cfg: dict) -> list[dict]:
    out:  list[dict] = []
    seen: set[str]   = set()
    for kw in cfg["aggregator_keyword_groups"]:
        print(f"  Remotive (search: {kw})...")
        resp = _get(f"https://remotive.com/api/remote-jobs?category=software-dev&search={quote(kw)}")
        if not resp:
            continue
        try:
            raw = resp.json().get("jobs", [])
        except Exception:
            print("  [warn] Remotive: invalid JSON")
            continue

        added = 0
        for item in raw:
            apply_url = item.get("url", "")
            if not apply_url or apply_url in seen:
                continue
            seen.add(apply_url)
            out.append({
                "title":       item.get("title", ""),
                "company":     item.get("company_name", ""),
                "location":    item.get("candidate_required_location", "Remote"),
                "apply_url":   apply_url,
                "jd_text":     html_to_text(item.get("description", "")),
                "date_posted": (item.get("publication_date") or "")[:10] or None,
                "source":      "remotive",
            })
            added += 1
        print(f"    → {added} listings")
    return out


# ── Lane 2: ATS direct ────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str, company: str) -> list[dict]:
    resp = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not resp:
        return []
    try:
        raw = resp.json().get("jobs", [])
    except Exception:
        print(f"  [warn] Greenhouse/{slug}: invalid JSON")
        return []

    out = []
    for item in raw:
        out.append({
            "title":       item.get("title", ""),
            "company":     company,
            "location":    (item.get("location") or {}).get("name", ""),
            "apply_url":   item.get("absolute_url", ""),
            "jd_text":     html_to_text(item.get("content", "")),
            "date_posted": (item.get("updated_at") or "")[:10] or None,
            "source":      "greenhouse",
        })
    return out


def fetch_lever(slug: str, company: str) -> list[dict]:
    resp = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not resp:
        return []
    try:
        raw = resp.json()
        if not isinstance(raw, list):
            raw = []
    except Exception:
        print(f"  [warn] Lever/{slug}: invalid JSON")
        return []

    out = []
    for item in raw:
        # Prefer plain text; fall back to assembling from lists/additional blocks
        jd = item.get("descriptionPlain", "")
        if not jd:
            parts = [item.get("description", "")]
            for block in item.get("lists", []):
                parts.append(block.get("text", ""))
                parts.extend(block.get("content", []))
            for block in item.get("additional", []) if isinstance(item.get("additional"), list) else []:
                parts.append(block.get("text", ""))
            raw_desc = "\n".join(filter(None, parts))
            jd = html_to_text(raw_desc) if "<" in raw_desc else raw_desc

        out.append({
            "title":       item.get("text", ""),
            "company":     company,
            "location":    (item.get("categories") or {}).get("location", ""),
            "apply_url":   item.get("hostedUrl", ""),
            "jd_text":     jd,
            "date_posted": _ts_to_date(item.get("createdAt")),
            "source":      "lever",
        })
    return out


def fetch_ashby(slug: str, company: str) -> list[dict]:
    resp = _get(
        f"https://jobs.ashbyhq.com/api/non-admin/organization/job-board"
        f"?organizationHostedJobsPageName={slug}"
    )
    if not resp:
        return []
    try:
        raw = resp.json().get("jobPostings", [])
    except Exception:
        print(f"  [warn] Ashby/{slug}: invalid JSON")
        return []

    out = []
    for item in raw:
        out.append({
            "title":       item.get("title", ""),
            "company":     company,
            "location":    item.get("locationName", item.get("location", "")),
            "apply_url":   item.get("jobPostingUrl",
                           f"https://jobs.ashbyhq.com/{slug}/{item.get('id','')}"),
            "jd_text":     html_to_text(item.get("descriptionHtml", "")),
            "date_posted": (item.get("publishedDate") or "")[:10] or None,
            "source":      "ashby",
        })
    return out


# ── Per-run JSONL log ────────────────────────────────────────────────────────-

def _categorize_reason(reason: str) -> str:
    """Map pre_filter's reason string to a stable funnel category."""
    if reason.startswith("title seniority"): return "title_seniority"
    if reason.startswith("title excluded"):  return "title_exclude"
    if reason.startswith("location"):        return "location"
    if reason.startswith("stack score"):     return "stack"
    return "other"


def _log_crawl_run(record: dict) -> None:
    """Append a single crawl-run summary to CRAWL_LOG_PATH. Best-effort; never raises."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **record,
        }
        with CRAWL_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── Main crawl ────────────────────────────────────────────────────────────────

def crawl(
    dry_run: bool = False,
    verbose: bool = False,
    source:  str | None = None,
    limit:   int | None = None,
) -> int:
    """Run the full two-lane crawl. Returns number of jobs ingested."""
    started_at    = time.time()
    cfg           = load_crawl_config()
    existing_urls = {j["apply_url"] for j in load_json(JOB_PIPELINE_PATH) if j.get("apply_url")}

    print(f"\n── Crawl starting ──────────────────────────────────────────────")
    print(f"  Seniority filter:  {', '.join(cfg['seniority_titles'])}")
    print(f"  Location filter:   {', '.join(cfg['location_allow'])}")
    print(f"  Min stack score:   {cfg['min_pre_filter_score']}")
    print(f"  Pipeline size:     {len(existing_urls)} known URLs (dedup)")
    print()

    listings: list[dict] = []

    # ── Lane 1: Aggregators ───────────────────────────────────────────────────
    if source in (None, "remoteok"):
        listings += fetch_remoteok(cfg)
        time.sleep(1)

    if source in (None, "remotive"):
        listings += fetch_remotive(cfg)
        time.sleep(1)

    # ── Lane 2: ATS direct ────────────────────────────────────────────────────
    if source not in ("remoteok", "remotive"):
        boards = load_json(TARGET_BOARDS_PATH)
        if not boards:
            print("  No ATS boards configured in data/target_boards.json.")
        for board in boards:
            ats     = board.get("ats", "").lower()
            slug    = board.get("slug", "")
            company = board.get("company", slug)
            if source and source != ats:
                continue
            if ats not in SUPPORTED_ATSES:
                continue  # auto-discovered (e.g. workday, smartrecruiters) but no fetcher yet
            print(f"  {ats.capitalize()}: {company} ({slug})...")
            if ats == "greenhouse":
                fetched = fetch_greenhouse(slug, company)
            elif ats == "lever":
                fetched = fetch_lever(slug, company)
            elif ats == "ashby":
                fetched = fetch_ashby(slug, company)
            else:
                continue  # unreachable given SUPPORTED_ATSES check; defensive
            print(f"    → {len(fetched)} listings")
            listings += fetched
            time.sleep(0.3)

    print(f"\n  Total fetched: {len(listings)}")

    # ── Pre-filter + dedup + ATS auto-discovery ───────────────────────────────
    candidates:    list[dict] = []
    skipped_dupe   = 0
    skipped_filter = 0
    funnel         = Counter()
    auto_added     = []

    for listing in listings:
        url = listing.get("apply_url", "")
        if not url:
            continue

        if url in existing_urls:
            skipped_dupe += 1
            continue

        # Auto-discover ATS from aggregator URLs
        if listing["source"] in ("remoteok", "remotive"):
            ats_info = detect_ats(url)
            if ats_info:
                added = auto_add_board(listing["company"], *ats_info)
                if added:
                    auto_added.append({
                        "company": listing["company"],
                        "ats":     ats_info[0],
                        "slug":    ats_info[1],
                    })
                    print(f"  [+] Auto-discovered {ats_info[0]}: {listing['company']} ({ats_info[1]})")

        passes, reason = pre_filter(
            listing.get("title", ""),
            listing.get("location", ""),
            listing.get("jd_text", ""),
            cfg,
            source=listing.get("source"),
        )

        if passes:
            candidates.append(listing)
            funnel["pass"] += 1
            if verbose:
                print(f"  ✓ [{listing['source']:<11}] {listing['company']} — {listing['title']} ({reason})")
        else:
            skipped_filter += 1
            funnel[_categorize_reason(reason)] += 1
            if verbose:
                print(f"  ✗ [{listing['source']:<11}] {listing['company']} — {listing['title']} ({reason})")

    print(f"\n── Pre-filter results ──────────────────────────────────────────")
    print(f"  Passed:    {len(candidates)}")
    print(f"  Dupes:     {skipped_dupe}")
    print(f"  Filtered:  {skipped_filter}")
    if auto_added:
        print(f"  New ATS boards auto-added: {len(auto_added)}")

    if limit:
        candidates = candidates[:limit]
        if limit < len(candidates):
            print(f"  Capped at --limit {limit}")

    ingested = 0
    failed   = 0

    if not candidates:
        print("\n  No new candidates to ingest.")
    elif dry_run:
        print(f"\n── Candidates ──────────────────────────────────────────────────")
        for i, c in enumerate(candidates, 1):
            print(f"  {i:>3}. [{c['source']:<11}] {c['company']:<24} {c['title']}")
        print(f"\n  Dry run — {len(candidates)} would be ingested.")
    else:
        print(f"\n── Candidates ──────────────────────────────────────────────────")
        for i, c in enumerate(candidates, 1):
            print(f"  {i:>3}. [{c['source']:<11}] {c['company']:<24} {c['title']}")
        print(f"\n── Ingesting {len(candidates)} candidates ───────────────────────────────")
        for i, listing in enumerate(candidates, 1):
            print(f"\n[{i}/{len(candidates)}] {listing['company']} — {listing['title']}")
            try:
                job = ingest_job(
                    apply_url    = listing["apply_url"],
                    company_name = listing["company"],
                    title        = listing["title"],
                    location     = listing["location"],
                    jd_text      = listing.get("jd_text", ""),
                    date_posted  = listing.get("date_posted"),
                    source       = listing["source"],
                )
                if job:
                    ingested += 1
                    existing_urls.add(listing["apply_url"])
                else:
                    failed += 1
            except Exception as e:
                print(f"  Error: {e}")
                failed += 1
            time.sleep(0.5)
        print(f"\n── Crawl complete ──────────────────────────────────────────────")
        print(f"  Ingested: {ingested}   Failed/skipped: {failed}")

    _log_crawl_run({
        "duration_s":        int(time.time() - started_at),
        "dry_run":           dry_run,
        "source_filter":     source,
        "total_fetched":     len(listings),
        "dedup_hits":        skipped_dupe,
        "filtered_total":    skipped_filter,
        "funnel":            dict(funnel),
        "passed":            len(candidates),
        "ingested":          ingested,
        "ingest_failed":     failed,
        "auto_added_boards": auto_added,
    })

    return ingested


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crawl job boards and ingest matching jobs.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show candidates without ingesting")
    parser.add_argument("--verbose", action="store_true",
                        help="Show pre-filter decision for every listing")
    parser.add_argument("--source",  metavar="NAME",
                        choices=["remoteok", "remotive", "greenhouse", "lever", "ashby"],
                        help="Run only this source")
    parser.add_argument("--limit",   type=int, metavar="N",
                        help="Cap ingest at N candidates (testing)")
    args = parser.parse_args()

    crawl(
        dry_run = args.dry_run,
        verbose = args.verbose,
        source  = args.source,
        limit   = args.limit,
    )


if __name__ == "__main__":
    main()
