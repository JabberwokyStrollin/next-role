"""
resync_tracker_country.py — Re-derive the stored ``country`` field on every
application in ``application_tracker.json`` from its stored ``location``.

``update_status.log_application`` stamps ``country = derive_country(location)``
at apply time, so the field is correct *when written*. It is a **snapshot**,
though, and ``derive_country`` keeps getting sharper — US state names, Canadian
province codes, the IE/CA-before-US ordering. Every such improvement leaves
already-logged rows holding a stale verdict; the "Mississauga, ON (Remote)" row
stamped ``OTHER`` predates province-code detection.

Nothing in the codebase currently *reads* this field — cover-letter generation
re-derives from ``location`` through ``geography.py``, so a stale value can't
mis-stamp a work-permit paragraph today. It is reporting/analysis surface, and a
wrong country there silently skews any per-geography read of the tracker (a
"Remote, Toronto" application counted as OTHER instead of CA).

This is the tracker-side parallel of ``scan_foreign_locations.py``: run it after
you change ``derive_country`` / ``geography.py``. On a normal run it finds zero.

Usage:
    python scripts/resync_tracker_country.py            # dry-run
    python scripts/resync_tracker_country.py --apply    # write corrections
"""

import argparse
import shutil
import sys

from config import (
    APPLICATION_TRACKER_PATH,
    derive_country,
    load_json,
    save_json,
)


def find_stale(apps: list[dict]) -> list[tuple[dict, str, str]]:
    """Return ``(app, stored, fresh)`` for every row whose stored ``country``
    disagrees with a fresh ``derive_country(location)``. A row with no stored
    country counts as stale (it should carry one)."""
    out = []
    for a in apps:
        fresh  = derive_country(a.get("location") or "")
        stored = a.get("country")
        if stored != fresh:
            out.append((a, stored, fresh))
    return out


def resync_country(apply: bool = True, verbose: bool = False) -> int:
    """Rewrite stale ``country`` values in place. Returns the number corrected
    (or that WOULD be corrected when ``apply=False``). Writes a ``.bak`` backup
    only when there's something to change, so a no-op run touches nothing.

    No process-log entry: this corrects a derived cache to match the SSOT, it
    doesn't change the application's real-world state the way a status change
    does."""
    apps    = load_json(APPLICATION_TRACKER_PATH)
    matches = find_stale(apps)
    if not matches:
        return 0

    if verbose:
        for a, stored, fresh in matches:
            label = f"{(a.get('company_name') or '?')[:24]} -- {(a.get('title') or '?')[:34]}"
            print(f"  {str(stored):>6} -> {fresh:<6} {label}")
            print(f"      location: {(a.get('location') or '')!r}")

    if not apply:
        return len(matches)

    backup = APPLICATION_TRACKER_PATH.with_suffix(
        APPLICATION_TRACKER_PATH.suffix + ".bak")
    shutil.copyfile(APPLICATION_TRACKER_PATH, backup)

    for a, _stored, fresh in matches:
        a["country"] = fresh

    save_json(APPLICATION_TRACKER_PATH, apps)
    return len(matches)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Re-derive stored country on tracker applications.")
    p.add_argument("--apply", action="store_true",
                   help="Write changes. Default is dry-run.")
    args = p.parse_args()

    preview = resync_country(apply=False, verbose=True)
    if preview == 0:
        print("Every application's country matches derive_country(location).")
        return 0
    if not args.apply:
        print(f"\nDry run -- pass --apply to correct these {preview} row(s).")
        return 0

    n = resync_country(apply=True)
    print(f"\nCorrected {n} application(s). "
          f"Backup at {APPLICATION_TRACKER_PATH.name}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
