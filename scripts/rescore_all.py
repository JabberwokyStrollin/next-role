"""
rescore_all.py — Re-score every active job under the current rubric.

Use after editing profile/scoring_rubric.md or profile/stack_keywords.yaml to
bring already-ingested jobs in line with the new rules. Mechanical stack score
is always recomputed (free). Seniority + domain scores are re-fetched from
Claude (paid API call per job).

Usage:
    python scripts/rescore_all.py --dry-run             # estimate count + cost, no writes
    python scripts/rescore_all.py                       # rescore active + cover_letter_ready
    python scripts/rescore_all.py --limit 10            # cap at 10 jobs (for testing)
    python scripts/rescore_all.py --include-applied     # also rescore applied/archived
    python scripts/rescore_all.py --stack-only          # mechanical only, no Claude

The pipeline file is backed up to data/job_pipeline.json.bak before the first
write. Progress is checkpointed to disk every CHECKPOINT_EVERY jobs so a
Ctrl+C mid-run doesn't lose completed work.
"""

import argparse
import shutil
import sys
import time
import traceback

from config import (
    JOB_PIPELINE_PATH,
    compute_stack_score,
    composite_score,
    load_json,
    save_json,
    now_utc,
)
from score_jd import score_jd

CHECKPOINT_EVERY = 10

# Per-call cost estimate for Claude Sonnet 4.5 (input $3/M, output $15/M).
# Assumes ~3500 input tokens (rubric + JD) and ~150 output tokens.
EST_COST_PER_CALL = 3500 * 3.0 / 1_000_000 + 150 * 15.0 / 1_000_000  # ~$0.013


def select_jobs(jobs: list, include_applied: bool) -> list:
    if include_applied:
        statuses = {"active", "cover_letter_ready", "applied", "archived"}
    else:
        statuses = {"active", "cover_letter_ready"}
    return [j for j in jobs if j.get("pipeline_status") in statuses]


