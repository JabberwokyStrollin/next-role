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
COMP_ESTIMATES_PATH        = DATA_DIR / "comp_estimates.json"
APPLICATION_QUESTIONS_PATH = DATA_DIR / "application_questions.json"
RESUME_ENTRY_NOTES_PATH    = DATA_DIR / "resume_entry_notes.json"

# ─── Rules files ──────────────────────────────────────────────────────────────

PROFILE_DIR             = ROOT / "profile"
COVER_LETTER_RULES      = PROFILE_DIR / "cover_letter_rules.md"
RESUME_PATH             = PROFILE_DIR / "resume.md"
SCORING_RUBRIC_PATH     = PROFILE_DIR / "scoring_rubric.md"
STACK_KEYWORDS_PATH     = PROFILE_DIR / "stack_keywords.yaml"
ANSWER_QUESTIONS_RULES  = PROFILE_DIR / "answer_questions_rules.md"

# ─── Resume entry slugs (canonical names for sections of profile/resume.md) ──
#
# Slugs are short stable identifiers the answer-questions generator uses to
# label which resume entries it drew on for a given answer (so the operator
# can override the selection). Display labels are short human-readable
# strings shown in the chip UI. Add a new entry here when adding a new
# project / role to profile/resume.md that the generator should be able to
# cite.

RESUME_ENTRY_SLUGS: dict[str, str] = {
    "haloc_distilled":            "HALOC Distilled (Spark microbatching, petabyte-scale cost reduction)",
    "haloc_flink_distilled":      "HALOC Flink Distilled (Flink streaming framework, Java, petabyte-scale)",
    "jailer":                     "Jailer re-architecture (reverse-engineered, 24x dup pattern, >50% cost reduction)",
    "yaml_ingestion":             "YAML ingestion framework (12+ Spark dataflows, config-only onboarding)",
    "mass_gpc":                   "MASS/GPC (enterprise-wide event-driven initiatives, six-team stakeholder group)",
    "splunk_base":                "Splunk Base queries (pre-aggregation, dashboard restoration)",
    "storm_portal_microservices": "7 Spring Boot microservices (Insight Global, 100% coverage)",
    "storm_portal_arch_docs":     "Storm Portal architecture documentation (Insight Global)",
    "raytheon_consolidation":     "6-repo consolidation + data orchestration program (Raytheon)",
    "ingersoll_crossdomain":      "Cross-domain classification bridge (Ingersoll Consulting)",
    "next_role":                  "next-role AI job search pipeline (Anthropic API, Sonnet+Haiku)",
}

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

CLAUDE_MODEL      = "claude-sonnet-4-5-20250929"  # JD scoring
CLAUDE_MODEL_FAST = "claude-haiku-4-5-20251001"   # Company research (10x cheaper)
CL_MODEL          = "claude-sonnet-4-6"           # Cover letters, answer-questions (matches generate_cl.js)

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
RESEARCH_QUEUE_MIN_SCORE: int = 45

# US-role sponsorship floor (native 0-15 scale, the ``sponsorship`` component's
# native_max). For US-derived roles ``composite_score`` substitutes THIS value
# for the company's sponsorship_score: the operator is a US citizen (no
# sponsorship needed), but US is a reluctant stop-gap, so the value is kept
# deliberately low — a thumb on the scale, NOT a hard tier. CA/IE roles with
# normal sponsorship outrank comparable US roles, yet a strong-stack US role can
# still beat a weak CA/IE one. Tune here only (set to 0 for "zero added from
# sponsorship"). Only consulted when "US" is in TARGET_COUNTRIES.
US_SPONSORSHIP_SCORE: int = 3


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

GHOSTED_DAYS = 21   # applications with no response after this many days auto-flip to 'ghosted'

# Second-stage aging: a 'ghosted' application still without a response this
# many days after it was submitted auto-converts to a rejection (with
# rejection_reason "ghosted_timeout"). Clears the Ghosted tab of long-dead
# applications while preserving them as rejections for metrics. Must be
# greater than GHOSTED_DAYS. See auto_age_application() below.
GHOSTED_REJECTED_DAYS = 45

