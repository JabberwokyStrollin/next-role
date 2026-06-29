# next-role — Conventions for Claude sessions

This file holds project-specific rules that override default behavior. Read
it before you change scoring, ranking, pre-filter, or anything documented
in `ARCHITECTURE.md` / `DATA.md`.

## Documentation parity

Four docs in this repo describe the codebase; their accuracy depends on
staying in sync with the code.

- `README.md` — overview, daily workflow, project structure, cost reference
- `SETUP.md` — install steps, env vars, `profile/` contents, crawl config
- `ARCHITECTURE.md` — per-script reference (every function in every script)
- `DATA.md` — schema reference (every field of every file under `data/`)

**Code changes ship with doc changes in the same commit.** When you change
something below, update the matching doc(s) before declaring the task done.

| Code change | Doc(s) to update |
|---|---|
| Add / remove / rename a function, method, class, or module-level constant | `ARCHITECTURE.md` |
| Add a new script under `scripts/` (or rename one) | `ARCHITECTURE.md` (new section) + `README.md` "Project structure" tree |
| Add / remove / rename a field on a record in `data/*.json` | `DATA.md` |
| Add / change a JSON `event_type`, pipeline `status`, or other enum value | `DATA.md` |
| Add a new file under `data/` | `DATA.md` (new section); also `README.md` if operator-relevant |
| Change a CLI flag, web-UI route, or daily-workflow step | `README.md` |
| Change prerequisites, env vars, or `profile/` structure | `SETUP.md` |
| Change a scoring weight, `native_max`, or SSOT rule | This file (`CLAUDE.md`) + `ARCHITECTURE.md` (`config.py` section) |
| Change the Claude model used by any script | `README.md` (cost reference) + `ARCHITECTURE.md` (script section) |
| Remove or deprecate a feature | Every doc that mentions it — see verification below |

### Verification before declaring done

After the code + doc edits, grep the docs for any symbol you renamed or
removed. From the repo root:

```bash
grep -rn "<old_symbol_or_field_name>" README.md SETUP.md ARCHITECTURE.md DATA.md CLAUDE.md
```

If anything still references the old name, update or delete it.

### Test of fitness

If a developer only read the docs (never the code), would they get an
accurate picture? If no, the doc change isn't done.

If you can't decide whether a change warrants a doc update, the answer
is yes — bias toward updating.

## Scoring SSOT — single source of truth

The composite ranking system has exactly two scoring functions and three
canonical sources of truth for their inputs. **Never duplicate the values
in another file.** If you need a denominator, weight, or rule, import it
from one of these.

### Two scoring profiles, one canonical location

| Profile | Canonical function | Ceiling | When to use |
|---|---|---|---|
| **Full composite** | `composite_score(job, company)` | `COMPOSITE_MAX` (130) | Apply-time ranking; cover-letter selection; anywhere the operator is about to act on a researched job. Requires `company` argument — needs sponsorship + remote. |
| **Pre-research composite** | `composite_score_pre_research(job)` | `PRE_RESEARCH_MAX` (100) | Ranking stub companies for the research queue. Zeros out sponsorship + remote so stub defaults can't bias the order. **Never** use for apply-time decisions — sponsorship is too important to ignore there. |

| Concern | Canonical location | How to read it |
|---|---|---|
| Component weights (both profiles) + display denominators | `scripts/config.py:COMPONENTS` + `COMPOSITE_MAX` + `PRE_RESEARCH_MAX` | `from config import COMPONENTS, COMPOSITE_MAX, PRE_RESEARCH_MAX` |
| Title → seniority cap | `scripts/config.py:_SENIORITY_BUCKETS` (via `title_seniority_cap()` / `apply_title_cap()`) | `from config import apply_title_cap` |
| Stack keyword scores + stack max + pre-filter title/location lists | `profile/stack_keywords.yaml` | loaded once by `_load_stack_keywords()` and `load_crawl_config()` |
| Claude's native output ranges (seniority 0-25, domain 0-20) | `profile/scoring_rubric.md` | mirrored in `COMPONENTS[k].native_max`; update both together |
| Research-queue minimum score | `scripts/config.py:RESEARCH_QUEUE_MIN_SCORE` | `from config import RESEARCH_QUEUE_MIN_SCORE` |
| Active target geographies (the US toggle) | `scripts/config.py:TARGET_COUNTRIES` | `from config import TARGET_COUNTRIES` |
| US-role sponsorship floor | `scripts/config.py:US_SPONSORSHIP_SCORE` | `from config import US_SPONSORSHIP_SCORE` |
| Free-text location → country code | `scripts/config.py:derive_country()` | `from config import derive_country` |

