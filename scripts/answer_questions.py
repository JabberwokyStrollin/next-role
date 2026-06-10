"""
answer_questions.py — Claude-driven ad-hoc application question answering.

Web-UI only. No CLI entrypoint. Driven by serve.py's /answer-questions
routes (see ARCHITECTURE.md for the route table).

Two question classes:
  - motivation  → "Why this company / role?"
  - behavioral  → "Describe a time you did X"

Each generation call:
  1. Loads the question record + the job + the company (may be None)
  2. Builds a system prompt from profile/answer_questions_rules.md + the
     resume + RESUME_ENTRY_SLUGS registry + non-empty entry notes
  3. Calls Sonnet 4.6 (CL_MODEL — same model as cover letters)
  4. Parses tolerantly (mirrors comp_estimate.parse_comp_json), runs the
     answer through sanitize_answer_text, appends a new draft version
  5. Never overwrites finalized_answer — regeneration always appends a
     new draft version; the user explicitly finalizes or unfinalizes

Per-class char_cap is honored at prompt time; the model is told it's a
hard limit. The UI displays char_count vs char_cap.
"""

import json
import uuid

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CL_MODEL,
    APPLICATION_QUESTIONS_PATH,
    RESUME_ENTRY_NOTES_PATH,
    ANSWER_QUESTIONS_RULES,
    RESUME_ENTRY_SLUGS,
    RESUME_PATH,
    JOB_PIPELINE_PATH,
    COMPANY_REGISTRY_PATH,
    PROCESS_LOG_PATH,
    sanitize_answer_text,
    load_json,
    save_json,
    now_utc,
)


MAX_TOKENS = 1500

QUESTION_CLASSES: tuple[str, ...] = ("motivation", "behavioral")
QUESTION_STATUSES: tuple[str, ...] = ("draft", "finalized")


# ─── Persistence ─────────────────────────────────────────────────────────────

def load_questions() -> dict:
    """Return the dict keyed by job_id, or {} if file missing / empty."""
    if not APPLICATION_QUESTIONS_PATH.exists():
        return {}
    raw = load_json(APPLICATION_QUESTIONS_PATH)
    # load_json returns [] for missing/empty; coerce to {} for this dict-shaped file.
    if isinstance(raw, list):
        return {}
    return raw or {}


def save_questions(data: dict) -> None:
    save_json(APPLICATION_QUESTIONS_PATH, data)


def load_entry_notes() -> dict:
    """Return the notes dict. Initializes a complete set of slugs (empty
    strings) on first access so the UI can iterate slugs without missing-key
    handling."""
    if not RESUME_ENTRY_NOTES_PATH.exists():
        seed = {slug: "" for slug in RESUME_ENTRY_SLUGS}
        save_json(RESUME_ENTRY_NOTES_PATH, seed)
        return seed
    raw = load_json(RESUME_ENTRY_NOTES_PATH)
    if isinstance(raw, list):
        raw = {}
    notes = dict(raw or {})
    # Backfill any newly-added slugs so the UI never hits a KeyError.
    for slug in RESUME_ENTRY_SLUGS:
        notes.setdefault(slug, "")
    return notes


def save_entry_notes(notes: dict) -> None:
    # Drop unknown slugs; preserve only the canonical set.
    clean = {slug: str(notes.get(slug, "") or "") for slug in RESUME_ENTRY_SLUGS}
    save_json(RESUME_ENTRY_NOTES_PATH, clean)


# ─── Question CRUD ───────────────────────────────────────────────────────────

def _empty_buckets() -> dict:
    return {cls: [] for cls in QUESTION_CLASSES}


def get_job_questions(job_id: str) -> dict:
    data = load_questions()
    bucket = data.get(job_id)
    if not bucket:
        return _empty_buckets()
    # Backfill missing class keys defensively.
    return {cls: list(bucket.get(cls, [])) for cls in QUESTION_CLASSES}


