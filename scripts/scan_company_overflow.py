"""
scan_company_overflow.py — Cap how many ACTIVE rows any one company holds in the
pipeline, keeping its best and archiving the overflow.

A crawl that widens a gate can dump a single company's whole board into the
pipeline at once — one run put 228 Databricks rows in, leaving that company
holding 19% of every active row. Steady-state intake is only a handful per
company per crawl, so this is a stock problem, not a flow problem: the fix is to
bound the standing inventory rather than to block intake.

What "cycle out older / lower fit" means here: rows are ranked by the full
``composite_score`` and everything past ``MAX_ACTIVE_JOBS_PER_COMPANY`` is
archived. Age needs no separate rule — velocity (10) and freshness (8) are
already 18 of the composite's 130 points, so a stale posting sinks on its own.
Ranking by composite also avoids the failure mode of an explicit age rule, which
would let a fresh weak posting evict an older strong one.

Cycling is a consequence of re-running, not of any bookkeeping: as a company's
top rows are applied to or expire, its slots free up and the next crawl's
arrivals take them.

NOT a company throttle. The apply-time rule (``config.company_block_reason``,
``MAX_ACTIVE_APPS_PER_COMPANY``) is untouched and remains the only thing that
decides whether a company can be applied to — see the Company-filter SSOT in
CLAUDE.md. This bounds stored inventory; that bounds in-flight applications.

Usage:
    python scripts/scan_company_overflow.py                   # dry-run
    python scripts/scan_company_overflow.py --apply           # archive overflow
    python scripts/scan_company_overflow.py --cap 25          # override the cap
    python scripts/scan_company_overflow.py --include-ready   # also cover_letter_ready
"""

import argparse
import shutil
import sys
import uuid as uuid_lib
from collections import defaultdict

from config import (
    COMPANY_REGISTRY_PATH,
    JOB_PIPELINE_PATH,
    MAX_ACTIVE_JOBS_PER_COMPANY,
    PROCESS_LOG_PATH,
    composite_score,
    load_json,
    save_json,
    now_utc,
    today,
)

ARCHIVE_REASON = "company inventory cap (kept top {cap} by composite)"
_DEFAULT_STATUSES = {"active"}


def find_overflow(jobs: list[dict],
                  statuses: set[str],
                  cap: int,
                  co_by_id: dict) -> list[dict]:
    """Return the in-scope rows beyond each company's ``cap``, lowest composite
    first. A company at or under the cap contributes nothing."""
    by_company: dict = defaultdict(list)
    for j in jobs:
        if j.get("pipeline_status") in statuses:
            by_company[j.get("company_id")].append(j)

    overflow: list[dict] = []
    for _cid, rows in by_company.items():
        if len(rows) <= cap:
            continue
        ranked = sorted(
            rows,
            key=lambda j: composite_score(j, co_by_id.get(j.get("company_id"))),
            reverse=True,
        )
        overflow.extend(ranked[cap:])
    return overflow


def archive_company_overflow(apply: bool = True,
                             cap: int | None = None,
                             include_ready: bool = False,
                             verbose: bool = False) -> int:
    """Archive per-company overflow in place. Returns the number archived (or
    that WOULD be, when ``apply=False``). Writes a ``.bak`` backup +
    ``job_archived`` log entries only when there's something to archive, so a
    no-op call touches nothing. Shared by the CLI and the crawl's end-of-run
    auto-sweep."""
    cap      = MAX_ACTIVE_JOBS_PER_COMPANY if cap is None else cap
    jobs     = load_json(JOB_PIPELINE_PATH)
    co_by_id = {c["company_id"]: c for c in load_json(COMPANY_REGISTRY_PATH)}
    statuses = set(_DEFAULT_STATUSES) | ({"cover_letter_ready"} if include_ready else set())
    matches  = find_overflow(jobs, statuses, cap, co_by_id)
    if not matches:
        return 0

    if verbose:
        per_co: dict = defaultdict(int)
        for j in matches:
            per_co[j.get("company_name") or "?"] += 1
        for name, n in sorted(per_co.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5} over cap  {name[:40]}")

    if not apply:
        return len(matches)

    backup = JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak")
    shutil.copyfile(JOB_PIPELINE_PATH, backup)

    log    = load_json(PROCESS_LOG_PATH)
    now    = now_utc()
    reason = ARCHIVE_REASON.format(cap=cap)
    for j in matches:
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
            "detail":       f"Retroactive archive: {reason}.",
        })

    save_json(JOB_PIPELINE_PATH, jobs)
    save_json(PROCESS_LOG_PATH, log)
    return len(matches)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Archive per-company pipeline overflow beyond the inventory cap.")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Default is dry-run.")
    p.add_argument("--cap", type=int, default=None,
                   help=f"Override MAX_ACTIVE_JOBS_PER_COMPANY "
                        f"(default {MAX_ACTIVE_JOBS_PER_COMPANY}).")
    p.add_argument("--include-ready", action="store_true",
                   help="Also sweep cover_letter_ready rows (default: active only).")
    args = p.parse_args()

    cap     = MAX_ACTIVE_JOBS_PER_COMPANY if args.cap is None else args.cap
    preview = archive_company_overflow(apply=False, cap=cap,
                                       include_ready=args.include_ready, verbose=True)
    if preview == 0:
        print(f"No company holds more than {cap} active row(s).")
        return 0
    if not args.apply:
        print(f"\nDry run -- pass --apply to archive these {preview} row(s).")
        return 0

    n = archive_company_overflow(apply=True, cap=cap, include_ready=args.include_ready)
    print(f"\nArchived {n} overflow row(s) at cap {cap}. "
          f"Backup at {JOB_PIPELINE_PATH.name}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
