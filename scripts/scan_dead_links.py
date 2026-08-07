"""
scan_dead_links.py — Verify that queued postings still accept applications, and
archive the ones that don't.

Nothing re-checks a posting after ingest: `date_last_verified` is written once
and never updated. Employers pull listings constantly, so the apply queue
accumulates roles that 404 or say "no longer accepting applications" — the
operator was hitting several per day while working the top of the queue.

Default target is the TOP of the apply queue, not the whole pipeline. The roles
about to be applied to are the ones worth a network round trip; verifying all
~790 active rows would be slow, rude to the hosts, and mostly wasted on rows
that will never surface.

**LinkedIn is skipped by default.** A third of active rows carry a
linkedin.com apply URL, and bulk-fetching those got this project soft-banned
once already. They're also unreliable to classify: LinkedIn answers with a 200
auth-wall for live *and* dead postings, so a check would read "alive" either
way. `--include-linkedin` exists for deliberate small runs and is capped and
slowed accordingly.

Detection, calibrated against real boards:
  - Lever serves a dead posting as **404**.
  - Greenhouse serves one as **200 + redirect** to the careers home
    ("…/jobs/0000000" → "…/company/careers/open-positions"), so status alone is
    not enough — the job id vanishing from the final URL is the signal.
  - Some boards keep the URL and add "no longer accepting applications".

Anything ambiguous — 403, 429, 5xx, timeouts, connection errors — is classified
`unknown` and NEVER archived. A false positive silently deletes a good role from
the queue; a false negative just means the operator clicks one dead link.

Usage:
    python scripts/scan_dead_links.py                     # dry-run, top of queue
    python scripts/scan_dead_links.py --apply             # archive confirmed dead
    python scripts/scan_dead_links.py --limit 100         # check more
    python scripts/scan_dead_links.py --all               # every active non-LinkedIn row
    python scripts/scan_dead_links.py --include-linkedin  # opt in, capped + slowed
"""

import argparse
import re
import shutil
import sys
import time
import uuid as uuid_lib
from urllib.parse import urlparse

import requests

from config import (
    COMPANY_REGISTRY_PATH,
    JOB_PIPELINE_PATH,
    PROCESS_LOG_PATH,
    apply_queue_order,
    load_json,
    save_json,
    now_utc,
    today,
)

ARCHIVE_REASON = "dead link ({detail})"
_DEFAULT_STATUSES = {"active", "cover_letter_ready"}
_DEFAULT_LIMIT    = 40

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

REQUEST_DELAY_S  = 1.2   # between requests generally
LINKEDIN_DELAY_S = 6.0   # deliberately slow — see the module docstring
LINKEDIN_CAP     = 15    # hard ceiling per run even with --include-linkedin

# Statuses that say nothing about the posting and are worth one more try.
#
# A 517-row run produced 16 x HTTP 202 (Toast, GoDaddy) and 8 x 403 (ZoomInfo).
# What was actually going on took a few passes to pin down, and the honest
# answer is that some hosts are simply not verifiable from here:
#   - A single hand request to a Toast URL, before any bulk run, returned 200
#     with the job title in the body — so the postings are live.
#   - Retrying recovered only 4 of 26.
#   - Spacing those requests ~21s apart still returned 202 every time.
# So it is IP-level throttling with a cooldown longer than a run, not a
# per-request transient. Retrying is still worth its one extra request for the
# genuinely transient cases, but it does NOT fix Toast or ZoomInfo.
#
# Those rows stay `unknown` and are never archived, which is the correct
# outcome: unverifiable is not dead. Do NOT be tempted to treat 202 as alive —
# that stamps date_last_verified off a throttle response whose body was never
# the posting.
_TRANSIENT_STATUSES = frozenset({202, 408, 425, 429, 403, 500, 502, 503, 504})
RETRY_BACKOFF_S = 4.0
FETCH_ATTEMPTS  = 2