def _find_question(data: dict, job_id: str, question_id: str) -> tuple[str | None, dict | None]:
    """Return (class_key, record) or (None, None) if not found."""
    bucket = data.get(job_id) or {}
    for cls in QUESTION_CLASSES:
        for record in bucket.get(cls, []):
            if record.get("question_id") == question_id:
                return cls, record
    return None, None


def add_question(
    job_id: str,
    question_text: str,
    question_class: str,
    char_cap: int | None,
) -> dict:
    if question_class not in QUESTION_CLASSES:
        raise ValueError(
            f"question_class must be one of {QUESTION_CLASSES}, got {question_class!r}"
        )
    text = (question_text or "").strip()
    if not text:
        raise ValueError("question_text must not be empty")

    data   = load_questions()
    bucket = data.setdefault(job_id, _empty_buckets())
    bucket.setdefault(question_class, [])

    record = {
        "question_id":             str(uuid.uuid4()),
        "question_text":           text,
        "question_class":          question_class,
        "char_cap":                int(char_cap) if char_cap else None,
        "resume_entries_used":     [],
        "question_override_notes": "",
        "draft_history":           [],
        "finalized_answer":        None,
        "finalized_at":            None,
        "status":                  "draft",
    }
    bucket[question_class].append(record)
    save_questions(data)
    return record


def delete_question(job_id: str, question_id: str) -> bool:
    """Remove a draft question. Refuses to delete finalized records."""
    data = load_questions()
    bucket = data.get(job_id)
    if not bucket:
        return False
    for cls in QUESTION_CLASSES:
        records = bucket.get(cls, [])
        for i, rec in enumerate(records):
            if rec.get("question_id") == question_id:
                if rec.get("status") == "finalized":
                    raise ValueError("Cannot delete a finalized question; unfinalize first.")
                records.pop(i)
                save_questions(data)
                return True
    return False


def update_question_override(job_id: str, question_id: str, override_notes: str) -> dict:
    data = load_questions()
    _, record = _find_question(data, job_id, question_id)
    if not record:
        raise ValueError(f"Question {question_id} not found for job {job_id}")
    record["question_override_notes"] = (override_notes or "").strip()
    save_questions(data)
    return record


def update_resume_entries(job_id: str, question_id: str, slugs: list[str]) -> dict:
    data = load_questions()
    _, record = _find_question(data, job_id, question_id)
    if not record:
        raise ValueError(f"Question {question_id} not found for job {job_id}")
    # Keep only known slugs, in the order provided.
    seen = set()
    clean: list[str] = []
    for s in slugs or []:
        if s in RESUME_ENTRY_SLUGS and s not in seen:
            clean.append(s)
            seen.add(s)
    record["resume_entries_used"] = clean
    save_questions(data)
    return record


# ─── Prompt construction ─────────────────────────────────────────────────────

def _format_slug_registry() -> str:
    lines = ["RESUME_ENTRY_SLUGS (use these exact slug strings in resume_entries_used):"]
    for slug, label in RESUME_ENTRY_SLUGS.items():
        lines.append(f"  - {slug}: {label}")
    return "\n".join(lines)


def _format_entry_notes(notes: dict) -> str:
    rows = [
        f"  - {slug}: {note.strip()}"
        for slug, note in notes.items()
        if (note or "").strip()
    ]
    if not rows:
        return ""
    return (
        "GLOBAL RESUME ENTRY NOTES (authoritative overrides — apply when "
        "drawing on the named entry):\n" + "\n".join(rows)
    )


