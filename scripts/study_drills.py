"""
study_drills.py — daily Algorithm and Interview drills for the /today page.

Two question banks live in profile/ as plain markdown, one item per
``## N. Title`` heading:

  - profile/algorithm_reference.md  — assessment-style algorithm patterns, each
    with a problem, recognition cues, a Java and a Python reference solution,
    complexity and gotchas.
  - profile/behavioral_reference.md — common interview questions, each with what
    it tests, the resume story it draws on, a model answer and follow-ups.

Each day /today shows ONE item from each bank. The pick is the item touched
longest ago (never-completed first, then doc order), so the whole bank rotates
before anything repeats. Completing an item moves the pick on; a daily goal
(config.DAILY_ALGORITHM_GOAL / DAILY_INTERVIEW_GOAL) earns the section's
checkmark.

Algorithm items also get an in-depth explanation, generated ONCE per pattern
by Claude (CL_MODEL / Sonnet) from the reference entry and cached in
data/algorithm_drills.json together with a hash of the entry it was written
from — so editing the reference marks the explanation stale instead of
silently showing an explanation of the old text. Interview items are rendered
straight from the doc; they cost nothing.

Usage:
    python scripts/study_drills.py list
    python scripts/study_drills.py explain --number 7
    python scripts/study_drills.py explain --all          # pre-generate every pattern
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from config import (
    ALGORITHM_DRILLS_PATH,
    ALGORITHM_REFERENCE_PATH,
    BEHAVIORAL_REFERENCE_PATH,
    CL_MODEL,
    DAILY_ALGORITHM_GOAL,
    DAILY_INTERVIEW_GOAL,
    DRILL_LANGUAGES,
    INTERVIEW_DRILLS_PATH,
    load_json,
    now_utc,
    save_json,
    today,
)

# ─── Kinds ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StudyKind:
    """One question bank: where its items come from and where progress goes."""
    key:       str    # "algorithm" | "interview" — the URL + CLI value
    label:     str
    reference: Path   # profile/*.md question bank
    store:     Path   # data/*.json progress records
    goal:      int    # items per day that earn the /today checkmark


STUDY_KINDS: dict[str, StudyKind] = {
    "algorithm": StudyKind("algorithm", "Algorithm drill",
                           ALGORITHM_REFERENCE_PATH, ALGORITHM_DRILLS_PATH,
                           DAILY_ALGORITHM_GOAL),
    "interview": StudyKind("interview", "Interview drill",
                           BEHAVIORAL_REFERENCE_PATH, INTERVIEW_DRILLS_PATH,
                           DAILY_INTERVIEW_GOAL),
}


def study_kind(key: str) -> StudyKind:
    """Resolve a kind key. Raises KeyError on an unknown key — callers that take
    the key from a URL check membership in STUDY_KINDS first."""
    return STUDY_KINDS[key]


# ─── Reference parsing ────────────────────────────────────────────────────────

_ITEM_HEADING_RE = re.compile(r"(?m)^## (\d+)\.\s+(.+?)\s*$")
_FENCE_RE        = re.compile(r"```(\w+)\n(.*?)```", re.S)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_reference(path: Path) -> list[dict]:
    """Every ``## N. Title`` item in a reference doc, in doc order:
    ``{"number", "title", "body", "hash"}``. Unnumbered ``##`` sections (the
    pattern-spotting table, the story map) are context, not items, and are
    skipped. A missing file yields ``[]`` so /today degrades to a notice."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    heads = list(_ITEM_HEADING_RE.finditer(text))
    items = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        # A later unnumbered `## ` section ends the item too.
        nxt = re.search(r"(?m)^## (?!\d+\.)", body)
        if nxt:
            body = body[:nxt.start()]
        body = re.sub(r"\n-{3,}\s*$", "", body.strip()).strip()
        items.append({"number": int(m.group(1)), "title": m.group(2).strip(),
                      "body": body, "hash": _hash(m.group(2) + "\n" + body)})
    return items


def reference_items(kind: str) -> list[dict]:
    return parse_reference(study_kind(kind).reference)


def find_item(kind: str, number: int) -> dict | None:
    return next((it for it in reference_items(kind) if it["number"] == number),
                None)


