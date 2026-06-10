"""
ingest.py — Fetch, validate, score, and write a job to the pipeline.

Handles two input modes:
  --url   : Fetch a job posting from a URL, extract JD text, score and ingest.
  --paste : Read JD text from stdin (for portals that block scraping).

Calls score_jd.py for seniority/domain scoring.
Calls research_company.py if the company is not already in the registry
or if the registry record is older than 30 days.

Usage:
    python scripts/ingest.py --url "https://boards.greenhouse.io/stripe/jobs/123"
    python scripts/ingest.py --url "https://..." --company "Stripe"
    cat jd.txt | python scripts/ingest.py --paste --company "Stripe" --title "Staff Engineer" --location "Remote Canada" --apply-url "https://..."
"""

import argparse
import sys
import uuid as uuid_lib
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    COMPANY_REGISTRY_PATH,
    COMPONENTS,
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    detect_no_sponsorship,
    load_json,
    save_json,
    now_utc,
    today,
    compute_stack_score,
    compute_velocity_score,
    compute_staleness,
    classify_role_exposure,
)
from score_jd import score_jd
from research_company import build_registry_record, upsert_company

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_JD_LENGTH = 200  # Minimum characters for a substantive JD

# ── Logging ───────────────────────────────────────────────────────────────────

def append_log(entry: dict) -> None:
    log = load_json(PROCESS_LOG_PATH)
    log.append({
        "log_id":       str(uuid_lib.uuid4()),
        "timestamp":    now_utc(),
        "session_date": today(),
        **entry,
    })
    save_json(PROCESS_LOG_PATH, log)


# ── JD fetching ───────────────────────────────────────────────────────────────

