# next-role — Data Files

Every piece of state the pipeline keeps lives in `data/` as either a JSON
array (mutable state, rewritten in full on every change) or a JSONL log
(append-only diagnostic stream). The whole directory is **gitignored** —
your pipeline, applications, and research never leave the machine.

This document specifies every field in every file: type, valid values,
who writes it, who reads it.

---

## Conventions

| Concern | Convention |
|---|---|
| **Location** | All files under `<repo>/data/`. Defined in `scripts/config.py` (`COMPANY_REGISTRY_PATH`, `JOB_PIPELINE_PATH`, ...). |
| **Encoding** | UTF-8. Surrogate characters are stripped via `_sanitize` before write to survive Windows cp1252. |
| **Timestamps** | All `*_at` / `*_updated` / `*_created` fields are ISO 8601 UTC strings (e.g. `"2026-05-13T20:27:20.453956+00:00"`). |
| **Dates** | `date_*` / `*_date` fields are `YYYY-MM-DD` (local), produced by `config.today()`. |
| **IDs** | Most IDs are v4 UUIDs (`config` doesn't enforce — just `uuid.uuid4()`). Exceptions: `linkedin_job_id` is LinkedIn's numeric job ID; `staging_id` is 12 hex chars (`uuid.uuid4().hex[:12]`). |
| **Nulls** | `null` is allowed and meaningful for fields that may not yet be known (`date_posted`, `response_date`, `glassdoor_rating`, etc.). Empty string `""` is used for fields that were set deliberately to nothing (notes, `layoff_notes`, etc.). |
| **Pretty-printing** | Every JSON file is written with `indent=2, ensure_ascii=False` for diff readability. |
| **JSONL** | Each line is a self-contained JSON object. Order is append-time. Best-effort writes — losing a line never blocks the pipeline. |

### Foreign-key map

```
job_pipeline.job_id        ← application_tracker.job_id
                           ← comp_estimates.job_id
                           ← process_log.entity_id (when entity_type="job")
                           ← cover-letter filename (output/*.docx)

job_pipeline.company_id    → company_registry.company_id
application_tracker.company_id → company_registry.company_id
process_log.entity_id      → company_registry.company_id (when entity_type="company")
                           → application_tracker.application_id (when entity_type="application")
```

`target_boards`, `crawl_log.jsonl`, `email_*.json`, and the diagnostic
JSONL logs have no foreign keys to the rest — they're parallel.

---

## File index

| File | Format | Role | Writers | Readers |
|---|---|---|---|---|
| `job_pipeline.json` | JSON array | Every ingested job — JD text, scores, lifecycle status | `ingest.py`, `update_status.py`, `generate_cl.js`, `rescore_all.py`, `scan_no_sponsorship.py`, `scan_foreign_locations.py` | every surface |
| `company_registry.json` | JSON array | Per-company research (sponsorship, remote, ethics) | `ingest.py` (stubs), `research_company.py` (full records), `run.py` (clear stub flag) | every surface |
| `application_tracker.json` | JSON array | Submitted applications + lifecycle status | `update_status.py`, `serve.py` (ghosted auto-flip) | dashboard, `/today`, `company_block_reason` |
| `target_boards.json` | JSON array | ATS boards the crawler polls directly | `ingest.py`, `crawl.py`, `backfill_target_boards.py`, `discover_boards_from_careers.py` | `crawl.py` |
| `comp_estimates.json` | JSON array | Opus-generated comp estimates keyed by `job_id` | `comp_estimate.py` | `/today` cover-letters surface, `/job/<id>` |
| `process_log.json` | JSON array | Pipeline event log (lifecycle audit trail) | `ingest.py`, `update_status.py`, `comp_estimate.py`, `generate_cl.js`, `scan_no_sponsorship.py`, `scan_foreign_locations.py` | manual inspection |
| `daily_checklist.json` | JSON object | `/today` UI section-done flags keyed by date | `serve.py` | `serve.py` |
| `email_config.json` | JSON object | LinkedIn IMAP sender allowlist | `linkedin_fetch.py` (auto-creates) | `linkedin_fetch.py` |
| `email_state.json` | JSON object | Cross-run `seen_message_ids` for IMAP dedup | `linkedin_fetch.py` | `linkedin_fetch.py` |
| `email_staged.json` | JSON array | Parsed LinkedIn alert jobs awaiting per-row ingest | `linkedin_fetch.py`, `prefilter_staged.py`, `cleanup_staged_jd.py`, `serve.py` | `serve.py` `/today` |
| `inbox_matches.json` | JSON array | Staged rejection / interview / offer email matches awaiting one-click review | `inbox_scan.py`, `serve.py` (apply/dismiss) | `serve.py` `/today` |
| `inbox_scan_state.json` | JSON object | Cross-run `processed_message_ids` for the inbox scanner's own dedup | `inbox_scan.py` | `inbox_scan.py` |
| `drills.json` | JSON array | Claude-generated code drills (prompt, partial interface, status, review feedback) for the `/today` Code-drills section | `scripts/drills.py`, `serve.py` (complete) | `serve.py` `/today` |
| `crawl_log.jsonl` | JSONL | Per-run crawl summaries (funnel breakdown) | `crawl.py` | manual inspection |
| `jd_fetch_log.jsonl` | JSONL | Per-URL JD-fetch diagnostics from `linkedin_fetch._fetch_jd_text` | `linkedin_fetch.py` | manual inspection |
| `board_discovery_log.jsonl` | JSONL | Per-company careers-page scrape diagnostics | `discover_boards_from_careers.py` | manual inspection |

> `.bak` files (e.g. `job_pipeline.json.bak`) are written by
> `rescore_all.py` and `scan_no_sponsorship.py` before any destructive
> change. They're not part of the schema — restore by `cp`.

---

## `data/job_pipeline.json`

**Role.** The master table. Every job that passes ingest lives here as a
single record. Composite scoring, dashboards, cover-letter selection,
status transitions, and the `/today` apply queue all read from this file.

**Lifecycle.**

- **Created** by `ingest.ingest_job` after validation + scoring.
- **Mutated by** `update_status.cmd_log` (`pipeline_status: "active" → "applied"`), `generate_cl.js` (`cover_letter_generated`, `cover_letter_version`, `cover_letter_path`, `pipeline_status: "active" → "cover_letter_ready"`), `rescore_all.py` (re-writes `stack_match_score`, `seniority_score`, `domain_fit_score`, `score_notes`, `scored_at`), `scan_no_sponsorship.py` / `scan_foreign_locations.py` / `scan_stale_jobs.py` (`pipeline_status: → "archived"` with `archived_at` + `archived_reason`; the latter two also run from `crawl.crawl`'s end-of-run sweeps — `scan_stale_jobs.py` expires un-applied rows older than `PIPELINE_EXPIRY_DAYS` since `date_found` — also runs on every `/today` render via `serve.apply_stale_job_check`), `serve.py` `/today/cl/archive` (operator archives a dead posting).
- **Deletion** never happens — old jobs are archived in place.

### Schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `job_id` | UUID v4 string | ✅ | Primary key. |
| `company_id` | UUID v4 string | ✅ | FK → `company_registry.company_id`. |
| `company_name` | string | ✅ | Denormalized for human-readable surfaces. |
| `title` | string | ✅ | Free-text job title as posted. |
| `apply_url` | URL string | ✅ | Used for dedup; uniqueness enforced by `ingest.check_duplicate`. |
| `location` | string | ✅ | Free text. `"remote canada"`, `"Vancouver, Canada"`, `"Remote, US"`, etc. — used by `config.derive_country` (→ `CA`/`IE`/`US`/`OTHER`) and the location pre-filter heuristics. US roles are remote-only and only ingest when `"US" in config.TARGET_COUNTRIES` (see `config.location_passes`). |
| `job_type` | `"remote"` / `"unknown"` | ✅ | From `config.is_remote_role(location, source)` at ingest: `"remote"` when the location text says so **or** the listing came from a remote-only board (`REMOTE_ONLY_SOURCES`); else `"unknown"`. |
| `jd_text` | string | ✅ | Full JD body, ≥ `MIN_JD_LENGTH` (200) chars. May contain HTML for ATS-API ingests where the upstream returned HTML. |
| `date_posted` | `YYYY-MM-DD` / `null` | optional | Source-supplied posting date when available. |
| `date_found` | ISO datetime | ✅ | When this row was ingested. |
| `date_last_verified` | ISO datetime | ✅ | Set equal to `date_found` at ingest; never updated automatically. |
| `source` | string | ✅ | One of: `"direct_scrape"`, `"manual"`, `"remoteok"`, `"remotive"`, `"greenhouse"`, `"lever"`, `"ashby"`. |
| `staleness_status` | `"fresh"` / `"soft_stale"` / `"hard_stale"` | ✅ | From `config.compute_staleness` (≤30d / 30-59d / ≥60d). |
| `staleness_updated` | ISO datetime | ✅ | When `staleness_status` was last refreshed (currently only at ingest). |
| `stack_match_score` | int 0-35 | ✅ | From `config.compute_stack_score`. Native max = `STACK_SCORE_MAX` (35). |
| `seniority_score` | int 0-25 | ✅ | From `score_jd` after the title-bucket cap. Native max 25. |
| `domain_fit_score` | int 0-20 | ✅ | From `score_jd`. Native max 20. |
| `hiring_velocity_score` | int 0-5 | ✅ | From `config.compute_velocity_score`. Native max 5. |
| `score_notes` | string | ✅ | One- to three-sentence Claude rationale for seniority + domain. |
| `role_exposure` | enum | ✅ | Gov/defense screen role classification: `insulated`, `ambiguous`, `exposed`. Resolved at ingest by `config.classify_role_exposure(title, claude_judgment)` — deterministic title rules (SA / professional services / support, etc.) over `score_jd`'s JD-level read. Defaults `insulated`. |
| `seniority_raw` | int 0-25 | optional | Pre-cap value (only present when `apply_title_cap` reduced the score). Audit trail. |
| `seniority_cap_title` | string | optional | The title that triggered the cap. Only present when `seniority_raw` is. |
| `cover_letter_generated` | bool | ✅ | `false` at ingest; `true` after `generate_cl.js` runs. |
| `cover_letter_version` | int | ✅ | `0` at ingest; bumped on each regen. Drives the filename suffix. |
| `cover_letter_path` | string | optional | POSIX-style path under `output/` (e.g. `"output/2026-05-15_EA_Senior_Software_Engineer.docx"`). Present once `cover_letter_generated` is `true`. |
| `pipeline_status` | enum | ✅ | `"active"` (ingested, no CL yet), `"cover_letter_ready"` (CL exists), `"applied"` (logged), `"archived"` (manually retired or no-sponsorship sweep). |
| `pay_range_min` | int / `null` | ✅ | Always `null` in current builds — reserved for future structured-pay parsing. |
| `pay_range_max` | int / `null` | ✅ | Same. |
| `pay_currency` | string / `null` | ✅ | Same. |
| `tags` | list[string] | ✅ | Always `[]` in current builds. Reserved. |
| `notes` | string | ✅ | Always `""` at ingest. Surfaces don't write to it yet. |
| `scored_at` | ISO datetime | optional | Set by `score_jd.update_job_record` and `rescore_all.py`. Absent on rows ingested before that field was added. |
| `archived_at` | ISO datetime | optional | Set by `scan_no_sponsorship.py`, `scan_foreign_locations.py`, `scan_stale_jobs.py`, and `/today/cl/archive`. |
| `archived_reason` | string | optional | Set alongside `archived_at`. E.g. `"JD says no sponsorship"`, `"foreign-pinned remote (not an eligible geography)"`, `"stale_pipeline"` (un-applied past `PIPELINE_EXPIRY_DAYS` since `date_found`). |

### Cross-references

- `company_id` → `company_registry.json` (required; if missing, `composite_score` falls back to 0 for sponsorship + remote).
- `apply_url` is the dedup key — `ingest.check_duplicate` rejects new ingests of an existing **non-archived** apply URL.

### Example

```json
{
  "job_id": "3db0c798-5bc3-4b06-af59-af336b11a693",
  "company_id": "c43a2194-e686-4498-b1f3-fc1a8c9baf15",
  "company_name": "EA",
  "title": "Senior Software Engineer",
  "apply_url": "https://jobs.ea.com/en_US/careers/JobDetail/Sr-Software-Engineer/213715",
  "location": "Remote Canada",
  "job_type": "remote",
  "jd_text": "Electronic Arts creates next-level entertainment experiences... [truncated]",
  "date_posted": null,
  "date_found": "2026-05-07T00:23:25.653957+00:00",
  "date_last_verified": "2026-05-07T00:23:25.653957+00:00",
  "source": "direct_scrape",
  "staleness_status": "fresh",
  "staleness_updated": "2026-05-07T00:23:25.653957+00:00",
  "stack_match_score": 21,
  "seniority_score": 15,
  "domain_fit_score": 18,
  "hiring_velocity_score": 0,
  "score_notes": "This Senior role (capped at 15) focuses heavily on real-time streaming...",
  "cover_letter_generated": true,
  "cover_letter_version": 1,
  "cover_letter_path": "output/2026-05-15_EA_Senior_Software_Engineer.docx",
  "pipeline_status": "applied",
  "pay_range_min": null,
  "pay_range_max": null,
  "pay_currency": null,
  "tags": [],
  "notes": "",
  "scored_at": "2026-05-14T23:13:36.230047+00:00"
}
```

---

## `data/company_registry.json`

**Role.** One record per company: identity, sponsorship score, remote
fitness, ethics flags. Two of its fields (`sponsorship_score`,
`remote_fit`) feed the composite scoring; two more
(`ethics_hard_exclude`, `stub`) gate ingest behavior; the rest are
advisory.

**Lifecycle.**

- **Stubs** are created by `ingest.get_or_stub_company` when a JD names an unknown company — neutral defaults, `stub: true` flag, no API call.
- **Full records** are produced by `research_company.research_company` (Tier-1 Haiku + Tier-2 web search) and merged via `build_registry_record` + `upsert_company`. Name-matched upsert preserves `company_id`, `record_created`, and `confirmed_clean` from any prior stub or record.
- **Stub-clear** happens in `run.py:research_top_stubs` after a successful research call — pops the `stub` key off the record.

### Schema

| Field | Type | Role | Notes |
|---|---|---|---|
| `company_id` | UUID v4 | identity | Primary key. |
| `name` | string | identity | Canonical name. Case-insensitive uniqueness enforced by `upsert_company`. |
| `industry` | string | metadata | Free text; `"unknown"` is a valid value for stubs. |
| `size_tier` | `"startup"` / `"mid"` / `"large"` / `"enterprise"` | metadata | Rough employee scale. |
| `country_hq` | ISO country code string / `""` | metadata | Empty for stubs. |
| `job_portal_url` | URL string / `""` | metadata | Careers page; read by `discover_boards_from_careers.py`. |
| `scrape_tier` | `"1_direct"` / `"2_alert"` / `"3_manual"` / `"4_rss"` | metadata | Advisory; suggested cadence for tracking new postings at this company. |
| `sponsorship_score` | int 0-15 | **scoring** | Composite weight 35; native max 15. From Tier 1 Claude research. |
| `sponsorship_notes` | string | advisory | One-sentence rationale for the score. |
| `remote_fit` | int 0-5 | **scoring** | Composite weight 12; native max 5. From Tier 1 Claude research. |
| `glassdoor_rating` | float / `null` | advisory | Tier-2 overrides Tier-1 if found. |
| `glassdoor_engineering_sentiment` | `"positive"` / `"mixed"` / `"negative"` / `"unknown"` | advisory | Tier-2 overrides if not `"unknown"`. |
| `blind_sentiment` | `"positive"` / `"mixed"` / `"negative"` / `"unknown"` | advisory | Same. |
| `recent_layoffs` | bool | advisory | Tier-2 only. |
| `layoff_notes` | string / `""` | advisory | One sentence if `recent_layoffs=true`. |
| `ethics_hard_exclude` | bool | **filter (ingest-time)** | When `true`, `ingest.get_or_stub_company` returns `None` and the JD is discarded. Hard kill switch. |
| `ethics_flags` | list[object] | advisory | See ethics-flag schema below. |
| `ethics_notes` | string / `""` | advisory | One-sentence summary across all flags. |
| `gov_defense_flag` | enum | advisory + apply-time ranking | Government/defense entanglement tier: `none`, `tier_c`, `tier_b`, `tier_a`. Set by Haiku research, then floored to `tier_a` for industry-detected defense contractors via `config.reconcile_gov_defense_flag`. Combined with a job's `role_exposure` at display time via `config.gov_screen_result`: a `flag` applies a `−GOV_SCREEN_FLAG_PENALTY_PCT`% penalty to the **apply-rank only** (`apply_rank_score`; composite is unchanged), and a `fail` (tier_a) hides the role from apply surfaces (`gov_screen_block_reason`). Industry-detected `tier_a` is additionally excluded at ingest via `ethics_hard_exclude`. |
| `flag_evidence` | list[string] | advisory | Short strings naming each gov/defense signal Haiku matched (empty for `none`). |
| `confirmed_clean` | bool | metadata | Operator-only — manually flipped to `true` after a review. |
| `record_created` | ISO datetime | metadata | Preserved across `upsert_company`. |
| `record_updated` | ISO datetime | metadata | Bumped on every upsert. |
| `stub` | bool | **filter (research)** | Present only on stub records. Read by `run.py:research_top_stubs` to decide whether to research. Removed once research completes. |

#### Ethics-flag object

| Field | Type | Notes |
|---|---|---|
| `category` | enum | `direct_harm`, `indirect_harm`, `monopoly`, `human_rights`, `protected_class`, `union_busting`, `environmental`, `surveillance`, `predatory_practices`, `other`. |
| `status` | enum | `confirmed`, `alleged`, `historical`, `clean`. |
| `description` | string | One sentence. |
| `source` | string | Publication or organization. |
| `source_date` | `YYYY-MM-DD` / `""` | Empty if unknown. |

### Example (researched company)

```json
{
  "company_id": "a4e00b35-055d-4894-8183-70cf99b75b2d",
  "name": "Shopify",
  "industry": "E-commerce Platform",
  "size_tier": "enterprise",
  "country_hq": "CA",
  "job_portal_url": "https://www.shopify.com/careers",
  "scrape_tier": "1_direct",
  "sponsorship_score": 15,
  "sponsorship_notes": "Canadian company with established visa sponsorship programs...",
  "remote_fit": 5,
  "glassdoor_rating": 4.0,
  "glassdoor_engineering_sentiment": "mixed",
  "blind_sentiment": "positive",
  "recent_layoffs": true,
  "layoff_notes": "Shopify conducted layoffs in November 2025 affecting approximately 80 employees...",
  "ethics_hard_exclude": false,
  "ethics_flags": [
    {
      "category": "indirect_harm",
      "status": "alleged",
      "description": "Platform has been used by merchants selling counterfeit goods...",
      "source": "Various media reports",
      "source_date": "2022-06-15"
    }
  ],
  "ethics_notes": "Generally clean reputation; addressed platform misuse issues...",
  "gov_defense_flag": "none",
  "flag_evidence": [],
  "confirmed_clean": false,
  "record_created": "2026-05-06T02:57:08.075147+00:00",
  "record_updated": "2026-05-06T04:13:52.322265+00:00"
}
```

### Example (stub)

```json
{
  "company_id": "4de487cd-019c-4142-9019-51e8b62ac293",
  "name": "Wavelo",
  "industry": "Unknown",
  "size_tier": "mid",
  "country_hq": "",
  "sponsorship_score": 7,
  "remote_fit": 3,
  "ethics_hard_exclude": false,
  "ethics_flags": [],
  "gov_defense_flag": "none",
  "flag_evidence": [],
  "record_created": "2026-05-10T01:57:12.971015+00:00",
  "record_updated": "2026-05-10T01:57:12.971015+00:00",
  "stub": true
}
```

---

## `data/application_tracker.json`

**Role.** Every submitted application. Drives the dashboard's
applications panel, the `/today` status-updates section, and (via
`company_block_reason`) the apply-time company throttle.

**Lifecycle.**

- **Created** by `update_status.cmd_log` — flips the source job's `pipeline_status` to `"applied"` and snapshots the composite score. Blocks a second application to the same company + core title (see `config.find_duplicate_application`) unless `--force` is passed.
- **Mutated by** `update_status.cmd_status` (status transitions, `response_date` on first non-applied transition, `rejection_reason` when rejected) and the time-based aging in `config.auto_age_application` — called by both `serve.py:apply_ghosted_check` and `update_status.cmd_list`. Aging advances un-answered applications: `applied` → `ghosted` after `GHOSTED_DAYS` (21), then `ghosted` → `rejected` after `GHOSTED_REJECTED_DAYS` (45, `rejection_reason="ghosted_timeout"`).
- **Never deleted.**

### Schema

| Field | Type | Notes |
|---|---|---|
| `application_id` | UUID v4 | Primary key. |
| `job_id` | UUID v4 | FK → `job_pipeline.job_id`. |
| `company_id` | UUID v4 | FK → `company_registry.company_id`. |
| `company_name` | string | Denormalized. |
| `title` | string | Denormalized. |
| `apply_url` | URL string | Denormalized. |
| `location` | string | Denormalized. |
| `country` | `"CA"` / `"IE"` / `"US"` / `"OTHER"` | From `config.derive_country(location)` (imported by `update_status`). `"US"` only appears for roles ingested while `"US"` was in `TARGET_COUNTRIES` (remote-only US stop-gap). |
| `date_applied` | `YYYY-MM-DD` | Today's date at log time. |
| `application_method` | enum | `greenhouse`, `lever`, `workday`, `builtin`, `linkedin`, `direct`, `other`. |
| `cover_letter_version` | int | Copied from the job at log time. |
| `plain_text_submitted` | bool | Operator marks whether a plain-text version was used (vs `.docx`). |
| `composite_score_at_apply` | int / `null` | Snapshot of `composite_score(job, company)` at log time. |
| `status` | enum | `applied`, `recruiter_screen`, `interview`, `offer`, `rejected`, `ghosted`, `withdrawn`. |
| `status_updated` | ISO datetime | Bumped on every status change. |
| `response_date` | `YYYY-MM-DD` / `null` | Set on the **first** transition out of `applied`/`ghosted`. This is what frees the company-throttle slot. The `ghosted` → `rejected` auto-conversion deliberately leaves this `null` (no real response occurred), so ghosted-timeout rejections stay out of funnel-speed metrics. |
| `ghosted_flag` | bool | Set `True` by `config.auto_age_application` once `date_applied` ages past `GHOSTED_DAYS` (21); reset to `False` if the record later auto-converts to `rejected`. |
| `rejection_reason` | enum / `null` | Only meaningful when `status="rejected"`. One of `config.REJECTION_REASONS`: `generic`, `position_filled`, `interview_failed`, `ghosted_timeout`. `null` for non-rejected apps and for rejections logged before this field existed (shown as `unspecified` in `/metrics`). |
| `notes` | string | Free-text; appended (not replaced) by `--notes` and by the rejection-reason label. |
| `inaccuracies_noted` | string | Reserved — never written by the current pipeline. |

### Throttle semantics

`company_block_reason(company_id, apps)` counts records where:

- `company_id` matches, **AND**
- `status` is in `{applied, recruiter_screen, interview}` (i.e. `IN_FLIGHT_STATUSES`), **AND**
- `response_date` is null.

When the count hits `MAX_ACTIVE_APPS_PER_COMPANY` (3), the company is
hidden from apply surfaces. Once any of those rows transitions to a
terminal status (or `response_date` gets set), the slot frees
immediately — there's no time-based cooldown.

### Example

```json
{
  "application_id": "1b589bc8-3bd9-4287-942a-acb7801a8cd9",
  "job_id": "24127a68-a2e7-419b-82ff-bef0021397cd",
  "company_id": "085c31a4-972f-4555-a946-fa5df6c10dfa",
  "company_name": "PointClickCare",
  "title": "Principal Software/Data Engineer (event architecture)",
  "apply_url": "https://www.linkedin.com/jobs/view/4324300685/",
  "location": "Mississauga, ON (Remote)",
  "country": "OTHER",
  "date_applied": "2026-05-13",
  "application_method": "direct",
  "cover_letter_version": 1,
  "plain_text_submitted": false,
  "composite_score_at_apply": 74,
  "status": "applied",
  "status_updated": "2026-05-13T20:27:20.453956+00:00",
  "response_date": null,
  "ghosted_flag": false,
  "rejection_reason": null,
  "notes": "",
  "inaccuracies_noted": ""
}
```

---

## `data/target_boards.json`

**Role.** The crawler's Lane-2 source list. Every entry is one ATS board
(Greenhouse / Lever / Ashby) to poll directly. Workday and SmartRecruiters
entries are recorded for visibility but skipped at fetch time (no
implementation).

**Lifecycle.**

- **Seeded** by hand (entries with `added_via: "seed"`).
- **Auto-added** by `crawl.auto_add_board` whenever an aggregator listing's apply URL matches a known ATS pattern — `added_via: "auto_discovery"`.
- **Auto-added** by `ingest.ingest_job` after a successful ingest — `added_via: "ingest"`.
- **Batch-added** by `backfill_target_boards.py` over already-ingested jobs — `added_via: "backfill_pipeline"`.
- **Batch-added** by `discover_boards_from_careers.py` after careers-page scrape — `added_via: "careers_page_scrape"`.
- Dedup key is `(ats, slug)` — the same slug at a different ATS would coexist (unlikely but allowed).

### Schema

| Field | Type | Notes |
|---|---|---|
| `company` | string | Display name. Not used for dedup; can drift from `company_registry.name`. |
| `ats` | `"greenhouse"` / `"lever"` / `"ashby"` / `"workday"` / `"smartrecruiters"` | Workday/SmartRecruiters are recorded but skipped at fetch time. |
| `slug` | string | Company identifier in the board URL. E.g. `"databricks"` for `boards.greenhouse.io/databricks`. |
| `added` | `YYYY-MM-DD` | Date the entry was added. |
| `added_via` | string | Provenance: `"seed"` / `"auto_discovery"` / `"ingest"` / `"backfill_pipeline"` / `"careers_page_scrape"`. |

### Example

```json
[
  { "company": "Databricks", "ats": "greenhouse", "slug": "databricks",
    "added": "2026-05-06", "added_via": "seed" },
  { "company": "ClickHouse", "ats": "greenhouse", "slug": "clickhouse",
    "added": "2026-05-06", "added_via": "seed" },
  { "company": "Babylist",   "ats": "greenhouse", "slug": "babylist",
    "added": "2026-05-11", "added_via": "careers_page_scrape" }
]
```

---

## `data/comp_estimates.json`

**Role.** One Opus-generated comp estimate per `job_id`. Consumed by the
`/today` cover-letters surface (button next to "Generate CL") and the
per-job detail page.

**Lifecycle.**

- **Written** by `comp_estimate.py` via `upsert_estimate` — replaces any existing record with the same `job_id`.
- **Never auto-invalidated** — re-runs require a manual `comp_estimate.py --job-id <uuid>`.

### Schema

| Field | Type | Notes |
|---|---|---|
| `job_id` | UUID v4 | FK → `job_pipeline.job_id`. Primary key. |
| `company_name` | string | Denormalized snapshot. |
| `title` | string | Denormalized snapshot. |
| `location` | string | Denormalized snapshot. |
| `generated_at` | ISO datetime | When the Claude call returned. |
| `model` | string | Anthropic model ID (currently `"claude-opus-4-7"`). |
| `estimate` | object | The validated response — schema below. |

#### `estimate` object

| Field | Type | Notes |
|---|---|---|
| `currency` | ISO 4217 string | `CAD`, `EUR`, `GBP`, `USD`. |
| `base.min` | int | Realistic floor (~p50 of market band), whole thousands. |
| `base.max` | int | Realistic ceiling (~p90), whole thousands. |
| `base.target` | int | Recommended ask (~p85), whole thousands. |
| `year_end_bonus` | object | See bonus-component schema. `target_pct` int 0-100 + `target_amount` int (in `currency`). |
| `signon` | object | `target` int / `null`. |
| `relocation` | object | `target` int / `null`. |
| `equity` | object | `target_annual` int / `null`. |
| `confidence` | `"HIGH"` / `"MED"` / `"LOW"` | LOW prompts the operator to sanity-check via Levels.fyi/Glassdoor. |
| `reasoning` | string | 2-3 sentences overall rationale. |

#### Bonus-component object (shared shape)

| Field | Type | Notes |
|---|---|---|
| `classification` | `"Expected"` / `"Possible"` / `"Unusual"` / `"Stated-in-JD"` | Validated by `comp_estimate.validate`. |
| `reason` | string | One sentence explanation. |
| `target` / `target_pct` / `target_amount` / `target_annual` | int / `null` | The recommended ask. **Always `null` when `classification == "Unusual"`**. |

### Example

```json
{
  "job_id": "cd9589cf-4faa-48ab-bdb0-9127115f01a8",
  "company_name": "Dropbox",
  "title": "Staff Backend Product Software Engineer, Core",
  "location": "Canada (Remote)",
  "generated_at": "2026-05-15T21:07:34.464479+00:00",
  "model": "claude-opus-4-7",
  "estimate": {
    "currency": "CAD",
    "base":           { "min": 215000, "max": 277000, "target": 268000 },
    "year_end_bonus": { "classification": "Stated-in-JD",
                        "reason": "JD explicitly states all regular employees are eligible...",
                        "target_pct": 15, "target_amount": 40000 },
    "signon":         { "classification": "Possible",
                        "reason": "Dropbox sometimes offers modest sign-on bonuses for Staff hires...",
                        "target": 25000 },
    "relocation":     { "classification": "Unusual",
                        "reason": "Fully remote Virtual First role...",
                        "target": null },
    "equity":         { "classification": "Stated-in-JD",
                        "reason": "JD explicitly states RSU grants are part of total rewards...",
                        "target_annual": 70000 },
    "confidence": "MED",
    "reasoning": "Dropbox published the Canada pay range ($204.9k-$277.2k CAD) directly in the JD..."
  }
}
```

---

## `data/application_questions.json`

**Role.** All ad-hoc application questions and their generated answers,
keyed by `job_id`. Surfaces only on `/answer-questions?job_id=<uuid>`
(reachable from the Cover Letters & Apply queue's per-job action strip).

**Lifecycle.**

- **Top level is a dict** `{ job_id: { motivation: [...], behavioral: [...] } }` — *not* an array. Empty / missing file is treated as `{}`.
- **Created** by `answer_questions.add_question` on first add per job + class.
- **Mutated** by `answer_questions.generate_answer` (appends a new `draft_history` version), `finalize_answer`, `unfinalize_answer`, `update_question_override`, `update_resume_entries`.
- **Deleted from** only by `answer_questions.delete_question` — refuses to delete a question with `status == "finalized"`.
- **`draft_history` is append-only**; regeneration never overwrites a prior version.
- **`finalized_answer` is never overwritten by regeneration** — only by `finalize_answer` / `unfinalize_answer`.

### Schema per question record

| Field | Type | Notes |
|---|---|---|
| `question_id` | UUID v4 | Primary key within the job + class list. |
| `question_text` | string | The verbatim question pasted by the operator. |
| `question_class` | `"motivation"` / `"behavioral"` | Drives which strategy section of `profile/answer_questions_rules.md` the prompt invokes. |
| `char_cap` | int / `null` | Hard character cap. Set once at add time; to change, delete and re-add. |
| `resume_entries_used` | list of slug strings | Slugs from `scripts/config.py:RESUME_ENTRY_SLUGS`. Initially `[]`; the model populates it on first generate; the operator can override via chips. |
| `question_override_notes` | string | Per-question one-shot context (e.g. "Lead with Jailer here"). Auto-saved on textarea blur. Survives regeneration; never propagates to other questions. |
| `draft_history` | list of draft objects | Append-only; each has `version` (1-indexed), `answer`, `char_count`, `generated_at`, and `source` (`"generated"` for Claude calls, `"manual_edit"` for operator edits via the editable answer textarea — older entries written before this field existed have no key). |
| `finalized_answer` | string / `null` | Set by `finalize_answer` (clones latest draft); cleared by `unfinalize_answer`. |
| `finalized_at` | ISO datetime / `null` | When the finalize click happened. |
| `status` | `"draft"` / `"finalized"` | Mirrors the presence of `finalized_answer`. |

### Example

```json
{
  "24127a68-a2e7-419b-82ff-bef0021397cd": {
    "motivation": [
      {
        "question_id": "e0c2f1a4-7d1b-4d39-9e02-1b0f88c6b5ac",
        "question_text": "What is it about Miro that makes you interested in joining?",
        "question_class": "motivation",
        "char_cap": 900,
        "resume_entries_used": ["haloc_flink_distilled", "mass_gpc"],
        "question_override_notes": "",
        "draft_history": [
          { "version": 1, "answer": "...", "char_count": 412, "generated_at": "2026-05-25T14:23:00+00:00" }
        ],
        "finalized_answer": null,
        "finalized_at": null,
        "status": "draft"
      }
    ],
    "behavioral": []
  }
}
```

---

## `data/resume_entry_notes.json`

**Role.** Global supplemental notes per resume-entry slug. Every entry that
has a non-empty note is injected into **every** answer-generation prompt
that draws on that entry. Treat as authoritative corrections / constraints
(e.g. "partial adoption only — not full team rollout"). Edited via the
"Resume entry notes" panel at the bottom of `/answer-questions`.

**Lifecycle.**

- **Top level is a flat dict** keyed by slug (`{slug: note_text}`).
- **Initialized** by `answer_questions.load_entry_notes` on first access — all keys from `RESUME_ENTRY_SLUGS` written with empty-string values.
- **Backfilled** on every load: any new slug added to `RESUME_ENTRY_SLUGS` since the last save shows up with an empty value automatically.
- **Mutated** by `answer_questions.save_entry_notes` (called by `/answer-questions/entry-notes` on textarea blur). Unknown slugs are dropped on save.

### Schema

| Field | Type | Notes |
|---|---|---|
| `<slug>` | string | One key per entry in `RESUME_ENTRY_SLUGS`. Empty string = no note. Trimmed/lowercased? No — notes preserve operator formatting. |

### Example

```json
{
  "haloc_distilled":       "The intermediate dataset design was the hardest part — not exactly-once. Solo work — not team-led.",
  "jailer":                "Partial adoption — I proposed and architected; the implementing team built it. The >50% cost reduction came from partial adoption only.",
  "mass_gpc":              "I pitched the message structure and authored the standards.",
  "haloc_flink_distilled": "",
  "yaml_ingestion":        "",
  "splunk_base":           "",
  "storm_portal_microservices": "",
  "storm_portal_arch_docs": "",
  "raytheon_consolidation": "",
  "ingersoll_crossdomain":  "",
  "next_role":              ""
}
```

---

## `data/process_log.json`

**Role.** Append-only audit trail for pipeline events. Read manually
(`type data/process_log.json | jq …`) to answer "when did X happen" and
"why was this job discarded". Surfaces don't read it — it's purely for
forensics.

**Lifecycle.**

- **Appended** by `ingest.append_log`, `update_status.append_log`, `comp_estimate.append_log`, `generate_cl.js:appendLog`, `scan_no_sponsorship.py`, `scan_foreign_locations.py`.
- **Never compacted** — grows monotonically. Not a problem at the user's volume.

### Schema (common fields)

| Field | Type | Notes |
|---|---|---|
| `log_id` | UUID v4 | Primary key. |
| `timestamp` | ISO datetime | UTC. |
| `session_date` | `YYYY-MM-DD` | Local date the event happened (handy when correlating with `daily_checklist.json`). |
| `event_type` | enum | One of the values below. |
| `entity_type` | `"job"` / `"company"` / `"application"` | What the event is about. |
| `entity_id` | UUID v4 / `null` | FK to the relevant table. Null for events that haven't created an entity yet (e.g. validation-failed discards). |
| `entity_name` | string | Denormalized human-readable label (`"Company — Title"` or `"Company"`). |
| `source_url` | URL string | optional | Present for ingest-related events. |
| `detail` | string | Free text. Each `event_type` has its own conventional body — see below. |

### Event types

| `event_type` | Written by | `detail` shape |
|---|---|---|
| `company_created` | `ingest.get_or_stub_company` | `"Stub record created for <name> — research pending on rank."` |
| `validation_summary` | `ingest.ingest_job` (success path) | `"Job ingested. Stack: X/35, Velocity: Y/5, Seniority: Z/25, Domain: W/20. Staleness: …"` |
| `job_discarded` | `ingest.ingest_job` (various gates) | `"Job discarded: <reason>"` — reasons include missing fields, JD too short, ethics-excluded company, JD refuses sponsorship (skipped for US roles), location not an enabled target geography (US off / not remote, or remote pinned to a foreign region like "Remote - India"). |
| `job_archived` | `scan_no_sponsorship.py`, `scan_foreign_locations.py`, `scan_stale_jobs.py` (the latter two also via `crawl.crawl`'s end-of-run sweeps; `scan_stale_jobs.py` additionally on every `/today` render via `serve.apply_stale_job_check`) | `"Retroactive archive: JD says no sponsorship (\"...<snippet>...\")."`, `"Retroactive archive: foreign-pinned remote location '...'."`, or `"Expired from pipeline: <n>d un-applied (> <N>d since ingest)."` |
| `application_logged` | `update_status.cmd_log` | `"Application logged. Method: <m>. Country: <c>. CL v<n>. Score at apply: <s>."` |
| `application_status_change` | `update_status.cmd_status` | `"Status: <old> → <new>."` |
| `cover_letter_generated` | `generate_cl.js` | `"Cover letter v<n> generated → <filename>"` |
| `comp_estimate_generated` | `comp_estimate.py` | `"base target <CUR> <n>; confidence <HIGH\|MED\|LOW>"` |
| `application_question_generated` | `answer_questions.generate_answer` | `"class=<motivation\|behavioral> version=<n> chars=<n> tokens_in=<n> tokens_out=<n>"` |
| `application_question_edited` | `answer_questions.save_edit` | `"version=<n> chars=<n>"` |
| `application_question_finalized` | `answer_questions.finalize_answer` | `"version=<n> chars=<n>"` |
| `drill_generated` | `drills.generate_drill` | `"<Generated\|Regenerated> Drill <n> (java): <title>"`. Also carries the full `prompt`, `interface`, and a `regenerated` bool on the event — the durable record of every drill prompt created (the `drills.json` store keeps only the latest active version). |
| `drill_reviewed` | `drills.review_drill` | `"Reviewed Drill <n>."` |

### Example

```json
[
  {
    "log_id": "9e3348ed-c15d-4da8-bed8-aba4f272d2c1",
    "timestamp": "2026-05-10T01:57:12.971015+00:00",
    "session_date": "2026-05-09",
    "event_type": "company_created",
    "entity_type": "company",
    "entity_id": "4de487cd-019c-4142-9019-51e8b62ac293",
    "entity_name": "Wavelo",
    "detail": "Stub record created for Wavelo — research pending on rank."
  },
  {
    "log_id": "29110620-a402-4d24-ad0f-cbd242cc2538",
    "timestamp": "2026-05-10T01:57:20.743399+00:00",
    "session_date": "2026-05-09",
    "event_type": "validation_summary",
    "entity_type": "job",
    "entity_id": "4e07dd6a-54fd-4187-aa79-6369c495e26c",
    "entity_name": "Wavelo — Principal Engineer, Product Development",
    "source_url": "https://www.linkedin.com/jobs/view/4391889341/",
    "detail": "Job ingested. Stack: 0/35, Velocity: 0/5, Seniority: 23/25, Domain: 12/20. Staleness: fresh."
  }
]
```

---

## `data/daily_checklist.json`

**Role.** Section-done flags for the `/today` UI, keyed by local date.
Lets the UI render checkmarks for completed sections without inferring
state from other files.

**Lifecycle.**

- **Read** by `serve.py:load_daily_state(date_iso)` on every `/today` render.
- **Written** by `serve.py:save_daily_state` via the `/today/toggle` POST handler when the operator clicks a section checkbox.

### Schema

Top-level object keyed by `YYYY-MM-DD` string. Each date's value is an
object with **any subset** of these boolean keys (absent ≡ `false`):

| Key | UI section |
|---|---|
| `status_updates` | Status updates |
| `crawl` | Crawl job boards |
| `linkedin_ingest` | LinkedIn alert ingest |
| `cover_letters` | Cover letters & apply |

Section IDs come from `serve.py:CHECKLIST_SECTIONS`.

> The **`cover_letters`** checkmark is **also auto-earned** (without a stored
> flag) once the day's logged applications reach `config.DAILY_APPLICATION_GOAL`
> — `serve.py:section_done` treats the section done when either the stored flag
> is `true` **or** `applications_today_count()` (derived from
> `application_tracker.date_applied == today()`) meets the goal. So a green
> `cover_letters` checkmark may appear on a date with no `cover_letters` key
> here. The manual toggle still works to force it done early.

### Example

```json
{
  "2026-05-17": {
    "crawl":           true,
    "status_updates":  true,
    "linkedin_ingest": true
  },
  "2026-05-18": {
    "status_updates":  true,
    "crawl":           true,
    "linkedin_ingest": true
  }
}
```

---

## `data/email_config.json`

**Role.** IMAP sender allowlist for `linkedin_fetch.py`. Only `UNSEEN`
messages whose `FROM` matches a sender in this list get pulled in.

**Lifecycle.**

- **Auto-created** by `linkedin_fetch.load_allowlist` on first call, populated with `DEFAULT_SENDERS = ["jobalerts-noreply@linkedin.com"]`.
- **Edited by hand** to add senders for other job-alert providers (e.g. `alerts@otta.com`, `jobs@builtin.com`).

### Schema

| Field | Type | Notes |
|---|---|---|
| `senders` | list[string] | Each is a bare email address used verbatim as an IMAP `FROM` filter. Empty list falls back to `DEFAULT_SENDERS`. |

### Example

```json
{
  "senders": [
    "jobalerts-noreply@linkedin.com",
    "alerts@otta.com"
  ]
}
```

---

## `data/email_state.json`

**Role.** Cross-run dedup state for `linkedin_fetch.py`. Records RFC 822
`Message-ID` headers of every alert email we've already harvested, so a
re-fetch (or a manual unflag of `\Seen`) won't re-stage the same jobs.

**Lifecycle.**

- **Updated** by `linkedin_fetch.add_seen_ids` after every successful fetch.
- **Cleared** entirely by `linkedin_fetch.reset_seen_state` (the `--reset` CLI flag), alongside removing `\Seen` from the corresponding messages on the server. Preserves `\Seen` on LinkedIn alerts the user read naturally.

### Schema

| Field | Type | Notes |
|---|---|---|
| `seen_message_ids` | list[string] | Sorted list of RFC 822 Message-IDs (the angle-bracketed `<…@host>` form). |

### Example

```json
{
  "seen_message_ids": [
    "<100477128.13855672.1776019839555@lor1-app126432.prod.linkedin.com>",
    "<102065253.30801307.1777575041794@ltx1-app24593.prod.linkedin.com>",
    "<1030564546.20656966.1777337440823@lor1-app91163.prod.linkedin.com>"
  ]
}
```

---

## `data/email_staged.json`

**Role.** Jobs parsed out of LinkedIn alert emails, awaiting per-row
review and ingest in the `/today` UI. Rows live here until the operator
either ingests them (which removes them) or discards them.

**Lifecycle.**

- **Appended** by `linkedin_fetch.fetch_via_imap` / `fetch_from_sample`.
- **Annotated** by `prefilter_staged.py` with `_prefilter_pass` and `_prefilter_reason`.
- **JD-enriched** by `serve.py:fetch_jd_for_staged` (the per-row "Fetch JD" button) — sets the `jd_text` field.
- **Bulk-discarded** by `serve.py:discard_failing_staged` (drops every row where `_prefilter_pass=False`).
- **Cleared** of corrupted `jd_text` by `scripts/cleanup_staged_jd.py` (sets `jd_text=""` if it matches LinkedIn's similar-jobs noise).
- **Removed** one-by-one by `serve.py:remove_staged` after ingest or manual discard.

### Schema

| Field | Type | Source | Notes |
|---|---|---|---|
| `staging_id` | hex string (12 chars) | `uuid.uuid4().hex[:12]` | Primary key; short to keep `/today` URLs readable. |
| `linkedin_job_id` | numeric string | LinkedIn URL | Dedup key against pre-existing staged rows. |
| `title` | string | parsed | LinkedIn's job-card title. |
| `company` | string | parsed | LinkedIn's "Company" line (before the U+00B7 middle dot). |
| `location` | string | parsed | LinkedIn's "Location" line (after the middle dot). |
| `apply_url` | URL string | parsed + normalized | Normalized via `_normalize_linkedin_url`: `/comm/` stripped + query string dropped. |
| `source_message_id` | string | RFC 822 | The originating email's Message-ID. |
| `source_subject` | string | RFC 822 | The originating email's Subject. |
| `fetched_at` | ISO datetime | client clock | When this row was staged. |
| `jd_text` | string | optional | Populated by per-row Fetch JD; `""` or missing otherwise. |
| `_prefilter_pass` | bool | optional | Set by `prefilter_staged.py`. |
| `_prefilter_reason` | string | optional | Set by `prefilter_staged.py`. Stable prefixes (`title seniority miss`, `title excluded by …`, `location miss`, `location US-gated …`, `stack score N < M`, `title+location ok (no JD yet)`, `stack N`). Note: rows for crawl-covered companies (`prefilter_staged.crawl_covered_companies`) are **deleted** by the pre-filter, not annotated — so `company crawl-covered` is no longer a stored reason value; those rows simply disappear from the file. |

### Example

```json
[
  {
    "staging_id":        "a3f9e2c1b7d4",
    "linkedin_job_id":   "4391889341",
    "title":             "Principal Engineer, Product Development",
    "company":           "Wavelo",
    "location":          "Toronto, ON (Remote)",
    "apply_url":         "https://www.linkedin.com/jobs/view/4391889341/",
    "source_message_id": "<100477128.13855672.1776019839555@lor1-app126432.prod.linkedin.com>",
    "source_subject":    "Principal Engineer, Product Development at Wavelo and 4 other roles",
    "fetched_at":        "2026-05-10T01:35:00+00:00",
    "_prefilter_pass":   true,
    "_prefilter_reason": "title+location ok (no JD yet)"
  }
]
```

When the staged list is empty (current state on disk), the file contains
just `[]`.

---

## `data/inbox_matches.json`

**Role.** Rejection / interview-request / offer emails found by `inbox_scan.py`,
matched to an open application and awaiting one-click review in the `/today`
"Status updates" section. Staged suggestions only — applying a match is always
an explicit operator action. The record stores the **raw email signal**
(`email_status`/`email_reason`); the concrete status shown/applied is derived at
surface time from that signal + the application's live status via
`config.suggest_status_transition` (so an interview email reads as a recruiter
screen for a still-`applied` role, an interview for one already screening, and a
rejection after any live contact as an interview failure).

**Lifecycle.**

- **Appended** by `inbox_scan.py` (`scan_via_imap` / `scan_from_sample`) for each newly-matched, newly-classified message.
- **Removed** one-by-one by `serve.py:remove_inbox_match` when the operator **Applies** (which shells out to `update_status.py status`) or **Dismisses** a match.
- **Cleared** entirely by `inbox_scan.py --reset` (alongside `inbox_scan_state.json`).
- A match stays out of the file once applied/dismissed because its Message-ID remains in `inbox_scan_state.json`, so a re-scan won't re-stage it. The `/today` renderer also hides any match whose application has since reached a terminal status.
- **Legacy fields.** Matches staged before the raw-signal split carry `suggested_status`/`suggested_reason` (the pre-resolved value) instead of `email_status`/`email_reason`. `serve.py:inbox_match_suggestion` falls back to them, so old files still render — but re-scanning (`--reset` first) restages under the current classifier.

### Schema

| Field | Type | Notes |
|---|---|---|
| `match_id` | hex string (12 chars) | Primary key. |
| `message_id` | string | Dedup key — RFC 822 `Message-ID`, or a synthesized `nomid:<digest>` when the header is absent. |
| `application_id` | string | The matched `application_tracker.application_id`. |
| `job_id` | string | The matched application's `job_id` (convenience). |
| `company_name` | string | From the matched application. |
| `title` | string | From the matched application. |
| `app_status` | string | The application's status at scan time. |
| `from_addr` | string | The email's `From` header (decoded, truncated). |
| `subject` | string | The email's `Subject` (decoded, truncated). |
| `received` | ISO date | The email's `Date` header as `YYYY-MM-DD` (`""` if unparseable). |
| `email_status` | string | The email's own signal: `"rejected"`, `"offer"`, or `"interview"` (advancement — recruiter screen OR interview invitation). Resolved to a concrete `update_status.py` status at surface time by `config.suggest_status_transition`. |
| `email_reason` | string \| null | A `REJECTION_REASONS` key (`position_filled` / `generic`) when `email_status="rejected"`; `null` otherwise. |
| `evidence` | string | Short snippet around the phrase that triggered the classification (operator context). |
| `detected_at` | ISO datetime | When the scan staged this match. |

### Example

```json
[
  {
    "match_id":         "9c2a71f0e4b8",
    "message_id":       "<CAF...@mail.gmail.com>",
    "application_id":   "b1e7…",
    "job_id":           "a0c4…",
    "company_name":     "Stripe",
    "title":            "Staff Software Engineer",
    "app_status":       "applied",
    "from_addr":        "Stripe Recruiting <no-reply@stripe.com>",
    "subject":          "Update on your application",
    "received":         "2026-07-20",
    "email_status":     "rejected",
    "email_reason":     "generic",
    "evidence":         "we have decided to move forward with other candidates",
    "detected_at":      "2026-07-21T14:03:00+00:00"
  }
]
```

---

## `data/inbox_scan_state.json`

**Role.** The inbox scanner's own cross-run dedup state — the `Message-ID`s
(or synthesized keys) of every message it has already staged. Kept separate
from the server-side `\Seen` flag on purpose: `inbox_scan.py` never marks mail
read, so this file is the only "already handled" signal, and reading a message
in your own client doesn't affect it.

**Lifecycle.**

- **Updated** by `inbox_scan.add_processed_ids` after staging matches.
- **Cleared** by `inbox_scan.py --reset` (alongside `inbox_matches.json`).

### Schema

| Field | Type | Notes |
|---|---|---|
| `processed_message_ids` | list[string] | Sorted list of Message-IDs / `nomid:<digest>` keys already staged. |

### Example

```json
{
  "processed_message_ids": [
    "<CAF...@mail.gmail.com>",
    "nomid:stripeupdateonyourapplication20260720"
  ]
}
```

---

## `data/drills.json`

**Role.** Claude-generated interview-prep coding drills for the `/today` "Code
drills" section. Each drill is a short, deliberately underspecified prompt with
a partial interface (method names + params, **no return types**). The actual
`Drill<N>.java` + `Drill<N>Test.java` live in the sibling Maven project
(`config.MANUAL_CODE_DRILLS_DIR`, default `../manual-code-drills`), **not** here;
this file only holds the generated prompts, status, and review feedback.

**Lifecycle.**

- **Written** by `scripts/drills.py generate` (`config.save_drills`). The number
  only advances once the current drill is marked **complete**: while the current
  drill is `active`, regenerating *replaces* it at the same number (a reroll);
  otherwise a new record takes the next number — one past the highest
  `Drill<N>.java` in the Maven project and the highest number in the store
  (`config.next_drill_number`). The store therefore keeps only the latest version
  of an active drill; the full history of every generated prompt lives in the
  process log (`drill_generated` events).
- **Mutated by** `scripts/drills.py review` (appends to `feedback`) and
  `serve.py` via `config.mark_drill_complete` (sets `status`/`completed_at`).
- **Read** by `serve.py` for the current drill (`config.current_drill`), the
  drills-completed-today meter (`config.drills_completed_today`), and
  `section_done`.
- **Never deleted.**

### Schema

A JSON array; each record:

| Field | Type | Notes |
|---|---|---|
| `number` | int | Drill number (continues the `Drill<N>.java` sequence). Primary key. |
| `language` | string | `"java"` (only language currently generated). |
| `title` | string | Short generated name. |
| `prompt` | string | The interview-style, underspecified problem statement. No hints. |
| `interface` | list[string] | Method-name-with-params strings, **without return types** (deciding those is part of the drill). |
| `status` | `"active"` / `"complete"` | `complete` once marked done. |
| `created_at` | ISO datetime | When generated. |
| `completed_at` | ISO datetime / `null` | Set by `mark_drill_complete`; its date drives the daily goal. |
| `feedback` | list[object] | Review history: `{"at": ISO, "text": <markdown feedback>}`, appended by each review. |

### Example

```json
[
  {
    "number": 3,
    "language": "java",
    "title": "Recent Activity Feed Tracker",
    "prompt": "Build a simple activity feed. Users log events (a user + an event type); support pulling the most recent N events, the distinct event types for a user, and the most active user…",
    "interface": ["record(String user, String eventType)", "recent(int n)", "typesFor(String user)", "mostActive()"],
    "status": "active",
    "created_at": "2026-07-23T23:28:08+00:00",
    "completed_at": null,
    "feedback": []
  }
]
```

---

## `data/crawl_log.jsonl`

**Role.** Per-run crawl summary. One JSON object per line, appended after
each `crawl()` call (including dry-runs). Used to spot regressions in the
funnel — e.g. "did `title_seniority` start blocking 99% of listings after
the rubric change?".

**Lifecycle.**

- **Appended** by `crawl._log_crawl_run`. Best-effort; never raises.
- **Never compacted.**

### Schema (per line)

| Field | Type | Notes |
|---|---|---|
| `ts` | ISO datetime | Seconds precision (`isoformat(timespec="seconds")`). |
| `duration_s` | int | Wall time of the crawl. |
| `dry_run` | bool | Whether `--dry-run` was passed. |
| `source_filter` | string / `null` | The `--source` flag value, or `null` for "all sources". |
| `total_fetched` | int | Total listings across all sources before any filtering. |
| `dedup_hits` | int | Listings rejected because the apply URL was already in the pipeline. |
| `filtered_total` | int | Listings rejected by the pre-filter. |
| `funnel` | object | Categorized rejection counts. Keys: `pass`, `title_seniority`, `title_exclude`, `location`, `stack`, `other`. Categories from `crawl._categorize_reason`. |
| `passed` | int | Listings that survived pre-filter. |
| `ingested` | int | Listings that successfully ingested. |
| `ingest_failed` | int | Listings that pre-filtered through but failed `ingest_job` (validation, ethics-exclude, no-sponsorship, etc.). |
| `auto_added_boards` | list[object] | New ATS boards discovered this run. Each item: `{"company": str, "ats": str, "slug": str}`. |

### Example

```json
{"ts": "2026-05-15T00:25:20+00:00", "duration_s": 215, "dry_run": false, "source_filter": null,
 "total_fetched": 8268, "dedup_hits": 6, "filtered_total": 8236,
 "funnel": {"title_seniority": 4806, "stack": 1114, "location": 1931, "title_exclude": 385, "pass": 26},
 "passed": 26, "ingested": 9, "ingest_failed": 17, "auto_added_boards": []}
```

---

## `data/jd_fetch_log.jsonl`

**Role.** Per-URL diagnostic record for every call to
`linkedin_fetch._fetch_jd_text`. Lets you tell auth-walls from expired
postings from genuinely-short JD bodies without re-fetching.

**Lifecycle.**

- **Appended** by `linkedin_fetch._log_jd_fetch`. Best-effort; never raises.
- **Never compacted.**

### Schema (per line)

| Field | Type | Always present? | Notes |
|---|---|---|---|
| `ts` | ISO datetime | ✅ | Seconds precision. |
| `url_in` | URL string | ✅ | The URL the caller passed in. |
| `url_resolved` | URL string | only on HTTP success | After redirects. Differs from `url_in` for `/comm/` strips, auth-wall redirects, expired-job redirects. |
| `status` | int | only on HTTP success | HTTP status code. |
| `raw_html_len` | int | only on HTTP success | Bytes of raw response body. |
| `content_type` | string | only on HTTP success | `Content-Type` response header. |
| `stripped_text_len` | int | only on parse | Length after BeautifulSoup + line normalization. |
| `winning_selector` | string / `null` | only on parse | First CSS selector that yielded ≥ `MIN_JD_LENGTH` chars (e.g. `"div.description__text--rich"`). `null` if no selector won. |
| `selector_hits` | list[[str, int]] | only on parse | Every selector that **matched an element** plus the element's stripped length. Diagnostic — tells you which selectors are present but too short. |
| `body_snippet` | string | only on `reason=short` | First 300 chars of stripped text. |
| `proxy_unverified` | object | optional | Set in `discover_boards_from_careers.py` only — present here for shared logging. Ignored in this file. |
| `exc_type` | string | only on `reason=exception` | Python exception class name. |
| `exc_msg` | string | only on `reason=exception` | First 200 chars of the exception message. |
| `reason` | enum | ✅ | `ok`, `auth_wall`, `expired`, `http_error`, `exception`, `short`. See below. |

### Reason values

| `reason` | Meaning |
|---|---|
| `ok` | JD fetched successfully and is ≥ 200 chars. |
| `auth_wall` | LinkedIn redirected to `/uas/login`, `login?session_redirect`, or `/ssr-login/passwordless-email-login`. Usually means IP-throttled — retry later. |
| `expired` | Resolved URL no longer contains `/jobs/view/` — LinkedIn redirected to a similar-jobs landing page (e.g. `?trk=expired_jd_redirect`). Posting is dead. |
| `http_error` | Non-200 response. |
| `exception` | `requests.get` threw (timeout, DNS, TLS, etc.). |
| `short` | Got 200 OK but post-strip text is < `MIN_JD_LENGTH`. |

### Example (success)

```json
{"ts": "2026-05-10T01:56:51+00:00",
 "url_in":       "https://www.linkedin.com/jobs/view/4391889341/",
 "url_resolved": "https://www.linkedin.com/jobs/view/4391889341/",
 "status": 200, "raw_html_len": 302253,
 "content_type": "text/html; charset=utf-8",
 "stripped_text_len": 6550,
 "winning_selector": "div.description__text--rich",
 "selector_hits": [["div.description__text--rich", 6550]],
 "reason": "ok"}
```

### Example (auth-wall via OTP redirect)

```json
{"ts": "2026-05-10T01:35:37+00:00",
 "url_in":       "https://www.linkedin.com/jobs/view/4391889341/?...&otpToken=…",
 "url_resolved": "https://www.linkedin.com/ssr-login/passwordless-email-login?…",
 "status": 200, "raw_html_len": 16395,
 "content_type": "text/html; charset=utf-8",
 "stripped_text_len": 153,
 "winning_selector": null,
 "selector_hits": [["main", 145]],
 "reason": "short",
 "body_snippet": "Sign in\nWe're signing you in\n…"}
```

---

## `data/board_discovery_log.jsonl`

**Role.** Diagnostic record for every careers-page scrape attempted by
`discover_boards_from_careers.py`. Lets you answer "why didn't Shopify's
careers page yield a board" without re-running the scrape.

**Lifecycle.**

- **Appended** by `discover_boards_from_careers._log`. Best-effort; never raises.
- **Never compacted.**

### Schema (per line)

| Field | Type | Always present? | Notes |
|---|---|---|---|
| `ts` | ISO datetime | ✅ | Seconds precision. |
| `company` | string | ✅ | Company name from `company_registry`. |
| `url_in` | URL string / `null` | ✅ | The careers URL the scraper tried. `null` for `api_probe_only` (no careers page). |
| `url_resolved` | URL string | only on HTTP success | After redirects. |
| `status` | int | only on HTTP success | HTTP status code. |
| `raw_html_len` | int | only on HTTP success | Bytes of raw response body. |
| `content_type` | string | only on HTTP success | `Content-Type` header. |
| `ats` | string | only on match | Detected ATS — see `crawl.detect_ats` patterns. |
| `slug` | string | only on match | The slug recorded in `target_boards.json` if accepted. |
| `hit_url` | string | only on `reason=match_<tag>_<attr>` | First 200 chars of the URL on the matching tag (`<a href>`, `<iframe src>`, `<script src>`). |
| `proxy_unverified` | object | optional | When a `gh_jid` / `ashby_jid` / `lever_jid` was found but `validate_ats_slug` rejected the guessed slug. Shape: `{"ats": str, "slug_tried": str}`. |
| `exc_type` | string | only on `reason=exception` | Python exception class name. |
| `exc_msg` | string | only on `reason=exception` | First 200 chars of the exception message. |
| `reason` | enum | ✅ | See below. |

### Reason values

| `reason` | Strategy |
|---|---|
| `match_redirect` | Resolved URL matched `detect_ats` (Strategy 1). |
| `match_a_href` / `match_iframe_src` / `match_script_src` | `detect_ats` matched a tag attribute (Strategy 2). |
| `match_raw_html` | `detect_ats` matched against the response body text (Strategy 3). |
| `match_proxy_validated` | Proxy query param hint + API-validated slug guess (Strategy 4). |
| `match_api_probe` | Blind API probe with the slug guess succeeded (Strategy 5). |
| `match_api_probe_no_page` | Same, but the company had no `job_portal_url` to scrape. |
| `no_ats_found` / `no_ats_found_no_page` | All strategies failed. |
| `http_error` | Non-200 response. |
| `exception` | `requests.get` threw. |

### Example (match)

```json
{"ts": "2026-05-11T06:39:43+00:00",
 "company":      "Babylist",
 "url_in":       "https://www.babylist.com/careers",
 "url_resolved": "https://www.babylist.com/about/careers",
 "status": 200, "raw_html_len": 100537,
 "content_type": "text/html; charset=utf-8",
 "reason": "match_api_probe",
 "ats": "greenhouse",
 "slug": "babylist"}
```

### Example (no match, with proxy hint)

```json
{"ts": "2026-05-11T06:39:41+00:00",
 "company":      "Shopify",
 "url_in":       "https://www.shopify.com/careers",
 "url_resolved": "https://www.shopify.com/careers",
 "status": 200, "raw_html_len": 524597,
 "content_type": "text/html; charset=utf-8",
 "proxy_unverified": {"ats": "ashby", "slug_tried": "shopify"},
 "reason": "no_ats_found"}
```

---

## Out-of-scope files

Files under `data/` that aren't part of the runtime schema:

| File | What it is |
|---|---|
| `*.bak` (and tagged variants like `*.precap.bak`) | Backups — auto-written by `rescore_all.py`, `scan_no_sponsorship.py`, and `scan_foreign_locations.py` before destructive changes, or saved by hand before manual migrations. Restore with `cp <name>.bak <name>`. Safe to delete once the corresponding migration is verified. |
| `_sample_linkedin_alert.eml` | Local sample for `linkedin_fetch.py --sample`. Not loaded at runtime. |
| `rescore_targets.txt`, `rescore_bucket_a_zeros.txt` | One-off plain-text lists of `job_id`s used as input to `rescore_all.py --job-ids-file`. Disposable. |