def split_algorithm_item(body: str) -> dict:
    """Break an algorithm entry into the parts /today shows separately:

    - ``prompt``    — the problem statement and cues (everything before the
                      first solution heading). Shown as the question.
    - ``solutions`` — ``{language: code}`` from the fenced blocks, keyed by the
                      fence tag (``java`` / ``python``).
    - ``notes``     — complexity + gotchas after the code.
    - ``stubs``     — ``{language: starter}``: the reference's own first
                      function signature with an empty body, so the operator
                      starts from the same name and parameters as the answer.
    """
    first_sol = re.search(r"(?m)^### ", body)
    prompt = body[:first_sol.start()].strip() if first_sol else body
    solutions = {m.group(1).lower(): m.group(2).rstrip()
                 for m in _FENCE_RE.finditer(body)}
    last_fence = list(_FENCE_RE.finditer(body))
    notes = body[last_fence[-1].end():].strip() if last_fence else ""
    return {"prompt": prompt, "solutions": solutions, "notes": notes,
            "stubs": {lang: _stub(lang, code) for lang, code in solutions.items()}}


def _stub(language: str, code: str) -> str:
    """Starter code: the reference's first function signature, body emptied.
    Reveals only the name and parameter types, which the problem statement
    already implies."""
    for line in code.splitlines():
        s = line.strip()
        if language == "python" and s.startswith("def "):
            return f"{s}\n    # your solution\n    pass\n"
        if language == "java" and s.startswith("static ") and s.endswith("{"):
            return f"{s}\n    // your solution\n}}\n"
    return ""


# ─── Progress store ───────────────────────────────────────────────────────────
#
# One record per item number, created on first touch:
#   {"number", "title", "completions": [iso_ts, ...], "skipped_at": iso_ts|None,
#    # algorithm only:
#    "attempts": {language: {"code", "saved_at"}},
#    "explanation": {"text", "at", "model", "source_hash"}}


def load_store(kind: str) -> list:
    data = load_json(study_kind(kind).store)
    return data if isinstance(data, list) else []


def save_store(kind: str, rows: list) -> None:
    save_json(study_kind(kind).store, rows)


def _record(rows: list, number: int, title: str = "") -> dict:
    rec = next((r for r in rows if r.get("number") == number), None)
    if rec is None:
        rec = {"number": number, "title": title, "completions": []}
        rows.append(rec)
    elif title:
        rec["title"] = title
    return rec


def get_record(kind: str, number: int) -> dict:
    """The stored record for an item, or an empty one (not persisted)."""
    return next((r for r in load_store(kind) if r.get("number") == number),
                {"number": number, "completions": []})


def _last_touch(rec: dict | None) -> str:
    """Most recent completion or skip. "" (sorts first) when never touched."""
    if not rec:
        return ""
    return max([*(rec.get("completions") or []), rec.get("skipped_at") or ""])


def current_item(kind: str) -> dict | None:
    """Today's item: least recently touched, ties broken by doc order. Never
    random, so a page refresh shows the same item, and the full bank rotates
    before any item repeats."""
    items = reference_items(kind)
    if not items:
        return None
    by_num = {r.get("number"): r for r in load_store(kind)}
    return min(items, key=lambda it: (_last_touch(by_num.get(it["number"])),
                                      it["number"]))


def completed_today(kind: str, rows: list | None = None) -> int:
    """Items completed today (drives the section goal). Derived, never stored."""
    rows = load_store(kind) if rows is None else rows
    t = today()
    return sum(1 for r in rows for c in (r.get("completions") or [])
               if c[:10] == t)


def last_completed_today(kind: str) -> dict | None:
    """The record completed most recently today — the target of Undo."""
    t = today()
    best = None
    for r in load_store(kind):
        for c in r.get("completions") or []:
            if c[:10] == t and (best is None or c > best[0]):
                best = (c, r)
    return best[1] if best else None


def mark_complete(kind: str, number: int) -> dict | None:
    item = find_item(kind, number)
    if item is None:
        return None
    rows = load_store(kind)
    rec = _record(rows, number, item["title"])
    rec.setdefault("completions", []).append(now_utc())
    save_store(kind, rows)
    return rec


def revert_complete(kind: str, number: int) -> bool:
    """Undo today's latest completion of one item. An undo, not a status: it
    only ever removes a completion stamped today, so it can't rewrite history."""
    rows = load_store(kind)
    rec = next((r for r in rows if r.get("number") == number), None)
    comps = (rec or {}).get("completions") or []
    todays = [c for c in comps if c[:10] == today()]
    if not todays:
        return False
    comps.remove(max(todays))
    save_store(kind, rows)
    return True


def skip_item(kind: str, number: int) -> dict | None:
    """Send an item to the back of the rotation without completing it."""
    item = find_item(kind, number)
    if item is None:
        return None
    rows = load_store(kind)
    rec = _record(rows, number, item["title"])
    rec["skipped_at"] = now_utc()
    save_store(kind, rows)
    return rec