def build_prompt(
    job: dict,
    company: dict | None,
    question_record: dict,
    entry_notes: dict,
) -> tuple[str, str]:
    """Return (system_prompt, user_message) for one generation call."""
    rules       = ANSWER_QUESTIONS_RULES.read_text(encoding="utf-8") if ANSWER_QUESTIONS_RULES.exists() else ""
    resume_text = RESUME_PATH.read_text(encoding="utf-8") if RESUME_PATH.exists() else ""
    cls         = question_record.get("question_class", "behavioral")

    sys_parts = [
        "You write tailored answers to ad-hoc application questions. "
        "Return ONLY valid JSON — no preamble, no markdown fences.",
        "",
        "## Rules",
        rules,
        "",
        "## The candidate's resume",
        resume_text,
        "",
        "## Resume entry slug registry",
        _format_slug_registry(),
    ]
    notes_block = _format_entry_notes(entry_notes)
    if notes_block:
        sys_parts += ["", "## Global entry notes", notes_block]
    sys_parts += [
        "",
        "## Question class for this call",
        f"This question is class **{cls}** — apply the matching strategy section "
        f"from the rules above.",
    ]
    system_prompt = "\n".join(sys_parts)

    company_line = ""
    if company:
        bits = []
        if company.get("industry"):     bits.append(f"industry={company['industry']}")
        if company.get("size_tier"):    bits.append(f"size_tier={company['size_tier']}")
        if company.get("country_hq"):   bits.append(f"hq_country={company['country_hq']}")
        if bits:
            company_line = "Company context: " + ", ".join(bits)

    char_cap = question_record.get("char_cap")
    cap_line = (
        f"Character cap: {char_cap} (HARD LIMIT — count precisely and stay under)"
        if char_cap else
        "Character cap: none (aim for a focused, complete answer per the rules)"
    )

    override = (question_record.get("question_override_notes") or "").strip()
    override_line = f"For this question only: {override}" if override else "For this question only: none"

    prior_slugs = question_record.get("resume_entries_used") or []
    if prior_slugs:
        prior_line = (
            "Previously used entries (this is a regeneration — prefer these unless "
            "you can clearly justify a different choice): "
            + ", ".join(prior_slugs)
        )
    else:
        prior_line = "Previously used entries: none — auto-select per the strategy."

    user_parts = [
        f"Job: {job.get('title','?')} at {job.get('company_name','?')}",
        f"Location: {job.get('location','?')}",
    ]
    if company_line:
        user_parts.append(company_line)
    user_parts += [
        "",
        "Job Description:",
        (job.get("jd_text") or "(JD text unavailable.)").strip(),
        "",
        f"Question ({cls}): {question_record.get('question_text','')}",
        cap_line,
        override_line,
        prior_line,
        "",
        'Return JSON: {"answer": "...", "resume_entries_used": ["slug1", ...]}',
    ]
    return system_prompt, "\n".join(user_parts)


# ─── Claude call + parsing ───────────────────────────────────────────────────

def _parse_answer_json(raw: str) -> dict:
    """Tolerant JSON parser — mirrors comp_estimate.parse_comp_json."""
    cleaned = (raw or "").strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    elif "{" in cleaned:
        cleaned = cleaned[cleaned.index("{") : cleaned.rindex("}") + 1]
    return json.loads(cleaned)


def _call_claude(system: str, user_message: str) -> tuple[str, int, int]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CL_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    text = msg.content[0].text
    return text, msg.usage.input_tokens, msg.usage.output_tokens


def _append_log(event: dict) -> None:
    log = load_json(PROCESS_LOG_PATH) or []
    log.append({"timestamp": now_utc(), **event})
    save_json(PROCESS_LOG_PATH, log)


# ─── Generation ──────────────────────────────────────────────────────────────

