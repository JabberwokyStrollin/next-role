"""
scan_geography_policy.py -- Retroactively archive jobs that the CURRENT
geography / work-model policy would no longer admit.

New ingests are already gated (``config.location_passes`` and
``config.work_model_discard_reason`` both run in ``ingest.ingest_job``), so this
script exists for the retroactive half of a policy change:

  1. You removed a country from ``geography.TARGET_COUNTRIES``. Rows already in
     the pipeline keep flowing to the apply queue until swept -- the gate blocks
     future ingest, not existing inventory.
  2. You narrowed ``config.US_ACCEPTED_WORK_MODELS``. Same story for US rows
     whose stored ``job_type`` is no longer accepted.

Run it as PART OF the policy change, not as follow-up -- the same obligation
``scan_no_sponsorship.py`` and ``scan_foreign_locations.py`` carry. Written for
the 2026-10-03 pivot (CA dropped; US narrowed to confirmed-remote), but the
predicates read the live config, so it stays correct for later changes.

SCOPE -- deliberately narrow. Two rules only:

  * **Country disabled.** ``derive_country(location)`` is a specific country
    code (CA / IE / US) that is NOT in ``TARGET_COUNTRIES``.
  * **Work model rejected.** ``work_model_discard_reason(location, job_type)``
    fires, i.e. a US row whose stored work model left
    ``US_ACCEPTED_WORK_MODELS``.

It does NOT re-run the full ``location_passes`` gate. That gate's US branch
falls back to the strict "explicit remote marker in the location" rule when no
``jd_text`` is supplied, and this script has no JD bodies to hand it -- so
running it here would archive US rows that were admitted on the strength of
their JD, which is a policy this script was never asked to reverse. OTHER-
derived rows are likewise left alone: they're ``scan_foreign_locations.py``'s
job, and "OTHER" is not a country being toggled off.

Default is a DRY RUN. Nothing is touched until ``--apply``.

Usage:
    python scripts/scan_geography_policy.py                   # dry run
    python scripts/scan_geography_policy.py --apply
    python scripts/scan_geography_policy.py --include-applied
"""

import argparse
import shutil
import sys
import uuid as uuid_lib
from collections import Counter

from config import (
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    TARGET_COUNTRIES,
    derive_country,
    work_model_discard_reason,
    load_json,
    save_json,
    now_utc,
    today,
)

# Country codes this script is willing to archive on. "OTHER" is excluded on
# purpose -- it is not a country being toggled, and foreign-pinned OTHER rows
# belong to scan_foreign_locations.py.
_TOGGLEABLE = ("CA", "IE", "US")

_DEFAULT_STATUSES = {"active", "cover_letter_ready"}


def policy_reject_reason(job: dict) -> str | None:
    """SSOT predicate: why the current policy would reject this row, or None.

    Reads ``TARGET_COUNTRIES`` and ``work_model_discard_reason`` live rather
    than hardcoding the 2026-10-03 values, so re-running after a later toggle
    sweeps whatever is disabled *then*."""
    location = job.get("location", "") or ""
    country  = derive_country(location)

    if country in _TOGGLEABLE and country not in TARGET_COUNTRIES:
        return f"{country} is no longer a target geography"

    # Reuses the ingest-time gate verbatim. Returns None for non-US rows and
    # for US rows whose work model is still accepted.
    reason = work_model_discard_reason(location, job.get("job_type") or "unstated")
    if reason:
        return reason
    return None


def find_rejects(jobs: list[dict], statuses: set[str]) -> list[tuple[dict, str]]:
    """Return (job, reason) for every in-scope row the current policy rejects."""
    out = []
    for j in jobs:
        if j.get("pipeline_status") not in statuses:
            continue
        reason = policy_reject_reason(j)
        if reason:
            out.append((j, reason))
    return out


def archive_policy_rejects(apply: bool = True,
                           include_applied: bool = False,
                           verbose: bool = False) -> int:
    """Archive rows the current geography / work-model policy rejects. Returns
    the count archived (or that WOULD be, when ``apply=False``). Writes a
    ``.bak`` backup + ``job_archived`` process-log entries only when there is
    something to archive, so a no-op run touches nothing."""
    jobs     = load_json(JOB_PIPELINE_PATH)
    statuses = set(_DEFAULT_STATUSES) | ({"applied"} if include_applied else set())
    matches  = find_rejects(jobs, statuses)
    if not matches:
        return 0

    if verbose:
        by_reason = Counter(r for _, r in matches)
        for reason, n in by_reason.most_common():
            print(f"  {n:>4}  {reason}")
        print()
        for j, reason in matches[:15]:
            label = f"{(j.get('company_name') or '?')[:26]} -- {(j.get('title') or '?')[:34]}"
            print(f"  [{j.get('pipeline_status','?'):18}] {label}")
            print(f"      location: {(j.get('location') or '')!r}  ->  {reason}")
        if len(matches) > 15:
            print(f"  ... and {len(matches) - 15} more")

    if not apply:
        return len(matches)

    backup = JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak")
    shutil.copyfile(JOB_PIPELINE_PATH, backup)

    log = load_json(PROCESS_LOG_PATH)
    now = now_utc()
    for j, reason in matches:
        j["pipeline_status"] = "archived"
        j["archived_at"]     = now
        j["archived_reason"] = reason
        log.append({
            "log_id":       str(uuid_lib.uuid4()),
            "timestamp":    now,
            "session_date": today(),
            "event_type":   "job_archived",
            "entity_type":  "job",
            "entity_id":    j.get("job_id"),
            "entity_name":  f"{j.get('company_name','?')} -- {j.get('title','?')}",
            "source_url":   j.get("apply_url"),
            "detail":       f"Retroactive archive (geography policy): {reason}. "
                            f"Location '{(j.get('location') or '')[:50]}'.",
        })

    save_json(JOB_PIPELINE_PATH, jobs)
    save_json(PROCESS_LOG_PATH, log)
    return len(matches)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Archive jobs the current geography / work-model policy rejects.")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Default is dry-run.")
    p.add_argument("--include-applied", action="store_true",
                   help="Also scan applied jobs (default: active + cover_letter_ready).")
    args = p.parse_args()

    print(f"Target geographies : {', '.join(sorted(TARGET_COUNTRIES))}")
    preview = archive_policy_rejects(apply=False,
                                     include_applied=args.include_applied,
                                     verbose=True)
    if preview == 0:
        print("No policy-rejected jobs found.")
        return 0
    if not args.apply:
        print(f"\nDry run -- pass --apply to archive these {preview} job(s).")
        return 0

    n = archive_policy_rejects(apply=True, include_applied=args.include_applied)
    print(f"\nArchived {n} policy-rejected job(s). "
          f"Backup at {JOB_PIPELINE_PATH.name}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
