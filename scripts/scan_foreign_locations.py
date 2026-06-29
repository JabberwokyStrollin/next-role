"""
scan_foreign_locations.py — Retroactively archive jobs whose location is pinned
to a foreign region the operator can't work in ("Remote - India", "European
Union (Remote)", "Berlin, Germany", …).

New ingests are already blocked at the gate (``config.location_passes`` rejects
foreign-pinned rows in ``ingest.ingest_job``), so this script exists only for:
  1. Re-sweeping the pipeline after you EXPAND ``config._FOREIGN_LOCATION_TOKENS``
     (newly-denied regions won't retroactively archive themselves).
  2. The odd manually-pasted row.

The same logic is run automatically at the end of every real crawl via
``archive_foreign_pinned`` (see ``crawl.crawl``); on a normal run it finds zero.

Matching SSOT: a job is foreign-pinned iff
``derive_country(location) == "OTHER"`` AND ``names_foreign_location(location)``
— exactly the rows ``location_passes`` rejects on the OTHER branch.

Usage:
    python scripts/scan_foreign_locations.py                   # dry-run
    python scripts/scan_foreign_locations.py --apply           # archive matches
    python scripts/scan_foreign_locations.py --include-applied # also scan applied
"""

import argparse
import shutil
import sys
import uuid as uuid_lib

from config import (
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    derive_country,
    names_foreign_location,
    load_json,
    save_json,
    now_utc,
    today,
)

ARCHIVE_REASON = "foreign-pinned remote (not an eligible geography)"
_DEFAULT_STATUSES = {"active", "cover_letter_ready"}


def is_foreign_pinned(location: str) -> bool:
    """SSOT predicate: the location resolves to OTHER and names a non-target
    region (mirrors ``location_passes``'s OTHER-branch rejection)."""
    loc = location or ""
    return derive_country(loc) == "OTHER" and names_foreign_location(loc)


def find_foreign(jobs: list[dict], statuses: set[str]) -> list[dict]:
    """Return the in-scope jobs whose location is foreign-pinned."""
    return [
        j for j in jobs
        if j.get("pipeline_status") in statuses
        and is_foreign_pinned(j.get("location", "") or "")
    ]


def archive_foreign_pinned(apply: bool = True,
                           include_applied: bool = False,
                           verbose: bool = False) -> int:
    """Archive active foreign-pinned jobs in place. Returns the number archived
    (or that WOULD be archived when ``apply=False``). Writes a ``.bak`` backup +
    ``job_archived`` process-log entries only when there's something to archive,
    so a no-op call (the common case) touches nothing. Shared by the CLI and the
    crawl's end-of-run auto-sweep."""
    jobs     = load_json(JOB_PIPELINE_PATH)
    statuses = set(_DEFAULT_STATUSES) | ({"applied"} if include_applied else set())
    matches  = find_foreign(jobs, statuses)
    if not matches:
        return 0

    if verbose:
        for j in matches:
            label = f"{(j.get('company_name') or '?')[:26]} -- {(j.get('title') or '?')[:34]}"
            print(f"  [{j.get('pipeline_status','?'):18}] {label}")
            print(f"      location: {j.get('location','')!r}")

    if not apply:
        return len(matches)

    backup = JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak")
    shutil.copyfile(JOB_PIPELINE_PATH, backup)

    log = load_json(PROCESS_LOG_PATH)
    now = now_utc()
    for j in matches:
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
            "detail":       f"Retroactive archive: foreign-pinned remote location "
                            f"'{(j.get('location') or '')[:50]}'.",
        })

    save_json(JOB_PIPELINE_PATH, jobs)
    save_json(PROCESS_LOG_PATH, log)
    return len(matches)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Archive jobs pinned to a foreign (non-target) region.")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Default is dry-run.")
    p.add_argument("--include-applied", action="store_true",
                   help="Also scan applied jobs (default: active + cover_letter_ready).")
    args = p.parse_args()

    preview = archive_foreign_pinned(apply=False,
                                     include_applied=args.include_applied,
                                     verbose=True)
    if preview == 0:
        print("No foreign-pinned jobs found.")
        return 0
    if not args.apply:
        print(f"\nDry run -- pass --apply to archive these {preview} job(s).")
        return 0

    n = archive_foreign_pinned(apply=True, include_applied=args.include_applied)
    print(f"\nArchived {n} foreign-pinned job(s). "
          f"Backup at {JOB_PIPELINE_PATH.name}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
