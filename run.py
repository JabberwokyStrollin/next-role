"""
run.py — Main entry point for the next-role pipeline.

Daily workflow:
  1. Ingest jobs from URLs or paste mode
  2. Rank stubs by pre-research composite and research the top N
     (`--research-queue` flow — pre-research signals only, no stub noise)
  3. Generate cover letters for top apply candidates (full composite)
  4. Show dashboard

Usage:
    # Ingest a single job then show dashboard
    python run.py --url "https://boards.greenhouse.io/company/jobs/123"

    # Ingest multiple jobs from a file (one URL per line)
    python run.py --url-file urls.txt

    # Just show dashboard (no ingest)
    python run.py --dashboard

    # Research top N stubs ranked by pre-research composite (preferred)
    python run.py --research-queue 20

    # Preview which stubs the research queue would pick, without spending API credits
    python run.py --research-queue 20 --dry-run

    # Legacy variant: research top N stubs ranked by full composite
    # (sponsorship/remote stub defaults influence this ordering)
    python run.py --research-top 5

    # Full daily run: ingest from file + research queue + dashboard
    python run.py --url-file urls.txt --research-queue 20

    # Dry run: ingest and score but don't research companies
    python run.py --url-file urls.txt --dry-run
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Composite scoring + company filtering are defined ONLY in scripts/config.py
# — see the SCORING SSOT and COMPANY-FILTER SSOT banners there. Do not
# redefine composite_score or composite_score_pre_research or duplicate the
# company-filter rule here.
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from config import (  # noqa: E402
    MAX_ACTIVE_APPS_PER_COMPANY,
    APPLICATION_TRACKER_PATH,
    COMPANY_REGISTRY_PATH,
    JOB_PIPELINE_PATH,
    PRE_RESEARCH_MAX,
    RESEARCH_QUEUE_MIN_SCORE,
    company_block_reason,
    composite_score,
    composite_score_pre_research,
    apply_rank_score,
    derive_country,
    gov_screen_block_reason,
    load_json,
    save_json,
)

# ── stdout encoding (Windows cp1252 → UTF-8) ─────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).parent.resolve()
SCRIPTS    = ROOT / "scripts"
DATA_DIR   = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

# ── Helpers ───────────────────────────────────────────────────────────────────

def run_python(script: str, *args) -> int:
    """Run a Python script in scripts/ with the current interpreter."""
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


def run_node(script: str, *args) -> int:
    """Run a Node.js script in scripts/."""
    cmd = ["node", str(SCRIPTS / script), *args]
    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


# ── Ingest ────────────────────────────────────────────────────────────────────

def ingest_url(url: str, posted: str = None, dry_run: bool = False) -> bool:
    """Ingest a single URL. Returns True if successful."""
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Ingesting: {url}")
    args = ["--url", url]
    if posted:
        args += ["--posted", posted]
    if dry_run:
        print("  Dry run — skipping API calls.")
        return True
    rc = run_python("ingest.py", *args)
    return rc == 0


def ingest_url_file(filepath: str, dry_run: bool = False) -> int:
    """
    Ingest jobs from a text file. Each line is either:
      https://...
      https://... YYYY-MM-DD
    Lines starting with # are comments. Returns count of successful ingests.
    """
    path = Path(filepath)
    if not path.exists():
        print(f"Error: URL file not found: {filepath}")
        sys.exit(1)

    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    lines = [l for l in lines if l and not l.startswith("#")]

    print(f"\nIngesting {len(lines)} jobs from {filepath}...")
    success = 0
    for line in lines:
        parts  = line.split()
        url    = parts[0]
        posted = parts[1] if len(parts) > 1 else None
        if ingest_url(url, posted=posted, dry_run=dry_run):
            success += 1

    print(f"\nIngested {success}/{len(lines)} jobs successfully.")
    return success


# ── Company research ──────────────────────────────────────────────────────────
#
# Two entry points. Both shell out to ``research_company.py --name <name>``
# for the actual Haiku + 1-web-search call; they differ only in HOW they
# pick candidates from the active pipeline:
#
#   research_top_stubs(n)   — ranks active jobs by FULL composite, then picks
#                             the top-N stub companies among them. Inherited
#                             API; stub-default sponsorship/remote values
#                             influence which companies surface.
#
#   research_queue(n)       — ranks active jobs by PRE-RESEARCH composite
#                             (no company-derived signals), applies the
#                             RESEARCH_QUEUE_MIN_SCORE gate, then picks the
#                             top-N distinct stub companies. Preferred for
#                             routine use because stub noise can't bias the
#                             ordering.


def _execute_research(queue: list, dry_run: bool, label: str) -> int:
    """Run ``research_company.py`` for each company in the queue; clear stub flag on success.

    ``queue`` is a list of ``(score, job, company)`` tuples already deduped
    by company. ``label`` prefixes each per-company log line so the operator
    can tell which ranking surfaced the candidate.
    Returns the number of companies actually researched (or counted in
    dry-run mode).
    """
    researched = 0
    for i, (score, _job, company) in enumerate(queue, 1):
        name = company["name"]
        print(f"\n  [{i}/{len(queue)}] {name} ({label}: {score})")
        if dry_run:
            print("  Dry run — skipping research.")
            researched += 1
            continue
        rc = run_python("research_company.py", "--name", name)
        if rc == 0:
            # Clear stub flag now that research is done
            companies = load_json(COMPANY_REGISTRY_PATH)
            updated   = []
            for c in companies:
                if c["name"].lower() == name.lower():
                    c.pop("stub", None)
                updated.append(c)
            save_json(COMPANY_REGISTRY_PATH, updated)
            researched += 1
        else:
            print(f"  Warning: research failed for {name}")
    return researched


def research_top_stubs(n: int, dry_run: bool = False) -> int:
    """Research top-N stubs ranked by FULL composite. Inherited surface.

    Ranks all active jobs by ``composite_score(job, company)`` and picks the
    N highest-ranked stub companies, deduped. Stub-default sponsorship +
    remote values influence this ordering, so for routine research prefer
    ``research_queue(n)`` which uses the pre-research composite instead.
    """
    jobs      = load_json(JOB_PIPELINE_PATH)
    companies = load_json(COMPANY_REGISTRY_PATH)
    co_by_id  = {c["company_id"]: c for c in companies}

    active = [j for j in jobs if j.get("pipeline_status") not in ["archived", "applied"]]
    scored = []
    for job in active:
        company = co_by_id.get(job.get("company_id"))
        score   = composite_score(job, company)
        scored.append((score, job, company))
    scored.sort(key=lambda x: x[0], reverse=True)

    queue          = []
    seen_companies = set()
    for score, job, company in scored[:max(n * 3, 15)]:  # look beyond top N to find stubs
        if len(queue) >= n:
            break
        if not company or not company.get("stub"):
            continue
        cid = company["company_id"]
        if cid in seen_companies:
            continue
        seen_companies.add(cid)
        queue.append((score, job, company))

    if not queue:
        print("\nNo stub companies in top jobs — all companies already researched.")
        return 0

    print(f"\nResearching {len(queue)} stub companies for top-ranked jobs (full composite)...")
    researched = _execute_research(queue, dry_run, label="job score before research")
    print(f"\nResearched {researched} companies.")
    return researched


def research_queue(n: int, dry_run: bool = False) -> int:
    """Research top-N stubs ranked by PRE-RESEARCH composite.

    Ranks all active jobs by ``composite_score_pre_research(job)`` — which
    zero-weights sponsorship + remote — so the order isn't contaminated by
    stub defaults. Applies the ``RESEARCH_QUEUE_MIN_SCORE`` gate so we
    don't spend Haiku + 1 web search on clearly-mediocre stubs. Picks the
    N highest-ranked distinct stub companies attached to active jobs.

    Use this for routine research. Use ``research_top_stubs`` only when
    you explicitly want the full-composite ordering (rare).
    """
    jobs      = load_json(JOB_PIPELINE_PATH)
    companies = load_json(COMPANY_REGISTRY_PATH)
    co_by_id  = {c["company_id"]: c for c in companies}

    active = [j for j in jobs if j.get("pipeline_status") not in ["archived", "applied"]]

    eligible = []
    for job in active:
        pre = composite_score_pre_research(job)
        if pre < RESEARCH_QUEUE_MIN_SCORE:
            continue
        company = co_by_id.get(job.get("company_id"))
        if not company or not company.get("stub"):
            continue
        eligible.append((pre, job, company))
    eligible.sort(key=lambda x: x[0], reverse=True)

    queue          = []
    seen_companies = set()
    for pre, job, company in eligible:
        if len(queue) >= n:
            break
        cid = company["company_id"]
        if cid in seen_companies:
            continue
        seen_companies.add(cid)
        queue.append((pre, job, company))

    if not queue:
        print(
            f"\nNo stub companies meet research-queue criteria "
            f"(active job with pre-research score ≥ {RESEARCH_QUEUE_MIN_SCORE}/{PRE_RESEARCH_MAX} "
            f"and stub=True)."
        )
        return 0

    print(
        f"\nResearch queue: {len(queue)} stub compan{'y' if len(queue) == 1 else 'ies'} "
        f"(pre-research rank, min score {RESEARCH_QUEUE_MIN_SCORE}/{PRE_RESEARCH_MAX})"
        + (" — dry run" if dry_run else "")
        + "..."
    )
    researched = _execute_research(queue, dry_run, label="pre-research score")
    print(
        f"\n{'Would research' if dry_run else 'Researched'} "
        f"{researched} compan{'y' if researched == 1 else 'ies'}."
    )
    return researched


# ── Cover letter generation ───────────────────────────────────────────────────

def generate_cover_letters(top_n: int = 5, auto: bool = False) -> None:
    """
    Offer to generate cover letters for top-N cover_letter_ready or active jobs.
    """
    jobs      = load_json(DATA_DIR / "job_pipeline.json")
    companies = load_json(DATA_DIR / "company_registry.json")
    co_by_id  = {c["company_id"]: c for c in companies}
    apps      = load_json(APPLICATION_TRACKER_PATH)

    # Status-side eligibility.
    raw_eligible = [
        j for j in jobs
        if j.get("pipeline_status") in ["active", "cover_letter_ready"]
        and not j.get("cover_letter_generated")
    ]

    # Company-side eligibility via the COMPANY-FILTER SSOT
    # (config.company_block_reason). Suppress jobs at companies with
    # MAX_ACTIVE_APPS_PER_COMPANY or more in-flight applications.
    scored     = []
    suppressed = 0
    gov_excluded = 0
    for job in raw_eligible:
        company = co_by_id.get(job.get("company_id"))
        if company_block_reason(job.get("company_id"), apps):
            suppressed += 1
            continue
        # Gov-screen fail (tier_a / defense entanglement) is hidden from apply
        # surfaces, same handling as company_block_reason. SSOT: config.
        if gov_screen_block_reason(job, company):
            gov_excluded += 1
            continue
        # Rank by the gov-screen-adjusted score (flag → -GOV_SCREEN_FLAG_PENALTY_PCT%).
        score   = apply_rank_score(job, company)
        scored.append((score, job))
    scored.sort(key=lambda x: x[0], reverse=True)

    candidates = scored[:top_n]
    if not candidates:
        print("\nNo jobs ready for cover letter generation.")
        return

    print(f"\n── Cover letter candidates (top {top_n}, ≤{MAX_ACTIVE_APPS_PER_COMPANY} active apps/co) ──────────")
    if suppressed:
        print(f"  ({suppressed} suppressed: company already at {MAX_ACTIVE_APPS_PER_COMPANY} active applications)")
    if gov_excluded:
        print(f"  ({gov_excluded} excluded: gov/defense entanglement)")
    for i, (score, job) in enumerate(candidates, 1):
        print(f"  {i}. [{score:>3}] {job['company_name']} — {job['title']}")

    if auto:
        selected = candidates
    else:
        print("\nGenerate cover letters for these jobs? (y/n/select numbers e.g. 1,3)")
        choice = input("> ").strip().lower()

        if choice == "n":
            return

        if choice == "y":
            selected = candidates
        else:
            try:
                indices  = [int(x.strip()) - 1 for x in choice.split(",")]
                selected = [candidates[i] for i in indices if 0 <= i < len(candidates)]
            except (ValueError, IndexError):
                print("Invalid selection.")
                return

    for score, job in selected:
        # Canonical country derivation (CA/IE/US/OTHER).
        country = derive_country(job.get("location", ""))
        print(f"\nGenerating: {job['company_name']} — {job['title']}")
        if country == "US":
            # US citizen — no work-authorization paragraph expected; omit --country.
            run_node("generate_cl.js", "--job-id", job["job_id"])
        else:
            # Ambiguous-location remote roles (OTHER) fall back to CA, the
            # operator's default market.
            if country == "OTHER":
                country = "CA"
            run_node("generate_cl.js", "--job-id", job["job_id"], "--country", country)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="next-role pipeline orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Ingest options
    ingest_group = parser.add_mutually_exclusive_group()
    ingest_group.add_argument("--url",      metavar="URL",  help="Ingest a single job URL")
    ingest_group.add_argument("--url-file", metavar="FILE", help="Ingest jobs from a URL file")

    parser.add_argument("--posted",  metavar="DATE", help="Date posted YYYY-MM-DD (for --url)")

    # Pipeline actions
    parser.add_argument("--research-queue", type=int, nargs="?", const=20, metavar="N",
                        help="Research top N stub companies ranked by pre-research "
                             "composite (default N=20). Preferred over --research-top.")
    parser.add_argument("--research-top", type=int, metavar="N",
                        help="Research top N stub companies ranked by full composite. "
                             "Inherited surface; prefer --research-queue for routine use.")
    parser.add_argument("--cover-letters", action="store_true",
                        help="Offer cover letter generation for top 5 after research")
    parser.add_argument("--auto-cl", action="store_true",
                        help="Generate cover letters for top N without prompting (implies --cover-letters)")
    parser.add_argument("--crawl", action="store_true",
                        help="Crawl configured job boards and aggregators for new jobs")
    parser.add_argument("--dashboard", action="store_true",
                        help="Show pipeline dashboard")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and score but skip all API calls. "
                             "With --research-queue, prints the selected companies "
                             "without spending API credits.")
    parser.add_argument("--top", type=int, default=5, metavar="N",
                        help="Number of top jobs to show/consider (default 5)")

    args = parser.parse_args()

    # --auto-cl implies --cover-letters
    if args.auto_cl:
        args.cover_letters = True

    # If no action specified, show dashboard
    if not any([args.url, args.url_file, args.research_top, args.research_queue,
                args.cover_letters, args.dashboard, args.crawl]):
        args.dashboard = True

    # ── Step 1: Crawl ─────────────────────────────────────────────────────────
    if args.crawl:
        sys.path.insert(0, str(SCRIPTS))
        from crawl import crawl as run_crawl
        run_crawl(dry_run=args.dry_run)

    # ── Step 2: Ingest ────────────────────────────────────────────────────────
    if args.url:
        ingest_url(args.url, posted=args.posted, dry_run=args.dry_run)

    elif args.url_file:
        ingest_url_file(args.url_file, dry_run=args.dry_run)

    # ── Step 3: Research stub companies ───────────────────────────────────────
    # --research-queue is preferred; --research-top is the inherited surface.
    # Both can be passed in the same run if you want to compare; in that case
    # --research-queue runs first.
    # --research-queue respects --dry-run (prints the queue without spending
    # API credits). --research-top skips entirely on --dry-run (inherited
    # behavior; do not change).
    if args.research_queue is not None:
        researched = research_queue(args.research_queue, dry_run=args.dry_run)
        if researched > 0 and not args.dry_run:
            print("\nCompany research complete — scores updated.")

    if args.research_top and not args.dry_run:
        researched = research_top_stubs(args.research_top, dry_run=args.dry_run)
        if researched > 0:
            print("\nCompany research complete — scores updated.")

    # ── Step 4: Cover letter generation ───────────────────────────────────────
    if args.cover_letters and not args.dry_run:
        generate_cover_letters(top_n=args.top, auto=args.auto_cl)

    # ── Step 5: Dashboard ─────────────────────────────────────────────────────
    if (args.dashboard or args.url or args.url_file
            or args.research_top or args.research_queue is not None
            or args.crawl):
        print()
        run_python("dashboard.py", "--top", str(args.top), "--stubs")


if __name__ == "__main__":
    main()