# Rejection-reason SSOT. The key is stored in
# application_tracker.rejection_reason; the value is the human label shown in
# the UI and appended to the application note. Surfaces (serve.py status
# buttons, metrics.py breakdown) import this — never hardcode a reason key or
# label elsewhere.
REJECTION_REASONS: dict[str, str] = {
    "generic":          "Generic rejection",
    "position_filled":  "Position filled",
    "interview_failed": "Interview failed",
    "ghosted_timeout":  "Auto-rejected (ghosted)",
}

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


# ─── Answer-text sanitization (for application question outputs) ─────────────
#
# Application forms are typically plain-text inputs that mangle smart quotes,
# em dashes, bullets, and any HTML-ish characters. We strip these at
# generation time so the stored answer is already copy-paste safe. The web UI
# binds the copy button to the stored value, so no second pass is needed.

_ANSWER_SANITIZE_MAP: list[tuple[str, str]] = [
    ("‘", "'"),    # left single quote
    ("’", "'"),    # right single quote
    ("“", '"'),    # left double quote
    ("”", '"'),    # right double quote
    ("•", ""),     # bullet
    ("·", ""),     # middle dot
    (" ", " "),    # non-breaking space
    (">",      ""),
    ("<",      ""),
    ("*",      ""),
    ("#",      ""),
]

# Dash handling lives in regex instead of the char map because:
#   1) ``--`` and `` - `` (single hyphen between spaces) need surrounding-
#      whitespace context, not char-by-char substitution.
#   2) A single hyphen between digits (``5-10``) or inside a compound word
#      (``well-known``) must be preserved -- only prose-em-dash usage should
#      collapse to a comma.
# Every dash variant collapses to ``", "`` because that reads as natural
# prose. If a colon would have been better in a given spot, the operator
# can swap it via the editable answer textarea on the /answer-questions
# card (the manual-edit path also runs through this sanitizer).
import re as _aq_re
_DASH_PROSE_RE  = _aq_re.compile(r"\s*(?:—|–|--)\s*")
_DASH_SINGLE_RE = _aq_re.compile(r"(?<!\d)\s+-\s+(?!\d)")
_COMMA_CHAIN_RE = _aq_re.compile(r",(?:\s*,)+")
_MULTISPACE_RE  = _aq_re.compile(r"  +")


def sanitize_answer_text(text: str) -> str:
    """
    Strip / replace characters unsafe for plain-text application fields,
    and collapse AI-tell dash patterns to commas. Called on every generated
    answer AND on every manual edit before storage; the copy button uses
    the stored (already sanitized) value, so no second pass at copy time.

    Dash policy (applied after char-substitutions, in this order):
      - em / en / double-hyphen with any surrounding whitespace -> ``", "``
      - single hyphen with whitespace on both sides -> ``", "``, but only
        when not flanked by digits (so ``"5 - 10"`` survives intact)
      - chains like ``", , ,"`` collapse to a single ``","``
    """
    if not text:
        return ""
    for char, replacement in _ANSWER_SANITIZE_MAP:
        text = text.replace(char, replacement)
    text = _DASH_PROSE_RE.sub(", ", text)
    text = _DASH_SINGLE_RE.sub(", ", text)
    text = _COMMA_CHAIN_RE.sub(",", text)
    text = _MULTISPACE_RE.sub(" ", text)
    return text.strip()


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


# ─── Geography SSOT (re-exported from geography.py) ──────────────────────────
#
# All location → country derivation and the geography pre-filter gate live in
# the dependency-free ``geography`` module (no API key, importable by the Node
# cover-letter generator via subprocess). They are re-exported here so the rest
# of the pipeline keeps using ``from config import derive_country`` etc.
# unchanged. ``TARGET_COUNTRIES`` (the US toggle) lives in geography.py;
# ``US_SPONSORSHIP_SCORE`` (a scoring concern) stays in this module, above.
from geography import (  # noqa: E402
    TARGET_COUNTRIES,
    REMOTE_ONLY_SOURCES,
    derive_country,
    is_remote_role,
    names_foreign_location,
    location_passes,
)


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