def generate_answer(job_id: str, question_id: str) -> dict:
    """Generate one new draft answer for the named question. Appends a new
    version to draft_history; never overwrites finalized_answer. Returns the
    updated question record."""
    data = load_questions()
    cls, record = _find_question(data, job_id, question_id)
    if not record:
        raise ValueError(f"Question {question_id} not found for job {job_id}")

    jobs = load_json(JOB_PIPELINE_PATH) or []
    job  = next((j for j in jobs if j.get("job_id") == job_id), None)
    if not job:
        raise ValueError(f"Job {job_id} not found in pipeline")

    companies = load_json(COMPANY_REGISTRY_PATH) or []
    company   = next((c for c in companies if c.get("company_id") == job.get("company_id")), None)

    entry_notes = load_entry_notes()
    system, user = build_prompt(job, company, record, entry_notes)

    raw, in_toks, out_toks = _call_claude(system, user)

    try:
        parsed = _parse_answer_json(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned non-JSON ({e}). Raw response:\n{raw}") from e

    answer_raw = parsed.get("answer")
    if not isinstance(answer_raw, str) or not answer_raw.strip():
        raise ValueError(f"Response missing 'answer' string. Parsed: {parsed!r}")
    slugs_raw = parsed.get("resume_entries_used") or []
    if not isinstance(slugs_raw, list):
        raise ValueError(f"Response 'resume_entries_used' must be a list, got: {slugs_raw!r}")

    answer = sanitize_answer_text(answer_raw)
    # Filter to known slugs only.
    chosen_slugs = [s for s in slugs_raw if isinstance(s, str) and s in RESUME_ENTRY_SLUGS]

    version = len(record.get("draft_history") or []) + 1
    record.setdefault("draft_history", []).append({
        "version":       version,
        "answer":        answer,
        "char_count":    len(answer),
        "generated_at":  now_utc(),
    })
    record["resume_entries_used"] = chosen_slugs

    save_questions(data)

    _append_log({
        "event_type":  "application_question_generated",
        "entity_type": "job",
        "entity_id":   job_id,
        "entity_name": f"{job.get('company_name','?')} — {job.get('title','?')}",
        "detail":      (
            f"class={cls} version={version} chars={len(answer)} "
            f"tokens_in={in_toks} tokens_out={out_toks}"
        ),
    })
    return record


def save_edit(job_id: str, question_id: str, answer_text: str) -> dict:
    """Persist a manually-edited answer as a new draft version. Runs the
    edited text through ``sanitize_answer_text`` first (so the dash policy
    and other invariants apply to operator edits, not just Claude output).
    Never mutates a prior version — every save appends a fresh entry to
    ``draft_history`` with ``source = "manual_edit"`` so the version picker
    can distinguish edits from regenerations. Raises ``ValueError`` if the
    sanitized text is empty."""
    data = load_questions()
    _, record = _find_question(data, job_id, question_id)
    if not record:
        raise ValueError(f"Question {question_id} not found for job {job_id}")

    cleaned = sanitize_answer_text(answer_text or "")
    if not cleaned:
        raise ValueError("Edited answer is empty after sanitization.")

    version = len(record.get("draft_history") or []) + 1
    record.setdefault("draft_history", []).append({
        "version":      version,
        "answer":       cleaned,
        "char_count":   len(cleaned),
        "generated_at": now_utc(),
        "source":       "manual_edit",
    })
    save_questions(data)

    _append_log({
        "event_type":  "application_question_edited",
        "entity_type": "job",
        "entity_id":   job_id,
        "detail":      f"version={version} chars={len(cleaned)}",
    })
    return record


def finalize_answer(job_id: str, question_id: str) -> dict:
    """Mark the latest draft as the finalized answer."""
    data = load_questions()
    _, record = _find_question(data, job_id, question_id)
    if not record:
        raise ValueError(f"Question {question_id} not found for job {job_id}")
    history = record.get("draft_history") or []
    if not history:
        raise ValueError("Cannot finalize — no draft has been generated yet.")
    latest = history[-1]
    record["finalized_answer"] = latest.get("answer")
    record["finalized_at"]     = now_utc()
    record["status"]           = "finalized"
    save_questions(data)

    _append_log({
        "event_type":  "application_question_finalized",
        "entity_type": "job",
        "entity_id":   job_id,
        "detail":      f"version={latest.get('version')} chars={latest.get('char_count')}",
    })
    return record


def unfinalize_answer(job_id: str, question_id: str) -> dict:
    """Revert a finalized question back to draft state. The draft history
    is preserved — only the finalized snapshot is cleared."""
    data = load_questions()
    _, record = _find_question(data, job_id, question_id)
    if not record:
        raise ValueError(f"Question {question_id} not found for job {job_id}")
    record["finalized_answer"] = None
    record["finalized_at"]     = None
    record["status"]           = "draft"
    save_questions(data)
    return record
