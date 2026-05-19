# next-role

Most job-search tools are spam bots — fire thousands of applications and
hope something sticks. **next-role** is an aim bot. It identifies the
exact positions you're qualified for, scores them against your profile,
researches each company, and generates a targeted cover letter. You
apply to fewer jobs, but to the right ones.

Built for senior engineers who would rather spend an afternoon on one
strong application than a week on fifty weak ones.

---

## How it works

1. **Ingest** — paste a job URL (or the JD text for JS-rendered portals).
   The pipeline fetches the posting, scores it against your stack and
   target-role criteria, and adds it to a local JSON pipeline.
2. **Research** — for your top-ranked jobs, a second Claude call
   researches the company: sponsorship history, remote-work patterns,
   recent layoffs, ethics flags.
3. **Dashboard** — a terminal summary (and the `/today` web UI) ranks
   all active jobs by composite score so you know where to focus.
4. **Cover letter** — one command generates a tailored `.docx` cover
   letter using your resume, your rules, and the specific JD.
5. **Comp estimate** — on demand, generate a salary + bonus estimate
   for any job to anchor your negotiation.

All scoring criteria live in your `profile/` directory — it's your
rubric, not a generic one.

---

## Documentation

| Doc | What's in it |
|---|---|
| **README.md** *(this file)* | Overview, daily operation, project structure, cost reference. |
| **[SETUP.md](SETUP.md)** | One-time install, API key, profile configuration, IMAP credentials, crawl config. |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Engineer-facing tech doc — every function in every script (`run.py`, `serve.py`, all 15 files under `scripts/`, plus `generate_cl.js`). |
| **[DATA.md](DATA.md)** | Schema reference for every file under `data/` — `job_pipeline.json`, `company_registry.json`, `application_tracker.json`, the JSONL logs, etc. |
| **[CLAUDE.md](CLAUDE.md)** | Conventions for Claude sessions. Defines the scoring SSOT, company-filter SSOT, and ingest-time hard excludes. Read this before changing any scoring or filtering code. |

First time? Start with **SETUP.md**. Already set up? Skip to *Daily
workflow* below.

---

## Daily workflow

### Option A — Web UI (recommended)

```bash
python serve.py
```

Opens `http://localhost:5000/today` — the daily checklist with four
sections you tick off as you work through them:

1. **Status updates** — outcomes on already-submitted applications
   (rejections, interview requests, position-filled letters).
2. **Crawl** — kick off the two-lane crawler in the background and
   watch its tail update live.
3. **LinkedIn ingest** — pull job-alert emails via IMAP, pre-filter
   them, fetch the JD body on demand for each row, ingest the ones
   you like.
4. **Cover letters & apply** — ranked apply queue. For each job:
   generate a cover letter, optionally generate a comp estimate, open
   the `.docx`, log the application.

The plain `/` route is the single-URL ingest form — paste a posting URL,
fill in title/company/location, submit. The server auto-fetches the JD
if it can; for JS-rendered pages (Workday, Taleo) it prompts you to
paste the text manually.

### Option B — Command line

```bash
# Ingest a single job
python run.py --url "https://boards.greenhouse.io/company/jobs/123456"

# Ingest from a file (one URL per line; optional date: https://... YYYY-MM-DD)
python run.py --url-file urls.txt

# Full daily run: ingest + research top 5 companies + dashboard
python run.py --url-file urls.txt --research-top 5

# Generate cover letters for top 3 jobs (interactive — prompts y/n)
python run.py --cover-letters --top 3

# Same but non-interactive (generates without asking)
python run.py --auto-cl --top 3
```

> **Apply-queue throttle.** Both the `/today` queue and `run.py
> --cover-letters` hide any company that already has
> `MAX_ACTIVE_APPS_PER_COMPANY` (default 3) in-flight applications. Once
> a status moves to a terminal state (or `response_date` is set), the
> slot frees immediately — there is no time-based cooldown. The rule
> lives in `scripts/config.py:company_block_reason`; see `CLAUDE.md`
> for the SSOT convention.

### Check the pipeline

```bash
python run.py --dashboard
```

Ranked table of active jobs with composite scores, staleness, and
clickable apply links.

---

## Crawl

`--crawl` automates discovery so you don't have to feed URLs in by hand.
It runs two lanes in one pass:

**Lane 1 — Aggregators (broad).** Hits the public RemoteOK and Remotive
APIs using your `aggregator_tags` / `aggregator_keywords`. Catches
smaller and unknown companies you'd never think to look at.

**Lane 2 — ATS direct (deep).** Calls Greenhouse, Lever, and Ashby
boards directly for companies you've curated in
`data/target_boards.json`. Thorough coverage of every open role at
companies you actually care about.

Both lanes share a cheap mechanical pre-filter — title must match a
seniority term, location must match an allowed region, and the JD must
clear `min_pre_filter_score` from your stack keywords — before anything
is sent to Claude. Apply URLs from Lane 1 that point at a known ATS are
auto-added to `target_boards.json`, so the curated list grows on its own.

