"""
research_company.py — Claude judgment layer for company research.

Uses a two-tier approach to minimize API costs:
  Tier 1 (free): Training knowledge for stable facts — industry, size, HQ,
                 Glassdoor, Blind, sponsorship history, remote hiring patterns,
                 known ethics issues.
  Tier 2 (1 web search): Only recent layoffs and new ethics flags from the
                         last 12 months that training data may not have.

Cost target: ~$0.03-0.05 per company vs $0.27+ with open-ended web search.

Usage:
    python scripts/research_company.py --name "Stripe"
    python scripts/research_company.py --name "Shopify"
    python scripts/research_company.py --company-id <uuid>  # refresh existing record
"""

import argparse
import json
import uuid as uuid_lib

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    CLAUDE_MODEL_FAST,
    COMPANY_REGISTRY_PATH,
    company_auto_exclude_reason,
    load_json,
    save_json,
    now_utc,
    today,
)

# ── Tier 1 prompt — training knowledge only, no web search ────────────────────

TIER1_SYSTEM = """
You are a company research assistant helping a Staff Software Engineer evaluate
potential employers for remote roles in Canada or Ireland. Answer from your
training knowledge. Return ONLY valid JSON — no preamble, no markdown fences.

## Sponsorship score (0-15)
13-15: Explicitly advertises sponsorship; documented history within 2 years
9-12:  Multiple credible third-party confirmations
5-8:   Some signal, unconfirmed or older than 2 years
1-4:   No history; size/type suggests possible
0:     Documented refusal or too small to realistically sponsor

## Remote fit (0-5)
5: Explicitly remote-first, confirmed roles in Canada or Ireland
4: Remote-friendly, some fully remote roles in target countries
3: Hybrid, some office presence required
1-2: Mostly in-office
0: In-person only

## Ethics flag categories
direct_harm, indirect_harm, monopoly, human_rights, protected_class,
union_busting, environmental, surveillance, predatory_practices, other

## Ethics flag status values
confirmed, alleged, historical, clean

## Required JSON output format
{
  "name": "<canonical company name>",
  "industry": "<industry string>",
  "size_tier": "<startup|mid|large|enterprise>",
  "country_hq": "<ISO country code>",
  "job_portal_url": "<careers page URL or empty string>",
  "sponsorship_score": <integer 0-15>,
  "sponsorship_notes": "<one sentence source of sponsorship signal>",
  "remote_fit": <integer 0-5>,
  "glassdoor_rating": <float or null>,
  "glassdoor_engineering_sentiment": "<positive|mixed|negative|unknown>",
  "blind_sentiment": "<positive|mixed|negative|unknown>",
  "ethics_hard_exclude": <true|false>,
  "ethics_flags": [
    {
      "category": "<category>",
      "status": "<status>",
      "description": "<one sentence>",
      "source": "<publication or org>",
      "source_date": "<YYYY-MM-DD or empty string>"
    }
  ],
  "ethics_notes": "<one sentence summary or empty string>",
  "scrape_tier": "<1_direct|2_alert|3_manual|4_rss>"
}
"""

# ── Tier 2 prompt — one targeted web search for recent news only ───────────────

TIER2_SYSTEM = """
You are checking for recent news about a company published in the last 12 months.
Use exactly one web search: "<company name> Glassdoor Blind rating layoffs ethics lawsuit 2025 2026"

Return ONLY valid JSON — no preamble, no markdown fences.

## Required JSON output format
{
  "recent_layoffs": <true|false>,
  "layoff_notes": "<one sentence describing layoffs or empty string>",
  "glassdoor_rating": <float or null>,
  "glassdoor_engineering_sentiment": "<positive|mixed|negative|unknown>",
  "blind_sentiment": "<positive|mixed|negative|unknown>",
  "new_ethics_flags": [
    {
      "category": "<direct_harm|indirect_harm|monopoly|human_rights|protected_class|union_busting|environmental|surveillance|predatory_practices|other>",
      "status": "<confirmed|alleged|historical|clean>",
      "description": "<one sentence>",
      "source": "<publication or org>",
      "source_date": "<YYYY-MM-DD or empty string>"
    }
  ]
}

Set glassdoor_rating to the current rating if found in search results, otherwise null.
Set glassdoor_engineering_sentiment based on review themes if discernible, otherwise "unknown".
Set blind_sentiment based on Blind app reviews if discernible, otherwise "unknown".
Only include ethics flags for events in the last 12 months that are genuinely
concerning — not minor criticism or normal business disputes.
If nothing significant found, return recent_layoffs: false, layoff_notes: "",
glassdoor_rating: null, glassdoor_engineering_sentiment: "unknown",
blind_sentiment: "unknown", new_ethics_flags: [].
"""


