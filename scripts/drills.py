"""
drills.py — generate interview-prep coding drills, review attempts, and produce
reference solutions.

Three Claude-backed actions behind the /today "Code drills" section (Java only):

    generate  Produce the NEXT drill as a MULTI-PART SERIES: one small theme
              split into config.DRILL_MIN_PARTS..DRILL_MAX_PARTS parts, each
              sized for a single ~config.DRILL_PART_TARGET_MINUTES sitting.
              Part 1 is the plain working version; every later part adds
              exactly ONE new gotcha to the same class. Each part's prompt is
              short, informal and deliberately underspecified, with a minimal
              interface given as method names + parameters but WITHOUT return
              types (deciding the return shape is part of the exercise) and no
              hints about what to watch for. Appended to data/drills.json with
              status "active"; only the current part is ever surfaced.

    review    Read the operator's Drill<N>.java + Drill<N>Test.java from the
              sibling Maven project and return interview-style feedback on the
              CURRENT PART (correctness, whether that part's gotcha was really
              handled, regressions in earlier parts, idiomatic Java, complexity,
              test quality, and signal issues an interviewer would flag).
              Stored on the part.

    solve     Produce the reference "correct answer" for the class as of the
              current part — a senior/staff-level implementation + JUnit test
              with a design-decisions block — from the prompts + interfaces
              alone (does not read the attempt). Stored on the part as
              `solution`.

All parts of a drill share ONE Drill<N>.java / Drill<N>Test.java, which the
operator keeps extending; review and solve are therefore cumulative — they
judge / write the class as of the current part, not that part in isolation.

The code + JUnit tests live in the sibling Maven project
(config.MANUAL_CODE_DRILLS_DIR, default ../manual-code-drills); this script
only produces prompts, reviews, and reference solutions — it never compiles or
runs Java.

Uses CL_MODEL (Sonnet), matching answer_questions.py / generate_cl.js.

Usage:
    python scripts/drills.py generate
    python scripts/drills.py review --number 3 [--part 2]
    python scripts/drills.py solve  --number 3 [--part 2]

``--part`` defaults to the drill's current (first not-yet-complete) part.

Machine-readable last line:
    GENERATED: <number>          on generate success
    REVIEWED: <number>.<part>    on review success
    SOLVED: <number>.<part>      on solve success
    ERROR: <message>             on failure
"""

import argparse
import re
import sys

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CL_MODEL,
    CORRECT_CODE_BEGIN,
    CORRECT_CODE_END,
    DRILL_IDIOMS,
    DRILL_MAX_PARTS,
    DRILL_MIN_PARTS,
    DRILL_PART_TARGET_MINUTES,
    DRILL_SKILL_MAX,
    DRILL_SKILLS,
    PROCESS_LOG_PATH,
    drill_proficiency_brief,
    current_drill,
    drill_impl_path,
    drill_part_progress,
    drill_parts,
    drill_test_path,
    find_drill_part,
    load_drills,
    load_json,
    mark_drill_part_complete,
    revert_drill_part,
    next_drill_number,
    now_utc,
    save_drills,
    save_json,
    strip_correct_code,
    today,
)

MAX_TOKENS = 2000
# The reference solution is cumulative — by the last part it must implement
# every gotcha in the series plus its tests, so it needs more room than a
# prompt or a review.
SOLVE_MAX_TOKENS = 4000

# A thorough review of a messy attempt runs long, and the assessment JSON comes
# LAST — at MAX_TOKENS a real review truncated mid-object, silently costing the
# grade. Give the review its own ceiling with headroom above the longest prose
# observed (~1900 tokens) so the trailing JSON always fits.
REVIEW_MAX_TOKENS = 3000

# The review returns prose THEN a JSON assessment, split on this sentinel. One
# call, not two: the candidate's code is already in that prompt, so grading it
# costs only the handful of output tokens the JSON takes.
_ASSESSMENT_SENTINEL = "---ASSESSMENT---"

# Skill/idiom vocabularies are interpolated from config so the prompt, the
# stored assessment, and the derived profile can't drift apart.
_SKILL_KEYS = ", ".join(f'"{k}": 0-{DRILL_SKILL_MAX}' for k in DRILL_SKILLS)
_SKILL_DESCRIPTIONS = "; ".join(f"{k} = {v}" for k, v in DRILL_SKILLS.items())
_IDIOM_DESCRIPTIONS = "; ".join(f"{k} = {v}" for k, v in DRILL_IDIOMS.items())

