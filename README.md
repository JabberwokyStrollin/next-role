# next-role

Most job-search tools are spam bots — fire thousands of applications and hope something sticks. **next-role** is an aim bot. It identifies the exact positions you're qualified for, scores them against your profile, researches each company, and generates a targeted cover letter. You apply to fewer jobs, but to the right ones.

Built for senior engineers who would rather spend an afternoon on one strong application than a week on fifty weak ones.

---

## How it works

1. **Ingest** — paste a job URL (or the JD text for JS-rendered portals). The pipeline fetches the posting, scores it against your stack and target role criteria, and adds it to a local JSON pipeline.
2. **Research** — for your top-ranked jobs, a second Claude call researches the company: sponsorship history, remote-work patterns, recent layoffs, ethics flags.
3. **Dashboard** — a terminal summary ranks all active jobs by composite score so you know where to focus.
4. **Cover letter** — one command generates a tailored `.docx` cover letter using your resume, your rules, and the specific JD.

All scoring criteria live in your `profile/` directory — it's your rubric, not a generic one.

---

## Prerequisites

- Python 3.11+
- Node.js 18+
- An [Anthropic API key](https://console.anthropic.com/)

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/yourname/next-role.git
cd next-role

# Python dependencies
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux
pip install anthropic requests beautifulsoup4

# Node dependencies (cover letter generator)
npm install
```

### 2. Set your API key

**Windows (persistent):**
```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
```

**Mac/Linux:**
```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc
```

### 3. Configure your profile

Copy the example profile and fill in your details:

```bash
cp -r profile.example profile   # Mac/Linux
# Windows:
Copy-Item -Recurse profile.example profile
```

Edit these four files in `profile/`:

| File | What it controls |
|---|---|
| `resume.md` | Your resume — injected into the cover letter prompt |
| `cover_letter_rules.md` | Tone, section structure, projects to reference, work authorization paragraphs |
| `scoring_rubric.md` | Claude system prompt for JD scoring — your seniority and domain criteria |
| `stack_keywords.md` | Keyword weights for mechanical stack-match scoring |

> **profile/ is gitignored.** Your resume and personal scoring criteria never leave your machine. Back up this directory externally (OneDrive, Dropbox, or a private repo).

### 4. (Optional) Set up data persistence

`data/` and `output/` are gitignored. The easiest way to persist them is a symlink to a cloud-synced folder:

```powershell
# Windows — move data/ to OneDrive, symlink back
Move-Item data "$env:USERPROFILE\OneDrive\next-role-data"
New-Item -ItemType SymbolicLink -Path data -Target "$env:USERPROFILE\OneDrive\next-role-data"
```

---

## Daily workflow

### Option A — Web UI (recommended for ingestion)

```bash
python serve.py
```

Opens `http://localhost:5000` in your browser. Paste a job URL, fill in the title/company/location, and submit. The server auto-fetches the JD if it can; if the page is JS-rendered (Workday, Taleo), it prompts you to paste the text manually.

### Option B — Command line

```bash
# Ingest a single job
python run.py --url "https://boards.greenhouse.io/company/jobs/123456"

# Ingest from a file (one URL per line, optional date: https://... YYYY-MM-DD)
python run.py --url-file urls.txt

# Full daily run: ingest + research top 5 companies + dashboard
python run.py --url-file urls.txt --research-top 5

# Generate cover letters for top 3 jobs (interactive — prompts y/n)
python run.py --cover-letters --top 3

# Same but non-interactive (generates without asking)
python run.py --auto-cl --top 3
```

> **Top-1 per company:** when picking candidates, only the highest-scoring open role at each company is surfaced — so you don't end up writing three cover letters for three jobs at the same employer in one batch. Sibling roles stay in the pipeline; the next-best surfaces once you apply to or archive the current one. (Separate from the 90-day post-apply company cooldown set by `update_status.py log`.)

### Check the pipeline

```bash
python run.py --dashboard
```

Shows a ranked table of all active jobs with composite scores, staleness, and apply links.

---

## Crawl

`--crawl` automates discovery so you don't have to feed URLs in by hand. It runs two lanes in one pass:

**Lane 1 — Aggregators (broad).** Hits the public RemoteOK and Remotive APIs using your `aggregator_tags` / `aggregator_keywords`. Catches smaller and unknown companies you'd never think to look at.

**Lane 2 — ATS direct (deep).** Calls Greenhouse, Lever, and Ashby boards directly for companies you've curated in `data/target_boards.json`. Thorough coverage of every open role at companies you actually care about.

Both lanes share a cheap mechanical pre-filter — title must match a seniority term, location must match an allowed region, and the JD must clear `min_pre_filter_score` from your stack keywords — before anything is sent to Claude for full ingest. Apply URLs from Lane 1 that point at a known ATS are auto-added to `target_boards.json`, so the curated list grows on its own.

### Workflow

```bash
# See what would be ingested without spending API credits
python run.py --crawl --dry-run

# Run a real crawl, then research top stubs and show dashboard
python run.py --crawl --research-top 5
```

You can also call `scripts/crawl.py` directly for finer-grained control:

```bash
python scripts/crawl.py --source remoteok       # one source only
python scripts/crawl.py --limit 10 --verbose    # cap ingest, show pre-filter decisions
```

### Configuration

**`data/target_boards.json`** — list of ATS boards to poll directly. Starts empty; entries get appended automatically when Lane 1 turns up a company on a known ATS. You can also seed it by hand:

```json
[
  {"company": "Acme Data", "ats": "greenhouse", "slug": "acmedata"},
  {"company": "Stream Co",  "ats": "lever",      "slug": "streamco"},
  {"company": "Pipeline Inc","ats": "ashby",      "slug": "pipelineinc"}
]
```

`ats` is one of `greenhouse`, `lever`, or `ashby`. `slug` is the company identifier in the board URL (e.g. `boards.greenhouse.io/<slug>`).

**`profile/stack_keywords.md` → `## Crawl Config`** — controls the pre-filter and aggregator queries:

| Key | Purpose |
|---|---|
| `seniority_titles` | Comma-separated terms; the job title must contain at least one |
| `title_exclude` | Comma-separated terms; reject the title if it contains any (filters pre-sales / customer-success roles that slip past `architect` / `lead`) |
| `location_allow` | Comma-separated regions; the location must contain at least one (or `remote`) |
| `aggregator_tags` | RemoteOK tag filters. Use `\|` to separate groups; tags within a group are AND-filtered. Each group fires its own API call. Use `/` for one-or-the-other alternates at a position (e.g. `spark/databricks, python` fans out to two queries — one for each alternate) — useful for technologies that don't co-occur. Example: `kafka, flink, java \| spark/databricks, python` |
| `aggregator_keywords` | Remotive full-text search queries. Use `\|` to separate groups; each group is a separate query. `/` works the same way as in `aggregator_tags`. Example: `kafka flink java \| spark/databricks python` |
| `min_pre_filter_score` | Minimum stack-keyword score required before full ingest |

---

## Scoring

Each job gets a composite score out of 105:

| Component | Max | Source |
|---|---|---|
| Stack match | 35 | Keyword scan of JD (`profile/stack_keywords.md`) |
| Seniority alignment | 25 | Claude (`profile/scoring_rubric.md`) |
| Domain fit | 20 | Claude (`profile/scoring_rubric.md`) |
| Hiring velocity | 5 | Days since posted |
| Sponsorship | 15 | Company research |
| Remote fit | 5 | Company research |

Company research is deferred by default — a stub record is created on ingest so you don't pay for research on every job. Run `--research-top N` to research only the companies attached to your N highest-ranked jobs.

---

## Tracking applications

After submitting an application:

```bash
python scripts/update_status.py log --job-id <uuid> --method greenhouse
```

Update status as things progress:

```bash
python scripts/update_status.py status --app-id <uuid> --status recruiter_screen
# statuses: recruiter_screen, technical_screen, onsite, offer, rejected, withdrawn
```

---

## Cost reference

| Operation | Model | Approx. cost |
|---|---|---|
| Ingest + score one job | Sonnet | ~$0.003 |
| Research one company | Haiku + 1 web search | ~$0.05–0.07 |
| Generate cover letter | Sonnet | ~$0.03 |

---

## Project structure

```
run.py                  — CLI orchestrator
serve.py                — local web UI (localhost:5000)
scripts/
  config.py             — shared paths, constants, JSON helpers
  ingest.py             — fetch, validate, score, write job to pipeline
  score_jd.py           — Claude seniority + domain scoring
  research_company.py   — two-tier company research (Haiku + 1 web search)
  crawl.py              — two-lane job board crawler (aggregators + ATS direct)
  dashboard.py          — terminal pipeline summary
  update_status.py      — application logging and status tracking
  generate_cl.js        — cover letter generator → .docx
profile/                — gitignored: your resume, rules, and scoring criteria
profile.example/        — committed templates — copy to profile/ to get started
data/                   — gitignored: pipeline JSON files
output/                 — gitignored: generated cover letters (.docx)
```

---

© 2026 Johnny Ray Blanton III. All rights reserved.
