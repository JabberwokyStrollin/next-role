"""
backfill_target_boards.py — discover ATS boards from existing pipeline entries.

ingest_job() and the crawler auto-add ATS boards when an apply URL matches one
of the known patterns (Greenhouse, Lever, Ashby, Workday, SmartRecruiters).
Earlier ingests pre-date that wiring, so jobs already in job_pipeline.json
contributed nothing to target_boards.json. This script runs the same
detect_ats() pass over every pipeline entry and adds any new boards in one
batched write.

Safe to re-run — existing (ats, slug) pairs are deduped against target_boards.

Usage:
    python scripts/backfill_target_boards.py --dry-run   # report only
    python scripts/backfill_target_boards.py             # apply
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    JOB_PIPELINE_PATH,
    TARGET_BOARDS_PATH,
    load_json,
    save_json,
    today,
)
from crawl import detect_ats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover ATS boards from existing job_pipeline.json entries."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be added without writing.")
    args = parser.parse_args()

    jobs   = load_json(JOB_PIPELINE_PATH)
    boards = load_json(TARGET_BOARDS_PATH)

    already = {(b.get("ats", "").lower(), b.get("slug", "")) for b in boards}

    # Group discoveries by (ats, slug); keep company name + a couple example URLs.
    discovered: dict = {}
    for j in jobs:
        url = j.get("apply_url", "")
        if not url:
            continue
        info = detect_ats(url)
        if not info:
            continue
        ats, slug = info
        key = (ats, slug)
        if key in already:
            continue
        if key not in discovered:
            discovered[key] = {
                "company":  j.get("company_name", "?"),
                "examples": [],
            }
        if len(discovered[key]["examples"]) < 2:
            discovered[key]["examples"].append(url)

    print(f"Scanned {len(jobs)} positions in pipeline.")
    print(f"Already in target_boards.json:  {len(boards):>3} board(s).")
    print(f"New boards discovered:           {len(discovered):>3}.")
    print()

    if not discovered:
        return

    by_ats: dict = defaultdict(list)
    for (ats, slug), meta in discovered.items():
        by_ats[ats].append((slug, meta["company"]))

    for ats in sorted(by_ats):
        rows = by_ats[ats]
        print(f"{ats.upper()} ({len(rows)}):")
        for slug, company in sorted(rows, key=lambda r: r[1].lower()):
            print(f"  + {company:<30}  {slug}")
        print()

    if args.dry_run:
        print("Dry run — no changes written.")
        return

    iso = today()
    new_entries = [
        {
            "company":   meta["company"],
            "ats":       ats,
            "slug":      slug,
            "added":     iso,
            "added_via": "backfill_pipeline",
        }
        for (ats, slug), meta in discovered.items()
    ]

    boards.extend(new_entries)
    save_json(TARGET_BOARDS_PATH, boards)
    print(f"Wrote {len(new_entries)} new board(s) to {TARGET_BOARDS_PATH.relative_to(ROOT)}.")


if __name__ == "__main__":
    main()