_GENERATE_SYSTEM = f"""You are an interviewer creating a SERIES of short live-coding \
drills for a senior software engineer practicing Java.

The candidate works ONE part per sitting, on separate days, with about \
{DRILL_PART_TARGET_MINUTES} minutes per sitting. So take one SMALL theme and \
split it into {DRILL_MIN_PARTS}-{DRILL_MAX_PARTS} parts that build up the SAME \
class in a single file the candidate keeps extending. Favour themes in the \
spirit of a real technical interview — the kind an interviewer describes out \
loud, not a LeetCode puzzle — that hinge on choosing the right collections and \
data structures (maps, sets, ordered structures, counters, simple caches, \
grouping, ranking) and clean method decomposition.

The candidate sees exactly two things each sitting: the drill-level OVERVIEW \
(the same text every part) and the CURRENT part. Between them they must have \
enough to start typing code immediately, without guessing at the domain.

The overview ("premise") is the brief for the whole drill. Write 3-5 sentences \
covering:
- What is being built and what problem it solves, in plain domain terms.
- Who calls it and why — a concrete usage story ("a dashboard polls it every \
few seconds", "the front desk looks up availability").
- The shape of the thing: it is ONE class the candidate keeps extending across \
several sittings, holding its state in memory.
The overview must NOT restate the current part's task — it is the standing \
context that makes the part make sense — and must NOT mention, hint at, or \
foreshadow what any later part will ask for — in particular, keep ALL \
performance, efficiency, ordering, concurrency and immutability language out of \
the overview ("without rescanning", "quickly", "thread-safe"); those are \
later-part twists and naming one in the standing brief hands it over on day \
one. Describe only what the thing is for. Never invent a name for the class: \
it is always `Drill<N>`, fixed by the candidate's project layout, so describe \
it by what it does ("the registry", "the tracker") and never as "a class called \
BookingRegistry".

How to size the parts — this is the part people get wrong, so read it twice:

**ONE part = ONE method plus its tests.** That is the whole unit. A part that \
asks for two or three methods is a multi-hour sitting, not a \
{DRILL_PART_TARGET_MINUTES}-minute one. AT MOST ONE new method per part is a \
hard cap, including part 1. A "small helper to register things first" is a \
SECOND METHOD and is forbidden — fold whatever it set up into the one method, \
or push it to its own part.

**Size budget: a part's complete solution — implementation AND tests together — \
should be roughly 60-90 lines of Java.** This is the check that matters, \
because method count alone doesn't catch it. A real generated part came back as \
246 lines and took hours: it had two methods, four domain concepts, a record, a \
handful of custom exceptions and a dozen tests. Before you emit a part, ask what \
its solution would actually look like written out. If it's over ~90 lines, cut \
it down or split it.

**Keep the domain surface tiny.** Part 1 may involve AT MOST TWO domain \
concepts (say "room" and "booking"). Every extra attribute — a capacity, an \
owner, a category, a timestamp — is another field to model, another branch, \
another test, and it is the fastest way to blow the budget. Attributes that the \
part's one method doesn't actually use MUST NOT appear at all.

- Part 1 is: decide how the data will be stored, write the single core \
write/ingest method, test it. Nothing else. Because there is no query method \
yet, part 1's method must be observable on its own — its return value or a \
thrown exception has to be enough to write real tests against.
- Do not ask for custom exception types, a value/record type, or an interface \
in part 1. If the candidate wants one they can introduce it; requiring it turns \
a one-method sitting into a design exercise.
- Every later part adds AT MOST ONE new method, ONE new constraint on \
behaviour that already exists, or both — never two of either. Good constraints: \
an ordering or tie-breaking guarantee, bounding/eviction, defensive copying or \
immutability, thread safety, a performance requirement that forbids rescanning \
on read, a lifecycle/state rule, safe iteration during modification.
- A part whose tasks name more than one method to write is too big. Split it \
into two parts, or push the second method to a later part.
- Each part must feel SMALLER than seems right to you. A part that only changes \
the BEHAVIOUR of a method that already exists is ideal.

Each part also carries "tasks": AT MOST 3 short imperative steps (a hard cap) \
telling the candidate what to actually DO in this sitting, in order, the last \
of which is always writing the tests. They define the SCOPE of the sitting, \
never the solution. The canonical shape for part 1 is exactly:
  "Decide how the bookings will be stored."
  "Write the book method."
  "Write tests for it."
Three steps, one method. Later parts look the same with the design step swapped \
for the new constraint. If you find yourself writing "write the X, Y and Z \
methods", the part is too big — that is the signal to split it.
Naming the work is fine; naming the answer is not. "Decide how the data will be \
stored" is right; "use a map of maps" is forbidden.

Hard rules:
- Each part's prompt is SHORT and informal — one short paragraph, as spoken \
aloud, framing what this sitting is about — and DELIBERATELY UNDERSPECIFIED: \
leave ambiguities (case sensitivity, tie-breaking, null/empty handling, what to \
return in edge cases) UNSTATED. Resolving them is the candidate's job. \
Underspecified means the SEMANTICS are open, never that the task is vague: the \
candidate must always know what to build this sitting.
- Every operation the prompt or tasks describe MUST be reachable through a \
method listed for this part or an earlier one. Never describe an action \
("open a session", "flush the buffer") that has no corresponding method — that \
reads as a missing requirement, not as an ambiguity to resolve.
- **Define the data model precisely, even while leaving behaviour open.** Every \
domain noun must be pinned down well enough to declare a field for it. Anything \
time-, range- or identity-shaped is where this goes wrong: if you say "slot", \
state whether it is a single instant or an interval, and give the exact form \
("a slot is an integer hour 0-23", "a slot is the string 'Monday 9am', treated \
as an opaque label"). A real drill called "Monday 9am" a *slot* — an instant \
described as an interval — and the candidate could not tell whether bookings \
needed overlap logic or plain equality. That is not productive ambiguity, it is \
an unanswerable question, and it costs an hour before any code is written. \
Leave the SEMANTICS open (case sensitivity, tie-breaking, what happens on a \
clash); never leave the SHAPE OF THE DATA open.
- Give each part's interface as method names WITH their parameters, but WITHOUT \
return types and WITHOUT full signatures — choosing the return shape is part of \
the exercise. Example: "add(String text)", "topN(int n)", "get(String key)".
- List in a part's interface ONLY the methods that part introduces — never \
repeat methods from an earlier part.
- Do NOT include hints, tips, edge-case checklists, "watch out for…", \
complexity targets, or any mention of what makes a good solution. No design \
notes. The prompt must not coach. State each part's new requirement plainly and \
let the candidate discover what it costs — never flag it as a warning or name \
the trap.
- Do NOT provide any implementation, pseudo-code, or tests.
- The theme must be genuinely distinct from the drills already used (listed by \
the user): a different DOMAIN and a different core data-structure problem. \
Rewording an earlier title, or reusing its subject with a new noun, does NOT \
count as distinct.

TARGETING. If the user's message carries a "Targeting" section, it summarises \
the candidate's graded history — the skills they score worst on and the Java \
idioms they rarely or never reach for. Use it:
- Pick a theme that naturally exercises the WEAK SKILLS named there.
- Include EXACTLY ONE re-implementation part that forces a rare idiom. This \
part adds NO new method and NO new behaviour: it asks the candidate to rewrite \
what they already built using the required idiom, keeping every existing test \
passing unchanged as proof the rewrite is faithful. Its "interface" is an empty \
list. Word the constraint as a REQUIREMENT, never a suggestion — "Rewrite the \
lookup logic entirely with the Streams API", "Redo this the way you'd have had \
to in Java 8: no var, no records, no List.of". Its tasks look like: "Rewrite X \
using <idiom>." / "Keep every existing test passing unchanged." Place it after \
the part whose code it rewrites.
- NEVER more than one idiom-constrained part per drill, and never require an \
idiom the Targeting section doesn't name.
- With no "Targeting" section, generate normally — no re-implementation part.

Return ONLY a JSON object, no prose, no code fences:
{{"title": "<3-6 word name for the whole series>", "premise": "<3-5 sentence \
overview of the whole drill: what is being built, who calls it and why, and \
that it is one class extended across sittings>", "parts": [{{"title": "<3-6 \
word name for this part>", "prompt": "<the spoken-style prompt for THIS part \
only>", "tasks": ["<imperative step>", "..."], "interface": \
["method(params)", "..."]}}]}}"""