def fetch_jd_from_url(url: str) -> str:
    """
    Fetch a job posting page and extract the main text content.
    Returns cleaned text string.
    Raises requests.RequestException on network errors.
    Raises ValueError if page returns non-200 or no meaningful content found.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=15)

    if resp.status_code != 200:
        raise ValueError(f"URL returned HTTP {resp.status_code}: {url}")

    # Check for redirect to a generic careers home (dead link indicator)
    if resp.url != url and any(
        dead in resp.url for dead in ["/jobs", "/careers", "/404", "lever.co/", "greenhouse.io/"]
        if not resp.url.endswith(url.split("/")[-1])
    ):
        raise ValueError(f"URL redirected to generic page — likely a dead link: {resp.url}")

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove boilerplate elements
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    # Try common job description container selectors first
    jd_text = ""
    for selector in [
        "[data-qa='job-description']",     # Lever
        "#content",                         # Greenhouse
        ".job-description",
        ".job__description",
        ".description",
        "main",
        "article",
    ]:
        el = soup.select_one(selector)
        if el:
            jd_text = el.get_text(separator="\n", strip=True)
            if len(jd_text) >= MIN_JD_LENGTH:
                break

    # Fall back to full body text
    if len(jd_text) < MIN_JD_LENGTH:
        jd_text = soup.get_text(separator="\n", strip=True)

    # Collapse excessive whitespace
    lines = [l.strip() for l in jd_text.splitlines() if l.strip()]
    jd_text = "\n".join(lines)

    return jd_text


# ── Validation ────────────────────────────────────────────────────────────────

def validate_job(title: str, apply_url: str, location: str, jd_text: str) -> list[str]:
    """
    Check minimum required fields. Returns list of failure reasons (empty = pass).
    """
    failures = []
    if not title or not title.strip():
        failures.append("missing title")
    if not apply_url or not apply_url.strip():
        failures.append("missing apply_url")
    if not location or not location.strip():
        failures.append("missing location")
    if not jd_text or len(jd_text.strip()) < MIN_JD_LENGTH:
        failures.append(f"jd_text too short ({len(jd_text.strip())} chars, min {MIN_JD_LENGTH})")
    return failures


def check_duplicate(apply_url: str, jobs: list) -> dict | None:
    """Return existing active job record if apply_url already exists in pipeline."""
    return next(
        (j for j in jobs
         if j["apply_url"] == apply_url and j["pipeline_status"] != "archived"),
        None
    )


# ── Company lookup ────────────────────────────────────────────────────────────

def get_or_stub_company(company_name: str) -> dict | None:
    """
    Look up company in registry. If not found, create a stub record with
    default scores — no API call. Research happens later only for top-ranked jobs.
    Returns company record dict, or None if hard-excluded.

    Note: company-based throttling (the "already have N apps in flight" rule)
    is enforced at apply-surface time via ``config.company_block_reason``, not
    here — the crawl is intentionally permissive so good roles enter the
    pipeline regardless of existing in-flight applications.
    """
    companies = load_json(COMPANY_REGISTRY_PATH)
    existing = next(
        (c for c in companies if c["name"].lower() == company_name.lower()),
        None
    )

    if existing:
        if existing.get("ethics_hard_exclude"):
            print(f"  Company {company_name} is ethics-excluded — skipping job.")
            return None
        print(f"  Using existing company record.")
        return existing

    # Create stub with neutral defaults — no API call
    print(f"  Company not in registry — creating stub record.")
    stub = build_registry_record({"name": company_name})
    stub["stub"] = True  # flag so run.py knows to research if job ranks well
    upsert_company(stub)
    append_log({
        "event_type":  "company_created",
        "entity_type": "company",
        "entity_id":   stub["company_id"],
        "entity_name": company_name,
        "detail":      f"Stub record created for {company_name} — research pending on rank.",
    })
    return stub


# ── Main ingest ───────────────────────────────────────────────────────────────

def ingest_job(
    apply_url:    str,
    company_name: str,
    title:        str,
    location:     str,
    jd_text:      str,
    date_posted:  str | None,
    source:       str,
) -> dict | None:
    """
    Full ingest pipeline for a single job.
    Returns the written job record, or None if discarded.
    """
    # Sanitize all text fields — strip surrogates that break JSON on Windows,
    # then strip leading/trailing whitespace from the single-line fields so
    # near-duplicate titles like "Staff Engineer" vs "Staff Engineer " don't
    # render as visually distinct rows.
    jd_text      = jd_text.encode("utf-8", errors="ignore").decode("utf-8")
    title        = title.encode("utf-8", errors="ignore").decode("utf-8").strip()
    company_name = company_name.encode("utf-8", errors="ignore").decode("utf-8").strip()
    location     = location.encode("utf-8", errors="ignore").decode("utf-8").strip()

    jobs = load_json(JOB_PIPELINE_PATH)

    # ── Deduplication ──────────────────────────────────────────────────────────
    dupe = check_duplicate(apply_url, jobs)
    if dupe:
        print(f"  Duplicate — job already in pipeline: {dupe['job_id']}")
        return None

    # ── Validation ────────────────────────────────────────────────────────────
    failures = validate_job(title, apply_url, location, jd_text)
    if failures:
        reason = ", ".join(failures)
        print(f"  Validation failed: {reason}")
        append_log({
            "event_type":  "job_discarded",
            "entity_type": "job",
            "entity_name": f"{company_name} — {title}",
            "source_url":  apply_url,
            "detail":      f"Job discarded: {reason}",
        })
        return None

    # ── Company lookup ────────────────────────────────────────────────────────
    company = get_or_stub_company(company_name)
    if company is None:
        append_log({
            "event_type":  "job_discarded",
            "entity_type": "job",
            "entity_name": f"{company_name} — {title}",
            "source_url":  apply_url,
            "detail":      "Job discarded: company is ethics-excluded.",
        })
        return None

    # ── ATS auto-onboarding ──────────────────────────────────────────────────-
    # Every ingest contributes to the ATS deep-crawl lane, not just aggregator
    # listings. Lazy import: crawl.py imports ingest_job, so a module-level
    # import here would be circular.
    from crawl import detect_ats, auto_add_board  # noqa: WPS433
    ats_info = detect_ats(apply_url)
    if ats_info:
        if auto_add_board(company["name"], *ats_info, added_via="ingest"):
            print(f"  [+] Added ATS board: {ats_info[0]} / {ats_info[1]}")

    # ── No-sponsorship JD discard ─────────────────────────────────────────────
    # Per-JD hard exclude: the posting explicitly refuses visa sponsorship.
    # Runs before scoring so we don't spend a Claude call on something we're
    # going to discard. Independent of the company-level sponsorship signal,
    # which scores the org's historical record across all postings.
    no_sponsor_snippet = detect_no_sponsorship(jd_text)
    if no_sponsor_snippet:
        print(f"  JD refuses sponsorship: \"...{no_sponsor_snippet}...\" — discarding.")
        append_log({
            "event_type":  "job_discarded",
            "entity_type": "job",
            "entity_name": f"{company_name} — {title}",
            "source_url":  apply_url,
            "detail":      f"Job discarded: JD says no sponsorship (\"...{no_sponsor_snippet}...\").",
        })
        return None

    # ── Mechanical scores (no Claude) ─────────────────────────────────────────
    stack_score    = compute_stack_score(jd_text)
    velocity_score = compute_velocity_score(date_posted)
    staleness      = compute_staleness(date_posted)

    print(f"  Stack score: {stack_score}/{COMPONENTS['stack'].native_max}  "
          f"Velocity: {velocity_score}/{COMPONENTS['velocity'].native_max}  "
          f"Staleness: {staleness}")

    # ── Claude judgment scores ────────────────────────────────────────────────
    print("  Scoring JD with Claude...", flush=True)
    scores = score_jd(jd_text)
    print(f"  Seniority: {scores['seniority_score']}/{COMPONENTS['seniority'].native_max}  "
          f"Domain: {scores['domain_fit_score']}/{COMPONENTS['domain'].native_max}")
    print(f"  Notes: {scores['score_notes']}")

    # ── Build job record ──────────────────────────────────────────────────────
    job_id  = str(uuid_lib.uuid4())
    now     = now_utc()
    job = {
        "job_id":               job_id,
        "company_id":           company["company_id"],
        "company_name":         company["name"],
        "title":                title,
        "apply_url":            apply_url,
        "location":             location,
        "job_type":             "remote" if "remote" in location.lower() else "unknown",
        "jd_text":              jd_text,
        "date_posted":          date_posted,
        "date_found":           now,
        "date_last_verified":   now,
        "source":               source,
        "staleness_status":     staleness,
        "staleness_updated":    now,
        "stack_match_score":    stack_score,
        "seniority_score":      scores["seniority_score"],
        "domain_fit_score":     scores["domain_fit_score"],
        "hiring_velocity_score": velocity_score,
        "score_notes":          scores["score_notes"],
        "role_exposure":        classify_role_exposure(title, scores.get("role_exposure")),
        "cover_letter_generated": False,
        "cover_letter_version":   0,
        "pipeline_status":      "active",
        "pay_range_min":        None,
        "pay_range_max":        None,
        "pay_currency":         None,
        "tags":                 [],
        "notes":                "",
    }

    jobs.append(job)
    save_json(JOB_PIPELINE_PATH, jobs)

    append_log({
        "event_type":  "validation_summary",
        "entity_type": "job",
        "entity_id":   job_id,
        "entity_name": f"{company_name} — {title}",
        "source_url":  apply_url,
        "detail": (
            f"Job ingested. "
            f"Stack: {stack_score}/{COMPONENTS['stack'].native_max}, "
            f"Velocity: {velocity_score}/{COMPONENTS['velocity'].native_max}, "
            f"Seniority: {scores['seniority_score']}/{COMPONENTS['seniority'].native_max}, "
            f"Domain: {scores['domain_fit_score']}/{COMPONENTS['domain'].native_max}. "
            f"Staleness: {staleness}."
        ),
    })

    return job


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest a job into the pipeline.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url",   metavar="URL",  help="Fetch JD from this URL")
    mode.add_argument("--paste", action="store_true", help="Read JD text from stdin")

    parser.add_argument("--company",   metavar="NAME", help="Company name (required for --paste, optional override for --url)")
    parser.add_argument("--title",     metavar="TITLE", help="Job title (required for --paste)")
    parser.add_argument("--location",  metavar="LOC",   help="Location string e.g. 'Remote Canada' (required for --paste)")
    parser.add_argument("--apply-url", metavar="URL",   help="Apply URL (required for --paste)")
    parser.add_argument("--posted",    metavar="DATE",  help="Date posted YYYY-MM-DD (optional)")
    args = parser.parse_args()

    if args.url:
        apply_url = args.url
        print(f"Fetching JD from {apply_url}...")
        try:
            jd_text = fetch_jd_from_url(apply_url)
        except Exception as e:
            print(f"Error fetching URL: {e}")
            sys.exit(1)

        # Derive title and location from args or prompt user
        title    = args.title    or input("Job title: ").strip()
        location = args.location or input("Location (e.g. Remote Canada): ").strip()
        company  = args.company  or input("Company name: ").strip()
        source   = "direct_scrape"

    else:  # --paste
        if not all([args.company, args.title, args.location, args.apply_url]):
            print("Error: --paste requires --company, --title, --location, and --apply-url")
            sys.exit(1)
        print("Reading JD from stdin (paste text, then Ctrl+Z on Windows / Ctrl+D on Mac)...")
        jd_text  = sys.stdin.read()
        apply_url = args.apply_url
        title     = args.title
        location  = args.location
        company   = args.company
        source    = "manual"

    print(f"\nIngesting: {company} — {title}")
    print(f"  Location:  {location}")
    print(f"  Apply URL: {apply_url}")
    print(f"  JD length: {len(jd_text)} chars\n")

    job = ingest_job(
        apply_url    = apply_url,
        company_name = company,
        title        = title,
        location     = location,
        jd_text      = jd_text,
        date_posted  = args.posted,
        source       = source,
    )

    if job:
        print(f"\nJob written to pipeline: {job['job_id']}")
        print(f"Pipeline status: {job['pipeline_status']}")
    else:
        print("\nJob was not added to pipeline (see reason above).")


if __name__ == "__main__":
    main()