### Rules

1. **Never define a parallel scoring function** anywhere outside
   `scripts/config.py`. There is exactly one `composite_score` and
   exactly one `composite_score_pre_research`. A duplicate copy silently
   falls behind config.py whenever a weight changes.

2. **Never inline a partial composite** for sort order or display, even
   "just to skip the company lookup." If sponsorship + remote are at
   stub defaults, that's what `composite_score_pre_research` is for —
   use it instead of hand-rolling a subset of the full composite. If you
   need to sort the apply queue, do:
   ```python
   co_by_id = load_companies_by_id()
   sorted_jobs = sorted(jobs, key=lambda j: composite_score(j, co_by_id.get(j.get("company_id"))), reverse=True)
   ```
   To sort the research queue:
   ```python
   sorted_jobs = sorted(jobs, key=composite_score_pre_research, reverse=True)
   ```

3. **Never hardcode a score denominator** in display code. No `/35`,
   `/130`, `/100`, `f"Stack X/25"`, etc. Always read from
   `COMPONENTS[k].native_max` (when showing the stored value),
   `COMPONENTS[k].weight` (contribution to full composite),
   `COMPONENTS[k].pre_research_weight` (contribution to pre-research
   composite), `COMPOSITE_MAX`, or `PRE_RESEARCH_MAX`.

4. **Cover-letter generation only triggers from the post-research full
   composite ranked queue.** Stub companies should be researched first via
   `--research-queue` before they enter the apply path. The `/today`
   apply queue and `run.py:generate_cover_letters` both rank by
   `composite_score(job, company)`; never swap that for the pre-research
   variant in apply-time surfaces.

5. **Pre-filter is intentionally separate.** `scripts/crawl.py:pre_filter`
   and `scripts/prefilter_staged.py:pre_filter_relaxed` run BEFORE any
   Claude calls — they MUST NOT call `composite_score`,
   `composite_score_pre_research`, or any function that requires Claude/
   research output. Doing so would multiply API costs by ~1000x (raw
   aggregator output vs the few that pass filtering). The pre-filter has
   its own SSOT in `profile/stack_keywords.yaml`.

6. **Extending the composite:** add a new key to `COMPONENTS` with both
   a `weight` (full) and a `pre_research_weight` (set to 0 if the signal
   isn't available before research) AND a new line in *both* scoring
   functions' `raw` dict. Don't add code in other files that reaches
   around the SSOT.

7. **Tuning a weight:** edit `COMPONENTS` only — both profiles live
   there. Display denominators auto-update because surfaces read from
   `COMPONENTS[k]` / `COMPOSITE_MAX` / `PRE_RESEARCH_MAX`.

8. **Tuning Claude's output range** (e.g. changing seniority from /25
   to /30): edit `profile/scoring_rubric.md` AND `COMPONENTS["seniority"].native_max`
   in lockstep. The rubric tells Claude the range; `native_max` tells the
   composite math how to multiply it (for both profiles).

### Geography / US target toggle

The operator needs visa sponsorship for **CA / IE** but is a **US citizen**, so
US roles need none — they're a reluctant **remote-only stop-gap**. The whole
feature hangs off `TARGET_COUNTRIES` (currently `frozenset({"CA","IE","US"})`;
remove `"US"` to disable — when absent the US branches never fire and CA/IE
behavior is byte-identical). Country is **derived on the fly** from `location`
via `config.derive_country` (→ `CA`/`IE`/`US`/`OTHER`) —
no stored field. `derive_country` matches IE/CA before US so a combined
"Remote, Canada/US" posting resolves to the sponsorship-bearing country, and
never uses a bare `"us"` substring (would match "houston"). Region codes (CA
provinces `ON`/`BC`/…, US states) are matched only in an anchored "City, XX" form
(`_has_region_code`, US states omitting the `in`/`de`/`co` country-code
collisions); Canada is detected first by name / Canadian city / **province
code** ("London, ON" → CA), so the bare `"CA"` code resolves to **California
(US)**, not Canada.

