# next-role — Architecture & Script Specifications

This is the engineer-facing companion to `README.md`. The README explains how
to install and use next-role; this document specifies what each script does
internally — every function, its parameters, and its role in the pipeline.

Two cross-cutting rules govern most of the code and are referenced throughout:

- **Scoring SSOT.** Composite weights, native score ranges, and the title-cap
  rules live exclusively in `scripts/config.py`. No other file may duplicate
  them or define a parallel `composite_score`. See `CLAUDE.md` for the full
  convention.
- **Company-filter SSOT.** `scripts/config.py:company_block_reason` is the
  only rule for "should this company be hidden from apply surfaces". It is
  consulted at apply time only, never at ingest or crawl time.

---

## Table of contents

**Foundational**

- [`scripts/config.py`](#scriptsconfigpy) — shared paths, constants, SSOTs, mechanical scoring
- [`scripts/geography.py`](#scriptsgeographypy) — location → country SSOT + geography gate (dependency-free; re-exported by config, callable by JS)

**Pipeline (ingest → score → research)**

- [`scripts/ingest.py`](#scriptsingestpy) — fetch + validate + score + write a single job
- [`scripts/score_jd.py`](#scriptsscore_jdpy) — Claude judgment for seniority + domain fit
- [`scripts/research_company.py`](#scriptsresearch_companypy) — two-tier company research (Haiku)
- [`scripts/crawl.py`](#scriptscrawlpy) — two-lane (aggregators + ATS direct) crawler
- [`scripts/prefilter_staged.py`](#scriptsprefilter_stagedpy) — relaxed pre-filter for staged LinkedIn rows
- [`scripts/linkedin_fetch.py`](#scriptslinkedin_fetchpy) — IMAP fetch of LinkedIn job-alert emails

**Surfaces (CLI + web)**

- [`run.py`](#runpy) — root CLI orchestrator
- [`serve.py`](#servepy) — local web UI (Flask-free stdlib HTTP)
- [`scripts/dashboard.py`](#scriptsdashboardpy) — terminal pipeline summary
- [`scripts/update_status.py`](#scriptsupdate_statuspy) — application logging + status transitions
- [`scripts/metrics.py`](#scriptsmetricspy) — read-only analytics behind the `/metrics` route

**Per-job utilities**

- [`scripts/comp_estimate.py`](#scriptscomp_estimatepy) — Opus-driven salary + bonus estimator
- [`scripts/generate_cl.js`](#scriptsgenerate_cljs) — Node-side `.docx` cover-letter generator

**One-off maintenance**

- [`scripts/rescore_all.py`](#scriptsrescore_allpy) — bulk re-score under a new rubric
- [`scripts/scan_no_sponsorship.py`](#scriptsscan_no_sponsorshippy) — retroactive no-sponsorship sweep
- [`scripts/scan_foreign_locations.py`](#scriptsscan_foreign_locationspy) — retroactive foreign-pinned-location sweep
- [`scripts/cleanup_staged_jd.py`](#scriptscleanup_staged_jdpy) — clear similar-jobs noise from staged rows
- [`scripts/backfill_target_boards.py`](#scriptsbackfill_target_boardspy) — discover ATS boards from existing pipeline
- [`scripts/discover_boards_from_careers.py`](#scriptsdiscover_boards_from_careerspy) — discover ATS boards from careers pages

---

## `scripts/config.py`

**Role.** Foundational module imported by every other script in the pipeline.
Holds repo paths, data-file locations, Claude model IDs, the two SSOTs
(scoring + company-filter), mechanical scoring helpers that don't require an
API call (stack-keyword match, velocity tier, freshness bonus, title-based
seniority cap, no-sponsorship detector), and JSON / date helpers. **No
business logic lives anywhere else that could equivalently live here** — if
you're tempted to redefine a constant in another script, it belongs in this
file.

### Module-level constants

| Name | Type | Purpose |
|---|---|---|
| `ROOT` | `Path` | Repo root (two levels above `config.py`). |
| `DATA_DIR` | `Path` | `<ROOT>/data` — gitignored pipeline JSON files. Auto-created. |
| `COMPANY_REGISTRY_PATH` | `Path` | `data/company_registry.json` — per-company research. |
| `JOB_PIPELINE_PATH` | `Path` | `data/job_pipeline.json` — every ingested job. |
| `APPLICATION_TRACKER_PATH` | `Path` | `data/application_tracker.json` — submitted applications. |
| `PROCESS_LOG_PATH` | `Path` | `data/process_log.json` — pipeline event log. |
| `TARGET_BOARDS_PATH` | `Path` | `data/target_boards.json` — ATS boards the crawler polls. |
| `CRAWL_LOG_PATH` | `Path` | `data/crawl_log.jsonl` — JSONL append-only crawler log. |
| `COMP_ESTIMATES_PATH` | `Path` | `data/comp_estimates.json` — comp-estimate results keyed by job_id. |
| `APPLICATION_QUESTIONS_PATH` | `Path` | `data/application_questions.json` — answer-questions records keyed by job_id (dict, not list). |
| `RESUME_ENTRY_NOTES_PATH` | `Path` | `data/resume_entry_notes.json` — global supplemental notes per resume-entry slug. |
| `PROFILE_DIR` | `Path` | `<ROOT>/profile` — gitignored user-rules directory. |
| `COVER_LETTER_RULES` | `Path` | `profile/cover_letter_rules.md` — tone + section structure. |
| `RESUME_PATH` | `Path` | `profile/resume.md` — active resume. |
| `SCORING_RUBRIC_PATH` | `Path` | `profile/scoring_rubric.md` — Claude system prompt for JD scoring. |
| `STACK_KEYWORDS_PATH` | `Path` | `profile/stack_keywords.yaml` — keyword weights + crawl pre-filter. |
| `ANSWER_QUESTIONS_RULES` | `Path` | `profile/answer_questions_rules.md` — system-prompt rules for `scripts/answer_questions.py`. |
| `RESUME_ENTRY_SLUGS` | `dict[str, str]` | Slug → human-readable label registry for the resume entries `answer_questions.py` can cite. SSOT — both the prompt and the UI chip picker read from here. |
| `OUTPUT_DIR` | `Path` | `<ROOT>/output` — generated `.docx` cover letters. Auto-created. |
| `ANTHROPIC_API_KEY` | `str` | Read from environment; module-level `EnvironmentError` if unset. |
| `CLAUDE_MODEL` | `str` | Sonnet 4.5 model ID — used for JD scoring. |
| `CLAUDE_MODEL_FAST` | `str` | Haiku 4.5 model ID — used for company research (~10× cheaper). |
| `CL_MODEL` | `str` | Sonnet 4.6 model ID — used for cover letters (`generate_cl.js`) and answer-questions (`answer_questions.py`). |
| `STACK_KEYWORDS` | `dict[str, int]` | Lowercased keyword → points map, loaded from YAML at import time. |
| `STACK_SCORE_MAX` | `int` | Cap for `compute_stack_score`; from YAML `max_score`. |
| `COMPONENTS` | `dict[str, ScoringComponent]` | **SSOT** for both scoring profiles' weights + native max per signal. |
| `COMPOSITE_MAX` | `int` | Sum of all `COMPONENTS[k].weight` — the full composite ceiling (130). |
| `PRE_RESEARCH_MAX` | `int` | Sum of all `COMPONENTS[k].pre_research_weight` — the pre-research composite ceiling (100). |
| `RESEARCH_QUEUE_MIN_SCORE` | `int` | Pre-research-score gate (45) for the research queue — jobs below this don't get research budget. |
| `US_SPONSORSHIP_SCORE` | `int` | Sponsorship floor (native 0-15, default 3) substituted for the company score on US-derived roles in `composite_score`. Thumb-on-scale so CA/IE generally outrank US; set to 0 for "zero added". Only consulted when `"US" in TARGET_COUNTRIES`. |
| Geography constants (`TARGET_COUNTRIES`, `REMOTE_ONLY_SOURCES`, the location token lists, region codes) | — | Defined in [`scripts/geography.py`](#scriptsgeographypy); re-exported here. |
| `VELOCITY_TIERS` | `list[(int, int)]` | `(max_days_since_posted, score)`; first match wins; default 0. |
| `FRESHNESS_TIERS` | `list[(int, int)]` | `(max_age_days, bonus)`; bonus stacks on top of velocity. |
| `STALENESS_TIERS` | `dict[str, (int, int)]` | Inclusive day-range per tier label (`fresh` / `soft_stale` / `hard_stale`). |
| `GHOSTED_DAYS` | `int` | Applications with no response after N days (21) auto-flip to `ghosted` (see `auto_age_application`). |
| `GHOSTED_REJECTED_DAYS` | `int` | A `ghosted` application still un-answered after N days (45) since applying auto-converts to `rejected` (reason `ghosted_timeout`). Must exceed `GHOSTED_DAYS`. |
| `REJECTION_REASONS` | `dict[str, str]` | SSOT for rejection-reason key → human label: `generic`, `position_filled`, `interview_failed`, `ghosted_timeout`. Consumed by serve.py status buttons and `metrics.py`. |
| `GOV_SCREEN_FLAGGED_REGIONS` | `list[str]` | User-editable ISO 3166-1 alpha-2 region codes for the gov/defense screen's tier_c escalation. Empty by default (region logic dormant). |
| `GOV_SCREEN_FLAG_PENALTY_PCT` | `int` | Apply-rank penalty (%) for a gov-screen `flag` result. Consumed by `gov_screen_penalty_factor` / `apply_rank_score`. |
| `GOV_SCREEN_SUPPORT_ROLES_EXPOSED` | `bool` | Whether support engineering counts as `exposed` (follow-the-sun ticket routing). Read by `classify_role_exposure`. |
| `GOV_DEFENSE_FLAGS` / `ROLE_EXPOSURES` | `tuple[str, ...]` | Valid values for `gov_defense_flag` (`none`/`tier_c`/`tier_b`/`tier_a`) and `role_exposure` (`insulated`/`ambiguous`/`exposed`). |
| `GOV_SCREEN_INTERVIEW_QUESTIONS` | `list[str]` | Role-clarity questions surfaced when the combination matrix emits them. |
| `_GOV_SCREEN_MATRIX` | `dict` | Part 3 combination matrix: `{flag: {exposure: (result, emit_questions)}}`. Consumed by `gov_screen_result`. |
| `MAX_ACTIVE_APPS_PER_COMPANY` | `int` | Apply-time throttle — hide a company once N in-flight apps exist (3). |
| `IN_FLIGHT_STATUSES` | `frozenset[str]` | What "in-flight" means for the throttle: `applied`, `recruiter_screen`, `interview`. `ghosted` is intentionally excluded so dead apps free the slot. |
| `_SENIORITY_BUCKETS` | `list[(str, Pattern, int)]` | Ordered (bucket, regex, cap) — first match wins. Used by `title_seniority_cap`. |
| `_NO_SPONSORSHIP_PATTERNS` | `list[Pattern]` | Regexes that detect explicit no-sponsorship language in JD text. Single source consumed via `detect_no_sponsorship`. |
| `_EMPLOYEE_SURVEILLANCE_RE` | `Pattern` | Word-boundary regex (`employee\|worker\|workforce`) matched against an ethics-flag description. Consumed via `is_employee_surveillance_flag`. |
| `_MASS_SURVEILLANCE_DESC_RE` | `Pattern` | Alternation regex for mass-surveillance indicators (`mass surveillance`, `facial recognition`, `law enforcement`, `intelligence agencies`, `predictive policing`, `border surveillance`, `spyware`, `government surveillance`). Consumed via `is_mass_surveillance_flag`. |
| `_DEFENSE_INDUSTRY_RE` | `Pattern` | Word-boundary regex (`defense\|defence\|military\|weapons\|munitions\|armaments`) matched against the company's `industry` field. Consumed via `is_defense_contractor`. |

### Side effects at import time

- Reconfigures `sys.stdout` / `sys.stderr` to UTF-8 with `errors="replace"` so
  Windows cp1252 default doesn't crash on Claude's em-dashes or the
  dashboard's box-drawing characters.
- Creates `DATA_DIR` and `OUTPUT_DIR` if missing.
- Raises `EnvironmentError` immediately if `ANTHROPIC_API_KEY` is unset — by
  design, so every entry point fails fast on misconfig.
- Loads `STACK_KEYWORDS` and `STACK_SCORE_MAX` from
  `profile/stack_keywords.yaml`; missing file raises `FileNotFoundError`.

### Classes

#### `ScoringComponent`
Frozen dataclass — one entry in the `COMPONENTS` SSOT. Carries one
weight per scoring profile.

- **Fields**
  - `weight: int` — contribution to the **full composite** (display denominator for `composite_score`).
  - `native_max: int` — max value the underlying stored field can hold (its storage scale; shared by both profiles).
  - `pre_research_weight: int` — contribution to the **pre-research composite** (display denominator for `composite_score_pre_research`). Set to `0` for company-derived signals (sponsorship, remote) so stub defaults can't bias the research-queue ordering.
- **Property `multiplier -> float`** — `weight / native_max`. How much each stored point contributes to the full composite. Returns `0.0` if `native_max` is 0.
- **Property `pre_research_multiplier -> float`** — `pre_research_weight / native_max`. Same shape, for the pre-research composite.

### Functions

#### `_load_stack_keywords(path: Path) -> tuple[dict, int]`
Internal loader called once at module import. Reads the stack-keyword YAML
and returns `(keywords_dict, max_score)` with all keys lowercased and values
coerced to `int`. Raises `FileNotFoundError` with a remediation hint if the
file is missing.

#### `_sanitize(obj)`
Recursively strips surrogate characters (UTF-16 lone halves) from any
strings inside a nested structure. Called by `save_json` so that Windows
text scraped by Beautiful Soup doesn't break `json.dump`.

- **Parameters:** `obj` — any JSON-serializable value (str / list / dict / scalar).
- **Returns:** the same shape with strings re-encoded.

#### `load_json(path: Path) -> list`
Reads a JSON file from disk and returns the parsed value. Returns `[]` (not
`None`) if the file doesn't exist — every caller expects an iterable.

#### `save_json(path: Path, data: list) -> None`
Writes `data` to `path` as pretty-printed UTF-8 JSON (`indent=2`,
`ensure_ascii=False`). Runs `_sanitize` first so surrogate chars don't
poison the dump.

#### `sanitize_answer_text(text: str) -> str`
Strip / replace characters unsafe for plain-text application-form inputs
on generated answers, then collapse AI-tell dash patterns to commas.
Char-level pass (via `_ANSWER_SANITIZE_MAP`) replaces smart quotes with
ASCII, normalizes non-breaking space to a regular space, and removes
bullets / middle dots / asterisks / hashes / `<` / `>`. Regex pass then
collapses em-dashes, en-dashes, and double-hyphens (with any surrounding
whitespace) to `", "`, plus single hyphens with whitespace on both sides
when not flanked by digits (so `"5-10"` and `"well-known"` survive
intact). Final cleanup collapses `", , ,"` chains and double spaces, then
trims.

Called by `answer_questions.generate_answer` before persisting Claude's
output AND by `answer_questions.save_edit` before persisting an operator
edit — same policy in both paths. The copy button binds to the stored
value, so no second pass is needed at copy time.

Module-level state:
- `_ANSWER_SANITIZE_MAP` — char replacement table.
- `_DASH_PROSE_RE`, `_DASH_SINGLE_RE`, `_COMMA_CHAIN_RE`, `_MULTISPACE_RE` — pre-compiled regex passes applied in order.

#### `today() -> str`
Returns today's date as an ISO `YYYY-MM-DD` string. **Always use this — never
hardcode** so the pipeline reads as time-aware.

#### `now_utc() -> str`
Returns current UTC datetime as an ISO 8601 string (with timezone offset).
Used for `*_at` timestamp fields and the process log.

#### `days_since(iso_date: str) -> int`
Number of full days between an ISO date string and today. Raises
`ValueError` on malformed input — callers like `compute_freshness_bonus`
catch it and degrade gracefully.

#### `title_seniority_cap(title: str) -> tuple[str, int]`
Classifies a job title into one of four seniority buckets and returns
`(bucket_letter, max_seniority_score)`:

| Bucket | Examples | Cap |
|---|---|---|
| **A** — at target | Staff, Senior Staff, Tech Lead, Architect | 25 |
| **B** — one step below | Senior, Sr. | 15 |
| **C** — one step above | Principal | 15 |
| **D** — out of range | Distinguished, Fellow, VP, Junior, Intern, Associate Engineer, Senior Principal | 0 |

Order matters: more specific patterns (e.g. `Senior Staff`, `Senior
Principal`) appear before broader ones (`Senior`, `Principal`) so substring
matches are first-wins. Defaults to `("A", 25)` if no bucket matches —
under-cap rather than silently zero an unfamiliar title.

#### `apply_title_cap(raw_seniority: int, title: str) -> int`
Clamps a raw Claude seniority score (0-25) by the title bucket's cap.
Negative inputs are floored at 0.

#### `strip_company_boilerplate(jd_text: str) -> str`
Truncates trailing company boilerplate (About / EEO / Benefits / Pay Range
Transparency / Compliance) before keyword scoring. Searches only the
**trailing half** of `jd_text` for any pattern in `_BOILERPLATE_MARKERS`
(Greenhouse `content-conclusion` / `content-pay-transparency` divs, HTML
heading wrappers like `<strong>About …</strong>`, plaintext heading lines,
RemoteOK's spam-protector tag) and cuts at the earliest match. Returns the
input unchanged if no marker matches. The half-only safety bound prevents
in-body section headings like "About this role" from triggering truncation.

#### `compute_stack_score(jd_text: str) -> int`
Mechanical keyword scan of the JD. Calls `strip_company_boilerplate` first
so keywords appearing only in the trailing About / EEO / Pay-Range sections
don't count (e.g. "Apache Spark" in Databricks' About blurb appeared on
every Databricks JD, even pure-frontend roles). Then sums points for every
keyword in `STACK_KEYWORDS` whose **word-boundary regex** matches the
lowercased body — `\bjava\b` no longer matches `javascript`. Caps at
`STACK_SCORE_MAX`. No Claude call — runs both in the pipeline (post-fetch)
and in the pre-filter (pre-Claude) on the **full JD** (the pre-filter no longer
truncates to a prefix — a prefix window dropped roles whose stack keywords
appeared later and was stricter than ingest's own scoring; the boilerplate
strip + word-boundary matching apply identically in both paths).

#### `compute_velocity_score(date_posted: str | None) -> int`
Walks `VELOCITY_TIERS` and returns the score for the first tier whose
`max_days` exceeds the posting age. Returns 0 for missing dates or jobs
older than the last tier. Native range `0..5`; `composite_score` multiplies
by `COMPONENTS["velocity"].multiplier`.

#### `compute_staleness(date_posted: str | None) -> str`
Returns a label `"fresh"` (< 30 days), `"soft_stale"` (30-59 days), or
`"hard_stale"` (≥ 60 days). Missing dates default to `"fresh"` rather than
penalize the row.

#### `compute_freshness_bonus(job: dict) -> int`
Day-grained bonus stacking on top of velocity. Recomputed on every call
(not stored) so the bonus decays naturally as the job ages. Prefers
`job["date_posted"]`; falls back to the date portion of `job["date_found"]`.
Returns 0 on parse failure.

#### `detect_no_sponsorship(jd_text: str) -> str | None`
Scans JD text for explicit no-sponsorship language. Returns a 20-char-padded
snippet around the first match (for logging), or `None` if no refusal is
found. Patterns deliberately err on false negatives — each requires an
explicit negation token near the word "sponsor". Caller (`ingest.py`) owns
the discard decision. **Callers skip this for US-derived roles** (the operator
is a US citizen) — see `ingest.ingest_job` and `scan_no_sponsorship.py`.

#### Geography functions (re-exported from `geography.py`)
`derive_country`, `is_remote_role`, `names_foreign_location`, `location_passes`,
plus `TARGET_COUNTRIES` / `REMOTE_ONLY_SOURCES`, are **defined in
`scripts/geography.py`** (dependency-free) and re-exported here so
`from config import derive_country` etc. work unchanged. See the
[`scripts/geography.py`](#scriptsgeographypy) section for their docs.

#### `is_employee_surveillance_flag(flag: dict) -> bool`
True iff an ethics flag (as produced by `research_company`) is a confirmed
employee-targeted surveillance flag. Trigger: `status == "confirmed"` AND
`category == "surveillance"` AND the description matches
`_EMPLOYEE_SURVEILLANCE_RE` (`\bemployee|worker|workforce\b`). The narrow
description filter prevents customer-data/regulatory/seller surveillance
descriptions (e.g. RBC's KYC obligations, eBay's seller-fraud monitoring)
from triggering exclusion — only employee-targeting language does.

#### `is_mass_surveillance_flag(flag: dict) -> bool`
True iff an ethics flag is confirmed mass surveillance — `status ==
"confirmed"` AND `category == "surveillance"` AND the description matches
any term in `_MASS_SURVEILLANCE_DESC_RE` (law enforcement, intelligence
agencies, facial recognition, predictive policing, border surveillance,
spyware, government surveillance). Targets the Palantir / Clearview / NSO
/ ShotSpotter class of company.

#### `is_defense_contractor(company: dict) -> bool`
True iff the company's `industry` field matches `_DEFENSE_INDUSTRY_RE`
(`defense|defence|military|weapons|munitions|armaments`). Pure
industry-field match — does not inspect ethics_flags. Intentionally
excludes ambiguous tokens like `aerospace` and `intelligence` alone, both
of which false-positive on commercial industries.

#### `company_auto_exclude_reason(company: dict) -> str | None`
Unified entry point for the three deterministic `ethics_hard_exclude`
auto-triggers. Returns a short reason string for the first rule that
fires, or `None` if no rule applies. Order: `is_defense_contractor` (fastest
check, industry-only), then per-flag rules `is_employee_surveillance_flag`,
`is_mass_surveillance_flag` (in the order each flag appears).
`research_company.research_company` calls this after merging Tier-2 flags
and flips `ethics_hard_exclude` to True if a reason is returned. The same
function powers the retroactive sweep over the existing registry.

#### `composite_score(job: dict, company: dict | None) -> int`
**The only full-composite function in the codebase.** Reads each stored
score off the job + company dicts, applies `COMPONENTS[k].multiplier` to
each, and returns the integer total. Used for apply-time ranking and
cover-letter selection.

US sponsorship floor: when `"US" in TARGET_COUNTRIES` **and**
`derive_country(job["location"]) == "US"`, the `sponsorship` input is replaced
by `US_SPONSORSHIP_SCORE` (a low floor) instead of the company
`sponsorship_score` — a thumb-on-scale so CA/IE generally outrank US without a
hard tier. This is a country-conditional on the existing input, **not** a new
component or parallel composite; when US is off the branch never fires and
CA/IE composites are byte-identical. `composite_score_pre_research` is
unaffected (it already zero-weights sponsorship).

- **Parameters**
  - `job` — a record from `job_pipeline.json`.
  - `company` — the matching record from `company_registry.json`, or `None` if research hasn't run yet (sponsorship + remote default to 0).
- **Returns:** `int` in `[0, COMPOSITE_MAX]`.

#### `composite_score_pre_research(job: dict) -> int`
**The only pre-research composite function in the codebase.** Sums the
five components whose data is available at ingest time (stack, domain,
seniority, velocity, freshness) weighted by
`COMPONENTS[k].pre_research_multiplier`. Sponsorship and remote-fit are
intentionally zero-weighted because their values come from company
research; using them with stub defaults (sponsorship=7/15, remote=3/5)
creates a ~23-point baseline that dominates rankings.

Used by `run.py:research_queue` to pick which stub companies to research
next. **Never** use for apply-time ranking — see `CLAUDE.md` rule 4.

- **Parameters:** `job` — a record from `job_pipeline.json`. No company argument.
- **Returns:** `int` in `[0, PRE_RESEARCH_MAX]`.

#### `company_block_reason(company_id: str | None, apps: list[dict]) -> str | None`
**The only company-throttle rule.** Returns a short human-readable reason
string if the company should be hidden from apply surfaces, or `None` if it
can be shown.

- **Parameters**
  - `company_id` — the company's ID to test (returns `None` immediately if falsy).
  - `apps` — every record from `application_tracker.json`. The function does the company-id filter itself.
- **Behavior:** counts applications at this company whose status is in `IN_FLIGHT_STATUSES` and which have no `response_date` set. If the count reaches `MAX_ACTIVE_APPS_PER_COMPANY`, returns `f"{n} active applications"`.
- **Called by:** `serve.py:render_cover_letters_body` and `run.py:generate_cover_letters`. **Not** called by crawl, prefilter, or ingest — those layers are intentionally permissive.

#### `auto_age_application(app: dict) -> bool`
**SSOT for the two time-based status transitions.** Mutates one application
record in place and returns `True` iff it changed. Applies, in order:
`applied → ghosted` after `GHOSTED_DAYS`, then `ghosted → rejected` after
`GHOSTED_REJECTED_DAYS` (setting `rejection_reason="ghosted_timeout"` and
appending an explanatory note). Records with a `response_date`, or already
past these states, are untouched; the auto-rejection leaves `response_date`
`null` on purpose. **Called by** `serve.py:apply_ghosted_check` and
`update_status.cmd_list` so the web and CLI never diverge. Distinct from the
company throttle — this advances application *status*; it is not a cooldown.

#### `normalize_role_title(title: str) -> str`
Reduces a title to a comparable core: lowercases, drops any specialization
after the first comma or `(`, strips punctuation, collapses whitespace.
`"Staff II Software Engineer, Data Ingestion"` and `"Staff II Software
Engineer"` both → `"staff ii software engineer"`. Used only by
`find_duplicate_application`.

#### `find_duplicate_application(company_id, title, apps, exclude_app_id=None) -> dict | None`
Apply-time duplicate guard. Returns an existing application at the same
`company_id` whose `normalize_role_title` matches `title` (regardless of that
app's status), else `None`. **Called by** `update_status.cmd_log` (blocks the
log unless `--force`) and `serve.py:render_cl_row` (warning badge + Mark-Applied
confirm). Catches the same role reposted under a different listing URL, which
`ingest.check_duplicate` (exact-URL, non-archived only) cannot see.

#### `classify_role_exposure(title, claude_exposure=None) -> str`
Gov-screen role exposure resolver (`insulated` | `ambiguous` | `exposed`).
Deterministic title rules win first (SA / professional services / forward-
deployed / sales / TAM / support, with support gated by
`GOV_SCREEN_SUPPORT_ROLES_EXPOSED`); else falls back to Claude's JD-level
judgment, defaulting `insulated`. Called by `ingest.py` (which has the title
`score_jd` doesn't).

#### `reconcile_gov_defense_flag(company) -> str`
Resolves a company's `gov_defense_flag`, forcing it to `tier_a` for industry-
detected defense contractors (`is_defense_contractor`) regardless of the LLM's
classification — mirrors the `ethics_hard_exclude` floor. Called in
`research_company` after the Haiku merge.

#### `gov_screen_result(gov_defense_flag, role_exposure) -> tuple[str, bool]`
SSOT for the Part 3 combination matrix. Returns `(result, emit_questions)`,
result ∈ `pass`/`flag`/`fail`. Unknown inputs degrade to `none`/`insulated`.
**Derived on display** (serve.py) from the live company flag + the job's stored
`role_exposure`; the result is never persisted, so re-research can't leave a
stale value.

#### `gov_screen_penalty_factor(job, company) -> float`
`1 - GOV_SCREEN_FLAG_PENALTY_PCT/100` when the gov-screen result is `flag`,
else `1.0` (`fail` is handled by exclusion, not penalty). Consumed by
`apply_rank_score`.

#### `apply_rank_score(job, company) -> int`
Apply-time ranking value = `composite_score(job, company)` × the gov penalty
factor. A thin wrapper over the canonical composite (not a parallel/partial
composite) plus a documented policy factor. Used **only** at the two apply-time
sort sites (`serve.render_cover_letters_body`, `run.generate_cover_letters`);
`composite_score` stays the displayed score everywhere else, and `metrics.py`
is unaffected.

#### `gov_screen_block_reason(job, company) -> str | None`
Apply-time exclusion predicate parallel to `company_block_reason`: returns a
reason when the gov-screen result is `fail` (tier_a / defense entanglement),
else `None`. Hides the role from the apply queue + cover-letter generation
without touching ingest.

---

## `scripts/geography.py`

**Role.** Single source of truth for location → country derivation and the
geography pre-filter gate. **Dependency-free** (stdlib only — no API key, no
yaml, does NOT import `config`) for two reasons: (1) `config.py` re-exports
everything here so every Python consumer shares one implementation, and (2) the
Node cover-letter generator can't import Python, so it calls this module as a
subprocess instead of carrying a parallel JS copy (which kept drifting —
California, Galway, Toronto). `TARGET_COUNTRIES` lives here (a geography
concern); `US_SPONSORSHIP_SCORE` stays in `config.py` (a scoring concern).

### Module-level constants

| Name | Purpose |
|---|---|
| `TARGET_COUNTRIES` | **SSOT** for active target geographies (currently `{"CA","IE","US"}`; remove `"US"` to disable US remote-only roles). Read by `config.composite_score` and `location_passes`. |
| `_IE_/_CA_/_US_LOCATION_TOKENS` | Space-padded location substrings for `derive_country`. IE/CA before US; no bare `"us"`. Canada isn't detected by bare `"CA"` (collides with California). |
| `_CA_PROVINCE_CODES` / `_US_STATE_CODES` | Two-letter codes matched only in an anchored "City, XX" form by `_has_region_code`. US states omit `in`/`de`/`co` (country-code collisions). |
| `REMOTE_ONLY_SOURCES` | Boards where every listing is remote (`remoteok`, `remotive`). |
| `_REMOTE_LOCATION_TOKENS` | Substrings denoting remote (`remote`, `anywhere`, `worldwide`, `distributed`). |
| `_FLEXIBLE_LOCATION_TOKENS` | `worldwide`/`anywhere`/`global`/`americas`/… — an OTHER role with one passes; checked before the denylist. |
| `_FOREIGN_LOCATION_TOKENS` | Non-target regions (UK, India, EU/EMEA, LATAM, APAC, …); an OTHER role pinned to one is rejected. Operator-editable. |

### Functions

#### `derive_country(location: str) -> str`
Maps a free-text location to `"CA" | "IE" | "US" | "OTHER"`. Padded-substring
match; IE/CA before US so a combined "Remote, Canada/US" resolves to the
sponsorship-bearing country. Canada is matched by name / Canadian city /
**province code** ("London, ON" → CA), so bare `"CA"` resolves to **California
(US)** ("San Francisco, CA" → US, "Toronto, CA" → CA).

#### `_has_region_code(padded_loc, codes) -> bool`
`True` if the padded lowercased location holds one of `codes` in an anchored
"City, XX" / "(XX)" form (comma/paren + trailing boundary stop full country
names and embedded letters).

#### `is_remote_role(location, source=None) -> bool`
`True` if the location text says remote (`_REMOTE_LOCATION_TOKENS`) **or** the
listing came from a `REMOTE_ONLY_SOURCES` board. Used by `location_passes` and
`ingest.ingest_job` (the stored `job_type`).

#### `names_foreign_location(location) -> bool`
`True` if pinned to a non-target region (`_FOREIGN_LOCATION_TOKENS`), with
`_FLEXIBLE_LOCATION_TOKENS` winning first. Only meaningful for OTHER locations.

#### `location_passes(location, enabled_countries=None, source=None) -> bool`
Pre-filter-safe subtractive gate (no Claude/composite). **US** kept only if
enabled AND remote; **CA/IE** always kept; **OTHER** kept unless
`names_foreign_location`. Layered after the YAML `location_allow` allowlist;
only ever subtracts. Called by `crawl.pre_filter`,
`prefilter_staged.pre_filter_relaxed`, and `ingest.ingest_job`.

#### CLI (`python scripts/geography.py "<location>"`)
Prints `derive_country(argv[1])`. No API key required — used by
`generate_cl.js` to get the country without re-implementing derivation in JS.

---

## `scripts/score_jd.py`

**Role.** Claude judgment layer for the two scores that aren't mechanical.
Called by `ingest.py` after stack / velocity / freshness have already been
computed; writes `seniority_score`, `domain_fit_score`, and `score_notes`
back to the job record. Also runs standalone as a CLI for re-scoring a JD
file, an existing pipeline row, or stdin — useful when the rubric changes.

Calls Sonnet (`CLAUDE_MODEL`) with `profile/scoring_rubric.md` as the system
prompt and the JD as the user message; requires only the two numeric scores
in the JSON response (`score_notes` defaults to `""` when omitted — it's
display-only and must not break ingest); clamps the integers to the rubric
ranges (0-25 seniority, 0-20 domain); then applies the title-based seniority
cap mechanically (see
`apply_title_cap` in `config.py`) because the model has been observed
reclassifying Principal titles based on JD scope language.

Also passes through an optional `role_exposure` (gov-screen JD judgment) when
the model returns a valid value (`insulated`/`ambiguous`/`exposed`), else
`None` — intentionally **not** required so a model miss can't break ingest.
`ingest.py` resolves the final value via `config.classify_role_exposure`
(deterministic title rules over this raw judgment) and stores it on the job.

### Functions

#### `_load_rubric() -> str`
Reads `profile/scoring_rubric.md` and returns the contents as the Claude
system prompt. Raises `FileNotFoundError` with a remediation hint if the
file is missing (i.e. the user hasn't copied `profile.example/`).

#### `score_jd(jd_text: str, title: str | None = None) -> dict`
The library entrypoint. Calls Claude with the JD, parses + validates the
response, clamps to rubric ranges, and (if `title` is provided) applies the
title cap.

- **Parameters**
  - `jd_text` — raw JD text. Sanitized for surrogate chars before sending.
  - `title` — optional job title. When provided and the cap reduces the score, the function also records `seniority_raw` (the pre-cap value) and `seniority_cap_title` for audit. Pass `None` to skip the cap (e.g. scoring a JD outside the pipeline).
- **Returns:** `dict` with `seniority_score: int (0..25)`, `domain_fit_score: int (0..20)`, `score_notes: str`. May also include `seniority_raw: int` and `seniority_cap_title: str` if the cap fired.
- **Raises:** `ValueError` if Claude's response is not parseable JSON or is missing either required numeric score (`seniority_score` / `domain_fit_score`). `score_notes` and `role_exposure` are optional — missing values default to `""` and `None` respectively. Markdown code fences are stripped before parsing.

#### `update_job_record(job_id: str, scores: dict) -> None`
Loads the pipeline, finds the row matching `job_id`, writes back
`seniority_score`, `domain_fit_score`, `score_notes`, and a fresh
`scored_at` ISO timestamp, then saves.

- **Raises:** `ValueError` if no job matches `job_id`.

#### `main() -> None`
CLI driver. Mutually exclusive input modes:

- `--jd FILE` — read JD text from a file path.
- `--job-id UUID` — read `jd_text` and `title` from `job_pipeline.json` and write scores back to that record.
- `--stdin` — read JD text from stdin (useful for `Get-Clipboard | python score_jd.py --stdin`).

Prints scores to stdout in all modes. Persists only when `--job-id` is
passed — file/stdin runs are dry-run by design.

---

## `scripts/comp_estimate.py`

**Role.** One-shot compensation estimator for a single job. Reads the job
and (optionally) its company record, derives a currency from the location,
loads the resume, calls Opus 4.7 with a structured prompt, validates the
JSON response against a strict schema, and upserts the result into
`data/comp_estimates.json` keyed by `job_id`. Output is consumed by the
`/today` cover-letters surface and the per-job detail page in `serve.py`.

Uses Opus 4.7 (not the pipeline default Sonnet 4.5) because Opus has deeper
salary-band knowledge and matches the user's prior manual workflow on
claude.ai. One job per invocation by design — this is not a batch tool.

### Module-level constants

| Name | Purpose |
|---|---|
| `COMP_MODEL` | Anthropic model ID — Opus 4.7. |
| `MAX_TOKENS` | 1500 — enough for the full JSON response with room for reasoning. |
| `_CAD_HINTS`, `_EUR_HINTS`, `_GBP_HINTS`, `_USD_HINTS` | Substring tuples used by `derive_currency` to map location text to a currency. |
| `_HQ_TO_CURRENCY` | Company-HQ ISO country code → currency fallback when location text doesn't match any hint. |
| `_REQUIRED_TOP`, `_REQUIRED_BASE`, `_VALID_CLASSIFICATIONS`, `_VALID_CONFIDENCE` | Schema constants consumed by `validate`. |

### Functions

#### `derive_currency(location: str, company_hq: str | None = None) -> str`
Deterministic mapping from a free-text location (and optional HQ ISO code)
to one of `CAD` / `EUR` / `GBP` / `USD`. Falls back to USD when nothing
matches.

- **Parameters**
  - `location` — the job's location field (free text).
  - `company_hq` — optional ISO country code from the company registry; used only when no location hint matches.
- **Returns:** ISO 4217 currency code (string).

#### `build_system_prompt(resume_text: str, currency: str) -> str`
Constructs the multi-section Claude system prompt: candidate resume, base
salary methodology (p50 / p85 / p90 with rationale for the p85 ask anchor),
bonus-component classifications (`Expected` / `Possible` / `Unusual` /
`Stated-in-JD`), confidence bands, the asymmetric-risk note, and the
required JSON output schema. Currency is interpolated so the model emits
amounts in the right unit.

#### `build_user_message(job: dict, company: dict | None, currency: str) -> str`
Builds the user message: company name, title, location, currency, optional
company-context bits (industry, size tier, HQ country, Glassdoor rating,
recent-layoffs flag), and the JD text. If `jd_text` is empty, an explicit
note tells the model to estimate from metadata alone and lower its
confidence accordingly.

#### `parse_comp_json(raw: str) -> dict`
Tolerant JSON parser. Handles three cases in order:
1. ` ```json ... ``` ` fence — extract between fences.
2. Generic ` ``` ... ``` ` fence — same.
3. Leading prose before a `{` — slice from the first `{` to the last `}`.

Then `json.loads`. Raises `JSONDecodeError` if all three fall through.

#### `validate(result: dict) -> None`
Strict schema check on the parsed response. Verifies all top-level keys are
present, `base` has `min` / `max` / `target`, each of `year_end_bonus` /
`signon` / `relocation` / `equity` is an object with valid `classification`
and `reason`, and `confidence` is `HIGH` / `MED` / `LOW`. Raises
`ValueError` with a pointed message on the first violation.

#### `call_claude(system: str, user_message: str) -> tuple[str, int, int]`
Single Anthropic API call. Returns `(response_text, input_tokens,
output_tokens)`. No retry — failures propagate to `main` for clean exit.

#### `load_estimates() -> list[dict]`
Reads `data/comp_estimates.json` and returns the list of records (empty
list if file missing or empty).

#### `upsert_estimate(record: dict) -> None`
Replaces any existing record with the same `job_id`, then appends the new
one. Persists with `save_json`.

#### `append_log(event: dict) -> None`
Appends a timestamped event to `data/process_log.json`. Used by `main` to
record `comp_estimate_generated` events.

#### `main() -> int`
CLI driver and only entrypoint. Exit codes:

- `0` — success (or dry-run completed).
- `2` — job_id not found, or resume missing.
- `3` — Anthropic API call failed.
- `4` — response wasn't valid JSON.
- `5` — schema validation failed.

CLI flags:

- `--job-id UUID` (required) — the job to estimate.
- `--currency CODE` (optional) — override the deterministic currency mapping.
- `--dry-run` — print the result to stdout and skip persistence + log.

Pipeline: load job → load company → derive currency → load resume → build
prompts → call Claude → parse → validate → upsert + log → print summary
(base range, target ask, confidence). On `--dry-run`, the validated result
is printed and nothing is written.

---

## `scripts/ingest.py`

**Role.** Single-job ingest pipeline. Fetches a JD (or accepts pasted text),
validates it, deduplicates against the pipeline, looks up or stubs the
company, applies the no-sponsorship hard discard, runs mechanical scoring,
calls `score_jd` for the Claude judgment scores, opportunistically
auto-adds the apply URL's ATS board to `target_boards.json`, and writes the
finished record to `job_pipeline.json`. Called both from the CLI
(`run.py --url`, `serve.py /ingest`) and as a library from `crawl.py`.

The function `get_or_stub_company` deliberately stubs unknown companies
with neutral defaults rather than blocking — research is deferred to the
top-N stub flow in `run.py`. The ethics hard-exclude check is the only
ingest-time company gate.

### Module-level constants

| Name | Purpose |
|---|---|
| `MIN_JD_LENGTH` | 200 — minimum substantive JD length; validation rejects shorter texts. |

### Functions

#### `append_log(entry: dict) -> None`
Appends a UUID + ISO-timestamped record to `data/process_log.json`. Used
throughout the module to log job discards and successful ingests.

#### `fetch_jd_from_url(url: str) -> str`
HTTP GET the posting and extract the JD body. Uses a Chrome user-agent,
15s timeout, strips `<script>`/`<style>`/`<nav>`/`<header>`/`<footer>`/
`<aside>`, then tries a list of common JD container selectors (Lever
`[data-qa]`, Greenhouse `#content`, generic `.job-description`, `main`,
`article`). Falls back to full body text if no selector yields
`MIN_JD_LENGTH` chars.

- **Raises:** `ValueError` on non-200 responses or redirects to a generic
  careers-home (dead-link heuristic). `requests.RequestException` on
  network errors.

#### `validate_job(title: str, apply_url: str, location: str, jd_text: str) -> list[str]`
Returns a list of failure reasons; empty list = pass. Checks all four
fields are non-empty/whitespace and that `jd_text` is at least
`MIN_JD_LENGTH` chars.

#### `check_duplicate(apply_url: str, jobs: list) -> dict | None`
Returns an existing active job record matching the apply URL, or `None`.
Archived records don't block re-ingest — by design, since you might want
to revisit a position that closed and reopened.

#### `get_or_stub_company(company_name: str) -> dict | None`
Case-insensitive lookup in `company_registry.json`. If found:
- returns `None` if `ethics_hard_exclude=True` (caller treats this as a discard).
- returns the existing record otherwise.

If not found, builds a stub record via `research_company.build_registry_record`,
sets `stub=True` so `run.py:research_top_stubs` knows to research it later,
logs a `company_created` event, and returns the stub.

#### `ingest_job(apply_url, company_name, title, location, jd_text, date_posted, source) -> dict | None`
Full per-job pipeline:

1. Sanitize text fields (strip surrogates + whitespace).
2. Deduplicate against pipeline by apply URL.
3. Validate; on failure, log `job_discarded` and return `None`.
4. Geography gate: `config.location_passes(location)`; on fail (US off / not
   remote) log `job_discarded` and return `None`. Mirrors the pre-filters so a
   manual `--paste` is gated too.
5. Look up or stub the company; if ethics-excluded, log + return `None`.
6. Lazy-import `crawl.detect_ats` + `crawl.auto_add_board` to record the
   ATS board the URL points at (circular-import dance).
7. Run `detect_no_sponsorship` on the JD; if matched, log + return `None`
   (before any Claude call). **Skipped for US-derived roles**
   (`derive_country(location) == "US"`) — the operator is a US citizen, so US
   "no sponsorship" boilerplate isn't disqualifying.
8. Compute mechanical scores (`compute_stack_score`,
   `compute_velocity_score`, `compute_staleness`).
9. Call `score_jd.score_jd(jd_text)` for seniority + domain (+ raw
   `role_exposure` judgment).
10. Assemble the record (UUIDs, ISO timestamps, default flags), resolving
   `role_exposure` via `config.classify_role_exposure(title, …)`, append to
   `job_pipeline.json`, save, log `validation_summary`, return the record.

- **Parameters:** all required. `source` is a free-text label (`direct_scrape`, `manual`, `remoteok`, `lever`, ...).
- **Returns:** the persisted job dict, or `None` if discarded at any gate.

#### `main() -> None`
CLI driver. Two mutually exclusive modes:

- `--url URL [--company NAME] [--title TITLE] [--location LOC] [--posted DATE]` — fetches the JD; prompts at the terminal for any required field not provided as a flag.
- `--paste --company NAME --title TITLE --location LOC --apply-url URL [--posted DATE]` — reads JD from stdin (for JS-rendered portals).

Exits 1 if `--paste` is missing required flags or if URL fetch fails.

---

## `scripts/crawl.py`

**Role.** Two-lane automated discovery. Lane 1 hits public aggregator APIs
(RemoteOK, Remotive) using your `aggregator_tags` / `aggregator_keywords`;
Lane 2 polls every board listed in `data/target_boards.json` directly
(Greenhouse, Lever, Ashby). Both lanes feed into one mechanical pre-filter
(title allowlist + title blocklist + location allowlist + mechanical stack
score), then surviving listings go through `ingest.ingest_job`. ATS boards
detected in aggregator apply URLs are auto-added to `target_boards.json`,
so the curated list grows on its own.

Pre-filter is **intentionally pre-LLM** — it must not call `composite_score`
or any function that requires research output. Doing so would multiply API
costs by ~1000× (raw aggregator output vs. the few that pass filtering).
The pre-filter SSOT is `profile/stack_keywords.yaml` under the `crawl:` key.

### Module-level constants

| Name | Purpose |
|---|---|
| `HEADERS` | `User-Agent` for outbound HTTP calls. |
| `CRAWL_CONFIG_DEFAULTS` | Defaults for the crawl config dict when no YAML override is present. |
| `SUPPORTED_ATSES` | `{"greenhouse", "lever", "ashby"}` — ATSes with a `fetch_*` implementation. `detect_ats` may return others (Workday, SmartRecruiters); they get recorded for visibility but skipped at fetch time. |

### Functions

#### `load_crawl_config() -> dict`
Reads `profile/stack_keywords.yaml`, merges the `crawl:` section over
`CRAWL_CONFIG_DEFAULTS`, and normalizes types (lowercase strings, int
`min_pre_filter_score`). Returns a defaults-only copy when the YAML is
missing.

#### `html_to_text(html: str) -> str`
Strips tags via BeautifulSoup and rejoins non-empty lines. Used to
normalize aggregator-provided HTML JD bodies into plain text.

#### `title_excluded(title_lower: str, terms: list[str]) -> str | None`
Word-aware membership check used by both `pre_filter` (here) and
`prefilter_staged.pre_filter_relaxed`. Returns the first term in `terms`
that appears as a whole word inside `title_lower`, otherwise `None`.
Surrounds each term with non-letter lookarounds (`(?<![a-z])TERM(?![a-z])`)
so a single-word term like `"intern"` does not match inside a longer
word like `"international"`; multi-word terms (`"solutions architect"`)
and terms with non-letter chars (`"jr."`, `"entry-level"`) work too.
Both inputs MUST already be lowercase — callers lowercase the title
per-row and the terms once at config-load time.

#### `pre_filter(title, location, text, cfg, source=None) -> tuple[bool, str]`
Returns `(passes, reason_string)`. Cheap mechanical gate run on every raw
listing. Reasons start with stable prefixes (`title seniority`, `title
excluded`, `location`, `stack score`) so `_categorize_reason` can bucket
them in the funnel log without parsing free text. `title_exclude` uses
`title_excluded` for word-aware matching. The positive location check passes
if the YAML `location_allow` matches **or** `config.derive_country(location)`
is in `TARGET_COUNTRIES` (so target cities the allowlist doesn't enumerate —
"Galway", "Montreal", province codes — aren't dropped). It then applies the
`config.location_passes` subtractive gate (US remote-only + foreign-pinned
reject; reason prefix `location US-gated …`, so it buckets under `location` in
the funnel); `source` is forwarded so the remote check is source-aware
(remote-only boards count region-only US locations as remote).

#### `detect_ats(url: str) -> tuple[str, str] | None`
Pattern-matches an apply URL against five ATS shapes (Greenhouse hosted
boards + API URLs, Lever US/EU, Ashby, Workday, SmartRecruiters) and
returns `(ats_name, slug)` if any match. Returns `None` if no pattern
matches.

#### `auto_add_board(company: str, ats: str, slug: str, added_via: str = "auto_discovery") -> bool`
Appends `{company, ats, slug, added: today(), added_via}` to
`target_boards.json` unless `(ats, slug)` is already present. Returns
`True` only on a new write. `added_via` records provenance
(`auto_discovery`, `ingest`, `backfill_pipeline`, `careers_page_scrape`)
for later audit.

#### `_get(url: str) -> requests.Response | None`
HTTP GET wrapper with timeout + `raise_for_status` + caught-and-printed
exception. Returns `None` on any failure so callers can `if not resp:
continue`.

#### `_ts_to_date(ms: int | None) -> str | None`
Converts a Unix-millis epoch (Lever's `createdAt`) into a `YYYY-MM-DD`
string. Returns `None` if input is falsy.

#### `fetch_remoteok(cfg: dict) -> list[dict]`
Hits `https://remoteok.io/api?tags=<comma-separated>` once per
`aggregator_tag_groups` entry. Dedupes by apply URL within the call.
Returns a list of normalized listing dicts with the schema:
`{title, company, location, apply_url, jd_text, date_posted, source}`.

#### `fetch_remotive(cfg: dict) -> list[dict]`
Hits `https://remotive.com/api/remote-jobs?category=software-dev&search=<kw>`
once per `aggregator_keyword_groups` entry. Same output schema as
`fetch_remoteok`.

#### `fetch_greenhouse(slug: str, company: str) -> list[dict]`
Polls `https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true`.
Same output schema. `date_posted` uses Greenhouse's `updated_at`.

#### `fetch_lever(slug: str, company: str) -> list[dict]`
Polls `https://api.lever.co/v0/postings/<slug>?mode=json`. Prefers Lever's
`descriptionPlain` but falls back to assembling `description` +
`lists[].text` + `lists[].content[]` + `additional[].text` and HTML-stripping
the result. `date_posted` is `createdAt` (Unix millis).

#### `fetch_ashby(slug: str, company: str) -> list[dict]`
Polls Ashby's job-board JSON endpoint. Same output schema. Falls back to
constructing a job URL from the slug + posting ID if `jobPostingUrl` is
absent.

#### `_categorize_reason(reason: str) -> str`
Maps the `pre_filter` reason string to a stable funnel category
(`title_seniority`, `title_exclude`, `location`, `stack`, `other`) for
JSONL logging.

#### `_log_crawl_run(record: dict) -> None`
Appends one JSON line to `data/crawl_log.jsonl` summarizing the run
(duration, totals, funnel breakdown, auto-added boards). Best-effort —
never raises.

#### `crawl(dry_run=False, verbose=False, source=None, limit=None) -> int`
Main entry. Returns the number of jobs ingested.

- **Parameters**
  - `dry_run` — print candidates without calling `ingest_job`.
  - `verbose` — print pre-filter decision (pass/fail + reason) for every listing.
  - `source` — restrict to one of `remoteok`/`remotive`/`greenhouse`/`lever`/`ashby`. Default is all.
  - `limit` — cap ingest count after pre-filter.
- **Side effects:** appends to `crawl_log.jsonl`, may grow `target_boards.json` via `auto_add_board`, calls `ingest_job` for each passing candidate, and (non-dry-run only) runs `scan_foreign_locations.archive_foreign_pinned(apply=True)` as a best-effort self-cleaning sweep — a no-op unless the foreign denylist expanded or a stray foreign row slipped in.

#### `main() -> None`
CLI shim around `crawl()`. Flags: `--dry-run`, `--verbose`, `--source NAME`, `--limit N`.

---

## `scripts/prefilter_staged.py`

**Role.** Apply a relaxed version of the crawl pre-filter to LinkedIn-staged
rows in `data/email_staged.json`. Mutates each surviving row in place to add
`_prefilter_pass: bool` and `_prefilter_reason: str`; rows for crawl-covered
companies are **deleted** outright rather than annotated (see `main`). The
`/today` UI reads these to badge rows green/red and to show a "discard all
failing" action.

"Relaxed" means: rows that don't have a JD body yet (typical for raw
LinkedIn URLs that auth-wall) skip the stack-score check rather than
auto-failing for missing keywords that would appear in the JD anyway.
Re-running this script after a JD is pasted upgrades the row to full
filtering.

Same pre-LLM constraint as `crawl.pre_filter` — see the SSOT banner in
`config.py`.

### Module-level constants

| Name | Purpose |
|---|---|
| `STAGED_PATH` | `data/email_staged.json` — the file mutated in place. |
| `MIN_JD_LENGTH` | 200 — threshold above which stack scoring is applied. |

### Functions

#### `_normalize_company(name: str) -> str`
Lowercase + whitespace-collapse a company name for matching.

#### `crawl_covered_companies() -> set[str]`
Normalized names of companies that already have a **crawlable** ATS board
(`ats in crawl.SUPPORTED_ATSES`) in `target_boards.json`. Used to **delete**
LinkedIn staged rows for companies the ATS crawl already covers comprehensively
(duplicate review work). Conservative exact-name match. Workday/SmartRecruiters
boards do **not** count as covered until a fetcher exists (i.e. once they're in
`SUPPORTED_ATSES`), so the deletion automatically tracks crawl capability.

#### `pre_filter_relaxed(title: str, location: str, jd_text: str, cfg: dict) -> tuple[bool, str]`
Like `crawl.pre_filter` but skips the stack-score check when `jd_text` is
shorter than `MIN_JD_LENGTH`. Lazy-imports `compute_stack_score` so the
no-JD path stays fast. Uses `crawl.title_excluded` for word-aware
`title_exclude` matching, the same positive location gate (YAML `location_allow`
**or** `derive_country` in `TARGET_COUNTRIES`), and the same
`config.location_passes` subtractive gate (reason prefix `location US-gated …`).
Returns `(passes, reason)`.

#### `main() -> None`
Loads `email_staged.json`. For each row it first checks
`crawl_covered_companies()` — if the row's company already has a crawlable
board, the row is **deleted** (dropped from the list written back, not just
flagged) so it never clutters the failing queue; otherwise the row runs through
`pre_filter_relaxed` and is kept with `_prefilter_pass` / `_prefilter_reason`
set. Writes the surviving list back and prints a machine-readable last line
`PREFILTER: passed=<n> failed=<n> deleted=<n>` (where `deleted` counts the
crawl-covered rows removed) that `serve.py:run_linkedin_prefilter` parses for
its flash message.

---

## `scripts/research_company.py`

**Role.** Two-tier company research, called from `run.py:research_top_stubs`
(typical flow) and as a standalone CLI for refreshing a single record.

- **Tier 1 (free):** Claude's training knowledge via Haiku — stable facts
  like industry, size, HQ, sponsorship history, remote patterns, Glassdoor
  baseline, ethics issues. No web search.
- **Tier 2 (1 web search):** A single targeted query
  (`<name> Glassdoor Blind rating layoffs ethics lawsuit 2025 2026`) for
  recent layoffs and new ethics flags that training data may miss. Live
  Glassdoor/Blind sentiment overrides Tier 1 if discernible.

Cost target: ~$0.03-0.05 per company vs $0.27+ with open-ended web search.

### Module-level constants

| Name | Purpose |
|---|---|
| `TIER1_SYSTEM` | Multi-section system prompt: scoring bands for sponsorship + remote, ethics categories + statuses, the gov/defense `gov_defense_flag` tier guidance (folded in — no extra search), required JSON schema. |
| `TIER2_SYSTEM` | System prompt for the single-search recency check; same JSON schema for the merge. |

### Functions

#### `_parse_json_response(message) -> dict`
Walks Claude's `content` blocks, concatenates `text`, strips markdown
fences (` ```json `, ``` ``` ```, or raw `{…}`), and returns the parsed
dict. Raises `ValueError` on parse failure.

#### `research_company(name: str, model: str = CLAUDE_MODEL_FAST) -> dict`
Calls Tier 1 then Tier 2 and merges:

- Tier 2 `recent_layoffs` / `layoff_notes` overwrite Tier 1.
- Tier 2 `glassdoor_rating` overrides if non-null.
- Tier 2 sentiment fields override if not `"unknown"`.
- Tier 2 `new_ethics_flags` are appended to Tier 1's flags.
- After merging, `config.company_auto_exclude_reason` is consulted; if any
  deterministic rule fires (defense contractor / employee surveillance /
  mass surveillance), `ethics_hard_exclude` is forced `True` (additive —
  never downgrades an LLM-set `True`) and the reason is printed.
- `gov_defense_flag` is resolved via `config.reconcile_gov_defense_flag`
  (Haiku's value, floored to `tier_a` for defense industries); `flag_evidence`
  is coerced to a list of strings. The Tier-1 user message also injects
  `GOV_SCREEN_FLAGGED_REGIONS` so region-based tier_b/tier_c can fire.
- `sponsorship_score` clamped to `0..15`, `remote_fit` clamped to `0..5`.

Default `model` is Haiku (`CLAUDE_MODEL_FAST`); pass Sonnet for higher-stakes
runs.

#### `build_registry_record(research: dict, existing_id: str | None = None) -> dict`
Wraps the research dict in a full registry record: UUID, default values
for advisory fields (including `gov_defense_flag` → `"none"` and
`flag_evidence` → `[]`), `record_created` / `record_updated` timestamps.
`confirmed_clean` starts `False` so `upsert_company` can preserve it
when updating.

#### `upsert_company(record: dict) -> tuple[str, bool]`
Case-insensitive name-matched upsert into `company_registry.json`. If a
match exists, preserves `company_id`, `record_created`, and
`confirmed_clean` from the old row. Returns `(company_id, created)`
where `created=True` only on insert.

#### `main() -> None`
CLI driver. Mutually exclusive: `--name NAME` (research new or refresh by
name) or `--company-id UUID` (refresh by ID — looks up the name from the
registry first). Prints a multi-field summary including any ethics flags.

---

## `run.py`

**Role.** Root CLI orchestrator. Sequences crawl → ingest → top-N research
→ cover letters → dashboard in a single command, with each step optional.
Imports SSOT helpers from `scripts/config.py` and shells out to other
scripts via `subprocess` for the heavy work (`ingest.py`,
`research_company.py`, `generate_cl.js`, `dashboard.py`).

The cover-letter selection uses **the full composite via `composite_score`**
and applies `company_block_reason` for the throttle — never reimplement a
partial score or company filter here.

### Module-level constants

| Name | Purpose |
|---|---|
| `ROOT`, `SCRIPTS`, `DATA_DIR`, `OUTPUT_DIR` | Path constants. |

### Functions

#### `run_python(script: str, *args) -> int`
Spawns `python scripts/<script> <args>` with the current interpreter,
returning the exit code. Output streams to the parent terminal.

#### `run_node(script: str, *args) -> int`
Spawns `node scripts/<script> <args>`. Same contract.

#### `ingest_url(url: str, posted: str = None, dry_run: bool = False) -> bool`
Shells out to `scripts/ingest.py --url <url> [--posted <date>]`. Returns
`True` on exit code 0. With `dry_run=True`, prints and returns without
calling the script.

#### `ingest_url_file(filepath: str, dry_run: bool = False) -> int`
Reads a text file of URLs (one per line, optional ` YYYY-MM-DD` after
the URL, `#` comments allowed) and calls `ingest_url` per line. Returns
the count of successful ingests. Exits 1 if the file doesn't exist.

#### `_execute_research(queue: list, dry_run: bool, label: str) -> int`
Shared inner loop for both research entry points. Takes a list of
`(score, job, company)` tuples already deduped by company, shells out
to `research_company.py --name <name>` for each, clears the `stub` flag
on success, and prints per-row progress. `label` prefixes each log line
so the operator can tell which ranking surfaced the candidate
(`"job score before research"` vs `"pre-research score"`). With
`dry_run=True`, prints what would be researched without calling Claude.
Returns the number processed.

#### `research_queue(n: int, dry_run: bool = False) -> int`
**Preferred research entry point.** Ranks active jobs by
`composite_score_pre_research(job)` — which zero-weights sponsorship +
remote — so stub-default values can't bias the ordering. Applies the
`RESEARCH_QUEUE_MIN_SCORE` gate (jobs scoring below are not eligible
for research budget). Picks the top N distinct stub companies and runs
`_execute_research` on them.

- **Returns:** number of companies actually researched (or counted in dry-run).
- **Wired to CLI** as `--research-queue [N]` (default 20 if no value).

#### `research_top_stubs(n: int, dry_run: bool = False) -> int`
**Inherited surface.** Ranks active jobs by full `composite_score(job,
company)` and picks the top N stub companies (within the first
`max(n*3, 15)` ranked jobs, deduped by `company_id`). Stub-default
sponsorship + remote values influence this ordering, which is why
`research_queue` exists.

- **Returns:** number of companies actually researched.
- **Wired to CLI** as `--research-top N` (preserved for backwards compatibility — prefer `--research-queue` for routine use).

#### `generate_cover_letters(top_n: int = 5, auto: bool = False) -> None`
Selects candidates from `active` / `cover_letter_ready` jobs whose company
isn't blocked by `company_block_reason` **or** `gov_screen_block_reason`
(gov/defense `fail`), sorts by `apply_rank_score` (full composite minus the
gov-screen `flag` penalty), takes the top N, prints them with their score, and:

- If `auto=True`, generates a `.docx` for each.
- Otherwise prompts `y/n/<comma-list>` and generates for the chosen rows.

Country selection for the cover letter uses `config.derive_country`
(`CA`/`IE`/`US`/`OTHER`). `US` roles are passed **without** `--country` so
`generate_cl.js` omits the work-auth paragraph (US citizen — none expected);
ambiguous-location (`OTHER`) roles fall back to `CA` (the operator's default
market). Otherwise the resolved code is passed to `generate_cl.js --country`.

#### `main() -> None`
Argparse entry point. Flags:

- Ingest: `--url URL`, `--url-file FILE` (mutually exclusive), `--posted DATE`.
- Pipeline: `--crawl`, `--research-queue [N]` (default N=20), `--research-top N`, `--cover-letters`, `--auto-cl` (implies `--cover-letters`), `--dashboard`.
- Modifiers: `--dry-run`, `--top N` (default 5).

Order of operations: crawl → ingest → research-queue → research-top → cover letters → dashboard.
When no action flag is passed, defaults to `--dashboard`. `--dashboard` is
also auto-appended whenever the run ingested anything.

`--research-queue` honors `--dry-run` (prints the queue without spending
API credits). `--research-top` skips entirely on `--dry-run` (inherited
behavior; do not change).

---

## `serve.py`

**Role.** Local web UI on `http://localhost:5000`. Single-file stdlib
HTTP server (`BaseHTTPRequestHandler`) with three top-level surfaces:

- `/` — single-URL ingest form. Auto-fetches JD; falls back to a paste
  textbox for JS-rendered pages (Workday, etc.).
- `/today` — daily checklist with four collapsible sections: status
  updates, crawl, LinkedIn ingest, cover letters & apply. Each section
  has its own POST handlers; the page round-trips state through query
  params (`?open=<section>&view=<view>`).
- `/pipeline`, `/resume`, `/job/<id>`, `/search` — supporting views. The
  top-nav search box (rendered by `page()` on every surface) submits to
  `/search?q=...` for finding a role by company name or title — built
  for recruiter-call prep, where you need to locate an applied / in-flight
  role and pull up its JD, company research, and application timeline.

Imports `composite_score`, `company_block_reason`, etc. from the
**scoring + company-filter SSOTs in `scripts/config.py`**. Never inline
a partial composite for sort or display — see `CLAUDE.md` for the SSOT
convention.

LinkedIn ingest is the most complex flow: emails are fetched by a
subprocess to `scripts/linkedin_fetch.py`, parsed into staged rows,
pre-filtered by `scripts/prefilter_staged.py`, then per-row ingested via
`scripts/ingest.py --paste`. JD bodies are fetched on demand per row by
calling `linkedin_fetch._fetch_jd_text`.

### Module-level constants

| Name | Purpose |
|---|---|
| `ROOT`, `SCRIPTS`, `DATA_DIR`, `OUTPUT_DIR` | Path constants. |
| `APPLICATION_TRACKER_PATH` | Local copy of the tracker path (also imported from `config.py`). |
| `MIN_JD_LENGTH` | 200 — JD body length threshold (mirrors ingest). |
| `DAILY_CHECKLIST_PATH`, `EMAIL_STAGED_PATH` | Daily-checklist state + LinkedIn staged-rows file. |
| `CHECKLIST_SECTIONS` | Ordered `(id, title, hint)` for the four `/today` sections. |
| `STATUS_ACTION_MAP` | Button-value → `(status, rejection_reason \| None)` for status updates POSTed from `/today`. The reason key (SSOT `config.REJECTION_REASONS`) is passed to `update_status.py status --rejection-reason`; includes `rejected_interview_failed`. |
| `CRAWL_TAIL_MAX`, `INGESTED_RE` | Background crawl: tail-line cap (50) + regex to capture `Ingested: N` from stdout. |
| `crawl_state_lk`, `crawl_state` | Threading lock + state dict for the background crawl. |
| `LINKEDIN_REQUIRED_ENV` | `("NEXTROLE_IMAP_HOST", "NEXTROLE_IMAP_USER", "NEXTROLE_IMAP_APP_PASSWORD")`. |
| `_linkedin_flash`, `_cl_flash`, `_research_flash` | One-shot flash-message slots. `_linkedin_flash` + `_cl_flash` render in their respective `/today` sections; `_research_flash` is popped from both the cover-letters body and `/job/<id>` so the result of "Research now" follows the user back wherever they were. |
| `CL_RENDER_CAP` | 30 — rows visible in the cover-letters section by default. |
| `RESUME_MD_PATH`, `PROFILE_LINKS`, `_MONTH_ABBREVS`, `_DATE_RANGE_RE` | Resume-snippet parsing config (Experience + Education sections of `profile/resume.md`). |
| `_STATE_AT_END_RE` | Regex for trailing US state codes in education entries. |
| `_SCRIPTS_ON_PATH` | Mutex: whether `scripts/` has been inserted on `sys.path` (lazy). |
| `STYLE` | Inline CSS — every page emits this once. |

### Routes

#### `GET /` — ingest form (default landing).
#### `GET /today?open=<section>&view=<view>` — daily checklist.
#### `GET /today/crawl/status` — JSON: background crawl state for polling.
#### `GET /pipeline` — full ranked table of active jobs.
#### `GET /search?q=<text>` — find a role by company name or title (case-insensitive substring across non-archived pipeline entries; results link to `/job/<id>`).
#### `GET /metrics` — read-only analytics dashboard rendered from `scripts/metrics.py:build_metrics()`.
#### `GET /resume` — Experience + Education snippet builder.
#### `GET /job/<id>` — per-job detail (composite breakdown, JD viewer, comp panel, company-research card).
#### `GET /answer-questions?job_id=<uuid>` — ad-hoc application question answerer page for one job. Two question lists (motivation, behavioral) + the global resume-entry-notes editor. Reachable from the "Answer Questions" button on every cover-letters row.
#### `POST /ingest` — handle the ingest form; auto-fetch JD or render paste form.
#### `POST /today/crawl/start` — kick off background crawl worker.
#### `POST /today/linkedin/fetch` — shell out to `linkedin_fetch.py`.
#### `POST /today/linkedin/ingest` — promote one staged row to a full pipeline ingest via `ingest.py --paste`.
#### `POST /today/linkedin/prefilter` — shell out to `prefilter_staged.py`.
#### `POST /today/linkedin/discard_failing` — drop every staged row with `_prefilter_pass=False`.
#### `POST /today/linkedin/fetchjd` — on-demand JD fetch for a single staged row.
#### `POST /today/linkedin/discard` — drop one staged row by `staging_id`.
#### `POST /today/cl/generate` — shell out to `generate_cl.js --job-id`.
#### `POST /today/comp/estimate` — shell out to `comp_estimate.py --job-id`.
#### `POST /today/company/research` — shell out to `research_company.py --company-id`; strip the stub flag on success and flash the result. Used by the "Research now" button on the stub banner in `render_company_card` and the stub badge in `render_cl_row`. Honors `return_to` so the user lands back on `/job/<id>` (or the cover-letters apply queue).
#### `POST /today/cl/open` — open generated `.docx` in the OS default app.
#### `POST /today/cl/archive` — flip job to `archived` (e.g. closed posting).
#### `POST /today/apply/log` — shell out to `update_status.py log`.
#### `POST /today/toggle` — flip a section's done flag in `daily_checklist.json`.
#### `POST /today/status` — shell out to `update_status.py status`.
#### `POST /answer-questions/add` — create a new question (JSON in/out: `{job_id, question_text, question_class, char_cap}` → `{ok, card_html, error}`). Driven by `scripts/answer_questions.py:add_question`.
#### `POST /answer-questions/delete` — delete a draft question (JSON). Refuses to delete finalized questions.
#### `POST /answer-questions/generate` — generate/regenerate an answer for one question (JSON). Blocks the request thread for the Sonnet 4.6 call (~15–30s). Appends a new draft version; never overwrites `finalized_answer`.
#### `POST /answer-questions/save-edit` — persist a manually-edited answer as a new draft version with `source="manual_edit"`. Body: `{job_id, question_id, answer}`. Runs through `sanitize_answer_text` before storage. Driven by the editable answer textarea + "Save edit" button on each card.
#### `POST /answer-questions/finalize` — snapshot the latest draft as the finalized answer (JSON). The client-side click handler in `_AQ_PAGE_JS` first checks whether the editable answer textarea has an unsaved edit (compares `value` against the dirty-baseline `dataset.savedValue`); if dirty, it POSTs to `/answer-questions/save-edit` first so the finalize uses the operator's edited text, not the previously persisted draft. The server endpoint itself is single-purpose and always locks `history[-1]`.
#### `POST /answer-questions/unfinalize` — clear the finalized snapshot; preserves draft history (JSON).
#### `POST /answer-questions/override` — save per-question one-shot notes; silent autosave on textarea blur (JSON in, `{ok, error}` only — no card_html).
#### `POST /answer-questions/entries` — replace `resume_entries_used` for one question; called on chip add / × click (JSON).
#### `POST /answer-questions/entry-notes` — save the full global entry-notes dict; silent autosave on textarea blur (JSON).

### Functions

#### Storage helpers
- `load_applications() -> list` / `save_applications(apps) -> None` — read/write `application_tracker.json` (mirrored from config to avoid the import-time API-key check).
- `load_pipeline() -> list` — read `job_pipeline.json`. Strips surrogates.
- `load_comp_estimates_by_job() -> dict` — read `comp_estimates.json` and index by `job_id`.
- `load_companies_by_id() -> dict` — read `company_registry.json` and index by `company_id`.
- `load_daily_state(date_iso) -> dict` / `save_daily_state(date_iso, state)` — keyed-by-ISO-date checklist state.

#### `days_since_iso(iso_date: str) -> int`
Local stdlib-only port of `config.days_since` so this file stays importable without an API key.

#### `fetch_jd(url: str) -> tuple[str, bool]`
Best-effort scrape mirroring `ingest.fetch_jd_from_url` but tolerant —
returns `("", False)` on any failure so the caller can show the paste
form. Adds LinkedIn-specific selectors at the top of the selector list.

#### `run_ingest(apply_url, company, title, location, jd_text, posted) -> tuple[bool, str]`
Writes the JD to a temp file, shells out to `ingest.py --paste`, returns
`(success, combined_stdout_stderr)`. Temp file is always unlinked.

#### `job_score(job: dict, co_by_id: dict) -> int`
Convenience: `composite_score(job, co_by_id.get(job['company_id']))`.

#### `apply_ghosted_check() -> None`
Runs the time-based application aging on every record by delegating to
`config.auto_age_application` (the SSOT shared with `update_status.cmd_list`):
`applied → ghosted` after `GHOSTED_DAYS`, then `ghosted → rejected` after
`GHOSTED_REJECTED_DAYS`. Called on `/today` render so the web view stays in
sync with the CLI.

#### Background-crawl helpers
- `_crawl_worker() -> None` — daemon thread; runs `scripts/crawl.py` and streams its output into `crawl_state["output_tail"]` while parsing `Ingested: N` from the last matching line. Updates state to `done`/`error` on exit.
- `start_crawl() -> bool` — kicks off the worker if not already running. Returns `False` if a crawl is in flight.
- `crawl_status_payload() -> dict` — snapshot for the `/today/crawl/status` JSON endpoint: state, elapsed seconds, ingested count, last 8 tail lines, error.

#### LinkedIn-ingest helpers
- `linkedin_env_missing() -> list[str]` — names of unset required env vars; empty list = good.
- `load_staged_emails() -> list[dict]` / `save_staged_emails(rows)` — `data/email_staged.json` reader/writer.
- `remove_staged(staging_id) -> dict | None` — drop one row by ID; returns the removed dict.
- `staged_view_param(params) -> str` — extract + validate the staged-list view (`STAGED_VIEWS` = `default`/`all`/`failing`) from parsed POST params; unknown/missing → `default`. Used to echo the active view back into the post-action redirect so LinkedIn actions don't bounce you to the Passing view.
- `set_linkedin_flash(kind, text)` / `pop_linkedin_flash() -> dict | None` — one-shot flash message for LinkedIn-section responses (`kind` ∈ `"ok"`/`"warn"`/`"info"`).
- `run_linkedin_fetch() -> tuple[bool, int, str]` — subprocess `linkedin_fetch.py`; parses `FETCHED: N` from output.
- `run_linkedin_prefilter() -> tuple[bool, int, int, int, str]` — subprocess `prefilter_staged.py`; parses `PREFILTER: passed=N failed=N deleted=N` from output; returns `(ok, passed, failed, deleted, output)`.
- `discard_failing_staged() -> int` — drop every staged row with `_prefilter_pass=False`; returns count.
- `fetch_jd_for_staged(staging_id) -> tuple[bool, str]` — lazily imports `linkedin_fetch._fetch_jd_text`, attempts a single JD fetch for the staged row, persists if it succeeds, and returns a user-facing message keyed off the failure reason (`auth_wall`, `expired`, `short`, `http_error`, `exception`).

#### Cover-letter flash helpers
- `set_cl_flash(kind, text)` / `pop_cl_flash() -> dict | None` — same shape as the LinkedIn flash slot.

#### Research-now action + flash helpers
- `set_research_flash(kind, text)` / `pop_research_flash() -> dict | None` — slot for the result of `/today/company/research`. Popped at the top of `render_cover_letters_body` and `job_detail_page` so feedback follows the user back to either surface.
- `_flash_notice_html(flash) -> str` — render a popped flash dict as a `.notice` div, or empty string. Generic over the three flash kinds (`ok`/`warn`/`info`).
- `run_company_research(company_id) -> tuple[bool, str]` — pre-checks that the id exists in the registry (skips the subprocess on bogus input), shells out to `research_company.py --company-id`, verifies via `record_updated` that the upsert actually ran (since the script exits 0 even when it just prints an error), then strips the stub flag and writes the registry. Returns `(ok, message)` for the flash.

#### Resume-snippet parsing (private helpers)
- `_to_mm_yyyy(month, year) -> str` — `"Jan 2020"` → `"01/2020"`.
- `_split_date_range(text) -> (frm, to)` — parses one date range from a free-text segment.
- `_section_block(md, heading) -> str` — body of a top-level `## <heading>` section in `resume.md`.
- `_split_title_company(head) -> (title, company)` — splits a heading line on `—` / `–` / ` - `.
- `_coalesce_description(body_lines) -> str` — re-flow soft-wrapped bullets/paragraphs into single lines, drop `---` rules, collapse blank runs.

#### Resume-snippet public API
- `parse_experience(md: str) -> list[dict]` — return one dict per `### …` entry under `## Experience`. Fields: `title`, `company`, `location`, `from`, `to`, `description`.
- `parse_education(md: str) -> list[dict]` — return one dict per bullet under `## Education`. Fields: `degree`, `institution`, `location`, `from`, `to`.
- `parse_resume_snippets() -> dict` — convenience: `{"experience": [...], "education": [...]}` (or `"error"` if `resume.md` is missing or unreadable).

#### Template / page renderers
- `page(title, body, nav_query='', wide=False) -> str` — outer HTML skeleton with the shared `STYLE` block. Injects the top-nav search box on every page; `nav_query` pre-fills it (only `search_page` passes a value). `wide=True` adds the `.wrap.wide` modifier (1100px column instead of 760px) — used by the `/today` page so the 7-button status-update row fits on one line.
- `ingest_form(...) -> str` — the `/` ingest form (and the paste-mode variant).
- `pipeline_card() -> str` — short top-10 pipeline preview on the ingest landing page.
- `pipeline_page() -> str` — the full `/pipeline` table.
- `search_page(query) -> str` — the `/search` results view. Case-insensitive substring match on `company_name` and `title` across non-archived pipeline jobs; sort puts jobs that have an `application_tracker.json` entry first (by `date_applied` desc), then the rest by composite score. Empty query falls back to a browse list (capped at 40). Each row links to `/job/<id>`.
- `metrics_page() -> str` — the `/metrics` analytics page; calls `scripts/metrics.py:build_metrics()` and renders cards: overview, status breakdown, rejection-reason breakdown (labels from `config.REJECTION_REASONS`; `unspecified` for pre-field rejections), avg composite by cohort, component contribution averages, score-distribution histogram, funnel speed.
- `_fmt_num(v, suffix='') -> str` / `_hist_row(band, by_cohort, max_count) -> str` — helpers for the metrics page (number formatting + one stacked histogram row).
- `_sanitize_snippet(value: str) -> str` — escape HTML-unsafe chars inside snippet textareas.
- `_snippet_field(field_id, label, value, multiline=False) -> str` — one field + copy button.
- `render_experience_entry(idx, exp) -> str` / `render_education_entry(idx, edu) -> str` — collapsible snippet rows.
- `render_links_card() -> str` — LinkedIn + GitHub copy snippets at the bottom of `/resume`.
- `render_company_card(company, return_to='') -> str` — researched-company panel surfaced on `/job/<id>` for recruiter-call prep: industry, size, HQ, careers URL, sponsorship score+notes, remote_fit, glassdoor/blind sentiment, recent layoffs, ethics flags, and (via `_render_gov_company_block`) the company-level gov/defense flag + evidence. Reads `COMPONENTS["sponsorship"].native_max` + `COMPONENTS["remote"].native_max` for denominators (per the scoring SSOT). When the company record is still a stub, renders a warning banner with a "Research now" form that POSTs to `/today/company/research` and brings the user back to `return_to`.
- `_render_gov_company_block(company) -> str` / `_render_gov_job_block(job, company) -> str` — gov/defense screen surfacing. The first renders the company-level flag + evidence (empty for `none`); the second computes the per-role result via `config.gov_screen_result` and renders a card with flag, role exposure, result badge, the apply-rank effect (`flag` → −`GOV_SCREEN_FLAG_PENALTY_PCT`% penalty shown as `base → adj`; `fail` → "excluded"), and the interview questions when emitted (empty when there's nothing to surface). `_GOV_FLAG_LABEL` / `_GOV_RESULT_LABEL` are the shared badge label+color SSOT.
- `job_detail_page(job_id) -> str` — full per-job view with score breakdown, JD viewer, comp panel, the company-research card, and the gov/defense screen card (`_render_gov_job_block`), pinned high (right under the header for non-interview status, right under the comp card for interview-stage).
- `resume_page() -> str` — `/resume` view.
- `render_section_body(sid, view='default') -> str` — dispatches to the per-section body renderer. A single `view` query param is shared across sections; only the open section interprets it (status_updates: `active`/`ghosted`; linkedin: `default`/`all`/`failing`).
- `render_linkedin_body(view='default') -> str` — LinkedIn-ingest section body; `view` controls passing-only vs. all-rows filter. Every action form (fetch / pre-filter / bulk-discard / per-row) carries a hidden `view` field so the post-action redirect stays on the active view; the section's inline `<script>` also saves/restores `window.scrollY` via `sessionStorage` so acting on a row keeps the scroll position instead of jumping to the top.
- `render_staged_row(row, view='default') -> str` — one staged-row card; embeds the hidden `view` field so Ingest/Fetch JD/Discard preserve the active view on redirect.
- `render_crawl_body() -> str` — crawl-section body with the live status badge.
- `render_status_updates_body(view='active') -> str` / `render_app_row(app) -> str` — status-updates section + per-app row with status-change buttons. Two sub-tabs: `active` (live applications, excludes ghosted) and `ghosted` (auto-flipped, awaiting the `ghosted_timeout` auto-rejection). `render_app_row` includes the `rejected_interview_failed` button.
- `render_cover_letters_body() -> str` — top-N apply queue, ranked by `apply_rank_score` (full composite minus the gov-screen `flag` penalty), filtered by `company_block_reason` and `gov_screen_block_reason` (gov/defense `fail` roles hidden). Rows still display the pure composite via `job_score`.
- `_fmt_currency(value, currency) -> str` — `"CAD 245,000"` formatting.
- `render_comp_panel(comp_record, job_id) -> str` — comp-estimate accordion inside a cover-letter row.
- `render_cl_row(job, co_by_id=None, comp_record=None, apps=None) -> str` — one cover-letter row in the apply queue. When `apps` is supplied, runs `config.find_duplicate_application`; on a hit it renders an "⚠ already applied" badge and gates Mark Applied behind a confirm dialog that posts `force=1`. Also renders a `gov/defense: flag` badge showing the apply-rank penalty (`rank N/130`) when the gov-screen result is `flag`; `fail` roles are excluded upstream so they don't reach this renderer. The row still displays the pure composite via `job_score`, but the apply queue is ordered by `apply_rank_score`.
- `daily_checklist_page(open_section=None, view='default') -> str` — the `/today` page assembly; `view` is the shared sub-tab param threaded to `render_section_body`.
- `_aq_chip_html(slug, label) -> str` — one resume-entry chip on the answer-questions card.
- `_aq_card_html(job_id, question) -> str` — full question card HTML, used both for initial page render and as the `card_html` payload returned by every `/answer-questions/*` mutating endpoint so the client can swap a single card's `outerHTML` without a full reload.
- `render_answer_questions_page(job_id) -> str` — the `/answer-questions` full-page view: header (back link + job header + composite score), motivation section, behavioral section, global resume-entry-notes panel, and the page-local JS (`_AQ_PAGE_JS`). Lazy-imports `answer_questions` so the module's API-key check doesn't fire on server startup.
- `_AQ_PAGE_JS` — module-level string holding the page-local `<script>` block. Event-delegated (matches `closest('.aq-card')`) so dynamically-replaced cards stay bound without rebinding. Every mutating endpoint returns `{ok, card_html, error}`; on success the client replaces the matching card's `outerHTML` with `card_html` (delete returns `null` and removes the card instead). Tracks dirty edit state by stamping `textarea.dataset.savedValue` on first keystroke (baseline = `defaultValue`) so the **Save edit** button can disable itself when the textarea matches what was last persisted. **Finalize chains `save-edit` first when dirty** so the locked snapshot reflects what the operator sees, not the previously persisted draft. Helpers: `currentCard(qid)` re-queries the DOM after each `replaceCard` (the prior `card` reference is detached and stale); `isDirtyEdit(card)` is the single source of truth for "are there unsaved edits".

#### `Handler(BaseHTTPRequestHandler)`
Single HTTP handler class — one method per HTTP verb plus small helpers.

- `log_message(self, fmt, *args)` — overridden to suppress the default access-log spam.
- `send_html(self, html, status=200)` — set headers + write the body.
- `send_json(self, payload, status=200)` — same for JSON.
- `do_GET(self)` — dispatch on `urlparse(self.path).path`. Routes listed above.
- `redirect_today(self, open_section=None, fragment=None, view=None)` — 303 to `/today?open=…&view=…#…` (the `view` param is omitted when `default`/`None`).
- `redirect_or_today(self, params, open_section=None, fragment=None)` — same-origin `return_to` redirect or fall back to `redirect_today`.
- `do_POST(self)` — dispatch on path; each route consumes the body, runs the action, sets a flash, and redirects. The `/answer-questions/*` prefix is dispatched first and uses the JSON shell below instead of the redirect pattern.
- `_read_json_body(self) -> dict` — parse a JSON-encoded request body. Returns `{}` on missing / invalid JSON (the dispatcher then returns `{"ok": false, "error": "..."}`).
- `_aq_handle(self, fn) -> None` — shared shell for `/answer-questions/*` JSON handlers. Lazy-imports `answer_questions`, reads the body, calls `fn(aq, body)` which returns `(ok, card_html, error)`, and writes the JSON response. Any exception is caught and serialized as `{"ok": false, "error": str(e)}`.

#### `main() -> None`
Argparse: `--port PORT` (default 5000), `--no-browser`. Starts an
`HTTPServer` on a daemon thread, optionally opens `http://localhost:<port>/today`,
and blocks on `Ctrl+C`.

---

## `scripts/dashboard.py`

**Role.** Read-only terminal pipeline summary. No Claude, no API calls — just
loads the three JSON files, applies the SSOT composite score, and prints a
colored ranked table plus a per-component breakdown of the top 3.

Reads denominators directly from `COMPONENTS[k].native_max` and `COMPOSITE_MAX`
— no hardcoded `/25`, `/130`, etc.

### Module-level constants

| Name | Purpose |
|---|---|
| `RESET`, `GREEN`, `YELLOW`, `RED`, `GRAY`, `BOLD`, `BLUE` | ANSI color escapes. |
| `STALE_COLOR` | `{staleness_label: color}` — used to colorize the Stale column. |
| `STATUS_COLOR` | `{pipeline_status: color}` — used to colorize the Status column. |

### Functions

#### `color(text, c) -> str`
Wraps `text` in the given ANSI color + `RESET`.

#### `hyperlink(url: str) -> str`
Emits an OSC 8 clickable hyperlink (Windows Terminal, iTerm2, Hyper, etc.).
Returns `url` (or `"N/A"`) unchanged if no URL is provided.

#### `score_bar(value, max_val=COMPOSITE_MAX, width=12) -> str`
Renders a `█░` progress bar of `width` chars and the numeric value,
color-coded by tier: green ≥70, blue ≥50, yellow ≥30, red below.

#### `main() -> None`
Argparse: `--top N` (default 10), `--all` (include archived),
`--stubs` (annotate stub companies). Pipeline:

1. Load jobs + companies + applications.
2. Score every job via `composite_score`; sort desc.
3. Print a header (counts: active, applied, ghosted, companies, stubs).
4. Print the top-N table.
5. Print a per-component breakdown of the top 3 (reads denominators from
   `COMPONENTS`).
6. Print the applications summary (most-recent first).

---

## `scripts/update_status.py`

**Role.** Application tracker. Logs new applications, transitions status,
and lists current applications. No Claude. Mutates
`data/application_tracker.json` and (when logging a new application)
flips the corresponding job in `job_pipeline.json` to `applied`.

This is the surface that frees a company's throttle slot — once status
moves to a terminal state (or `response_date` gets set), the
`company_block_reason` check in `serve.py:render_cover_letters_body`
stops suppressing the company.

### Functions

#### `append_log(entry: dict) -> None`
Same shape as `ingest.append_log`. Appends a UUID + timestamped entry to
`data/process_log.json`.

#### `derive_country` (imported from `config`)
`update_status` no longer defines its own — it imports the canonical
`config.derive_country` (`"CA"` / `"IE"` / `"US"` / `"OTHER"`) and uses it to
fill in `country` on a new application record.

Time-based aging lives in `config.auto_age_application` (the SSOT shared with
`serve.py:apply_ghosted_check`), not here — `update_status` calls it from
`cmd_list`.

#### `cmd_log(args) -> None`
Implements `update_status.py log --job-id UUID [--method M] [--plain-text] [--notes T] [--force]`.

- Loads the job from `job_pipeline.json`. Exits 1 if not found.
- Refuses to double-log the **same job** (warns and returns).
- Refuses to log a **same company + core title** duplicate
  (`config.find_duplicate_application`) unless `--force` — exits 2 with a
  warning otherwise.
- Builds an application record (including `rejection_reason=None`) with
  `composite_score_at_apply` filled from the SSOT `composite_score`.
- Flips the job's `pipeline_status` to `applied`.
- Logs an `application_logged` event.

#### `cmd_status(args) -> None`
Implements `update_status.py status --app-id UUID --status NAME [--rejection-reason R] [--notes T]`.

- Updates `status` + `status_updated`.
- Sets `response_date` to today on the **first** transition out of
  `applied`/`ghosted` — this is what frees the throttle slot.
- When `--rejection-reason` is given (key from `config.REJECTION_REASONS`),
  stores it on `rejection_reason` and appends the human label to `notes`.
- Appends free-text notes if provided.
- Logs an `application_status_change` event.

#### `cmd_list(args) -> None`
Implements `update_status.py list`. Runs `config.auto_age_application`
against every app; if any changed, persists. Prints a single sorted table
of all applications.

#### `main() -> None`
Argparse with three subcommands (`log`, `status`, `list`). Status choices
are constrained: `applied`, `recruiter_screen`, `interview`, `offer`,
`rejected`, `ghosted`, `withdrawn`. `--rejection-reason` choices come from
`config.REJECTION_REASONS`.

---

## `scripts/metrics.py`

**Role.** Read-only analytics module. Loads pipeline + companies +
applications, builds cohorts (`in_flight` / `dead` / `positive` /
`other`), and aggregates per-component contribution averages, composite
score distribution, and funnel-speed stats. The single public entry
point `build_metrics()` returns a dict consumed by
`serve.py:metrics_page()` for the `/metrics` route.

No Claude calls, no mutations. All denominators (`COMPOSITE_MAX`,
per-component weights) read from the scoring SSOT in `config.py` — no
hardcoded numbers.

### Module-level constants

| Name | Purpose |
|---|---|
| `DEAD_STATUSES` | Presentation-only cohort definition: `{"rejected", "ghosted"}`. Scoped to this module — does not gate the pipeline anywhere. |
| `POSITIVE_STATUSES` | Presentation-only cohort definition: `{"offer"}`. Same scope. |

`IN_FLIGHT_STATUSES` is **not** redefined here — imported from
`config.py` per the company-filter SSOT.

### Functions

#### `_load_data() -> tuple[dict, dict, list]`
Returns `(jobs_by_id, companies_by_id, apps)`. Indexes pipeline and
registry by their respective primary keys for cheap lookup.

#### `_component_scores(job: dict, company: dict | None) -> dict[str, float | None]`
Returns the per-component **weighted contribution** for one job (same
math as `composite_score` — `raw * COMPONENTS[k].multiplier`). For
components whose underlying raw value is missing, the entry is `None`
rather than 0 so averages don't get pulled toward zero artificially.
Mirrors `composite_score`'s US substitution (uses `US_SPONSORSHIP_SCORE` for
the sponsorship component on US roles when US is enabled) so the bars keep
summing to the composite.

#### `_build_cohorts(jobs_by_id, companies_by_id, apps) -> dict[str, list[dict]]`
Walks every application, enriches it with `_composite`, `_components`,
`_days_to_response`, and bins into one of four cohorts. Composite
prefers the snapshotted `composite_score_at_apply` from the application
record; falls back to recomputing via `composite_score(job, company)`
when missing.

#### `_avg_components(records) -> dict[str, float | None]`
Average weighted contribution per component across a list of enriched
apps. Returns `None` for any component where no record has a value.

#### `_avg_composite(records) -> float | None`
Average composite across a cohort, or `None` if empty.

#### `_score_distribution(records, bucket_size=10) -> dict[str, int]`
Histogram: bands of `bucket_size` points from 0 to `COMPOSITE_MAX`,
with every band initialized to 0 so the chart always has a full x-axis.

#### `_funnel_speed(records) -> dict`
Days-to-response stats for records where `response_date` is set.
Returns `{count, min, max, median, mean}` or `{}` if no data.

#### `_status_counts(apps) -> dict[str, int]`
Flat `{status: count}` mapping for the status-breakdown table.

#### `_rejection_reasons(apps) -> dict[str, int]`
`{rejection_reason: count}` over `rejected` apps only. Rejections with no
`rejection_reason` (logged before the field) fall into `unspecified`.
Feeds the `/metrics` rejection-reason breakdown card.

#### `build_metrics() -> dict`
**Public entry point.** Returns a single dict with these keys:

| Key | Type | Meaning |
|---|---|---|
| `total_apps` | int | Total application records. |
| `status_counts` | `{status: count}` | All statuses, including `withdrawn`/`other`. |
| `rejection_reasons` | `{reason: count}` | Rejected apps by `rejection_reason` (`unspecified` for pre-field rows). |
| `cohort_sizes` | `{in_flight, dead, positive, other}` | Per-cohort counts. |
| `avg_composite` | `{in_flight, dead, positive}` | Mean composite per cohort, or `None`. |
| `avg_components` | `{cohort: {component: float\|None}}` | Mean weighted contribution per component per cohort. |
| `component_max` | `{component: int}` | Each component's `weight` from `COMPONENTS`. Display denominator. |
| `composite_max` | int | Mirrors `COMPOSITE_MAX`. |
| `score_distribution` | `{band: {in_flight, dead, positive, total}}` | Histogram data. |
| `funnel_speed` | `{cohort: {count, min, max, median, mean} \| {}}` | Days-to-response stats per cohort. |
| `score_band_size` | int | Histogram bucket width (10). |
| `components_ordered` | `list[str]` | Component display order — keys from `COMPONENTS` in insertion order. |

---

## `scripts/linkedin_fetch.py`

**Role.** Pulls LinkedIn job-alert emails out of the user's inbox via IMAP,
parses each one's HTML, and writes the extracted jobs to
`data/email_staged.json` for the `/today` UI to render. Authentication is
via env vars (`NEXTROLE_IMAP_HOST` / `NEXTROLE_IMAP_USER` /
`NEXTROLE_IMAP_APP_PASSWORD`) so OAuth flows aren't required — for Gmail,
the user generates a 16-char app password and points
`NEXTROLE_IMAP_HOST=imap.gmail.com` at it.

Three dedup safeguards: `staged_ids` / `staged_urls` (per-run, in-memory),
`data/email_state.json` `seen_message_ids` (cross-run, by RFC 822 Message-ID),
and `\Seen` on the server (so a re-fetch ignores the same UID). The
`--reset` flag clears all three so an alert can be re-staged.

JD bodies are **not** fetched automatically — LinkedIn soft-banned the
user when prior runs did bulk JD fetches. The `/today` UI offers a
per-row "Fetch JD" button that calls `_fetch_jd_text` at human cadence
instead.

### Module-level constants

| Name | Purpose |
|---|---|
| `ROOT`, `DATA_DIR`, `EMAIL_CFG`, `EMAIL_STATE`, `STAGED_PATH`, `JD_FETCH_LOG` | Path constants. |
| `DEFAULT_SENDERS` | `["jobalerts-noreply@linkedin.com"]` — written to `email_config.json` if it doesn't exist. |
| `MIN_JD_LENGTH` | 200 — mirrors `serve.py` so a JD fetch that yields less is treated as failure. |
| `JD_FETCH_HEADERS` | Chrome user-agent for outbound JD fetches. |

### Functions

#### Config / state I/O
- `load_allowlist() -> list[str]` — reads `data/email_config.json`'s `senders` list, falls back to `DEFAULT_SENDERS`. Auto-creates the file on first call.
- `load_seen_ids() -> set[str]` — reads `data/email_state.json` `seen_message_ids`.
- `add_seen_ids(new_ids: set[str]) -> None` — union with existing IDs and write back.
- `load_staged() -> list[dict]` / `save_staged(rows: list[dict]) -> None` — `data/email_staged.json` reader/writer.

#### HTML parsing
- `_decode_subject(raw: str | None) -> str` — handles RFC 2047 encoded headers.
- `_extract_html(msg: email.message.Message) -> str` — walks multipart, returns the first `text/html` body decoded with its declared charset.
- `_normalize_linkedin_url(url: str) -> str` — strips `/comm/` from `/comm/jobs/view/` (auth-walls) and drops the entire query string (which often carries `otpToken=…` triggering an email-login redirect). Applied at parse time so both auto-fetch and the user's "open ↗" link land on the no-auth variant.
- `parse_linkedin_alert(html: str) -> list[dict]` — finds every anchor matching `/jobs/view/<id>`, groups by ID, picks the anchor whose parent `<td>` has the richest text, and emits `{linkedin_job_id, title, company, location, apply_url}`. Splits the second line on U+00B7 (middle dot) to separate company from location.

#### JD auto-fetch
- `_log_jd_fetch(record: dict) -> None` — appends one JSON line to `data/jd_fetch_log.jsonl`. Best-effort; never raises.
- `_fetch_jd_text(url: str) -> tuple[str, bool, str]` — best-effort GET + extract. Returns `(text, ok, reason)` with `reason ∈ {ok, auth_wall, expired, http_error, exception, short}`. LinkedIn-specific behavior: only `div.description__text--rich` / `section.show-more-less-html` are trusted as JD containers (generic `main`/`article` selectors return sign-in chrome on LinkedIn); the function detects login-wall redirects (`/uas/login`, `/ssr-login/`) and expired-posting redirects (where `/jobs/view/` disappears from the resolved URL). Called one URL at a time at human cadence — bulk parallel fetch trips LinkedIn's bot detection.

#### IMAP fetch
- `get_creds() -> tuple[str, str, str]` — read all three env vars or exit 2 with a helpful message.
- `fetch_via_imap(dry_run: bool = False) -> int` — main flow. Returns the number of new staged jobs.
- `reset_seen_state() -> tuple[int, int, int]` — clear local dedup state, clear staged-jobs list, remove `\Seen` on the server for every previously-tracked Message-ID. Returns `(n_local_cleared, n_staged_cleared, n_server_unflagged)`. Preserves `\Seen` on LinkedIn messages the user read outside the fetch flow.
- `rehydrate_staged() -> tuple[int, int]` — re-run `_normalize_linkedin_url` over existing staged rows after a parser upgrade. Returns `(n_normalized, n_total)`.
- `fetch_from_sample(path: Path, dry_run: bool = False) -> int` — parse a local `.eml` file as if it had been fetched. For testing without an IMAP server.

#### `main() -> None`
Argparse: `--dry-run`, `--sample EML_PATH`, `--reset`, `--rehydrate`.
Prints machine-readable last lines for `serve.py` to parse:
`FETCHED: N`, `RESET: local=N staged=N server=N`, `REHYDRATE:
normalized=N total=N`, or `ERROR: <message>`.

---

## `scripts/comp_estimate.py`

**Role.** One-shot compensation estimator for a single job. Reads the job
and (optionally) its company record, derives a currency from the location,
loads the resume, calls Opus 4.7 with a structured prompt, validates the
JSON response against a strict schema, and upserts the result into
`data/comp_estimates.json` keyed by `job_id`. Output is consumed by the
`/today` cover-letters surface and the per-job detail page in `serve.py`.

Uses Opus 4.7 (not the pipeline default Sonnet 4.5) because Opus has deeper
salary-band knowledge and matches the user's prior manual workflow on
claude.ai. One job per invocation by design — this is not a batch tool.

### Module-level constants

| Name | Purpose |
|---|---|
| `COMP_MODEL` | Anthropic model ID — Opus 4.7. |
| `MAX_TOKENS` | 1500 — enough for the full JSON response with room for reasoning. |
| `_CAD_HINTS`, `_EUR_HINTS`, `_GBP_HINTS`, `_USD_HINTS` | Substring tuples used by `derive_currency` to map location text to a currency. |
| `_HQ_TO_CURRENCY` | Company-HQ ISO country code → currency fallback when location text doesn't match any hint. |
| `_REQUIRED_TOP`, `_REQUIRED_BASE`, `_VALID_CLASSIFICATIONS`, `_VALID_CONFIDENCE` | Schema constants consumed by `validate`. |

### Functions

#### `derive_currency(location: str, company_hq: str | None = None) -> str`
Deterministic mapping from a free-text location (and optional HQ ISO code)
to one of `CAD` / `EUR` / `GBP` / `USD`. Falls back to USD when nothing
matches.

#### `build_system_prompt(resume_text: str, currency: str) -> str`
Constructs the multi-section Claude system prompt: candidate resume, base
salary methodology (p50 / p85 / p90 with rationale for the p85 ask anchor),
bonus-component classifications (`Expected` / `Possible` / `Unusual` /
`Stated-in-JD`), confidence bands, the asymmetric-risk note, and the
required JSON output schema. Currency is interpolated so the model emits
amounts in the right unit.

#### `build_user_message(job: dict, company: dict | None, currency: str) -> str`
Builds the user message: company name, title, location, currency, optional
company-context bits (industry, size tier, HQ country, Glassdoor rating,
recent-layoffs flag), and the JD text. If `jd_text` is empty, an explicit
note tells the model to estimate from metadata alone and lower its
confidence accordingly.

#### `parse_comp_json(raw: str) -> dict`
Tolerant JSON parser. Handles three cases in order:
1. ` ```json ... ``` ` fence — extract between fences.
2. Generic ` ``` ... ``` ` fence — same.
3. Leading prose before a `{` — slice from the first `{` to the last `}`.

Then `json.loads`. Raises `JSONDecodeError` if all three fall through.

#### `validate(result: dict) -> None`
Strict schema check on the parsed response. Verifies all top-level keys are
present, `base` has `min` / `max` / `target`, each of `year_end_bonus` /
`signon` / `relocation` / `equity` is an object with valid `classification`
and `reason`, and `confidence` is `HIGH` / `MED` / `LOW`. Raises
`ValueError` with a pointed message on the first violation.

#### `call_claude(system: str, user_message: str) -> tuple[str, int, int]`
Single Anthropic API call. Returns `(response_text, input_tokens,
output_tokens)`. No retry — failures propagate to `main` for clean exit.

#### `load_estimates() -> list[dict]`
Reads `data/comp_estimates.json` and returns the list of records (empty
list if file missing or empty).

#### `upsert_estimate(record: dict) -> None`
Replaces any existing record with the same `job_id`, then appends the new
one. Persists with `save_json`.

#### `append_log(event: dict) -> None`
Appends a timestamped event to `data/process_log.json`. Used by `main` to
record `comp_estimate_generated` events.

#### `main() -> int`
CLI driver and only entrypoint. Exit codes:

- `0` — success (or dry-run completed).
- `2` — job_id not found, or resume missing.
- `3` — Anthropic API call failed.
- `4` — response wasn't valid JSON.
- `5` — schema validation failed.

CLI flags:

- `--job-id UUID` (required) — the job to estimate.
- `--currency CODE` (optional) — override the deterministic currency mapping.
- `--dry-run` — print the result to stdout and skip persistence + log.

Pipeline: load job → load company → derive currency → load resume → build
prompts → call Claude → parse → validate → upsert + log → print summary
(base range, target ask, confidence). On `--dry-run`, the validated result
is printed and nothing is written.

---

## `scripts/answer_questions.py`

**Role.** Ad-hoc application question answerer. Operator pastes a question
("Why this company?", "Tell us about a time you made a high-impact
architectural decision…"); the module assembles a prompt from
`profile/answer_questions_rules.md` + the resume + `RESUME_ENTRY_SLUGS` +
non-empty global entry notes + any per-question override, calls Sonnet 4.6,
sanitizes the answer, and appends it to `draft_history` for that question.

Web-UI only — no CLI entrypoint, no `main()`. Driven by `serve.py`'s
`/answer-questions/*` JSON routes.

Two question classes (`motivation`, `behavioral`) get separate strategy
sections in the rules file but share the same accuracy/tone constraints.
`char_cap` is honored as a hard limit at prompt time; over-cap drafts still
get persisted so the operator can see what happened and regenerate.

`draft_history` is append-only — regeneration **never** overwrites a prior
version. `finalized_answer` is only set by an explicit `finalize_answer`
call and is preserved across regenerations until the operator unfinalizes.

### Module-level constants

| Name | Purpose |
|---|---|
| `MAX_TOKENS` | 1500 — enough for the JSON payload + a long answer with room for reasoning. |
| `QUESTION_CLASSES` | `("motivation", "behavioral")` — validated by `add_question`. |
| `QUESTION_STATUSES` | `("draft", "finalized")` — only used for documentation; status is set by the lifecycle helpers. |

### Functions

#### `load_questions() -> dict`
Reads `data/application_questions.json` and returns the top-level dict
keyed by `job_id`. Returns `{}` if file missing. Coerces a stray empty
list (from a brand-new `load_json` return) to `{}`.

#### `save_questions(data: dict) -> None`
Persists via `save_json`.

#### `load_entry_notes() -> dict`
Reads `data/resume_entry_notes.json` and returns the slug→note dict. On
first access, seeds the file with every slug from `RESUME_ENTRY_SLUGS`
mapped to `""`. On subsequent loads, backfills any newly-added slugs so
the UI never hits a KeyError.

#### `save_entry_notes(notes: dict) -> None`
Persists. Drops unknown slugs on write — only the canonical
`RESUME_ENTRY_SLUGS` set survives.

#### `get_job_questions(job_id: str) -> dict`
Returns `{"motivation": [...], "behavioral": [...]}` for the job. Both
class keys are always present even if the job has no questions yet.

#### `_find_question(data: dict, job_id: str, question_id: str) -> tuple[str | None, dict | None]`
Internal lookup. Returns `(class_key, record)` or `(None, None)`.

#### `add_question(job_id: str, question_text: str, question_class: str, char_cap: int | None) -> dict`
Creates a new question with a fresh UUID, appends it to the job + class
list, persists, returns the new record. Raises `ValueError` on unknown
`question_class` or empty `question_text`.

#### `delete_question(job_id: str, question_id: str) -> bool`
Removes a draft question. Refuses (`ValueError`) to delete a question
with `status == "finalized"` — operator must unfinalize first. Returns
`True` if found and removed, `False` if not found.

#### `update_question_override(job_id: str, question_id: str, override_notes: str) -> dict`
Writes `question_override_notes`. Per-question, one-shot, survives
regeneration but never propagates to other questions.

#### `update_resume_entries(job_id: str, question_id: str, slugs: list[str]) -> dict`
Replaces `resume_entries_used` with the operator's chip selection.
Filters to slugs that exist in `RESUME_ENTRY_SLUGS` (unknown ones are
dropped) and deduplicates while preserving order.

#### `_format_slug_registry() -> str`
Builds the `RESUME_ENTRY_SLUGS:` block of the system prompt — one line
per slug with its display label. Tells the model which slug strings are
valid for `resume_entries_used`.

#### `_format_entry_notes(notes: dict) -> str`
Builds the global-notes block. Skips slugs with empty notes; if all are
empty, returns `""` so the prompt stays clean.

#### `build_prompt(job: dict, company: dict | None, question_record: dict, entry_notes: dict) -> tuple[str, str]`
Returns `(system_prompt, user_message)`. System prompt assembles the rules
file, the resume, the slug registry, non-empty entry notes, and a final
"This question is class X" pointer. User message carries job + JD + the
question itself + the char-cap line + override notes + previously-used
slugs (regeneration hint).

#### `_parse_answer_json(raw: str) -> dict`
Tolerant JSON parser. Mirrors `comp_estimate.parse_comp_json` exactly:
strips ` ```json ... ``` ` fences, generic ` ``` ... ``` ` fences, then
leading prose before `{`.

#### `_call_claude(system: str, user_message: str) -> tuple[str, int, int]`
Single Anthropic call against `CL_MODEL` (Sonnet 4.6). Returns
`(response_text, input_tokens, output_tokens)`.

#### `_append_log(event: dict) -> None`
Appends to `data/process_log.json`. Event types written:
`application_question_generated`, `application_question_finalized`.

#### `generate_answer(job_id: str, question_id: str) -> dict`
End-to-end generation. Loads the question, job, company, and entry notes;
builds the prompt; calls Claude; parses; sanitizes via
`config.sanitize_answer_text`; appends a new `draft_history` version with
the computed `char_count`; writes the model's `resume_entries_used` back
onto the record (filtered to known slugs). Never touches
`finalized_answer`. Returns the updated record.

Raises `ValueError` for: question not found, job not found, non-JSON
response, missing/blank `answer`, non-list `resume_entries_used`.

#### `save_edit(job_id: str, question_id: str, answer_text: str) -> dict`
Persist a manually-edited answer as a new `draft_history` entry with
`source = "manual_edit"`. The submitted text is run through
`config.sanitize_answer_text` first so the dash policy and other
invariants apply uniformly to operator edits and Claude output. Never
mutates a prior version — every save appends. Raises `ValueError` if the
sanitized text is empty.

#### `finalize_answer(job_id: str, question_id: str) -> dict`
Snapshots the latest draft into `finalized_answer` + `finalized_at` and
sets `status = "finalized"`. Raises `ValueError` if there are no drafts.

#### `unfinalize_answer(job_id: str, question_id: str) -> dict`
Clears `finalized_answer` / `finalized_at` and sets `status = "draft"`.
Draft history is preserved.

---

## `scripts/generate_cl.js`

**Role.** Node-side `.docx` cover-letter generator. Reads the job from
`data/job_pipeline.json`, loads `profile/resume.md` and
`profile/cover_letter_rules.md`, calls Claude (Sonnet 4.6) to draft the
letter as a strict-shape JSON, enforces a 380-word cap (auto-retry with a
trim prompt), parses the visa paragraph for the right country out of
`cover_letter_rules.md`, assembles a styled `.docx` via the `docx` npm
library, and writes it to `output/<date>_<company>_<title>.docx`.

Written in Node specifically because the `docx` library has better fidelity
for the styling spec (Calibri, navy headers, exact margins) than the Python
alternatives the user evaluated.

### Module-level constants

| Name | Purpose |
|---|---|
| `ROOT`, `DATA_DIR`, `OUTPUT_DIR`, `PROFILE_DIR`, `PIPELINE_PATH`, `REGISTRY_PATH`, `LOG_PATH`, `RESUME_PATH`, `CL_RULES_PATH` | Path constants. |
| `API_KEY` | Read from `process.env.ANTHROPIC_API_KEY`; exits 1 if unset. |
| `CL_MODEL` | `"claude-sonnet-4-6"`. |
| `MAX_TOKENS` | 4000. |
| `COUNTRY_NAME_TO_CODE` | `{"canada": "CA", "ireland": "IE", "united kingdom": "UK", "uk": "UK"}` — used by `parseVisaParagraphs` to match `### <country>` headings. **US is intentionally absent** (US citizen → no work-auth paragraph), so US jobs produce no visa section. |
| `WORD_CAP` | 380 — hard cap; over-cap drafts get one trim retry. |

### Functions

#### Helpers
- `loadJson(p) / saveJson(p, data)` — read/write JSON.
- `todayISO()` — local-time `YYYY-MM-DD`. Uses local components rather than `toISOString()` so the filename date matches `todayLong()` around midnight.
- `todayLong()` — `"May 18, 2026"` for the letter body.
- `uuidv4()` — RFC 4122 v4 UUID for log entries.
- `appendLog(entry)` — adds a UUID + timestamps + writes to `data/process_log.json`.
- `slugify(str)` — non-alphanum → `_`, trim underscores. Used for filenames.

#### `callClaude(system, userMessage)` — async
Single `fetch` against `https://api.anthropic.com/v1/messages` with the API
key in `x-api-key`. Logs token counts to stdout. Throws on non-200.

#### `parseVisaParagraphs(rulesText)`
Locates the `## Locked Visa / Work Authorization Paragraphs` section in
`profile/cover_letter_rules.md` and extracts per-country paragraphs.
Each `### <Country>` subsection becomes one entry; the country heading
is mapped to an ISO code via `COUNTRY_NAME_TO_CODE` (`Canada` → `CA`,
`Ireland` → `IE`, `United Kingdom` / `UK` → `UK`). Returns `{CA: "…", IE: "…"}`
(etc.). There is no US entry — US roles intentionally get no visa paragraph.
Before parsing, HTML comments (`<!-- … -->`) and standalone Markdown horizontal
rules (`---`) are stripped from the section: the last subsection has no
following `### ` to bound its capture, so without this it would swallow any
trailing comment/rule that separates the section from the next `## ` — which
previously leaked into Ireland letters (Ireland being the last subsection).

#### `buildSystem(resumeText, rulesText)`
Constructs the Claude system prompt: resume, the full rules document, the
required JSON output schema (`re_line`, `opening`, `body_paragraphs`,
`closing`), and the explicit "do not produce a visa paragraph" instruction
— the visa paragraph is appended server-side after the signature.

#### `buildDocx(content, outputPath, visaText = null)` — async
Assembles the `.docx` via the `docx` library:

- Header: centered "Johnny Ray Blanton III" in 14pt navy bold.
- Contact line centered in 9.5pt.
- Date (computed locally — never trust Claude's date), salutation, bold `Re:` line.
- Body paragraphs in 10.5pt Calibri, soft spacing.
- "Sincerely," + bold name.
- Visa paragraph (if any) prefixed with `"Note: "` after the signature.
- US-Letter page size with explicit margins.

Writes the file via `Packer.toBuffer` + `fs.writeFileSync`.

#### `main()` — async
- Parses `--job-id UUID` and optional `--country CA|IE`.
- Loads the job; exits 1 if not found.
- Derives the country from `--country` (passed by `run.py`/`serve.py` from the Python SSOT) or, when absent, by shelling out to `python scripts/geography.py "<location>"` — the same `config.derive_country` logic, so no parallel JS derivation. Maps `CA`/`IE` → the matching visa paragraph; `US`/`OTHER` → none.
- Loads + reads resume.md, cover_letter_rules.md; exits 1 if either is missing.
- Resolves the country-specific visa paragraph (replaces `[Company Name]` placeholders); logs whether one was applied.
- Calls Claude with the build prompts.
- Parses the JSON response; on parse failure, exits 1 with the raw response.
- Counts words; if over 380, sends a strict trim prompt and replaces `content` with the trimmed JSON. (Even if the trim still overshoots, the trimmed version is used.)
- Filename: `<YYYY-MM-DD>_<Company>_<Title>.docx` with `_v2`, `_v3`, ... appended on same-day collisions.
- Calls `buildDocx`, writes the file.
- Updates the job record (`cover_letter_generated=true`, version bump, `cover_letter_path`, `pipeline_status="cover_letter_ready"`).
- Logs a `cover_letter_generated` event.
- Prints a post-generation checklist (word count, paragraph count, visa-applied note, manual verification reminders).

---

## `scripts/rescore_all.py`

**Role.** Bulk re-score utility for use after editing
`profile/scoring_rubric.md` or `profile/stack_keywords.yaml`. Recomputes
the mechanical stack score for every selected job (free) and re-fetches
seniority + domain from Claude (paid, ~$0.013/job). Backs up
`job_pipeline.json` to a `.bak` before the first write and checkpoints
every 10 jobs so a `Ctrl+C` mid-run doesn't lose completed work.

### Module-level constants

| Name | Purpose |
|---|---|
| `CHECKPOINT_EVERY` | 10 — flush partial progress to disk every N jobs. |
| `EST_COST_PER_CALL` | Cost estimate (~$0.013) for Sonnet 4.5 — `3500 input × $3/M + 150 output × $15/M`. Used to print the projected bill in `--dry-run`. |

### Functions

#### `select_jobs(jobs: list, include_applied: bool) -> list`
Returns jobs whose `pipeline_status` is in `{active, cover_letter_ready}`
by default, plus `{applied, archived}` when `include_applied=True`.

#### `main() -> int`
Argparse: `--dry-run`, `--limit N`, `--include-applied`, `--stack-only`,
`--job-ids ID[,ID...]`, `--job-ids-file PATH`.

Pipeline:

1. Load pipeline; exit 0 if empty.
2. Resolve target set: explicit `--job-ids` / `--job-ids-file` override
   the status filter; otherwise `select_jobs(jobs, include_applied)`.
3. Print cost estimate (skipped on `--stack-only`).
4. On `--dry-run`, exit 0.
5. Back up to `<pipeline>.bak`.
6. Loop selected jobs:
   - Skip rows without `jd_text`.
   - Recompute `stack_match_score`.
   - Unless `--stack-only`, call `score_jd(jd_text, title=…)` (passes
     title so the mechanical cap applies on the way out). On API failure,
     **roll back** the stack change to keep the row internally consistent
     until retry.
   - Print per-job before/after diff.
   - Checkpoint to disk every `CHECKPOINT_EVERY` jobs with elapsed/ETA.
7. Final save + summary (biggest drops, biggest gains, failures).

Exits 0 on full success, 1 if any LLM call failed, 130 on Ctrl+C, 2 on
unexpected exception. The `.bak` is always retained.

---

## `scripts/scan_no_sponsorship.py`

**Role.** Retroactive sweep for jobs whose JD explicitly refuses visa
sponsorship. New ingests are caught automatically by `ingest_job`'s
`detect_no_sponsorship` call — this script exists only for one-off passes
over already-ingested rows (e.g. when the regex set expands). Default is
dry-run; pass `--apply` to archive matches.

### Functions

#### `main() -> int`
Argparse: `--apply`, `--include-applied`.

Pipeline:

1. Load pipeline; exit 0 if empty.
2. Filter to `{active, cover_letter_ready}` (plus `applied` if requested).
3. For each, skip US-derived rows (`config.derive_country(location) == "US"`
   — the operator is a US citizen, mirroring the ingest-time skip), then call
   `detect_no_sponsorship(job.jd_text)`; collect matches with their snippets.
4. Print the matched list. On `--apply=False`, return 0.
5. Back up `job_pipeline.json` to `.bak`.
6. For each match: flip `pipeline_status="archived"`, set `archived_at` +
   `archived_reason="JD says no sponsorship"`, append a `job_archived`
   event to `data/process_log.json`.

---

## `scripts/scan_foreign_locations.py`

**Role.** Retroactive sweep for jobs pinned to a foreign (non-target) region —
"Remote - India", "European Union (Remote)", "Berlin, Germany", etc. New
ingests are already blocked at the gate (`config.location_passes` in
`ingest.ingest_job`), so this exists for two cases: re-sweeping after the
operator **expands** `config._FOREIGN_LOCATION_TOKENS`, and the odd
manually-pasted row. Default is dry-run; `--apply` archives. It also runs
automatically (apply mode) at the end of every real crawl via
`crawl.crawl` → `archive_foreign_pinned`.

### Module-level constants

| Name | Purpose |
|---|---|
| `ARCHIVE_REASON` | `"foreign-pinned remote (not an eligible geography)"` — written to `archived_reason` + the log detail. |
| `_DEFAULT_STATUSES` | `{active, cover_letter_ready}` — the statuses swept unless `--include-applied`. |

### Functions

#### `is_foreign_pinned(location: str) -> bool`
SSOT predicate: `derive_country(location) == "OTHER"` AND
`names_foreign_location(location)` — exactly the rows `location_passes` rejects
on its OTHER branch.

#### `find_foreign(jobs, statuses) -> list[dict]`
The in-scope jobs (status in `statuses`) whose location is foreign-pinned.

#### `archive_foreign_pinned(apply=True, include_applied=False, verbose=False) -> int`
Archives active foreign-pinned jobs in place; returns the count archived (or
that *would* be, when `apply=False`). Writes a `.bak` backup + `job_archived`
log entries **only when there's something to archive**, so a no-op call (the
common case) touches nothing. Shared by the CLI and the crawl's end-of-run
auto-sweep.

#### `main() -> int`
Argparse `--apply` / `--include-applied`. Previews via
`archive_foreign_pinned(apply=False, verbose=True)`, then archives on `--apply`.

---

## `scripts/cleanup_staged_jd.py`

**Role.** One-off cleanup script for `data/email_staged.json` rows whose
`jd_text` contains LinkedIn's similar-jobs / expired-job landing page text
rather than a real JD body. The current `linkedin_fetch._fetch_jd_text`
classifies these correctly (returns `reason="expired"`); this script
cleans up rows fetched **before** that detection landed.

After running, the affected rows have empty `jd_text`, the "Fetch JD"
button re-appears in the `/today` UI, and the next per-row fetch
classifies them correctly.

### Module-level constants

| Name | Purpose |
|---|---|
| `STAGED_PATH` | `data/email_staged.json`. |
| `CORRUPTION_MARKERS` | Tuple of phrases (`"Sign in to set job alerts for"`, `"Get notified when a new job is posted"`, `"You've viewed all jobs for this search"`) that appear only on LinkedIn's landing pages. Match-any. |

### Functions

#### `looks_corrupted(jd_text: str) -> bool`
Returns `True` if any `CORRUPTION_MARKERS` phrase is in `jd_text`.

#### `main() -> None`
Argparse: `--dry-run`. Loads staged rows, filters by `looks_corrupted`,
prints the first 5 matches, and (unless `--dry-run`) clears `jd_text`
on every match and persists.

---

## `scripts/backfill_target_boards.py`

**Role.** One-time pass over `data/job_pipeline.json` to discover ATS
boards from already-ingested apply URLs and append them to
`data/target_boards.json`. Was needed because the ATS auto-detection in
`ingest.py` and `crawl.py` was added after the pipeline already had
hundreds of jobs. Safe to re-run — existing `(ats, slug)` pairs are
deduped.

### Functions

#### `main() -> None`
Argparse: `--dry-run`. Pipeline:

1. Load pipeline + target_boards.
2. Build a `{(ats, slug)}` set of already-known boards.
3. Iterate jobs, run `crawl.detect_ats(apply_url)` on each, group new
   discoveries by `(ats, slug)`, keep up to 2 example URLs per group for
   debugging.
4. Print discoveries grouped by ATS.
5. On `--dry-run`, exit. Otherwise append new entries with
   `added=today()`, `added_via="backfill_pipeline"` and save.

---

## `scripts/discover_boards_from_careers.py`

**Role.** Pulls ATS boards out of company careers pages. For each company
in `company_registry.json`, fetches the `job_portal_url` (or skips to an
API probe if the URL is missing) and applies a five-strategy detector to
find the underlying Greenhouse / Lever / Ashby slug. Strategies 4 and 5
do a live API hit before recording so we never store an unverified
`(ats, slug)` pair.

Writes diagnostic records to `data/board_discovery_log.jsonl` so
"why didn't X get discovered" is auditable after the fact without
re-running the scrape.

### Module-level constants

| Name | Purpose |
|---|---|
| `BOARD_DISCOVERY_LOG` | `data/board_discovery_log.jsonl`. |
| `HEADERS` | Chrome user-agent. |
| `REQUEST_DELAY_S` | 0.8 — pacing between careers-page fetches (different host each time). |
| `API_DELAY_S` | 0.3 — pacing between ATS API validation calls (three shared hosts). |
| `PROXY_PARAMS` | `{"gh_jid": "greenhouse", "ashby_jid": "ashby", "lever_jid": "lever"}` — query params that betray which ATS a careers proxy fronts. |
| `PROXY_PARAM_RE` | Compiled regex hitting any `PROXY_PARAMS` key. |

### Functions

#### `slug_from_name(name: str) -> str`
Best-guess slug: lowercase + alphanumeric only. Catches the common case
where the slug matches the company name (`stripe`, `databricks`, `lyft`)
but misses legal-name / rebrand cases (e.g. DoorDash's `doordashusa`).

#### `validate_ats_slug(ats: str, slug: str) -> bool`
Hits the appropriate ATS public API and returns `True` only if the
response has at least one job. Skips known-unsupported ATSes.

#### `_log(record: dict) -> None`
Appends a JSON line to `BOARD_DISCOVERY_LOG`. Best-effort; never raises.

#### `api_probe_only(company_name: str) -> tuple[tuple[str, str] | None, dict]`
Used when the company has no `job_portal_url`. Probes all three
supported ATSes with `slug_from_name(company)` and returns the first
that validates, plus a diagnostic dict.

#### `detect_in_careers_page(url: str, company_name: str) -> tuple[tuple[str, str] | None, dict]`
Five-strategy detector applied to the careers page in order, cheapest first:

1. **Redirect** — `detect_ats(resp.url)` (the resolved URL after redirects).
2. **Tag attributes** — `<a href>`, `<iframe src>`, `<script src>` each checked through `detect_ats`.
3. **Raw HTML** — run `detect_ats` against the response body text.
4. **Proxy param + validated slug** — if the body contains `gh_jid=` / `ashby_jid=` / `lever_jid=`, take that as a hint, guess the slug from the company name, and validate via `validate_ats_slug` before recording.
5. **API probe** — blindly try all three ATSes with the guessed slug.

Returns `(ats_info_or_None, diagnostic_dict)`. The diagnostic carries a
`reason` field (`match_redirect`, `match_a_href`, `match_iframe_src`,
`match_script_src`, `match_raw_html`, `match_proxy_validated`,
`match_api_probe`, `no_ats_found`, `exception`, `http_error`) which is
written to the log.

#### `main() -> None`
Argparse: `--dry-run`, `--limit N`, `--company NAME`, `--verbose`.

Pipeline:

1. Load registry + existing target_boards.
2. Cheap early-skip: if a company already has any board in target_boards
   (matched on lowercased name), skip it.
3. For each remaining company: full careers-page scrape if
   `job_portal_url` is set, otherwise `api_probe_only`.
4. Log every diagnostic to `BOARD_DISCOVERY_LOG`.
5. Print summary (scanned, discovered, already known, no-match, fetch-failed).
6. On `--dry-run`, exit. Otherwise append discoveries with
   `added=today()`, `added_via="careers_page_scrape"` and save.
