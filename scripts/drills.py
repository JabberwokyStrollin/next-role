"""
drills.py — generate interview-prep coding drills and review manual attempts.

Two Claude-backed actions behind the /today "Code drills" section (Java only):

    generate  Produce the NEXT drill — a short, informal, deliberately
              underspecified interview-style prompt plus a minimal interface
              given as method names + parameters but WITHOUT return types
              (deciding the return shape is part of the exercise). The prompt
              never tells the candidate what to watch for. Appended to
              data/drills.json with status "active".

    review    Read the operator's Drill<N>.java + Drill<N>Test.java from the
              sibling Maven project and return interview-style feedback
              (correctness, idiomatic Java, complexity, test quality, and
              signal issues an interviewer would flag). Stored on the record.

The code + JUnit tests live in the sibling Maven project
(config.MANUAL_CODE_DRILLS_DIR, default ../manual-code-drills); this script
only produces prompts and reviews attempts — it never compiles or runs Java.

Uses CL_MODEL (Sonnet), matching answer_questions.py / generate_cl.js.

Usage:
    python scripts/drills.py generate
    python scripts/drills.py review --number 3

Machine-readable last line:
    GENERATED: <number>     on generate success
    REVIEWED: <number>      on review success
    ERROR: <message>        on failure
"""

import argparse
import sys

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CL_MODEL,
    PROCESS_LOG_PATH,
    current_drill,
    drill_impl_path,
    drill_test_path,
    load_drills,
    load_json,
    next_drill_number,
    now_utc,
    save_drills,
    save_json,
    today,
)

MAX_TOKENS = 2000

_GENERATE_SYSTEM = """You are an interviewer creating a whiteboard/live-coding drill for a \
senior software engineer practicing Java.

Produce ONE small, self-contained exercise in the spirit of a real technical \
interview — the kind an interviewer describes out loud, not a LeetCode puzzle. \
Favour exercises that hinge on choosing the right collections and data \
structures (maps, sets, ordered structures, counters, simple caches, grouping, \
ranking) and clean method decomposition. It should be implementable by hand in \
20-40 minutes as a single class.

Hard rules:
- The prompt is SHORT and informal (one or two paragraphs) and DELIBERATELY \
UNDERSPECIFIED: leave ambiguities (case sensitivity, tie-breaking, null/empty \
handling, what to return in edge cases) UNSTATED. Resolving them is the \
candidate's job.
- Give a minimal interface as method names WITH their parameters, but WITHOUT \
return types and WITHOUT full signatures — choosing the return shape is part of \
the exercise. Example: "add(String text)", "topN(int n)", "get(String key)".
- Do NOT include hints, tips, edge-case checklists, "watch out for…", \
complexity targets, or any mention of what makes a good solution. No design \
notes. The prompt must not coach.
- Do NOT provide any implementation, pseudo-code, or tests.
- Make it distinct from the drills already used (listed by the user).

Return ONLY a JSON object, no prose, no code fences:
{"title": "<3-6 word name>", "prompt": "<the spoken-style prompt>", \
"interface": ["method(params)", "..."]}"""