1. **The US sponsorship floor lives ONLY in `composite_score`.** For a
   US-derived role (and only when `"US" in TARGET_COUNTRIES`), the canonical
   `composite_score` substitutes `US_SPONSORSHIP_SCORE` (native 0-15, default 3)
   for the company `sponsorship_score`. This is a **thumb on the scale**, not a
   hard tier: CA/IE roles with normal sponsorship outrank comparable US roles,
   but a strong-stack US role can still beat a weak CA/IE one. It is **not** a
   new component and **not** a parallel composite — it's a country-conditional
   on the existing `sponsorship` input, inside the one canonical function. Tune
   via `US_SPONSORSHIP_SCORE` only (set to 0 for "zero added from sponsorship").
   `composite_score_pre_research` is untouched (already zero-weights
   sponsorship), so research-queue ranking stays country-agnostic. When US is
   off, the branch never fires and CA/IE composites are byte-identical to before.

2. **`config.location_passes` removes rows the operator can't take** — a
   pure-string, pre-filter-safe (no-Claude) **subtractive** gate layered AFTER
   the YAML `location_allow` allowlist. Three cases: **US** rows are removed
   unless US is enabled AND the role is remote per `config.is_remote_role` (the
   SSOT remote check, **source-aware**: a region-only US location like "USA"
   counts as remote from a remote-only board in `REMOTE_ONLY_SOURCES`, but an
   ATS-board US role needs an explicit remote marker). **CA/IE** always pass.
   **OTHER** rows are removed if `names_foreign_location` matches — a remote role
   pinned to a non-target region ("Remote - India", "European Union (Remote)")
   wants a candidate based there, so it's dropped; "Worldwide"/"Americas"/bare
   "Remote" pass (`_FLEXIBLE_LOCATION_TOKENS` win before `_FOREIGN_LOCATION_TOKENS`,
   which is operator-editable). Expanding `_FOREIGN_LOCATION_TOKENS` needs a
   retroactive sweep of already-ingested rows: `scan_foreign_locations.py`
   (dry-run default, `--apply`), which also runs automatically at the end of
   every real crawl via `archive_foreign_pinned` — parallel to
   `scan_no_sponsorship.py`. Called by `crawl.pre_filter`,
   `prefilter_staged.pre_filter_relaxed`, and `ingest.ingest_job` (so a manual
   paste is gated too). `location_passes` only ever subtracts.

   **Positive gate composes two SSOTs.** A row clears the pre-filter's positive
   location check if the YAML `location_allow` matches (remote / flexible /
   region terms) **OR** `derive_country(location)` is in `TARGET_COUNTRIES` — so
   target cities the YAML doesn't enumerate ("Galway", "Montreal", province
   codes) aren't dropped, without duplicating `derive_country`'s knowledge in the
   YAML. `derive_country` is pure-string (no Claude), so it's pre-filter-safe.
   Then `location_passes` subtracts (US remote-only, foreign-pinned reject).
   `is_remote_role` is also the SSOT for the stored `job_type`.

## Company-filter SSOT — single source of truth

There is exactly **one** rule for "should this company be hidden from apply
surfaces". It lives in `scripts/config.py:company_block_reason()`, alongside
the constants `MAX_ACTIVE_APPS_PER_COMPANY` and `IN_FLIGHT_STATUSES`.

| Concern | Canonical location | How to read it |
|---|---|---|
| "Hide this company right now?" predicate | `scripts/config.py:company_block_reason()` | `from config import company_block_reason` |
| Concurrent-application limit per company | `scripts/config.py:MAX_ACTIVE_APPS_PER_COMPANY` | same import |
| What "in-flight" means | `scripts/config.py:IN_FLIGHT_STATUSES` (frozenset) | same import |

### Rules

1. **Never reimplement the company-throttle rule.** No "if 3 apps at this
   company then skip" loop anywhere else. A per-company cap encoded at
   ingest time (or as a date-based cooldown) blocks good roles from
   entering the pipeline; only `company_block_reason` at apply time is
   correct.