def main() -> int:
    p = argparse.ArgumentParser(description="Re-score every active job under the current rubric.")
    p.add_argument("--dry-run",         action="store_true",
                   help="Show what would happen; don't call Claude or write.")
    p.add_argument("--limit",           type=int, default=0,
                   help="Cap at N jobs (0 = no cap).")
    p.add_argument("--include-applied", action="store_true",
                   help="Also re-score applied + archived jobs.")
    p.add_argument("--stack-only",      action="store_true",
                   help="Recompute mechanical stack score only; skip Claude.")
    p.add_argument("--job-ids",         metavar="ID[,ID...]",
                   help="Comma-separated list of job_ids; overrides status filter.")
    p.add_argument("--job-ids-file",    metavar="PATH",
                   help="File with one job_id per line; overrides status filter.")
    args = p.parse_args()

    jobs = load_json(JOB_PIPELINE_PATH)
    if not jobs:
        print("No jobs in pipeline.")
        return 0

    explicit_ids: set[str] = set()
    if args.job_ids:
        explicit_ids.update(s.strip() for s in args.job_ids.split(",") if s.strip())
    if args.job_ids_file:
        with open(args.job_ids_file, encoding="utf-8") as f:
            explicit_ids.update(line.strip() for line in f if line.strip())

    if explicit_ids:
        selected = [j for j in jobs if j.get("job_id") in explicit_ids]
        missing  = explicit_ids - {j.get("job_id") for j in selected}
        if missing:
            print(f"Warning: {len(missing)} job_id(s) not in pipeline: "
                  f"{', '.join(sorted(missing))[:200]}")
    else:
        selected = select_jobs(jobs, args.include_applied)

    if args.limit > 0:
        selected = selected[: args.limit]

    print(f"Pipeline jobs:    {len(jobs)}")
    print(f"Selected for run: {len(selected)}")
    if not args.stack_only:
        est = len(selected) * EST_COST_PER_CALL
        print(f"Estimated Claude cost: ~${est:.2f} "
              f"(@ ~${EST_COST_PER_CALL:.3f}/call, Sonnet 4.5).")
    if args.dry_run:
        print("\nDry run — no writes, no API calls.")
        return 0

    if not selected:
        print("Nothing to do.")
        return 0

    # Back up before any writes.
    backup_path = JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak")
    shutil.copyfile(JOB_PIPELINE_PATH, backup_path)
    print(f"\nBacked up pipeline to: {backup_path}")
    print(f"Starting rescore at {now_utc()}.\n")

    # Map job_id -> job dict for in-place mutation while preserving order.
    by_id = {j["job_id"]: j for j in jobs}

    deltas: list[tuple[str, str, int, int]] = []  # (job_id, label, old_composite, new_composite)
    failures: list[tuple[str, str]] = []          # (job_id, error)
    n_skipped_no_jd = 0
    started = time.time()

    for i, job in enumerate(selected, 1):
        job_id = job["job_id"]
        live   = by_id[job_id]
        label  = f"{live.get('company_name','?')[:28]} — {live.get('title','?')[:38]}"

        jd_text = (live.get("jd_text") or "").strip()
        if not jd_text:
            n_skipped_no_jd += 1
            print(f"[{i}/{len(selected)}] SKIP (no jd_text): {label}")
            continue

        old_stack     = live.get("stack_match_score") or 0
        old_seniority = live.get("seniority_score")   or 0
        old_domain    = live.get("domain_fit_score")  or 0
        old_composite = composite_score(live, None)   # company-side stable across rescore

        # Mechanical stack rescore (free).
        new_stack = compute_stack_score(f"{live.get('title','')} {jd_text}")
        live["stack_match_score"] = new_stack

        # LLM seniority + domain rescore (paid). Pass title so score_jd
        # applies the mechanical title-cap on the way out.
        if not args.stack_only:
            try:
                scores = score_jd(jd_text, title=live.get("title", ""))
            except Exception as e:
                failures.append((job_id, str(e)))
                print(f"[{i}/{len(selected)}] FAIL: {label} -- {e}")
                # Roll back the stack change for this job to keep the row
                # internally consistent until the user retries.
                live["stack_match_score"] = old_stack
                continue
            live["seniority_score"]  = scores["seniority_score"]
            live["domain_fit_score"] = scores["domain_fit_score"]
            live["score_notes"]      = scores["score_notes"]
            live["scored_at"]        = now_utc()

        new_composite = composite_score(live, None)
        deltas.append((job_id, label, old_composite, new_composite))

        delta = new_composite - old_composite
        sign  = "+" if delta >= 0 else ""
        print(f"[{i}/{len(selected)}] {label}")
        print(f"    stack {old_stack}->{live['stack_match_score']}  "
              f"sen {old_seniority}->{live.get('seniority_score', old_seniority)}  "
              f"dom {old_domain}->{live.get('domain_fit_score', old_domain)}  "
              f"composite {old_composite}->{new_composite} ({sign}{delta})")

        if i % CHECKPOINT_EVERY == 0:
            save_json(JOB_PIPELINE_PATH, jobs)
            elapsed = int(time.time() - started)
            rate    = i / max(1, elapsed)
            remaining = (len(selected) - i) / rate if rate > 0 else 0
            print(f"    -- checkpointed; {elapsed}s elapsed, ~{int(remaining)}s remaining")

    save_json(JOB_PIPELINE_PATH, jobs)
    elapsed_total = int(time.time() - started)

    # Summary
    print()
    print("=" * 60)
    print(f"Done in {elapsed_total}s.")
    print(f"Re-scored:    {len(deltas)}")
    print(f"No JD (skip): {n_skipped_no_jd}")
    print(f"Failures:     {len(failures)}")
    if failures:
        print("\nFailures:")
        for jid, err in failures[:10]:
            print(f"  {jid}: {err[:100]}")
        if len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more")

    if deltas:
        deltas.sort(key=lambda x: x[3] - x[2])
        print("\nBiggest drops (rubric tightened):")
        for jid, label, old, new in deltas[:5]:
            print(f"  {old:3d} -> {new:3d}  ({new - old:+d})  {label}")
        print("\nBiggest gains (rubric / stack expanded):")
        for jid, label, old, new in deltas[-5:][::-1]:
            print(f"  {old:3d} -> {new:3d}  ({new - old:+d})  {label}")

    print()
    print(f"Backup retained at: {backup_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Partial progress was saved at the last checkpoint.")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
