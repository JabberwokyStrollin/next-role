"""
scripts/metrics.py
------------------
Read-only analytics module for the /metrics route in serve.py.

Produces cohort comparisons (in-flight vs rejected/ghosted) across every
scoring component, a composite score distribution, and funnel-speed stats.

No Claude calls, no mutations. Reads three JSON files:
  - data/job_pipeline.json
  - data/company_registry.json
  - data/application_tracker.json

All denominators and component names are read from the SSOT in config.py —
no hardcoded /130, /25, etc.
"""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import date
from statistics import mean, median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import (
    load_json,
    JOB_PIPELINE_PATH,
    COMPANY_REGISTRY_PATH,
    APPLICATION_TRACKER_PATH,
    COMPONENTS,
    COMPOSITE_MAX,
    IN_FLIGHT_STATUSES,
    TARGET_COUNTRIES,
    US_SPONSORSHIP_SCORE,
    composite_score,
    compute_freshness_bonus,
    derive_country,
)

# ---------------------------------------------------------------------------
# Cohort definitions
# ---------------------------------------------------------------------------
#
# IN_FLIGHT_STATUSES is imported from the company-filter SSOT in
# scripts/config.py — do not redefine. DEAD_STATUSES and POSITIVE_STATUSES
# are presentation-only categorizations for this analytics surface; they
# don't gate the pipeline anywhere and are scoped to this module.

# Statuses that represent a dead end (explicit rejection or silence)
DEAD_STATUSES = frozenset({"rejected", "ghosted"})

# Positive outcomes — small set but worth tracking separately
POSITIVE_STATUSES = frozenset({"offer"})


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _load_data() -> tuple[dict, dict, list]:
    """
    Returns (jobs_by_id, companies_by_id, apps).
    jobs_by_id  : {job_id: job_record}
    companies_by_id : {company_id: company_record}
    apps        : list of all application_tracker records
    """
    jobs      = load_json(JOB_PIPELINE_PATH)
    companies = load_json(COMPANY_REGISTRY_PATH)
    apps      = load_json(APPLICATION_TRACKER_PATH)

    jobs_by_id     = {j["job_id"]: j for j in jobs if "job_id" in j}
    companies_by_id = {c["company_id"]: c for c in companies if "company_id" in c}
    return jobs_by_id, companies_by_id, apps


# ---------------------------------------------------------------------------
# Component score extraction
# ---------------------------------------------------------------------------

def _component_scores(job: dict, company: dict | None) -> dict[str, float | None]:
    """
    Returns a dict of {component_key: weighted_points} for every entry in
    COMPONENTS.  Uses the same multipliers composite_score uses so the
    per-component bars add up to the composite total.

    'weighted_points' means the raw stored score multiplied by the
    component's weight/native_max multiplier — i.e. the contribution
    to COMPOSITE_MAX, not the raw stored value.

    Returns None for a component whose underlying value is missing/null.
    """
    co = company or {}
    scores: dict[str, float | None] = {}

    field_map = {
        "stack":       ("stack_match_score",      job),
        "seniority":   ("seniority_score",         job),
        "domain":      ("domain_fit_score",        job),
        "velocity":    ("hiring_velocity_score",   job),
        "freshness":   (None,                      None),   # computed
        "sponsorship": ("sponsorship_score",       co),
        "remote":      ("remote_fit",              co),
    }

    # Mirror composite_score's US sponsorship substitution so the per-component
    # bars still sum to the composite for US roles (preserves the invariant).
    us_role = "US" in TARGET_COUNTRIES and derive_country(job.get("location", "")) == "US"

    for key, comp in COMPONENTS.items():
        if key == "freshness":
            raw = compute_freshness_bonus(job)
        elif key == "sponsorship" and us_role:
            raw = US_SPONSORSHIP_SCORE
        else:
            src_field, src_dict = field_map.get(key, (None, None))
            if src_field is None or src_dict is None:
                scores[key] = None
                continue
            raw = src_dict.get(src_field)
            if raw is None:
                scores[key] = None
                continue

        # Weighted contribution (same math as composite_score)
        scores[key] = round(raw * comp.multiplier, 2)

    return scores