# Trailing company-boilerplate markers. ATS JDs typically end with About /
# EEO / Benefits / Pay-Range-Transparency blocks that match stack keywords
# incidentally (e.g. "Apache Spark" in Databricks' About blurb appears on
# every Databricks JD, even pure-frontend ones). We truncate the JD at the
# earliest marker before keyword scoring.
#
# Safety: only search the trailing half — a heading like "About this role"
# inside the body description won't trip the heuristic.
_BOILERPLATE_MARKERS: tuple[_re.Pattern, ...] = (
    # Greenhouse structural wrappers
    _re.compile(r'<div\s+class\s*=\s*"content-conclusion"',                              _re.I),
    _re.compile(r'<div\s+class\s*=\s*"content-pay-transparency"',                        _re.I),
    # HTML section headings (<strong>/<b>/<h1-6>)
    _re.compile(r'<(?:strong|b|h[1-6])\b[^>]*>\s*about\s+\w',                            _re.I),
    _re.compile(r'<(?:strong|b|h[1-6])\b[^>]*>\s*(?:our\s+)?commitment\s+to\s+diversity', _re.I),
    _re.compile(r'<(?:strong|b|h[1-6])\b[^>]*>\s*equal\s+(?:opportunity|employment)',    _re.I),
    _re.compile(r'<(?:strong|b|h[1-6])\b[^>]*>\s*e\.?e\.?o',                             _re.I),
    _re.compile(r'<(?:strong|b|h[1-6])\b[^>]*>\s*pay\s+range\s+transparency',            _re.I),
    _re.compile(r'<(?:strong|b|h[1-6])\b[^>]*>\s*compliance\b',                          _re.I),
    _re.compile(r'<(?:strong|b|h[1-6])\b[^>]*>\s*benefits?\s*</',                        _re.I),
    # Plaintext heading patterns (start of line/paragraph)
    _re.compile(r'(?m)^\s*about\s+us\b',                                                 _re.I),
    _re.compile(r'(?m)^\s*about\s+the\s+company\b',                                      _re.I),
    _re.compile(r'(?m)^\s*equal\s+(?:opportunity|employment)\s+employer\b',              _re.I),
    _re.compile(r'(?m)^\s*our\s+commitment\s+to\s+diversity\b',                          _re.I),
    # RemoteOK spam-protector tag (always at very end of those JDs)
    _re.compile(r'\bplease\s+mention\s+the\s+word\s+\*\*',                               _re.I),
)


def strip_company_boilerplate(jd_text: str) -> str:
    """
    Return ``jd_text`` truncated at the earliest trailing-boilerplate marker
    in the trailing half. If no marker matches, returns the input unchanged.
    Used by ``compute_stack_score`` to avoid scoring keywords that only
    appear in the company's About / EEO / Benefits / Pay-Range sections.
    """
    if not jd_text:
        return ""
    n = len(jd_text)
    search_from = n // 2
    earliest = n
    for pat in _BOILERPLATE_MARKERS:
        m = pat.search(jd_text, search_from)
        if m and m.start() < earliest:
            earliest = m.start()
    return jd_text[:earliest]


def compute_stack_score(jd_text: str) -> int:
    """
    Keyword-based stack match score over the role body of ``jd_text``.

    Two filters distinguish this from a naive substring scan:
      1. Trailing company boilerplate (About / EEO / Benefits / Pay Range)
         is dropped via ``strip_company_boilerplate`` so keywords only
         appearing there (e.g. "Apache Spark" in Databricks' About blurb)
         don't count.
      2. Keyword matching uses word-boundary regex (``\\bkw\\b``) so e.g.
         "java" no longer matches "javascript".
    """
    text = strip_company_boilerplate(jd_text).lower()
    score = 0
    for keyword, pts in STACK_KEYWORDS.items():
        if _re.search(rf"\b{_re.escape(keyword)}\b", text):
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


# ─── Ethics auto-exclude rules (deterministic post-process) ──────────────────
#
# These rules force ``ethics_hard_exclude=True`` on a company regardless of
# the LLM's overall judgment. The principle: Claude returns categorized,
# described ethics flags; we (Python) own the policy decision of which
# categories + descriptions are absolute disqualifiers.
#
# Per the project's "deterministic rules in code" principle: when an LLM
# must apply a hard policy alongside its judgment calls, the policy lives
# in a post-process, not the prompt. Rules added here also apply
# retroactively via a sweep over existing company records.
#
# Single entry point: ``company_auto_exclude_reason(company)``. The
# per-rule predicates beneath it are also exposed so the surfaces / tests
# can introspect *which* rule fired.