_REVIEW_SYSTEM = f"""You are a staff-level engineer reviewing a candidate's drill \
attempt right after a timed interview-style exercise. Be direct and specific, \
the way a strong interviewer debriefs.

You are reviewing ONE PART of a multi-part drill. The file also contains the \
work from earlier parts, since every part extends the same class — so judge the \
whole file for correctness, but spend your feedback on what THIS part asked for.

Cover, in this order, only what's relevant:
1. Correctness — does it do what this part asks? Call out concrete bugs with \
the input that breaks them.
2. This part's new requirement — is it genuinely handled, or only apparently? \
Name the case that would expose a half-measure.
3. Regressions — did adding this part break or paper over behaviour an earlier \
part established? Skip this for part 1.
4. The ambiguities the prompt left open (case sensitivity, tie-breaking, \
null/empty, edge cases) — did the candidate resolve them, and did they make \
those decisions explicit?
5. Idiomatic Java & data-structure choice — right collection for the job, \
standard-library methods that would simplify the code, naming. Flag it if the \
earlier structure should have been reshaped for this part rather than bolted on.
6. Complexity — time/space, and any needless rework.
7. The tests — do they actually pin down the behaviour, including edge cases?
8. Interview signal — what would make an interviewer raise an eyebrow even if \
the code works.

End with a one-line verdict: would this part clear a senior bar? Keep it tight \
— Markdown, no preamble. Do not speculate about, or hint at, what later parts \
might ask for.

Then, after the prose, emit a machine-readable assessment. Put this sentinel on \
a line of its own:

{_ASSESSMENT_SENTINEL}

and follow it with ONLY a JSON object — no prose, no code fence:
{{"skills": {{{_SKILL_KEYS}}}, "idioms_used": ["<idiom>", "..."]}}

Scoring rules:
- Score every skill 0-{DRILL_SKILL_MAX} against a SENIOR bar, where \
{DRILL_SKILL_MAX} means "an interviewer would call this exemplary" and 3 means \
"acceptable, with reservations". Judge only what this part asked for. Be \
honest — inflated scores make the whole profile useless.
- Skills are: {_SKILL_DESCRIPTIONS}
- "idioms_used" lists ONLY the idioms genuinely present in the candidate's \
code, drawn from this exact vocabulary (use the keys verbatim, omit any that \
don't apply, never invent one): {_IDIOM_DESCRIPTIONS}
- Judge idioms by what the code actually does, not by what would have been \
nice. A single `.forEach` is not "streams"; an explicit indexed loop is \
"legacy_java8"."""