```bash
# See what would be ingested without spending API credits
python run.py --crawl --dry-run

# Run a real crawl, then research top stubs and show dashboard
python run.py --crawl --research-top 5

# Finer-grained control
python scripts/crawl.py --source remoteok       # one source only
python scripts/crawl.py --limit 10 --verbose    # cap ingest, show pre-filter decisions
```

Crawl configuration (`stack_keywords.yaml → crawl:` and
`target_boards.json`) is documented in **SETUP.md**.

---

## Tracking applications

After submitting, log the application — this is what flips the source
job to `applied` and counts against the per-company throttle:

```bash
python scripts/update_status.py log --job-id <uuid> --method greenhouse
```

Update status as things progress:

```bash
python scripts/update_status.py status --app-id <uuid> --status recruiter_screen
# statuses: recruiter_screen, interview, offer, rejected, ghosted, withdrawn
```

Applications with no response after 21 days (`GHOSTED_DAYS` in
`config.py`) auto-flip to `ghosted` the next time you run `--dashboard`
or open `/today`, which also frees the company's throttle slot. There
is no time-based cooldown — the slot is gated entirely on application
status.

---

## Scoring (summary)

Each job gets a composite score out of **130**, weighted across seven
components:

| Component | Weight | Source |
|---|---:|---|
| Sponsorship | 35 | Company research (Haiku) |
| Stack match | 30 | Keyword scan of JD (`profile/stack_keywords.yaml`) |
| Domain fit | 25 | Claude (`profile/scoring_rubric.md`) |
| Remote fit | 12 | Company research (Haiku) |
| Velocity | 10 | Days since posted |
| Seniority alignment | 10 | Claude + mechanical title-bucket cap |
| Freshness bonus | 8 | Today/yesterday/2-days-ago bump |

All weights, native score ranges, and the title-cap rules live in
`scripts/config.py:COMPONENTS` and `_SENIORITY_BUCKETS`. **No other
file may duplicate them** — see `CLAUDE.md` for the SSOT convention.

Company research is deferred by default. `ingest` creates a stub record
with neutral defaults; `run.py --research-top N` runs the two-tier
Haiku flow only for the companies attached to your N highest-ranked
jobs.

For the per-component breakdown, the title-cap buckets, and the
company-research field schema, see:

- **ARCHITECTURE.md** → `scripts/config.py` (composite, title cap,
  freshness/velocity tiers) and `scripts/research_company.py` (Tier 1 +
  Tier 2 prompts).
- **DATA.md** → `data/company_registry.json` (every researched field,
  with valid values).

---

## Cost reference

Approximate per-operation cost at current model pricing. Actual cost
varies with JD length and the resume size you carry in the system
prompt.

| Operation | Model | Approx. cost |
|---|---|---|
| Ingest + score one job | Sonnet 4.5 | ~$0.013 |
| Research one company | Haiku 4.5 + 1 web search | ~$0.03–0.05 |
| Generate cover letter | Sonnet 4.6 | ~$0.03 |
| Generate comp estimate | Opus 4.7 | ~$0.15–0.20 |

Bulk re-score under a new rubric:
`python scripts/rescore_all.py --dry-run` prints a projected bill before
calling Claude for any job.

---

## Project structure

```
README.md               — overview + daily operation (this file)
SETUP.md                — one-time install + configuration
ARCHITECTURE.md         — per-script tech reference
DATA.md                 — per-file data schema reference
CLAUDE.md               — SSOT conventions for code changes
run.py                  — CLI orchestrator
serve.py                — local web UI (localhost:5000)
scripts/
  config.py             — shared paths, constants, scoring + filter SSOTs
  ingest.py             — fetch + validate + score + write one job
  score_jd.py           — Claude seniority + domain scoring
  research_company.py   — two-tier company research (Haiku + 1 web search)
  crawl.py              — two-lane (aggregators + ATS) crawler
  prefilter_staged.py   — relaxed pre-filter for LinkedIn-staged rows
  linkedin_fetch.py     — IMAP fetch of LinkedIn job-alert emails
  dashboard.py          — terminal pipeline summary
  update_status.py      — application logging + status transitions
  comp_estimate.py      — Opus-driven salary + bonus estimator
  generate_cl.js        — cover-letter generator → .docx (Node)
  rescore_all.py        — bulk re-score under a new rubric
  scan_no_sponsorship.py — retroactive no-sponsorship sweep
  cleanup_staged_jd.py  — clear similar-jobs noise from staged rows
  backfill_target_boards.py — seed target_boards.json from existing jobs
  discover_boards_from_careers.py — find ATS boards from careers pages
profile/                — gitignored: your resume, rules, scoring criteria
profile.example/        — committed templates — copy to profile/ to start
data/                   — gitignored: pipeline JSON files (see DATA.md)
output/                 — gitignored: generated cover letters (.docx)
```

---

© 2026 Johnny Ray Blanton III. All rights reserved.
