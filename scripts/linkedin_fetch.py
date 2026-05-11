"""
linkedin_fetch.py — IMAP fetch of LinkedIn job-alert emails.

Connects via IMAP, searches the inbox for unseen messages from senders in
data/email_config.json, parses each message's HTML for job listings, and
writes them to data/email_staged.json for the /today UI to render.

After parsing, marks each fetched message \\Seen on the server and records
its Message-ID in data/email_state.json so a re-fetch (or unflag) won't
re-stage the same email.

Credentials via env vars (matches scripts/config.py ANTHROPIC_API_KEY pattern):
    NEXTROLE_IMAP_HOST          (e.g. imap.gmail.com)
    NEXTROLE_IMAP_USER          (full email address)
    NEXTROLE_IMAP_APP_PASSWORD  (Gmail/provider app password — NOT login password)

Usage:
    python scripts/linkedin_fetch.py
    python scripts/linkedin_fetch.py --dry-run     # parse only, no \\Seen, no state write
    python scripts/linkedin_fetch.py --sample FILE # parse a local .eml (no IMAP)

Output (last line of stdout, machine-readable):
    FETCHED: <n_new_jobs>      on success
    ERROR: <message>           on failure
"""

import argparse
import email
import imaplib
import json
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.header import decode_header, make_header
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT         = Path(__file__).parent.parent
DATA_DIR     = ROOT / "data"
EMAIL_CFG    = DATA_DIR / "email_config.json"
EMAIL_STATE  = DATA_DIR / "email_state.json"
STAGED_PATH  = DATA_DIR / "email_staged.json"
JD_FETCH_LOG = DATA_DIR / "jd_fetch_log.jsonl"

DEFAULT_SENDERS = ["jobalerts-noreply@linkedin.com"]

# Minimum JD length to count an auto-fetch as successful (mirrors serve.py).
MIN_JD_LENGTH = 200

JD_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── Config / state I/O ────────────────────────────────────────────────────────

def load_allowlist() -> list[str]:
    if not EMAIL_CFG.exists():
        EMAIL_CFG.write_text(
            json.dumps({"senders": DEFAULT_SENDERS}, indent=2),
            encoding="utf-8",
        )
        return list(DEFAULT_SENDERS)
    try:
        cfg = json.loads(EMAIL_CFG.read_text(encoding="utf-8"))
    except Exception:
        return list(DEFAULT_SENDERS)
    senders = cfg.get("senders") or []
    return [s.strip() for s in senders if s.strip()] or list(DEFAULT_SENDERS)


def load_seen_ids() -> set[str]:
    if not EMAIL_STATE.exists():
        return set()
    try:
        data = json.loads(EMAIL_STATE.read_text(encoding="utf-8"))
        return set(data.get("seen_message_ids", []))
    except Exception:
        return set()


def add_seen_ids(new_ids: set[str]) -> None:
    if not new_ids:
        return
    existing = load_seen_ids() | new_ids
    EMAIL_STATE.write_text(
        json.dumps({"seen_message_ids": sorted(existing)}, indent=2),
        encoding="utf-8",
    )