_SOLVE_SYSTEM = """You are a staff-level engineer writing the reference solution to \
an interview-style Java drill — the answer that would clear a strong senior/staff \
bar in a live coding round.

The drill is a multi-part series in which every part extends the same class, so \
your solution is CUMULATIVE: implement the current part AND every earlier part \
in one class, exactly as the candidate's file should look at this point. Do not \
implement, mention, or leave hooks for parts that haven't been given to you.

Because the prompt is deliberately underspecified, FIRST state the design \
decisions you're committing to (case sensitivity, tie-breaking, null/empty \
handling, edge-case returns, and the return types the interface left open) in a \
short `// Design Decisions:` comment block at the top of the class.

Then write the full implementation, holding a senior bar:
- Pick the right data structures; pre-aggregate on write when the prompt implies \
reads must stay fast (don't rescan on read).
- Return defensive copies / unmodifiable views — never expose internal mutable \
collections.
- Use modern idiomatic Java (records for value types, standard-library methods \
over hand-rolled loops) and clear naming.
- Guard/validate inputs per your stated decisions; keep methods small.

Also provide a JUnit 5 test class that pins down the behaviour INCLUDING the \
edge cases your design decisions call out.

Output format (Markdown), in this exact order and nothing else:
1. A `### Design notes` section: 3-6 bullets on the key senior-level choices and \
any trade-off worth naming aloud in an interview. When this isn't part 1, one \
bullet must say what the current part's requirement forced you to change in the \
earlier design, and why bolting it on would not have worked.
2. A ```java fenced block with the complete `Drill<N>.java`.
3. A ```java fenced block with the complete `Drill<N>Test.java`."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _call_claude(system: str, user_message: str, max_tokens: int = MAX_TOKENS) -> str:
    msg = _client().messages.create(
        model=CL_MODEL,
        max_tokens=max_tokens,
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
    """Generate a multi-part drill series and return the record.

    Regenerating rerolls the current drill IN PLACE, at the same number, only
    while none of its parts are complete — once a sitting has been banked, a
    reroll would throw that work away, so generation moves on to the next
    number instead (as it also does when the drill is finished or the store is
    empty). Every generated series — including rerolls — is logged to the
    process log, so there's a durable record of which drills were created even
    though the store keeps only the latest version of an active drill."""
    drills = load_drills()
    cur    = current_drill(drills)
    banked = drill_part_progress(cur)[0] if cur else 0
    # Work exists before it's marked complete. Code written against this drill's
    # prompts counts as banked even with zero parts ticked off, or a reroll
    # silently orphans the file the operator has been writing all session.
    if cur and not banked and _has_written_code(int(cur.get("number", 0))):
        banked = 1
    if cur and cur.get("status") == "active" and banked == 0:
        number, regen = int(cur["number"]), True
    else:
        number, regen = next_drill_number(), False

    existing = [f"Drill {d.get('number')}: {d.get('title','')}" for d in drills]
    used = ("\n".join(existing) if existing
            else "(none yet — Drill 1 was a multi-level flag store, "
                 "Drill 2 a word-frequency counter; avoid those shapes)")
    user = (f"This is Drill {number}. Drills already used:\n{used}\n\n"
            f"Create a new, distinct Java drill series.")
    # Targeting is omitted entirely until something has been graded, so the
    # first drills aren't aimed at noise.
    brief = drill_proficiency_brief(drills)
    if brief:
        user += f"\n\n## Targeting\n{brief}"

    raw  = _call_claude(_GENERATE_SYSTEM, user)
    data = _extract_json(raw)

    parts = []
    for i, p in enumerate(data.get("parts") or [], start=1):
        parts.append({
            "part":         i,
            "title":        (p.get("title") or f"Part {i}").strip(),
            "prompt":       (p.get("prompt") or "").strip(),
            "tasks":        [s.strip() for s in (p.get("tasks") or []) if s.strip()],
            "interface":    [s.strip() for s in (p.get("interface") or []) if s.strip()],
            "status":       "active",
            "completed_at": None,
            "feedback":     [],
        })
    if not parts:
        raise ValueError("Claude returned no parts for the drill.")

    record = {
        "number":       number,
        "language":     language,
        "title":        (data.get("title") or f"Drill {number}").strip(),
        "premise":      (data.get("premise") or "").strip(),
        "parts":        parts,
        "status":       "active",
        "created_at":   now_utc(),
        "completed_at": None,
    }
    if regen:
        drills = [record if int(d.get("number", 0)) == number else d for d in drills]
    else:
        drills.append(record)
    save_drills(drills)

    verb = "Regenerated" if regen else "Generated"
    _append_log({"event_type": "drill_generated", "entity_type": "drill",
                 "entity_id": str(number), "entity_name": record["title"],
                 "detail": f"{verb} Drill {number} ({language}): {record['title']} "
                           f"— {len(parts)} parts",
                 "premise": record["premise"], "parts": parts,
                 "regenerated": regen})
    return record


def _iface_lines(part: dict) -> str:
    return "\n".join(f"- {s}" for s in part.get("interface", [])) or "- (none new)"


def _task_lines(part: dict) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(part.get("tasks") or [], 1))


def _has_written_code(number: int) -> bool:
    """True if the operator has written anything of their own into
    `Drill<N>.java`. An appended reference solution doesn't count — it's ours,
    not theirs — so it's stripped before the check."""
    path = drill_impl_path(number)
    if not path.exists():
        return False
    body = strip_correct_code(path.read_text(encoding="utf-8", errors="replace"))
    return bool(body.strip())


