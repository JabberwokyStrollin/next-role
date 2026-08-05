"""
inbox_scan.py — Scan the mailbox for rejection letters and interview requests.

Connects via IMAP, looks at INBOX messages received within the last
config.INBOX_SCAN_WINDOW_DAYS days (default 14), matches each to an *open*
application in data/application_tracker.json (by company name in the sender /
subject, or by the sender's domain label), and deterministically classifies it
as a rejection or an interview request via config.classify_inbox_email. Matches
are written to data/inbox_matches.json for the /today "Status updates" UI to
surface for one-click review — nothing mutates application status here.

Read-flag safety. Unlike linkedin_fetch.py, this scanner NEVER marks messages
\\Seen: every fetch uses BODY.PEEK, and it issues no STORE. It keeps its own
processed-Message-ID list in data/inbox_scan_state.json — every message whose
body it examined, matched or not — so reading mail in your own client neither
hides matches from us nor is changed by us, and a re-scan does no repeat work.

Credentials via the same env vars as linkedin_fetch.py:
    NEXTROLE_IMAP_HOST          (e.g. imap.gmail.com)
    NEXTROLE_IMAP_USER          (full email address)
    NEXTROLE_IMAP_APP_PASSWORD  (Gmail/provider app password — NOT login password)

Usage:
    python scripts/inbox_scan.py
    python scripts/inbox_scan.py --dry-run          # parse only, no state/matches write
    python scripts/inbox_scan.py --window-days 30    # override the look-back window
    python scripts/inbox_scan.py --sample FILE       # classify a local .eml (no IMAP)
    python scripts/inbox_scan.py --reset             # clear matches + processed state

Output (last line of stdout, machine-readable):
    SCANNED: <n_new_matches>   on success
    ERROR: <message>           on failure
"""

import argparse
import email
import imaplib
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

from config import (
    APPLICATION_TRACKER_PATH,
    INBOX_SCAN_WINDOW_DAYS,
    classify_inbox_email,
    load_json,
    now_utc,
    save_json,
)
# Reuse the IMAP credential loader so the two mailbox tools never diverge.
from linkedin_fetch import get_creds

ROOT          = Path(__file__).parent.parent
DATA_DIR      = ROOT / "data"
INBOX_MATCHES = DATA_DIR / "inbox_matches.json"
INBOX_STATE   = DATA_DIR / "inbox_scan_state.json"

# Applications in these statuses are done — a rejection/interview email can no
# longer change them, so they're excluded from matching. Every other status
# (applied / recruiter_screen / interview / ghosted) is "open": a real reply to
# a ghosted application resurrects it, so ghosted stays in scope.
TERMINAL_STATUSES = frozenset({"rejected", "offer", "withdrawn"})

# Messages per header FETCH. The scan needs the headers of every message in the
# window (the Message-ID dedup key is only knowable after fetching), but one
# FETCH per message is one network round trip per message: a routine 400-message
# 14-day window cost ~170s on Gmail, which — plus body fetches and login —
# overran serve.py's 300s subprocess timeout and surfaced as "Inbox scan timed
# out after 300s". Batched, the same window is a few round trips (~4s).
HEADER_BATCH_SIZE = 100

# Leading sequence number of an untagged FETCH response line: b'22546 (BODY[...'
_FETCH_SEQ_RE = re.compile(rb"^\s*(\d+)\s+\(")

# Socket timeout (seconds) for the IMAP connection. Without one, a stalled TCP
# connect or a mid-scan server stall blocks forever and the only thing that ends
# the scan is serve.py's 300s subprocess kill — which reports "Inbox scan timed
# out after 300s" with no clue whether the mailbox was slow or the network hung.
# This bounds a single blocking socket operation, not the whole scan, so it can
# be well under the 300s cap: the largest individual fetch is a ~100-message
# header batch (~2s) and data keeps flowing throughout.
IMAP_TIMEOUT_SECONDS = 60

# Company-name tokens too generic to match on (dropped from the match phrase).
_GENERIC_CO_TOKENS = {
    "inc", "llc", "ltd", "limited", "corp", "co", "company", "technologies",
    "technology", "labs", "software", "systems", "solutions", "group",
    "holdings", "the", "and",
}


# ── State / matches I/O ───────────────────────────────────────────────────────

def load_matches() -> list[dict]:
    return load_json(INBOX_MATCHES)


def save_matches(rows: list[dict]) -> None:
    save_json(INBOX_MATCHES, rows)


