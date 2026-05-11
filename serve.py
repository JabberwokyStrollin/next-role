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
</style>
"""

def page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — next-role</title>{STYLE}</head>
<body><div class="wrap">
<h1>next-role</h1>
<p class="sub">Job search pipeline · <a href="/today">Today</a> · <a href="/">Ingest</a> · <a href="/pipeline">Pipeline</a></p>
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
    jobs = load_pipeline()
    active = [j for j in jobs if j.get("pipeline_status") != "archived"]
    if not active:
        return '<div class="card"><h2>Pipeline</h2><p style="color:#888">No jobs yet.</p></div>'

    rows = ""
    for job in sorted(active, key=lambda j: (
        (j.get("stack_match_score") or 0) +
        (j.get("seniority_score") or 0) +
        (j.get("domain_fit_score") or 0) +
        (j.get("hiring_velocity_score") or 0)
    ), reverse=True)[:10]:
        partial = (
            (job.get("stack_match_score") or 0) +
            (job.get("seniority_score") or 0) +
            (job.get("domain_fit_score") or 0) +
            (job.get("hiring_velocity_score") or 0)
        )
        apply_url = job.get("apply_url", "")
        apply_link = f'<a class="apply-link" href="{apply_url}" target="_blank">Apply ↗</a>' if apply_url else ""
        rows += f"""
        <div class="job-row">
          <span class="score">{partial}</span>
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
    jobs = load_pipeline()
    active = [j for j in jobs if j.get("pipeline_status") != "archived"]

    rows = ""
    for job in sorted(active, key=lambda j: (
        (j.get("stack_match_score") or 0) +
        (j.get("seniority_score") or 0) +
        (j.get("domain_fit_score") or 0) +
        (j.get("hiring_velocity_score") or 0)
    ), reverse=True):
        partial = (
            (job.get("stack_match_score") or 0) +
            (job.get("seniority_score") or 0) +
            (job.get("domain_fit_score") or 0) +
            (job.get("hiring_velocity_score") or 0)
        )
        apply_url = job.get("apply_url", "")
        apply_link = f'<a class="apply-link" href="{apply_url}" target="_blank">Apply ↗</a>' if apply_url else ""
        cl = "✓" if job.get("cover_letter_generated") else ""
        rows += f"""
        <div class="job-row">
          <span class="score">{partial}</span>
          <span class="company">{job['company_name'][:20]}</span>
          <span class="title-cell">{job['title'][:45]}</span>
          <span style="color:#888;font-size:11px">{job.get('pipeline_status','')}</span>
          <span style="color:#1a5c2e;font-size:11px">{cl}</span>
          {apply_link}
        </div>"""

    return page("Pipeline", f"""
<div class="card">
  <h2>All active jobs ({len(active)})</h2>
  <div class="pipeline">{rows or '<p style="color:#888">No jobs yet.</p>'}</div>
</div>
<p><a href="/">← Add job</a></p>
""")


def render_section_body(sid: str, linkedin_view: str = "default") -> str:
    if sid == "status_updates":
        return render_status_updates_body()
    if sid == "crawl":
        return render_crawl_body()
    if sid == "linkedin_ingest":
        return render_linkedin_body(linkedin_view)
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

    def do_POST(self):
        path = urlparse(self.path).path

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
