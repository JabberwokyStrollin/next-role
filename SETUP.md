# next-role — Setup

One-time installation and configuration. After this, see `README.md` for
daily operation.

---

## Prerequisites

- Python 3.11+
- Node.js 18+
- An [Anthropic API key](https://console.anthropic.com/)

---

## 1. Clone and install dependencies

```bash
git clone https://github.com/yourname/next-role.git
cd next-role

# Python dependencies
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux
pip install -r requirements.txt

# Node dependencies (cover letter generator)
npm install
```

---

## 2. Set the API key

**Windows (persistent):**

```powershell
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
```

**Mac/Linux:**

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc
```

`scripts/config.py` raises `EnvironmentError` at import time if this is
unset, so every entry point fails fast on misconfiguration.

---

## 3. Configure your profile

Copy the example profile and fill in your details:

```bash
cp -r profile.example profile          # Mac/Linux
Copy-Item -Recurse profile.example profile   # Windows
```

Edit these files in `profile/`:

| File | What it controls |
|---|---|
| `resume.md` | Your resume — injected into the cover-letter prompt and the comp-estimate prompt. |
| `cover_letter_rules.md` | Tone, section structure, projects to reference, work-authorization paragraphs. |
| `scoring_rubric.md` | Claude system prompt for JD scoring — your seniority and domain criteria, plus the gov-screen role-exposure classification. |
| `stack_keywords.yaml` | Keyword weights for mechanical stack scoring **and** the crawl pre-filter. |

> **profile/ is gitignored.** Your resume and personal scoring criteria
> never leave your machine. Back up the directory externally (OneDrive,
> Dropbox, or a private repo).

### Code drills (optional)

The `/today` **Code drills** section generates interview-prep Java drills
with Claude and reviews your manual attempts. There's nothing to author —
click **Generate new drill prompt** and Claude produces a short,
deliberately underspecified prompt plus a partial interface (method names +
params, no return types — deciding those is part of the drill). You
implement it by hand, then click **Check my code & get feedback**.

It expects a **sibling Maven project** where the code + JUnit tests live:

```
applications/
  next-role/            ← this repo
  manual-code-drills/   ← sibling: Drill1.java, Drill2.java, … + tests
```

- The default location is `../manual-code-drills` (override with the
  `NEXTROLE_DRILLS_DIR` env var). Generated drills continue the numbering
  after the highest `Drill<N>.java` already there.
- **Open manual-code-drills** launches the folder in your editor. It defaults
  to the **VS Code** CLI (`code`), which works out of the box if VS Code's
  "code" command is on your PATH (it is by default on Windows). To use a
  different editor, set `NEXTROLE_EDITOR_CMD` to its CLI:

  ```powershell
  $env:NEXTROLE_EDITOR_CMD = "code"          # VS Code (default)
  # or an IntelliJ launcher, Sublime, etc.:
  # $env:NEXTROLE_EDITOR_CMD = "C:\Program Files\JetBrains\...\bin\idea64.exe"
  ```

  If the launch fails, the button falls back to opening the folder in File
  Explorer.
- Generation + review use the same `ANTHROPIC_API_KEY` and Sonnet model as
  cover letters; next-role never compiles or runs the Java itself. Drill
  state is stored in `data/drills.json` (gitignored).

### Resume tips

- The user's active resume in `profile/resume.md` is country-specific
  (Canada vs Ireland variant). Keep the inactive variant separately and
  swap it in when you change target market. (If you enable US roles — see
  §6 — you can keep a US variant the same way; there's no automatic
  per-country resume switching.)
- `serve.py /resume` parses Experience and Education sections out of
  `resume.md` for the copy-paste snippet builder — keep the section
  headings (`## Experience`, `## Education`) and the date-range format
  (`Jan 2020 – Mar 2026`).

### Scoring rubric

`profile/scoring_rubric.md` is the system prompt Claude sees when scoring
a JD. It defines two output ranges:

- `seniority_score` — 0-25
- `domain_fit_score` — 0-20

If you change these ranges, update `scripts/config.py:COMPONENTS` to
match — the composite math reads `native_max` from there.

---

## 4. (Optional) Data persistence

`data/` and `output/` are gitignored. The easiest way to persist them
across machines is a symlink to a cloud-synced folder:

```powershell
# Windows — move data/ to OneDrive, symlink back
Move-Item data "$env:USERPROFILE\OneDrive\next-role-data"
New-Item -ItemType SymbolicLink -Path data -Target "$env:USERPROFILE\OneDrive\next-role-data"
```

**Automatic snapshots.** Opening `/today` also takes an at-most-once-per-day
snapshot of the `data/*.json` files into `<backup-dir>/<date>/` (kept 7 days;
`scripts/backup_data.py`), so a stray delete or corrupt write is recoverable by
copying the file back. The default backup dir is `data/backups/` (in-repo,
gitignored) — that guards individual files *within* `data/`. To also survive
losing the whole `data/` tree, set `NEXTROLE_BACKUP_DIR` to a path **outside the
repo** (ideally the cloud-synced folder above):

```powershell
$env:NEXTROLE_BACKUP_DIR = "$env:USERPROFILE\OneDrive\next-role-backups"
```

See `DATA.md` for the full schema of every file under `data/`.

---

## 5. (Optional) IMAP credentials for LinkedIn ingest + mailbox scan

Two `/today` features read your inbox over IMAP (stdlib `imaplib` — no
third-party email integration) and share the same three env vars below:

- **LinkedIn ingest** (Status → LinkedIn section) pulls job postings out of
  LinkedIn job-alert emails so you don't have to copy URLs by hand — it filters
  to a sender allowlist, parses each email's HTML, and stages the postings for
  per-row review and ingest.
- **Mailbox scan** (Status updates section → *Scan inbox for replies*, or
  `python scripts/inbox_scan.py`) reads inbox mail from the last
  `INBOX_SCAN_WINDOW_DAYS` days, matches it to your open applications, and
  stages any detected rejection / interview-request replies for one-click
  status updates. Unlike LinkedIn ingest, the scanner **never marks messages
  read** and keeps its own dedup state.

Skip this section if you're sticking to the crawl + manual ingest paths.

**For Gmail**, generate an app password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(requires 2FA on your account). The app password is **not** your account
password — Google issues a separate 16-character token for IMAP clients.

**Windows (persistent):**

```powershell
[System.Environment]::SetEnvironmentVariable("NEXTROLE_IMAP_HOST", "imap.gmail.com", "User")
[System.Environment]::SetEnvironmentVariable("NEXTROLE_IMAP_USER", "you@gmail.com", "User")
[System.Environment]::SetEnvironmentVariable("NEXTROLE_IMAP_APP_PASSWORD", "abcd efgh ijkl mnop", "User")
```

**Mac/Linux:**

```bash
cat >> ~/.zshrc <<'EOF'
export NEXTROLE_IMAP_HOST="imap.gmail.com"
export NEXTROLE_IMAP_USER="you@gmail.com"
export NEXTROLE_IMAP_APP_PASSWORD="abcd efgh ijkl mnop"
EOF
```

For other providers, swap `NEXTROLE_IMAP_HOST` for your provider's IMAP
host (e.g. `outlook.office365.com`, `imap.fastmail.com`) and follow that
provider's app-password flow.

### Optional env vars

| Var | Purpose |
|---|---|
| `NEXTROLE_EDITOR_CMD` | Editor CLI for the Code-drills **Open manual-code-drills** button (default `code`, VS Code). Falls back to the OS file manager if the launch fails. |
| `NEXTROLE_DRILLS_DIR` | Override the sibling drills project location (default `../manual-code-drills`). |
| `NEXTROLE_BACKUP_DIR` | Where daily `data/` snapshots are written (default `data/backups/`, in-repo). Set to a path outside the repo so snapshots survive a full `data/` loss. |

### Sender allowlist

`data/email_config.json` is auto-created on first fetch with
`jobalerts-noreply@linkedin.com`. Extend it with any sender that emails
you job alerts:

```json
{ "senders": ["jobalerts-noreply@linkedin.com", "alerts@otta.com"] }
```

After each successful fetch, the script marks every harvested message
`\Seen` on the server (so re-fetches don't restage them) and records
Message-IDs in `data/email_state.json` as a second-line dedup safeguard.
Run `python scripts/linkedin_fetch.py --reset` to clear both layers and
re-fetch from scratch.

The sender allowlist applies to **LinkedIn ingest only** — the mailbox scan
(`inbox_scan.py`) ignores it and instead matches every recent inbox message
against your open applications by company name / sender domain. The scan tracks
its own processed-Message-ID list in `data/inbox_scan_state.json` (never the
server `\Seen` flag); `python scripts/inbox_scan.py --reset` clears it along
with the staged matches.

---

## 6. (Optional) Crawl configuration

`run.py --crawl` and `python scripts/crawl.py` run two-lane discovery
across the public job boards. The crawl reads its config from two files.

### `data/target_boards.json` — Lane 2 (direct ATS polling)

List of ATS boards the crawler polls directly. The file is empty by
default; entries get appended automatically as `crawl.py` and
`ingest.py` see apply URLs that match a known ATS. You can also seed it
by hand:

```json
[
  {"company": "Acme Data",   "ats": "greenhouse", "slug": "acmedata"},
  {"company": "Stream Co",   "ats": "lever",      "slug": "streamco"},
  {"company": "Pipeline Inc","ats": "ashby",      "slug": "pipelineinc"}
]
```

- `ats` is one of `greenhouse`, `lever`, or `ashby` for boards the
  crawler can actually fetch. `workday` and `smartrecruiters` are also
  recorded by `detect_ats` for visibility, but `crawl.py` skips them
  silently (no implementation).
- `slug` is the company identifier in the board URL (e.g.
  `boards.greenhouse.io/<slug>`).

Two helpers grow this list in bulk:

```bash
# Seed from companies already in job_pipeline.json
python scripts/backfill_target_boards.py --dry-run

# Scrape every careers page in company_registry.json for an embedded ATS
python scripts/discover_boards_from_careers.py --dry-run --verbose
```

See `ARCHITECTURE.md` for details on each script.

### `profile/stack_keywords.yaml → crawl:` — pre-filter + Lane 1 queries

The `crawl:` section of `stack_keywords.yaml` controls both the mechanical
pre-filter (run before any Claude call) and the aggregator queries:

| Key | Purpose |
|---|---|
| `seniority_titles` | List of terms; the job title must contain at least one. |
| `title_exclude` | List of terms; reject the title if any appears as a whole word (filters pre-sales / customer-success roles that slip past `architect` / `lead`). Whole-word match is letter-boundary-aware, so `intern` blocks `"Software Intern"` but not `"... International"`; multi-word terms like `solutions architect` work too. Variants that share a prefix need separate entries — e.g. list both `intern` and `internship`. |
| `location_allow` | List of regions; the location must contain at least one (or `remote`). This is the *positive* allowlist (first gate) and now includes US/region tokens (`united states`, `usa`, `north america`, `worldwide`, `anywhere`) so US-eligible remote roles aren't dropped before the US gate. A separate code-level gate (`config.location_passes`, driven by `TARGET_COUNTRIES`) then subtracts US rows when US is disabled or not remote — see "Targeting the US" below. |
| `aggregator_tags` | List of tag groups for the RemoteOK API. Each top-level item is one API call; tags within an inner list are AND-filtered (e.g. `- [kafka, java]`). For one-or-the-other alternates (technologies that don't co-occur), write them as separate items. |
| `aggregator_keywords` | List of Remotive full-text search queries. Each item is one query string (e.g. `- kafka flink java`). |
| `min_pre_filter_score` | Minimum stack-keyword score required before full ingest. |

The `keywords:` and `max_score:` sections of the same file drive the
mechanical stack score (`stack_match_score`) for every JD — used by both
ingest and the pre-filter. See `ARCHITECTURE.md` for the SSOT rules
around stack scoring.

### Targeting the US (optional, remote-only)

The pipeline targets **Canada** and **Ireland**, plus the **US** as an
optional, **remote-only stop-gap**. Active geographies are one constant in
`scripts/geography.py` (the location-SSOT module; US is **currently enabled**):

```python
TARGET_COUNTRIES: frozenset[str] = frozenset({"CA", "IE", "US"})  # remove "US" to disable
```

You also need US/region tokens in `location_allow` (in your gitignored
`profile/stack_keywords.yaml`) so US-eligible remote roles survive the first
gate — `united states`, `usa`, `north america`, `worldwide`, `anywhere`. Without
them, region-only locations like `"USA"`/`"Worldwide"` are dropped at
`location_allow` before the US gate ever runs. (These are already present in the
maintained profile.)

What US being enabled turns on (all keyed off `config.derive_country(location)`):

- **Remote-only intake.** Only remote US roles ingest; onsite/hybrid US is
  discarded by `config.location_passes` (applied in both pre-filters and at
  ingest). The remote check (`config.is_remote_role`) is **source-aware**: a
  region-only US location ("USA", "United States") counts as remote when it
  came from a remote-only board (RemoteOK / Remotive), but an ATS-board US role
  needs an explicit remote marker. With US off, US roles are excluded entirely.
  (Note: most crawl volume is the niche stack at Staff level, so geography is
  rarely the binding filter — `title_seniority` and `stack` drop the bulk.)
- **No-sponsorship JDs kept.** The ingest-time `detect_no_sponsorship` discard
  is skipped for US roles (you're a US citizen), so "we do not sponsor"
  boilerplate no longer throws the posting away. CA/IE still honor it.
- **Low US sponsorship score.** `composite_score` substitutes
  `US_SPONSORSHIP_SCORE` (default `3`/15) for the company sponsorship score on
  US roles, so CA/IE generally outrank US — tune it in `config.py` (set to `0`
  for "zero added from sponsorship"). A strong-stack US role can still beat a
  weak CA/IE one.
- **Cover-letter work authorization.** US cover letters get **no**
  work-authorization paragraph — a US citizen applying to a US role needs none.
  `run.py` omits `--country` for US jobs and `generate_cl.js` doesn't derive US,
  so the visa section is cleanly skipped. (CA/IE still get their locked
  paragraphs from `profile/cover_letter_rules.md`.)

Removing `"US"` reverts everything; CA/IE scoring is unchanged either way.

---

## Verification

After setup, run a dry crawl and a dashboard to verify everything is
wired up:

```bash
python run.py --crawl --dry-run
python run.py --dashboard
```

If the crawl prints a funnel breakdown without errors and the dashboard
prints "No jobs in pipeline" (because you haven't ingested any yet),
you're good to go. See `README.md` for the daily-operation flow from
here.