_JAVA_BLOCK_RE = re.compile(r"```java\s*\n(.*?)```", re.S)


def _split_solution_blocks(markdown: str) -> tuple[str, str, str]:
    """``(design notes, impl code, test code)`` from a solve response, which is
    a notes section followed by two ```java blocks. Missing blocks come back
    empty rather than raising — a partial solution is still worth appending."""
    blocks = _JAVA_BLOCK_RE.findall(markdown)
    notes  = markdown.split("```", 1)[0].strip()
    impl   = blocks[0].strip() if blocks else ""
    test   = blocks[1].strip() if len(blocks) > 1 else ""
    return notes, impl, test


def write_correct_code(number: int, part_no: int, solution_md: str) -> list:
    """Append the reference solution to the operator's own `Drill<N>.java` and
    `Drill<N>Test.java`, as a clearly delimited block comment at the end of each
    file, and return the paths written.

    Idempotent: any previous block is stripped first, so re-running solve
    replaces it rather than stacking copies. Only files that already exist are
    touched — this never creates a file in the Maven project. `*/` inside the
    reference is defanged to `* /`, since Java block comments don't nest and one
    would otherwise close the comment early and break the build."""
    notes, impl, test = _split_solution_blocks(solution_md)
    written = []
    for path, code, with_notes in ((drill_impl_path(number), impl, True),
                                   (drill_test_path(number), test, False)):
        if not path.exists() or not code:
            continue
        body  = f"{notes}\n\n{code}" if (with_notes and notes) else code
        block = (f"{CORRECT_CODE_BEGIN} — part {part_no} (reference) =====\n\n"
                 f"{body.replace('*/', '* /')}\n\n{CORRECT_CODE_END}")
        current = strip_correct_code(
            path.read_text(encoding="utf-8", errors="replace"))
        path.write_text(f"{current}\n\n{block}\n", encoding="utf-8")
        written.append(path)
    return written


