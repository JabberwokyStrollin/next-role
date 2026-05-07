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


def score_jd(jd_text: str) -> dict:
    """
    Call Claude with the JD text and return the scoring dict.
    Returns: {"seniority_score": int, "domain_fit_score": int, "score_notes": str}
    Raises: ValueError if response cannot be parsed as valid JSON with required keys.
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

    required = {"seniority_score", "domain_fit_score", "score_notes"}
    missing = required - result.keys()
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}\nGot: {result}")

    # Clamp scores to valid ranges
    result["seniority_score"]  = max(0, min(25, int(result["seniority_score"])))
    result["domain_fit_score"] = max(0, min(20, int(result["domain_fit_score"])))

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
        job_id = args.job_id

    if not jd_text.strip():
        print("Error: JD text is empty.", file=sys.stderr)
        sys.exit(1)

    print("Scoring JD with Claude...", flush=True)
    scores = score_jd(jd_text)

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
