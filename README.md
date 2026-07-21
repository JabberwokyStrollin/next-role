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
sections you tick off as you work through them. Opening this page also runs
two self-cleaning sweeps (no cron needed): application aging (ghosted →
rejected) and **pipeline expiry** — un-applied jobs older than
`PIPELINE_EXPIRY_DAYS` (45) since they were ingested are auto-archived, so the
apply queue never fills with months-old postings. (The same expiry sweep also
runs at the end of every crawl.)

1. **Status updates** — outcomes on already-submitted applications
   (recruiter screens, interview requests, rejections, offers). Rejections
   are categorized — generic, position filled, or **interview failed** — so
   `/metrics` can break them down. Two sub-tabs: **Active** (live
   applications) and **Ghosted** (no response past 21 days; these
   auto-convert to a rejection once they pass 45 days since you applied, so
   the list keeps itself clean). The **Scan inbox for replies** button pulls
   rejection and interview-request emails from the last 14 days (via IMAP),
   matches them to your open applications, and stages one-click status
   suggestions — you review and Apply/Dismiss each; nothing changes
   automatically. It never marks your mail read (see *Mailbox scan* below).
2. **Crawl** — kick off the two-lane crawler in the background and
   watch its tail update live.
3. **LinkedIn ingest** — pull job-alert emails via IMAP, pre-filter
   them, fetch the JD body on demand for each row, ingest the ones
   you like.