def _split_assessment(text: str) -> tuple[str, dict | None]:
    """Split a review into ``(prose, assessment | None)`` on the sentinel.

    Anything unparseable degrades to ``(whole text, None)``: a malformed grade
    must never cost the operator their written feedback, and an ungraded part
    simply doesn't contribute to the profile. Unknown skill/idiom keys are
    dropped and scores clamped, so a hallucinated dimension can't enter the
    vocabulary through the back door."""
    if _ASSESSMENT_SENTINEL not in text:
        return text.strip(), None
    prose, _, raw = text.partition(_ASSESSMENT_SENTINEL)
    try:
        data = _extract_json(raw)
    except Exception:  # noqa: BLE001
        return text.strip(), None
    if not isinstance(data, dict):
        return prose.strip(), None

    skills = {}
    for key, val in (data.get("skills") or {}).items():
        if key not in DRILL_SKILLS:
            continue
        try:
            skills[key] = max(0, min(DRILL_SKILL_MAX, int(round(float(val)))))
        except (TypeError, ValueError):
            continue
    idioms = [s for s in (data.get("idioms_used") or [])
              if isinstance(s, str) and s in DRILL_IDIOMS]
    if not skills:
        return prose.strip(), None
    return prose.strip(), {"at": now_utc(), "skills": skills,
                           "idioms_used": sorted(set(idioms))}


def _load_for_part(number: int, part: int | None) -> tuple[list, dict, dict]:
    """Resolve ``(all drills, record, part)`` for drill ``number``, defaulting
    to its current part. Materializes a legacy record's adapted part into
    ``parts`` so the caller's mutation of the returned part persists."""
    drills = load_drills()
    record = next((d for d in drills if int(d.get("number", 0)) == int(number)), None)
    if not record:
        raise ValueError(f"Drill {number} not found in the store.")
    record["parts"] = drill_parts(record)
    target = find_drill_part(record, part)
    if target is None:
        raise ValueError(f"Drill {number} has no part {part}.")
    return drills, record, target


def _series_context(record: dict, part: dict) -> str:
    """Markdown recap of the series up to and including ``part``: the shared
    premise, the earlier parts the class already implements, then the current
    part. Everything AFTER the current part is withheld — a reviewer or a
    reference solution that knew the later twists would design for them in
    advance, which is exactly the head start the parts split exists to
    withhold from the candidate."""
    parts  = drill_parts(record)
    idx    = int(part.get("part", 1))
    number = record.get("number")

    out = [f"## Drill {number}: {record.get('title','')} — part {idx} of {len(parts)}"]
    if record.get("premise"):
        out.append(f"## Overview (the brief for the whole drill, shown every "
                   f"part)\n{record['premise']}")
    for p in (q for q in parts if int(q.get("part", 0)) < idx):
        out.append(f"## Earlier — part {p.get('part')}: {p.get('title','')}\n"
                   f"(already built in this same class)\n\n{p.get('prompt','')}\n\n"
                   f"Methods it introduced:\n{_iface_lines(p)}")
    out.append(f"## THIS part — part {idx}: {part.get('title','')}\n{part.get('prompt','')}")
    if part.get("tasks"):
        out.append(f"## What this sitting asked for\n{_task_lines(part)}")
    out.append(f"## Methods this part introduces "
               f"(return types intentionally omitted)\n{_iface_lines(part)}")
    return "\n\n".join(out)


