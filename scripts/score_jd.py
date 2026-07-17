"""
score_jd.py — Claude judgment layer for seniority and domain scoring.

Usage:
    python score_jd.py --jd "path/to/jd.txt"
    python score_jd.py --job-id <uuid>          # reads jd_text from job_pipeline.json
    python score_jd.py --stdin                  # reads JD text from stdin

Called by ingest.py after mechanical scores are computed. Writes
seniority_score, domain_fit_score, and score_notes back to the job record.
Also usable standalone for re-scoring an existing job.
"""

import argparse
import json
import sys

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    JOB_PIPELINE_PATH,
    SCORING_RUBRIC_PATH,
    apply_title_cap,
    load_json,
    save_json,
    now_utc,
)


def _load_rubric() -> str:
    if not SCORING_RUBRIC_PATH.exists():
        raise FileNotFoundError(
            f"Scoring rubric not found: {SCORING_RUBRIC_PATH}\n"
            "Copy profile.example/scoring_rubric.md to profile/ and customize it."
        )
    return SCORING_RUBRIC_PATH.read_text(encoding="utf-8")


def score_jd(jd_text: str, title: str | None = None) -> dict:
    """
    Call Claude with the JD text and return the scoring dict.
    Returns: {"seniority_score": int, "domain_fit_score": int, "score_notes": str}
    Only the two numeric scores are required; ``score_notes`` defaults to "" and
    ``role_exposure`` to None when the model omits them (display/advisory fields
    must not break ingest).
    Raises: ValueError if the response isn't valid JSON or is missing either
    required numeric score.

    If ``title`` is provided, ``seniority_score`` is mechanically capped by the
    title bucket (Senior/Principal → 15, Distinguished/VP/Junior → 0, Staff/
    Architect/Lead → 25). Pass ``None`` to skip the cap (e.g. when scoring a
    raw JD outside the pipeline).
    """
    # Sanitize surrogate characters that break JSON serialization on Windows
    jd_text = jd_text.encode("utf-8", errors="ignore").decode("utf-8")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        system=_load_rubric(),
        messages=[
            {"role": "user", "content": f"Score this job description:\n\n{jd_text}"}
        ],
    )

    raw = message.content[0].text.strip()

    # Strip accidental markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned non-JSON response:\n{raw}") from e

    # Only the two numeric scores are structurally required — they drive the
    # composite. A missing one means the model produced an unusable response.
    required = {"seniority_score", "domain_fit_score"}
    missing = required - result.keys()
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}\nGot: {result}")

    # score_notes is display-only (dashboard / ingest print / stored field) and
    # never feeds the composite. Intentionally NOT required: an occasional model
    # miss must not break ingest. Default to "" so downstream reads stay valid.
    result["score_notes"] = str(result.get("score_notes") or "").strip()

    # role_exposure is the gov-screen JD-level judgment. Intentionally NOT
    # required: a model miss must not break ingest. ingest.py resolves the
    # final value via config.classify_role_exposure (which applies the
    # deterministic title rules on top of this raw judgment).
    exp = result.get("role_exposure")
    result["role_exposure"] = exp if exp in ("insulated", "ambiguous", "exposed") else None

    # Clamp scores to valid ranges
    result["seniority_score"]  = max(0, min(25, int(result["seniority_score"])))
    result["domain_fit_score"] = max(0, min(20, int(result["domain_fit_score"])))

    # Apply title-based cap to seniority. Done here (after Claude) rather than
    # in the prompt because the model has shown a tendency to reclassify
    # Principal titles based on JD scope language, which over-penalizes them.
    if title:
        raw = result["seniority_score"]
        capped = apply_title_cap(raw, title)
        if capped != raw:
            result["seniority_score"]    = capped
            result["seniority_raw"]      = raw
            result["seniority_cap_title"] = title

    return result


def update_job_record(job_id: str, scores: dict) -> None:
    """Write scoring results back to the job record in job_pipeline.json."""
    jobs = load_json(JOB_PIPELINE_PATH)
    matched = False
    for job in jobs:
        if job["job_id"] == job_id:
            job["seniority_score"]  = scores["seniority_score"]
            job["domain_fit_score"] = scores["domain_fit_score"]
            job["score_notes"]      = scores["score_notes"]
            job["scored_at"]        = now_utc()
            matched = True
            break
    if not matched:
        raise ValueError(f"Job ID not found in pipeline: {job_id}")
    save_json(JOB_PIPELINE_PATH, jobs)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Score a job description with Claude.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--jd",      metavar="FILE", help="Path to a text file containing the JD")
    group.add_argument("--job-id",  metavar="UUID", help="Score an existing job in job_pipeline.json")
    group.add_argument("--stdin",   action="store_true", help="Read JD text from stdin")
    args = parser.parse_args()

    title: str | None = None
    if args.jd:
        with open(args.jd, encoding="utf-8") as f:
            jd_text = f.read()
        job_id = None
    elif args.stdin:
        jd_text = sys.stdin.read()
        job_id = None
    else:  # --job-id
        jobs = load_json(JOB_PIPELINE_PATH)
        match = next((j for j in jobs if j["job_id"] == args.job_id), None)
        if not match:
            print(f"Error: job ID {args.job_id} not found.", file=sys.stderr)
            sys.exit(1)
        jd_text = match.get("jd_text", "")
        title   = match.get("title", "")
        job_id  = args.job_id

    if not jd_text.strip():
        print("Error: JD text is empty.", file=sys.stderr)
        sys.exit(1)

    print("Scoring JD with Claude...", flush=True)
    scores = score_jd(jd_text, title=title)

    print(f"  Seniority score:  {scores['seniority_score']}/25")
    print(f"  Domain fit score: {scores['domain_fit_score']}/20")
    print(f"  Notes: {scores['score_notes']}")

    if job_id:
        update_job_record(job_id, scores)
        print(f"\nJob record {job_id} updated.")
    else:
        print("\nNo job ID provided — scores printed only, not written to pipeline.")


if __name__ == "__main__":
    main()
