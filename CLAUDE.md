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

The composite ranking system has exactly three canonical sources of truth.
**Never duplicate the values in another file.** If you need a denominator,
weight, or rule, import it from one of these.

| Concern | Canonical location | How to read it |
|---|---|---|
| Composite component weights + display denominators | `scripts/config.py:COMPONENTS` + `COMPOSITE_MAX` | `from config import COMPONENTS, COMPOSITE_MAX` |
| Title → seniority cap | `scripts/config.py:_SENIORITY_BUCKETS` (via `title_seniority_cap()` / `apply_title_cap()`) | `from config import apply_title_cap` |
| Stack keyword scores + stack max + pre-filter title/location lists | `profile/stack_keywords.yaml` | loaded once by `_load_stack_keywords()` and `load_crawl_config()` |
| Claude's native output ranges (seniority 0-25, domain 0-20) | `profile/scoring_rubric.md` | mirrored in `COMPONENTS[k].native_max`; update both together |

### Rules

1. **Never define a parallel `composite_score` function** anywhere outside
   `scripts/config.py`. There is exactly one. A duplicate copy silently
   falls behind config.py whenever a weight changes.

2. **Never inline a partial composite** for sort order or display, even
   "just to skip the company lookup." Skipping sponsorship, remote, and
   freshness surfaces US-only stack-heavy roles above sponsorship-history
   Canada matches — which inverts the apply queue. If you need to sort, do:
   ```python
   co_by_id = load_companies_by_id()
   sorted_jobs = sorted(jobs, key=lambda j: composite_score(j, co_by_id.get(j.get("company_id"))), reverse=True)
   ```

3. **Never hardcode a score denominator** in display code. No `/35`,
   `/130`, `f"Stack X/25"`, etc. Always read from `COMPONENTS[k].native_max`
   (when showing the stored value) or `COMPONENTS[k].weight` (when showing
   the contribution to composite) or `COMPOSITE_MAX` (for the overall total).

4. **Pre-filter is intentionally separate.** `scripts/crawl.py:pre_filter`
   and `scripts/prefilter_staged.py:pre_filter_relaxed` run BEFORE any
   Claude calls — they MUST NOT call `composite_score` or any function
   that requires Claude/research output. Doing so would multiply API
   costs by ~1000x (raw aggregator output vs the few that pass filtering).
   The pre-filter has its own SSOT in `profile/stack_keywords.yaml`.

5. **Extending the composite:** add a new key to `COMPONENTS` AND a new
   line in `composite_score()`'s `raw` dict. Don't add code in other
   files that reaches around the SSOT.

6. **Tuning a weight:** edit `COMPONENTS` only. Display denominators auto-
   update because surfaces read from it.

7. **Tuning Claude's output range** (e.g. changing seniority from /25
   to /30): edit `profile/scoring_rubric.md` AND `COMPONENTS["seniority"].native_max`
   in lockstep. The rubric tells Claude the range; `native_max` tells the
   composite math how to multiply it.

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

3. **No date-based cooldown.** A rejection / interview / withdraw / offer
   flips status and/or sets `response_date` in `update_status.cmd_status`,
   which immediately frees the slot. There is no time-based timer anywhere
   in the throttle — application status is the only gate.

4. **`ghosted` does NOT count as in-flight.** Once an application ages
   past `GHOSTED_DAYS` without a response and gets auto-flipped to
   `ghosted`, the slot is freed — explicit choice so silently-dead apps
   don't permanently block the company.

5. **Tuning the limit:** edit `MAX_ACTIVE_APPS_PER_COMPANY` only. UI
   labels read the constant.

## Ingest-time hard excludes

Two mechanical rules can discard a posting at ingest. Both log via
`append_log({"event_type": "job_discarded", ...})` and the job never enters
the pipeline. Neither is a throttle — both are absolute.

| Rule | Scope | Canonical location |
|---|---|---|
| `ethics_hard_exclude` on the company record | per-company | checked in `scripts/ingest.py:get_or_stub_company` |
| JD text explicitly refuses sponsorship | per-JD | `scripts/config.py:detect_no_sponsorship` (+ `_NO_SPONSORSHIP_PATTERNS`); called in `scripts/ingest.py:ingest_job` before scoring |

### Rules

1. **One detector for "JD refuses sponsorship".** The regex set lives only in
   `_NO_SPONSORSHIP_PATTERNS` and is consumed through `detect_no_sponsorship`.
   Don't reach around it with ad-hoc string checks elsewhere.

2. **Per-JD ≠ company-level sponsorship.** The composite's `sponsorship`
   weight (35) is a per-company historical score from Haiku research. A
   company can score well there and still have an individual JD that opts
   out — these two signals are intentionally independent.

3. **Retroactive cleanup:** `scripts/scan_no_sponsorship.py` exists for
   one-off passes over already-ingested rows (e.g. when the regex set
   expands). Default is dry-run; pass `--apply` to archive. New ingests
   are caught automatically by `ingest_job`, so don't schedule this.

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