_REVIEW_SYSTEM = """You are a staff-level engineer reviewing a candidate's drill \
attempt right after a timed interview-style exercise. Be direct and specific, \
the way a strong interviewer debriefs.

Cover, in this order, only what's relevant:
1. Correctness — does it do what the prompt asks? Call out concrete bugs with \
the input that breaks them.
2. The ambiguities the prompt left open (case sensitivity, tie-breaking, \
null/empty, edge cases) — did the candidate resolve them, and did they make \
those decisions explicit?
3. Idiomatic Java & data-structure choice — right collection for the job, \
standard-library methods that would simplify the code, naming.
4. Complexity — time/space, and any needless rework.
5. The tests — do they actually pin down the behaviour, including edge cases?
6. Interview signal — what would make an interviewer raise an eyebrow even if \
the code works.

End with a one-line verdict: would this pass a senior bar? Keep it tight — \
Markdown, no preamble."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _call_claude(system: str, user_message: str) -> str:
    msg = _client().messages.create(
        model=CL_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return msg.content[0].text


def _extract_json(text: str) -> dict:
    import json
    cleaned = text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    elif "{" in cleaned:
        cleaned = cleaned[cleaned.index("{"): cleaned.rindex("}") + 1]
    return json.loads(cleaned)


def _append_log(event: dict) -> None:
    log = load_json(PROCESS_LOG_PATH) or []
    log.append({"timestamp": now_utc(), **event})
    save_json(PROCESS_LOG_PATH, log)


def generate_drill(language: str = "java") -> dict:
    """Generate the next drill, append it to the store, and return the record."""
    number   = next_drill_number()
    existing = [f"Drill {d.get('number')}: {d.get('title','')}" for d in load_drills()]
    used = ("\n".join(existing) if existing
            else "(none yet — Drill 1 was a multi-level flag store, "
                 "Drill 2 a word-frequency counter; avoid those shapes)")
    user = (f"This is Drill {number}. Drills already used:\n{used}\n\n"
            f"Create a new, distinct Java drill.")

    raw  = _call_claude(_GENERATE_SYSTEM, user)
    data = _extract_json(raw)

    record = {
        "number":       number,
        "language":     language,
        "title":        (data.get("title") or f"Drill {number}").strip(),
        "prompt":       (data.get("prompt") or "").strip(),
        "interface":    [s.strip() for s in data.get("interface", []) if s.strip()],
        "status":       "active",
        "created_at":   now_utc(),
        "completed_at": None,
        "feedback":     [],
    }
    drills = load_drills()
    drills.append(record)
    save_drills(drills)
    _append_log({"event_type": "drill_generated", "entity_type": "drill",
                 "entity_id": str(number), "entity_name": record["title"],
                 "detail": f"Generated Drill {number} ({language})."})
    return record


def review_drill(number: int) -> str:
    """Review the operator's attempt for drill ``number``. Reads the impl + test
    from the Maven project, calls Claude, stores + returns the feedback."""
    drills = load_drills()
    record = next((d for d in drills if int(d.get("number", 0)) == int(number)), None)
    if not record:
        raise ValueError(f"Drill {number} not found in the store.")

    impl_p, test_p = drill_impl_path(number), drill_test_path(number)
    if not impl_p.exists():
        raise FileNotFoundError(
            f"No attempt found at {impl_p}. Write Drill{number}.java first "
            f"(in the manual-code-drills project).")
    impl_code = impl_p.read_text(encoding="utf-8", errors="replace")
    test_code = (test_p.read_text(encoding="utf-8", errors="replace")
                 if test_p.exists() else "(no test file written yet)")

    iface = "\n".join(f"- {s}" for s in record.get("interface", []))
    user = (
        f"## Prompt\n{record.get('prompt','')}\n\n"
        f"## Interface given (return types intentionally omitted)\n{iface}\n\n"
        f"## Candidate's Drill{number}.java\n```java\n{impl_code}\n```\n\n"
        f"## Candidate's Drill{number}Test.java\n```java\n{test_code}\n```\n")

    feedback = _call_claude(_REVIEW_SYSTEM, user).strip()

    record.setdefault("feedback", []).append({"at": now_utc(), "text": feedback})
    save_drills(drills)
    _append_log({"event_type": "drill_reviewed", "entity_type": "drill",
                 "entity_id": str(number), "entity_name": record.get("title", ""),
                 "detail": f"Reviewed Drill {number}."})
    return feedback


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate / review code drills.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="Generate the next drill prompt.")
    g.add_argument("--language", default="java")

    r = sub.add_parser("review", help="Review a manual attempt.")
    r.add_argument("--number", type=int, required=True)

    args = parser.parse_args()
    try:
        if args.cmd == "generate":
            rec = generate_drill(args.language)
            print(f"Generated Drill {rec['number']}: {rec['title']}")
            print(f"GENERATED: {rec['number']}")
        elif args.cmd == "review":
            fb = review_drill(args.number)
            print(fb)
            print(f"REVIEWED: {args.number}")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