# --- Rule 1: employee-targeted surveillance ----------------------------------
# The "surveillance" Haiku category is broad — covers customer data, KYC,
# seller monitoring, ad targeting, AND employee surveillance. Only the
# employee-targeting variant is the policy disqualifier here.
_EMPLOYEE_SURVEILLANCE_RE: _re.Pattern = _re.compile(
    r"\b(?:employee|worker|workforce)\b", _re.I
)

# --- Rule 2: mass surveillance ------------------------------------------------
# Confirmed `surveillance` category flag whose description names law
# enforcement, intelligence agencies, facial recognition, predictive
# policing, spyware sales, or similar — the Palantir / Clearview / NSO
# / ShotSpotter category.
_MASS_SURVEILLANCE_DESC_RE: _re.Pattern = _re.compile(
    r"\bmass\s+surveillance\b"
    r"|\bfacial\s+recognition\b"
    r"|\bpredictive\s+policing\b"
    r"|\blaw\s+enforcement\b"
    r"|\bintelligence\s+agen(?:cy|cies)\b"
    r"|\bborder\s+(?:surveillance|patrol|enforcement)\b"
    r"|\bspyware\b"
    r"|\bgovernment\s+surveillance\b",
    _re.I,
)

# --- Rule 3: direct defense contractor ---------------------------------------
# Industry-field match. Haiku reliably populates ``industry``; defense
# contractors get labels like "Aerospace & Defense", "Defense Technology",
# "Military Systems". Intentionally excludes ambiguous terms like
# "aerospace" alone (would match SpaceX / commercial aviation) and
# "intelligence" alone (would false-positive on Sales-Intelligence SaaS).
_DEFENSE_INDUSTRY_RE: _re.Pattern = _re.compile(
    r"\b(?:defense|defence|military|weapons?|munitions?|armaments?)\b",
    _re.I,
)


def is_employee_surveillance_flag(flag: dict) -> bool:
    """True iff ``flag`` is a confirmed employee-targeted surveillance flag."""
    if not flag or flag.get("status") != "confirmed":
        return False
    if flag.get("category") != "surveillance":
        return False
    return bool(_EMPLOYEE_SURVEILLANCE_RE.search(flag.get("description") or ""))


def is_mass_surveillance_flag(flag: dict) -> bool:
    """True iff ``flag`` is a confirmed mass-surveillance flag (law-enforcement,
    intelligence, spyware, facial-recognition, etc.)."""
    if not flag or flag.get("status") != "confirmed":
        return False
    if flag.get("category") != "surveillance":
        return False
    return bool(_MASS_SURVEILLANCE_DESC_RE.search(flag.get("description") or ""))


def is_defense_contractor(company: dict) -> bool:
    """True iff the company's ``industry`` string names defense/military/weapons."""
    return bool(_DEFENSE_INDUSTRY_RE.search((company or {}).get("industry") or ""))


def company_auto_exclude_reason(company: dict) -> str | None:
    """
    Apply the deterministic ethics_hard_exclude rules to a researched
    company record. Returns a short reason string for the first rule that
    fires, or None if no rule applies. Order is fixed (industry first,
    then per-flag rules) so the reason is reproducible.
    """
    if not company:
        return None
    if is_defense_contractor(company):
        return "defense contractor (industry match)"
    for f in company.get("ethics_flags") or []:
        if is_employee_surveillance_flag(f):
            return "confirmed employee surveillance"
        if is_mass_surveillance_flag(f):
            return "confirmed mass surveillance"
    return None