def review_drill(number: int, part: int | None = None) -> tuple[int, str]:
    """Review the operator's attempt at ONE part of drill ``number`` (defaults
    to the current part). Reads the shared Drill<N>.java + test from the Maven
    project — every part extends the same class, so the review sees the whole
    file and can flag regressions in earlier parts — calls Claude, stores the
    feedback on the part and returns ``(part number, feedback)``."""
    drills, record, target = _load_for_part(number, part)
    pnum = int(target.get("part", 1))

    impl_p, test_p = drill_impl_path(number), drill_test_path(number)
    if not impl_p.exists():
        raise FileNotFoundError(
            f"No attempt found at {impl_p}. Write Drill{number}.java first "
            f"(in the manual-code-drills project).")
    # Strip any previously appended reference solution FIRST — otherwise Claude
    # reads its own correct answer as the candidate's work and grades it as
    # theirs, which would silently inflate the whole proficiency profile.
    impl_code = strip_correct_code(
        impl_p.read_text(encoding="utf-8", errors="replace"))
    test_code = (strip_correct_code(
                     test_p.read_text(encoding="utf-8", errors="replace"))
                 if test_p.exists() else "(no test file written yet)")

    user = (
        f"{_series_context(record, target)}\n\n"
        f"## Candidate's Drill{number}.java\n```java\n{impl_code}\n```\n\n"
        f"## Candidate's Drill{number}Test.java\n```java\n{test_code}\n```\n")

    raw = _call_claude(_REVIEW_SYSTEM, user, max_tokens=REVIEW_MAX_TOKENS)
    feedback, assessment = _split_assessment(raw)

    target.setdefault("feedback", []).append({"at": now_utc(), "text": feedback})
    # Latest grade wins — a part reviewed three times contributes ONE sample, so
    # re-reviewing to check a fix can't inflate the profile.
    if assessment:
        target["assessment"] = assessment
    save_drills(drills)

    graded = (", ".join(f"{k} {v}/{DRILL_SKILL_MAX}"
                        for k, v in assessment["skills"].items())
              if assessment else "ungraded")
    _append_log({"event_type": "drill_reviewed", "entity_type": "drill",
                 "entity_id": str(number), "entity_name": record.get("title", ""),
                 "part": pnum, "assessment": assessment,
                 "detail": f"Reviewed Drill {number} part {pnum} ({graded})."})
    return pnum, feedback


def solve_drill(number: int, part: int | None = None) -> tuple[int, str]:
    """Generate a senior/staff-level reference solution for drill ``number`` as
    of ONE part (defaults to the current part), from the prompts + interfaces
    alone — it does NOT read the operator's attempt. The solution is cumulative
    (this part plus every earlier one) because the parts share a class. Stores
    it on the part as ``solution`` and returns ``(part number, markdown)``."""
    drills, record, target = _load_for_part(number, part)
    pnum = int(target.get("part", 1))

    user = (
        f"{_series_context(record, target)}\n\n"
        f"Write the reference solution as class `Drill{number}` "
        f"(test class `Drill{number}Test`), implementing this part and every "
        f"earlier part shown above.")

    solution = _call_claude(_SOLVE_SYSTEM, user, max_tokens=SOLVE_MAX_TOKENS).strip()

    target["solution"] = {"at": now_utc(), "text": solution}
    save_drills(drills)
    # Also land it in the operator's own files, so the answer sits next to the
    # attempt instead of only in the web UI.
    written = write_correct_code(number, pnum, solution)

    _append_log({"event_type": "drill_solved", "entity_type": "drill",
                 "entity_id": str(number), "entity_name": record.get("title", ""),
                 "part": pnum,
                 "wrote_correct_code": [str(p) for p in written],
                 "detail": f"Generated reference solution for Drill {number} "
                           f"part {pnum}"
                           + (f"; appended Correct Code to "
                              f"{', '.join(p.name for p in written)}."
                              if written else " (no files to append to).")})
    return pnum, solution, written


def restore_correct_code(number: int, record: dict) -> list:
    """Put the Java files back the way they were before the reverted finish:
    strip the appended Correct Code, then re-append the block belonging to the
    latest part that is STILL complete.

    The re-append matters. `write_correct_code` replaces rather than stacks, so
    finishing part 2 overwrote part 1's reference. Merely stripping would leave
    the operator worse off than before the misclick — they'd silently lose an
    answer they had legitimately earned."""
    touched: list = []
    for path in (drill_impl_path(number), drill_test_path(number)):
        if not path.exists():
            continue
        current  = path.read_text(encoding="utf-8", errors="replace")
        stripped = strip_correct_code(current)
        if stripped != current:
            path.write_text(stripped + "\n", encoding="utf-8")
            touched.append(path)

    done = [p for p in drill_parts(record)
            if p.get("status") == "complete" and p.get("solution")]
    if done:
        prev = max(done, key=lambda p: int(p.get("part", 0)))
        for p in write_correct_code(number, int(prev["part"]),
                                    prev["solution"]["text"]):
            if p not in touched:
                touched.append(p)
    return touched


