"""
comp_estimate.py — Claude-driven compensation range estimator.

Usage:
    python comp_estimate.py --job-id <uuid>
    python comp_estimate.py --job-id <uuid> --currency CAD
    python comp_estimate.py --job-id <uuid> --dry-run

Reads the job from data/job_pipeline.json, optionally enriches with the
company record from data/company_registry.json, asks Opus 4.7 for a salary
+ bonus estimate in local currency, and writes the result to
data/comp_estimates.json keyed by job_id.

The output is consumed by the /today cover-letters surface (button next to
"Generate CL") and by the per-job detail page.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    COMP_ESTIMATES_PATH,
    COMPANY_REGISTRY_PATH,
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    RESUME_PATH,
    load_json,
    save_json,
    now_utc,
)


# Opus 4.7 has deeper salary-band knowledge than the pipeline default
# (Sonnet 4.5) and matches what the user has been doing manually on Claude.ai.
COMP_MODEL = "claude-opus-4-7"
MAX_TOKENS = 1500


# ─── Currency mapping ─────────────────────────────────────────────────────────

_CAD_HINTS = (
    "canada", " ca,", " ca ", "(ca)",
    "toronto", "vancouver", "montreal", "ottawa",
    "calgary", "edmonton", "waterloo", "kitchener",
)
_EUR_HINTS = ("ireland", " ie,", " ie ", "(ie)", "dublin", "cork", "galway", "limerick")
_GBP_HINTS = ("united kingdom", " uk,", " uk ", "(uk)", "london", "manchester", "edinburgh")
_USD_HINTS = ("united states", " usa", " us,", " us ", "(us)", "remote (us)")

_HQ_TO_CURRENCY = {
    "CA": "CAD", "IE": "EUR", "UK": "GBP", "GB": "GBP", "US": "USD",
}


def derive_currency(location: str, company_hq: str | None = None) -> str:
    """Map a job location to a currency code. Falls back to company HQ, then USD."""
    loc = (location or "").lower()
    if any(h in loc for h in _EUR_HINTS):
        return "EUR"
    if any(h in loc for h in _CAD_HINTS):
        return "CAD"
    if any(h in loc for h in _GBP_HINTS):
        return "GBP"
    if any(h in loc for h in _USD_HINTS):
        return "USD"
    if company_hq:
        return _HQ_TO_CURRENCY.get(company_hq.upper(), "USD")
    return "USD"


# ─── Prompt construction ──────────────────────────────────────────────────────

def build_system_prompt(resume_text: str, currency: str) -> str:
    return f"""You are a compensation analyst for senior software engineers seeking new roles. You produce realistic, well-grounded salary and bonus estimates that the candidate can use to anchor their negotiations. Return ONLY valid JSON — no preamble, no markdown fences.

## The candidate's resume

{resume_text}

## Methodology

### Base salary (min / max / target)

- min = realistic floor a non-negotiating candidate would accept; roughly p50 of the market band for this role at this company in this location.
- max = realistic ceiling a strong, leveraged candidate lands; roughly p90.
- target = the number to ASK in the application form, anchored at ~p85. Logic: companies expect a 5-10% counter-down on base; asking at p85 lands the post-negotiation outcome around p80, which is the candidate's sweet spot — high in the acceptable range, but with room for the company to negotiate so they don't disengage.

All amounts in {currency}. Use whole-thousands precision (e.g. 245000, not 244537).

### Bonus components

For each of year_end_bonus, signon, relocation, equity — classify and (when appropriate) recommend a target ask.

classification is one of: Expected, Possible, Unusual, Stated-in-JD
- Expected — almost certainly part of comp for this company/role type. Year-end bonus at any public bank or large tech co; sign-on at FAANG-tier; equity at public tech companies.
- Possible — might be on the table but not standard. Sign-on at a Series C startup, year-end bonus at a private mid-size co.
- Unusual — not customary; asking would signal naivete and may hurt negotiation. Sign-on at a small agency, equity at a bank (banks pay cash bonuses instead), relocation at a fully remote role, year-end bonus at an early-stage startup that uses equity in lieu.
- Stated-in-JD — the JD text itself mentions this benefit. Pull the actual numbers from the JD when quoted (e.g. "$10k relocation assistance" or "15% annual bonus target").

reason — one short sentence explaining the classification (e.g. "RBC is a public bank with standard 15-20% target bonus for Staff ICs").