# ─── Government / defense entanglement screen (Phase 1: surface-only) ─────────
#
# Extends the tier_a defense-contractor exclusion above with a graded
# company-level flag (gov_defense_flag) plus a per-role exposure modifier
# (role_exposure). The surfaced result is a function of BOTH, because the
# concern is personal assignment risk, not mere association: product/infra
# roles are insulated even at a company with government customers; field
# roles (SA / professional services / support) are exposed.
#
# PHASE 1 IS SURFACE-ONLY. We detect, store, and surface the result + the
# interview questions; we do NOT yet apply a composite penalty for a `flag`
# (GOV_SCREEN_FLAG_PENALTY_PCT is reserved for a future scoring phase). `fail`
# still means "exclude", but only tier_a produces it, and tier_a's actual
# hard-exclude continues to flow through the deterministic
# `is_defense_contractor` rule above — Haiku-only tier_a on a non-defense
# industry is surfaced, not newly auto-excluded.
#
# Division of labor (per the "deterministic rules in code" principle):
#   - Detection of gov_defense_flag + flag_evidence  → Haiku research (judgment)
#   - role_exposure read of the JD                    → Sonnet score_jd (judgment)
#   - support-role-is-exposed toggle, the combination matrix, and the tier_a
#     floor                                           → Python here (policy)

# User-editable config. flagged_regions uses ISO 3166-1 alpha-2 codes; the
# region (tier_c) escalation logic ships but stays dormant until populated.
GOV_SCREEN_FLAGGED_REGIONS: list[str] = []
GOV_SCREEN_FLAG_PENALTY_PCT: int = 20            # reserved for Phase 2 scoring; UNUSED in Phase 1
GOV_SCREEN_SUPPORT_ROLES_EXPOSED: bool = True    # treat support engineering as exposed (follow-the-sun routing)

GOV_DEFENSE_FLAGS: tuple[str, ...] = ("none", "tier_c", "tier_b", "tier_a")
ROLE_EXPOSURES:    tuple[str, ...] = ("insulated", "ambiguous", "exposed")

GOV_SCREEN_INTERVIEW_QUESTIONS: list[str] = [
    "How is customer-specific work assigned and scoped for this role?",
    "Does this role involve professional services engagements or customer "
    "escalations, and can engineers opt out of specific engagements?",
    "What does the public-sector side of the business look like, and which "
    "teams touch it?",
]

# Titles that are exposed regardless of JD body text. Support engineering is
# listed separately because it's gated by GOV_SCREEN_SUPPORT_ROLES_EXPOSED.
_ROLE_EXPOSED_TITLE_RE: _re.Pattern = _re.compile(
    r"\b(?:solutions?\s+architect|professional\s+services|forward[-\s]deployed|"
    r"field\s+engineer(?:ing)?|sales\s+engineer(?:ing)?|"
    r"technical\s+account\s+manager|customer\s+engineer|"
    r"implementation\s+(?:engineer|consultant|specialist)|"
    r"deployment\s+strategist|solutions?\s+engineer)\b",
    _re.I,
)
_ROLE_SUPPORT_TITLE_RE: _re.Pattern = _re.compile(
    r"\bsupport\s+engineer(?:ing)?\b", _re.I
)


def classify_role_exposure(title: str, claude_exposure: str | None = None) -> str:
    """Resolve a role's gov-screen exposure (insulated | ambiguous | exposed).

    Deterministic title rules win first (support gated by
    GOV_SCREEN_SUPPORT_ROLES_EXPOSED); otherwise fall back to Claude's
    JD-level judgment, defaulting to 'insulated' when absent/invalid. Called
    by ingest.py with the title that score_jd doesn't see."""
    t = title or ""
    if _ROLE_EXPOSED_TITLE_RE.search(t):
        return "exposed"
    if _ROLE_SUPPORT_TITLE_RE.search(t) and GOV_SCREEN_SUPPORT_ROLES_EXPOSED:
        return "exposed"
    if claude_exposure in ROLE_EXPOSURES:
        return claude_exposure
    return "insulated"


def reconcile_gov_defense_flag(company: dict | None) -> str:
    """Resolve a company's gov_defense_flag, forcing it to at least `tier_a`
    for industry-detected defense contractors regardless of the LLM's
    classification (mirrors the ethics_hard_exclude floor). Returns a value in
    GOV_DEFENSE_FLAGS. Called in research_company after the Haiku merge."""
    if is_defense_contractor(company):
        return "tier_a"
    raw = (company or {}).get("gov_defense_flag") or "none"
    return raw if raw in GOV_DEFENSE_FLAGS else "none"


