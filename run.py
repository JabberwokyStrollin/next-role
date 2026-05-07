"""
run.py — Main entry point for the next-role pipeline.

Daily workflow:
  1. Ingest jobs from URLs or paste mode
  2. Run dashboard to show current pipeline
  3. Trigger company research for top-N jobs with stub companies
  4. Re-rank and show final top 5

Usage:
    # Ingest a single job then show dashboard
    python run.py --url "https://boards.greenhouse.io/company/jobs/123"

    # Ingest multiple jobs from a file (one URL per line)
    python run.py --url-file urls.txt

    # Just show dashboard (no ingest)
    python run.py --dashboard

    # Research top N stub companies and re-rank
    python run.py --research-top 5

    # Full daily run: ingest from file + research top 5 + dashboard
    python run.py --url-file urls.txt --research-top 5

    # Dry run: ingest and score but don't research companies
    python run.py --url-file urls.txt --dry-run
"""

import argparse
import subprocess
import sys
import os
from pathlib import Path

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


def load_json(path):
    import json
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def composite_score(job, company):
    return (
        (job.get("stack_match_score")     or 0) +
        (job.get("seniority_score")       or 0) +
        (job.get("domain_fit_score")      or 0) +
        (job.get("hiring_velocity_score") or 0) +
        ((company.get("sponsorship_score") if company else None) or 0) +
        ((company.get("remote_fit")        if company else None) or 0)
    )


def top_per_company(scored):
    """Given (score, job) pairs sorted desc, keep only the top job per company_id.
    Prevents spamming a single company with multiple simultaneous applications."""
    seen = set()
    out  = []
    for score, job in scored:
        cid = job.get("company_id")
        if cid in seen:
            continue
        seen.add(cid)
        out.append((score, job))
    return out


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


# ── Top-N company research ────────────────────────────────────────────────────

def research_top_stubs(n: int, dry_run: bool = False) -> int:
    """
    Find top-N active jobs whose company is a stub, research those companies
    using Haiku (cheap), then update composite scores.
    Returns count of companies researched.
    """
    jobs      = load_json(DATA_DIR / "job_pipeline.json")
    companies = load_json(DATA_DIR / "company_registry.json")
    co_by_id  = {c["company_id"]: c for c in companies}

    # Score all active jobs
    active = [j for j in jobs if j.get("pipeline_status") not in ["archived", "applied"]]
    scored = []
    for job in active:
        company = co_by_id.get(job.get("company_id"))
        score   = composite_score(job, company)
        scored.append((score, job, company))
    scored.sort(key=lambda x: x[0], reverse=True)

    # Find stub companies in top N
    stubs_to_research = []
    seen_companies    = set()
    for score, job, company in scored[:max(n * 3, 15)]:  # look beyond top N to find stubs
        if len(stubs_to_research) >= n:
            break
        if not company or not company.get("stub"):
            continue
        cid = company["company_id"]
        if cid in seen_companies:
            continue
        seen_companies.add(cid)
        stubs_to_research.append((score, job, company))

    if not stubs_to_research:
        print("\nNo stub companies in top jobs — all companies already researched.")
        return 0

    print(f"\nResearching {len(stubs_to_research)} stub companies for top-ranked jobs...")
    researched = 0

    for score, job, company in stubs_to_research:
        name = company["name"]
        print(f"\n  [{researched+1}/{len(stubs_to_research)}] {name} (job score before research: {score})")

        if dry_run:
            print("  Dry run — skipping research.")
            researched += 1
            continue

        rc = run_python("research_company.py", "--name", name)
        if rc == 0:
            # Clear stub flag now that research is done
            companies = load_json(DATA_DIR / "company_registry.json")
            updated   = []
            for c in companies:
                if c["name"].lower() == name.lower():
                    c.pop("stub", None)
                updated.append(c)
            save_json(DATA_DIR / "company_registry.json", updated)
            researched += 1
        else:
            print(f"  Warning: research failed for {name}")

    print(f"\nResearched {researched} companies.")
    return researched


# ── Cover letter generation ───────────────────────────────────────────────────

def generate_cover_letters(top_n: int = 5, auto: bool = False) -> None:
    """
    Offer to generate cover letters for top-N cover_letter_ready or active jobs.
    """
    jobs      = load_json(DATA_DIR / "job_pipeline.json")
    companies = load_json(DATA_DIR / "company_registry.json")
    co_by_id  = {c["company_id"]: c for c in companies}

    eligible = [
        j for j in jobs
        if j.get("pipeline_status") in ["active", "cover_letter_ready"]
        and not j.get("cover_letter_generated")
    ]

    scored = []
    for job in eligible:
        company = co_by_id.get(job.get("company_id"))
        score   = composite_score(job, company)
        scored.append((score, job))
    scored.sort(key=lambda x: x[0], reverse=True)

    deduped    = top_per_company(scored)
    candidates = deduped[:top_n]
    if not candidates:
        print("\nNo jobs ready for cover letter generation.")
        return

    suppressed = len(scored) - len(deduped)
    print(f"\n── Cover letter candidates (top {top_n}, one per company) ──────────")
    if suppressed:
        print(f"  ({suppressed} lower-scoring sibling job{'s' if suppressed != 1 else ''} suppressed at same companies)")
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
        country = "IE" if "ireland" in job.get("location", "").lower() else "CA"
        print(f"\nGenerating: {job['company_name']} — {job['title']}")
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
    parser.add_argument("--research-top", type=int, metavar="N",
                        help="Research top N stub companies after ingest")
    parser.add_argument("--cover-letters", action="store_true",
                        help="Offer cover letter generation for top 5 after research")
    parser.add_argument("--auto-cl", action="store_true",
                        help="Generate cover letters for top N without prompting (implies --cover-letters)")
    parser.add_argument("--crawl", action="store_true",
                        help="Crawl configured job boards and aggregators for new jobs")
    parser.add_argument("--dashboard", action="store_true",
                        help="Show pipeline dashboard")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and score but skip all API calls")
    parser.add_argument("--top", type=int, default=5, metavar="N",
                        help="Number of top jobs to show/consider (default 5)")

    args = parser.parse_args()

    # --auto-cl implies --cover-letters
    if args.auto_cl:
        args.cover_letters = True

    # If no action specified, show dashboard
    if not any([args.url, args.url_file, args.research_top,
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

    # ── Step 2: Research top stub companies ───────────────────────────────────
    if args.research_top and not args.dry_run:
        researched = research_top_stubs(args.research_top, dry_run=args.dry_run)
        if researched > 0:
            print("\nCompany research complete — scores updated.")

    # ── Step 3: Cover letter generation ──────────────────────────────────────
    if args.cover_letters and not args.dry_run:
        generate_cover_letters(top_n=args.top, auto=args.auto_cl)

    # ── Step 4: Dashboard ─────────────────────────────────────────────────────
    if args.dashboard or args.url or args.url_file or args.research_top or args.crawl:
        print()
        run_python("dashboard.py", "--top", str(args.top), "--stubs")


if __name__ == "__main__":
    main()