def load_staged() -> list[dict]:
    if not STAGED_PATH.exists():
        return []
    try:
        return json.loads(STAGED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_staged(rows: list[dict]) -> None:
    STAGED_PATH.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── HTML parsing ──────────────────────────────────────────────────────────────

def _decode_subject(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _extract_html(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    elif msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def _normalize_linkedin_url(url: str) -> str:
    """
    LinkedIn alert emails embed two kinds of tracker URLs that break
    unauthenticated GETs:

      1. /comm/jobs/view/<id>/ — auth-walls (307 → login). The /jobs/view/
         variant (same path with /comm/ stripped) serves the public page.
      2. /jobs/view/<id>/?...&otpToken=... — the otpToken query param
         triggers a passwordless-email-login redirect to /ssr-login/.
         The resulting 200 body is a "we're signing you in" placeholder,
         not the JD, so a vanilla GET sees ~150 chars of useless text.

    Strip the /comm/ prefix and the entire query string for /jobs/view/
    URLs. Applied at parse time so auto-fetch and the user's "open ↗"
    link both land on the no-auth, no-otp variant.
    """
    url = url.replace("/comm/jobs/view/", "/jobs/view/")
    if "/jobs/view/" in url:
        url = url.split("?", 1)[0]
    return url


def parse_linkedin_alert(html: str) -> list[dict]:
    """
    Extract jobs from a LinkedIn job-alert HTML email.

    Each job appears in a <td> whose multi-line text follows:
        line 0: Title
        line 1: Company · Location          (separator U+00B7 middle dot)
        line 2+: badges ('Easy Apply', '6 company alumni', etc.) — ignored

    Multiple anchors point at the same /jobs/view/<id>; pick the one whose
    parent <td> has the richest text.
    """
    soup = BeautifulSoup(html, "html.parser")

    by_id: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/jobs/view/(\d+)", a["href"])
        if m:
            by_id.setdefault(m.group(1), []).append(a)

    jobs = []
    for jid, anchors in by_id.items():
        best, best_lines = None, []
        for a in anchors:
            parent = a.find_parent("td")
            if not parent:
                continue
            lines = [
                ln.strip()
                for ln in parent.get_text("\n").splitlines()
                if ln.strip()
            ]
            if len(lines) > len(best_lines):
                best, best_lines = a, lines

        if not best or len(best_lines) < 2:
            continue

        title       = best_lines[0]
        meta        = best_lines[1]
        company, _, location = meta.partition("·")
        company  = company.strip()
        location = location.strip()
        if not company:
            # Fallback if separator missing: leave full meta in company
            company = meta

        jobs.append({
            "linkedin_job_id": jid,
            "title":           title[:200],
            "company":         company[:120],
            "location":        location[:120],
            "apply_url":       _normalize_linkedin_url(best["href"]),
        })

    return jobs


# ── Best-effort JD auto-fetch ────────────────────────────────────────────────-

def _log_jd_fetch(record: dict) -> None:
    """Append a single fetch outcome to JD_FETCH_LOG. Best-effort; never raises."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **record,
        }
        with JD_FETCH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _fetch_jd_text(url: str) -> tuple[str, bool, str]:
    """
    Best-effort GET of the apply URL; extract JD text if the page is plain HTML.

    Returns (text, ok, reason). reason ∈ {ok, auth_wall, expired, http_error,
    exception, short} so callers can show a specific message instead of a
    generic failure. "expired" means LinkedIn redirected the /jobs/view/<id>/
    URL to a similar-jobs search page (typical for closed postings).

    Each call appends a diagnostic record to data/jd_fetch_log.jsonl so
    failure modes can be told apart after the fact.
    """
    try:
        resp = requests.get(url, headers=JD_FETCH_HEADERS, timeout=15,
                            allow_redirects=True)
    except Exception as e:
        _log_jd_fetch({
            "url_in":   url,
            "reason":   "exception",
            "exc_type": type(e).__name__,
            "exc_msg":  str(e)[:200],
        })
        return "", False, "exception"

    base = {
        "url_in":       url,
        "url_resolved": resp.url,
        "status":       resp.status_code,
        "raw_html_len": len(resp.text),
        "content_type": resp.headers.get("Content-Type", ""),
    }

    if resp.status_code != 200:
        _log_jd_fetch({**base, "reason": "http_error"})
        return "", False, "http_error"

    # Detect login-wall redirects:
    #   /uas/login              — classic auth wall
    #   login?session_redirect  — older variant
    #   /ssr-login/             — passwordless-email-login (otpToken trigger).
    #                             We strip otpToken at parse time, but keep
    #                             this as defense-in-depth for any new token
    #                             param LinkedIn introduces.
    if (
        "/uas/login"             in resp.url
        or "login?session_redirect" in resp.url
        or "/ssr-login/"            in resp.url
    ):
        _log_jd_fetch({**base, "reason": "auth_wall"})
        return "", False, "auth_wall"

    # Detect LinkedIn redirecting /jobs/view/<id>/ away from the JD path —
    # most commonly to /jobs/<title>-jobs?trk=expired_jd_redirect for an
    # expired posting. Body is a "similar jobs" search page, not the JD.
    if (
        "linkedin.com" in url
        and "/jobs/view/" in url
        and "/jobs/view/" not in resp.url
    ):
        _log_jd_fetch({**base, "reason": "expired"})
        return "", False, "expired"

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    # On LinkedIn pages, only the JD-specific containers are trustworthy.
    # Generic <main>/<article>/#content selectors on LinkedIn /jobs/view/
    # pages frequently match sign-in chrome or similar-jobs panels with
    # >200 chars of noise — we'd return reason="ok" with garbage content.
    is_linkedin           = "linkedin.com" in resp.url
    linkedin_jd_selectors = {"div.description__text--rich", "section.show-more-less-html"}

    jd_text          = ""
    winning_selector = None
    selector_hits    = []  # [selector, stripped_len] for any element actually present
    for selector in [
        # LinkedIn public job-view markup (most reliable for our staged URLs)
        "div.description__text--rich",
        "section.show-more-less-html",
        # Generic ATS / careers-page fallbacks
        "[data-qa='job-description']",
        "#content",
        ".job-description",
        ".job__description",
        ".description",
        "main",
        "article",
    ]:
        if is_linkedin and selector not in linkedin_jd_selectors:
            continue
        el = soup.select_one(selector)
        if el:
            jd_text = el.get_text(separator="\n", strip=True)
            selector_hits.append([selector, len(jd_text)])
            if len(jd_text) >= MIN_JD_LENGTH:
                winning_selector = selector
                break

    # Generic full-page fallback only for non-LinkedIn URLs. LinkedIn's
    # chrome (sign-in prompt, similar-jobs lists) routinely yields >200
    # chars of noise that would falsely classify as a successful JD fetch.
    if not is_linkedin and len(jd_text) < MIN_JD_LENGTH:
        jd_text = soup.get_text(separator="\n", strip=True)

    lines   = [ln.strip() for ln in jd_text.splitlines() if ln.strip()]
    jd_text = "\n".join(lines)

    record = {
        **base,
        "stripped_text_len": len(jd_text),
        "winning_selector":  winning_selector,
        "selector_hits":     selector_hits,
    }
    if len(jd_text) < MIN_JD_LENGTH:
        _log_jd_fetch({**record, "reason": "short", "body_snippet": jd_text[:300]})
        return "", False, "short"
    _log_jd_fetch({**record, "reason": "ok"})
    return jd_text, True, "ok"


def _attach_jd_text(jobs: list[dict], max_workers: int = 4) -> int:
    """
    Best-effort parallel fetch of JD body for each job; mutates jobs in place.
    Returns the count of jobs where a JD was successfully attached.

    Not called automatically anywhere — kept for opt-in scripted use against
    non-LinkedIn URLs. Bulk fetch against linkedin.com trips their bot detection
    even at modest concurrency, so the in-app flow uses per-row on-demand
    fetches at human cadence instead.
    """
    if not jobs:
        return 0

    def _one(j: dict) -> bool:
        text, ok, _reason = _fetch_jd_text(j.get("apply_url", ""))
        if ok:
            j["jd_text"] = text
            return True
        return False

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(_one, jobs))
    return sum(1 for r in results if r)


# ── IMAP fetch ────────────────────────────────────────────────────────────────

def get_creds() -> tuple[str, str, str]:
    host = os.environ.get("NEXTROLE_IMAP_HOST", "").strip()
    user = os.environ.get("NEXTROLE_IMAP_USER", "").strip()
    pw   = os.environ.get("NEXTROLE_IMAP_APP_PASSWORD", "").strip()
    missing = [
        n for n, v in (
            ("NEXTROLE_IMAP_HOST", host),
            ("NEXTROLE_IMAP_USER", user),
            ("NEXTROLE_IMAP_APP_PASSWORD", pw),
        ) if not v
    ]
    if missing:
        sys.stderr.write(
            "Missing env var(s): " + ", ".join(missing) + "\n"
            "Set them before running. For Gmail, generate an app password at\n"
            "  https://myaccount.google.com/apppasswords\n"
            "and use imap.gmail.com as NEXTROLE_IMAP_HOST.\n"
        )
        print("ERROR: missing IMAP credentials in environment")
        sys.exit(2)
    return host, user, pw


def fetch_via_imap(dry_run: bool = False) -> int:
    """Fetch unseen LinkedIn alerts from IMAP. Returns count of new staged jobs."""
    host, user, pw = get_creds()
    senders = load_allowlist()
    seen    = load_seen_ids()
    staged  = load_staged()

    # Avoid re-staging the same job (URL or job-id) sitting in staged already
    staged_ids  = {s.get("linkedin_job_id") for s in staged if s.get("linkedin_job_id")}
    staged_urls = {s.get("apply_url")       for s in staged if s.get("apply_url")}

    print(f"Connecting to {host} as {user}...")
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, pw)
    except imaplib.IMAP4.error as e:
        print(f"ERROR: IMAP login failed: {e}")
        sys.exit(2)

    M.select("INBOX")

    new_jobs        = []
    new_message_ids = set()
    fetched_uids    = []

    for sender in senders:
        typ, data = M.search(None, "UNSEEN", "FROM", f'"{sender}"')
        if typ != "OK":
            print(f"  [warn] search failed for {sender}: {typ}")
            continue
        uids = data[0].split() if data and data[0] else []
        print(f"  {sender}: {len(uids)} unseen message(s)")
        for uid in uids:
            typ, msg_data = M.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            mid = (msg.get("Message-ID") or "").strip()
            if mid and mid in seen:
                continue
            html = _extract_html(msg)
            if not html:
                print(f"  [warn] no HTML body in message {uid.decode()}")
                continue

            subject       = _decode_subject(msg.get("Subject"))
            jobs_in_email = parse_linkedin_alert(html)
            kept = 0
            for j in jobs_in_email:
                if j["linkedin_job_id"] in staged_ids:
                    continue
                if j["apply_url"] in staged_urls:
                    continue
                j.update({
                    "staging_id":         uuid.uuid4().hex[:12],
                    "source_message_id":  mid,
                    "source_subject":     subject,
                    "fetched_at":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
                new_jobs.append(j)
                staged_ids.add(j["linkedin_job_id"])
                staged_urls.add(j["apply_url"])
                kept += 1

            print(f"    msg {uid.decode()}: parsed {len(jobs_in_email)}, kept {kept}")
            if mid and kept > 0:
                new_message_ids.add(mid)
                fetched_uids.append(uid)
            elif kept == 0:
                # Parse yielded nothing usable — leave message untouched so
                # it can be re-investigated, no \Seen mark, no Message-ID record.
                pass

    if not dry_run:
        for uid in fetched_uids:
            try:
                M.store(uid, "+FLAGS", "\\Seen")
            except Exception as e:
                print(f"  [warn] failed to mark {uid.decode()} \\Seen: {e}")

    try:
        M.logout()
    except Exception:
        pass

    if new_jobs and not dry_run:
        staged.extend(new_jobs)
        save_staged(staged)
        add_seen_ids(new_message_ids)

    print(f"FETCHED: {len(new_jobs)}")
    return len(new_jobs)


def reset_seen_state() -> tuple[int, int, int]:
    """
    Clear local dedup state, clear the staged-jobs list, and remove \\Seen on
    the server for messages whose Message-ID we'd previously tracked. Lets a
    re-fetch pull the same alerts.

    Returns (n_local_cleared, n_staged_cleared, n_server_unflagged). Preserves
    \\Seen on any LinkedIn messages the user had naturally read outside our
    fetch flow.
    """
    seen_ids   = list(load_seen_ids())
    n_local    = len(seen_ids)
    n_staged   = len(load_staged())

    if EMAIL_STATE.exists():
        EMAIL_STATE.unlink()
    if STAGED_PATH.exists():
        STAGED_PATH.unlink()

    if not seen_ids:
        return n_local, n_staged, 0

    host, user, pw = get_creds()
    print(f"Connecting to {host} to unflag {len(seen_ids)} message(s)...")
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, pw)
    except imaplib.IMAP4.error as e:
        print(f"ERROR: IMAP login failed during reset: {e}")
        return n_local, n_staged, 0

    M.select("INBOX")
    n_server = 0
    for mid in seen_ids:
        try:
            typ, data = M.search(None, "HEADER", "Message-ID", f'"{mid}"')
        except Exception as e:
            print(f"  [warn] search failed for {mid}: {e}")
            continue
        if typ != "OK":
            continue
        uids = data[0].split() if data and data[0] else []
        for uid in uids:
            try:
                M.store(uid, "-FLAGS", "\\Seen")
                n_server += 1
            except Exception as e:
                print(f"  [warn] failed to unflag {uid.decode()}: {e}")

    try:
        M.logout()
    except Exception:
        pass

    return n_local, n_staged, n_server


def rehydrate_staged() -> tuple[int, int]:
    """
    Migrate an existing staged file: normalize /comm/jobs/view/ → /jobs/view/
    on each apply_url. Useful after a parser upgrade — avoids reset + IMAP
    re-fetch. Does NOT auto-fetch JDs (per-row UI button does that on demand).

    Returns (n_normalized, n_total).
    """
    rows = load_staged()
    n_total = len(rows)
    if not rows:
        return 0, 0

    n_normalized = 0
    for r in rows:
        old = r.get("apply_url", "")
        new = _normalize_linkedin_url(old)
        if new != old:
            r["apply_url"] = new
            n_normalized += 1

    save_staged(rows)
    return n_normalized, n_total


def fetch_from_sample(path: Path, dry_run: bool = False) -> int:
    """Parse a local .eml as if it had been fetched. For testing without IMAP."""
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw)
    mid = (msg.get("Message-ID") or "").strip()
    seen = load_seen_ids()
    if mid and mid in seen:
        print(f"Sample already in seen_message_ids; skipping. (mid={mid})")
        print("FETCHED: 0")
        return 0

    html = _extract_html(msg)
    if not html:
        print("ERROR: sample has no HTML body")
        sys.exit(2)

    subject = _decode_subject(msg.get("Subject"))
    parsed  = parse_linkedin_alert(html)

    staged      = load_staged()
    staged_ids  = {s.get("linkedin_job_id") for s in staged if s.get("linkedin_job_id")}
    staged_urls = {s.get("apply_url")       for s in staged if s.get("apply_url")}

    new_jobs = []
    for j in parsed:
        if j["linkedin_job_id"] in staged_ids:
            continue
        if j["apply_url"] in staged_urls:
            continue
        j.update({
            "staging_id":         uuid.uuid4().hex[:12],
            "source_message_id":  mid,
            "source_subject":     subject,
            "fetched_at":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        new_jobs.append(j)
        staged_ids.add(j["linkedin_job_id"])
        staged_urls.add(j["apply_url"])

    print(f"  sample: parsed {len(parsed)}, kept {len(new_jobs)}")

    if new_jobs and not dry_run:
        staged.extend(new_jobs)
        save_staged(staged)
        if mid:
            add_seen_ids({mid})

    print(f"FETCHED: {len(new_jobs)}")
    return len(new_jobs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch LinkedIn job-alert emails via IMAP.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse only; do not mark \\Seen, do not persist staged/state files.")
    parser.add_argument("--sample", metavar="EML_PATH",
                        help="Parse a local .eml file instead of connecting to IMAP.")
    parser.add_argument("--reset", action="store_true",
                        help="Clear local dedup state and unflag previously-ingested messages "
                             "on the server so they can be re-fetched. Preserves \\Seen on any "
                             "LinkedIn alerts you'd naturally read outside the fetch flow.")
    parser.add_argument("--rehydrate", action="store_true",
                        help="Normalize URLs in existing staged rows (strip /comm/ from "
                             "LinkedIn URLs). Useful after a parser upgrade — avoids "
                             "reset + IMAP re-fetch. Does not auto-fetch JDs.")
    args = parser.parse_args()

    if args.reset:
        n_local, n_staged, n_server = reset_seen_state()
        print(
            f"Cleared {n_local} local Message-ID(s); "
            f"cleared {n_staged} staged row(s); "
            f"unflagged {n_server} message(s) on server."
        )
        print(f"RESET: local={n_local} staged={n_staged} server={n_server}")
        return

    if args.rehydrate:
        nn, nt = rehydrate_staged()
        print(f"Rehydrated {nt} staged row(s): normalized {nn} URL(s).")
        print(f"REHYDRATE: normalized={nn} total={nt}")
        return

    if args.sample:
        fetch_from_sample(Path(args.sample), dry_run=args.dry_run)
    else:
        fetch_via_imap(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