def _parse_json_response(message) -> dict:
    """Extract and parse JSON from a Claude message response."""
    raw = ""
    for block in message.content:
        if block.type == "text":
            raw += block.text
    raw = raw.strip()

    # Extract JSON block wherever it appears
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    elif "{" in raw:
        raw = raw[raw.index("{"):raw.rindex("}")+1].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned non-JSON response:\n{raw}") from e


def research_company(name: str, model: str = CLAUDE_MODEL_FAST) -> dict:
    """
    Research a company using two-tier approach.
    Tier 1: Training knowledge using specified model (default: Haiku).
    Tier 2: One web search for recent layoffs and ethics flags.
    Returns merged dict matching the company registry schema.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Tier 1: training knowledge ─────────────────────────────────────────────
    print("  [1/2] Querying training knowledge...", flush=True)
    msg1 = client.messages.create(
        model=model,
        max_tokens=1500,
        system=TIER1_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Research this company for a Staff Software Engineer targeting "
                f"remote roles in Canada or Ireland:\n\nCompany: {name}"
            )
        }],
    )
    tier1 = _parse_json_response(msg1)

    # ── Tier 2: one web search for recent news ─────────────────────────────────
    print("  [2/2] Checking recent news (1 web search)...", flush=True)
    msg2 = client.messages.create(
        model=model,
        max_tokens=800,
        system=TIER2_SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        messages=[{
            "role": "user",
            "content": f"Check for recent layoffs and ethics news about: {name}"
        }],
    )
    tier2 = _parse_json_response(msg2)

    # ── Merge results ──────────────────────────────────────────────────────────
    result = tier1.copy()
    result["recent_layoffs"] = tier2.get("recent_layoffs", False)
    result["layoff_notes"]   = tier2.get("layoff_notes", "")

    # Override Glassdoor rating and sentiment with live values if tier 2 found them
    if tier2.get("glassdoor_rating") is not None:
        result["glassdoor_rating"] = tier2["glassdoor_rating"]
    if tier2.get("glassdoor_engineering_sentiment") not in (None, "unknown"):
        result["glassdoor_engineering_sentiment"] = tier2["glassdoor_engineering_sentiment"]
    if tier2.get("blind_sentiment") not in (None, "unknown"):
        result["blind_sentiment"] = tier2["blind_sentiment"]

    # Merge new ethics flags from tier 2 into existing flags from tier 1
    existing_flags = result.get("ethics_flags", [])
    new_flags      = tier2.get("new_ethics_flags", [])
    result["ethics_flags"] = existing_flags + new_flags

    # Deterministic post-process — defense contractor / employee surveillance
    # / mass surveillance always trigger hard exclude regardless of the LLM's
    # overall judgment. Rule SSOT: config.company_auto_exclude_reason. See
    # the "Ingest-time hard excludes" auto-trigger table in CLAUDE.md.
    if not result.get("ethics_hard_exclude"):
        auto_reason = company_auto_exclude_reason(result)
        if auto_reason:
            result["ethics_hard_exclude"] = True
            print(f"  Auto-exclude: {auto_reason}", flush=True)

    # Clamp scores
    result["sponsorship_score"] = max(0, min(15, int(result.get("sponsorship_score", 7))))
    result["remote_fit"]        = max(0, min(5,  int(result.get("remote_fit", 3))))

    return result


def build_registry_record(research: dict, existing_id: str | None = None) -> dict:
    """Merge research results into a full company registry record."""
    now = now_utc()
    return {
        "company_id":      existing_id or str(uuid_lib.uuid4()),
        "name":            research.get("name", ""),
        "industry":        research.get("industry", "Unknown"),
        "size_tier":       research.get("size_tier", "mid"),
        "country_hq":      research.get("country_hq", ""),
        "job_portal_url":  research.get("job_portal_url", ""),
        "scrape_tier":     research.get("scrape_tier", "3_manual"),
        "sponsorship_score":   research.get("sponsorship_score", 7),
        "sponsorship_notes":   research.get("sponsorship_notes", ""),
        "remote_fit":          research.get("remote_fit", 3),
        "glassdoor_rating":    research.get("glassdoor_rating"),
        "glassdoor_engineering_sentiment": research.get("glassdoor_engineering_sentiment", "unknown"),
        "blind_sentiment":     research.get("blind_sentiment", "unknown"),
        "recent_layoffs":      research.get("recent_layoffs", False),
        "layoff_notes":        research.get("layoff_notes", ""),
        "ethics_hard_exclude": research.get("ethics_hard_exclude", False),
        "ethics_flags":        research.get("ethics_flags", []),
        "ethics_notes":        research.get("ethics_notes", ""),
        "confirmed_clean":     False,
        "record_created":      now,
        "record_updated":      now,
    }


def upsert_company(record: dict) -> tuple[str, bool]:
    """
    Write or update a company record in company_registry.json.
    Returns (company_id, created) where created=True if new record.
    """
    companies = load_json(COMPANY_REGISTRY_PATH)
    existing_idx = next(
        (i for i, c in enumerate(companies)
         if c["name"].lower() == record["name"].lower()),
        None
    )
    if existing_idx is not None:
        record["company_id"]      = companies[existing_idx]["company_id"]
        record["record_created"]  = companies[existing_idx]["record_created"]
        record["confirmed_clean"] = companies[existing_idx].get("confirmed_clean", False)
        companies[existing_idx]   = record
        save_json(COMPANY_REGISTRY_PATH, companies)
        return record["company_id"], False
    else:
        companies.append(record)
        save_json(COMPANY_REGISTRY_PATH, companies)
        return record["company_id"], True


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Research a company for the job pipeline.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name",       metavar="NAME", help="Company name to research")
    group.add_argument("--company-id", metavar="UUID", help="Refresh existing company by ID")
    args = parser.parse_args()

    if args.company_id:
        companies = load_json(COMPANY_REGISTRY_PATH)
        match = next((c for c in companies if c["company_id"] == args.company_id), None)
        if not match:
            print(f"Error: company ID {args.company_id} not found.")
            return
        name = match["name"]
    else:
        name = args.name

    print(f"\nResearching {name}...")
    research = research_company(name)

    print(f"\n  Industry:       {research.get('industry')}")
    print(f"  Size:           {research.get('size_tier')}")
    print(f"  HQ:             {research.get('country_hq')}")
    print(f"  Sponsorship:    {research.get('sponsorship_score')}/15 — {research.get('sponsorship_notes')}")
    print(f"  Remote fit:     {research.get('remote_fit')}/5")
    print(f"  Glassdoor:      {research.get('glassdoor_rating')} ({research.get('glassdoor_engineering_sentiment')})")
    print(f"  Blind:          {research.get('blind_sentiment')}")
    print(f"  Recent layoffs: {research.get('recent_layoffs')} — {research.get('layoff_notes')}")
    print(f"  Ethics exclude: {research.get('ethics_hard_exclude')}")
    if research.get("ethics_flags"):
        for flag in research["ethics_flags"]:
            print(f"    [{flag['status']}] {flag['category']}: {flag['description']}")
    else:
        print(f"  Ethics flags:   none")

    record = build_registry_record(research, existing_id=args.company_id)
    company_id, created = upsert_company(record)
    action = "Created" if created else "Updated"
    print(f"\n{action} company record: {company_id}")
    print(f"Written to {COMPANY_REGISTRY_PATH}")


if __name__ == "__main__":
    main()