Target fields (target / target_pct / target_amount / target_annual) — fill in only when classification is Expected, Possible, or Stated-in-JD. Set to null when Unusual.

### Confidence
- HIGH — large public company with well-known comp bands (FAANG, major banks, major consulting). Plenty of training-data signal.
- MED — established private or smaller public; bands inferable from similar companies in the same market segment.
- LOW — small startup, obscure company, or sparse training signal. Recommend the candidate sanity-check via Levels.fyi/Glassdoor before submitting.

### Asymmetric risk note

The cost of asking for an inappropriate bonus (e.g. sign-on at a small agency) is HIGHER than the cost of not asking for one we could have gotten. When uncertain, classify as Unusual rather than Expected — it is strictly safer.

## Required JSON output shape

{{
  "currency": "{currency}",
  "base": {{ "min": <int>, "max": <int>, "target": <int> }},
  "year_end_bonus": {{
    "classification": "<Expected|Possible|Unusual|Stated-in-JD>",
    "reason": "<one short sentence>",
    "target_pct": <int or null>,
    "target_amount": <int or null>
  }},
  "signon":     {{ "classification": "<...>", "reason": "<...>", "target": <int or null> }},
  "relocation": {{ "classification": "<...>", "reason": "<...>", "target": <int or null> }},
  "equity":     {{ "classification": "<...>", "reason": "<...>", "target_annual": <int or null> }},
  "confidence": "<HIGH|MED|LOW>",
  "reasoning":  "<2-3 sentences explaining the overall rationale>"
}}

Return ONLY the JSON object. No preamble, no markdown fences, no commentary."""


def build_user_message(job: dict, company: dict | None, currency: str) -> str:
    parts = [
        "Estimate compensation for this job.",
        "",
        f"Company: {job.get('company_name', '?')}",
        f"Title: {job.get('title', '?')}",
        f"Location: {job.get('location', '?')}",
        f"Currency: {currency}",
    ]
    if company:
        ctx_bits = []
        if company.get("industry"):         ctx_bits.append(f"industry={company['industry']}")
        if company.get("size_tier"):        ctx_bits.append(f"size_tier={company['size_tier']}")
        if company.get("country_hq"):       ctx_bits.append(f"hq_country={company['country_hq']}")
        if company.get("glassdoor_rating"): ctx_bits.append(f"glassdoor={company['glassdoor_rating']}")
        if company.get("recent_layoffs"):   ctx_bits.append("recent_layoffs=yes")
        if ctx_bits:
            parts.append(f"Company context: {', '.join(ctx_bits)}")

    jd = (job.get("jd_text") or "").strip()
    parts.append("")
    parts.append("Job Description:")
    parts.append(jd if jd else "(JD text unavailable — estimate from title + company + location only and lower your confidence accordingly.)")
    return "\n".join(parts)


# ─── Claude call + response parsing ───────────────────────────────────────────

def parse_comp_json(raw: str) -> dict:
    """Tolerant JSON parser: strips markdown fences and leading prose."""
    cleaned = raw.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    elif "{" in cleaned:
        cleaned = cleaned[cleaned.index("{") : cleaned.rindex("}") + 1]
    return json.loads(cleaned)


_REQUIRED_TOP = {"currency", "base", "year_end_bonus", "signon", "relocation",
                 "equity", "confidence", "reasoning"}
_REQUIRED_BASE = {"min", "max", "target"}
_VALID_CLASSIFICATIONS = {"Expected", "Possible", "Unusual", "Stated-in-JD"}
_VALID_CONFIDENCE = {"HIGH", "MED", "LOW"}


def validate(result: dict) -> None:
    """Raise ValueError if the response doesn't match the expected schema."""
    missing = _REQUIRED_TOP - result.keys()
    if missing:
        raise ValueError(f"Response missing top-level keys: {missing}")
    if not isinstance(result["base"], dict) or _REQUIRED_BASE - result["base"].keys():
        raise ValueError(f"base must contain min/max/target, got: {result.get('base')}")
    for comp_key in ("year_end_bonus", "signon", "relocation", "equity"):
        comp = result[comp_key]
        if not isinstance(comp, dict):
            raise ValueError(f"{comp_key} must be an object, got: {type(comp).__name__}")
        if "classification" not in comp or "reason" not in comp:
            raise ValueError(f"{comp_key} missing classification/reason: {comp}")
        if comp["classification"] not in _VALID_CLASSIFICATIONS:
            raise ValueError(
                f"{comp_key}.classification must be one of {_VALID_CLASSIFICATIONS}, "
                f"got: {comp['classification']!r}"
            )
    if result["confidence"] not in _VALID_CONFIDENCE:
        raise ValueError(f"confidence must be one of {_VALID_CONFIDENCE}, got: {result['confidence']!r}")


