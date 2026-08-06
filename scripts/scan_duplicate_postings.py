"""
scan_duplicate_postings.py — Collapse the same role posted many times, keeping
the best copy and archiving the rest.

Employers routinely post one opening once per office: Databricks lists the same
"Sr. Forward Deployed Engineer (FDE) - Communications" across 14 US cities, each
with its own apply URL. ``ingest.check_duplicate`` matches on ``apply_url``, so
it never sees these as duplicates and all 14 enter the pipeline — 20% of active
rows are redundant this way.

That was mostly cosmetic until the apply queue grew country quotas. Now it costs
real slots: with ``APPLY_QUEUE_MAX_PER_COMPANY`` at 2, two copies of one role can
occupy both of a company's places in a country's allocation, and archiving a
dead posting can bubble up *the same posting again* — which is exactly the loop
the operator was hitting by hand.

**The dedup key includes the derived country**, not just company + title. Two
copies in the same country are redundant; copies in different countries are
different opportunities and both are kept. Camunda posts one backend role in
both County Galway (IE) and Denmark, NB (CA) — collapsing those would silently
delete an entire country option from the quota. Country-aware keying removes 188
rows where a naive company+title key would remove 246.

Ranking within a group is by full ``composite_score``, tie-broken by most recent
``date_posted`` then ``job_id`` so the choice is deterministic across runs.

Applied rows are never touched — only ``active`` (plus ``cover_letter_ready``
with ``--include-ready``).

Usage:
    python scripts/scan_duplicate_postings.py                  # dry-run
    python scripts/scan_duplicate_postings.py --apply          # archive dupes
    python scripts/scan_duplicate_postings.py --include-ready  # also cover_letter_ready
"""

import argparse
import re
import shutil
import sys
import uuid as uuid_lib
from collections import defaultdict

from config import (
    COMPANY_REGISTRY_PATH,
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    composite_score,
    derive_country,
    load_json,
    save_json,
    now_utc,
    today,
)

ARCHIVE_REASON = "duplicate posting (kept best of {n})"
_DEFAULT_STATUSES = {"active"}

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    """Lowercase, collapse every run of non-alphanumerics to one space, trim.

    Deliberately does NOT strip a banking rank suffix: "… - AVP" and
    "… - Vice President" are different pay bands at the same bank, so merging
    them would discard a genuinely distinct posting. Contrast
    ``config.strip_banking_rank``, which exists to stop that suffix distorting
    the *seniority* read — a different question from identity."""
    return _NORM_RE.sub(" ", (title or "").lower()).strip()


def duplicate_key(job: dict) -> tuple:
    """Identity for dedup: same company, same normalized title, same derived
    country. Country is part of the key on purpose — see the module docstring."""
    return (
        job.get("company_id"),
        normalize_title(job.get("title")),
        derive_country(job.get("location") or ""),
    )


def find_duplicates(jobs: list[dict], statuses: set[str], co_by_id: dict) -> list[dict]:
    """Return the redundant rows — every member of a duplicate group except its
    best. Ranked by composite, then most recent ``date_posted``, then ``job_id``
    so repeated runs make the same choice."""
    groups: dict = defaultdict(list)
    for j in jobs:
        if j.get("pipeline_status") in statuses:
            groups[duplicate_key(j)].append(j)

    redundant: list[dict] = []
    for _key, rows in groups.items():
        if len(rows) < 2:
            continue
        ranked = sorted(
            rows,
            key=lambda j: (
                composite_score(j, co_by_id.get(j.get("company_id"))),
                j.get("date_posted") or "",
                j.get("job_id") or "",
            ),
            reverse=True,
        )
        for j in ranked[1:]:
            j["_dupe_group_size"] = len(rows)   # transient, for the log line only
            redundant.append(j)
    return redundant


def archive_duplicate_postings(apply: bool = True,
                               include_ready: bool = False,
                               verbose: bool = False) -> int:
    """Archive redundant copies in place. Returns the number archived (or that
    WOULD be, when ``apply=False``). Writes a ``.bak`` backup + ``job_archived``
    log entries only when there's something to archive, so a no-op call touches
    nothing. Shared by the CLI and the crawl's end-of-run auto-sweep."""
    jobs     = load_json(JOB_PIPELINE_PATH)
    co_by_id = {c["company_id"]: c for c in load_json(COMPANY_REGISTRY_PATH)}
    statuses = set(_DEFAULT_STATUSES) | ({"cover_letter_ready"} if include_ready else set())
    matches  = find_duplicates(jobs, statuses, co_by_id)
    if not matches:
        return 0

    if verbose:
        per_co: dict = defaultdict(int)
        for j in matches:
            per_co[j.get("company_name") or "?"] += 1
        for name, n in sorted(per_co.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {n:5} redundant  {name[:40]}")

    if not apply:
        for j in matches:
            j.pop("_dupe_group_size", None)
        return len(matches)

    backup = JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak")
    shutil.copyfile(JOB_PIPELINE_PATH, backup)

    log = load_json(PROCESS_LOG_PATH)
    now = now_utc()
    for j in matches:
        n_group = j.pop("_dupe_group_size", 2)
        reason  = ARCHIVE_REASON.format(n=n_group)
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
        description="Archive duplicate postings of the same role.")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Default is dry-run.")
    p.add_argument("--include-ready", action="store_true",
                   help="Also sweep cover_letter_ready rows (default: active only).")
    args = p.parse_args()

    preview = archive_duplicate_postings(apply=False,
                                         include_ready=args.include_ready,
                                         verbose=True)
    if preview == 0:
        print("No duplicate postings found.")
        return 0
    if not args.apply:
        print(f"\nDry run -- pass --apply to archive these {preview} redundant row(s).")
        return 0

    n = archive_duplicate_postings(apply=True, include_ready=args.include_ready)
    print(f"\nArchived {n} duplicate posting(s). "
          f"Backup at {JOB_PIPELINE_PATH.name}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
