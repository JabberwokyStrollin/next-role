"""
discard_ledger.py — inspect, backfill and reset the crawl's discard ledger.

The ledger (data/discarded_urls.json) remembers postings the pipeline already
judged and threw away. Without it the crawl has no memory of them: a discard
never reaches job_pipeline.json, which is where the URL dedup comes from, so
every run re-fetches and re-processes the same rejects. That was measured at 123
postings per crawl — 860 wasted Sonnet scoring calls and 10.1 hours across 7
runs, for zero ingests — because the work-model verdict can only be reached
AFTER score_jd.

Three modes:

    (default)   Summarise what's in the ledger, by reason.

    --backfill  Rebuild entries from `job_discarded` events already in
                process_log.json. The log has recorded every discard's URL all
                along, so this recovers the memory retroactively instead of
                waiting for the crawl to relearn it one expensive run at a time.

    --reason X --apply / --all --apply
                Delete entries. Needed after LOOSENING a policy: widen
                US_ACCEPTED_WORK_MODELS to accept "hybrid" and every
                `work_model` entry becomes a role you would now take but the
                crawl will never look at again. The ledger's parallel of the
                scan_no_sponsorship.py / scan_foreign_locations.py sweeps.

Deleting is a dry run unless --apply is passed. --backfill only ever adds.

Usage:
    python scripts/discard_ledger.py
    python scripts/discard_ledger.py --backfill
    python scripts/discard_ledger.py --reason work_model
    python scripts/discard_ledger.py --reason work_model --apply
    python scripts/discard_ledger.py --all --apply

Machine-readable last line:
    LEDGER: <total>          summary
    BACKFILLED: <n_added>    on --backfill
    CLEARED: <n>             on --apply
    WOULD_CLEAR: <n>         on a dry-run delete
"""

import argparse
import re
import sys
from collections import Counter

from config import (
    DISCARD_REASONS,
    DISCARDED_URLS_PATH,
    PROCESS_LOG_PATH,
    clear_discarded_urls,
    load_discarded_urls,
    load_json,
    save_discarded_urls,
)

# Map a historical `job_discarded` detail string back to a reason code. The log
# stores prose, not codes, so backfill has to recognise the phrasing each gate
# emits. Order matters: work-model wording also contains "role", so the specific
# patterns come first.
_DETAIL_TO_REASON: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bwork model\b|\brole is (?:hybrid|onsite|remote|unstated)\b"
                r"|accepted:\s*remote", re.I), "work_model"),
    (re.compile(r"no sponsorship|does not sponsor", re.I),                "no_sponsorship"),
    (re.compile(r"not an enabled target|foreign|location", re.I),         "location"),
    (re.compile(r"ethics", re.I),                                         "ethics"),
]


def reason_for_detail(detail: str) -> str:
    for pat, code in _DETAIL_TO_REASON:
        if pat.search(detail or ""):
            return code
    # Everything else that reached job_discarded was a field-validation failure.
    return "validation"


def backfill() -> tuple[int, int, Counter]:
    """Add ledger entries for every `job_discarded` event in the process log that
    carries a source_url. Never overwrites an existing entry (the live one is
    more precise) and never deletes. Returns (added, skipped_existing, by_reason)."""
    log = load_json(PROCESS_LOG_PATH) or []
    ledger = load_discarded_urls()
    added, skipped, by_reason = 0, 0, Counter()

    for ev in log:
        if ev.get("event_type") != "job_discarded":
            continue
        url = (ev.get("source_url") or "").strip()
        if not url:
            continue
        if url in ledger:
            skipped += 1
            continue
        detail = ev.get("detail") or ""
        reason = reason_for_detail(detail)
        name   = ev.get("entity_name") or ""
        company, _, title = name.partition(" — ")
        ledger[url] = {"reason": reason, "detail": detail[:200],
                       "at": ev.get("timestamp", ""),
                       "company": company.strip(), "title": title.strip()}
        added += 1
        by_reason[reason] += 1

    if added:
        save_discarded_urls(ledger)
    return added, skipped, by_reason


def print_summary(ledger: dict) -> None:
    print(f"Discard ledger: {DISCARDED_URLS_PATH}")
    print(f"  {len(ledger)} entr{'y' if len(ledger) == 1 else 'ies'}\n")
    for reason, n in Counter(v.get("reason", "?")
                             for v in ledger.values()).most_common():
        print(f"  {reason:<16} {n:>6}   "
              f"{DISCARD_REASONS.get(reason, '(unknown reason code)')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect / backfill / reset the discard ledger.")
    ap.add_argument("--backfill", action="store_true",
                    help="Rebuild entries from job_discarded events in the process log.")
    ap.add_argument("--reason", help=f"Select one reason code: {', '.join(DISCARD_REASONS)}")
    ap.add_argument("--all", action="store_true", help="Select every entry.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete the selection (default is a dry run).")
    ap.add_argument("--limit", type=int, default=8, help="Example rows to print.")
    args = ap.parse_args()

    if args.backfill:
        added, skipped, by_reason = backfill()
        print(f"Backfilled from {PROCESS_LOG_PATH.name}: {added} added, "
              f"{skipped} already present.")
        for reason, n in by_reason.most_common():
            print(f"  {reason:<16} {n:>6}")
        print()
        print_summary(load_discarded_urls())
        print(f"BACKFILLED: {added}")
        return

    ledger = load_discarded_urls()
    print_summary(ledger)
    print()

    if args.reason and args.reason not in DISCARD_REASONS:
        print(f"ERROR: unknown reason '{args.reason}'. Known: {', '.join(DISCARD_REASONS)}")
        sys.exit(2)
    if not args.reason and not args.all:
        print("Pass --backfill to populate, or --reason <code> / --all "
              "(then --apply) to delete.")
        print(f"LEDGER: {len(ledger)}")
        return

    selected = [(u, v) for u, v in ledger.items()
                if args.all or v.get("reason") == args.reason]
    print(f"Selected {len(selected)} "
          f"({'all reasons' if args.all else args.reason}):")
    for u, v in selected[:args.limit]:
        who = " — ".join(x for x in (v.get("company"), v.get("title")) if x)
        print(f"  {who or '(unknown)'}\n    {u}")
    if len(selected) > args.limit:
        print(f"  … and {len(selected) - args.limit} more")
    print()

    if not args.apply:
        print("Dry run — nothing deleted. Re-run with --apply.")
        print("Note: a cleared URL is re-fetched AND re-scored on the next "
              "crawl, so this costs roughly one Claude call per entry.")
        print(f"WOULD_CLEAR: {len(selected)}")
        return

    n = clear_discarded_urls(None if args.all else args.reason)
    print(f"Cleared {n}. They will be re-examined on the next crawl.")
    print(f"CLEARED: {n}")


if __name__ == "__main__":
    main()
