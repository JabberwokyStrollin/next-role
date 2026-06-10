"""
serve.py — Local web UI for job ingestion.

Starts a lightweight web server on localhost:5000. Navigate to it in any
browser to ingest jobs without touching the command line.

Handles two cases:
  - Scrapeable pages (Greenhouse, Lever, most direct career pages):
    Paste the URL, hit Submit — server fetches and ingests automatically.
  - JS-rendered pages (Workday, Taleo, etc.):
    Server detects the fetch failed, shows a text box — paste the JD text
    manually, hit Submit — server ingests with the URL and pasted text.

Usage:
    python serve.py
    python serve.py --port 8080
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── stdout encoding (Windows cp1252 → UTF-8) ─────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import requests
from bs4 import BeautifulSoup

ROOT       = Path(__file__).parent.resolve()
SCRIPTS    = ROOT / "scripts"
DATA_DIR   = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

# Ranking-related symbols come from the scoring SSOT (scripts/config.py).
# Do not redefine composite_score, composite_score_pre_research, score
# weights, or denominators in this file.
sys.path.insert(0, str(SCRIPTS))
from config import (  # noqa: E402
    COMPONENTS,
    COMPOSITE_MAX,
    PRE_RESEARCH_MAX,
    MAX_ACTIVE_APPS_PER_COMPANY,
    company_block_reason,
    composite_score,
    composite_score_pre_research,
)

# Mirrors scripts/config.py. Duplicated to keep serve.py importable without
# the ANTHROPIC_API_KEY check that config.py runs at import time.
APPLICATION_TRACKER_PATH = DATA_DIR / "application_tracker.json"
GHOSTED_DAYS             = 21


def load_applications() -> list:
    if not APPLICATION_TRACKER_PATH.exists():
        return []
    try:
        return json.loads(APPLICATION_TRACKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_applications(apps: list) -> None:
    APPLICATION_TRACKER_PATH.write_text(
        json.dumps(apps, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def days_since_iso(iso_date: str) -> int:
    return (date.today() - date.fromisoformat(iso_date)).days

MIN_JD_LENGTH = 200

# ── Daily checklist sections ──────────────────────────────────────────────────

DAILY_CHECKLIST_PATH = DATA_DIR / "daily_checklist.json"
EMAIL_STAGED_PATH    = DATA_DIR / "email_staged.json"

CHECKLIST_SECTIONS = [
    ("status_updates",  "Status updates",
     "Update outcomes on jobs you've already applied to "
     "(rejections, interview requests, position-filled letters)."),
    ("crawl",           "Crawl job boards",
     "Run the crawler across configured aggregators (RemoteOK, Remotive) "
     "and ATS sources (Greenhouse, Lever, Ashby)."),
    ("linkedin_ingest", "LinkedIn alert ingest",
     "Pull LinkedIn job-alert emails from your inbox and ingest the postings."),
    ("cover_letters",   "Cover letters & apply",
     "Generate cover letters for top-scoring jobs and log applications "
     "as you submit them."),
]

# ── JD fetch (mirrors ingest.py logic) ───────────────────────────────────────

def fetch_jd(url: str) -> tuple[str, bool]:
    """
    Attempt to fetch JD text from URL.
    Returns (jd_text, success). success=False means JS rendering required.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return "", False
        if "/uas/login" in resp.url or "login?session_redirect" in resp.url:
            return "", False

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        jd_text = ""
        for selector in [
            "div.description__text--rich",
            "section.show-more-less-html",
            "[data-qa='job-description']",
            "#content",
            ".job-description",
            ".job__description",
            ".description",
            "main",
            "article",
        ]:
            el = soup.select_one(selector)
            if el:
                jd_text = el.get_text(separator="\n", strip=True)
                if len(jd_text) >= MIN_JD_LENGTH:
                    break

        if len(jd_text) < MIN_JD_LENGTH:
            jd_text = soup.get_text(separator="\n", strip=True)

        lines   = [l.strip() for l in jd_text.splitlines() if l.strip()]
        jd_text = "\n".join(lines)

        if len(jd_text) < MIN_JD_LENGTH:
            return "", False

        return jd_text, True

    except Exception:
        return "", False