def revert_part(number: int, part: int | None = None) -> dict:
    """Undo a finish: restore the part in the store AND clean up the Java files.
    ``part=None`` reverts the most recently completed part.

    Safe to run: it's logged as `drill_part_reverted`, and it only ever removes
    things this tool added — the operator's own code is never touched."""
    before = next((d for d in load_drills()
                   if int(d.get("number", 0)) == int(number)), None)
    if not before:
        raise ValueError(f"Drill {number} not found in the store.")

    record = revert_drill_part(number, part)
    if record is None:
        raise ValueError(
            f"Nothing to revert on Drill {number}"
            + (f" part {part}" if part is not None else "")
            + " — no completed part found.")

    reverted = next((p for p in drill_parts(record)
                     if p.get("status") != "complete"), None)
    pnum  = int(reverted.get("part", 0)) if reverted else 0
    files = restore_correct_code(number, record)

    _append_log({"event_type": "drill_part_reverted", "entity_type": "drill",
                 "entity_id": str(number), "entity_name": record.get("title", ""),
                 "part": pnum, "files": [str(f) for f in files],
                 "detail": f"Reverted Drill {number} part {pnum} to active "
                           f"(grade, reference answer and last review dropped)."})
    return {"part": pnum, "files": files, "title": record.get("title", "")}


def finish_part(number: int, part: int | None = None) -> dict:
    """Finish one sitting in a single step: grade the attempt, generate the
    reference answer (appending it to the operator's own files), and mark the
    part complete.

    This is the ONE action the operator takes when they're done, because in
    practice grading and completing were always the same intent — and the
    separate "mark complete" click was the one that got forgotten, so parts
    stayed open and the daily goal under-counted.

    Order and failure policy are deliberate:
      * Grading runs FIRST and its failure aborts everything — no attempt on
        disk means there is nothing to finish, so the part must stay open.
      * The reference answer is a bonus. If it fails, the part is still graded
        and still completes; the operator can retry it from the button.

    Returns ``{part, feedback, solution, written, completed}``."""
    pnum, feedback = review_drill(number, part)

    solution, written = None, []
    try:
        _, solution, written = solve_drill(number, pnum)
    except Exception as e:  # noqa: BLE001 — never block completion on the bonus
        print(f"WARNING: reference answer failed ({e}); grading still stands.")

    record = mark_drill_part_complete(number, pnum)
    return {"part": pnum, "feedback": feedback, "solution": solution,
            "written": written, "completed": bool(record)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate / review code drills.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="Generate the next drill series.")
    g.add_argument("--language", default="java")

    r = sub.add_parser("review", help="Review a manual attempt at one part.")
    r.add_argument("--number", type=int, required=True)
    r.add_argument("--part", type=int, default=None,
                   help="Part to review (default: the current part).")

    s = sub.add_parser("solve", help="Generate the reference (correct) solution.")
    s.add_argument("--number", type=int, required=True)
    s.add_argument("--part", type=int, default=None,
                   help="Part to solve up to (default: the current part).")

    v = sub.add_parser("revert", help="Undo a finish/complete for one part.")
    v.add_argument("--number", type=int, required=True)
    v.add_argument("--part", type=int, default=None,
                   help="Part to revert (default: the most recently completed).")

    f = sub.add_parser("finish", help="Grade + reference answer + mark complete.")
    f.add_argument("--number", type=int, required=True)
    f.add_argument("--part", type=int, default=None,
                   help="Part to finish (default: the current part).")

    args = parser.parse_args()
    try:
        if args.cmd == "generate":
            rec = generate_drill(args.language)
            print(f"Generated Drill {rec['number']}: {rec['title']} "
                  f"({len(rec['parts'])} parts)")
            print(f"GENERATED: {rec['number']}")
        elif args.cmd == "review":
            pnum, fb = review_drill(args.number, args.part)
            print(fb)
            print(f"REVIEWED: {args.number}.{pnum}")
        elif args.cmd == "solve":
            pnum, sol, written = solve_drill(args.number, args.part)
            print(sol)
            for p in written:
                print(f"Appended Correct Code to {p}")
            print(f"SOLVED: {args.number}.{pnum}")
        elif args.cmd == "revert":
            r = revert_part(args.number, args.part)
            print(f"Reverted Drill {args.number} part {r['part']} to active.")
            print("Dropped: grade, reference answer, last review.")
            for f_ in r["files"]:
                print(f"Updated {f_}")
            print(f"REVERTED: {args.number}.{r['part']}")
        elif args.cmd == "finish":
            r = finish_part(args.number, args.part)
            print(r["feedback"])
            if r["solution"]:
                print(f"\n{r['solution']}")
            for p in r["written"]:
                print(f"Appended Correct Code to {p}")
            print(f"FINISHED: {args.number}.{r['part']}")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