def call_claude(system: str, user_message: str) -> tuple[str, int, int]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=COMP_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    text = msg.content[0].text
    return text, msg.usage.input_tokens, msg.usage.output_tokens


# ─── Storage ──────────────────────────────────────────────────────────────────

def load_estimates() -> list[dict]:
    if not COMP_ESTIMATES_PATH.exists():
        return []
    return load_json(COMP_ESTIMATES_PATH) or []


def upsert_estimate(record: dict) -> None:
    estimates = load_estimates()
    job_id = record["job_id"]
    estimates = [e for e in estimates if e.get("job_id") != job_id]
    estimates.append(record)
    save_json(COMP_ESTIMATES_PATH, estimates)


def append_log(event: dict) -> None:
    log = load_json(PROCESS_LOG_PATH) or []
    log.append({"timestamp": now_utc(), **event})
    save_json(PROCESS_LOG_PATH, log)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a comp estimate for one job.")
    parser.add_argument("--job-id",   required=True, help="job_id from data/job_pipeline.json")
    parser.add_argument("--currency", default=None,  help="Override deterministic currency mapping (e.g. CAD)")
    parser.add_argument("--dry-run",  action="store_true", help="Print result to stdout but do not persist")
    args = parser.parse_args()

    jobs = load_json(JOB_PIPELINE_PATH) or []
    job  = next((j for j in jobs if j.get("job_id") == args.job_id), None)
    if not job:
        print(f"Error: job_id {args.job_id} not found in {JOB_PIPELINE_PATH}", file=sys.stderr)
        return 2

    companies = load_json(COMPANY_REGISTRY_PATH) or []
    company   = next((c for c in companies if c.get("company_id") == job.get("company_id")), None)

    currency = args.currency or derive_currency(job.get("location", ""), (company or {}).get("country_hq"))

    if not RESUME_PATH.exists():
        print(f"Error: resume not found at {RESUME_PATH}", file=sys.stderr)
        return 2
    resume_text = RESUME_PATH.read_text(encoding="utf-8")

    print(f"Estimating comp for: {job.get('company_name','?')} — {job.get('title','?')}")
    print(f"  Currency: {currency} (location: {job.get('location','?')})")
    print(f"  Model:    {COMP_MODEL}")

    system  = build_system_prompt(resume_text, currency)
    user    = build_user_message(job, company, currency)

    try:
        raw, in_toks, out_toks = call_claude(system, user)
    except Exception as e:
        print(f"Error: Claude API call failed: {e}", file=sys.stderr)
        return 3
    print(f"  Tokens — in: {in_toks}  out: {out_toks}")

    try:
        result = parse_comp_json(raw)
    except json.JSONDecodeError as e:
        print(f"Error: response was not valid JSON: {e}\n--- Raw response ---\n{raw}", file=sys.stderr)
        return 4

    try:
        validate(result)
    except ValueError as e:
        print(f"Error: schema validation failed: {e}\n--- Parsed result ---\n{json.dumps(result, indent=2)}", file=sys.stderr)
        return 5

    record = {
        "job_id":       args.job_id,
        "company_name": job.get("company_name"),
        "title":        job.get("title"),
        "location":     job.get("location"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model":        COMP_MODEL,
        "estimate":     result,
    }

    if args.dry_run:
        print("--- DRY RUN — not persisting ---")
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    upsert_estimate(record)
    append_log({
        "event_type":  "comp_estimate_generated",
        "entity_type": "job",
        "entity_id":   args.job_id,
        "entity_name": f"{job.get('company_name','?')} — {job.get('title','?')}",
        "detail":      f"base target {currency} {result['base']['target']}; confidence {result['confidence']}",
    })

    base = result["base"]
    print(f"\nBase: {currency} {base['min']:,} – {base['max']:,}   TARGET: {currency} {base['target']:,}")
    print(f"Confidence: {result['confidence']}")
    print(f"Saved to {COMP_ESTIMATES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