def run_ingest(
    apply_url: str, company: str, title: str,
    location: str, jd_text: str, posted: str
) -> tuple[bool, str]:
    """
    Write JD to a temp file and call ingest.py --paste mode via subprocess.
    Returns (success, output_text).
    """
    tmp = ROOT / "data" / f"_tmp_jd_{uuid.uuid4().hex[:8]}.txt"
    try:
        tmp.write_text(jd_text, encoding="utf-8")
        cmd = [
            sys.executable, str(SCRIPTS / "ingest.py"),
            "--paste",
            "--company",   company,
            "--title",     title,
            "--location",  location,
            "--apply-url", apply_url,
        ]
        if posted:
            cmd += ["--posted", posted]

        result = subprocess.run(
            cmd, input=jd_text, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace"
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output
    finally:
        if tmp.exists():
            tmp.unlink()


def load_pipeline():
    p = DATA_DIR / "job_pipeline.json"
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        raw = raw.encode("utf-8", errors="ignore").decode("utf-8")
        return json.loads(raw)
    except Exception:
        return []


def load_comp_estimates_by_job() -> dict:
    """Read data/comp_estimates.json once and index by job_id."""
    p = DATA_DIR / "comp_estimates.json"
    if not p.exists():
        return {}
    try:
        records = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return {r["job_id"]: r for r in records if r.get("job_id")}


def load_companies_by_id() -> dict:
    """Read company_registry.json once per page render and index by company_id.

    composite_score() needs the company record to apply sponsorship + remote
    weights. Callers should join job → company via job['company_id'].
    """
    p = DATA_DIR / "company_registry.json"
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        raw = raw.encode("utf-8", errors="ignore").decode("utf-8")
        return {c["company_id"]: c for c in json.loads(raw)}
    except Exception:
        return {}


def job_score(job: dict, co_by_id: dict) -> int:
    """Convenience: full composite for a single job using a preloaded company map."""
    return composite_score(job, co_by_id.get(job.get("company_id")))


def load_daily_state(date_iso: str) -> dict:
    """Return today's checklist state ({section_id: bool}); {} if no entry."""
    if not DAILY_CHECKLIST_PATH.exists():
        return {}
    try:
        all_state = json.loads(DAILY_CHECKLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return all_state.get(date_iso, {})


def save_daily_state(date_iso: str, state: dict) -> None:
    """Merge today's state into the keyed-by-date checklist file."""
    if DAILY_CHECKLIST_PATH.exists():
        try:
            all_state = json.loads(DAILY_CHECKLIST_PATH.read_text(encoding="utf-8"))
        except Exception:
            all_state = {}
    else:
        all_state = {}
    all_state[date_iso] = state
    DAILY_CHECKLIST_PATH.write_text(
        json.dumps(all_state, indent=2), encoding="utf-8"
    )


def apply_ghosted_check() -> None:
    """Auto-flip 'applied' apps to 'ghosted' once they pass GHOSTED_DAYS without a
    response. Mirrors scripts/update_status.py cmd_list side effect so the web view
    stays in sync with the CLI."""
    apps = load_applications()
    if not apps:
        return
    terminal = {"offer", "rejected", "withdrawn", "recruiter_screen", "interview"}
    updated  = False
    for app in apps:
        if app.get("response_date") or app.get("status") in terminal:
            continue
        applied = app.get("date_applied")
        if not applied:
            continue
        is_ghosted = days_since_iso(applied) > GHOSTED_DAYS
        if is_ghosted != app.get("ghosted_flag", False):
            app["ghosted_flag"] = is_ghosted
            if is_ghosted and app.get("status") == "applied":
                app["status"] = "ghosted"
            updated = True
    if updated:
        save_applications(apps)


# ── Status-update action map (button value → update_status.py args) ─────────-

STATUS_ACTION_MAP = {
    "recruiter_screen":         ("recruiter_screen", ""),
    "interview":                ("interview",        ""),
    "offer":                    ("offer",            ""),
    "rejected_generic":         ("rejected",         "Generic rejection"),
    "rejected_position_filled": ("rejected",         "Position filled"),
    "withdrawn":                ("withdrawn",        ""),
}


# ── Crawl background runner ──────────────────────────────────────────────────-

CRAWL_TAIL_MAX  = 50
INGESTED_RE     = re.compile(r"Ingested:\s+(\d+)")
crawl_state_lk  = threading.Lock()
crawl_state     = {
    "state":       "idle",   # idle | running | done | error
    "started_at":  None,
    "ended_at":    None,
    "returncode":  None,
    "ingested":    None,
    "output_tail": [],
    "error":       None,
}


def _crawl_worker() -> None:
    """Run scripts/crawl.py and stream output into crawl_state."""
    cmd = [sys.executable, "-u", str(SCRIPTS / "crawl.py")]
    try:
        proc = subprocess.Popen(
            cmd, cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            bufsize=1,
        )
    except Exception as e:
        with crawl_state_lk:
            crawl_state["state"]    = "error"
            crawl_state["error"]    = f"Failed to launch crawl.py: {e}"
            crawl_state["ended_at"] = time.time()
        return

    last_ingested = None
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            with crawl_state_lk:
                tail = crawl_state["output_tail"]
                tail.append(line)
                if len(tail) > CRAWL_TAIL_MAX:
                    del tail[: len(tail) - CRAWL_TAIL_MAX]
            m = INGESTED_RE.search(line)
            if m:
                last_ingested = int(m.group(1))
    except Exception as e:
        with crawl_state_lk:
            crawl_state["error"] = f"Stream read error: {e}"

    rc = proc.wait()
    with crawl_state_lk:
        crawl_state["returncode"] = rc
        crawl_state["ended_at"]   = time.time()
        crawl_state["ingested"]   = last_ingested if last_ingested is not None else 0
        if rc == 0:
            crawl_state["state"]  = "done"
        else:
            crawl_state["state"]  = "error"
            if not crawl_state["error"]:
                crawl_state["error"] = f"crawl.py exited with code {rc}"


def start_crawl() -> bool:
    """Kick off a background crawl. Returns False if one is already running."""
    with crawl_state_lk:
        if crawl_state["state"] == "running":
            return False
        crawl_state.update({
            "state":       "running",
            "started_at":  time.time(),
            "ended_at":    None,
            "returncode":  None,
            "ingested":    None,
            "output_tail": [],
            "error":       None,
        })
    threading.Thread(target=_crawl_worker, daemon=True).start()
    return True


def crawl_status_payload() -> dict:
    with crawl_state_lk:
        s        = crawl_state
        elapsed  = None
        if s["started_at"] is not None:
            end     = s["ended_at"] if s["ended_at"] is not None else time.time()
            elapsed = int(end - s["started_at"])
        return {
            "state":     s["state"],
            "elapsed_s": elapsed,
            "ingested":  s["ingested"],
            "tail":      list(s["output_tail"][-8:]),
            "error":     s["error"],
        }


# ── LinkedIn ingest staging ──────────────────────────────────────────────────-

LINKEDIN_REQUIRED_ENV = (
    "NEXTROLE_IMAP_HOST",
    "NEXTROLE_IMAP_USER",
    "NEXTROLE_IMAP_APP_PASSWORD",
)

# One-shot flash message displayed on the next /today render.
_linkedin_flash: dict | None = None


def linkedin_env_missing() -> list[str]:
    return [n for n in LINKEDIN_REQUIRED_ENV if not os.environ.get(n, "").strip()]


def load_staged_emails() -> list[dict]:
    if not EMAIL_STAGED_PATH.exists():
        return []
    try:
        return json.loads(EMAIL_STAGED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_staged_emails(rows: list[dict]) -> None:
    EMAIL_STAGED_PATH.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def remove_staged(staging_id: str) -> dict | None:
    rows = load_staged_emails()
    target, kept = None, []
    for r in rows:
        if r.get("staging_id") == staging_id:
            target = r
        else:
            kept.append(r)
    if target is not None:
        save_staged_emails(kept)
    return target


def set_linkedin_flash(kind: str, text: str) -> None:
    """kind: 'ok' | 'warn' | 'info'."""
    global _linkedin_flash
    _linkedin_flash = {"kind": kind, "text": text}


def pop_linkedin_flash() -> dict | None:
    global _linkedin_flash
    f, _linkedin_flash = _linkedin_flash, None
    return f


# ── Cover letters & apply (phase 5) ───────────────────────────────────────────-

CL_RENDER_CAP = 30  # rows visible in the cover_letters section by default

_cl_flash: dict | None = None


def set_cl_flash(kind: str, text: str) -> None:
    """kind: 'ok' | 'warn' | 'info'."""
    global _cl_flash
    _cl_flash = {"kind": kind, "text": text}


def pop_cl_flash() -> dict | None:
    global _cl_flash
    f, _cl_flash = _cl_flash, None
    return f


_research_flash: dict | None = None


def set_research_flash(kind: str, text: str) -> None:
    """One-shot flash for 'Research now' actions on stub companies. Surfaces
    on whichever page the user was returned to (cover-letters body or
    /job/<id>)."""
    global _research_flash
    _research_flash = {"kind": kind, "text": text}


def pop_research_flash() -> dict | None:
    global _research_flash
    f, _research_flash = _research_flash, None
    return f


def _flash_notice_html(flash: dict | None) -> str:
    """Render a popped flash dict as a .notice div, or empty string."""
    if not flash:
        return ""
    kind = flash.get("kind", "info")
    cls  = "notice-warn" if kind == "warn" else ("notice-ok" if kind == "ok" else "notice-info")
    from html import escape as esc
    return f'<div class="notice {cls}">{esc(flash.get("text", ""))}</div>'


def run_company_research(company_id: str) -> tuple[bool, str]:
    """Shell out to scripts/research_company.py --company-id <id>, then strip
    the stub flag on success (mirrors run.py:_execute_research). Returns
    (ok, message) where message is a short summary suitable for a flash.

    Blocks the request thread for the duration of the research call (Haiku
    + 1 web search, typically 20-30s). Timeout is 120s to match comp_estimate.
    """
    company_registry_path = DATA_DIR / "company_registry.json"
    try:
        companies = json.loads(company_registry_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"Could not read company registry: {e}"

    pre = next((c for c in companies if c.get("company_id") == company_id), None)
    if not pre:
        return False, f"Unknown company_id: {company_id}"
    pre_name = pre.get("name", "")

    cmd = [sys.executable, "-u", str(SCRIPTS / "research_company.py"),
           "--company-id", company_id]
    try:
        result = subprocess.run(
            cmd, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "Research timed out after 120s."
    except Exception as e:  # noqa: BLE001
        return False, f"Failed to launch research_company.py: {e}"

    if result.returncode != 0:
        tail = "; ".join(
            [l for l in (result.stdout or "").splitlines() if l.strip()][-3:]
        )
        return False, f"Research failed — {tail or 'see server log'}"

    # research_company.py exits 0 even when it just prints "Error: company ID
    # X not found." — guard against that by checking the registry state.
    try:
        companies = json.loads(company_registry_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"Researched ok but couldn't re-read registry: {e}"

    post = next((c for c in companies if c.get("company_id") == company_id), None)
    if not post:
        return False, f"Research call exited 0 but {pre_name} is no longer in the registry."
    if post.get("stub") and post.get("record_updated") == pre.get("record_updated"):
        return False, (
            f"Research did not update {pre_name} — check server log "
            f"(stdout tail: {(result.stdout or '').strip().splitlines()[-1][:120]})"
        )

    # Strip the stub flag (same logic as run.py:_execute_research).
    post.pop("stub", None)
    company_registry_path.write_text(
        json.dumps(companies, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return True, f"Researched {pre_name}."


def run_linkedin_fetch() -> tuple[bool, int, str]:
    """Run scripts/linkedin_fetch.py. Returns (ok, n_fetched, output)."""
    cmd = [sys.executable, "-u", str(SCRIPTS / "linkedin_fetch.py")]
    try:
        result = subprocess.run(
            cmd, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, 0, "Fetch timed out after 300s."
    except Exception as e:
        return False, 0, f"Failed to launch fetch: {e}"

    output = result.stdout or ""
    n      = 0
    for line in reversed(output.splitlines()):
        m = re.match(r"FETCHED:\s+(\d+)", line.strip())
        if m:
            n = int(m.group(1))
            break
    return result.returncode == 0, n, output


def run_linkedin_prefilter() -> tuple[bool, int, int, str]:
    """Run scripts/prefilter_staged.py. Returns (ok, n_passed, n_failed, output)."""
    cmd = [sys.executable, "-u", str(SCRIPTS / "prefilter_staged.py")]
    try:
        result = subprocess.run(
            cmd, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, 0, 0, "Pre-filter timed out after 60s."
    except Exception as e:
        return False, 0, 0, f"Failed to launch pre-filter: {e}"

    output = result.stdout or ""
    passed, failed = 0, 0
    for line in reversed(output.splitlines()):
        m = re.match(r"PREFILTER:\s+passed=(\d+)\s+failed=(\d+)", line.strip())
        if m:
            passed = int(m.group(1))
            failed = int(m.group(2))
            break
    return result.returncode == 0, passed, failed, output


def discard_failing_staged() -> int:
    """Remove staged rows where _prefilter_pass is False. Returns count discarded."""
    rows = load_staged_emails()
    kept = [r for r in rows if r.get("_prefilter_pass", True)]
    n_discarded = len(rows) - len(kept)
    if n_discarded:
        save_staged_emails(kept)
    return n_discarded


# ── Resume snippets (Experience & Education) ─────────────────────────────────-

RESUME_MD_PATH = ROOT / "profile" / "resume.md"

PROFILE_LINKS = [
    ("LinkedIn", "linkedin.com/in/johnny-blanton"),
    ("GitHub",   "github.com/JabberwokyStrollin"),
]

_MONTH_ABBREVS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

# Date range like "Jan 2020 – Mar 2026", "Mar 2026 – Present", with en/em/hyphen.
_DATE_RANGE_RE = re.compile(
    r"(?P<m1>[A-Za-z]+)\s+(?P<y1>\d{4})"
    r"\s*[–—\-]\s*"
    r"(?P<m2>[A-Za-z]+)\s*(?P<y2>\d{4})?"
)


def _to_mm_yyyy(month_name: str, year: str) -> str:
    if not month_name or not year:
        return ""
    mm = _MONTH_ABBREVS.get(month_name.strip().lower()[:3], "")
    return f"{mm}/{year}" if mm else f"{month_name} {year}"


def _split_date_range(text: str) -> tuple[str, str]:
    m = _DATE_RANGE_RE.search(text or "")
    if not m:
        return "", ""
    frm = _to_mm_yyyy(m.group("m1"), m.group("y1"))
    m2  = m.group("m2") or ""
    if m2.lower().startswith("present"):
        to = "Present"
    else:
        to = _to_mm_yyyy(m2, m.group("y2") or "")
    return frm, to


def _section_block(md: str, heading: str) -> str:
    """Body of a top-level '## {heading}' section, up to the next '## '."""
    pattern = re.compile(
        r"^##\s+" + re.escape(heading) + r"\s*\n(.*?)(?=^##\s)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(md + "\n## ")  # trailing sentinel so the last section terminates
    return m.group(1) if m else ""


def _split_title_company(head: str) -> tuple[str, str]:
    for sep in ("—", "–", " - "):
        pat = r"^(.+?)\s+" + re.escape(sep.strip()) + r"\s+(.+)$"
        m   = re.match(pat, head)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return head.strip(), ""


def _coalesce_description(body_lines: list[str]) -> str:
    """
    Join soft-wrapped paragraphs and bullets into single lines. Preserves blank
    lines between bullets/paragraphs. Drops '---' horizontal rules.
    """
    cleaned = [l for l in body_lines if l.strip() != "---"]
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    coalesced: list[str] = []
    for ln in cleaned:
        stripped = ln.strip()
        if not stripped:
            coalesced.append("")
            continue
        if stripped.startswith("- "):
            coalesced.append(stripped)
        elif coalesced and coalesced[-1]:
            coalesced[-1] += " " + stripped
        else:
            coalesced.append(stripped)

    # Collapse runs of blank lines.
    out: list[str] = []
    prev_blank = False
    for ln in coalesced:
        if ln == "":
            if not prev_blank and out:
                out.append("")
            prev_blank = True
        else:
            out.append(ln)
            prev_blank = False
    return "\n".join(out).strip()


def parse_experience(md: str) -> list[dict]:
    block = _section_block(md, "Experience")
    if not block:
        return []
    entries = re.split(r"(?m)^### ", block)
    out: list[dict] = []
    for raw in entries:
        raw = raw.strip()
        if not raw:
            continue
        lines = raw.splitlines()
        title, company = _split_title_company(lines[0].strip())

        date_line  = ""
        body_start = 1
        for i in range(1, len(lines)):
            ln = lines[i].strip()
            if not ln:
                continue
            if "|" in ln and _DATE_RANGE_RE.search(ln):
                date_line  = ln
                body_start = i + 1
                break

        frm, to, location = "", "", ""
        if date_line:
            parts   = [p.strip() for p in date_line.split("|", 1)]
            frm, to = _split_date_range(parts[0])
            if len(parts) > 1:
                location = parts[1]

        description = _coalesce_description(lines[body_start:])

        out.append({
            "title":       title,
            "company":     company,
            "location":    location,
            "from":        frm,
            "to":          to,
            "description": description,
        })
    return out


_STATE_AT_END_RE = re.compile(r"\b[A-Z]{2,3}\s*$")


def parse_education(md: str) -> list[dict]:
    block = _section_block(md, "Education")
    if not block:
        return []

    # Bullets may wrap across lines. Continuations are indented or non-empty
    # non-bullet lines until the next '- ' or blank line.
    entries: list[str] = []
    current: str | None = None
    for line in block.splitlines():
        if line.startswith("- "):
            if current is not None:
                entries.append(current)
            current = line[2:].rstrip()
        elif current is not None and (line.startswith(" ") or line.startswith("\t")):
            current += " " + line.strip()
        elif current is not None and line.strip() == "":
            entries.append(current)
            current = None
    if current is not None:
        entries.append(current)

    out: list[dict] = []
    for entry in entries:
        segments = [s.strip() for s in entry.split("|") if s.strip()]
        if not segments:
            continue

        date_idx = -1
        for i, s in enumerate(segments):
            if _DATE_RANGE_RE.search(s):
                date_idx = i
                break

        frm, to = ("", "")
        if date_idx >= 0:
            frm, to = _split_date_range(segments[date_idx])

        # Institution: nearest segment before the date that ends with a state
        # code, else the segment immediately before the date.
        inst_idx = -1
        if date_idx > 0:
            for i in range(date_idx - 1, -1, -1):
                if _STATE_AT_END_RE.search(segments[i]):
                    inst_idx = i
                    break
            if inst_idx == -1:
                inst_idx = date_idx - 1

        institution, location = "", ""
        if inst_idx >= 0:
            inst_full = segments[inst_idx]
            m = re.match(r"^(.+),\s*([^,]+)$", inst_full)
            if m and _STATE_AT_END_RE.search(m.group(2)):
                institution = m.group(1).strip()
                location    = m.group(2).strip()
            else:
                institution = inst_full

        degree_end = inst_idx if inst_idx >= 0 else (
            date_idx if date_idx >= 0 else len(segments)
        )
        degree_parts = list(segments[:degree_end])
        if date_idx >= 0 and date_idx + 1 < len(segments):
            degree_parts.extend(segments[date_idx + 1:])
        degree = " | ".join(degree_parts).strip()

        out.append({
            "degree":      degree,
            "institution": institution,
            "location":    location,
            "from":        frm,
            "to":          to,
        })
    return out


def parse_resume_snippets() -> dict:
    if not RESUME_MD_PATH.exists():
        return {
            "experience": [],
            "education":  [],
            "error":      "profile/resume.md not found.",
        }
    try:
        md = RESUME_MD_PATH.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "experience": [],
            "education":  [],
            "error":      f"Failed to read resume.md: {e}",
        }
    return {
        "experience": parse_experience(md),
        "education":  parse_education(md),
    }


_SCRIPTS_ON_PATH = False


def fetch_jd_for_staged(staging_id: str) -> tuple[bool, str]:
    """
    On-demand single-URL JD fetch for one staged row. Updates the row's
    jd_text and persists. Returns (ok, message) for surfacing as a flash.

    Imported lazily so a server start doesn't pull bs4 twice.
    """
    rows   = load_staged_emails()
    target = next((r for r in rows if r.get("staging_id") == staging_id), None)
    if not target:
        return False, "Row not found — already ingested or discarded?"
    if target.get("jd_text") and len(target["jd_text"]) >= MIN_JD_LENGTH:
        return True, "JD already populated."

    url = target.get("apply_url", "").strip()
    if not url:
        return False, "Row has no apply URL."

    global _SCRIPTS_ON_PATH
    if not _SCRIPTS_ON_PATH:
        sys.path.insert(0, str(SCRIPTS))
        _SCRIPTS_ON_PATH = True
    from linkedin_fetch import _fetch_jd_text  # noqa: WPS433

    text, ok, reason = _fetch_jd_text(url)
    if ok:
        target["jd_text"] = text
        save_staged_emails(rows)
        return True, f"Fetched {len(text)} chars."

    if reason == "auth_wall":
        return False, (
            "LinkedIn returned a login page — likely IP-throttled. "
            "Wait a few hours and retry, or open the URL and paste manually."
        )
    if reason == "expired":
        return False, (
            "LinkedIn redirected to a similar-jobs page — posting is likely "
            "expired. Open the apply URL to confirm, then discard the row."
        )
    if reason == "short":
        return False, "Page loaded but JD body was too short to use. Paste manually."
    if reason == "http_error":
        return False, "HTTP error from LinkedIn. Try again, or paste manually."
    if reason == "exception":
        return False, "Network/timeout error. Try again, or paste manually."
    return False, f"Fetch failed ({reason})."


# ── HTML templates ────────────────────────────────────────────────────────────

STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f3; color: #1a1a18; font-size: 14px; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 32px 20px; }
  h1 { font-size: 20px; font-weight: 500; color: #1F3864; margin-bottom: 4px; }
  .sub { color: #888; font-size: 12px; margin-bottom: 28px; }
  .nav-bar { display: flex; align-items: center; gap: 16px;
             margin-bottom: 28px; flex-wrap: wrap; }
  .nav-bar .sub { flex: 1; min-width: 260px; }
  .nav-search { display: flex; align-items: center; gap: 6px;
                margin: 0; }
  .nav-search input[type=search] {
    width: 220px; padding: 5px 8px; font-size: 12px;
    border: 0.5px solid rgba(0,0,0,0.2); border-radius: 5px;
    background: #fff; font-family: inherit;
  }
  .nav-search .btn-mini { margin: 0; }
  .card { background: #fff; border: 0.5px solid rgba(0,0,0,0.12);
          border-radius: 10px; padding: 24px; margin-bottom: 20px; }
  label { display: block; font-size: 11px; font-weight: 500;
          color: #666; margin-bottom: 4px; margin-top: 14px; }
  label:first-child { margin-top: 0; }
  input, textarea, select {
    width: 100%; padding: 8px 10px; font-size: 13px;
    border: 0.5px solid rgba(0,0,0,0.2); border-radius: 6px;
    background: #fff; color: #1a1a18; font-family: inherit;
  }
  textarea { min-height: 160px; resize: vertical; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .btn { display: inline-block; padding: 9px 20px; font-size: 13px;
         font-weight: 500; border-radius: 6px; border: none;
         cursor: pointer; margin-top: 16px; }
  .btn-primary { background: #2E75B6; color: #fff; }
  .btn-primary:hover { background: #245f99; }
  .btn-secondary { background: #f0f0ee; color: #333; margin-left: 8px; }
  .notice { padding: 10px 14px; border-radius: 8px; font-size: 12px;
            margin-bottom: 16px; }
  .notice-warn { background: #fef3cd; color: #7a4f00;
                 border: 0.5px solid rgba(200,150,50,0.3); }
  .notice-info { background: #e6f1fb; color: #0c447c;
                 border: 0.5px solid rgba(46,117,182,0.3); }
  .notice-ok   { background: #e6f4ec; color: #1a5c2e;
                 border: 0.5px solid rgba(26,92,46,0.3); }
  .pipeline { margin-top: 8px; }
  .job-row { display: flex; align-items: center; gap: 12px;
             padding: 8px 0; border-bottom: 0.5px solid rgba(0,0,0,0.08); }
  .job-row:last-child { border-bottom: none; }
  .score { font-variant-numeric: tabular-nums; font-weight: 500;
           min-width: 36px; color: #2E75B6; }
  .company { font-weight: 500; min-width: 140px; }
  .title-cell { color: #555; flex: 1; }
  .apply-link { font-size: 11px; color: #2E75B6; text-decoration: none; }
  .apply-link:hover { text-decoration: underline; }
  pre { background: #f8f8f6; border-radius: 6px; padding: 12px;
        font-size: 11px; overflow-x: auto; white-space: pre-wrap;
        color: #333; margin-top: 12px; max-height: 200px; overflow-y: auto; }
  h2 { font-size: 15px; font-weight: 500; margin-bottom: 12px; color: #1a1a18; }
  .section-label { font-size: 11px; font-weight: 500; text-transform: uppercase;
                   letter-spacing: 0.06em; color: #888; margin-bottom: 8px; }
  .checklist-summary { color: #444; font-size: 13px; margin-bottom: 18px;
                       display: flex; align-items: center; gap: 12px; }
  .progress-bar { display: inline-block; flex: 1; max-width: 200px;
                  height: 6px; background: #f0f0ee; border-radius: 3px;
                  overflow: hidden; }
  .progress-fill { display: block; height: 100%; background: #2E75B6; }
  .checklist-section { background: #fff; border: 0.5px solid rgba(0,0,0,0.12);
                       border-radius: 10px; padding: 14px 20px;
                       margin-bottom: 10px; }
  .checklist-section[open] { padding-bottom: 20px; }
  .checklist-section summary { cursor: pointer; list-style: none;
                               display: flex; align-items: center; gap: 12px;
                               padding: 2px 0; font-size: 14px; font-weight: 500; }
  .checklist-section summary::-webkit-details-marker { display: none; }
  .section-badge { display: inline-flex; align-items: center;
                   justify-content: center; width: 22px; height: 22px;
                   border-radius: 50%; font-size: 12px; font-weight: 500;
                   flex-shrink: 0; }
  .badge-done   { background: #1a5c2e; color: #fff; }
  .badge-undone { background: #f0f0ee; color: #999;
                  border: 0.5px solid rgba(0,0,0,0.15); }
  .section-title { color: #1a1a18; flex: 1; }
  .section-hint { color: #666; font-size: 12px; margin-top: 0;
                  margin-bottom: 4px; }
  .section-placeholder { color: #aaa; font-size: 11px; font-style: italic;
                         margin: 8px 0 14px 0; }
  .checklist-section .section-body { margin-top: 14px; padding-top: 12px;
                                     border-top: 0.5px solid rgba(0,0,0,0.08); }
  .app-list { margin-top: 4px; }
  .app-row { padding: 12px 0;
             border-bottom: 0.5px solid rgba(0,0,0,0.08); }
  .app-row:last-child { border-bottom: none; }
  .app-meta { display: flex; align-items: center; gap: 12px;
              font-size: 13px; flex-wrap: wrap; }
  .app-company { font-weight: 500; min-width: 120px; }
  .app-title   { color: #555; flex: 1; min-width: 200px; }
  .app-applied { color: #888; font-size: 11px; white-space: nowrap; }
  .app-status  { font-size: 10px; padding: 2px 8px; border-radius: 4px;
                 text-transform: uppercase; letter-spacing: 0.04em;
                 font-weight: 500; white-space: nowrap; }
  .app-status-applied          { background: #e6f1fb; color: #0c447c; }
  .app-status-recruiter_screen { background: #fef3cd; color: #7a4f00; }
  .app-status-interview        { background: #d4edda; color: #155724; }
  .app-status-ghosted          { background: #f0f0ee; color: #888; }
  .app-buttons { display: flex; flex-wrap: wrap; gap: 6px;
                 margin: 8px 0 0 0; padding: 0; }
  .btn-status  { font-size: 11px; padding: 4px 10px; background: #f0f0ee;
                 color: #333; border: 0.5px solid rgba(0,0,0,0.12);
                 border-radius: 4px; cursor: pointer; font-family: inherit; }
  .btn-status:hover { background: #e8e8e6; }
  .crawl-status   { display: flex; align-items: center; gap: 10px;
                    flex-wrap: wrap; margin-bottom: 8px; }
  .crawl-badge    { font-size: 11px; padding: 3px 9px; border-radius: 4px;
                    text-transform: uppercase; letter-spacing: 0.04em;
                    font-weight: 500; }
  .crawl-badge-running { background: #fef3cd; color: #7a4f00; }
  .crawl-badge-done    { background: #d4edda; color: #155724; }
  .crawl-badge-error   { background: #f8d7da; color: #721c24; }
  .crawl-elapsed  { color: #888; font-size: 11px; font-variant-numeric: tabular-nums; }
  .crawl-tail     { font-size: 11px; max-height: 160px; }
  .staged-list    { margin-top: 10px; }
  .staged-row     { background: #fafaf8; border: 0.5px solid rgba(0,0,0,0.1);
                    border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }
  .staged-grid    { display: grid; grid-template-columns: 1fr 1fr;
                    gap: 8px 12px; margin-bottom: 6px; }
  .staged-form label { font-size: 10.5px; }
  .staged-form input, .staged-form textarea { font-size: 12px; padding: 6px 8px; }
  .staged-form textarea { min-height: 90px; }
  .staged-actions { display: flex; align-items: center; gap: 8px;
                    margin-top: 10px; flex-wrap: wrap; }
  .staged-source  { color: #888; font-size: 11px; margin-left: auto;
                    overflow: hidden; text-overflow: ellipsis;
                    white-space: nowrap; max-width: 50%; }
  .jd-status      { font-size: 10px; padding: 1px 6px; border-radius: 3px;
                    text-transform: none; letter-spacing: 0;
                    font-weight: 400; margin-left: 6px; }
  .jd-ok          { background: #d4edda; color: #155724; }
  .jd-empty       { background: #fef3cd; color: #7a4f00; }
  .linkedin-actions { display: flex; gap: 8px; align-items: center;
                      margin-bottom: 4px; flex-wrap: wrap; }
  .staged-summary { display: flex; align-items: center; gap: 8px;
                    flex-wrap: wrap; margin: 14px 0 8px;
                    color: #555; font-size: 12px; }
  .staged-count   { color: #555; }
  .staged-summary a { color: #2E75B6; text-decoration: none; }
  .staged-summary a:hover { text-decoration: underline; }
  .staged-row-head { display: flex; gap: 8px; align-items: center;
                     margin-bottom: 6px; min-height: 16px; }
  .pf-badge       { font-size: 10.5px; padding: 2px 7px; border-radius: 3px;
                    font-weight: 500; }
  .pf-pass        { background: #d4edda; color: #155724; }
  .pf-fail        { background: #f8d7da; color: #721c24; }
  .btn-mini       { font-size: 11px; padding: 5px 12px; background: #f0f0ee;
                    color: #333; border: 0.5px solid rgba(0,0,0,0.15);
                    border-radius: 4px; cursor: pointer; font-family: inherit; }
  .btn-mini:hover { background: #e8e8e6; }
  .btn-mini[disabled] { opacity: 0.5; cursor: wait; }
  .cl-list        { margin-top: 10px; }
  .cl-row         { background: #fafaf8; border: 0.5px solid rgba(0,0,0,0.1);
                    border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
  .cl-meta        { display: flex; align-items: center; gap: 10px;
                    font-size: 13px; flex-wrap: wrap; }
  .cl-meta .score { color: #2E75B6; font-weight: 500; min-width: 30px;
                    font-variant-numeric: tabular-nums; }
  .cl-meta .score-suffix { color: #888; font-weight: 400; font-size: 10px;
                           margin-left: 1px; }
  .cl-meta .score-secondary { color: #888; font-size: 10.5px; font-weight: 400;
                              font-variant-numeric: tabular-nums; cursor: help; }
  .pf-stub        { background: #fef3cd; color: #7a4f00; cursor: help; }
  .cl-meta .company { font-weight: 500; min-width: 130px; }
  .cl-meta .title-cell { color: #555; flex: 1; min-width: 220px; }
  .cl-location    { color: #888; font-size: 11px; }
  .cl-file-line   { font-size: 11px; color: #1a5c2e; margin-top: 6px;
                    font-family: ui-monospace, Consolas, monospace; }
  .cl-file-name   { background: #e6f4ec; padding: 2px 6px; border-radius: 3px; }
  .cl-breakdown   { font-size: 11px; color: #666; margin-top: 6px;
                    font-variant-numeric: tabular-nums; }
  .cl-breakdown strong { color: #1a1a18; font-weight: 500; }
  .cl-notes       { margin-top: 4px; }
  .cl-notes summary { font-size: 11px; color: #2E75B6; cursor: pointer;
                      list-style: none; padding: 2px 0; user-select: none; }
  .cl-notes summary::-webkit-details-marker { display: none; }
  .cl-notes summary::before { content: '▸ '; color: #888; }
  .cl-notes[open] summary::before { content: '▾ '; }
  .cl-notes-body  { font-size: 12px; color: #444; line-height: 1.45;
                    padding: 6px 10px; margin-top: 4px;
                    background: #f8f8f6; border-radius: 6px;
                    border-left: 2px solid #2E75B6; }
  .cl-actions     { display: flex; align-items: center; gap: 6px;
                    margin-top: 10px; flex-wrap: wrap; }
  .cl-actions .apply-link { font-size: 11px; }
  .snippet-entry  { background: #fafaf8; border: 0.5px solid rgba(0,0,0,0.1);
                    border-radius: 8px; padding: 0; margin-bottom: 10px; }
  .snippet-entry summary { padding: 12px 16px; cursor: pointer;
                           display: flex; align-items: center; gap: 8px;
                           list-style: none; font-size: 13px; flex-wrap: wrap; }
  .snippet-entry summary::-webkit-details-marker { display: none; }
  .snippet-entry summary::before { content: '▸'; color: #888; margin-right: 2px; }
  .snippet-entry[open] summary::before { content: '▾'; }
  .snippet-entry summary strong { font-weight: 500; color: #1a1a18; }
  .snippet-entry summary .snippet-meta { color: #888; font-size: 11px; }
  .snippet-fields { padding: 4px 16px 14px;
                    border-top: 0.5px solid rgba(0,0,0,0.06); }
  .snippet-field  { display: flex; align-items: flex-start; gap: 8px;
                    margin-top: 10px; }
  .snippet-field-col { flex: 1; min-width: 0; }
  .snippet-field label { margin-top: 0; margin-bottom: 4px; }
  .snippet-field input, .snippet-field textarea {
    font-size: 12px; padding: 6px 8px;
    font-family: ui-monospace, Consolas, monospace;
    background: #fff;
  }
  .snippet-field textarea { min-height: 140px; }
  .snippet-copy   { font-size: 11px; padding: 6px 12px; background: #f0f0ee;
                    color: #333; border: 0.5px solid rgba(0,0,0,0.15);
                    border-radius: 4px; cursor: pointer; font-family: inherit;
                    margin-top: 18px; white-space: nowrap; flex-shrink: 0; }
  .snippet-copy:hover { background: #e8e8e6; }
  .snippet-copy.copied { background: #1a5c2e; color: #fff;
                         border-color: #1a5c2e; }
  /* Comp estimate panel (inline within cover-letter row) */
  .comp-panel        { background: #f8fbfc; border: 0.5px solid rgba(46,117,182,0.25);
                       border-radius: 8px; padding: 12px 14px; margin-top: 10px; }
  .comp-summary-bar  { display: flex; align-items: center; gap: 8px;
                       margin-bottom: 10px; flex-wrap: wrap; }
  .comp-summary-label{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
                       color: #555; font-weight: 500; }
  .comp-summary-value{ flex: 1; font-size: 12.5px; color: #1a1a18;
                       font-variant-numeric: tabular-nums;
                       background: #fff; padding: 4px 8px; border-radius: 4px;
                       border: 0.5px solid rgba(0,0,0,0.08); min-width: 240px; }
  .comp-table        { width: 100%; border-collapse: collapse; font-size: 12px;
                       font-variant-numeric: tabular-nums; }
  .comp-table td     { padding: 6px 8px; border-bottom: 0.5px solid rgba(0,0,0,0.06);
                       vertical-align: middle; }
  .comp-table tr:last-child td { border-bottom: none; }
  .comp-label        { color: #555; font-weight: 500; min-width: 120px; }
  .comp-range        { color: #666; }
  .comp-target       { color: #1a5c2e; font-weight: 500; white-space: nowrap; }
  .comp-badge        { display: inline-block; font-size: 9.5px; padding: 1px 6px;
                       border-radius: 3px; text-transform: uppercase;
                       letter-spacing: 0.04em; font-weight: 500;
                       margin-right: 4px; vertical-align: middle; }
  .comp-badge-expected   { background: #d4edda; color: #155724; }
  .comp-badge-possible   { background: #fef3cd; color: #7a4f00; }
  .comp-badge-statedinjd { background: #cfe2ff; color: #084298; }
  .comp-badge-unusual    { background: #f0f0ee; color: #888; }
  .comp-reason       { color: #888; font-size: 10.5px; font-style: italic; }
  .comp-unusual      { margin-top: 8px; }
  .comp-unusual summary { font-size: 11px; color: #2E75B6; cursor: pointer;
                          list-style: none; padding: 2px 0; user-select: none; }
  .comp-unusual summary::-webkit-details-marker { display: none; }
  .comp-unusual summary::before { content: '▸ '; color: #888; }
  .comp-unusual[open] summary::before { content: '▾ '; }
  .comp-unusual-body { padding: 8px 10px; margin-top: 4px;
                       background: #fafaf8; border-radius: 6px; }
  .comp-unusual-item { padding: 3px 0; font-size: 11px; color: #555; }
  .comp-unusual-item strong { color: #1a1a18; font-weight: 500; }
  .comp-footer       { display: flex; align-items: flex-start; gap: 10px;
                       margin-top: 10px; padding-top: 8px;
                       border-top: 0.5px solid rgba(0,0,0,0.06);
                       font-size: 11px; flex-wrap: wrap; }
  .comp-conf         { font-weight: 500; padding: 2px 6px; border-radius: 3px;
                       text-transform: uppercase; letter-spacing: 0.04em;
                       font-size: 9.5px; white-space: nowrap; }
  .comp-conf-high    { background: #d4edda; color: #155724; }
  .comp-conf-med     { background: #fef3cd; color: #7a4f00; }
  .comp-conf-low     { background: #f8d7da; color: #721c24; }
  .comp-reasoning    { color: #666; flex: 1; min-width: 200px; line-height: 1.45; }
  .comp-stale        { color: #888; font-size: 10px; margin-left: auto;
                       white-space: nowrap; }
  /* Metrics page (/metrics) */
  .metrics-summary   { display: flex; gap: 20px; flex-wrap: wrap;
                       margin-bottom: 4px; }
  .metrics-stat      { flex: 1; min-width: 120px; }
  .metrics-stat .stat-label { font-size: 10px; text-transform: uppercase;
                              letter-spacing: 0.06em; color: #888;
                              font-weight: 500; }
  .metrics-stat .stat-value { font-size: 22px; color: #1F3864;
                              font-weight: 500; font-variant-numeric: tabular-nums;
                              margin-top: 2px; }
  .metrics-stat .stat-sub   { font-size: 11px; color: #666; margin-top: 2px; }
  .metrics-table     { width: 100%; border-collapse: collapse; font-size: 12.5px;
                       font-variant-numeric: tabular-nums; }
  .metrics-table th  { text-align: left; padding: 8px 10px;
                       border-bottom: 0.5px solid rgba(0,0,0,0.15);
                       font-weight: 500; color: #555; }
  .metrics-table th.num { text-align: right; }
  .metrics-table td  { padding: 6px 10px;
                       border-bottom: 0.5px solid rgba(0,0,0,0.06); }
  .metrics-table td.num { text-align: right; color: #1a1a18; }
  .metrics-table td.label { color: #555; font-weight: 500; }
  .metrics-table tr:last-child td { border-bottom: none; }
  .metrics-na        { color: #aaa; font-style: italic; }
  .hist              { margin-top: 4px; }
  .hist-row          { display: flex; align-items: center; gap: 10px;
                       padding: 3px 0; font-size: 11.5px;
                       font-variant-numeric: tabular-nums; }
  .hist-band         { width: 60px; color: #555; flex-shrink: 0; }
  .hist-bar          { flex: 1; height: 14px; display: flex;
                       background: #f0f0ee; border-radius: 3px; overflow: hidden;
                       min-width: 80px; }
  .hist-seg          { height: 100%; }
  .hist-seg-flight   { background: #2E75B6; }
  .hist-seg-dead     { background: #c75050; }
  .hist-seg-positive { background: #1a5c2e; }
  .hist-seg-other    { background: #aaa; }
  .hist-count        { color: #555; font-size: 11px; width: 60px;
                       text-align: right; flex-shrink: 0; }
  .hist-legend       { display: flex; gap: 14px; margin-top: 10px;
                       font-size: 11px; color: #666; }
  .hist-legend span::before { content: '■ '; }
  .hist-legend .leg-flight::before  { color: #2E75B6; }
  .hist-legend .leg-dead::before    { color: #c75050; }
  .hist-legend .leg-positive::before { color: #1a5c2e; }

  /* ── Answer Questions page ──────────────────────────────────────────── */
  .aq-add-form     { margin-bottom: 14px; }
  .aq-new-text     { width: 100%; padding: 8px; font-size: 13px;
                     border: 0.5px solid rgba(0,0,0,0.2); border-radius: 5px;
                     font-family: inherit; resize: vertical; }
  .aq-new-cap      { padding: 5px 8px; font-size: 12px;
                     border: 0.5px solid rgba(0,0,0,0.2); border-radius: 5px; }
  .aq-list         { display: flex; flex-direction: column; gap: 14px;
                     margin-top: 12px; }
  .aq-card         { border: 0.5px solid rgba(0,0,0,0.12); border-radius: 8px;
                     padding: 14px; background: #fafaf9; }
  .aq-card.aq-busy { opacity: 0.7; pointer-events: none; }
  .aq-card-head    { display: flex; gap: 8px; align-items: center;
                     margin-bottom: 8px; flex-wrap: wrap; }
  .aq-class        { text-transform: capitalize; }
  .aq-cap          { font-size: 11px; color: #666; }
  .aq-question-text{ font-size: 13.5px; font-weight: 500; color: #1F3864;
                     margin-bottom: 12px; line-height: 1.4; }
  .aq-section      { margin-top: 10px; }
  .aq-chips        { display: flex; gap: 6px; flex-wrap: wrap;
                     align-items: center; margin-top: 6px; }
  .aq-chip         { display: inline-flex; align-items: center; gap: 4px;
                     padding: 3px 6px 3px 8px; border-radius: 12px;
                     background: #e6f1fb; color: #0c447c; font-size: 11px; }
  .aq-chip-remove  { border: none; background: transparent; cursor: pointer;
                     color: #0c447c; font-size: 13px; padding: 0 2px;
                     line-height: 1; }
  .aq-chip-remove:hover { color: #c75050; }
  .aq-chip-add     { font-size: 11px; padding: 3px 6px;
                     border: 0.5px solid rgba(0,0,0,0.2); border-radius: 4px;
                     background: #fff; }
  .aq-override-text{ width: 100%; min-height: 48px; padding: 6px 8px;
                     font-size: 12px; font-family: inherit;
                     border: 0.5px solid rgba(0,0,0,0.15); border-radius: 5px;
                     resize: vertical; background: #fff; margin-top: 4px; }
  .aq-controls     { display: flex; gap: 8px; align-items: center;
                     flex-wrap: wrap; margin-bottom: 8px; }
  .aq-version-picker { font-size: 11px; padding: 4px 6px;
                       border: 0.5px solid rgba(0,0,0,0.2); border-radius: 4px;
                       background: #fff; }
  .aq-version-meta { font-size: 11px; color: #666; }
  .aq-answer-text  { width: 100%; min-height: 120px; padding: 10px;
                     font-size: 13px; font-family: ui-monospace, Consolas, monospace;
                     border: 0.5px solid rgba(0,0,0,0.2); border-radius: 5px;
                     resize: vertical; background: #fff; line-height: 1.45; }
  .aq-empty-answer { padding: 14px; background: #fff;
                     border: 0.5px dashed rgba(0,0,0,0.2); border-radius: 5px;
                     color: #888; font-size: 12px; }
  .aq-finalized    { margin-top: 12px; padding: 10px; border-radius: 6px;
                     background: #d4edda; border: 0.5px solid #b7d8c0; }
  .aq-finalized-label { font-size: 11px; font-weight: 500; color: #155724;
                        margin-bottom: 4px; text-transform: uppercase; }
  .aq-finalized-text { width: 100%; min-height: 100px; padding: 8px;
                       font-size: 13px; font-family: ui-monospace, Consolas, monospace;
                       background: #fff; border: 0.5px solid rgba(0,0,0,0.15);
                       border-radius: 4px; line-height: 1.45; }
  .aq-notes-grid   { display: grid; grid-template-columns: 1fr; gap: 10px;
                     margin-top: 12px; }
  .aq-note-row     { display: flex; flex-direction: column; gap: 4px; }
  .aq-note-label   { font-size: 11.5px; color: #1F3864; font-weight: 500; }
  .aq-note-text    { width: 100%; min-height: 40px; padding: 6px 8px;
                     font-size: 12px; font-family: inherit;
                     border: 0.5px solid rgba(0,0,0,0.15); border-radius: 4px;
                     resize: vertical; background: #fff; }
</style>
"""

def page(title: str, body: str, nav_query: str = "") -> str:
    from html import escape as esc
    q_val = esc(nav_query)
    search_form = (
        '<form method="GET" action="/search" class="nav-search">'
        f'<input name="q" type="search" value="{q_val}" placeholder="search company or role…" autocomplete="off">'
        '<button class="btn-mini" type="submit">Search</button>'
        '</form>'
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — next-role</title>{STYLE}</head>
<body><div class="wrap">
<h1>next-role</h1>
<div class="nav-bar">
  <p class="sub" style="margin-bottom:0">Job search pipeline · <a href="/today">Today</a> · <a href="/">Ingest</a> · <a href="/pipeline">Pipeline</a> · <a href="/resume">Snippets</a> · <a href="/metrics">Metrics</a></p>
  {search_form}
</div>
{body}
</div></body></html>"""


def ingest_form(
    url: str = "", company: str = "", title: str = "",
    location: str = "", posted: str = "", jd_text: str = "",
    notice: str = "", show_paste: bool = False
) -> str:
    today = date.today().isoformat()
    notice_html = f'<div class="notice notice-warn">{notice}</div>' if notice else ""
    paste_section = ""
    if show_paste:
        paste_section = f"""
        <label>Job description text
          <span style="color:#888;font-weight:400">
            — paste from the job posting page
          </span>
        </label>
        <textarea name="jd_text" placeholder="Paste the full job description here...">{jd_text}</textarea>
        """
    return page("Ingest job", f"""
<div class="card">
  <h2>Add job to pipeline</h2>
  {notice_html}
  <form method="POST" action="/ingest">
    <label>Job posting URL</label>
    <input name="url" type="url" value="{url}"
           placeholder="https://boards.greenhouse.io/company/jobs/123456" required>
    <div class="row">
      <div>
        <label>Company name</label>
        <input name="company" value="{company}" placeholder="Stripe" required>
      </div>
      <div>
        <label>Date posted (optional)</label>
        <input name="posted" type="date" value="{posted or today}">
      </div>
    </div>
    <label>Job title</label>
    <input name="title" value="{title}"
           placeholder="Staff Software Engineer" required>
    <label>Location</label>
    <input name="location" value="{location}"
           placeholder="Remote Canada" required>
    {paste_section}
    <button class="btn btn-primary" type="submit">
      {'Ingest job' if not show_paste else 'Ingest with pasted JD'}
    </button>
    <a href="/" class="btn btn-secondary">Clear</a>
  </form>
</div>
{pipeline_card()}
""")


def pipeline_card() -> str:
    jobs     = load_pipeline()
    co_by_id = load_companies_by_id()
    active   = [j for j in jobs if j.get("pipeline_status") != "archived"]
    if not active:
        return '<div class="card"><h2>Pipeline</h2><p style="color:#888">No jobs yet.</p></div>'

    rows = ""
    for job in sorted(active, key=lambda j: job_score(j, co_by_id), reverse=True)[:10]:
        score      = job_score(job, co_by_id)
        apply_url  = job.get("apply_url", "")
        apply_link = f'<a class="apply-link" href="{apply_url}" target="_blank">Apply ↗</a>' if apply_url else ""
        rows += f"""
        <div class="job-row">
          <span class="score">{score}</span>
          <span class="company">{job['company_name'][:18]}</span>
          <span class="title-cell">{job['title'][:40]}</span>
          {apply_link}
        </div>"""

    return f"""
<div class="card">
  <h2>Pipeline <span style="color:#888;font-weight:400;font-size:12px">
    ({len(active)} active) — <a href="/pipeline">view all</a>
  </span></h2>
  <div class="pipeline">{rows}</div>
</div>"""


def pipeline_page() -> str:
    from html import escape as esc

    jobs     = load_pipeline()
    co_by_id = load_companies_by_id()
    apps     = load_applications()
    active   = [j for j in jobs if j.get("pipeline_status") != "archived"]

    # ── Upcoming interviews (from application_tracker.json) ──────────────────
    interview_apps = [
        a for a in apps
        if a.get("status") in ("recruiter_screen", "interview", "offer")
    ]
    interview_card = ""
    if interview_apps:
        interview_rows = ""
        for app in interview_apps:
            job_id  = esc(app.get("job_id", ""))
            status  = app.get("status", "")
            cls     = _STATUS_LABEL_CLASS.get(status, "pf-badge")
            company = esc((app.get("company_name") or "?")[:30])
            title   = esc((app.get("title") or "?")[:50])
            applied = esc(app.get("date_applied", "") or "—")
            interview_rows += f"""
            <a href="/job/{job_id}" style="text-decoration:none;color:inherit">
              <div class="job-row" style="cursor:pointer">
                <span class="company">{company}</span>
                <span class="title-cell">{title}</span>
                <span class="app-status {cls}">{esc(status.replace('_',' '))}</span>
                <span style="color:#888;font-size:11px">applied {applied}</span>
              </div>
            </a>"""
        interview_card = f"""
<div class="card" style="background:#f8fbfc;border-color:rgba(46,117,182,0.35)">
  <h2>Upcoming interviews ({len(interview_apps)})
    <span style="color:#888;font-weight:400;font-size:12px">— click a row for the prep page (JD, comp, cover letter)</span>
  </h2>
  <div class="pipeline">{interview_rows}</div>
</div>
"""

    # ── All active jobs ──────────────────────────────────────────────────────
    rows = ""
    for job in sorted(active, key=lambda j: job_score(j, co_by_id), reverse=True):
        score      = job_score(job, co_by_id)
        job_id     = job.get("job_id", "")
        apply_url  = job.get("apply_url", "")
        apply_link = f'<a class="apply-link" href="{apply_url}" target="_blank">Apply ↗</a>' if apply_url else ""
        details_link = f'<a class="apply-link" href="/job/{job_id}">Details →</a>'
        cl = "✓" if job.get("cover_letter_generated") else ""
        rows += f"""
        <div class="job-row">
          <span class="score">{score}</span>
          <span class="company">{job['company_name'][:20]}</span>
          <span class="title-cell">{job['title'][:45]}</span>
          <span style="color:#888;font-size:11px">{job.get('pipeline_status','')}</span>
          <span style="color:#1a5c2e;font-size:11px">{cl}</span>
          {apply_link}
          {details_link}
        </div>"""

    return page("Pipeline", f"""
{interview_card}
<div class="card">
  <h2>All active jobs ({len(active)})</h2>
  <div class="pipeline">{rows or '<p style="color:#888">No jobs yet.</p>'}</div>
</div>
<p><a href="/">← Add job</a></p>
""")


# ── /search — find a role by company or title ────────────────────────────────

def search_page(query: str) -> str:
    """Find a role by company name or job title across non-archived pipeline
    entries (active + applied + in-flight + post-response). Built for
    recruiter-call prep: type the company, click the role, land on /job/<id>
    with the JD, company research, comp estimate, and application timeline.

    Match: case-insensitive substring against company_name and title.
    Sort:  jobs with an application first (date_applied desc), then the rest
           by composite score desc.
    Empty query: browse all (still sorted the same way), capped so the page
    stays light."""
    from html import escape as esc

    q       = (query or "").strip()
    q_lower = q.lower()
    jobs    = load_pipeline()
    apps    = load_applications()
    co_by_id = load_companies_by_id()

    apps_by_job = {a.get("job_id"): a for a in apps if a.get("job_id")}

    candidates = [j for j in jobs if j.get("pipeline_status") != "archived"]

    if q_lower:
        def matches(j: dict) -> bool:
            return (
                q_lower in (j.get("company_name") or "").lower()
                or q_lower in (j.get("title") or "").lower()
            )
        candidates = [j for j in candidates if matches(j)]

    with_app    = [j for j in candidates if apps_by_job.get(j.get("job_id"))]
    without_app = [j for j in candidates if not apps_by_job.get(j.get("job_id"))]
    with_app.sort(
        key=lambda j: apps_by_job[j["job_id"]].get("date_applied", ""),
        reverse=True,
    )
    without_app.sort(key=lambda j: job_score(j, co_by_id), reverse=True)

    BROWSE_CAP = 40
    truncated  = False
    if not q_lower:
        combined = with_app + without_app
        if len(combined) > BROWSE_CAP:
            combined  = combined[:BROWSE_CAP]
            truncated = True
    else:
        combined = with_app + without_app

    n = len(combined)
    if q_lower:
        header_text = (
            f'<strong>{n}</strong> result{"s" if n != 1 else ""} '
            f'for <span style="font-family:ui-monospace,Consolas,monospace">{esc(q)}</span>'
        )
    else:
        header_text = (
            f'Showing <strong>{n}</strong> non-archived role{"s" if n != 1 else ""}'
            + (' — top 40, type a query to narrow.' if truncated else '')
        )

    if not combined:
        if q_lower:
            empty = (
                '<p style="color:#888;font-size:13px">'
                'No matches. Try a shorter or different query — match is '
                'case-insensitive substring against company name and job title.'
                '</p>'
            )
        else:
            empty = (
                '<p style="color:#888;font-size:13px">'
                'No active or in-flight jobs in the pipeline yet.'
                '</p>'
            )
        body = f"""
<div class="card">
  <h2>Search</h2>
  <form method="GET" action="/search" style="margin-bottom:12px">
    <input name="q" type="search" value="{esc(q)}" placeholder="company name or job title…"
           autocomplete="off" style="font-size:13px">
    <button class="btn btn-primary" type="submit" style="margin-top:10px">Search</button>
  </form>
  {empty}
</div>"""
        return page("Search", body, nav_query=q)

    rows = []
    for job in combined:
        job_id     = esc(job.get("job_id", ""))
        score      = job_score(job, co_by_id)
        company    = esc((job.get("company_name") or "?")[:30])
        title      = esc((job.get("title") or "?")[:55])
        location   = esc((job.get("location") or "")[:30])
        apply_url  = esc(job.get("apply_url", ""))
        apply_link = (
            f'<a class="apply-link" href="{apply_url}" target="_blank" rel="noopener">Apply ↗</a>'
            if apply_url else ""
        )

        app = apps_by_job.get(job.get("job_id"))
        if app:
            status     = app.get("status", "applied")
            applied    = app.get("date_applied", "")
            sub_label  = f'applied {applied}' if applied else 'applied'
        else:
            status    = job.get("pipeline_status", "active")
            sub_label = status.replace("_", " ")
        status_cls = _STATUS_LABEL_CLASS.get(status, "pf-badge")

        rows.append(f"""
        <a href="/job/{job_id}" style="text-decoration:none;color:inherit">
          <div class="job-row" style="cursor:pointer">
            <span class="score">{score}</span>
            <span class="company">{company}</span>
            <span class="title-cell">{title}</span>
            <span style="color:#888;font-size:11px">{location}</span>
            <span class="app-status {status_cls}">{esc(sub_label)}</span>
            {apply_link}
          </div>
        </a>""")

    body = f"""
<div class="card">
  <h2>Search</h2>
  <form method="GET" action="/search" style="margin-bottom:14px">
    <input name="q" type="search" value="{esc(q)}" placeholder="company name or job title…"
           autocomplete="off" autofocus style="font-size:13px">
    <button class="btn btn-primary" type="submit" style="margin-top:10px">Search</button>
  </form>
  <p style="color:#666;font-size:12px;margin-bottom:10px">{header_text}</p>
  <div class="pipeline">{"".join(rows)}</div>
</div>"""

    return page("Search", body, nav_query=q)


# ── /metrics — read-only analytics ────────────────────────────────────────────

def _fmt_num(v, suffix: str = "") -> str:
    """Render a number, or '—' (in a muted span) if None."""
    if v is None:
        return '<span class="metrics-na">—</span>'
    if isinstance(v, float):
        return f"{v:g}{suffix}"
    return f"{v}{suffix}"


def _hist_row(band: str, by_cohort: dict, max_count: int) -> str:
    """One row of the score-distribution histogram. Stacked bar across cohorts."""
    in_flight = by_cohort.get("in_flight", 0)
    dead      = by_cohort.get("dead", 0)
    positive  = by_cohort.get("positive", 0)
    total     = in_flight + dead + positive

    # Each cohort gets a slice proportional to its count within the row's
    # total, scaled overall by row-total / max-row-total. So a band with
    # the most apps fills the bar; smaller bands fill proportionally.
    width_pct = (total / max_count * 100) if max_count else 0
    segs = ""
    if total > 0:
        f_pct = in_flight / total * width_pct
        d_pct = dead      / total * width_pct
        p_pct = positive  / total * width_pct
        if f_pct > 0:
            segs += f'<span class="hist-seg hist-seg-flight" style="width:{f_pct:.2f}%"></span>'
        if d_pct > 0:
            segs += f'<span class="hist-seg hist-seg-dead"   style="width:{d_pct:.2f}%"></span>'
        if p_pct > 0:
            segs += f'<span class="hist-seg hist-seg-positive" style="width:{p_pct:.2f}%"></span>'

    parts = []
    if in_flight: parts.append(f"{in_flight} in-flight")
    if dead:      parts.append(f"{dead} dead")
    if positive:  parts.append(f"{positive} offer")
    label = ", ".join(parts) if parts else ""

    return (
        f'<div class="hist-row">'
        f'  <span class="hist-band">{band}</span>'
        f'  <span class="hist-bar">{segs}</span>'
        f'  <span class="hist-count">{label}</span>'
        f'</div>'
    )


def metrics_page() -> str:
    """Render the /metrics page from metrics.build_metrics()."""
    from metrics import build_metrics  # local import keeps cold-start light

    m = build_metrics()
    total          = m["total_apps"]
    cohort_sizes   = m["cohort_sizes"]
    status_counts  = m["status_counts"]
    avg_composite  = m["avg_composite"]
    avg_components = m["avg_components"]
    component_max  = m["component_max"]
    composite_max  = m["composite_max"]
    score_dist     = m["score_distribution"]
    funnel_speed   = m["funnel_speed"]
    band_size      = m["score_band_size"]
    components_order = m["components_ordered"]

    # ── Overview card ─────────────────────────────────────────────────────────
    overview = f"""
    <div class="card">
      <h2>Overview</h2>
      <div class="metrics-summary">
        <div class="metrics-stat">
          <div class="stat-label">Total applications</div>
          <div class="stat-value">{total}</div>
        </div>
        <div class="metrics-stat">
          <div class="stat-label">In-flight</div>
          <div class="stat-value">{cohort_sizes['in_flight']}</div>
          <div class="stat-sub">applied · recruiter · interview</div>
        </div>
        <div class="metrics-stat">
          <div class="stat-label">Dead</div>
          <div class="stat-value">{cohort_sizes['dead']}</div>
          <div class="stat-sub">rejected · ghosted</div>
        </div>
        <div class="metrics-stat">
          <div class="stat-label">Offers</div>
          <div class="stat-value">{cohort_sizes['positive']}</div>
          <div class="stat-sub">offer</div>
        </div>
      </div>
    </div>"""

    # ── Status breakdown ──────────────────────────────────────────────────────
    if status_counts:
        status_rows = "".join(
            f'<tr><td class="label">{s}</td>'
            f'<td class="num">{n}</td>'
            f'<td class="num">{(n / total * 100):.1f}%</td></tr>'
            for s, n in sorted(status_counts.items(), key=lambda x: -x[1])
        )
        status_card = f"""
        <div class="card">
          <h2>Status breakdown</h2>
          <table class="metrics-table">
            <thead><tr><th>Status</th><th class="num">Count</th><th class="num">Share</th></tr></thead>
            <tbody>{status_rows}</tbody>
          </table>
        </div>"""
    else:
        status_card = ""

    # ── Average composite by cohort ───────────────────────────────────────────
    composite_card = f"""
    <div class="card">
      <h2>Average composite score by cohort</h2>
      <table class="metrics-table">
        <thead><tr>
          <th>Cohort</th>
          <th class="num">Count</th>
          <th class="num">Avg composite (/{composite_max})</th>
        </tr></thead>
        <tbody>
          <tr><td class="label">In-flight</td>
              <td class="num">{cohort_sizes['in_flight']}</td>
              <td class="num">{_fmt_num(avg_composite['in_flight'])}</td></tr>
          <tr><td class="label">Dead</td>
              <td class="num">{cohort_sizes['dead']}</td>
              <td class="num">{_fmt_num(avg_composite['dead'])}</td></tr>
          <tr><td class="label">Offer</td>
              <td class="num">{cohort_sizes['positive']}</td>
              <td class="num">{_fmt_num(avg_composite['positive'])}</td></tr>
        </tbody>
      </table>
    </div>"""

    # ── Per-component contribution averages ───────────────────────────────────
    comp_rows = ""
    for key in components_order:
        max_w = component_max[key]
        comp_rows += (
            f'<tr>'
            f'  <td class="label">{key.capitalize()}</td>'
            f'  <td class="num">{_fmt_num(avg_components["in_flight"].get(key))}</td>'
            f'  <td class="num">{_fmt_num(avg_components["dead"].get(key))}</td>'
            f'  <td class="num">{_fmt_num(avg_components["positive"].get(key))}</td>'
            f'  <td class="num" style="color:#888">/{max_w}</td>'
            f'</tr>'
        )
    components_card = f"""
    <div class="card">
      <h2>Component contribution averages</h2>
      <p style="color:#666;font-size:12px;margin-bottom:8px">
        Each cell is the average weighted contribution to the composite — same
        math as <code>composite_score</code>, so the rows sum to the cohort's
        average composite shown above.
      </p>
      <table class="metrics-table">
        <thead><tr>
          <th>Component</th>
          <th class="num">In-flight</th>
          <th class="num">Dead</th>
          <th class="num">Offer</th>
          <th class="num">Max</th>
        </tr></thead>
        <tbody>{comp_rows}</tbody>
      </table>
    </div>"""

    # ── Score-distribution histogram ──────────────────────────────────────────
    # Only render bands with at least one data point so empty leading/trailing
    # 0-9 / 130 bands don't dominate the chart vertically.
    non_empty_bands = {b: counts for b, counts in score_dist.items()
                       if counts["total"] > 0}
    if non_empty_bands:
        max_total = max(counts["total"] for counts in non_empty_bands.values())
        hist_rows = "".join(
            _hist_row(band, counts, max_total)
            for band, counts in non_empty_bands.items()
        )
        legend = (
            '<div class="hist-legend">'
            '<span class="leg-flight">In-flight</span>'
            '<span class="leg-dead">Dead</span>'
            '<span class="leg-positive">Offer</span>'
            '</div>'
        )
        histogram_card = f"""
        <div class="card">
          <h2>Composite-score distribution</h2>
          <p style="color:#666;font-size:12px;margin-bottom:8px">
            Apps grouped by composite score in bands of {band_size}.
            Bar width is the band's count; segments split it across cohorts.
          </p>
          <div class="hist">{hist_rows}</div>
          {legend}
        </div>"""
    else:
        histogram_card = ""

    # ── Funnel speed ──────────────────────────────────────────────────────────
    def _fmt_speed(s: dict | None) -> str:
        if not s:
            return '<span class="metrics-na">no data</span>'
        return (
            f'{s["count"]} response(s) · '
            f'median {s["median"]}d · mean {s["mean"]}d · '
            f'range {s["min"]}–{s["max"]}d'
        )
    funnel_card = f"""
    <div class="card">
      <h2>Funnel speed (days to first response)</h2>
      <table class="metrics-table">
        <thead><tr><th>Cohort</th><th>Days-to-response</th></tr></thead>
        <tbody>
          <tr><td class="label">In-flight</td><td>{_fmt_speed(funnel_speed.get('in_flight'))}</td></tr>
          <tr><td class="label">Dead</td><td>{_fmt_speed(funnel_speed.get('dead'))}</td></tr>
          <tr><td class="label">Offer</td><td>{_fmt_speed(funnel_speed.get('positive'))}</td></tr>
        </tbody>
      </table>
      <p style="color:#888;font-size:11px;margin-top:8px">
        Only counts applications with a <code>response_date</code> set —
        the in-flight cohort generally has none until status moves out of
        <code>applied</code>.
      </p>
    </div>"""

    body = overview + status_card + composite_card + components_card + histogram_card + funnel_card

    if total == 0:
        body = (
            '<div class="card">'
            '<h2>Metrics</h2>'
            '<p style="color:#888">No applications logged yet. Submit one via '
            '<code>scripts/update_status.py log</code> or the /today UI and '
            'come back here.</p>'
            '</div>'
        )

    return page("Metrics", body)


_SNIPPETS_JS = """
<script>
(function () {
  document.querySelectorAll('.snippet-copy').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var sel = btn.getAttribute('data-target');
      var el  = sel ? document.getElementById(sel) : null;
      if (!el) return;
      var text = (el.value !== undefined) ? el.value : el.textContent;
      var ok = function () {
        var orig = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function () {
          btn.textContent = orig;
          btn.classList.remove('copied');
        }, 1200);
      };
      var fail = function () {
        var orig = btn.textContent;
        btn.textContent = 'Copy failed';
        setTimeout(function () { btn.textContent = orig; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(ok, function () {
          // Fallback for non-secure contexts.
          try { el.select(); document.execCommand('copy'); ok(); }
          catch (e) { fail(); }
        });
      } else {
        try { el.select(); document.execCommand('copy'); ok(); }
        catch (e) { fail(); }
      }
    });
  });
})();
</script>
"""


_SNIPPET_STRIP_RE = re.compile(r"[\[\]{}<>]")
_SNIPPET_WS_RUN_RE = re.compile(r"[ \t]{2,}")


def _sanitize_snippet(value: str) -> str:
    """Strip characters that ATS forms commonly mangle or reject."""
    if not value:
        return value
    s = value.replace("—", "-").replace("–", "-")
    s = _SNIPPET_STRIP_RE.sub("", s)
    lines = [_SNIPPET_WS_RUN_RE.sub(" ", ln).rstrip() for ln in s.split("\n")]
    return "\n".join(lines)


def _snippet_field(field_id: str, label: str, value: str, multiline: bool = False) -> str:
    from html import escape as esc
    value = _sanitize_snippet(value)
    if multiline:
        input_el = (
            f'<textarea id="{field_id}" readonly '
            f'rows="{max(3, min(value.count(chr(10)) + 2, 18))}">'
            f'{esc(value)}</textarea>'
        )
    else:
        input_el = (
            f'<input id="{field_id}" type="text" '
            f'value="{esc(value)}" readonly>'
        )
    return f"""
    <div class="snippet-field">
      <div class="snippet-field-col">
        <label for="{field_id}">{esc(label)}</label>
        {input_el}
      </div>
      <button class="snippet-copy" type="button" data-target="{field_id}">Copy</button>
    </div>
    """


def render_experience_entry(idx: int, exp: dict) -> str:
    from html import escape as esc
    pfx      = f"exp{idx}"
    title    = exp.get("title", "")
    company  = exp.get("company", "")
    location = exp.get("location", "")
    frm      = exp.get("from", "")
    to       = exp.get("to", "")
    desc     = exp.get("description", "")

    head = f'<strong>{esc(title)}</strong>'
    if company:
        head += f' <span class="snippet-meta">· {esc(company)}</span>'
    if frm or to:
        head += f' <span class="snippet-meta">({esc(frm)} – {esc(to)})</span>'

    fields_html = "".join([
        _snippet_field(f"{pfx}-title",    "Title",           title),
        _snippet_field(f"{pfx}-company",  "Company",         company),
        _snippet_field(f"{pfx}-location", "Location",        location),
        _snippet_field(f"{pfx}-from",     "From (MM/YYYY)",  frm),
        _snippet_field(f"{pfx}-to",       "To (MM/YYYY)",    to),
        _snippet_field(f"{pfx}-desc",     "Job description", desc, multiline=True),
    ])

    return f"""
    <details class="snippet-entry">
      <summary>{head}</summary>
      <div class="snippet-fields">{fields_html}</div>
    </details>
    """


def render_education_entry(idx: int, edu: dict) -> str:
    from html import escape as esc
    pfx         = f"edu{idx}"
    degree      = edu.get("degree", "")
    institution = edu.get("institution", "")
    location    = edu.get("location", "")
    frm         = edu.get("from", "")
    to          = edu.get("to", "")

    head = f'<strong>{esc(degree[:80])}</strong>'
    if institution:
        head += f' <span class="snippet-meta">· {esc(institution)}</span>'
    if frm or to:
        head += f' <span class="snippet-meta">({esc(frm)} – {esc(to)})</span>'

    fields_html = "".join([
        _snippet_field(f"{pfx}-degree",      "Degree",          degree,
                       multiline=len(degree) > 60),
        _snippet_field(f"{pfx}-institution", "Institution",     institution),
        _snippet_field(f"{pfx}-location",    "Location",        location),
        _snippet_field(f"{pfx}-from",        "From (MM/YYYY)",  frm),
        _snippet_field(f"{pfx}-to",          "To (MM/YYYY)",    to),
    ])

    return f"""
    <details class="snippet-entry">
      <summary>{head}</summary>
      <div class="snippet-fields">{fields_html}</div>
    </details>
    """


def render_links_card() -> str:
    fields_html = "".join(
        _snippet_field(f"link-{label.lower()}", label, url)
        for label, url in PROFILE_LINKS
    )
    return f"""
    <div class="card">
      <h2>Links</h2>
      <div class="snippet-fields">{fields_html}</div>
    </div>
    """


_STATUS_LABEL_CLASS = {
    "applied":          "app-status-applied",
    "recruiter_screen": "app-status-recruiter_screen",
    "interview":        "app-status-interview",
    "ghosted":          "app-status-ghosted",
    "rejected":         "pf-fail",
    "withdrawn":        "app-status-ghosted",
    "offer":            "pf-pass",
    "active":           "pf-badge",
    "cover_letter_ready": "pf-pass",
}


def render_company_card(company: dict | None, return_to: str = "") -> str:
    """Researched company fields, surfaced on /job/<id> for recruiter prep.
    Reads sponsorship + remote denominators from COMPONENTS so the values
    stay in sync with the scoring SSOT (see CLAUDE.md). When the record is
    a stub, renders a 'Research now' button that POSTs to
    /today/company/research and brings the user back to ``return_to``."""
    from html import escape as esc

    if not company:
        return (
            '<div class="card"><h2>Company</h2>'
            '<p style="color:#888;font-size:12px">No company record on file.</p>'
            '</div>'
        )

    company_id  = esc(company.get("company_id", ""))
    name        = esc(company.get("name", "?"))
    industry    = esc(company.get("industry") or "—")
    size        = esc(company.get("size_tier") or "—")
    country     = esc(company.get("country_hq") or "—")
    portal      = (company.get("job_portal_url") or "").strip()
    portal_html = (
        f'<a href="{esc(portal)}" target="_blank" rel="noopener">careers ↗</a>'
        if portal else "—"
    )
    is_stub = bool(company.get("stub"))

    spons_score = company.get("sponsorship_score")
    spons_notes = esc((company.get("sponsorship_notes") or "").strip())
    remote      = company.get("remote_fit")
    layoffs     = bool(company.get("recent_layoffs"))
    layoff_notes = esc((company.get("layoff_notes") or "").strip())
    gd_rating   = company.get("glassdoor_rating")
    gd_sent     = esc(company.get("glassdoor_engineering_sentiment") or "unknown")
    blind       = esc(company.get("blind_sentiment") or "unknown")

    spons_max   = COMPONENTS["sponsorship"].native_max
    remote_max  = COMPONENTS["remote"].native_max

    if is_stub and company_id:
        rt_input = (
            f'<input type="hidden" name="return_to" value="{esc(return_to)}">'
            if return_to else ''
        )
        stub_banner = f"""
        <div class="notice notice-warn" style="margin-bottom:12px">
          <div style="margin-bottom:8px">
            Stub record — not researched yet. Fields below are neutral defaults.
          </div>
          <form method="POST" action="/today/company/research" class="cl-form" style="display:inline">
            <input type="hidden" name="company_id" value="{company_id}">
            {rt_input}
            <button class="btn-mini" type="submit">Research now</button>
          </form>
          <span style="color:#888;font-size:11px;margin-left:8px">~20–30s · Haiku + 1 web search</span>
        </div>
        """
    else:
        stub_banner = ""

    spons_val = (
        f'<strong>{spons_score}</strong> / {spons_max}'
        if spons_score is not None else "—"
    )
    if spons_notes:
        spons_val += f' — <span style="color:#555">{spons_notes}</span>'

    remote_val = (
        f'<strong>{remote}</strong> / {remote_max}'
        if remote is not None else "—"
    )

    gd_val = (
        f'{gd_rating if gd_rating is not None else "—"}'
        f' · engineering: {gd_sent}'
    )

    layoff_row = ""
    if layoffs:
        layoff_row = (
            '<tr><td class="comp-label">Recent layoffs</td>'
            f'<td><span class="app-status pf-fail">yes</span> '
            f'<span style="color:#555">{layoff_notes}</span></td></tr>'
        )

    ethics_flags = company.get("ethics_flags") or []
    ethics_notes = esc((company.get("ethics_notes") or "").strip())
    ethics_block = ""
    if ethics_flags:
        flag_items = []
        for flag in ethics_flags:
            cat  = esc(flag.get("category", "") or "—")
            stat = esc(flag.get("status", "") or "—")
            desc = esc(flag.get("description", "") or "")
            src  = esc(flag.get("source", "") or "")
            sd   = esc(flag.get("source_date", "") or "")
            src_line = ""
            if src:
                src_line = f' <span style="color:#888;font-size:11px">— {src}{(", " + sd) if sd else ""}</span>'
            stat_cls = "pf-fail" if stat == "confirmed" else "pf-badge"
            flag_items.append(
                '<li style="padding:6px 0;border-bottom:0.5px solid rgba(0,0,0,0.06);font-size:12.5px">'
                f'<span class="app-status {stat_cls}">{stat}</span> '
                f'<strong>{cat}</strong> — {desc}{src_line}'
                '</li>'
            )
        notes_html = (
            f'<p style="color:#666;font-size:11.5px;margin-top:8px">{ethics_notes}</p>'
            if ethics_notes else ""
        )
        ethics_block = (
            '<div style="margin-top:14px">'
            '<div class="section-label">Ethics flags</div>'
            f'<ul style="list-style:none;padding:0;margin:0">{"".join(flag_items)}</ul>'
            f'{notes_html}'
            '</div>'
        )

    return f"""
    <div class="card">
      <h2>Company <span style="color:#888;font-weight:400;font-size:12px">— {name}</span></h2>
      {stub_banner}
      <table class="comp-table">
        <tr><td class="comp-label">Industry</td><td>{industry}</td></tr>
        <tr><td class="comp-label">Size</td><td>{size}</td></tr>
        <tr><td class="comp-label">HQ</td><td>{country}</td></tr>
        <tr><td class="comp-label">Careers page</td><td>{portal_html}</td></tr>
        <tr><td class="comp-label">Sponsorship</td><td>{spons_val}</td></tr>
        <tr><td class="comp-label">Remote fit</td><td>{remote_val}</td></tr>
        <tr><td class="comp-label">Glassdoor</td><td>{gd_val}</td></tr>
        <tr><td class="comp-label">Blind sentiment</td><td>{blind}</td></tr>
        {layoff_row}
      </table>
      {ethics_block}
    </div>
    """


def job_detail_page(job_id: str) -> str:
    """Per-job detail surface: pulls together JD, comp estimate, cover letter,
    application timeline. The compensation card is pinned at the top when the
    job is in an interview-stage status — that's when the user needs the
    negotiation numbers most."""
    from html import escape as esc

    jobs = load_pipeline()
    job  = next((j for j in jobs if j.get("job_id") == job_id), None)
    if not job:
        return page("Job not found", (
            '<div class="card">'
            '<p>No job with that id in the pipeline. '
            'It may have been archived or never ingested.</p>'
            '<p><a href="/pipeline">← Pipeline</a> · <a href="/today">Today</a></p>'
            '</div>'
        ))

    co_by_id  = load_companies_by_id()
    company   = co_by_id.get(job.get("company_id")) or {}
    comp_rec  = load_comp_estimates_by_job().get(job_id)
    apps      = load_applications()
    app       = next((a for a in apps if a.get("job_id") == job_id), None)

    if app:
        status = app.get("status", "applied")
    elif job.get("pipeline_status") == "archived":
        status = "archived"
    else:
        status = job.get("pipeline_status", "active")
    is_interview_stage = status in ("recruiter_screen", "interview", "offer")

    company_name = esc(job.get("company_name", "?"))
    title        = esc(job.get("title", "?"))
    location     = esc(job.get("location", ""))
    apply_url    = esc(job.get("apply_url", ""))
    score        = job_score(job, co_by_id)

    status_class = _STATUS_LABEL_CLASS.get(status, "pf-badge")
    apply_link_html = (
        f'<a class="apply-link" href="{apply_url}" target="_blank" rel="noopener">Open posting ↗</a>'
        if apply_url else ''
    )

    header_card = f"""
    <div class="card">
      <div class="cl-meta" style="margin-bottom:8px">
        <span class="score">{score}</span>
        <span class="company" style="font-size:15px">{company_name}</span>
        <span class="app-status {status_class}">{esc(status.replace('_',' '))}</span>
      </div>
      <div style="color:#555;font-size:14px;margin-bottom:4px">{title}</div>
      <div style="color:#888;font-size:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span>{location}</span>
        {apply_link_html}
      </div>
    </div>
    """

    # ── Compensation card ────────────────────────────────────────────────────
    if comp_rec:
        comp_inner = render_comp_panel(comp_rec, job_id)
        title_suffix = (
            ' <span style="color:#888;font-weight:400;font-size:11px">'
            '— review before recruiter call</span>'
            if is_interview_stage else ''
        )
        comp_card = f"""
    <div class="card">
      <h2>Compensation{title_suffix}</h2>
      {comp_inner}
      <div style="margin-top:10px">
        <form method="POST" action="/today/comp/estimate" class="cl-form" style="display:inline">
          <input type="hidden" name="job_id" value="{esc(job_id)}">
          <input type="hidden" name="return_to" value="/job/{esc(job_id)}">
          <button class="btn-mini" type="submit">Re-estimate</button>
        </form>
      </div>
    </div>
        """
    else:
        comp_card = f"""
    <div class="card">
      <h2>Compensation</h2>
      <p style="color:#888;font-size:12px">
        No estimate generated yet. Run one to get a salary range and bonus structure
        you can use in the application form and at negotiation time.
      </p>
      <form method="POST" action="/today/comp/estimate" class="cl-form" style="display:inline">
        <input type="hidden" name="job_id" value="{esc(job_id)}">
        <input type="hidden" name="return_to" value="/job/{esc(job_id)}">
        <button class="btn btn-primary" type="submit">Estimate compensation</button>
      </form>
    </div>
        """

    # ── Application timeline card (only if applied) ──────────────────────────
    timeline_card = ""
    if app:
        applied_date = esc(app.get("date_applied", "") or "—")
        status_upd   = esc((app.get("status_updated", "") or "")[:10])
        response     = esc((app.get("response_date", "") or "")[:10] or "—")
        notes        = (app.get("notes") or "").strip()
        notes_html   = (
            f'<p style="color:#444;font-size:12px;line-height:1.5;margin-top:8px">{esc(notes)}</p>'
            if notes else ''
        )
        timeline_card = f"""
    <div class="card">
      <h2>Application timeline</h2>
      <table class="comp-table">
        <tr><td class="comp-label">Applied</td><td>{applied_date}</td></tr>
        <tr><td class="comp-label">Current status</td><td><span class="app-status {status_class}">{esc(status.replace('_',' '))}</span> · last updated {status_upd or '—'}</td></tr>
        <tr><td class="comp-label">Response date</td><td>{response}</td></tr>
      </table>
      {notes_html}
    </div>
        """

    # ── Cover letter card ────────────────────────────────────────────────────
    cl_done    = bool(job.get("cover_letter_generated"))
    cl_relpath = (job.get("cover_letter_path") or "")
    cl_abspath = str(ROOT / cl_relpath) if cl_relpath else ""
    cl_filename = cl_relpath.split("/")[-1] if cl_relpath else ""
    if cl_done:
        cl_card = f"""
    <div class="card">
      <h2>Cover letter</h2>
      <div class="cl-file-line"><span class="cl-file-name">{esc(cl_filename)}</span></div>
      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
        <form method="POST" action="/today/cl/open" class="cl-form" style="display:inline">
          <input type="hidden" name="job_id" value="{esc(job_id)}">
          <input type="hidden" name="return_to" value="/job/{esc(job_id)}">
          <button class="btn-mini" type="submit">Open</button>
        </form>
        <button class="btn-mini cl-copy-path" type="button" data-path="{esc(cl_abspath)}">Copy Path</button>
        <form method="POST" action="/today/cl/generate" class="cl-form" style="display:inline">
          <input type="hidden" name="job_id" value="{esc(job_id)}">
          <input type="hidden" name="return_to" value="/job/{esc(job_id)}">
          <button class="btn-mini" type="submit">Regenerate</button>
        </form>
      </div>
    </div>
        """
    else:
        cl_card = f"""
    <div class="card">
      <h2>Cover letter</h2>
      <p style="color:#888;font-size:12px">Not generated yet.</p>
      <form method="POST" action="/today/cl/generate" class="cl-form" style="display:inline">
        <input type="hidden" name="job_id" value="{esc(job_id)}">
        <input type="hidden" name="return_to" value="/job/{esc(job_id)}">
        <button class="btn btn-primary" type="submit">Generate cover letter</button>
      </form>
    </div>
        """

    # ── JD card ──────────────────────────────────────────────────────────────
    jd_text = (job.get("jd_text") or "").strip()
    if jd_text:
        jd_card = f"""
    <div class="card">
      <h2>Job description <span style="color:#888;font-weight:400;font-size:12px">— {len(jd_text):,} chars</span></h2>
      <pre style="max-height:480px;background:#fafaf8;padding:14px;font-size:12px;line-height:1.55">{esc(jd_text)}</pre>
    </div>
        """
    else:
        jd_card = """
    <div class="card">
      <h2>Job description</h2>
      <p style="color:#888;font-size:12px">No JD text on record for this job.</p>
    </div>
        """

    # ── Score breakdown card ────────────────────────────────────────────────
    from config import compute_freshness_bonus  # local import to match render_cl_row pattern
    sponsor_co = co_by_id.get(job.get("company_id"), {}) or {}
    NM = {k: c.native_max for k, c in COMPONENTS.items()}
    rows = [
        ("Stack",       job.get("stack_match_score") or 0,     NM["stack"]),
        ("Seniority",   job.get("seniority_score") or 0,       NM["seniority"]),
        ("Domain",      job.get("domain_fit_score") or 0,      NM["domain"]),
        ("Velocity",    job.get("hiring_velocity_score") or 0, NM["velocity"]),
        ("Freshness",   compute_freshness_bonus(job),          NM["freshness"]),
        ("Sponsorship", sponsor_co.get("sponsorship_score") or 0, NM["sponsorship"]),
        ("Remote fit",  sponsor_co.get("remote_fit") or 0,     NM["remote"]),
    ]
    score_rows_html = "".join(
        f'<tr><td class="comp-label">{esc(label)}</td>'
        f'<td><strong>{value}</strong> / {denom}</td></tr>'
        for label, value, denom in rows
    )
    notes_text = (job.get("score_notes") or "").strip()
    notes_block = (
        f'<details class="cl-notes" style="margin-top:10px"><summary>rationale</summary>'
        f'<div class="cl-notes-body">{esc(notes_text)}</div></details>'
        if notes_text else ''
    )
    score_card = f"""
    <div class="card">
      <h2>Score breakdown <span style="color:#888;font-weight:400;font-size:12px">— composite {score} / {COMPOSITE_MAX}</span></h2>
      <table class="comp-table">{score_rows_html}</table>
      {notes_block}
    </div>
    """

    # ── Company card (recruiter-prep payload) ──────────────────────────────
    company_card = render_company_card(company, return_to=f"/job/{job_id}")

    # ── Assemble (comp + company cards pinned high for interview-stage) ────
    nav = '<p style="margin-bottom:14px"><a href="/pipeline">← Pipeline</a> · <a href="/today">Today</a></p>'
    flash = _flash_notice_html(pop_research_flash())

    if is_interview_stage:
        body = nav + flash + header_card + comp_card + company_card + timeline_card + jd_card + cl_card + score_card
    else:
        body = nav + flash + header_card + company_card + timeline_card + cl_card + comp_card + jd_card + score_card

    # Reuse the cover-letters JS so Copy buttons + form debounce work on this page.
    body += """
<script>
(function () {
  document.querySelectorAll('.cl-copy-path').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var p = btn.getAttribute('data-path') || '';
      if (!p) return;
      navigator.clipboard.writeText(p).then(function () {
        var orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(function () { btn.textContent = orig; }, 1000);
      });
    });
  });
  document.querySelectorAll('.snippet-copy').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var sel = btn.getAttribute('data-target');
      var el  = sel ? document.getElementById(sel) : null;
      if (!el) return;
      var text = (el.value !== undefined) ? el.value : el.textContent;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          var orig = btn.textContent;
          btn.textContent = 'Copied!';
          btn.classList.add('copied');
          setTimeout(function () {
            btn.textContent = orig;
            btn.classList.remove('copied');
          }, 1200);
        });
      }
    });
  });
  document.querySelectorAll('.cl-form').forEach(function (f) {
    f.addEventListener('submit', function () {
      var btns = f.querySelectorAll('button[type=submit]');
      setTimeout(function () { btns.forEach(function (b) { b.disabled = true; }); }, 0);
      setTimeout(function () { btns.forEach(function (b) { b.disabled = false; }); }, 60000);
    });
  });
})();
</script>
"""

    return page(f"{job.get('company_name','?')} — {job.get('title','?')}", body)


def resume_page() -> str:
    from html import escape as esc
    data  = parse_resume_snippets()
    parts = []

    err = data.get("error")
    if err:
        parts.append(f'<div class="notice notice-warn">{esc(err)}</div>')

    parts.append('<div class="card">')
    parts.append(
        '<h2>Experience '
        '<span style="color:#888;font-weight:400;font-size:12px">'
        '— click a row to expand, then copy individual fields'
        '</span></h2>'
    )
    if not data["experience"]:
        parts.append('<p style="color:#888">No experience entries found in profile/resume.md.</p>')
    else:
        for i, exp in enumerate(data["experience"]):
            parts.append(render_experience_entry(i, exp))
    parts.append('</div>')

    parts.append('<div class="card">')
    parts.append('<h2>Education</h2>')
    if not data["education"]:
        parts.append('<p style="color:#888">No education entries found in profile/resume.md.</p>')
    else:
        for i, edu in enumerate(data["education"]):
            parts.append(render_education_entry(i, edu))
    parts.append('</div>')

    parts.append(render_links_card())

    parts.append(_SNIPPETS_JS)

    return page("Resume snippets", "\n".join(parts))


def render_section_body(sid: str, linkedin_view: str = "default") -> str:
    if sid == "status_updates":
        return render_status_updates_body()
    if sid == "crawl":
        return render_crawl_body()
    if sid == "linkedin_ingest":
        return render_linkedin_body(linkedin_view)
    if sid == "cover_letters":
        return render_cover_letters_body()
    return '<p class="section-placeholder">Section content arrives in a later phase.</p>'


RENDER_ROW_CAP = 60


def render_linkedin_body(view: str = "default") -> str:
    """
    view: 'default' | 'all' | 'failing'
        default — show passing rows after pre-filter has run, otherwise all
        all     — show every staged row regardless of pre-filter state
        failing — show only rows that failed pre-filter
    """
    from html import escape as esc

    parts = []

    flash = pop_linkedin_flash()
    if flash:
        cls = {
            "ok":   "notice notice-ok",
            "warn": "notice notice-warn",
            "info": "notice notice-info",
        }.get(flash["kind"], "notice notice-info")
        parts.append(f'<div class="{cls}">{esc(flash["text"])}</div>')

    missing = linkedin_env_missing()
    if missing:
        names = ", ".join(missing)
        parts.append(
            '<div class="notice notice-warn">'
            f'Missing env var(s): <code>{esc(names)}</code>. '
            'For Gmail, generate an app password at '
            '<a href="https://myaccount.google.com/apppasswords" target="_blank">'
            'myaccount.google.com/apppasswords</a> and set '
            '<code>NEXTROLE_IMAP_HOST=imap.gmail.com</code>, '
            '<code>NEXTROLE_IMAP_USER</code>=your address, '
            '<code>NEXTROLE_IMAP_APP_PASSWORD</code>=the app password.'
            '</div>'
        )

    fetch_disabled = ' disabled title="Set IMAP env vars first"' if missing else ""
    parts.append(
        '<div class="linkedin-actions">'
        '<form method="POST" action="/today/linkedin/fetch" style="display:inline">'
        f'<button class="btn btn-primary" style="margin-top:0"{fetch_disabled}>'
        'Fetch LinkedIn alerts</button>'
        '</form>'
        '<form method="POST" action="/today/linkedin/prefilter" style="display:inline">'
        '<button class="btn btn-secondary" style="margin-top:0">Run pre-filter</button>'
        '</form>'
        '</div>'
    )

    staged = load_staged_emails()
    if not staged:
        parts.append(
            '<p class="section-placeholder">'
            'No staged jobs. Click <strong>Fetch LinkedIn alerts</strong> to pull '
            'unread emails from senders in <code>data/email_config.json</code>.'
            '</p>'
        )
        return "\n".join(parts)

    has_filter_run = any("_prefilter_pass" in r for r in staged)
    n_pass         = sum(1 for r in staged if r.get("_prefilter_pass") is True)
    n_fail         = sum(1 for r in staged if r.get("_prefilter_pass") is False)
    n_unfiltered   = sum(1 for r in staged if "_prefilter_pass" not in r)

    summary_bits = [f"{len(staged)} total"]
    if has_filter_run:
        summary_bits.append(f"<strong>{n_pass} passing</strong>")
        summary_bits.append(f"{n_fail} failing")
        if n_unfiltered:
            summary_bits.append(f"{n_unfiltered} unfiltered")
    summary = " · ".join(summary_bits)

    if has_filter_run:
        view_links = []
        for label, vname in (("Passing", "default"), ("Failing", "failing"), ("All", "all")):
            if vname == view or (view == "default" and vname == "default"):
                view_links.append(f'<strong>{label}</strong>')
            else:
                qs = "linkedin_ingest"
                if vname != "default":
                    qs += f"&view={vname}"
                view_links.append(f'<a href="/today?open={qs}">{label}</a>')
        view_html = " · ".join(view_links)
    else:
        view_html = ""

    bulk_html = ""
    if has_filter_run and n_fail > 0:
        bulk_html = (
            '<form method="POST" action="/today/linkedin/discard_failing" '
            f'onsubmit="return confirm(\'Discard {n_fail} failing row(s)?\')" '
            'style="display:inline;margin-left:auto">'
            f'<button class="btn btn-secondary" style="margin-top:0;font-size:11px;padding:4px 10px">'
            f'Discard {n_fail} failing</button>'
            '</form>'
        )

    parts.append(
        '<div class="staged-summary">'
        f'<span class="staged-count">{summary}</span>'
        f'{("&nbsp;&nbsp;|&nbsp;&nbsp;" + view_html) if view_html else ""}'
        f'{bulk_html}'
        '</div>'
    )

    if view == "all":
        visible = list(staged)
    elif view == "failing":
        visible = [r for r in staged if r.get("_prefilter_pass") is False]
    else:  # default
        if has_filter_run:
            visible = [r for r in staged if r.get("_prefilter_pass") is not False]
        else:
            visible = list(staged)

    if not visible:
        parts.append('<p class="section-placeholder">Nothing to show in this view.</p>')
        return "\n".join(parts)

    capped  = visible[:RENDER_ROW_CAP]
    overage = len(visible) - len(capped)

    parts.append('<div class="staged-list">')
    for row in capped:
        parts.append(render_staged_row(row))
    parts.append('</div>')

    if overage > 0:
        parts.append(
            f'<p style="color:#888;font-size:11px;margin-top:8px">'
            f'Showing first {len(capped)} of {len(visible)} rows. '
            f'Discard, ingest, or pre-filter further to see the rest.'
            '</p>'
        )

    parts.append("""
<script>
(function () {
  // 3s submit-debounce on staged-row forms — guards against double-clicks on
  // Fetch JD / Ingest / Discard while the server is still processing the first.
  document.querySelectorAll('.staged-form').forEach(function (f) {
    f.addEventListener('submit', function () {
      var btns = f.querySelectorAll('button');
      setTimeout(function () { btns.forEach(function (b) { b.disabled = true; }); }, 0);
      setTimeout(function () { btns.forEach(function (b) { b.disabled = false; }); }, 3000);
    });
  });
})();
</script>
""")

    return "\n".join(parts)


def render_staged_row(row: dict) -> str:
    from html import escape as esc

    sid       = esc(row.get("staging_id", ""))
    company   = esc(row.get("company", ""))
    title     = esc(row.get("title", ""))
    location  = esc(row.get("location", ""))
    apply_url = esc(row.get("apply_url", ""))
    subject   = esc(row.get("source_subject", ""))[:120]
    jd_text   = row.get("jd_text", "") or ""

    pf_pass   = row.get("_prefilter_pass")
    pf_reason = esc(str(row.get("_prefilter_reason", "")))[:80]
    if pf_pass is True:
        pf_badge = f'<span class="pf-badge pf-pass">✓ {pf_reason}</span>'
    elif pf_pass is False:
        pf_badge = f'<span class="pf-badge pf-fail">✗ {pf_reason}</span>'
    else:
        pf_badge = ""

    if jd_text and len(jd_text) >= MIN_JD_LENGTH:
        jd_label    = f'Job description <span class="jd-status jd-ok">{len(jd_text)} chars — review and edit if needed</span>'
        jd_textarea = f'<textarea name="jd_text" rows="8">{esc(jd_text)}</textarea>'
        fetch_jd_btn = ""
    else:
        jd_label = (
            'Job description <span class="jd-status jd-empty">empty — '
            'click <strong>Fetch JD</strong> to auto-load, or paste manually</span>'
        )
        jd_textarea = (
            f'<textarea name="jd_text" rows="6" '
            f'placeholder="Paste JD here, or click Fetch JD. {MIN_JD_LENGTH}+ chars required."></textarea>'
        )
        fetch_jd_btn = (
            '<button class="btn-mini" type="submit" '
            'formaction="/today/linkedin/fetchjd" '
            'formnovalidate>Fetch JD</button>'
        )

    return f"""
    <div class="staged-row" id="row-{sid}">
      <form method="POST" action="/today/linkedin/ingest" class="staged-form">
        <input type="hidden" name="staging_id" value="{sid}">
        <div class="staged-row-head">{pf_badge}</div>
        <div class="staged-grid">
          <div>
            <label>Company</label>
            <input name="company" value="{company}">
          </div>
          <div>
            <label>Title</label>
            <input name="title" value="{title}">
          </div>
          <div>
            <label>Location</label>
            <input name="location" value="{location}">
          </div>
          <div>
            <label>Posted</label>
            <input name="posted" placeholder="YYYY-MM-DD (optional)">
          </div>
        </div>
        <label>Apply URL <a href="{apply_url}" target="_blank" class="apply-link">open ↗</a></label>
        <input name="apply_url" value="{apply_url}">
        <label>{jd_label}</label>
        {jd_textarea}
        <div class="staged-actions">
          <button class="btn btn-primary" style="margin-top:0">Ingest</button>
          {fetch_jd_btn}
          <button class="btn btn-secondary" formaction="/today/linkedin/discard" style="margin-top:0">Discard</button>
          <span class="staged-source">from: {subject}</span>
        </div>
      </form>
    </div>
    """


def render_crawl_body() -> str:
    from html import escape as esc
    s     = crawl_status_payload()
    state = s["state"]

    if state == "idle":
        return (
            '<form method="POST" action="/today/crawl/start">'
            '<button class="btn btn-primary" style="margin-top:0">Run crawl</button>'
            '</form>'
        )

    if state == "running":
        badge = '<span class="crawl-badge crawl-badge-running">Running…</span>'
    elif state == "done":
        n     = s["ingested"] or 0
        badge = f'<span class="crawl-badge crawl-badge-done">✓ {n} new job{"" if n == 1 else "s"} ingested</span>'
    else:  # error
        err   = esc(s["error"] or "see output")
        badge = f'<span class="crawl-badge crawl-badge-error">✗ {err}</span>'

    elapsed = f'<span class="crawl-elapsed">{s["elapsed_s"] or 0}s</span>'
    tail    = "\n".join(s["tail"]) or "(no output yet)"
    pre     = f'<pre class="crawl-tail">{esc(tail)}</pre>'

    rerun = ""
    if state in ("done", "error"):
        rerun = (
            '<form method="POST" action="/today/crawl/start" style="margin-top:10px">'
            '<button class="btn btn-secondary" style="margin-top:0">Run again</button>'
            '</form>'
        )

    poll_js = ""
    if state == "running":
        poll_js = """
<script>
(function () {
  var el = document.getElementById('crawl-section-status');
  if (!el) return;
  function poll() {
    fetch('/today/crawl/status')
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.state !== 'running') { window.location.reload(); return; }
        var t = el.querySelector('.crawl-elapsed');
        if (t) t.textContent = (j.elapsed_s || 0) + 's';
        var pre = el.querySelector('.crawl-tail');
        if (pre) pre.textContent = (j.tail || []).join('\\n') || '(no output yet)';
        setTimeout(poll, 2000);
      })
      .catch(function () { setTimeout(poll, 4000); });
  }
  setTimeout(poll, 2000);
})();
</script>"""

    return (
        f'<div id="crawl-section-status" data-state="{state}">'
        f'<div class="crawl-status">{badge}{elapsed}</div>'
        f'{pre}{rerun}{poll_js}'
        f'</div>'
    )


def render_status_updates_body() -> str:
    apps = load_applications()
    in_flight = [
        a for a in apps
        if a.get("status") not in ("rejected", "offer", "withdrawn")
    ]
    if not in_flight:
        return '<p style="color:#888;font-size:13px">No applications need check-in.</p>'
    in_flight.sort(key=lambda a: a.get("date_applied", ""))
    rows = "".join(render_app_row(a) for a in in_flight)
    return f'<div class="app-list">{rows}</div>'


def render_app_row(app: dict) -> str:
    from html import escape as esc
    company    = esc(app.get("company_name", "?"))[:30]
    title      = esc(app.get("title", ""))[:60]
    applied    = app.get("date_applied", "")
    days_label = f"Applied {days_since_iso(applied)}d ago" if applied else "Applied —"
    status     = app.get("status", "applied")
    status_lbl = status.replace("_", " ")
    app_id     = esc(app.get("application_id", ""))

    return f"""
    <div class="app-row">
      <div class="app-meta">
        <span class="app-company">{company}</span>
        <span class="app-title">{title}</span>
        <span class="app-applied">{days_label}</span>
        <span class="app-status app-status-{status}">{status_lbl}</span>
      </div>
      <form method="POST" action="/today/status" class="app-buttons">
        <input type="hidden" name="app_id" value="{app_id}">
        <button class="btn-status" type="submit" name="action" value="recruiter_screen">Recruiter screen</button>
        <button class="btn-status" type="submit" name="action" value="interview">Interview request</button>
        <button class="btn-status" type="submit" name="action" value="rejected_generic">Rejected (generic)</button>
        <button class="btn-status" type="submit" name="action" value="rejected_position_filled">Rejected (position filled)</button>
        <button class="btn-status" type="submit" name="action" value="offer">Offer</button>
        <button class="btn-status" type="submit" name="action" value="withdrawn">Withdrawn</button>
      </form>
    </div>
    """


def render_cover_letters_body() -> str:
    """
    Cover letters & apply section. Top CL_RENDER_CAP eligible rows by partial
    composite score; each row has state-dependent buttons:

      active (CL not yet generated):
        [Generate CL]  [Open Apply]  [Mark Applied]  [Archive]

      cover_letter_ready (CL generated):
        [Open]  [Copy Path]  [Open Apply]  [Regenerate]  [Mark Applied]  [Archive]
    """
    from html import escape as esc

    parts = []

    flash = pop_cl_flash()
    if flash:
        cls = {
            "ok":   "notice notice-ok",
            "warn": "notice notice-warn",
            "info": "notice notice-info",
        }.get(flash["kind"], "notice notice-info")
        parts.append(f'<div class="{cls}">{esc(flash["text"])}</div>')

    research_notice = _flash_notice_html(pop_research_flash())
    if research_notice:
        parts.append(research_notice)

    jobs     = load_pipeline()
    co_by_id = load_companies_by_id()
    apps     = load_applications()

    # Status-side eligibility (job has not yet been applied/archived).
    raw_eligible = [
        j for j in jobs
        if j.get("pipeline_status") in ("active", "cover_letter_ready")
    ]

    # Company-side eligibility (≤ MAX_ACTIVE_APPS_PER_COMPANY in-flight at
    # the company). The rule lives in config.company_block_reason — do not
    # reimplement it here. See SCORING/COMPANY-FILTER SSOT banners in
    # scripts/config.py.
    eligible   = []
    suppressed = 0
    for j in raw_eligible:
        if company_block_reason(j.get("company_id"), apps):
            suppressed += 1
            continue
        eligible.append(j)
    eligible.sort(key=lambda j: job_score(j, co_by_id), reverse=True)

    if not eligible:
        parts.append(
            '<p class="section-placeholder">'
            'No eligible jobs in the pipeline. Run the crawl or LinkedIn ingest to add some.'
            '</p>'
        )
        return "\n".join(parts)

    visible = eligible[:CL_RENDER_CAP]
    overage = len(eligible) - len(visible)

    n_ready = sum(1 for j in eligible if j.get("pipeline_status") == "cover_letter_ready")
    n_new   = len(eligible) - n_ready
    suppressed_html = (
        f' · <span class="staged-count">{suppressed} suppressed '
        f'(≥{MAX_ACTIVE_APPS_PER_COMPANY} active apps at company)</span>'
        if suppressed else ''
    )
    parts.append(
        '<div class="staged-summary">'
        f'<span class="staged-count">{n_new} need CL · {n_ready} CL ready to apply · {len(eligible)} eligible</span>'
        f'{suppressed_html}'
        '</div>'
    )

    comp_by_job = load_comp_estimates_by_job()

    parts.append('<div class="cl-list">')
    for job in visible:
        parts.append(render_cl_row(job, co_by_id, comp_by_job.get(job.get("job_id", ""))))
    parts.append('</div>')

    if overage > 0:
        parts.append(
            f'<p style="color:#888;font-size:11px;margin-top:8px">'
            f'Showing top {len(visible)} of {len(eligible)} by composite score. '
            f'Generate/apply/archive rows to surface the next batch.'
            '</p>'
        )

    # Clipboard handler + form-submit debounce. Wrapped in an IIFE to avoid
    # leaking globals; re-attaches every render since the page reloads on each form post.
    parts.append("""
<script>
(function () {
  document.querySelectorAll('.cl-copy-path').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var p = btn.getAttribute('data-path') || '';
      if (!p) return;
      navigator.clipboard.writeText(p).then(function () {
        var orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(function () { btn.textContent = orig; }, 1000);
      }, function () {
        btn.textContent = 'Copy failed';
        setTimeout(function () { btn.textContent = 'Copy Path'; }, 1500);
      });
    });
  });
  document.querySelectorAll('.snippet-copy').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var sel = btn.getAttribute('data-target');
      var el  = sel ? document.getElementById(sel) : null;
      if (!el) return;
      var text = (el.value !== undefined) ? el.value : el.textContent;
      var ok = function () {
        var orig = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(function () {
          btn.textContent = orig;
          btn.classList.remove('copied');
        }, 1200);
      };
      var fail = function () {
        var orig = btn.textContent;
        btn.textContent = 'Copy failed';
        setTimeout(function () { btn.textContent = orig; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(ok, fail);
      } else { fail(); }
    });
  });
  document.querySelectorAll('.cl-form').forEach(function (f) {
    f.addEventListener('submit', function () {
      var btns = f.querySelectorAll('button[type=submit]');
      setTimeout(function () { btns.forEach(function (b) { b.disabled = true; }); }, 0);
      // Generate CL / comp estimate take ~30s; leave buttons disabled longer.
      setTimeout(function () { btns.forEach(function (b) { b.disabled = false; }); }, 60000);
    });
  });
})();
</script>
""")

    return "\n".join(parts)


def _fmt_currency(value, currency: str) -> str:
    if value is None:
        return ""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{currency} {n:,}"


_COMP_BADGE_CLASS = {
    "Expected":      "comp-badge-expected",
    "Possible":      "comp-badge-possible",
    "Stated-in-JD":  "comp-badge-statedinjd",
    "Unusual":       "comp-badge-unusual",
}


def render_comp_panel(comp_record: dict, job_id: str) -> str:
    """Render the inline compensation-estimate panel for one job row."""
    from html import escape as esc

    est       = comp_record.get("estimate", {}) or {}
    currency  = est.get("currency", "USD")
    base      = est.get("base", {}) or {}
    confidence = est.get("confidence", "")
    reasoning = est.get("reasoning", "") or ""

    bonus_specs = [
        # (key,             label,            target field name,  short suffix)
        ("year_end_bonus", "Year-end bonus", "target_amount",    ""),
        ("signon",         "Sign-on bonus",  "target",           ""),
        ("relocation",     "Relocation",     "target",           ""),
        ("equity",         "Equity / RSU",   "target_annual",    "/yr"),
    ]

    # ── Build the one-line summary (target-asks for visible items only) ──────
    summary_bits = []
    base_target = base.get("target")
    if base_target:
        summary_bits.append(f"{currency} {int(base_target):,} base")
    for key, label, tgt_key, suffix in bonus_specs:
        comp = est.get(key, {}) or {}
        if comp.get("classification") == "Unusual":
            continue
        v = comp.get(tgt_key)
        if v is None:
            continue
        short_label = label.lower().replace(" bonus", "").replace(" / rsu", "")
        summary_bits.append(f"{currency} {int(v):,} {short_label}{suffix}")
    summary_text = " + ".join(summary_bits) if summary_bits else "(estimate has no actionable targets)"

    # ── Base salary row ──────────────────────────────────────────────────────
    base_min = _fmt_currency(base.get("min"), currency)
    base_max = _fmt_currency(base.get("max"), currency)
    base_tgt_fmt = _fmt_currency(base_target, currency)

    base_row_html = f"""
      <tr>
        <td class="comp-label">Base salary</td>
        <td class="comp-range">{esc(base_min)} – {esc(base_max)}</td>
        <td class="comp-target">TARGET: <span id="comp-base-{esc(job_id)}">{esc(base_tgt_fmt)}</span></td>
        <td><button class="snippet-copy btn-mini" type="button" data-target="comp-base-{esc(job_id)}">Copy</button></td>
      </tr>
    """

    # ── Bonus rows (Expected / Possible / Stated-in-JD visible) ─────────────
    visible_rows = []
    unusual_items = []
    for key, label, tgt_key, suffix in bonus_specs:
        comp = est.get(key, {}) or {}
        cls  = comp.get("classification", "")
        reason = (comp.get("reason") or "").strip()
        if cls == "Unusual":
            unusual_items.append((label, reason))
            continue

        badge_class = _COMP_BADGE_CLASS.get(cls, "")
        badge_html  = f'<span class="comp-badge {badge_class}">{esc(cls)}</span>'

        # Build value display
        target_amount = comp.get(tgt_key)
        if key == "year_end_bonus":
            pct = comp.get("target_pct")
            pct_str = f"~{pct}%" if pct else ""
            amt_fmt = _fmt_currency(target_amount, currency)
            value_display = " ".join(s for s in [pct_str, f"(~{amt_fmt})" if amt_fmt and pct_str else amt_fmt] if s)
        else:
            value_display = _fmt_currency(target_amount, currency)

        copy_id = f"comp-{key}-{esc(job_id)}"
        if target_amount is not None:
            copy_btn = f'<button class="snippet-copy btn-mini" type="button" data-target="{copy_id}">Copy</button>'
            target_cell = f'<span id="{copy_id}">{esc(_fmt_currency(target_amount, currency))}</span>'
        else:
            copy_btn = ""
            target_cell = '<span style="color:#888">—</span>'

        visible_rows.append(f"""
      <tr>
        <td class="comp-label">{esc(label)}</td>
        <td class="comp-range">{badge_html}{esc(value_display) if value_display else ""}<br><span class="comp-reason">{esc(reason)}</span></td>
        <td class="comp-target">{target_cell}</td>
        <td>{copy_btn}</td>
      </tr>
        """)

    # ── Unusual (collapsed by default; "Show all" toggle) ────────────────────
    unusual_html = ""
    if unusual_items:
        items_html = "".join(
            f'<div class="comp-unusual-item"><strong>{esc(label)}</strong> · '
            f'<span class="comp-badge comp-badge-unusual">Unusual</span> '
            f'<span class="comp-reason">{esc(reason)}</span></div>'
            for label, reason in unusual_items
        )
        skipped_names = ", ".join(label for label, _ in unusual_items)
        unusual_html = f"""
      <details class="comp-unusual">
        <summary>Not customary for this role — skipped: {esc(skipped_names.lower())} ({len(unusual_items)})</summary>
        <div class="comp-unusual-body">{items_html}</div>
      </details>
        """

    # ── Confidence + reasoning footer ────────────────────────────────────────
    conf_class = {"HIGH": "comp-conf-high", "MED": "comp-conf-med",
                  "LOW": "comp-conf-low"}.get(confidence, "")
    generated_at = comp_record.get("generated_at", "")
    stale_marker = (
        f'<span class="comp-stale">generated {esc(generated_at[:10])}</span>'
        if generated_at else ""
    )
    footer_html = f"""
      <div class="comp-footer">
        <span class="comp-conf {conf_class}">Confidence: {esc(confidence)}</span>
        <span class="comp-reasoning">{esc(reasoning)}</span>
        {stale_marker}
      </div>
    """

    summary_id = f"comp-summary-{esc(job_id)}"
    return f"""
    <div class="comp-panel">
      <div class="comp-summary-bar">
        <span class="comp-summary-label">Target ask</span>
        <span class="comp-summary-value" id="{summary_id}">{esc(summary_text)}</span>
        <button class="snippet-copy btn-mini" type="button" data-target="{summary_id}">Copy</button>
      </div>
      <table class="comp-table">
        {base_row_html}
        {''.join(visible_rows)}
      </table>
      {unusual_html}
      {footer_html}
    </div>
    """


def render_cl_row(job: dict, co_by_id: dict | None = None,
                  comp_record: dict | None = None) -> str:
    from html import escape as esc

    job_id    = esc(job.get("job_id", ""))
    company   = esc(job.get("company_name", "?"))[:30]
    title     = esc(job.get("title", "?"))[:60]
    location  = esc(job.get("location", ""))[:35]
    apply_url = esc(job.get("apply_url", ""))
    score     = job_score(job, co_by_id or {})
    pre_score = composite_score_pre_research(job)

    # Is the company a stub? Stub records are inflating the full composite
    # with default sponsorship/remote values; surface that to the operator
    # so they can choose to research before applying.
    company_rec = (co_by_id or {}).get(job.get("company_id")) if co_by_id else None
    is_stub     = bool(company_rec and company_rec.get("stub"))

    cl_done    = bool(job.get("cover_letter_generated"))
    cl_version = job.get("cover_letter_version", 0)
    cl_relpath = job.get("cover_letter_path", "") or ""
    cl_abspath = str(ROOT / cl_relpath) if cl_relpath else ""
    cl_filename = cl_relpath.split("/")[-1] if cl_relpath else ""

    state_pill = (
        f'<span class="pf-badge pf-pass">CL v{cl_version} ready</span>'
        if cl_done
        else '<span class="pf-badge" style="background:#fef3cd;color:#7a4f00">needs CL</span>'
    )

    apply_link = (
        f'<a class="apply-link" href="{apply_url}" target="_blank" rel="noopener">Open Apply ↗</a>'
        if apply_url else ''
    )
    detail_link = f'<a class="apply-link" href="/job/{job_id}" style="margin-left:6px">Details →</a>'

    # ── Score breakdown ──────────────────────────────────────────────────────
    # Denominators read from the SSOT (scripts/config.py:COMPONENTS) so they
    # stay correct if weights ever change. Shows the native (raw) stored
    # value for each component; the row's headline score is the weighted
    # full composite via composite_score(). Pre-research composite is shown
    # alongside for transparency — and is the only meaningful score when
    # the company is a stub.
    from config import compute_freshness_bonus  # local import to avoid cycle at top
    sponsor_co = company_rec or {}
    stack_s     = job.get("stack_match_score")        or 0
    seniority   = job.get("seniority_score")          or 0
    domain      = job.get("domain_fit_score")         or 0
    velocity    = job.get("hiring_velocity_score")    or 0
    freshness   = compute_freshness_bonus(job)
    sponsorship = sponsor_co.get("sponsorship_score") or 0
    remote      = sponsor_co.get("remote_fit")        or 0
    staleness   = esc(job.get("staleness_status", "") or "")
    notes       = (job.get("score_notes") or "").strip()

    NM = {k: c.native_max for k, c in COMPONENTS.items()}
    breakdown_line = (
        '<div class="cl-breakdown">'
        f'<span>Stack <strong>{stack_s}</strong>/{NM["stack"]}</span> · '
        f'<span>Sen <strong>{seniority}</strong>/{NM["seniority"]}</span> · '
        f'<span>Dom <strong>{domain}</strong>/{NM["domain"]}</span> · '
        f'<span>Vel <strong>{velocity}</strong>/{NM["velocity"]}</span> · '
        f'<span>Fresh <strong>{freshness}</strong>/{NM["freshness"]}</span> · '
        f'<span>Spons <strong>{sponsorship}</strong>/{NM["sponsorship"]}</span> · '
        f'<span>Rem <strong>{remote}</strong>/{NM["remote"]}</span>'
        f'{f" · <span>staleness: {staleness}</span>" if staleness else ""}'
        '</div>'
    )

    notes_block = ""
    if notes:
        notes_block = (
            '<details class="cl-notes">'
            '<summary>rationale</summary>'
            f'<div class="cl-notes-body">{esc(notes)}</div>'
            '</details>'
        )

    # Generate vs Regenerate button text changes; same endpoint.
    gen_label = "Regenerate" if cl_done else "Generate CL"
    gen_class = "btn-mini" if cl_done else "btn btn-primary"
    gen_style = "margin-top:0" if cl_done else "margin-top:0"

    cl_file_block = ""
    open_copy = ""
    if cl_done:
        cl_file_block = (
            f'<div class="cl-file-line">'
            f'<span class="cl-file-name">{esc(cl_filename)}</span>'
            f'</div>'
        )
        open_copy = (
            f'<form method="POST" action="/today/cl/open" class="cl-form" style="display:inline">'
            f'<input type="hidden" name="job_id" value="{job_id}">'
            f'<button class="btn-mini" type="submit">Open</button>'
            f'</form>'
            f'<button class="btn-mini cl-copy-path" type="button" '
            f'data-path="{esc(cl_abspath)}">Copy Path</button>'
        )

    comp_done   = comp_record is not None
    comp_label  = "Re-estimate Comp" if comp_done else "Estimate Comp"
    comp_panel  = render_comp_panel(comp_record, job_id) if comp_done else ""

    # ── Score pills ──────────────────────────────────────────────────────────
    # Researched company: full composite is the primary, pre-research is shown
    # alongside as context.
    # Stub company: full composite is unreliable (sponsorship + remote at stub
    # defaults), so de-emphasize it and surface pre-research as primary plus
    # a "research pending" badge so the operator knows to research first.
    if is_stub:
        stub_company_id = esc(company_rec.get("company_id", "")) if company_rec else ""
        research_form = (
            f'<form method="POST" action="/today/company/research" class="cl-form" '
            f'style="display:inline;margin:0">'
            f'<input type="hidden" name="company_id" value="{stub_company_id}">'
            f'<button class="btn-mini pf-stub" type="submit" '
            f'title="Run Haiku research now (~20–30s). Sponsorship + remote-fit '
            f'are at stub defaults until this completes.">research now</button>'
            f'</form>'
        ) if stub_company_id else (
            f'<span class="pf-badge pf-stub" title="Sponsorship + remote-fit are at '
            f'stub defaults; run --research-queue to refine.">research pending</span>'
        )
        score_block = (
            f'<span class="score" title="Pre-research composite — ranks only on signals '
            f'available before company research.">{pre_score}<span class="score-suffix">'
            f' /{PRE_RESEARCH_MAX} pre</span></span>'
            f'<span class="score-secondary" title="Full composite — unreliable until '
            f'company is researched (sponsorship + remote at stub defaults).">'
            f'{score}/{COMPOSITE_MAX} full*</span>'
            + research_form
        )
    else:
        score_block = (
            f'<span class="score" title="Full composite — apply-time ranking signal.">'
            f'{score}<span class="score-suffix"> /{COMPOSITE_MAX}</span></span>'
            f'<span class="score-secondary" title="Pre-research composite (for '
            f'context — this is what the research queue ranks by).">'
            f'{pre_score}/{PRE_RESEARCH_MAX} pre</span>'
        )

    return f"""
    <div class="cl-row" id="cl-{job_id}">
      <div class="cl-meta">
        {score_block}
        <span class="company">{company}</span>
        <span class="title-cell">{title}</span>
        <span class="cl-location">{location}</span>
        {state_pill}
      </div>
      {breakdown_line}
      {notes_block}
      {cl_file_block}
      {comp_panel}
      <div class="cl-actions">
        <form method="POST" action="/today/cl/generate" class="cl-form" style="display:inline">
          <input type="hidden" name="job_id" value="{job_id}">
          <button class="{gen_class}" type="submit" style="{gen_style}">{gen_label}</button>
        </form>
        <form method="POST" action="/today/comp/estimate" class="cl-form" style="display:inline">
          <input type="hidden" name="job_id" value="{job_id}">
          <button class="btn-mini" type="submit">{comp_label}</button>
        </form>
        <a class="btn-mini" href="/answer-questions?job_id={job_id}"
           style="text-decoration:none;display:inline-flex;align-items:center">Answer Questions</a>
        {open_copy}
        {apply_link}
        {detail_link}
        <form method="POST" action="/today/apply/log" class="cl-form" style="display:inline;margin-left:auto">
          <input type="hidden" name="job_id" value="{job_id}">
          <button class="btn-mini" type="submit">Mark Applied</button>
        </form>
        <form method="POST" action="/today/cl/archive" class="cl-form" style="display:inline">
          <input type="hidden" name="job_id" value="{job_id}">
          <button class="btn-mini" type="submit">Archive</button>
        </form>
      </div>
    </div>
    """


# ── /answer-questions — ad-hoc application question answers ──────────────────
#
# Full page surfaced from the cover-letters apply queue. Driven by
# scripts/answer_questions.py for all generation/persistence; the renderer
# below only assembles HTML. POST handlers below return JSON
# ({"ok": bool, "card_html": str|None, "error": str|None}) so the client can
# swap a single card's outerHTML without a full reload — generate is too slow
# (15-30s Sonnet call) to tolerate a reload-on-every-action UX.

def _aq_chip_html(slug: str, label: str) -> str:
    from html import escape as esc
    return (
        f'<span class="aq-chip" data-slug="{esc(slug)}" title="{esc(label)}">'
        f'<span class="aq-chip-label">{esc(slug)}</span>'
        f'<button type="button" class="aq-chip-remove" aria-label="Remove">×</button>'
        f'</span>'
    )


def _aq_card_html(job_id: str, question: dict) -> str:
    """Render a single question card. Used both for initial page render and
    for the JSON response after any mutation so the client can swap
    outerHTML with the freshest server-rendered card."""
    from html import escape as esc
    from config import RESUME_ENTRY_SLUGS

    qid     = esc(question.get("question_id", ""))
    qtext   = esc(question.get("question_text", ""))
    qclass  = esc(question.get("question_class", ""))
    cap     = question.get("char_cap")
    status  = question.get("status", "draft")
    is_final = status == "finalized"
    override = esc(question.get("question_override_notes", "") or "")
    history  = question.get("draft_history") or []
    used     = question.get("resume_entries_used") or []

    cap_label = f"{cap} char cap" if cap else "no cap"
    status_cls = "pf-pass" if is_final else "pf-badge"
    status_lbl = "finalized" if is_final else f"draft (v{len(history)})" if history else "draft (empty)"

    chips_html = "".join(
        _aq_chip_html(slug, RESUME_ENTRY_SLUGS.get(slug, slug))
        for slug in used
    )
    add_options = "".join(
        f'<option value="{esc(slug)}">{esc(slug)} — {esc(label)}</option>'
        for slug, label in RESUME_ENTRY_SLUGS.items()
        if slug not in used
    )
    chip_picker = (
        f'<select class="aq-chip-add"><option value="">+ add entry</option>{add_options}</select>'
        if add_options else ""
    )

    # Embed full draft history as JSON for client-side version switching.
    history_json = esc(json.dumps([
        {"v": h.get("version"), "answer": h.get("answer", ""),
         "chars": h.get("char_count", 0), "at": h.get("generated_at", "")}
        for h in history
    ]))

    if history:
        latest = history[-1]
        latest_answer = latest.get("answer", "")
        latest_chars  = latest.get("char_count", 0)
        cap_warn = ""
        if cap and latest_chars > cap:
            cap_warn = f' <span class="pf-fail">over by {latest_chars - cap}</span>'
        version_opts = "".join(
            f'<option value="{h.get("version")}"{" selected" if h is latest else ""}>'
            f'v{h.get("version")} ({h.get("char_count", 0)} chars'
            f'{" · edit" if h.get("source") == "manual_edit" else ""})</option>'
            for h in history
        )
        version_picker = (
            f'<select class="aq-version-picker">{version_opts}</select>'
            f' <span class="aq-version-meta">{latest_chars} chars{cap_warn}</span>'
        )
        # Editable so the operator can tweak wording inline. Save runs the
        # text through sanitize_answer_text and appends a new draft version
        # (source=manual_edit) — never mutates a prior version.
        answer_block = (
            f'<textarea class="aq-answer-text">{esc(latest_answer)}</textarea>'
        )
        copy_btn      = '<button type="button" class="btn-mini aq-copy">Copy</button>'
        save_edit_btn = (
            '<button type="button" class="btn-mini aq-save-edit" disabled '
            'title="Save edits as a new draft version">Save edit</button>'
        )
        gen_label = "Regenerate"
    else:
        version_picker = '<span style="color:#888;font-size:11px">no drafts yet</span>'
        answer_block = (
            '<div class="aq-empty-answer">'
            'No answer generated yet. Click <strong>Generate</strong> to produce a draft.'
            '</div>'
        )
        copy_btn      = ""
        save_edit_btn = ""
        gen_label     = "Generate"

    if is_final:
        finalized_text = esc(question.get("finalized_answer") or "")
        final_block = (
            '<div class="aq-finalized">'
            '<div class="aq-finalized-label">Finalized answer (locked):</div>'
            f'<textarea class="aq-finalized-text" readonly>{finalized_text}</textarea>'
            '<button type="button" class="btn-mini aq-copy-finalized">Copy finalized</button>'
            '</div>'
        )
        finalize_btn = '<button type="button" class="btn-mini aq-unfinalize">Unfinalize</button>'
        delete_btn   = ''
    else:
        final_block  = ''
        finalize_btn = (
            '<button type="button" class="btn-mini aq-finalize">Finalize</button>'
            if history else ''
        )
        delete_btn   = '<button type="button" class="btn-mini aq-delete">Delete</button>'

    return f"""
<div class="aq-card" data-question-id="{qid}" data-job-id="{esc(job_id)}" data-class="{qclass}">
  <div class="aq-card-head">
    <span class="aq-class pf-badge">{qclass}</span>
    <span class="aq-cap">{cap_label}</span>
    <span class="app-status {status_cls}">{status_lbl}</span>
  </div>
  <div class="aq-question-text">{qtext}</div>

  <div class="aq-section">
    <div class="section-label">Resume entries used</div>
    <div class="aq-chips" data-question-id="{qid}">{chips_html}{chip_picker}</div>
  </div>

  <div class="aq-section">
    <div class="section-label">For this question only (one-shot notes)</div>
    <textarea class="aq-override-text"
              placeholder="Per-question hints — e.g. 'Lead with Jailer here.'"
              >{override}</textarea>
  </div>

  <div class="aq-section">
    <div class="aq-controls">
      <button type="button" class="btn btn-primary aq-generate">{gen_label}</button>
      {version_picker}
      {save_edit_btn}
      {copy_btn}
      {finalize_btn}
      {delete_btn}
    </div>
    <div class="aq-history" data-history='{history_json}'></div>
    <div class="aq-answer-wrap">{answer_block}</div>
    {final_block}
  </div>
</div>
"""


def render_answer_questions_page(job_id: str) -> str:
    """Full page: motivation + behavioral question lists for one job, plus
    the global resume-entry-notes editor."""
    from html import escape as esc

    # Lazy-import so the answer_questions module's config-time API-key check
    # doesn't fire on server startup (matches the pattern other scripts/
    # modules follow in this file).
    sys.path.insert(0, str(SCRIPTS))
    import answer_questions as aq
    from config import RESUME_ENTRY_SLUGS

    jobs = load_pipeline()
    job  = next((j for j in jobs if j.get("job_id") == job_id), None)
    if not job:
        return page("Not found", "<div class='card'><h2>Job not found</h2></div>")

    co_by_id = load_companies_by_id()
    company  = co_by_id.get(job.get("company_id")) or {}

    buckets     = aq.get_job_questions(job_id)
    entry_notes = aq.load_entry_notes()

    title    = esc(job.get("title", "?"))
    company_name = esc(job.get("company_name") or company.get("name") or "?")
    score    = job_score(job, co_by_id)
    location = esc(job.get("location", "") or "")

    def render_bucket(class_key: str, heading: str, hint: str) -> str:
        cards = "".join(_aq_card_html(job_id, q) for q in buckets.get(class_key, []))
        empty = (
            '<p style="color:#888;font-size:12px;margin:6px 0 0 0">'
            'No questions yet — paste one above and click Add.'
            '</p>'
        ) if not buckets.get(class_key) else ""
        return f"""
<div class="card">
  <h2>{heading}</h2>
  <p style="color:#666;font-size:12px;margin-top:-4px">{hint}</p>
  <form class="aq-add-form" data-class="{class_key}" data-job-id="{esc(job_id)}">
    <textarea name="question_text" class="aq-new-text" rows="2"
              placeholder="Paste the question text here…" required></textarea>
    <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
      <input type="number" name="char_cap" class="aq-new-cap"
             placeholder="char cap (optional)" min="100" max="5000"
             style="width:200px">
      <button type="submit" class="btn-mini">Add question</button>
    </div>
  </form>
  <div class="aq-list" data-class="{class_key}">{cards}{empty}</div>
</div>
"""

    motivation_section = render_bucket(
        "motivation",
        "Why this company / role? (Motivation)",
        "Connect a specific thing in the JD to a specific thing in the resume. "
        "No emotional or culture claims.",
    )
    behavioral_section = render_bucket(
        "behavioral",
        "Behavioral / Experience (Describe a time you…)",
        "One project per answer. Build-to-win narrative arc — context, choice, outcome.",
    )

    notes_rows = "".join(
        f"""
<div class="aq-note-row" data-slug="{esc(slug)}">
  <div class="aq-note-label" title="{esc(slug)}">{esc(label)}</div>
  <textarea class="aq-note-text" data-slug="{esc(slug)}"
            placeholder="Optional correction/constraint — applies to every answer using this entry."
            >{esc(entry_notes.get(slug, '') or '')}</textarea>
</div>"""
        for slug, label in RESUME_ENTRY_SLUGS.items()
    )

    notes_section = f"""
<div class="card">
  <h2>Resume entry notes (global)</h2>
  <p style="color:#666;font-size:12px;margin-top:-4px">
    These notes are injected into <strong>every</strong> question's prompt for
    the entry they belong to. Treat them as authoritative overrides — corrections
    or constraints the model must respect. Auto-saves on blur.
  </p>
  <div class="aq-notes-grid">{notes_rows}</div>
  <div class="aq-notes-saved" style="color:#888;font-size:11px;margin-top:6px"></div>
</div>
"""

    header = f"""
<p style="margin-bottom:14px">
  <a href="/today?open=cover_letters">← Back to Apply Queue</a> ·
  <a href="/job/{esc(job_id)}">Job detail</a>
</p>
<div class="card">
  <h1 style="margin-bottom:6px">{company_name} — {title}</h1>
  <p class="sub" style="margin:0">
    {location} · composite <strong>{score}</strong>/{COMPOSITE_MAX}
  </p>
</div>
"""

    body = header + motivation_section + behavioral_section + notes_section + _AQ_PAGE_JS
    return page(f"Answer Questions — {company_name}", body)


# Page-local JS. Event-delegated so dynamically-replaced cards rebind for
# free. All mutating endpoints return {"ok": bool, "card_html": str|None,
# "error": str|None}; the client swaps the matching card's outerHTML.
_AQ_PAGE_JS = """
<script>
(function() {
  function postJSON(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    }).then(r => r.json());
  }
  function findCard(el) { return el.closest('.aq-card'); }
  function currentCard(questionId) {
    return document.querySelector('.aq-card[data-question-id="' + questionId + '"]');
  }
  function replaceCard(card, html) {
    const tmp = document.createElement('div');
    tmp.innerHTML = html.trim();
    const fresh = tmp.firstElementChild;
    if (fresh) card.replaceWith(fresh);
    return fresh;
  }
  function isDirtyEdit(card) {
    const ta = card.querySelector('.aq-answer-text');
    return !!(ta && ta.dataset.savedValue !== undefined
              && ta.value !== ta.dataset.savedValue);
  }
  function flashErr(msg) { alert(msg || 'Error'); }
  function showCopied(btn, label) {
    const orig = btn.textContent;
    btn.textContent = label || 'Copied';
    setTimeout(() => { btn.textContent = orig; }, 1200);
  }
  function setBusy(card, busy, label) {
    card.classList.toggle('aq-busy', !!busy);
    const btn = card.querySelector('.aq-generate');
    if (btn) {
      if (busy) {
        btn.dataset.origLabel = btn.textContent;
        btn.textContent = label || 'Generating…';
        btn.disabled = true;
      } else {
        if (btn.dataset.origLabel) btn.textContent = btn.dataset.origLabel;
        btn.disabled = false;
      }
    }
  }

  // ── Add question
  document.addEventListener('submit', function(e) {
    const form = e.target.closest('.aq-add-form');
    if (!form) return;
    e.preventDefault();
    const textEl = form.querySelector('.aq-new-text');
    const capEl  = form.querySelector('.aq-new-cap');
    const text   = (textEl.value || '').trim();
    if (!text) return;
    const cap = capEl.value ? parseInt(capEl.value, 10) : null;
    postJSON('/answer-questions/add', {
      job_id: form.dataset.jobId,
      question_text: text,
      question_class: form.dataset.class,
      char_cap: cap,
    }).then(r => {
      if (!r.ok) return flashErr(r.error);
      const list = document.querySelector(`.aq-list[data-class="${form.dataset.class}"]`);
      const tmp = document.createElement('div');
      tmp.innerHTML = r.card_html.trim();
      // Drop any 'no questions yet' placeholder.
      const placeholder = list.querySelector('p');
      if (placeholder) placeholder.remove();
      list.appendChild(tmp.firstElementChild);
      textEl.value = '';
      capEl.value  = '';
    }).catch(e => flashErr(String(e)));
  });

  // ── Generate / Regenerate
  document.addEventListener('click', function(e) {
    const btn = e.target.closest('.aq-generate');
    if (!btn) return;
    const card = findCard(btn);
    if (!card) return;
    setBusy(card, true, 'Generating… (15-30s)');
    postJSON('/answer-questions/generate', {
      job_id: card.dataset.jobId,
      question_id: card.dataset.questionId,
    }).then(r => {
      if (!r.ok) { setBusy(card, false); return flashErr(r.error); }
      replaceCard(card, r.card_html);
    }).catch(err => { setBusy(card, false); flashErr(String(err)); });
  });

  // ── Finalize / Unfinalize / Delete
  function cardAction(path, btnSelector, confirmMsg) {
    document.addEventListener('click', function(e) {
      const btn = e.target.closest(btnSelector);
      if (!btn) return;
      const card = findCard(btn);
      if (!card) return;
      if (confirmMsg && !confirm(confirmMsg)) return;
      postJSON(path, {
        job_id: card.dataset.jobId,
        question_id: card.dataset.questionId,
      }).then(r => {
        if (!r.ok) return flashErr(r.error);
        if (r.card_html) replaceCard(card, r.card_html);
        else card.remove();  // delete returns no html
      }).catch(err => flashErr(String(err)));
    });
  }
  cardAction('/answer-questions/unfinalize', '.aq-unfinalize', null);
  cardAction('/answer-questions/delete',     '.aq-delete',     'Delete this question? Drafts will be lost.');

  // ── Finalize: if the answer textarea has unsaved edits, persist them as a
  // new draft version FIRST so the finalized snapshot reflects what the
  // operator sees on screen (otherwise finalize_answer locks in the last
  // saved draft and silently drops the in-flight edit).
  document.addEventListener('click', async function(e) {
    const btn = e.target.closest('.aq-finalize');
    if (!btn) return;
    const card = findCard(btn);
    if (!card) return;
    const jid = card.dataset.jobId;
    const qid = card.dataset.questionId;

    if (isDirtyEdit(card)) {
      const ta = card.querySelector('.aq-answer-text');
      const r = await postJSON('/answer-questions/save-edit', {
        job_id: jid, question_id: qid, answer: ta.value,
      });
      if (!r.ok) return flashErr(r.error);
      const after = currentCard(qid);
      if (after && r.card_html) replaceCard(after, r.card_html);
    }

    const r2 = await postJSON('/answer-questions/finalize', {
      job_id: jid, question_id: qid,
    });
    if (!r2.ok) return flashErr(r2.error);
    const after2 = currentCard(qid);
    if (after2 && r2.card_html) replaceCard(after2, r2.card_html);
  });

  // ── Override notes auto-save on blur
  document.addEventListener('blur', function(e) {
    const ta = e.target.closest('.aq-override-text');
    if (!ta) return;
    const card = findCard(ta);
    if (!card) return;
    postJSON('/answer-questions/override', {
      job_id: card.dataset.jobId,
      question_id: card.dataset.questionId,
      override_notes: ta.value,
    });  // fire-and-forget
  }, true);

  // ── Resume entries chips: add via picker, remove via × button
  document.addEventListener('change', function(e) {
    const sel = e.target.closest('.aq-chip-add');
    if (!sel) return;
    const slug = sel.value;
    if (!slug) return;
    const card = findCard(sel);
    if (!card) return;
    const current = Array.from(card.querySelectorAll('.aq-chip[data-slug]')).map(el => el.dataset.slug);
    if (current.includes(slug)) return;
    current.push(slug);
    postJSON('/answer-questions/entries', {
      job_id: card.dataset.jobId,
      question_id: card.dataset.questionId,
      slugs: current,
    }).then(r => {
      if (!r.ok) return flashErr(r.error);
      replaceCard(card, r.card_html);
    });
  });
  document.addEventListener('click', function(e) {
    const x = e.target.closest('.aq-chip-remove');
    if (!x) return;
    const chip = x.closest('.aq-chip');
    const card = findCard(chip);
    if (!card) return;
    const removeSlug = chip.dataset.slug;
    const current = Array.from(card.querySelectorAll('.aq-chip[data-slug]'))
                         .map(el => el.dataset.slug)
                         .filter(s => s !== removeSlug);
    postJSON('/answer-questions/entries', {
      job_id: card.dataset.jobId,
      question_id: card.dataset.questionId,
      slugs: current,
    }).then(r => {
      if (!r.ok) return flashErr(r.error);
      replaceCard(card, r.card_html);
    });
  });

  // ── Version picker: swap displayed answer client-side from embedded JSON
  document.addEventListener('change', function(e) {
    const sel = e.target.closest('.aq-version-picker');
    if (!sel) return;
    const card = findCard(sel);
    if (!card) return;
    const histEl = card.querySelector('.aq-history');
    if (!histEl) return;
    let history;
    try { history = JSON.parse(histEl.dataset.history || '[]'); } catch (err) { return; }
    const want = parseInt(sel.value, 10);
    const entry = history.find(h => h.v === want);
    if (!entry) return;
    const ta = card.querySelector('.aq-answer-text');
    if (ta) {
      ta.value = entry.answer;
      ta.dataset.savedValue = entry.answer;  // reset dirty baseline
    }
    const saveBtn = card.querySelector('.aq-save-edit');
    if (saveBtn) saveBtn.disabled = true;
    const meta = card.querySelector('.aq-version-meta');
    if (meta) meta.firstChild.textContent = entry.chars + ' chars';
  });

  // ── Manual edit: typing in the answer textarea enables "Save edit"
  document.addEventListener('input', function(e) {
    const ta = e.target.closest('.aq-answer-text');
    if (!ta) return;
    const card = findCard(ta);
    if (!card) return;
    const saveBtn = card.querySelector('.aq-save-edit');
    if (!saveBtn) return;
    // Capture baseline on first input event so we can detect a real diff.
    if (ta.dataset.savedValue === undefined) ta.dataset.savedValue = ta.defaultValue;
    saveBtn.disabled = (ta.value === ta.dataset.savedValue);
  });
  document.addEventListener('click', function(e) {
    const btn = e.target.closest('.aq-save-edit');
    if (!btn || btn.disabled) return;
    const card = findCard(btn);
    if (!card) return;
    const ta = card.querySelector('.aq-answer-text');
    if (!ta) return;
    postJSON('/answer-questions/save-edit', {
      job_id:      card.dataset.jobId,
      question_id: card.dataset.questionId,
      answer:      ta.value,
    }).then(r => {
      if (!r.ok) return flashErr(r.error);
      replaceCard(card, r.card_html);
    });
  });

  // ── Copy buttons
  function bindCopy(btnClass, sourceClass) {
    document.addEventListener('click', function(e) {
      const btn = e.target.closest(btnClass);
      if (!btn) return;
      const card = findCard(btn);
      if (!card) return;
      const src = card.querySelector(sourceClass);
      if (!src) return;
      navigator.clipboard.writeText(src.value).then(() => showCopied(btn));
    });
  }
  bindCopy('.aq-copy',           '.aq-answer-text');
  bindCopy('.aq-copy-finalized', '.aq-finalized-text');

  // ── Resume-entry-notes panel autosave on blur (whole dict)
  let savedTimer = null;
  document.addEventListener('blur', function(e) {
    const ta = e.target.closest('.aq-note-text');
    if (!ta) return;
    const notes = {};
    document.querySelectorAll('.aq-note-text').forEach(el => {
      notes[el.dataset.slug] = el.value;
    });
    const status = document.querySelector('.aq-notes-saved');
    postJSON('/answer-questions/entry-notes', {notes: notes}).then(r => {
      if (!r.ok) { if (status) status.textContent = 'Save failed: ' + (r.error || '?'); return; }
      if (status) {
        status.textContent = 'Saved.';
        clearTimeout(savedTimer);
        savedTimer = setTimeout(() => { status.textContent = ''; }, 1500);
      }
    });
  }, true);
})();
</script>
"""


def daily_checklist_page(open_section: str | None = None, linkedin_view: str = "default") -> str:
    apply_ghosted_check()
    today_iso = date.today().isoformat()
    state     = load_daily_state(today_iso)

    done_count   = sum(1 for sid, _, _ in CHECKLIST_SECTIONS if state.get(sid))
    total        = len(CHECKLIST_SECTIONS)
    progress_pct = int(done_count / total * 100) if total else 0

    valid_sids    = {sid for sid, _, _ in CHECKLIST_SECTIONS}
    explicit_open = open_section if open_section in valid_sids else None
    first_undone  = next(
        (sid for sid, _, _ in CHECKLIST_SECTIONS if not state.get(sid)),
        None,
    )

    summary_html = (
        f'<div class="checklist-summary">'
        f'<strong>{today_iso}</strong> — {done_count} of {total} sections complete'
        f'<span class="progress-bar">'
        f'<span class="progress-fill" style="width:{progress_pct}%"></span>'
        f'</span></div>'
    )

    sections_html = ""
    for i, (sid, title, hint) in enumerate(CHECKLIST_SECTIONS, 1):
        done         = state.get(sid, False)
        badge_char   = "✓" if done else "○"
        badge_class  = "badge-done" if done else "badge-undone"
        is_open      = (sid == explicit_open) or (explicit_open is None and sid == first_undone)
        open_attr    = "open" if is_open else ""
        toggle_label = "Mark section incomplete" if done else "Mark section done"

        sections_html += f"""
        <details {open_attr} class="checklist-section">
          <summary>
            <span class="section-badge {badge_class}">{badge_char}</span>
            <span class="section-title">{i}. {title}</span>
          </summary>
          <div class="section-body">
            <p class="section-hint">{hint}</p>
            {render_section_body(sid, linkedin_view=linkedin_view)}
            <form method="POST" action="/today/toggle" style="margin-top:14px">
              <input type="hidden" name="section" value="{sid}">
              <button class="btn btn-secondary" style="margin-top:0">{toggle_label}</button>
            </form>
          </div>
        </details>
        """

    return page("Today", summary_html + sections_html)


# ── Request handler ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def send_html(self, html: str, status: int = 200):
        encoded = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: dict, status: int = 200):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(encoded))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        if path == "/":
            self.send_html(ingest_form())
        elif path == "/today":
            qs           = parse_qs(parsed.query)
            open_section = qs.get("open", [None])[0]
            view         = qs.get("view", ["default"])[0]
            self.send_html(daily_checklist_page(open_section, view))
        elif path == "/today/crawl/status":
            self.send_json(crawl_status_payload())
        elif path == "/pipeline":
            self.send_html(pipeline_page())
        elif path == "/search":
            qs = parse_qs(parsed.query)
            self.send_html(search_page(qs.get("q", [""])[0]))
        elif path == "/metrics":
            self.send_html(metrics_page())
        elif path == "/resume":
            self.send_html(resume_page())
        elif path == "/answer-questions":
            qs     = parse_qs(parsed.query)
            job_id = qs.get("job_id", [""])[0].strip()
            self.send_html(render_answer_questions_page(job_id))
        elif path.startswith("/job/"):
            job_id = path[len("/job/"):].strip("/")
            self.send_html(job_detail_page(job_id))
        else:
            self.send_html("<h1>Not found</h1>", 404)

    def redirect_today(self, open_section: str | None = None, fragment: str | None = None):
        location = "/today"
        if open_section:
            location += f"?open={open_section}"
        if fragment:
            location += f"#{fragment}"
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def redirect_or_today(self, params: dict, open_section: str | None = None,
                          fragment: str | None = None):
        """Redirect to params['return_to'] if it's a safe same-origin path, else /today."""
        return_to = (params.get("return_to", [""])[0] or "").strip()
        if return_to.startswith("/") and not return_to.startswith("//"):
            self.send_response(303)
            self.send_header("Location", return_to)
            self.end_headers()
            return
        self.redirect_today(open_section, fragment)

    def _read_json_body(self) -> dict:
        """Parse a JSON-encoded request body. Returns {} on missing / invalid."""
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _aq_handle(self, fn) -> None:
        """Shared shell for /answer-questions/* JSON handlers. Loads
        answer_questions lazily, calls ``fn(aq, body)`` which returns a tuple
        ``(ok, card_html_or_none, error_or_none)``. Always responds with JSON."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            import answer_questions as aq
        except Exception as e:  # noqa: BLE001
            self.send_json({"ok": False, "error": f"answer_questions import failed: {e}"})
            return
        body = self._read_json_body()
        try:
            ok, card_html, error = fn(aq, body)
        except Exception as e:  # noqa: BLE001
            self.send_json({"ok": False, "error": str(e)})
            return
        self.send_json({"ok": ok, "card_html": card_html, "error": error})

    def do_POST(self):
        path = urlparse(self.path).path

        # ── /answer-questions/* — JSON in, JSON out ─────────────────────────
        if path == "/answer-questions/add":
            def _do(aq, body):
                job_id   = (body.get("job_id") or "").strip()
                qtext    = (body.get("question_text") or "").strip()
                qclass   = (body.get("question_class") or "").strip()
                cap      = body.get("char_cap")
                if not job_id or not qtext or qclass not in ("motivation", "behavioral"):
                    return False, None, "missing job_id / question_text / question_class"
                rec = aq.add_question(job_id, qtext, qclass, cap)
                return True, _aq_card_html(job_id, rec), None
            self._aq_handle(_do); return

        if path == "/answer-questions/delete":
            def _do(aq, body):
                job_id = (body.get("job_id") or "").strip()
                qid    = (body.get("question_id") or "").strip()
                if not job_id or not qid:
                    return False, None, "missing job_id / question_id"
                removed = aq.delete_question(job_id, qid)
                if not removed:
                    return False, None, "question not found"
                return True, None, None
            self._aq_handle(_do); return

        if path == "/answer-questions/generate":
            def _do(aq, body):
                job_id = (body.get("job_id") or "").strip()
                qid    = (body.get("question_id") or "").strip()
                if not job_id or not qid:
                    return False, None, "missing job_id / question_id"
                rec = aq.generate_answer(job_id, qid)
                return True, _aq_card_html(job_id, rec), None
            self._aq_handle(_do); return

        if path == "/answer-questions/save-edit":
            def _do(aq, body):
                job_id = (body.get("job_id") or "").strip()
                qid    = (body.get("question_id") or "").strip()
                text   = body.get("answer") or ""
                if not job_id or not qid:
                    return False, None, "missing job_id / question_id"
                rec = aq.save_edit(job_id, qid, text)
                return True, _aq_card_html(job_id, rec), None
            self._aq_handle(_do); return

        if path == "/answer-questions/finalize":
            def _do(aq, body):
                job_id = (body.get("job_id") or "").strip()
                qid    = (body.get("question_id") or "").strip()
                if not job_id or not qid:
                    return False, None, "missing job_id / question_id"
                rec = aq.finalize_answer(job_id, qid)
                return True, _aq_card_html(job_id, rec), None
            self._aq_handle(_do); return

        if path == "/answer-questions/unfinalize":
            def _do(aq, body):
                job_id = (body.get("job_id") or "").strip()
                qid    = (body.get("question_id") or "").strip()
                if not job_id or not qid:
                    return False, None, "missing job_id / question_id"
                rec = aq.unfinalize_answer(job_id, qid)
                return True, _aq_card_html(job_id, rec), None
            self._aq_handle(_do); return

        if path == "/answer-questions/override":
            def _do(aq, body):
                job_id = (body.get("job_id") or "").strip()
                qid    = (body.get("question_id") or "").strip()
                if not job_id or not qid:
                    return False, None, "missing job_id / question_id"
                aq.update_question_override(job_id, qid, body.get("override_notes") or "")
                return True, None, None  # silent autosave, no card swap
            self._aq_handle(_do); return

        if path == "/answer-questions/entries":
            def _do(aq, body):
                job_id = (body.get("job_id") or "").strip()
                qid    = (body.get("question_id") or "").strip()
                slugs  = body.get("slugs") or []
                if not job_id or not qid:
                    return False, None, "missing job_id / question_id"
                if not isinstance(slugs, list):
                    return False, None, "slugs must be a list"
                rec = aq.update_resume_entries(job_id, qid, slugs)
                return True, _aq_card_html(job_id, rec), None
            self._aq_handle(_do); return

        if path == "/answer-questions/entry-notes":
            def _do(aq, body):
                notes = body.get("notes")
                if not isinstance(notes, dict):
                    return False, None, "notes must be a dict"
                aq.save_entry_notes(notes)
                return True, None, None
            self._aq_handle(_do); return

        if path == "/today/crawl/start":
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            start_crawl()
            self.redirect_today("crawl")
            return

        if path == "/today/linkedin/fetch":
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            ok, n, output = run_linkedin_fetch()
            if ok:
                set_linkedin_flash("ok", f"Fetched {n} new job(s).")
            else:
                tail_lines = [l for l in output.splitlines() if l.strip()][-3:]
                tail = "; ".join(tail_lines) or "see server log"
                set_linkedin_flash("warn", f"Fetch failed — {tail}")
            self.redirect_today("linkedin_ingest")
            return

        if path == "/today/linkedin/ingest":
            length     = int(self.headers.get("Content-Length", 0))
            raw        = self.rfile.read(length).decode("utf-8")
            params     = parse_qs(raw)
            def g(k):  return params.get(k, [""])[0].strip()
            staging_id = g("staging_id")
            company    = g("company")
            title      = g("title")
            location   = g("location")
            apply_url  = g("apply_url")
            posted     = g("posted")
            jd_text    = g("jd_text")

            if not staging_id:
                set_linkedin_flash("warn", "Ingest failed — missing staging_id.")
            elif len(jd_text) < MIN_JD_LENGTH:
                set_linkedin_flash(
                    "warn",
                    f"Ingest failed — JD must be at least {MIN_JD_LENGTH} chars "
                    f"(got {len(jd_text)}). Paste the full description.",
                )
            elif not (company and title and apply_url):
                set_linkedin_flash("warn", "Ingest failed — company, title, and apply URL are required.")
            else:
                success, output = run_ingest(apply_url, company, title, location, jd_text, posted)
                if success:
                    remove_staged(staging_id)
                    set_linkedin_flash("ok", f"Ingested: {company} — {title}")
                else:
                    tail_lines = [l for l in output.splitlines() if l.strip()][-3:]
                    tail = "; ".join(tail_lines) or "see server log"
                    set_linkedin_flash("warn", f"Ingest failed — {tail}")

            self.redirect_today("linkedin_ingest")
            return

        if path == "/today/linkedin/prefilter":
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            ok, n_pass, n_fail, output = run_linkedin_prefilter()
            if ok:
                set_linkedin_flash(
                    "ok",
                    f"Pre-filter applied — {n_pass} passing, {n_fail} failing.",
                )
            else:
                tail_lines = [l for l in output.splitlines() if l.strip()][-3:]
                tail = "; ".join(tail_lines) or "see server log"
                set_linkedin_flash("warn", f"Pre-filter failed — {tail}")
            self.redirect_today("linkedin_ingest")
            return

        if path == "/today/linkedin/discard_failing":
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            n = discard_failing_staged()
            set_linkedin_flash("info", f"Discarded {n} failing row(s).")
            self.redirect_today("linkedin_ingest")
            return

        if path == "/today/linkedin/fetchjd":
            length     = int(self.headers.get("Content-Length", 0))
            raw        = self.rfile.read(length).decode("utf-8")
            params     = parse_qs(raw)
            staging_id = params.get("staging_id", [""])[0].strip()

            if not staging_id:
                set_linkedin_flash("warn", "Fetch JD failed — missing staging_id.")
                self.redirect_today("linkedin_ingest")
                return

            ok, msg = fetch_jd_for_staged(staging_id)
            kind    = "ok" if ok else "warn"
            set_linkedin_flash(kind, f"Fetch JD: {msg}")
            self.redirect_today("linkedin_ingest", fragment=f"row-{staging_id}")
            return

        if path == "/today/linkedin/discard":
            length     = int(self.headers.get("Content-Length", 0))
            raw        = self.rfile.read(length).decode("utf-8")
            params     = parse_qs(raw)
            staging_id = params.get("staging_id", [""])[0].strip()
            if staging_id:
                target = remove_staged(staging_id)
                if target:
                    set_linkedin_flash(
                        "info",
                        f"Discarded: {target.get('company','?')} — {target.get('title','?')}",
                    )
            self.redirect_today("linkedin_ingest")
            return

        if path == "/today/cl/generate":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            job_id = params.get("job_id", [""])[0].strip()

            if not job_id:
                set_cl_flash("warn", "Generate CL failed — missing job_id.")
            else:
                cmd = ["node", str(SCRIPTS / "generate_cl.js"), "--job-id", job_id]
                try:
                    result = subprocess.run(
                        cmd, cwd=ROOT,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        encoding="utf-8", errors="replace",
                        timeout=120,
                    )
                    if result.returncode == 0:
                        # Re-load to find the new filename for the flash.
                        jobs = load_pipeline()
                        job  = next((j for j in jobs if j.get("job_id") == job_id), None)
                        fname = (job or {}).get("cover_letter_path", "").split("/")[-1]
                        set_cl_flash(
                            "ok",
                            f"Cover letter generated: {fname}" if fname else "Cover letter generated.",
                        )
                    else:
                        tail = "; ".join([l for l in (result.stdout or "").splitlines() if l.strip()][-3:])
                        set_cl_flash("warn", f"Generate CL failed — {tail or 'see server log'}")
                except subprocess.TimeoutExpired:
                    set_cl_flash("warn", "Generate CL timed out after 120s.")
                except Exception as e:
                    set_cl_flash("warn", f"Failed to launch generate_cl.js: {e}")

            self.redirect_or_today(params, "cover_letters", fragment=f"cl-{job_id}")
            return

        if path == "/today/company/research":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            company_id = params.get("company_id", [""])[0].strip()

            if not company_id:
                set_research_flash("warn", "Research failed — missing company_id.")
            else:
                ok, msg = run_company_research(company_id)
                set_research_flash("ok" if ok else "warn", msg)

            self.redirect_or_today(params, "cover_letters")
            return

        if path == "/today/comp/estimate":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            job_id = params.get("job_id", [""])[0].strip()

            if not job_id:
                set_cl_flash("warn", "Comp estimate failed — missing job_id.")
            else:
                cmd = [sys.executable, str(SCRIPTS / "comp_estimate.py"), "--job-id", job_id]
                try:
                    result = subprocess.run(
                        cmd, cwd=ROOT,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        encoding="utf-8", errors="replace",
                        timeout=120,
                    )
                    if result.returncode == 0:
                        ests = load_comp_estimates_by_job()
                        rec  = ests.get(job_id)
                        if rec:
                            est = rec.get("estimate", {})
                            cur = est.get("currency", "")
                            tgt = (est.get("base") or {}).get("target")
                            conf = est.get("confidence", "")
                            if tgt:
                                set_cl_flash("ok",
                                    f"Comp estimate: {cur} {tgt:,} target ({conf} confidence).")
                            else:
                                set_cl_flash("ok", "Comp estimate generated.")
                        else:
                            set_cl_flash("ok", "Comp estimate generated.")
                    else:
                        tail = "; ".join([l for l in (result.stdout or "").splitlines() if l.strip()][-3:])
                        set_cl_flash("warn", f"Comp estimate failed — {tail or 'see server log'}")
                except subprocess.TimeoutExpired:
                    set_cl_flash("warn", "Comp estimate timed out after 120s.")
                except Exception as e:
                    set_cl_flash("warn", f"Failed to launch comp_estimate.py: {e}")

            self.redirect_or_today(params, "cover_letters", fragment=f"cl-{job_id}")
            return

        if path == "/today/cl/open":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            job_id = params.get("job_id", [""])[0].strip()

            jobs = load_pipeline()
            job  = next((j for j in jobs if j.get("job_id") == job_id), None)
            rel  = (job or {}).get("cover_letter_path", "")
            if not job:
                set_cl_flash("warn", "Open failed — job not found.")
            elif not rel:
                set_cl_flash("warn", "Open failed — no cover_letter_path on this job. Generate first.")
            else:
                file_path = ROOT / rel
                if not file_path.exists():
                    set_cl_flash("warn", f"Open failed — file missing: {file_path}")
                else:
                    try:
                        os.startfile(str(file_path))   # Windows; launches default app
                    except Exception as e:
                        set_cl_flash("warn", f"Open failed — {e}")

            self.redirect_or_today(params, "cover_letters", fragment=f"cl-{job_id}")
            return

        if path == "/today/cl/archive":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            job_id = params.get("job_id", [""])[0].strip()

            if job_id:
                p = DATA_DIR / "job_pipeline.json"
                if p.exists():
                    jobs = json.loads(p.read_text(encoding="utf-8"))
                    target = next((j for j in jobs if j.get("job_id") == job_id), None)
                    if target:
                        target["pipeline_status"] = "archived"
                        p.write_text(
                            json.dumps(jobs, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        set_cl_flash(
                            "info",
                            f"Archived: {target.get('company_name','?')} — {target.get('title','?')}",
                        )
                    else:
                        set_cl_flash("warn", "Archive failed — job not found.")

            self.redirect_today("cover_letters")
            return

        if path == "/today/apply/log":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            job_id = params.get("job_id", [""])[0].strip()

            if not job_id:
                set_cl_flash("warn", "Mark Applied failed — missing job_id.")
            else:
                cmd = [
                    sys.executable, str(SCRIPTS / "update_status.py"), "log",
                    "--job-id", job_id, "--method", "direct",
                ]
                try:
                    result = subprocess.run(
                        cmd, cwd=ROOT,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        encoding="utf-8", errors="replace",
                        timeout=60,
                    )
                    if result.returncode == 0:
                        set_cl_flash("ok", "Application logged. Row moves to Status updates section.")
                    else:
                        tail = "; ".join([l for l in (result.stdout or "").splitlines() if l.strip()][-3:])
                        set_cl_flash("warn", f"Mark Applied failed — {tail or 'see server log'}")
                except Exception as e:
                    set_cl_flash("warn", f"Failed to launch update_status.py: {e}")

            self.redirect_today("cover_letters")
            return

        if path == "/today/toggle":
            length  = int(self.headers.get("Content-Length", 0))
            raw     = self.rfile.read(length).decode("utf-8")
            params  = parse_qs(raw)
            section = params.get("section", [""])[0].strip()

            valid = {sid for sid, _, _ in CHECKLIST_SECTIONS}
            if section in valid:
                today_iso      = date.today().isoformat()
                state          = load_daily_state(today_iso)
                state[section] = not state.get(section, False)
                save_daily_state(today_iso, state)

            self.redirect_today(section if section in valid else None)
            return

        if path == "/today/status":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            app_id = params.get("app_id", [""])[0].strip()
            action = params.get("action", [""])[0].strip()

            if app_id and action in STATUS_ACTION_MAP:
                status, note = STATUS_ACTION_MAP[action]
                cmd = [
                    sys.executable, str(SCRIPTS / "update_status.py"),
                    "status",
                    "--app-id", app_id,
                    "--status", status,
                ]
                if note:
                    cmd += ["--notes", note]
                subprocess.run(
                    cmd, cwd=ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    encoding="utf-8", errors="replace",
                )

            self.redirect_today("status_updates")
            return

        if path != "/ingest":
            self.send_html("<h1>Not found</h1>", 404)
            return

        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length).decode("utf-8")
        params  = parse_qs(raw)

        def get(key): return params.get(key, [""])[0].strip()

        url      = get("url")
        company  = get("company")
        title    = get("title")
        location = get("location")
        posted   = get("posted")
        jd_text  = get("jd_text")

        # If JD was pasted directly, skip fetch
        if jd_text and len(jd_text) >= MIN_JD_LENGTH:
            print(f"  Ingesting (paste mode): {company} — {title}")
            success, output = run_ingest(url, company, title, location, jd_text, posted)
            notice = (
                f'<div class="notice notice-ok">✓ Job ingested successfully.</div>'
                if success else
                f'<div class="notice notice-warn">Ingest failed — see output below.</div>'
            )
            html = page("Ingested", f"""
            <div class="card">
              {notice}
              <pre>{output}</pre>
              <a href="/" class="btn btn-primary" style="margin-top:12px">Add another</a>
              <a href="/pipeline" class="btn btn-secondary">View pipeline</a>
            </div>""")
            self.send_html(html)
            return

        # Try to fetch JD from URL
        print(f"  Fetching: {url}")
        jd_text, fetched = fetch_jd(url)

        if fetched:
            # Auto-ingest
            print(f"  Ingesting: {company} — {title}")
            success, output = run_ingest(url, company, title, location, jd_text, posted)
            notice = (
                '<div class="notice notice-ok">✓ Job ingested successfully.</div>'
                if success else
                '<div class="notice notice-warn">Ingest failed — see output below.</div>'
            )
            html = page("Ingested", f"""
            <div class="card">
              {notice}
              <pre>{output}</pre>
              <a href="/" class="btn btn-primary" style="margin-top:12px">Add another</a>
              <a href="/pipeline" class="btn btn-secondary">View pipeline</a>
            </div>""")
        else:
            # JS-rendered page — show paste form
            notice = (
                "This page requires JavaScript to load (e.g. Workday). "
                "Open the job posting in your browser, select all the job description "
                "text, copy it, and paste it below."
            )
            html = ingest_form(
                url=url, company=company, title=title,
                location=location, posted=posted,
                notice=notice, show_paste=True
            )

        self.send_html(html)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Local web UI for job ingestion.")
    parser.add_argument("--port", type=int, default=5000, metavar="PORT")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()

    server      = HTTPServer(("localhost", args.port), Handler)
    url         = f"http://localhost:{args.port}"
    landing_url = f"{url}/today"

    print(f"next-role server running at {url}")
    print(f"Daily checklist: {landing_url}")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(landing_url)).start()

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.shutdown()
        server.server_close()
        print("Server stopped.")


if __name__ == "__main__":
    main()
