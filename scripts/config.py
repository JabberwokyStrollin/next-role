"""
config.py — Shared configuration for the next-role pipeline.
All other scripts import from here. No logic lives here — only paths,
constants, and environment loading.
"""

import os
import sys
import json
import re
from pathlib import Path
from datetime import date, datetime, timezone

# ─── stdout encoding ──────────────────────────────────────────────────────────
# Windows defaults stdout to cp1252, which can't encode common Unicode that
# Claude returns in score_notes (arrows, em-dashes) or that the dashboard
# prints (box-drawing). Force UTF-8 so the pipeline doesn't crash on output.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ─── Repo root (two levels above this file) ────────────────────────────────────

ROOT = Path(__file__).parent.parent.resolve()

# ─── Data directory (gitignored) ──────────────────────────────────────────────

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ─── KV file paths ────────────────────────────────────────────────────────────

COMPANY_REGISTRY_PATH    = DATA_DIR / "company_registry.json"
JOB_PIPELINE_PATH        = DATA_DIR / "job_pipeline.json"
APPLICATION_TRACKER_PATH = DATA_DIR / "application_tracker.json"
PROCESS_LOG_PATH         = DATA_DIR / "process_log.json"
TARGET_BOARDS_PATH       = DATA_DIR / "target_boards.json"
CRAWL_LOG_PATH           = DATA_DIR / "crawl_log.jsonl"

# ─── Rules files ──────────────────────────────────────────────────────────────

PROFILE_DIR           = ROOT / "profile"
COVER_LETTER_RULES    = PROFILE_DIR / "cover_letter_rules.md"
RESUME_PATH           = PROFILE_DIR / "resume.md"
SCORING_RUBRIC_PATH   = PROFILE_DIR / "scoring_rubric.md"
STACK_KEYWORDS_PATH   = PROFILE_DIR / "stack_keywords.md"

# ─── Output directory for generated cover letters ─────────────────────────────

OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ─── Anthropic API key (set as system environment variable) ───────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise EnvironmentError(
        "ANTHROPIC_API_KEY environment variable is not set.\n"
        "Run: $env:ANTHROPIC_API_KEY = 'sk-ant-...'"
    )

# ─── Models ───────────────────────────────────────────────────────────────────

CLAUDE_MODEL      = "claude-sonnet-4-5-20250929"  # JD scoring, cover letters
CLAUDE_MODEL_FAST = "claude-haiku-4-5-20251001"   # Company research (10x cheaper)

# ─── Scoring constants (loaded from profile/stack_keywords.md) ───────────────

def _load_stack_keywords(path: Path) -> tuple[dict, int]:
    if not path.exists():
        raise FileNotFoundError(
            f"Stack keywords not found: {path}\n"
            "Copy profile.example/stack_keywords.md to profile/ and fill in your stack."
        )
    keywords = {}
    max_score = 35
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"max_score\s*:", line, re.IGNORECASE):
            try:
                max_score = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            try:
                keywords[parts[0].strip().lower()] = int(parts[1].strip())
            except ValueError:
                pass
    return keywords, max_score


STACK_KEYWORDS, STACK_SCORE_MAX = _load_stack_keywords(STACK_KEYWORDS_PATH)

VELOCITY_TIERS = [
    (7,  5),
    (14, 4),
    (21, 3),
    (45, 1),
]  # (days_since_posted, score) — first match wins; default 0

STALENESS_TIERS = {
    "fresh":      (0,  29),
    "soft_stale": (30, 59),
    "hard_stale": (60, 9999),
}

GHOSTED_DAYS = 21   # applications with no response after this many days

# ─── JSON helpers ─────────────────────────────────────────────────────────────

def _sanitize(obj):
    """Recursively strip surrogate characters from all strings in a data structure."""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="ignore").decode("utf-8")
    if isinstance(obj, list):
        return [_sanitize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    return obj


def load_json(path: Path) -> list:
    """Load a JSON array from disk, returning [] if file doesn't exist."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: list) -> None:
    """Write a JSON array to disk with readable formatting."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_sanitize(data), f, indent=2, ensure_ascii=False)


# ─── Date/time helpers ────────────────────────────────────────────────────────

def today() -> str:
    """Return today's date as ISO string (YYYY-MM-DD). Always use this — never hardcode."""
    return date.today().isoformat()


def now_utc() -> str:
    """Return current UTC datetime as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def days_since(iso_date: str) -> int:
    """Return number of days between iso_date and today."""
    d = date.fromisoformat(iso_date)
    return (date.today() - d).days


# ─── Scoring helpers (mechanical — no Claude needed) ─────────────────────────

def compute_stack_score(jd_text: str) -> int:
    """Keyword-based stack match score. Mirrors artifact JS logic exactly."""
    text = jd_text.lower()
    score = 0
    for keyword, pts in STACK_KEYWORDS.items():
        if keyword in text:
            score += pts
    return min(STACK_SCORE_MAX, score)


def compute_velocity_score(date_posted: str | None) -> int:
    """Hiring velocity score based on days since posting."""
    if not date_posted:
        return 0
    age = days_since(date_posted)
    for threshold, score in VELOCITY_TIERS:
        if age <= threshold:
            return score
    return 0


def compute_staleness(date_posted: str | None) -> str:
    """Return staleness tier string."""
    if not date_posted:
        return "fresh"
    age = days_since(date_posted)
    if age < 30:
        return "fresh"
    if age < 60:
        return "soft_stale"
    return "hard_stale"


def composite_score(job: dict, company: dict | None) -> int:
    """Compute composite score from all stored components."""
    return (
        (job.get("stack_match_score")     or 0) +
        (job.get("seniority_score")       or 0) +
        (job.get("domain_fit_score")      or 0) +
        (job.get("hiring_velocity_score") or 0) +
        (company.get("sponsorship_score") if company else 0) +
        (company.get("remote_fit")        if company else 0)
    )
