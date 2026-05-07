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

MIN_JD_LENGTH = 200

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

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        jd_text = ""
        for selector in [
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
</style>
"""

def page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — next-role</title>{STYLE}</head>
<body><div class="wrap">
<h1>next-role</h1>
<p class="sub">Job search pipeline · <a href="/">Ingest</a> · <a href="/pipeline">Pipeline</a></p>
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

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_html(ingest_form())
        elif path == "/pipeline":
            self.send_html(pipeline_page())
        else:
            self.send_html("<h1>Not found</h1>", 404)

    def do_POST(self):
        path = urlparse(self.path).path
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

    server = HTTPServer(("localhost", args.port), Handler)
    url    = f"http://localhost:{args.port}"

    print(f"next-role ingestion server running at {url}")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

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
