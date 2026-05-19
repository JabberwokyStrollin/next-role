"""
config.py — Shared configuration for the next-role pipeline.
All other scripts import from here. No logic lives here — only paths,
constants, and environment loading.
"""

import os
import sys
import json
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timezone

import yaml

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
COMP_ESTIMATES_PATH      = DATA_DIR / "comp_estimates.json"

# ─── Rules files ──────────────────────────────────────────────────────────────

PROFILE_DIR           = ROOT / "profile"
COVER_LETTER_RULES    = PROFILE_DIR / "cover_letter_rules.md"
RESUME_PATH           = PROFILE_DIR / "resume.md"
SCORING_RUBRIC_PATH   = PROFILE_DIR / "scoring_rubric.md"
STACK_KEYWORDS_PATH   = PROFILE_DIR / "stack_keywords.yaml"

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

# ─── Scoring constants (loaded from profile/stack_keywords.yaml) ─────────────

def _load_stack_keywords(path: Path) -> tuple[dict, int]:
    if not path.exists():
        raise FileNotFoundError(
            f"Stack keywords not found: {path}\n"
            "Copy profile.example/stack_keywords.yaml to profile/ and fill in your stack."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    max_score = int(data.get("max_score", 35))
    raw = data.get("keywords") or {}
    keywords = {str(k).lower(): int(v) for k, v in raw.items()}
    return keywords, max_score


STACK_KEYWORDS, STACK_SCORE_MAX = _load_stack_keywords(STACK_KEYWORDS_PATH)

# ─── SCORING SSOT ─────────────────────────────────────────────────────────────
#
# This block is the ONLY place composite-ranking parameters live in code.
# Two weight profiles exist for two distinct ranking purposes:
#
#   FULL composite (``weight`` field, ``COMPOSITE_MAX``)
#     Used for apply-time ranking and cover-letter selection. Sums all
#     seven signals; requires the company record (sponsorship + remote).
#     Computed by ``composite_score(job, company)``.
#
#   PRE-RESEARCH composite (``pre_research_weight`` field, ``PRE_RESEARCH_MAX``)
#     Used ONLY to rank stub companies for the research queue. Zeros out
#     company-derived signals (sponsorship, remote) so stub defaults don't
#     contaminate the ranking. Computed by ``composite_score_pre_research(job)``.
#
# Surfaces (serve.py, dashboard.py, ingest.py, run.py, README, etc.) MUST
# import from here and reference ``COMPONENTS[k].weight`` /
# ``COMPONENTS[k].pre_research_weight`` for display denominators, and
# ``COMPOSITE_MAX`` / ``PRE_RESEARCH_MAX`` for the overall ceilings.
#
# DO NOT:
#   - hardcode "X/25", "/130", "/100", or any score denominator in another file
#   - introduce a parallel ``composite_score`` or ``composite_score_pre_research``
#     function elsewhere — there is exactly one of each, defined in this module
#   - inline a partial composite (e.g. summing stack + seniority + domain +
#     velocity for sort order) — use the canonical function instead
#   - use ``composite_score_pre_research`` for apply-time ranking or
#     cover-letter selection (it ignores sponsorship + remote on purpose)
#   - use ``composite_score`` to rank candidates for the research queue
#     (stub defaults will dominate the ordering)
#
# DO:
#   - extend ``COMPONENTS`` to add a new ranking signal (give it both a
#     ``weight`` and a ``pre_research_weight``; set the latter to 0 if the
#     signal isn't available before research)
#   - update ``native_max`` here if the storage scale of a stored field
#     changes (e.g. if score_jd starts emitting seniority 0-30)
#
# Related SSOTs (each canonical for its own concern):
#   - ``_SENIORITY_BUCKETS`` below — title→cap mapping
#   - ``profile/stack_keywords.yaml`` — keyword scores + pre-filter lists
#   - ``profile/scoring_rubric.md``  — Claude's 0-25 / 0-20 output ranges
#
# Pre-filter (crawl.py, prefilter_staged.py) is INTENTIONALLY pre-LLM and
# reads only the YAML side of this triangle. Do not call ``composite_score``
# OR ``composite_score_pre_research`` from a pre-filter; both require Claude
# scoring on the job and would wreck the cost model.
# ──────────────────────────────────────────────────────────────────────────-

@dataclass(frozen=True)
class ScoringComponent:
    weight:              int   # contribution to full composite (display denominator)
    native_max:          int   # max value the stored field can hold (storage scale)
    pre_research_weight: int   # contribution to pre-research composite (0 for company-derived signals)

    @property
    def multiplier(self) -> float:
        """How much each stored point contributes to the full composite."""
        return self.weight / self.native_max if self.native_max else 0.0

    @property
    def pre_research_multiplier(self) -> float:
        """How much each stored point contributes to the pre-research composite."""
        return self.pre_research_weight / self.native_max if self.native_max else 0.0


COMPONENTS: dict[str, ScoringComponent] = {
    # Stack syncs from profile/stack_keywords.yaml so users tune it as data.
    "stack":       ScoringComponent(weight=STACK_SCORE_MAX, native_max=STACK_SCORE_MAX, pre_research_weight=25),
    "domain":      ScoringComponent(weight=25, native_max=20, pre_research_weight=32),  # Claude rubric 0-20
    "seniority":   ScoringComponent(weight=10, native_max=25, pre_research_weight=18),  # Claude rubric 0-25 (cap applied at storage time)
    "velocity":    ScoringComponent(weight=10, native_max=5,  pre_research_weight=15),  # VELOCITY_TIERS top tier
    "freshness":   ScoringComponent(weight=8,  native_max=8,  pre_research_weight=10),  # FRESHNESS_TIERS top tier
    "sponsorship": ScoringComponent(weight=35, native_max=15, pre_research_weight=0),   # Haiku research output (company-derived)
    "remote":      ScoringComponent(weight=12, native_max=5,  pre_research_weight=0),   # Haiku research output (company-derived)
}

COMPOSITE_MAX:     int = sum(c.weight              for c in COMPONENTS.values())
PRE_RESEARCH_MAX:  int = sum(c.pre_research_weight for c in COMPONENTS.values())

# Pre-research gate: jobs scoring below this on the pre-research composite
# are not researched, regardless of research-queue depth. Spending Haiku +
# 1 web search ($0.03-0.05) on a clearly-mediocre stub doesn't pay off.
RESEARCH_QUEUE_MIN_SCORE: int = 55


VELOCITY_TIERS = [
    (7,  5),
    (14, 4),
    (21, 3),
    (45, 1),
]  # (days_since_posted, score) — first match wins; default 0. Stays on /5
   # native scale; composite_score multiplies by COMPONENTS["velocity"].multiplier.

# Freshness bonus: extra weight for very recently posted/discovered jobs.
# Stacks on top of VELOCITY_TIERS. Scored natively on the same scale as the
# composite weight so no multiplier work is needed at composite time.
FRESHNESS_TIERS = [
    (0, 8),   # posted today
    (1, 3),   # 1 day ago
    (2, 1),   # 2 days ago
]  # (max_days, bonus); older than the last threshold = 0

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


# ─── Title-based seniority cap (mechanical, applied after Claude scoring) ───-

# Order matters: more specific patterns must come before broader ones.
# "Senior Principal" / "Sr. Principal" / "Senior Staff" must match before
# "Senior" or "Principal" alone, since substring matches are first-wins.
import re as _re

_SENIORITY_BUCKETS: list[tuple[str, _re.Pattern, int]] = [
    # Bucket D — score 0 (two steps away from Staff). Listed first so they
    # beat the broader Senior/Principal patterns that follow.
    ("D", _re.compile(r"\bdistinguished\b",                _re.I), 0),
    ("D", _re.compile(r"\bfellow\b",                       _re.I), 0),
    ("D", _re.compile(r"\bsenior\s+principal\b",           _re.I), 0),
    ("D", _re.compile(r"\bsr\.?\s+principal\b",            _re.I), 0),
    ("D", _re.compile(r"\bvp\b",                           _re.I), 0),
    ("D", _re.compile(r"\bvice\s+president\b",             _re.I), 0),
    ("D", _re.compile(r"\bjunior\b",                       _re.I), 0),
    ("D", _re.compile(r"\bjr\.?\b",                        _re.I), 0),
    ("D", _re.compile(r"\bintern\b",                       _re.I), 0),
    ("D", _re.compile(r"\bentry[-\s]level\b",              _re.I), 0),
    ("D", _re.compile(r"\bassociate\s+(software\s+)?engineer\b", _re.I), 0),

    # Bucket A — at target (no cap). Senior Staff stays here despite the word
    # "Senior" because it's senior to Staff, not below it.
    ("A", _re.compile(r"\bsenior\s+staff\b",               _re.I), 25),
    ("A", _re.compile(r"\bstaff\b",                        _re.I), 25),
    ("A", _re.compile(r"\bsr\.?\s+staff\b",                _re.I), 25),
    ("A", _re.compile(r"\blead\s+(engineer|developer)\b",  _re.I), 25),
    ("A", _re.compile(r"\btech\s+lead\b",                  _re.I), 25),
    ("A", _re.compile(r"\barchitecte?\b",                  _re.I), 25),

    # Bucket B — one step below target (cap 15).
    ("B", _re.compile(r"\bsenior\b",                       _re.I), 15),
    ("B", _re.compile(r"\bsr\.?\s",                        _re.I), 15),

    # Bucket C — one step above target (cap 15).
    ("C", _re.compile(r"\bprincipal\b",                    _re.I), 15),
]


def title_seniority_cap(title: str) -> tuple[str, int]:
    """
    Classify a job title and return (bucket_letter, max_seniority_score).
    Defaults to ('A', 25) if no bucket matches — better to under-cap than
    silently zero an unfamiliar title.
    """
    t = (title or "").strip()
    for bucket, pat, cap in _SENIORITY_BUCKETS:
        if pat.search(t):
            return bucket, cap
    return "A", 25


def apply_title_cap(raw_seniority: int, title: str) -> int:
    """Cap a raw Claude seniority score by the job title bucket."""
    _, cap = title_seniority_cap(title)
    return max(0, min(cap, int(raw_seniority)))


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


def compute_freshness_bonus(job: dict) -> int:
    """
    Bonus for very recently posted/discovered jobs. Recomputed on every call
    (not stored on the record) so the score decays naturally as the job ages.

    Prefers ``job['date_posted']`` (source-supplied YYYY-MM-DD); falls back to
    ``job['date_found']`` (full ISO ingest timestamp) when posting date is
    unavailable. Only the date portion is used — sub-day precision on
    ``date_found`` is intentionally ignored because source ``date_posted``
    is day-grained, so finer tiers would only be honest for one input.
    """
    raw = job.get("date_posted") or job.get("date_found") or ""
    if not raw:
        return 0
    iso_date = raw[:10]
    try:
        age = days_since(iso_date)
    except ValueError:
        return 0
    for threshold, bonus in FRESHNESS_TIERS:
        if age <= threshold:
            return bonus
    return 0


# ─── JD-level no-sponsorship filter (mechanical, runs at ingest time) ────────-
#
# Some JDs explicitly state the company will not sponsor a visa for the role.
# ``ingest.py`` uses ``detect_no_sponsorship`` to discard those postings before
# the Claude scoring call — saving the API cost and keeping them out of the
# pipeline entirely.
#
# This is a HARD ingest-time discard, parallel to ``ethics_hard_exclude`` on
# the company record but operating per-JD. It is intentionally separate from
# the composite's ``sponsorship`` component (0-15 company-level score based
# on the org's historical sponsorship record) — that score can still be good
# even when an individual posting opts out.
#
# Patterns deliberately err on false negatives over false positives: each
# requires an explicit negation token near the word "sponsor".
# Caught (representative): "we are unable to provide visa sponsorship",
# "we do not sponsor visas", "this role does not offer sponsorship",
# "must be authorized to work without sponsorship", "no visa sponsorship
# available", "sponsorship is not available for this position".
# Intentionally NOT caught: "we sponsor visas", "visa sponsorship available",
# "open to sponsorship".

_NO_SPONSORSHIP_PATTERNS: list[_re.Pattern] = [
    # Subject + negation + (within ~40 chars) the word "sponsor".
    _re.compile(
        r"\b(?:un(?:able|willing)|cannot|can'?t|won'?t|"
        r"(?:do(?:es)?|will|are|is|am)\s+not|"
        r"do(?:es)?n'?t|aren'?t|isn'?t|not\s+able)\b"
        r"[^.!?\n]{0,40}?\bsponsor",
        _re.I,
    ),
    # "without ... sponsor" — implies candidate must already be authorized.
    _re.compile(r"\bwithout\b[^.!?\n]{0,40}?\bsponsor", _re.I),
    # "no (visa|work) sponsorship"
    _re.compile(r"\bno\s+(?:visa\s+|work\s+(?:visa\s+)?)?sponsorship\b", _re.I),
    # "sponsorship ... not available/offered/provided/possible"
    _re.compile(
        r"\bsponsorship\b[^.!?\n]{0,30}?\bnot\s+(?:available|offered|provided|possible)\b",
        _re.I,
    ),
    # "not eligible for ... sponsor[ship]"
    _re.compile(r"\bnot\s+eligible\b[^.!?\n]{0,40}?\bsponsor", _re.I),
]


def detect_no_sponsorship(jd_text: str) -> str | None:
    """
    Scan JD text for an explicit refusal to sponsor a visa. Returns a short
    snippet around the first match (for logging), or None if no refusal
    language is found. Caller owns the discard/log decision.
    """
    if not jd_text:
        return None
    for pat in _NO_SPONSORSHIP_PATTERNS:
        m = pat.search(jd_text)
        if m:
            start = max(0, m.start() - 20)
            end   = min(len(jd_text), m.end() + 20)
            return jd_text[start:end].strip()
    return None


def composite_score(job: dict, company: dict | None) -> int:
    """
    Full composite score — used for apply-time ranking and cover-letter
    selection. Sums all seven components weighted by COMPONENTS[k].weight.

    Stored fields stay on their native scales (0-25 for Claude seniority, 0-15
    for sponsorship research, etc.); this function applies the per-component
    multiplier to bring each into its share of the COMPOSITE_MAX.

    Use ``composite_score_pre_research(job)`` instead when ranking stub
    companies for the research queue — that variant zeros out the
    company-derived signals so stub defaults don't dominate the order.
    """
    raw: dict[str, int] = {
        "stack":       (job.get("stack_match_score")     or 0),
        "domain":      (job.get("domain_fit_score")      or 0),
        "seniority":   (job.get("seniority_score")       or 0),
        "velocity":    (job.get("hiring_velocity_score") or 0),
        "freshness":   compute_freshness_bonus(job),
        "sponsorship": ((company or {}).get("sponsorship_score") or 0),
        "remote":      ((company or {}).get("remote_fit")        or 0),
    }
    return sum(int(raw[k] * c.multiplier) for k, c in COMPONENTS.items())


def composite_score_pre_research(job: dict) -> int:
    """
    Pre-research composite — used ONLY to rank stub companies for the
    research queue. Sums the components whose data is available at
    ingest time (stack, domain, seniority, velocity, freshness), weighted
    by COMPONENTS[k].pre_research_weight. Sponsorship and remote fit are
    intentionally zero-weighted because their values come from company
    research; including them with stub defaults (sponsorship=7, remote=3)
    creates a constant ~23-point baseline that dominates rankings.

    Returns an int in [0, PRE_RESEARCH_MAX]. Do NOT use this for apply-
    time ranking or cover-letter selection — those need the full
    sponsorship + remote signal via ``composite_score(job, company)``.
    """
    raw: dict[str, int] = {
        "stack":     (job.get("stack_match_score")     or 0),
        "domain":    (job.get("domain_fit_score")      or 0),
        "seniority": (job.get("seniority_score")       or 0),
        "velocity":  (job.get("hiring_velocity_score") or 0),
        "freshness": compute_freshness_bonus(job),
    }
    return sum(
        int(raw[k] * c.pre_research_multiplier)
        for k, c in COMPONENTS.items()
        if c.pre_research_weight > 0
    )


# ─── COMPANY-FILTER SSOT ──────────────────────────────────────────────────────
#
# Single source of truth for "should this company be hidden from apply
# surfaces right now". Anywhere that surfaces jobs for application MUST call
# this; do not implement a parallel rule elsewhere.
#
# Rule: hide companies with MAX_ACTIVE_APPS_PER_COMPANY in-flight applications.
# "In-flight" means status in IN_FLIGHT_STATUSES with no response_date yet.
# A rejection / interview / withdraw / offer flips the status (and/or sets
# response_date in update_status.cmd_status), which immediately frees the slot
# — there is no time-based cooldown. ``ghosted`` is intentionally excluded so
# silently-dead applications give the slot back.
#
# Surfaces that call this:
#   - serve.py:render_cover_letters_body (the web UI's apply queue)
#   - run.py:generate_cover_letters (legacy CLI surface; kept in sync)
#
# Pre-filters (crawl.py, prefilter_staged.py, ingest.py) DO NOT call this:
# the crawl is intentionally permissive so good roles enter the pipeline even
# at companies already in flight; the ranking surfaces handle suppression.
# (Separate from ``ethics_hard_exclude`` at ingest time, which is an absolute
# "never work here" kill switch.)
# ──────────────────────────────────────────────────────────────────────────-

MAX_ACTIVE_APPS_PER_COMPANY: int = 3

IN_FLIGHT_STATUSES: frozenset[str] = frozenset({
    "applied",
    "recruiter_screen",
    "interview",
})


def company_block_reason(company_id: str | None, apps: list[dict]) -> str | None:
    """
    Return a short reason string if a company should be hidden from apply
    surfaces, or None if it can be shown.

    A company is blocked when MAX_ACTIVE_APPS_PER_COMPANY or more applications
    at it are in-flight (status in IN_FLIGHT_STATUSES, no response_date).
    """
    if not company_id:
        return None
    active = sum(
        1 for a in apps
        if a.get("company_id") == company_id
        and a.get("status") in IN_FLIGHT_STATUSES
        and not a.get("response_date")
    )
    if active >= MAX_ACTIVE_APPS_PER_COMPANY:
        return f"{active} active applications"
    return None