# Part 3 combination matrix. Rows = gov_defense_flag, cols = role_exposure.
# Value = (result, emit_interview_questions). The spec's Part 2 note that
# `ambiguous` is "treated as insulated for scoring" is moot in Phase 1 (no
# scoring); this matrix is authoritative for the surfaced result.
_GOV_SCREEN_MATRIX: dict[str, dict[str, tuple[str, bool]]] = {
    "none":   {"insulated": ("pass", False), "ambiguous": ("pass", True),  "exposed": ("pass", False)},
    "tier_c": {"insulated": ("pass", False), "ambiguous": ("pass", True),  "exposed": ("flag", False)},
    "tier_b": {"insulated": ("pass", True),  "ambiguous": ("flag", False), "exposed": ("flag", False)},
    "tier_a": {"insulated": ("fail", False), "ambiguous": ("fail", False), "exposed": ("fail", False)},
}


def gov_screen_result(gov_defense_flag: str | None,
                      role_exposure: str | None) -> tuple[str, bool]:
    """SSOT for the Part 3 combination matrix. Returns
    ``(result, emit_questions)`` where result is one of pass | flag | fail.
    Unknown inputs degrade safely to `none` / `insulated`. Derived on display
    from the live company flag + the job's stored role_exposure, so a later
    company re-research never leaves a stale result behind."""
    flag = gov_defense_flag if gov_defense_flag in GOV_DEFENSE_FLAGS else "none"
    exp  = role_exposure    if role_exposure    in ROLE_EXPOSURES    else "insulated"
    return _GOV_SCREEN_MATRIX[flag][exp]


# ─── Gov-screen ranking effects (Phase 2) ────────────────────────────────────
#
# A `flag` result reranks (a configurable penalty) but never excludes; a `fail`
# result excludes from apply surfaces. Both effects are APPLY-TIME only and are
# the ONLY places the gov screen touches ordering — the canonical
# ``composite_score`` stays pure (so ``metrics.py``'s "components sum to
# composite" invariant holds). Never bake the penalty into ``composite_score``;
# always go through ``apply_rank_score``.

def gov_screen_penalty_factor(job: dict, company: dict | None) -> float:
    """Multiplier applied to the composite at apply-time ranking:
    ``1 - GOV_SCREEN_FLAG_PENALTY_PCT/100`` when the gov-screen result is
    `flag`, else ``1.0``. `fail` is handled by exclusion, not penalty, so it
    returns 1.0 here (the role is hidden before ranking)."""
    result, _ = gov_screen_result(
        (company or {}).get("gov_defense_flag"),
        (job or {}).get("role_exposure"),
    )
    if result == "flag":
        return max(0.0, 1.0 - GOV_SCREEN_FLAG_PENALTY_PCT / 100.0)
    return 1.0


def apply_rank_score(job: dict, company: dict | None) -> int:
    """Apply-time ranking value: the full composite reduced by the gov-screen
    penalty. Use this ONLY for apply-queue / cover-letter ordering;
    ``composite_score`` remains the canonical displayed score. This is a thin
    wrapper over the SSOT composite (not a parallel/partial composite), plus a
    documented policy factor."""
    return int(composite_score(job, company) * gov_screen_penalty_factor(job, company))