# ---------------------------------------------------------------------------
# Cohort builder
# ---------------------------------------------------------------------------

def _build_cohorts(
    jobs_by_id: dict,
    companies_by_id: dict,
    apps: list,
) -> dict[str, list[dict]]:
    """
    Returns {'in_flight': [...], 'dead': [...], 'positive': [...], 'other': [...]}
    Each entry is an enriched app record:
      app fields  +  composite_score  +  components  +  days_to_response
    """
    cohorts: dict[str, list[dict]] = {
        "in_flight": [],
        "dead":      [],
        "positive":  [],
        "other":     [],
    }

    for app in apps:
        status    = app.get("status", "")
        job_id    = app.get("job_id")
        company_id = app.get("company_id")

        job     = jobs_by_id.get(job_id) or {}
        company = companies_by_id.get(company_id)

        # Composite — prefer the snapshotted value; recompute as fallback
        composite = app.get("composite_score_at_apply")
        if composite is None and job:
            composite = composite_score(job, company)

        # Per-component weighted contributions
        components = _component_scores(job, company) if job else {}

        # Days to response
        days_to_response: int | None = None
        date_applied  = app.get("date_applied")
        response_date = app.get("response_date")
        if date_applied and response_date:
            try:
                d1 = date.fromisoformat(date_applied)
                d2 = date.fromisoformat(response_date)
                days_to_response = (d2 - d1).days
            except ValueError:
                pass

        enriched = {
            **app,
            "_composite":        composite,
            "_components":       components,
            "_days_to_response": days_to_response,
        }

        if status in IN_FLIGHT_STATUSES:
            cohorts["in_flight"].append(enriched)
        elif status in DEAD_STATUSES:
            cohorts["dead"].append(enriched)
        elif status in POSITIVE_STATUSES:
            cohorts["positive"].append(enriched)
        else:
            cohorts["other"].append(enriched)  # withdrawn, etc.

    return cohorts


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------

def _avg_components(records: list[dict]) -> dict[str, float | None]:
    """Average weighted component contribution across a list of enriched apps."""
    if not records:
        return {k: None for k in COMPONENTS}

    result = {}
    for key in COMPONENTS:
        vals = [
            r["_components"][key]
            for r in records
            if r["_components"].get(key) is not None
        ]
        result[key] = round(mean(vals), 1) if vals else None
    return result


def _avg_composite(records: list[dict]) -> float | None:
    vals = [r["_composite"] for r in records if r["_composite"] is not None]
    return round(mean(vals), 1) if vals else None


