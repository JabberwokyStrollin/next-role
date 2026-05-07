"""
dashboard.py — Print a ranked pipeline summary. No Claude, no API calls.

Usage:
    python scripts/dashboard.py
    python scripts/dashboard.py --top 10
    python scripts/dashboard.py --all        # include archived
"""

import argparse

from config import (
    JOB_PIPELINE_PATH,
    COMPANY_REGISTRY_PATH,
    APPLICATION_TRACKER_PATH,
    load_json,
    today,
    composite_score,
    GHOSTED_DAYS,
    days_since,
)

# ── Staleness colors (terminal) ───────────────────────────────────────────────

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
BOLD   = "\033[1m"
BLUE   = "\033[94m"

STALE_COLOR = {
    "fresh":      GREEN,
    "soft_stale": YELLOW,
    "hard_stale": RED,
    "dead_link":  RED,
    "archived":   GRAY,
}

STATUS_COLOR = {
    "active":             GREEN,
    "cover_letter_ready": BLUE,
    "applied":            YELLOW,
    "archived":           GRAY,
}


def color(text, c):
    return f"{c}{text}{RESET}"


def hyperlink(url: str) -> str:
    """OSC 8 clickable hyperlink — supported in Windows Terminal, iTerm2, etc."""
    if not url or url == "N/A":
        return url or "N/A"
    return f"\033]8;;{url}\033\\{url}\033]8;;\033\\"


def score_bar(value, max_val=105, width=12):
    filled = round((value / max_val) * width)
    bar    = "█" * filled + "░" * (width - filled)
    if value >= 70:
        c = GREEN
    elif value >= 50:
        c = BLUE
    elif value >= 30:
        c = YELLOW
    else:
        c = RED
    return f"{c}{bar}{RESET} {value:>3}"


def main():
    parser = argparse.ArgumentParser(description="Job pipeline dashboard.")
    parser.add_argument("--top",  type=int, default=10, metavar="N",
                        help="Show top N jobs (default 10)")
    parser.add_argument("--all",  action="store_true",
                        help="Include archived jobs")
    parser.add_argument("--stubs", action="store_true",
                        help="Show stub company flag")
    args = parser.parse_args()

    jobs      = load_json(JOB_PIPELINE_PATH)
    companies = load_json(COMPANY_REGISTRY_PATH)
    apps      = load_json(APPLICATION_TRACKER_PATH)

    # ── Build company lookup ──────────────────────────────────────────────────
    co_by_id = {c["company_id"]: c for c in companies}

    # ── Filter ────────────────────────────────────────────────────────────────
    if not args.all:
        jobs = [j for j in jobs if j.get("pipeline_status") != "archived"]

    # ── Score and sort ────────────────────────────────────────────────────────
    scored = []
    for job in jobs:
        company = co_by_id.get(job.get("company_id"))
        score   = composite_score(job, company)
        scored.append((score, job, company))
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── Stats ─────────────────────────────────────────────────────────────────
    active_count   = sum(1 for j in jobs if j.get("pipeline_status") in ["active", "cover_letter_ready"])
    applied_count  = len(apps)
    ghosted_count  = sum(1 for a in apps if a.get("ghosted_flag"))
    stub_count     = sum(1 for c in companies if c.get("stub"))

    print(f"\n{BOLD}── Job Search Pipeline ─────────────────────────────────────────{RESET}")
    print(f"  Date:        {today()}")
    print(f"  Active jobs: {active_count}")
    print(f"  Applied:     {applied_count}  (ghosted: {ghosted_count})")
    print(f"  Companies:   {len(companies)}  (stubs pending research: {stub_count})")
    print()

    # ── Top jobs table ────────────────────────────────────────────────────────
    display = scored[:args.top]
    if not display:
        print("  No jobs in pipeline.")
        return

    print(f"{BOLD}{'#':<3} {'Company':<22} {'Title':<32} {'Score':<18} {'Stale':<12} {'Status'}{RESET}")
    print("─" * 105)

    for i, (score, job, company) in enumerate(display, 1):
        stale   = job.get("staleness_status", "unknown")
        status  = job.get("pipeline_status", "unknown")
        sc      = STALE_COLOR.get(stale, GRAY)
        stc     = STATUS_COLOR.get(status, GRAY)
        stub    = " [stub]" if (args.stubs and company and company.get("stub")) else ""
        title   = job["title"][:31]

        print(
            f"{i:<3} "
            f"{job['company_name'][:21]:<22} "
            f"{title:<32} "
            f"{score_bar(score):<28} "
            f"{color(stale, sc):<22} "
            f"{color(status, stc)}{stub}"
        )

    print()

    # ── Score breakdown for top 3 ────────────────────────────────────────────
    print(f"{BOLD}── Score breakdown (top 3) ─────────────────────────────────────{RESET}")
    for i, (score, job, company) in enumerate(display[:3], 1):
        stub_note = " [stub — company not yet researched]" if (company and company.get("stub")) else ""
        print(f"\n  {i}. {job['company_name']} — {job['title']}{stub_note}")
        print(f"     Stack:      {job.get('stack_match_score') or 0:>3}/35")
        print(f"     Seniority:  {job.get('seniority_score') or 0:>3}/25")
        print(f"     Domain:     {job.get('domain_fit_score') or 0:>3}/20")
        print(f"     Velocity:   {job.get('hiring_velocity_score') or 0:>3}/5")
        print(f"     Sponsorship:{(company.get('sponsorship_score') if company else 0) or 0:>3}/15")
        print(f"     Remote fit: {(company.get('remote_fit') if company else 0) or 0:>3}/5")
        print(f"     {'─'*22}")
        print(f"     Composite:  {score:>3}/105")
        if job.get("score_notes"):
            print(f"     Notes: {job['score_notes'][:120]}...")
        print(f"     Apply:  {hyperlink(job.get('apply_url', 'N/A'))}")

    # ── Applications summary ──────────────────────────────────────────────────
    if apps:
        print(f"\n{BOLD}── Applications ────────────────────────────────────────────────{RESET}")
        for a in sorted(apps, key=lambda x: x.get("date_applied",""), reverse=True):
            ghost = color(" [GHOSTED]", RED) if a.get("ghosted_flag") else ""
            print(f"  {a['date_applied']}  {a['company_name']:<22} {a['status']:<18}{ghost}")

    print()


if __name__ == "__main__":
    main()
