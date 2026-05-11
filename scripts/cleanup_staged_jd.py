"""
cleanup_staged_jd.py — clear similar-jobs noise from staged jd_text fields.

Before scripts/linkedin_fetch.py learned to detect LinkedIn's expired-job
redirect (resp.url moves from /jobs/view/<id>/ to /jobs/<title>-jobs?trk=
expired_jd_redirect), Fetch JD on expired postings populated jd_text with
the body of LinkedIn's similar-jobs landing page (~5-10 KB of "Sign in to
set job alerts for X roles" plus a list of other postings). Those rows
look successfully-fetched in the /today UI but contain unusable text.

This script scans data/email_staged.json for rows whose jd_text contains
the distinctive markers and clears the field so the Fetch JD button
re-appears in the UI. Re-fetching with the current parser will classify
each row correctly (ok / expired / auth_wall / short).

Usage:
    python scripts/cleanup_staged_jd.py --dry-run    # preview, no writes
    python scripts/cleanup_staged_jd.py              # clear in place
"""

import argparse
import json
from pathlib import Path

ROOT        = Path(__file__).parent.parent
STAGED_PATH = ROOT / "data" / "email_staged.json"

# Phrases that appear on LinkedIn's similar-jobs / expired-job landing
# page but never in a real JD body. Match if ANY are present.
CORRUPTION_MARKERS = (
    "Sign in to set job alerts for",
    "Get notified when a new job is posted",
    "You've viewed all jobs for this search",
)


def looks_corrupted(jd_text: str) -> bool:
    return any(m in jd_text for m in CORRUPTION_MARKERS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear similar-jobs noise from staged jd_text fields."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    args = parser.parse_args()

    if not STAGED_PATH.exists():
        print(f"No staged file at {STAGED_PATH}; nothing to clean.")
        return

    rows    = json.loads(STAGED_PATH.read_text(encoding="utf-8"))
    n_total = len(rows)

    corrupted = [r for r in rows if looks_corrupted(r.get("jd_text") or "")]

    print(f"Scanned {n_total} staged row(s); "
          f"{len(corrupted)} match the similar-jobs noise pattern.")

    if not corrupted:
        return

    sample_n = min(5, len(corrupted))
    print(f"\nFirst {sample_n} matched row(s):")
    for r in corrupted[:sample_n]:
        company = (r.get("company") or "?")[:30]
        title   = (r.get("title")   or "?")[:40]
        chars   = len(r.get("jd_text") or "")
        print(f"  - {company:<30}  {title:<40}  ({chars} chars)")

    if args.dry_run:
        print("\nDry run — no changes written.")
        return

    for r in corrupted:
        r["jd_text"] = ""

    STAGED_PATH.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nCleared jd_text on {len(corrupted)} row(s). Use the /today UI "
          f"to Fetch JD or Discard each one — the parser will now classify "
          f"expired postings correctly.")


if __name__ == "__main__":
    main()