def _score_distribution(records: list[dict], bucket_size: int = 10) -> dict[str, int]:
    """
    Returns an ordered dict of score-band label → count.
    Bands: 0–9, 10–19, … 120–129, 130.
    """
    bands: dict[str, int] = {}
    # Initialise all bands so the chart always has a full x-axis
    for lo in range(0, COMPOSITE_MAX + 1, bucket_size):
        hi = min(lo + bucket_size - 1, COMPOSITE_MAX)
        label = f"{lo}–{hi}"
        bands[label] = 0

    for r in records:
        v = r["_composite"]
        if v is None:
            continue
        v = max(0, min(int(v), COMPOSITE_MAX))
        lo = (v // bucket_size) * bucket_size
        hi = min(lo + bucket_size - 1, COMPOSITE_MAX)
        label = f"{lo}–{hi}"
        bands[label] = bands.get(label, 0) + 1

    return bands


def _funnel_speed(records: list[dict]) -> dict:
    """
    Days-to-response stats for records that have a response_date.
    Returns {count, min, max, median, mean} or None if no data.
    """
    vals = [
        r["_days_to_response"]
        for r in records
        if r["_days_to_response"] is not None
    ]
    if not vals:
        return {}
    return {
        "count":  len(vals),
        "min":    min(vals),
        "max":    max(vals),
        "median": int(median(vals)),
        "mean":   round(mean(vals), 1),
    }


def _status_counts(apps: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in apps:
        s = a.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return counts


def _rejection_reasons(apps: list[dict]) -> dict[str, int]:
    """Count rejected applications by structured rejection_reason. Rejections
    logged before the field existed (or via --status rejected with no reason)
    fall into 'unspecified'. Keys are the SSOT keys from
    config.REJECTION_REASONS plus 'unspecified'."""
    counts: dict[str, int] = {}
    for a in apps:
        if a.get("status") != "rejected":
            continue
        reason = a.get("rejection_reason") or "unspecified"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Public API — called by serve.py
# ---------------------------------------------------------------------------

def build_metrics() -> dict:
    """
    Main entry point.  Returns a single dict consumed by the /metrics renderer.

    Keys
    ----
    total_apps          : int
    status_counts       : {status: count}
    rejection_reasons   : {reason: count}             — rejected apps by structured reason
    cohort_sizes        : {in_flight, dead, positive, other}
    avg_composite       : {in_flight, dead, positive}  — float or None
    avg_components      : {in_flight, dead, positive}  — {component: float|None}
    component_max       : {component: weight}           — COMPOSITE_MAX contribution ceiling
    composite_max       : int
    score_distribution  : {band_label: {in_flight, dead, positive, total}}
    funnel_speed        : {in_flight, dead, positive}  — {count,min,max,median,mean} or {}
    score_band_size     : int
    components_ordered  : list[str]                    — display order
    """
    jobs_by_id, companies_by_id, apps = _load_data()
    cohorts = _build_cohorts(jobs_by_id, companies_by_id, apps)

    in_flight = cohorts["in_flight"]
    dead      = cohorts["dead"]
    positive  = cohorts["positive"]
    all_apps  = in_flight + dead + positive + cohorts["other"]

    # Per-component max contribution (weight = points if you score 100%)
    component_max = {k: v.weight for k, v in COMPONENTS.items()}

    # Score distribution — merged across cohorts for the histogram
    BAND = 10
    dist_in_flight = _score_distribution(in_flight,  BAND)
    dist_dead      = _score_distribution(dead,        BAND)
    dist_positive  = _score_distribution(positive,    BAND)
    dist_all       = _score_distribution(all_apps,    BAND)

    score_distribution = {
        band: {
            "in_flight": dist_in_flight.get(band, 0),
            "dead":      dist_dead.get(band, 0),
            "positive":  dist_positive.get(band, 0),
            "total":     dist_all.get(band, 0),
        }
        for band in dist_all
    }

    return {
        "total_apps":      len(apps),
        "status_counts":   _status_counts(apps),
        "rejection_reasons": _rejection_reasons(apps),
        "cohort_sizes": {
            "in_flight": len(in_flight),
            "dead":      len(dead),
            "positive":  len(positive),
            "other":     len(cohorts["other"]),
        },
        "avg_composite": {
            "in_flight": _avg_composite(in_flight),
            "dead":      _avg_composite(dead),
            "positive":  _avg_composite(positive),
        },
        "avg_components": {
            "in_flight": _avg_components(in_flight),
            "dead":      _avg_components(dead),
            "positive":  _avg_components(positive),
        },
        "component_max":    component_max,
        "composite_max":    COMPOSITE_MAX,
        "score_distribution": score_distribution,
        "funnel_speed": {
            "in_flight": _funnel_speed(in_flight),
            "dead":      _funnel_speed(dead),
            "positive":  _funnel_speed(positive),
        },
        "score_band_size":   BAND,
        "components_ordered": list(COMPONENTS.keys()),
    }
