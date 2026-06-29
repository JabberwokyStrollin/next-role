"""
prefilter_staged.py — Apply crawl-style pre-filter to staged LinkedIn jobs.

LinkedIn alert ingest produces many rows but most won't make it through the
seniority/location/stack filters. This script runs each staged row through
crawl.py's pre_filter logic, mutating data/email_staged.json in place to add:

    _prefilter_pass:   bool
    _prefilter_reason: str

The stack-score component is skipped for rows that don't have a JD body yet
(typical for LinkedIn URLs, which auth-wall) — so the filter degrades to
title + location matching, which still narrows hundreds of rows down quickly.
Once a JD is pasted, re-running this script applies the full filter including
stack scoring.

Usage:
    python scripts/prefilter_staged.py

Output (last line of stdout, machine-readable):
    PREFILTER: passed=<n_pass> failed=<n_fail>
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from crawl import load_crawl_config, title_excluded  # noqa: E402

STAGED_PATH   = ROOT / "data" / "email_staged.json"
MIN_JD_LENGTH = 200


def pre_filter_relaxed(title: str, location: str, jd_text: str, cfg: dict) -> tuple[bool, str]:
    """
    Title + location + (optional) stack-score filter for LinkedIn-staged rows.

    INTENTIONALLY pre-LLM, same rationale as scripts/crawl.py:pre_filter — see
    that file's banner. Do NOT call composite_score from here; signals come
    from profile/stack_keywords.yaml only.

    If jd_text is shorter than MIN_JD_LENGTH, the stack-score check is skipped
    so rows without a JD body don't all get rejected for missing stack keywords
    that should appear in the JD, not the title.
    """
    t = title.lower()
    l = location.lower()

    if not any(kw in t for kw in cfg["seniority_titles"]):
        return False, "title seniority miss"

    bad = title_excluded(t, cfg.get("title_exclude", []))
    if bad:
        return False, f"title excluded by '{bad}'"

    if not any(kw in l for kw in cfg["location_allow"]):
        return False, f"location miss ({location[:40]})"

    # Subtractive US geography gate (config SSOT). Same logic as crawl.pre_filter:
    # US is remote-only and only when "US" is an enabled target. CA/IE/OTHER pass.
    from config import location_passes  # defer — keep import surface minimal
    if not location_passes(location):
        return False, f"location US-gated (off / not remote) ({location[:40]})"

    if len(jd_text) >= MIN_JD_LENGTH:
        from config import compute_stack_score  # heavy import — defer
        # Full JD (compute_stack_score strips boilerplate + caps), not a prefix —
        # matches crawl.pre_filter and ingest, which score the whole JD.
        score = compute_stack_score(f"{title} {jd_text}")
        if score < cfg["min_pre_filter_score"]:
            return False, f"stack score {score} < {cfg['min_pre_filter_score']}"
        return True, f"stack {score}"

    return True, "title+location ok (no JD yet)"


def main() -> None:
    if not STAGED_PATH.exists():
        print("No staged file. Nothing to do.")
        print("PREFILTER: passed=0 failed=0")
        return

    rows = json.loads(STAGED_PATH.read_text(encoding="utf-8"))
    if not rows:
        print("Staged file is empty.")
        print("PREFILTER: passed=0 failed=0")
        return

    cfg    = load_crawl_config()
    passed = 0
    failed = 0
    for row in rows:
        title    = row.get("title",    "")
        location = row.get("location", "")
        jd_text  = row.get("jd_text",  "") or ""
        ok, reason = pre_filter_relaxed(title, location, jd_text, cfg)
        row["_prefilter_pass"]   = ok
        row["_prefilter_reason"] = reason
        if ok:
            passed += 1
        else:
            failed += 1

    STAGED_PATH.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Pre-filter applied to {len(rows)} rows: passed={passed} failed={failed}")
    print(f"PREFILTER: passed={passed} failed={failed}")


if __name__ == "__main__":
    main()