4. **Cover letters & apply** — ranked apply queue. For each job:
   generate a cover letter, optionally generate a comp estimate, open
   the `.docx`, log the application. An **Applications sent today: X / 10**
   meter tracks the day's submissions; this section auto-earns its green
   checkmark once you hit the daily goal (10 by default —
   `DAILY_APPLICATION_GOAL` in `scripts/config.py`). The count is derived
   from that day's logged applications, so it resets every day. The
   **Answer Questions** button on each row opens `/answer-questions?job_id=…`
   for ad-hoc application prompts ("Why this company?", "Tell us about a time
   you…") — paste the question, hit Generate, copy the result.

The plain `/` route is the single-URL ingest form — paste a posting URL,
fill in title/company/location, submit. The server auto-fetches the JD
if it can; for JS-rendered pages (Workday, Taleo) it prompts you to
paste the text manually.

`/metrics` shows cohort comparisons (in-flight vs rejected/ghosted vs
offers) across every composite-score component, a composite-score
distribution, a rejection-reason breakdown (generic / position filled /
interview failed / auto-ghosted), and funnel-speed stats. Useful for asking
"is the composite predicting outcomes?" and "how often do I lose at the
interview vs the screen?". Data only — no Claude calls.

`/search?q=...` (also reachable from the top-nav search box on every
page) does a case-insensitive substring match on company name and job
title across non-archived pipeline jobs. Built for recruiter-call prep:
type the company, click the role, land on `/job/<id>` with the JD,
company-research card (industry, sponsorship, remote, layoffs, ethics
flags), a government/defense screen (per-role exposure × company flag — a
`flag` penalizes apply-queue ranking, a `fail` hides the role), comp
estimate, and application timeline. Jobs with a logged
application appear first (most-recently applied first); the rest follow
by composite score.

### Option B — Command line

```bash
# Ingest a single job
python run.py --url "https://boards.greenhouse.io/company/jobs/123456"

# Ingest from a file (one URL per line; optional date: https://... YYYY-MM-DD)
python run.py --url-file urls.txt

# Research the top 20 stub companies (ranked by pre-research composite),
# then refresh the dashboard
python run.py --research-queue 20

# Preview which stubs --research-queue would pick, without spending credits
python run.py --research-queue 20 --dry-run

# Full daily run: ingest + research queue + dashboard
python run.py --url-file urls.txt --research-queue 20

# Generate cover letters for top 3 jobs (interactive — prompts y/n)
python run.py --cover-letters --top 3

# Same but non-interactive (generates without asking)
python run.py --auto-cl --top 3
```

> **Two-stage ranking.** `--research-queue` ranks stubs by the
> **pre-research composite** (stack + domain + seniority + velocity +
> freshness only — sponsorship and remote are zero-weighted so stub
> defaults can't bias the order). Cover-letter generation always ranks
> by the **full composite** so sponsorship and remote-fit matter at
> apply time. See *Scoring* below and `CLAUDE.md` for the SSOT
> convention. The inherited `--research-top N` flag still exists — it
> ranks stubs by the full composite — but `--research-queue` is the
> preferred entry point.

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

### Mailbox scan (rejections & interview requests)

Instead of hand-updating status from each email, let next-role read them
for you. The **Scan inbox for replies** button in the `/today` *Status
updates* section (or `python scripts/inbox_scan.py`) connects over IMAP,
looks at inbox mail from the last `INBOX_SCAN_WINDOW_DAYS` days (14),
matches each message to one of your open applications by company name /
sender domain, and classifies it as a **rejection** or **interview
request** using deterministic phrase rules (no Claude call, no cost).

Each hit is **staged** as a one-click suggestion — you Apply (which runs
the normal status update) or Dismiss it. Nothing changes automatically.

It uses the same `NEXTROLE_IMAP_*` credentials as LinkedIn ingest (see
SETUP.md) and is careful with your mailbox: it **never marks messages
read** (every fetch uses `BODY.PEEK`) and keeps its own dedup state, so
reading a message in your own mail client neither hides it from the
scanner nor is affected by the scan.

```bash
python scripts/inbox_scan.py                 # scan + stage matches
python scripts/inbox_scan.py --dry-run       # classify only, write nothing
python scripts/inbox_scan.py --window-days 30  # widen the look-back window
python scripts/inbox_scan.py --reset         # clear staged matches + dedup state
```

---

## Scoring (summary)

next-role keeps **two** composite scores per job, each used at a
different stage of the workflow:

| Profile | Ceiling | Used for | Weights |
|---|---:|---|---|
| **Pre-research composite** | 100 | Ranking stub companies for the research queue | Stack 25, Domain 32, Seniority 18, Velocity 15, Freshness 10. **Sponsorship and Remote are zero-weighted** — their stub defaults would otherwise dominate the ordering. |
| **Full composite** | 130 | Apply-time ranking + cover-letter selection | Stack 30, Domain 25, Seniority 10, Velocity 10, Freshness 8, **Sponsorship 35, Remote 12**. |

Both profiles share one storage scale per component (e.g. Claude's
seniority is always 0-25) and only differ in how those stored points
get weighted into the composite total. All weights live in
`scripts/config.py:COMPONENTS`; both scoring functions
(`composite_score` and `composite_score_pre_research`) read from there.
**No other file may duplicate them** — see `CLAUDE.md` for the SSOT
convention.

### Target geographies (US is optional)

next-role targets **Canada** and **Ireland** (where the operator needs visa
sponsorship) plus, as an **optional remote-only stop-gap**, the **US**. Active
geographies live in `scripts/geography.py:TARGET_COUNTRIES` (currently
`{"CA","IE","US"}` — remove `"US"` to disable). When US is enabled:

- Only **remote** US roles enter the pipeline (onsite/hybrid US is gated out).
- US JDs that say "no sponsorship" are **kept** (the operator is a US citizen),
  whereas that language still discards CA/IE roles.
- US roles get a deliberately **low sponsorship score**
  (`US_SPONSORSHIP_SCORE`, default 3/15) so CA/IE roles generally outrank them —
  but a strong-stack US role can still beat a weak CA/IE one (thumb-on-scale,
  not a hard tier).

Turn US back off (remove `"US"` from `TARGET_COUNTRIES`) and behavior reverts
exactly — CA/IE composites are unchanged.

### Two-stage workflow

1. **Ingest** creates a stub company record with neutral defaults so
   you don't pay for research on every JD.
2. **`run.py --research-queue N`** ranks the stub-attached active jobs
   by *pre-research* composite (no contamination from stub defaults),
   applies a minimum-score gate (`RESEARCH_QUEUE_MIN_SCORE`, default
   45), and runs the two-tier Haiku flow on the top N distinct
   companies.
3. **Apply queue / cover letters** rank by *full* composite — so
   sponsorship and remote-fit signal matters at the moment you decide
   to apply.

The inherited `run.py --research-top N` flag still ranks by full
composite, but the stub defaults bias which companies surface there;
`--research-queue` is the preferred entry point for routine research.

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
| Answer one application question | Sonnet 4.6 | ~$0.02–0.05 |

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
requirements.txt        — Python dependencies (pip install -r)
run.py                  — CLI orchestrator
serve.py                — local web UI (localhost:5000)
scripts/
  config.py             — shared paths, constants, scoring + filter SSOTs
  geography.py          — location → country SSOT + geography gate (dependency-free; callable by JS)
  ingest.py             — fetch + validate + score + write one job
  score_jd.py           — Claude seniority + domain scoring
  research_company.py   — two-tier company research (Haiku + 1 web search)
  crawl.py              — two-lane (aggregators + ATS) crawler
  prefilter_staged.py   — relaxed pre-filter for LinkedIn-staged rows
  linkedin_fetch.py     — IMAP fetch of LinkedIn job-alert emails
  inbox_scan.py         — IMAP scan for rejection / interview replies to open applications
  dashboard.py          — terminal pipeline summary
  update_status.py      — application logging + status transitions
  metrics.py            — read-only analytics for the /metrics page
  comp_estimate.py      — Opus-driven salary + bonus estimator
  answer_questions.py   — Sonnet-driven ad-hoc application question answers
  generate_cl.js        — cover-letter generator → .docx (Node)
  rescore_all.py        — bulk re-score under a new rubric
  scan_no_sponsorship.py — retroactive no-sponsorship sweep
  scan_foreign_locations.py — retroactive foreign-pinned-location sweep
  scan_stale_jobs.py    — expire jobs sitting un-applied past PIPELINE_EXPIRY_DAYS
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