2. **Company filtering is intentionally apply-time, not ingest-time.** The
   crawl + ingest layer is permissive (only `ethics_hard_exclude` blocks at
   ingest — that's an absolute "never work here" switch, not a throttle).
   `company_block_reason` is consulted only by surfaces that present jobs
   for application: `serve.py:render_cover_letters_body` and
   `run.py:generate_cover_letters`. Adding it elsewhere breaks the model.

3. **No date-based cooldown in the throttle.** A rejection / interview /
   withdraw / offer flips status and/or sets `response_date` in
   `update_status.cmd_status`, which immediately frees the slot. There is no
   time-based timer anywhere in the throttle — `company_block_reason` reads
   only application *status*. (This is distinct from
   `config.auto_age_application`, which *does* advance an application's own
   status on a timer — `applied`→`ghosted`→`rejected`. That's status aging,
   not a throttle cooldown: the throttle never inspects dates, only the
   resulting status.)

4. **`ghosted` does NOT count as in-flight.** Once an application ages
   past `GHOSTED_DAYS` without a response and gets auto-flipped to
   `ghosted` (by `config.auto_age_application`), the slot is freed —
   explicit choice so silently-dead apps don't permanently block the
   company. After `GHOSTED_REJECTED_DAYS` the same function converts it to
   `rejected` (reason `ghosted_timeout`); still not in-flight, still freed.

5. **Tuning the limit:** edit `MAX_ACTIVE_APPS_PER_COMPANY` only. UI
   labels read the constant.

## Ingest-time hard excludes

Two mechanical rules can discard a posting at ingest. Both log via
`append_log({"event_type": "job_discarded", ...})` and the job never enters
the pipeline. Neither is a throttle — both are absolute.

| Rule | Scope | Canonical location |
|---|---|---|
| `ethics_hard_exclude` on the company record | per-company | checked in `scripts/ingest.py:get_or_stub_company` |
| JD text explicitly refuses sponsorship | per-JD | `scripts/config.py:detect_no_sponsorship` (+ `_NO_SPONSORSHIP_PATTERNS`); called in `scripts/ingest.py:ingest_job` before scoring. **Skipped for US-derived roles** (`derive_country(location) == "US"`) — the operator is a US citizen, so a US JD's "no sponsorship" boilerplate isn't disqualifying. CA/IE/OTHER still run it. The same skip guards `scan_no_sponsorship.py`. |
| Location not an enabled target geography | per-JD | `scripts/config.py:location_passes`; called in `scripts/ingest.py:ingest_job` after validation (also in both pre-filters). Discards US roles when US is off / not remote. Not a throttle — a geography gate. |

`ethics_hard_exclude` is set by company research. The Haiku model returns
its own judgment on the field; on top of that, deterministic rules can
force the flag to `True` regardless of what the LLM said. All rules
funnel through one SSOT entry point:

| Concern | Canonical location |
|---|---|
| Unified auto-exclude entry point | `scripts/config.py:company_auto_exclude_reason` — returns a reason string for the first rule that fires; applied in `scripts/research_company.py:research_company` after Tier-2 merge |
| Rule: direct defense contractor | `scripts/config.py:is_defense_contractor` (+ `_DEFENSE_INDUSTRY_RE`) — industry-field match |
| Rule: confirmed employee-targeted surveillance | `scripts/config.py:is_employee_surveillance_flag` (+ `_EMPLOYEE_SURVEILLANCE_RE`) — per-flag, description match |
| Rule: confirmed mass surveillance | `scripts/config.py:is_mass_surveillance_flag` (+ `_MASS_SURVEILLANCE_DESC_RE`) — per-flag, description match |

### Rules

1. **One detector for "JD refuses sponsorship".** The regex set lives only in
   `_NO_SPONSORSHIP_PATTERNS` and is consumed through `detect_no_sponsorship`.
   Don't reach around it with ad-hoc string checks elsewhere. (Gating the
   *call* by country — the US skip via `derive_country` at the two call sites —
   is fine; that doesn't duplicate or bypass the detector itself.)

2. **Per-JD ≠ company-level sponsorship.** The composite's `sponsorship`
   weight (35) is a per-company historical score from Haiku research. A
   company can score well there and still have an individual JD that opts
   out — these two signals are intentionally independent.

3. **Retroactive cleanup:** `scripts/scan_no_sponsorship.py` exists for
   one-off passes over already-ingested rows (e.g. when the regex set
   expands). Default is dry-run; pass `--apply` to archive. New ingests
   are caught automatically by `ingest_job`, so don't schedule this.

4. **All auto-exclude policy lives in `config.py`.** Add new rules as
   `is_<X>` predicates next to the existing three, then wire them into
   `company_auto_exclude_reason`. Don't reach around them with ad-hoc
   keyword checks elsewhere, and don't put rules in the Haiku prompt —
   per the "deterministic rules in code" principle, hard policy belongs
   in Python. Claude returns categorized, described flags; we own the
   judgment of which combinations force exclusion.

5. **Adding a rule = retroactive sweep too.** When you add a new
   `is_<X>` predicate or broaden an existing one, run the predicate over
   the existing `company_registry.json` and flip + archive any matches.
   `ethics_hard_exclude` blocks future ingest, not existing pipeline
   rows — `company_block_reason` does not consult it at apply time. So
   the retroactive step must also set `pipeline_status="archived"` and
   `archived_reason` on the affected company's active jobs.

## Government / defense entanglement screen (SSOT)

A graded screen layered on top of the tier_a defense exclusion above. The
concern is **personal assignment risk** in a specific role, not whether a
company merely has government customers — so the surfaced result is a function
of a company-level flag AND the role's exposure.

**Ranking effects are apply-time only** (Phase 2). A `flag` reduces the
apply-rank by `GOV_SCREEN_FLAG_PENALTY_PCT`; a `fail` (tier_a) hides the role
from apply surfaces. The canonical `composite_score` stays **pure** — the
penalty lives only in the `apply_rank_score` wrapper, never inside the
composite (so `metrics.py`'s "components sum to composite" invariant holds).

| Concern | Canonical location |
|---|---|
| Config (flagged regions, penalty pct, support-exposed toggle) | `scripts/config.py:GOV_SCREEN_FLAGGED_REGIONS` / `GOV_SCREEN_FLAG_PENALTY_PCT` / `GOV_SCREEN_SUPPORT_ROLES_EXPOSED` |
| Company flag (`gov_defense_flag`) detection | Haiku in `research_company.py` (Tier-1 prompt), floored to `tier_a` by `config.reconcile_gov_defense_flag` |
| Role exposure (`role_exposure`) classification | Sonnet in `score_jd.py` (rubric) + deterministic title rules in `config.classify_role_exposure` (applied at `ingest.py`) |
| Combination matrix → `(result, emit_questions)` | `config.gov_screen_result` (+ `_GOV_SCREEN_MATRIX`) — Part 3 of the spec, authoritative |
| Apply-rank penalty (`flag`) | `config.apply_rank_score` (= `composite_score` × `gov_screen_penalty_factor`) |
| Apply-surface exclusion (`fail`) | `config.gov_screen_block_reason` (parallel to `company_block_reason`) |
| Interview questions | `config.GOV_SCREEN_INTERVIEW_QUESTIONS` |
| Surfacing | `serve.py:_render_gov_company_block` (company card), `_render_gov_job_block` (job detail), and the apply-queue badge in `render_cl_row` |

### Rules

1. **Same division of labor as the ethics auto-excludes.** Claude detects and
   describes (flag tier, role exposure); Python owns the policy (the matrix,
   the `tier_a` floor, the support-exposed toggle). Don't put the combination
   matrix in a prompt.

2. **`result` is derived on display, never stored.** Only the two inputs
   persist (`gov_defense_flag` on the company, `role_exposure` on the job).
   Compute the result with `config.gov_screen_result` at surface time so a
   later company re-research can't leave a stale result behind.

3. **Two exclusion paths for `tier_a`, both correct.** An *industry*-detected
   defense contractor is excluded at **ingest** via `is_defense_contractor` →
   `ethics_hard_exclude` (the job never enters). A Haiku-only `tier_a` (not
   caught by the industry regex) is excluded at **apply time** via
   `gov_screen_block_reason` (result `fail`) — the job still ingests and is
   visible on `/job/<id>`, but is hidden from the apply queue + cover-letter
   generation. This is intentional: ingest stays permissive; apply surfaces
   enforce.

4. **The penalty lives ONLY in `apply_rank_score`.** Never bake the gov
   penalty into `composite_score` (it would break the metrics
   "components sum to composite" invariant and silently move the canonical
   score). Apply-time sort surfaces — `serve.render_cover_letters_body` and
   `run.generate_cover_letters` — rank by `apply_rank_score` while still
   *displaying* the pure composite. Pre-research ranking is never penalized
   (the company flag isn't known before research).

## Other project notes

- This is a **closed-source / proprietary** project. No `LICENSE` file, no
  contribution guidelines, no OSS framing. Treat as a commercial product.
- Data files (`data/*.json`, `output/*.docx`) are gitignored.
- The user's active resume in `profile/resume.md` is the Canada variant;
  Ireland variant is tracked separately. Don't propose merging.

## Persistent user-level memories

If you're reading this through Claude Code, the project memory directory at
`~/.claude/projects/.../memory/` carries durable context — user role,
feedback rules, project decisions. Check the index there for items relevant
to your task.
