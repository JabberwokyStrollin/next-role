"""
scan_no_sponsorship.py — Retroactively archive jobs whose JD explicitly
refuses visa sponsorship.

Runs ``detect_no_sponsorship`` over every job in ``data/job_pipeline.json``.
In dry-run mode (default) it prints what would be archived. With ``--apply``
it flips matching rows to ``pipeline_status="archived"`` and logs each one
to ``process_log.json``.

This is a one-off cleanup script for jobs ingested before the no-sponsorship
filter was added to ``ingest.py``. New ingests pass through the same check
automatically — no need to re-run this routinely.

Usage:
    python scripts/scan_no_sponsorship.py                       # dry-run
    python scripts/scan_no_sponsorship.py --apply               # archive matches
    python scripts/scan_no_sponsorship.py --include-applied     # also scan applied
"""

import argparse
import shutil
import sys
import uuid as uuid_lib

from config import (
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    detect_no_sponsorship,
    load_json,
    save_json,
    now_utc,
    today,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Archive jobs whose JD refuses sponsorship.")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Default is dry-run.")
    p.add_argument("--include-applied", action="store_true",
                   help="Also scan applied jobs (default: active + cover_letter_ready only).")
    args = p.parse_args()

    jobs = load_json(JOB_PIPELINE_PATH)
    if not jobs:
        print("No jobs in pipeline.")
        return 0

    statuses = {"active", "cover_letter_ready"}
    if args.include_applied:
        statuses.add("applied")

    matches: list[tuple[dict, str]] = []
    for job in jobs:
        if job.get("pipeline_status") not in statuses:
            continue
        snippet = detect_no_sponsorship(job.get("jd_text") or "")
        if snippet:
            matches.append((job, snippet))

    if not matches:
        print(f"Scanned {sum(1 for j in jobs if j.get('pipeline_status') in statuses)} "
              f"jobs; no no-sponsorship language found.")
        return 0

    print(f"Found {len(matches)} job(s) with no-sponsorship language:\n")
    for job, snippet in matches:
        label  = f"{(job.get('company_name') or '?')[:28]} -- {(job.get('title') or '?')[:38]}"
        status = job.get("pipeline_status", "?")
        print(f"  [{status:<19}] {label}")
        print(f"      snippet: ...{snippet}...")

    if not args.apply:
        print(f"\nDry run -- pass --apply to archive these {len(matches)} jobs.")
        return 0

    backup_path = JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak")
    shutil.copyfile(JOB_PIPELINE_PATH, backup_path)
    print(f"\nBacked up pipeline to: {backup_path}")

    log = load_json(PROCESS_LOG_PATH)
    now = now_utc()
    for job, snippet in matches:
        job["pipeline_status"] = "archived"
        job["archived_at"]     = now
        job["archived_reason"] = "JD says no sponsorship"
        log.append({
            "log_id":       str(uuid_lib.uuid4()),
            "timestamp":    now,
            "session_date": today(),
            "event_type":   "job_archived",
            "entity_type":  "job",
            "entity_id":    job.get("job_id"),
            "entity_name":  f"{job.get('company_name','?')} -- {job.get('title','?')}",
            "source_url":   job.get("apply_url"),
            "detail":       f"Retroactive archive: JD says no sponsorship (\"...{snippet}...\").",
        })

    save_json(JOB_PIPELINE_PATH, jobs)
    save_json(PROCESS_LOG_PATH, log)
    print(f"\nArchived {len(matches)} job(s).")
    print(f"Backup retained at: {backup_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