def load_processed_ids() -> set[str]:
    if not INBOX_STATE.exists():
        return set()
    try:
        data = json.loads(INBOX_STATE.read_text(encoding="utf-8"))
        return set(data.get("processed_message_ids", []))
    except Exception:
        return set()


def add_processed_ids(new_ids: set[str]) -> None:
    if not new_ids:
        return
    existing = load_processed_ids() | new_ids
    INBOX_STATE.write_text(
        json.dumps({"processed_message_ids": sorted(existing)}, indent=2),
        encoding="utf-8",
    )


def load_open_applications() -> list[dict]:
    """Applications still able to receive a rejection/interview reply."""
    apps = load_json(APPLICATION_TRACKER_PATH)
    return [a for a in apps if a.get("status") not in TERMINAL_STATUSES]


# ── Header / body parsing ─────────────────────────────────────────────────────

def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _extract_text(msg: email.message.Message) -> str:
    """Return the message body as plain text — prefer text/plain, else strip HTML."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            if part.get("Content-Disposition", "").lower().startswith("attachment"):
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text

    if plain.strip():
        body = plain
    elif html.strip():
        body = BeautifulSoup(html, "html.parser").get_text(separator="\n")
    else:
        body = ""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return "\n".join(lines)


def _received_date(msg: email.message.Message) -> str:
    raw = msg.get("Date")
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        return ""


# ── Company matching ──────────────────────────────────────────────────────────

def _company_core_tokens(name: str) -> list[str]:
    toks = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return [t for t in toks if t and t not in _GENERIC_CO_TOKENS]


def _company_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _sender_domain_label(from_header: str) -> str:
    """Registrable-ish label of the sender domain (second-to-last dotted part)."""
    m = re.search(r"@([a-z0-9.\-]+)", (from_header or "").lower())
    if not m:
        return ""
    parts = [p for p in m.group(1).split(".") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def company_matches(company_name: str, from_header: str, subject: str) -> bool:
    """True if the message plausibly relates to ``company_name`` — the name as a
    whole phrase in the From/Subject text, or a slug overlap with the sender's
    domain label. ATS relays (greenhouse.io, lever.co, …) name the company in
    the From-display / subject, so the phrase match covers them."""
    slug = _company_slug(company_name)
    if not slug or len(slug) < 3:
        return False

    hay = " " + re.sub(r"[^a-z0-9]+", " ", f"{from_header} {subject}".lower()) + " "
    core = _company_core_tokens(company_name)
    if core and f" {' '.join(core)} " in hay:
        return True

    dom_label = _sender_domain_label(from_header)
    if len(dom_label) >= 4 and (dom_label in slug or slug in dom_label):
        return True
    return False


def _title_tokens(title: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split() if len(t) > 3}


def match_application(open_apps: list[dict], from_header: str, subject: str,
                      body: str) -> dict | None:
    """Pick the open application this message relates to, or None. When several
    open applications share the matched company, prefer the one whose title
    tokens appear in the subject/body; otherwise the first candidate."""
    candidates = [a for a in open_apps
                  if company_matches(a.get("company_name", ""), from_header, subject)]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    text = f"{subject}\n{body}".lower()
    text_norm = set(re.sub(r"[^a-z0-9]+", " ", text).split())
    best, best_overlap = candidates[0], -1
    for a in candidates:
        overlap = len(_title_tokens(a.get("title", "")) & text_norm)
        if overlap > best_overlap:
            best, best_overlap = a, overlap
    return best


def _message_key(mid: str, from_header: str, subject: str, received: str) -> str:
    """Stable dedup key — the Message-ID when present, else a content digest."""
    mid = (mid or "").strip()
    if mid:
        return mid
    return "nomid:" + _company_slug(f"{received}{from_header}{subject}")[:48]


def build_match(app: dict, from_header: str, subject: str, received: str,
                email_status: str, email_reason: str | None, evidence: str,
                mid: str) -> dict:
    """Assemble one staged-match record. Stores the raw *email signal*
    (``email_status`` / ``email_reason`` from ``classify_inbox_email``); the
    concrete status the operator applies is derived from this signal AND the
    application's live status at surface time (``config.suggest_status_transition``
    via ``serve.py``), never frozen here — so a status change between scan and
    review is reflected."""
    return {
        "match_id":         uuid.uuid4().hex[:12],
        "message_id":       mid,
        "application_id":   app.get("application_id"),
        "job_id":           app.get("job_id"),
        "company_name":     app.get("company_name"),
        "title":            app.get("title"),
        "app_status":       app.get("status"),
        "from_addr":        from_header[:200],
        "subject":          subject[:200],
        "received":         received,
        "email_status":     email_status,
        "email_reason":     email_reason,
        "evidence":         evidence[:200],
        "detected_at":      now_utc(),
    }


# ── IMAP scan ─────────────────────────────────────────────────────────────────

def _since_date(window_days: int) -> str:
    """IMAP SINCE token (DD-Mon-YYYY) for `window_days` ago, UTC."""
    dt = datetime.now(timezone.utc) - timedelta(days=window_days)
    return dt.strftime("%d-%b-%Y")


def _fetch_header_batch(M: imaplib.IMAP4, seq_nums: list[bytes]) -> dict[bytes, bytes]:
    """Peek the match-relevant headers of many messages in one FETCH round trip.

    Returns ``{sequence_number: raw_header_bytes}``. Messages the server omits
    are simply absent — the caller skips them, exactly as it skipped a per-message
    fetch that came back non-OK. ``BODY.PEEK`` keeps the \\Seen guarantee."""
    if not seq_nums:
        return {}
    typ, resp = M.fetch(
        b",".join(seq_nums), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)])")
    if typ != "OK" or not resp:
        return {}
    out: dict[bytes, bytes] = {}
    for item in resp:
        # Each message is a (prefix, literal) tuple; the b')' terminators between
        # them are plain bytes and carry no payload.
        if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
            continue
        m = _FETCH_SEQ_RE.match(item[0] or b"")
        if m:
            out[m.group(1)] = item[1]
    return out


def scan_via_imap(window_days: int, dry_run: bool = False) -> int:
    """Scan INBOX for rejection/interview replies to open applications.
    Returns the count of new matches staged."""
    open_apps = load_open_applications()
    if not open_apps:
        print("No open applications to match against.")
        print("SCANNED: 0")
        return 0

    processed     = load_processed_ids()
    matches       = load_matches()
    matched_ids   = {m.get("message_id") for m in matches if m.get("message_id")}
    seen_keys     = processed | matched_ids

    host, user, pw = get_creds()
    print(f"Connecting to {host} as {user}...")
    try:
        M = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT_SECONDS)
    except (TimeoutError, OSError) as e:
        print(f"ERROR: could not connect to {host}: {e}")
        sys.exit(2)
    try:
        M.login(user, pw)
    except imaplib.IMAP4.error as e:
        print(f"ERROR: IMAP login failed: {e}")
        sys.exit(2)

    M.select("INBOX")
    since = _since_date(window_days)
    typ, data = M.search(None, "SINCE", since)
    if typ != "OK":
        print(f"ERROR: IMAP search failed ({typ})")
        M.logout()
        sys.exit(2)
    uids = data[0].split() if data and data[0] else []
    print(f"  {len(uids)} message(s) since {since}; matching against "
          f"{len(open_apps)} open application(s)")

    new_matches: list[dict] = []
    new_processed: set[str] = set()

    # Headers in batches — BODY.PEEK never sets \Seen. Bodies stay per-message:
    # batching them saves little (they're transfer-bound, ~0.35s either way);
    # what keeps them cheap is not re-examining them on the next scan.
    for start in range(0, len(uids), HEADER_BATCH_SIZE):
        batch   = uids[start:start + HEADER_BATCH_SIZE]
        headers = _fetch_header_batch(M, batch)

        for uid in batch:
            raw_hdr = headers.get(uid)
            if not raw_hdr:
                continue
            hmsg     = email.message_from_bytes(raw_hdr)
            from_hdr = _decode_header(hmsg.get("From"))
            subject  = _decode_header(hmsg.get("Subject"))
            mid      = (hmsg.get("Message-ID") or "").strip()
            received = _received_date(hmsg)
            key      = _message_key(mid, from_hdr, subject, received)

            if key in seen_keys:
                continue

            # Cheap company match on headers before pulling the full body.
            header_candidates = [
                a for a in open_apps
                if company_matches(a.get("company_name", ""), from_hdr, subject)
            ]
            if not header_candidates:
                continue

            typ, body_data = M.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not body_data or not body_data[0]:
                continue
            fmsg = email.message_from_bytes(body_data[0][1])
            body = _extract_text(fmsg)

            # The body is downloaded and classified — record the message as
            # processed whatever the verdict. Message content is immutable, so a
            # no-signal message can never become a match later, and re-examining
            # it means re-downloading the body on every future scan (the bulk of
            # the scan's wall-clock: company names appear in job-alert subjects,
            # so ~40% of a window reaches this point). `--reset` re-examines
            # everything if the classifier changes.
            new_processed.add(key)
            seen_keys.add(key)

            app = match_application(header_candidates, from_hdr, subject, body)
            if not app:
                continue

            status, reason, evidence = classify_inbox_email(subject, body)
            if not status:
                continue  # matched a company but no rejection/interview signal

            new_matches.append(
                build_match(app, from_hdr, subject, received, status, reason, evidence, key))
            print(f"    match: {app.get('company_name')} — {status}"
                  f"{('/' + reason) if reason else ''} · {subject[:60]}")

    try:
        M.logout()
    except Exception:
        pass

    if not dry_run:
        if new_matches:
            matches.extend(new_matches)
            save_matches(matches)
        # Written even with zero matches — that's what keeps the next scan cheap.
        add_processed_ids(new_processed)

    print(f"SCANNED: {len(new_matches)}")
    return len(new_matches)


def scan_from_sample(path: Path, dry_run: bool = False) -> int:
    """Classify a local .eml against open applications. For testing without IMAP."""
    raw  = path.read_bytes()
    msg  = email.message_from_bytes(raw)
    from_hdr = _decode_header(msg.get("From"))
    subject  = _decode_header(msg.get("Subject"))
    mid      = (msg.get("Message-ID") or "").strip()
    received = _received_date(msg)
    body     = _extract_text(msg)

    open_apps = load_open_applications()
    app = match_application(open_apps, from_hdr, subject, body)
    status, reason, evidence = classify_inbox_email(subject, body)

    print(f"  from:    {from_hdr[:80]}")
    print(f"  subject: {subject[:80]}")
    print(f"  matched: {app.get('company_name') if app else '(no open application)'}")
    print(f"  class:   {status or '(none)'}"
          f"{('/' + reason) if reason else ''}")
    if evidence:
        print(f"  evidence: {evidence}")

    if not (app and status):
        print("SCANNED: 0")
        return 0

    key = _message_key(mid, from_hdr, subject, received)
    if not dry_run:
        matches = load_matches()
        if key not in {m.get("message_id") for m in matches}:
            matches.append(
                build_match(app, from_hdr, subject, received, status, reason, evidence, key))
            save_matches(matches)
            add_processed_ids({key})

    print("SCANNED: 1")
    return 1


def reset_state() -> tuple[int, int]:
    """Clear staged matches and the processed-Message-ID list. Returns
    (n_matches_cleared, n_processed_cleared)."""
    n_matches   = len(load_matches())
    n_processed = len(load_processed_ids())
    if INBOX_MATCHES.exists():
        INBOX_MATCHES.unlink()
    if INBOX_STATE.exists():
        INBOX_STATE.unlink()
    return n_matches, n_processed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan the mailbox for rejection/interview replies to open applications.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify only; do not write matches/state files.")
    parser.add_argument("--window-days", type=int, default=INBOX_SCAN_WINDOW_DAYS,
                        help=f"Look-back window in days (default {INBOX_SCAN_WINDOW_DAYS}).")
    parser.add_argument("--sample", metavar="EML_PATH",
                        help="Classify a local .eml file instead of connecting to IMAP.")
    parser.add_argument("--reset", action="store_true",
                        help="Clear staged matches and processed-Message-ID state.")
    args = parser.parse_args()

    if args.reset:
        n_m, n_p = reset_state()
        print(f"Cleared {n_m} staged match(es); cleared {n_p} processed id(s).")
        print(f"RESET: matches={n_m} processed={n_p}")
        return

    if args.sample:
        scan_from_sample(Path(args.sample), dry_run=args.dry_run)
    else:
        try:
            scan_via_imap(args.window_days, dry_run=args.dry_run)
        except (TimeoutError, OSError, imaplib.IMAP4.abort) as e:
            # A stall partway through the scan trips IMAP_TIMEOUT_SECONDS (or the
            # server drops us). Report it on the machine-readable ERROR line
            # serve.py surfaces instead of dying with a traceback. This run's
            # progress is dropped — matches and processed ids are only written
            # once the scan completes — so the next scan simply redoes it.
            print(f"ERROR: IMAP connection failed mid-scan: {e}")
            sys.exit(2)


if __name__ == "__main__":
    main()
