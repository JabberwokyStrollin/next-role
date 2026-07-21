"""
scan_stale_jobs.py — Archive jobs that have sat un-applied in the pipeline too
long. A row whose ingest timestamp (``date_found``) is older than
``config.PIPELINE_EXPIRY_DAYS`` and that is still ``active`` /
``cover_letter_ready`` (i.e. never applied to) auto-archives with reason
``"stale_pipeline"``.

Why age-of-ingest, not ``date_posted``: ``date_posted`` is source-supplied and
often ``null``; ``date_found`` is always present and measures how long the row
has been *ours to act on*. This is distinct from ``config.STALENESS_TIERS``
(which key off ``date_posted`` and only nudge scoring) and from
``config.auto_age_application`` (which ages *applications*, not jobs). Applied
rows are deliberately never swept here — they age via ``auto_age_application``
(applied → ghosted → rejected).

The same logic runs automatically at the end of every real crawl via
``archive_stale_jobs`` (see ``crawl.crawl``); on a normal run it archives
whatever has crossed the expiry line since the last crawl.

Usage:
    python scripts/scan_stale_jobs.py                   # dry-run
    python scripts/scan_stale_jobs.py --apply           # archive matches
    python scripts/scan_stale_jobs.py --include-applied # also scan applied
"""

import argparse
import shutil
import sys
import uuid as uuid_lib

from config import (
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    PIPELINE_EXPIRY_DAYS,
    days_since,
    load_json,
    save_json,
    now_utc,
    today,
)

ARCHIVE_REASON = "stale_pipeline"
_DEFAULT_STATUSES = {"active", "cover_letter_ready"}


def _job_age_days(job: dict) -> int | None:
    """Days since the job entered the pipeline (``date_found``). Returns None
    when there's no parseable ingest timestamp, so such a row is never swept."""
    stamp = (job.get("date_found") or "")[:10]
    if not stamp:
        return None
    try:
        return days_since(stamp)
    except ValueError:
        return None


def is_stale(job: dict) -> bool:
    """SSOT predicate: the job has been in the pipeline longer than
    ``PIPELINE_EXPIRY_DAYS`` (measured from ``date_found``)."""
    age = _job_age_days(job)
    return age is not None and age > PIPELINE_EXPIRY_DAYS


def find_stale(jobs: list[dict], statuses: set[str]) -> list[dict]:
    """Return the in-scope jobs (status in ``statuses``) that are stale."""
    return [
        j for j in jobs
        if j.get("pipeline_status") in statuses and is_stale(j)
    ]


def archive_stale_jobs(apply: bool = True,
                       include_applied: bool = False,
                       verbose: bool = False) -> int:
    """Archive un-applied jobs older than ``PIPELINE_EXPIRY_DAYS`` in place.
    Returns the number archived (or that WOULD be, when ``apply=False``). Writes
    a ``.bak`` backup + ``job_archived`` process-log entries only when there's
    something to archive, so a no-op call touches nothing. Shared by the CLI and
    the crawl's end-of-run auto-sweep."""
    jobs     = load_json(JOB_PIPELINE_PATH)
    statuses = set(_DEFAULT_STATUSES) | ({"applied"} if include_applied else set())
    matches  = find_stale(jobs, statuses)
    if not matches:
        return 0

    if verbose:
        for j in matches:
            label = f"{(j.get('company_name') or '?')[:26]} -- {(j.get('title') or '?')[:34]}"
            print(f"  [{j.get('pipeline_status','?'):18}] {label}")
            print(f"      age: {_job_age_days(j)}d  (date_found {j.get('date_found','?')})")

    if not apply:
        return len(matches)

    backup = JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak")
    shutil.copyfile(JOB_PIPELINE_PATH, backup)

    log = load_json(PROCESS_LOG_PATH)
    now = now_utc()
    for j in matches:
        age = _job_age_days(j)
        j["pipeline_status"] = "archived"
        j["archived_at"]     = now
        j["archived_reason"] = ARCHIVE_REASON
        log.append({
            "log_id":       str(uuid_lib.uuid4()),
            "timestamp":    now,
            "session_date": today(),
            "event_type":   "job_archived",
            "entity_type":  "job",
            "entity_id":    j.get("job_id"),
            "entity_name":  f"{j.get('company_name','?')} -- {j.get('title','?')}",
            "source_url":   j.get("apply_url"),
            "detail":       f"Expired from pipeline: {age}d un-applied "
                            f"(> {PIPELINE_EXPIRY_DAYS}d since ingest).",
        })

    save_json(JOB_PIPELINE_PATH, jobs)
    save_json(PROCESS_LOG_PATH, log)
    return len(matches)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Archive jobs that have sat un-applied in the pipeline "
                    f"longer than {PIPELINE_EXPIRY_DAYS} days.")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Default is dry-run.")
    p.add_argument("--include-applied", action="store_true",
                   help="Also scan applied jobs (default: active + cover_letter_ready).")
    args = p.parse_args()

    preview = archive_stale_jobs(apply=False,
                                 include_applied=args.include_applied,
                                 verbose=True)
    if preview == 0:
        print("No stale jobs found.")
        return 0
    if not args.apply:
        print(f"\nDry run -- pass --apply to archive these {preview} job(s).")
        return 0

    n = archive_stale_jobs(apply=True, include_applied=args.include_applied)
    print(f"\nArchived {n} stale job(s). "
          f"Backup at {JOB_PIPELINE_PATH.name}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