def gov_screen_block_reason(job: dict, company: dict | None) -> str | None:
    """Return a short reason if a role must be hidden from apply surfaces due
    to the gov-screen (result == `fail`, i.e. tier_a / defense entanglement),
    else None. Apply-time only, parallel to ``company_block_reason`` — consulted
    by ``serve.render_cover_letters_body`` and ``run.generate_cover_letters``."""
    flag = (company or {}).get("gov_defense_flag") or "none"
    result, _ = gov_screen_result(flag, (job or {}).get("role_exposure"))
    if result == "fail":
        return f"gov/defense {flag} (exclude)"
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
    # US roles (operator is a US citizen — no sponsorship needed) substitute a
    # deliberately low US_SPONSORSHIP_SCORE for the company sponsorship_score, so
    # CA/IE roles generally outrank them without hard-flooring US below
    # everything. Only fires when "US" is enabled in TARGET_COUNTRIES; otherwise
    # the company score is used as before (CA/IE composites stay byte-identical).
    us_role = "US" in TARGET_COUNTRIES and derive_country(job.get("location", "")) == "US"
    raw: dict[str, int] = {
        "stack":       (job.get("stack_match_score")     or 0),
        "domain":      (job.get("domain_fit_score")      or 0),
        "seniority":   (job.get("seniority_score")       or 0),
        "velocity":    (job.get("hiring_velocity_score") or 0),
        "freshness":   compute_freshness_bonus(job),
        "sponsorship": (US_SPONSORSHIP_SCORE if us_role
                        else ((company or {}).get("sponsorship_score") or 0)),
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


# ─── Application aging (time-based status transitions) ───────────────────────
#
# Single source of truth for the two date-driven status transitions. Both the
# web view (serve.py:apply_ghosted_check) and the CLI (update_status.cmd_list)
# call this so the two surfaces never diverge.
#
# These are NOT throttle cooldowns (the company filter has none — see the
# COMPANY-FILTER SSOT banner above). They only advance an application that has
# received no response at all:
#   applied  → ghosted   after GHOSTED_DAYS          (ghosted_flag = True)
#   ghosted  → rejected  after GHOSTED_REJECTED_DAYS (rejection_reason
#                                                     "ghosted_timeout")
#
# An application with a response_date, or whose status is already past these
# states (recruiter_screen / interview / offer / rejected / withdrawn), is left
# untouched. The auto-rejection intentionally does NOT set response_date —
# there was no real response, so it stays out of funnel-speed metrics.

def auto_age_application(app: dict) -> bool:
    """Apply the time-based status transitions to one application in place.
    Returns True iff the record was mutated."""
    if app.get("response_date"):
        return False
    applied = app.get("date_applied")
    if not applied:
        return False
    try:
        age = days_since(applied)
    except ValueError:
        return False

    status  = app.get("status")
    changed = False

    if status == "applied" and age > GHOSTED_DAYS:
        app["status"]       = "ghosted"
        app["ghosted_flag"] = True
        status              = "ghosted"
        changed             = True

    if status == "ghosted" and age > GHOSTED_REJECTED_DAYS:
        app["status"]           = "rejected"
        app["ghosted_flag"]     = False
        app["rejection_reason"] = "ghosted_timeout"
        note = f"Auto-rejected after {GHOSTED_REJECTED_DAYS} days with no response (ghosted)."
        existing = app.get("notes") or ""
        app["notes"] = (existing + ("\n" if existing else "") + note).strip()
        changed = True

    return changed


# ─── Duplicate-application guard (apply-time) ────────────────────────────────
#
# The same role is frequently reposted under a different listing URL (or
# re-ingested after its first copy was archived), becoming a second pipeline
# job. ``ingest.check_duplicate`` only matches the exact apply_url of a
# non-archived job, so it can't see these — and ``cmd_log`` dedups only by
# job_id. This helper detects "same company + same core title" so apply
# surfaces can warn before a second application to effectively the same role.
#
# It is a WARNING signal, not a hard pipeline gate: ingest stays permissive on
# purpose, and the operator can override with --force / a confirm dialog.

def normalize_role_title(title: str) -> str:
    """Reduce a job title to a comparable core: lowercase, drop any
    specialization after the first comma or '(', strip punctuation, collapse
    whitespace. 'Staff II Software Engineer, Data Ingestion' and 'Staff II
    Software Engineer' both normalize to 'staff ii software engineer'."""
    t = (title or "").lower()
    for sep in (",", "("):
        i = t.find(sep)
        if i > 0:
            t = t[:i]
    t = _re.sub(r"[^a-z0-9 ]+", " ", t)
    return _re.sub(r"\s+", " ", t).strip()


def find_duplicate_application(
    company_id: str | None,
    title: str,
    apps: list[dict],
    exclude_app_id: str | None = None,
) -> dict | None:
    """Return an existing application at the same company whose normalized
    title matches ``title``, or None. Used to warn before logging or queuing a
    second application to effectively the same role. Matches regardless of the
    prior application's status — a prior rejection is still worth flagging."""
    if not company_id:
        return None
    norm = normalize_role_title(title)
    if not norm:
        return None
    for a in apps:
        if exclude_app_id and a.get("application_id") == exclude_app_id:
            continue
        if a.get("company_id") != company_id:
            continue
        if normalize_role_title(a.get("title", "")) == norm:
            return a
    return None
