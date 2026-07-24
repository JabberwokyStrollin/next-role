"""
backup_data.py — daily local snapshots of the gitignored data/ files.

data/ is gitignored and otherwise unbacked, so a stray delete or a corrupt write
loses real operator state (applications, drills, company research, comp
estimates, questions, …). This takes an at-most-once-per-day snapshot of the
important data/*.json files into data/backups/<YYYY-MM-DD>/ and prunes to the
most recent config.DATA_BACKUP_RETAIN_DAYS snapshots.

Scope: protects against single-file loss / corruption *within* data/. It does
NOT protect against losing the whole data/ tree (the snapshots live under it) —
for that, keep the external backup of data/ recommended in SETUP.md.
job_pipeline.json is skipped: it's large, regenerable via crawl, and already
gets its own .bak from rescore_all / scan_* before any destructive write.

Called best-effort on every /today render (serve.apply_data_backup) for an
at-least-daily cadence without a cron; also runnable directly:

    python scripts/backup_data.py            # snapshot today unless already done
    python scripts/backup_data.py --force    # snapshot even if today's exists
    python scripts/backup_data.py --list     # list existing snapshots
"""

import argparse
import shutil
from datetime import date
from pathlib import Path

from config import DATA_DIR, DATA_BACKUP_DIR, DATA_BACKUP_RETAIN_DAYS

# Skipped from snapshots: the large regenerable pipeline (has its own .bak) and
# any .bak files. The backups/ subdir is never matched (glob is non-recursive).
_SKIP_NAMES = {"job_pipeline.json"}


def _source_files() -> list[Path]:
    return sorted(
        p for p in DATA_DIR.glob("*.json")
        if p.is_file() and p.name not in _SKIP_NAMES and not p.name.endswith(".bak")
    )


def _snapshot_dirs() -> list[Path]:
    if not DATA_BACKUP_DIR.is_dir():
        return []
    return sorted(d for d in DATA_BACKUP_DIR.iterdir() if d.is_dir())


def prune(retain: int = DATA_BACKUP_RETAIN_DAYS) -> int:
    """Delete all but the most recent ``retain`` snapshot dirs. Returns count removed."""
    dirs = _snapshot_dirs()
    old  = dirs[:-retain] if retain > 0 and len(dirs) > retain else []
    for d in old:
        shutil.rmtree(d, ignore_errors=True)
    return len(old)


def backup_once(force: bool = False) -> tuple[bool, str]:
    """Snapshot today's data files unless today's snapshot already exists.
    Returns (did_backup, dest_dir). Copies are best-effort per file so one
    unreadable file can't abort the rest; prunes old snapshots afterward."""
    dest = DATA_BACKUP_DIR / date.today().isoformat()
    if dest.exists() and not force:
        return False, str(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for src in _source_files():
        try:
            shutil.copy2(src, dest / src.name)
        except Exception:
            pass
    prune()
    return True, str(dest)


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily snapshot of data/ JSON files.")
    ap.add_argument("--force", action="store_true", help="Snapshot even if today's exists.")
    ap.add_argument("--list", action="store_true", help="List existing snapshots.")
    args = ap.parse_args()

    if args.list:
        for d in _snapshot_dirs():
            n = len(list(d.glob("*.json")))
            print(f"{d.name}  ({n} file(s))")
        print(f"TOTAL: {len(_snapshot_dirs())} snapshot(s)")
        return

    did, dest = backup_once(force=args.force)
    print(f"{'Backed up' if did else 'Already backed up today'} -> {dest}")
    print(f"BACKUP: {'created' if did else 'skipped'} dest={dest}")


if __name__ == "__main__":
    main()