# Phrases boards use when the URL still resolves but applications are closed.
_DEAD_TEXT_RE = re.compile(
    r"no longer accepting applications"
    r"|this (?:job|position|posting) is no longer"
    r"|position (?:has been|is) (?:filled|closed)"
    r"|posting (?:has been|is) closed"
    r"|job (?:posting )?not found"
    r"|this job is closed",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def is_linkedin(url: str) -> bool:
    return "linkedin.com" in (url or "").lower()


def _job_token(url: str) -> str:
    """Last meaningful path segment — the posting id for every ATS we crawl."""
    path = urlparse(url or "").path.rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else ""


def _classify_ashby(url: str, session: requests.Session | None = None) -> tuple[str, str]:
    """Ashby needs its API, not its HTML.

    ``jobs.ashbyhq.com`` is a client-rendered SPA: it answers 200 with the same
    shell for a live posting and for a fabricated id, so the generic checks
    below read every Ashby row as alive. The public posting API — the same one
    ``crawl.fetch_ashby`` uses — lists the live postings, so membership is an
    exact answer instead of a guess."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return "unknown", "unparseable ashby url"
    slug, job_id = parts[0], parts[1]
    get = (session or requests).get
    try:
        resp = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                   headers=HEADERS, timeout=20)
    except Exception as e:                                   # noqa: BLE001
        return "unknown", f"{type(e).__name__}"
    if resp.status_code != 200:
        return "unknown", f"board HTTP {resp.status_code}"
    try:
        live = {str(j.get("id")) for j in (resp.json() or {}).get("jobs", [])}
    except Exception:                                        # noqa: BLE001
        return "unknown", "board JSON error"
    if not live:
        # An empty board is more likely a slug/API change than every posting
        # closing at once — don't archive a whole company on it.
        return "unknown", "board returned no postings"
    return ("alive", "listed on board") if job_id in live else ("dead", "not on Ashby board")


def classify_url(url: str, session: requests.Session | None = None) -> tuple[str, str]:
    """Return ``(verdict, detail)`` with verdict in ``dead`` | ``alive`` | ``unknown``.

    Biased hard toward ``unknown``: only an unambiguous signal returns ``dead``,
    because archiving a live role costs the operator an opportunity while a
    missed dead one costs a single click."""
    if not url:
        return "unknown", "no apply_url"
    if "jobs.ashbyhq.com" in url:
        return _classify_ashby(url, session)

    # One retry after a backoff on transient answers. A definitive status (404,
    # 410, 200) short-circuits immediately — only the ambiguous ones cost a
    # second request, so a healthy run is no slower.
    resp, failure = None, ("unknown", "no response")
    for attempt in range(FETCH_ATTEMPTS):
        try:
            r = (session or requests).get(url, headers=HEADERS, timeout=20,
                                         allow_redirects=True)
            if r.status_code not in _TRANSIENT_STATUSES:
                resp = r
                break
            failure = ("unknown", f"HTTP {r.status_code}")
        except requests.Timeout:
            failure = ("unknown", "timeout")
        except Exception as e:                               # noqa: BLE001
            failure = ("unknown", f"{type(e).__name__}")
        if attempt + 1 < FETCH_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_S)
    if resp is None:
        # Still ambiguous after the retry — say so rather than guess. Reported
        # detail keeps the last observed cause so a run's unknowns are readable.
        return failure

    if resp.status_code in (404, 410):
        return "dead", f"HTTP {resp.status_code}"
    if resp.status_code != 200:
        # Anything else non-200 that isn't in the transient set — nothing about
        # the posting can be concluded from it.
        return "unknown", f"HTTP {resp.status_code}"

    # Greenhouse (and others) answer 200 and bounce a removed posting to the
    # careers index. The posting id disappearing from the final URL is what
    # distinguishes that from a cosmetic redirect (trailing slash, locale).
    token = _job_token(url)
    if resp.url != url and token and token not in resp.url:
        return "dead", f"redirected to {urlparse(resp.url).path[:48] or '/'}"

    m = _DEAD_TEXT_RE.search(_TAG_RE.sub(" ", resp.text[:300000]))
    if m:
        return "dead", f'"{m.group(0)[:40]}"'
    return "alive", f"HTTP 200"


def select_targets(jobs: list[dict], co_by_id: dict, limit: int,
                   include_linkedin: bool, check_all: bool) -> list[dict]:
    """Rows to verify, apply-queue order first so the most imminent are covered
    when a limit applies."""
    pool = [j for j in jobs if j.get("pipeline_status") in _DEFAULT_STATUSES]
    if not include_linkedin:
        pool = [j for j in pool if not is_linkedin(j.get("apply_url"))]
    ordered = apply_queue_order(pool, co_by_id)
    if check_all:
        return ordered
    return ordered[:limit]


def interleave_by_host(rows: list[dict]) -> list[dict]:
    """Round-robin the selected rows across apply-URL hosts, preserving order
    within each host.

    Selection is by apply-queue rank, which clusters one company's rows
    together — Toast had ~20 consecutive rows, so the 1.2s global delay meant 20
    hits on one host in 24 seconds. Interleaving spreads each host's rows across
    the whole run at no extra wall-clock cost: measured on 408 rows it cuts the
    worst same-host run from 24 to 3 and lifts Toast's minimum gap from 1 row to
    18 (~21s).

    This is a politeness/burstiness improvement, and it is worth having on those
    grounds alone — but it did **not** recover the hosts that throttled. Toast
    still answers 202 at 21s spacing (see `_TRANSIENT_STATUSES`). Don't read a
    low unknown count as proof this fixed anything.

    Order here affects only request sequencing, never which rows are checked —
    the ``--limit`` cut happens before this, on rank.

    Each row is placed at its *fractional* position within its own host —
    ``(i + 0.5) / n`` — and everything is then sorted on that. Plain round-robin
    is not enough: one host dominates (Greenhouse held 193 of 408 rows), so
    draining a row per host per pass empties the small buckets early and leaves
    the dominant host as one solid block at the tail. Measured, that turned a
    worst-case run of 24 into 128. Fractional spreading keeps every host evenly
    distributed across the whole sequence regardless of how lopsided the mix is.
    """
    buckets: dict = {}
    for r in rows:
        buckets.setdefault(urlparse(r.get("apply_url") or "").netloc, []).append(r)
    keyed: list[tuple[float, str, dict]] = []
    for host, group in buckets.items():
        n = len(group)
        for i, r in enumerate(group):
            keyed.append(((i + 0.5) / n, host, r))
    keyed.sort(key=lambda k: (k[0], k[1]))       # host name breaks ties, so it's stable
    return [r for _pos, _host, r in keyed]


def scan_dead_links(apply: bool = False, limit: int = _DEFAULT_LIMIT,
                    include_linkedin: bool = False, check_all: bool = False,
                    verbose: bool = True) -> tuple[int, int, int]:
    """Verify targets and, when ``apply``, archive the confirmed-dead and stamp
    ``date_last_verified`` on the confirmed-alive. Returns
    ``(n_dead, n_alive, n_unknown)``."""
    jobs     = load_json(JOB_PIPELINE_PATH)
    co_by_id = {c["company_id"]: c for c in load_json(COMPANY_REGISTRY_PATH)}
    # Rank-order + limit first (so the most imminent roles are the ones covered),
    # then interleave hosts for the request loop to avoid per-host throttling.
    targets  = interleave_by_host(
        select_targets(jobs, co_by_id, limit, include_linkedin, check_all))

    dead, alive, unknown = [], [], []
    li_seen = 0
    session = requests.Session()
    for i, j in enumerate(targets, 1):
        url = j.get("apply_url")
        li  = is_linkedin(url)
        if li:
            if li_seen >= LINKEDIN_CAP:
                continue
            li_seen += 1
        verdict, detail = classify_url(url, session)
        (dead if verdict == "dead" else alive if verdict == "alive" else unknown).append((j, detail))
        if verbose:
            mark = {"dead": "DEAD ", "alive": "ok   ", "unknown": "?    "}[verdict]
            # flush per row: Python block-buffers stdout when it's redirected to
            # a file, so a 517-row run sat at 0 bytes for 20 minutes with no way
            # to tell progress from a hang.
            print(f"  [{i}/{len(targets)}] {mark} {detail[:34]:36} "
                  f"{(j.get('company_name') or '?')[:16]:18} {(j.get('title') or '?')[:38]}",
                  flush=True)
        time.sleep(LINKEDIN_DELAY_S if li else REQUEST_DELAY_S)

    if not apply:
        return len(dead), len(alive), len(unknown)

    if dead or alive:
        shutil.copyfile(JOB_PIPELINE_PATH,
                        JOB_PIPELINE_PATH.with_suffix(JOB_PIPELINE_PATH.suffix + ".bak"))
    log = load_json(PROCESS_LOG_PATH)
    now = now_utc()
    for j, detail in dead:
        reason = ARCHIVE_REASON.format(detail=detail)
        j["pipeline_status"] = "archived"
        j["archived_at"]     = now
        j["archived_reason"] = reason
        log.append({
            "log_id":       str(uuid_lib.uuid4()),
            "timestamp":    now,
            "session_date": today(),
            "event_type":   "job_archived",
            "entity_type":  "job",
            "entity_id":    j.get("job_id"),
            "entity_name":  f"{j.get('company_name','?')} -- {j.get('title','?')}",
            "source_url":   j.get("apply_url"),
            "detail":       f"Retroactive archive: {reason}.",
        })
    # Only the confirmed-alive get a fresh timestamp; an `unknown` must not look
    # verified, or a blocked host would mark half the pipeline as checked.
    for j, _ in alive:
        j["date_last_verified"] = now
    if dead or alive:
        save_json(JOB_PIPELINE_PATH, jobs)
    if dead:
        save_json(PROCESS_LOG_PATH, log)
    return len(dead), len(alive), len(unknown)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Verify queued postings still accept applications.")
    p.add_argument("--apply", action="store_true",
                   help="Archive confirmed-dead rows. Default is dry-run.")
    p.add_argument("--limit", type=int, default=_DEFAULT_LIMIT,
                   help=f"How many queue rows to check (default {_DEFAULT_LIMIT}).")
    p.add_argument("--all", action="store_true", dest="check_all",
                   help="Check every active row, not just the top of the queue.")
    p.add_argument("--include-linkedin", action="store_true",
                   help=f"Also check linkedin.com URLs (capped at {LINKEDIN_CAP}, "
                        f"{LINKEDIN_DELAY_S}s apart). Off by default — bulk LinkedIn "
                        f"fetching has earned a soft ban before, and its auth-wall "
                        f"answers 200 for live and dead postings alike.")
    args = p.parse_args()

    n_dead, n_alive, n_unknown = scan_dead_links(
        apply=args.apply, limit=args.limit,
        include_linkedin=args.include_linkedin, check_all=args.check_all,
    )
    print(f"\nChecked: {n_dead + n_alive + n_unknown}   "
          f"dead={n_dead}  alive={n_alive}  unknown={n_unknown}")
    if n_unknown:
        print("  (unknown = blocked / rate-limited / network error — never archived)")
    if not args.apply:
        if n_dead:
            print(f"\nDry run -- pass --apply to archive these {n_dead} dead link(s).")
        else:
            print("\nNothing dead found.")
    else:
        print(f"\nArchived {n_dead}; stamped date_last_verified on {n_alive}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