def save_attempt(number: int, language: str, code: str) -> bool:
    """Store the operator's code for one algorithm item in one language (latest
    wins). Returns False for an unknown item or language."""
    if language not in DRILL_LANGUAGES:
        return False
    item = find_item("algorithm", number)
    if item is None:
        return False
    rows = load_store("algorithm")
    rec = _record(rows, number, item["title"])
    rec.setdefault("attempts", {})[language] = {"code": code, "saved_at": now_utc()}
    save_store("algorithm", rows)
    return True


# ─── In-depth explanation (Claude, cached per pattern) ────────────────────────

EXPLAIN_MAX_TOKENS = 3000

_EXPLAIN_SYSTEM = """\
You are teaching an experienced senior software engineer who is preparing for \
timed online coding assessments and live coding rounds. They know Java and \
Python well but are rusty on algorithm patterns. You will be given one entry \
from their algorithm reference: a pattern, a canonical problem, recognition \
cues, reference solutions in Java and Python, complexity and gotchas.

Write an in-depth explanation of how the algorithm works and how to solve the \
problem. Use these markdown sections, in this order:

## The idea
The core insight in plain language. What makes the naive approach slow, and \
what this pattern changes.

## Why it works
The invariant or argument that makes it correct. Concrete, not hand-wavy.

## Walkthrough
Trace the reference approach by hand on one small concrete input, step by \
step, showing the state (pointers, table cells, map contents, queue) at each \
step.

## Solving it in an interview
How to get from reading the problem to working code: which cues identify the \
pattern, the brute force to state first, then the steps to the optimal \
version. Numbered steps.

## Variations
Two or three common disguises of this pattern in assessment questions, one or \
two sentences each, and what changes in the solution.

## Mistakes to avoid
The bugs people actually write, beyond what the reference already lists.

Rules:
- Stay consistent with the reference solution. Never contradict its approach, \
complexity or naming.
- Do NOT write out a complete solution; the reference solutions are shown \
separately and the reader will attempt their own first. Short snippets of one \
or two lines are fine where they clarify a step.
- No tables, no emojis, no preamble or sign-off. Start with "## The idea".
- About 700 to 1000 words."""


def _call_claude(system: str, user: str, max_tokens: int) -> str:
    import anthropic
    from config import ANTHROPIC_API_KEY
    msg = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model=CL_MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    return msg.content[0].text


def explanation_is_stale(rec: dict, item: dict) -> bool:
    """True when the cached explanation was written from a different version of
    the reference entry than the one in profile/ now."""
    exp = rec.get("explanation") or {}
    return bool(exp) and exp.get("source_hash") != item["hash"]


def explain(number: int) -> dict:
    """Generate (or regenerate) the in-depth explanation for one algorithm item
    and cache it on the item's record. One Sonnet call."""
    item = find_item("algorithm", number)
    if item is None:
        raise ValueError(f"No algorithm item {number} in "
                         f"{ALGORITHM_REFERENCE_PATH.name}")
    user = (f"Reference entry {item['number']}: {item['title']}\n\n"
            f"{item['body']}")
    text = _call_claude(_EXPLAIN_SYSTEM, user, EXPLAIN_MAX_TOKENS).strip()
    rows = load_store("algorithm")
    rec = _record(rows, number, item["title"])
    rec["explanation"] = {"text": text, "at": now_utc(), "model": CL_MODEL,
                          "source_hash": item["hash"]}
    save_store("algorithm", rows)
    return rec


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="Daily algorithm / interview drills.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Show both banks and today's picks.")
    e = sub.add_parser("explain", help="Generate an algorithm explanation.")
    g = e.add_mutually_exclusive_group(required=True)
    g.add_argument("--number", type=int)
    g.add_argument("--all", action="store_true",
                   help="Every pattern without a current explanation.")
    args = p.parse_args()

    try:
        if args.cmd == "list":
            for key, k in STUDY_KINDS.items():
                cur = current_item(key)
                print(f"{k.label}: {len(reference_items(key))} items in "
                      f"{k.reference.name}; {completed_today(key)}/{k.goal} today; "
                      f"current: {cur['number']}. {cur['title']}" if cur else
                      f"{k.label}: no items ({k.reference} missing?)")
        elif args.cmd == "explain":
            if args.number is not None:
                numbers = [args.number]
            else:
                by_num = {r.get("number"): r for r in load_store("algorithm")}
                numbers = [it["number"] for it in reference_items("algorithm")
                           if not (by_num.get(it["number"]) or {}).get("explanation")
                           or explanation_is_stale(by_num[it["number"]], it)]
            for n in numbers:
                rec = explain(n)
                print(f"Explained {n}. {rec['title']}")
            print(f"EXPLAINED: {len(numbers)}")
    except Exception as ex:  # noqa: BLE001
        print(f"ERROR: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
