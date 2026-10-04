"""
drills.py — generate interview-prep coding drills, review attempts, and produce
reference solutions.

Multi-language: every action takes a ``--language`` (see
``config.DRILL_LANGUAGES``, currently java + python). Each language is its own
NUMBERING TRACK and its own graded vocabulary, so ``--number`` alone no longer
identifies a drill — the pair (language, number) does. This module owns the
per-language PROMPT wording; ``config.py`` owns the facts (paths, filenames,
class names, skill/idiom vocabularies).

Three Claude-backed actions behind the /today "Code drills" section:

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

    review    Read the operator's impl + test file from the sibling drills
              project and return interview-style feedback on the CURRENT PART
              (correctness, whether that part's gotcha was really handled,
              regressions in earlier parts, idiomatic use of the language,
              complexity, test quality, and signal issues an interviewer would
              flag). Stored on the part.

    solve     Produce the reference "correct answer" for the class as of the
              current part — a senior/staff-level implementation + test module
              with a design-decisions block — from the prompts + interfaces
              alone (does not read the attempt). Stored on the part as
              `solution`.

All parts of a drill share ONE impl file and ONE test file, which the operator
keeps extending; review and solve are therefore cumulative — they judge / write
the class as of the current part, not that part in isolation.

The code + tests live in the sibling drills project
(config.MANUAL_CODE_DRILLS_DIR, default ../manual-code-drills) under a
per-language subdirectory; this script only produces prompts, reviews, and
reference solutions — it never compiles, runs or lints anything.

Generation can be steered per language by an optional
``profile/drill_focus_<language>.md`` (see `config.drill_focus`), which aims
themes and twists at a specific upcoming interview.

Uses CL_MODEL (Sonnet), matching answer_questions.py / generate_cl.js.

Usage:
    python scripts/drills.py generate [--language python]
    python scripts/drills.py review --number 3 [--part 2] [--language python]
    python scripts/drills.py solve  --number 3 [--part 2] [--language python]

``--part`` defaults to the drill's current (first not-yet-complete) part, and
``--language`` to config.DEFAULT_DRILL_LANGUAGE (java).

Machine-readable last line:
    GENERATED: <number>          on generate success
    REVIEWED: <number>.<part>    on review success
    SOLVED: <number>.<part>      on solve success
    ERROR: <message>             on failure
"""

import argparse
import re
import sys
from dataclasses import dataclass

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CL_MODEL,
    DEFAULT_DRILL_LANGUAGE,
    DRILL_LANGUAGES,
    DRILL_MAX_PARTS,
    DRILL_MIN_PARTS,
    DRILL_PART_TARGET_MINUTES,
    DRILL_SKILL_MAX,
    PROCESS_LOG_PATH,
    drill_class_name,
    drill_focus,
    drill_idioms,
    drill_impl_path,
    drill_lang,
    drill_language_of,
    drill_part_progress,
    drill_parts,
    drill_proficiency_brief,
    drill_skills,
    drill_test_class_name,
    drill_test_path,
    current_drill,
    find_drill,
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


# ─── Per-language prompt fragments ────────────────────────────────────────────
#
# Everything the PROMPTS say differently per language, in one record. The facts
# (paths, filenames, class names, graded vocabularies) live in config.py; this is
# wording only, which is why it lives with the prompts rather than in config.
#
# The Java fragments reproduce the original single-language prompt verbatim. That
# is deliberate: a live Java series is mid-flight, and drill sizing has regressed
# before from small prompt edits (see CLAUDE.md, "Code drills"), so the port must
# not perturb Java generation at all.

@dataclass(frozen=True)
class _LangPrompt:
    solution_lines:  str   # line budget for a part's whole solution
    part1_bans:      str   # constructs part 1 must not require
    twist_menu:      str   # the later-part constraints worth reaching for
    idiom_examples:  str   # how to word the forced-idiom rewrite part
    iface_note:      str   # how to express the given interface
    iface_examples:  str   # concrete interface line examples
    idiom_calibr:    str   # how strictly to credit an idiom at review time
    solve_style:     str   # senior-bar style rules for the reference answer
    design_block:    str   # where the reference states its design decisions
    language_notes:  str   # what THIS candidate needs from THIS language


_LANG_PROMPTS: dict[str, _LangPrompt] = {
    "java": _LangPrompt(
        solution_lines="60-90",
        part1_bans="Do not ask for custom exception types, a value/record type, "
                   "or an interface in part 1.",
        twist_menu="an ordering or tie-breaking guarantee, bounding/eviction, "
                   "defensive copying or immutability, thread safety, a "
                   "performance requirement that forbids rescanning on read, a "
                   "lifecycle/state rule, safe iteration during modification",
        idiom_examples='"Rewrite the lookup logic entirely with the Streams '
                       'API", "Redo this the way you\'d have had to in Java 8: '
                       'no var, no records, no List.of"',
        iface_note="method names WITH their parameters, but WITHOUT return "
                   "types and WITHOUT full signatures",
        iface_examples='"add(String text)", "topN(int n)", "get(String key)"',
        idiom_calibr='A single `.forEach` is not "streams"; an explicit indexed '
                     'loop is "legacy_java8".',
        solve_style="- Use modern idiomatic Java (records for value types, "
                    "standard-library methods over hand-rolled loops) and clear "
                    "naming.\n- Return defensive copies / unmodifiable views — "
                    "never expose internal mutable collections.",
        design_block="`// Design Decisions:` comment block at the top of the class",
        language_notes="",
    ),
    # Python's budget is tighter than Java's for the same work — the same drill
    # lands in noticeably fewer lines, so reusing 60-90 would quietly license a
    # bigger exercise and undo the sitting budget.
    "python": _LangPrompt(
        solution_lines="50-80",
        part1_bans="Do not ask for a custom exception class, a dataclass / "
                   "NamedTuple value type, an ABC, a decorator or a context "
                   "manager in part 1.",
        # Deliberately weighted toward Python's well-known runtime surprises:
        # these are what a Python interviewer probes and what trips an engineer
        # arriving from a statically-typed language.
        twist_menu="an ordering or tie-breaking guarantee, bounding/eviction, "
                   "defensive copying (and the shallow-vs-deep distinction), "
                   "immutability of what you hand back, exact decimal "
                   "arithmetic where floats were used, making instances usable "
                   "as dict keys or set members, safe mutation while iterating, "
                   "lazy iteration that must survive being consumed twice, a "
                   "performance requirement that forbids rescanning on read, "
                   "thread safety, a lifecycle/state rule",
        idiom_examples='"Rewrite the aggregation entirely with itertools and '
                       'functools — no explicit for-loops", "Redo this without '
                       'a single comprehension: plain loops and .append only"',
        iface_note="method names WITH their parameters (omit `self`), but "
                   "WITHOUT type annotations and WITHOUT return types",
        iface_examples='"add(text)", "top_n(n)", "get(key)" — snake_case, as '
                       'Python names them',
        idiom_calibr='A single generator expression passed to `sum()` is not '
                     '"generators" — that key is for `yield` and lazy '
                     'pipelines. An explicit `for i in range(len(xs))` loop is '
                     '"legacy_python".',
        solve_style="- Use idiomatic Python: comprehensions and generators where "
                    "they read better than loops, `dataclass` for value types, "
                    "the `collections` toolbox over hand-rolled equivalents, "
                    "f-strings, and type hints on the public methods.\n"
                    "- Use `Decimal` (never `float`) for money, and say so in "
                    "the design decisions.\n"
                    "- Never use a mutable default argument; return copies or "
                    "read-only views rather than internal containers.",
        design_block="`# Design Decisions:` comment block at the top of the module",
        # The whole reason the Python track exists: an experienced engineer whose
        # fluency is elsewhere. Drills have to build core-language reflexes, not
        # tour exotic library corners.
        language_notes="""
ABOUT THIS CANDIDATE AND THIS LANGUAGE. They are an experienced senior engineer \
whose primary language is NOT Python — they are strong at design and data \
structures, and rusty on Python's syntax and its runtime behaviour. Aim the \
drills accordingly:
- Exercise CORE language mechanics, not library trivia: dicts, sets, lists and \
tuples; slicing and unpacking; string formatting; iteration and enumerate/zip; \
comprehensions; exceptions; classes and dunder methods; sorting with a key. A \
drill that hinges on an obscure third-party API teaches nothing here.
- Use only the standard library. No pandas, no numpy, no external packages — the \
file must run under a bare interpreter.
- Prefer later-part twists drawn from Python's WELL-KNOWN RUNTIME GOTCHAS, the \
ones a Python interviewer actually probes and a newcomer actually hits: mutable \
default arguments, late binding in closures created in a loop, shallow vs deep \
copy, `is` vs `==`, mutating a collection while iterating it, a generator being \
single-use, `defaultdict` inserting on read, a mutable class attribute shared \
across instances, `__eq__` without `__hash__`, float rounding on money, \
truthiness of empty containers and `0`. Introduce these as a plain new \
REQUIREMENT on existing behaviour — never as a warning, and never by naming the \
gotcha.
""",
    ),
}


def _lp(language: str | None = None) -> _LangPrompt:
    """Prompt fragments for one language, defaulting like `config.drill_lang`."""
    return _LANG_PROMPTS.get(drill_lang(language).key,
                             _LANG_PROMPTS[DEFAULT_DRILL_LANGUAGE])


def _generate_system(language: str | None = None) -> str:
    """The generation system prompt for one language.

    A function rather than a constant because it interpolates the language's
    label, line budget, interface convention and gotcha menu. It MUST stay
    interpolated from `config` (part counts, sitting minutes, class naming) —
    hardcoding any of them is how the prompt and the sizing rules drift apart."""
    spec = drill_lang(language)
    lp   = _lp(language)
    return f"""You are an interviewer creating a SERIES of short live-coding \
drills for a senior software engineer practicing {spec.label}.

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
it is always `{spec.class_tmpl.format(n="<N>")}`, fixed by the candidate's \
project layout, so describe it by what it does ("the registry", "the tracker") \
and never as "a class called BookingRegistry".

How to size the parts — this is the part people get wrong, so read it twice:

**ONE part = ONE method plus its tests.** That is the whole unit. A part that \
asks for two or three methods is a multi-hour sitting, not a \
{DRILL_PART_TARGET_MINUTES}-minute one. AT MOST ONE new method per part is a \
hard cap, including part 1. A "small helper to register things first" is a \
SECOND METHOD and is forbidden — fold whatever it set up into the one method, \
or push it to its own part.

**Size budget: a part's complete solution — implementation AND tests together — \
should be roughly {lp.solution_lines} lines of {spec.label}.** This is the check \
that matters, because method count alone doesn't catch it. A real generated part \
came back as 246 lines and took hours: it had two methods, four domain concepts, \
a dedicated value type, a handful of custom exceptions and a dozen tests. Before \
you emit a part, ask what its solution would actually look like written out. If \
it's over that budget, cut it down or split it.

**Keep the domain surface tiny.** Part 1 may involve AT MOST TWO domain \
concepts (say "room" and "booking"). Every extra attribute — a capacity, an \
owner, a category, a timestamp — is another field to model, another branch, \
another test, and it is the fastest way to blow the budget. Attributes that the \
part's one method doesn't actually use MUST NOT appear at all.

- Part 1 is: decide how the data will be stored, write the single core \
write/ingest method, test it. Nothing else. Because there is no query method \
yet, part 1's method must be observable on its own — its return value or a \
thrown exception has to be enough to write real tests against. That last point \
is a DESIGN CONSTRAINT ON YOU when choosing the method, never text to put in \
the prompt: telling the candidate "make sure your return value is observable \
enough to test" is coaching, and it hands them a decision that is theirs.
- {lp.part1_bans} If the candidate wants one they can introduce it; requiring it \
turns a one-method sitting into a design exercise.
- Every later part adds AT MOST ONE new method, ONE new constraint on \
behaviour that already exists, or both — never two of either. Good constraints: \
{lp.twist_menu}.
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
- **State the IDENTITY MODEL in the overview whenever the series relies on \
one.** If any part identifies an entity by a field — a name, an id, a code — the \
overview must say so plainly ("passengers are identified by name"). This reveals \
no gotcha; it is a data-model fact, exactly like pinning down what a slot is. \
Omitting it is worse than ambiguous, it is a trap: a candidate who sees only \
`claim(String passenger, int seat)` reasonably notices that two passengers can \
share a name, designs an id-returning API to fix it, and only discovers hours \
later that a hidden method takes the name — a dead end your silence created.
- Give each part's interface as {lp.iface_note} — choosing the return shape is \
part of the exercise. Example: {lp.iface_examples}.
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
the candidate's graded history — the skills they score worst on and the \
{spec.label} idioms they rarely or never reach for. Use it:
- Pick a theme that naturally exercises the WEAK SKILLS named there.
- Include EXACTLY ONE re-implementation part that forces a rare idiom. This \
part adds NO new method and NO new behaviour: it asks the candidate to rewrite \
what they already built using the required idiom, keeping every existing test \
passing unchanged as proof the rewrite is faithful. Its "interface" is an empty \
list. Word the constraint as a REQUIREMENT, never a suggestion — \
{lp.idiom_examples}. Its tasks look like: "Rewrite X using <idiom>." / "Keep \
every existing test passing unchanged." Place it after the part whose code it \
rewrites.
- NEVER more than one idiom-constrained part per drill, and never require an \
idiom the Targeting section doesn't name.
- With no "Targeting" section, generate normally — no re-implementation part.

FOCUS. If the user's message carries a "Focus" section, it aims this drill at a \
SPECIFIC upcoming interview: the domains to draw the theme from, the shape of \
question that company asks, and gotchas worth using as later-part twists. Treat \
it as a constraint on WHAT you choose, and obey it over your own preference of \
theme. It does NOT relax anything above: the part sizing, the one-method cap, \
the line budget, the no-coaching rule and the ban on foreshadowing later parts \
all still hold exactly as written. If a focus seems to ask for a bigger part, \
split it into more parts instead. With no "Focus" section, choose freely.
{lp.language_notes}
Return ONLY a JSON object, no prose, no code fences:
{{"title": "<3-6 word name for the whole series>", "premise": "<3-5 sentence \
overview of the whole drill: what is being built, who calls it and why, and \
that it is one class extended across sittings>", "parts": [{{"title": "<3-6 \
word name for this part>", "prompt": "<the spoken-style prompt for THIS part \
only>", "tasks": ["<imperative step>", "..."], "interface": \
["method(params)", "..."]}}]}}"""

def _review_system(language: str | None = None) -> str:
    """The review + grading system prompt for one language.

    MUST stay interpolated (it was briefly a plain string, and the model
    dutifully echoed the literal sentinel placeholder and invented its own skill
    keys — silently ungraded, because the sanitizer drops unknown keys). It now
    also interpolates the LANGUAGE's vocabularies: grading a Python attempt
    against Java's idiom list would score every idiom absent."""
    spec = drill_lang(language)
    lp   = _lp(language)
    skill_keys  = ", ".join(f'"{k}": 0-{DRILL_SKILL_MAX}'
                            for k in drill_skills(spec.key))
    skill_descs = "; ".join(f"{k} = {v}"
                            for k, v in drill_skills(spec.key).items())
    idiom_descs = "; ".join(f"{k} = {v}"
                            for k, v in drill_idioms(spec.key).items())
    return f"""You are a staff-level engineer reviewing a candidate's drill \
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
5. Idiomatic {spec.label} & data-structure choice — right collection for the \
job, standard-library methods that would simplify the code, naming. Flag it if \
the earlier structure should have been reshaped for this part rather than bolted \
on.
6. Complexity — time/space, and any needless rework.
7. The tests — do they actually pin down the behaviour, including edge cases?
8. Interview signal — what would make an interviewer raise an eyebrow even if \
the code works. If they identified a genuine gap in the interface they were \
given — one that cannot express something the part requires — say so and count \
it in their favour: spotting that in a real design review is senior behaviour, \
not an excuse. Distinguish it from merely disliking a decision that was theirs \
to make.

End with a one-line verdict: would this part clear a senior bar? Keep it tight \
— Markdown, no preamble. Do not speculate about, or hint at, what later parts \
might ask for.

Then, after the prose, emit a machine-readable assessment. Put this sentinel on \
a line of its own:

{_ASSESSMENT_SENTINEL}

and follow it with ONLY a JSON object — no prose, no code fence:
{{"skills": {{{skill_keys}}}, "idioms_used": ["<idiom>", "..."]}}

Scoring rules:
- Score every skill 0-{DRILL_SKILL_MAX} against a SENIOR bar, where \
{DRILL_SKILL_MAX} means "an interviewer would call this exemplary" and 3 means \
"acceptable, with reservations". Judge only what this part asked for. Be \
honest — inflated scores make the whole profile useless.
- Skills are: {skill_descs}
- "idioms_used" lists ONLY the idioms genuinely present in the candidate's \
code, drawn from this exact vocabulary (use the keys verbatim, omit any that \
don't apply, never invent one): {idiom_descs}
- Judge idioms by what the code actually does, not by what would have been \
nice. {lp.idiom_calibr}"""


def _solve_system(number: int, language: str | None = None) -> str:
    """The reference-solution system prompt for one language.

    Takes the drill NUMBER as well, because the reference has to be written as
    the exact class the operator's file must declare — Java will not compile
    otherwise, and a Python module that defines a differently-named class breaks
    the test's import."""
    spec  = drill_lang(language)
    lp    = _lp(language)
    impl  = spec.impl_rel.format(n=number).rsplit("/", 1)[-1]
    test  = spec.test_rel.format(n=number).rsplit("/", 1)[-1]
    return f"""You are a staff-level engineer writing the reference solution to \
an interview-style {spec.label} drill — the answer that would clear a strong \
senior/staff bar in a live coding round.

The drill is a multi-part series in which every part extends the same class, so \
your solution is CUMULATIVE: implement the current part AND every earlier part \
in one class, exactly as the candidate's file should look at this point. Do not \
implement, mention, or leave hooks for parts that haven't been given to you.

Because the prompt is deliberately underspecified, FIRST state the design \
decisions you're committing to (case sensitivity, tie-breaking, null/empty \
handling, edge-case returns, and the return types the interface left open) in a \
short {lp.design_block}.

Then write the full implementation, holding a senior bar:
- Pick the right data structures; pre-aggregate on write when the prompt implies \
reads must stay fast (don't rescan on read).
{lp.solve_style}
- Guard/validate inputs per your stated decisions; keep methods small.

Also provide a {spec.test_framework} test suite that pins down the behaviour \
INCLUDING the edge cases your design decisions call out.

Output format (Markdown), in this exact order and nothing else:
1. A `### Design notes` section: 3-6 bullets on the key senior-level choices and \
any trade-off worth naming aloud in an interview. When this isn't part 1, one \
bullet must say what the current part's requirement forced you to change in the \
earlier design, and why bolting it on would not have worked.
2. A ```{spec.fence} fenced block with the complete `{impl}`, declaring class \
`{drill_class_name(number, spec.key)}`.
3. A ```{spec.fence} fenced block with the complete `{test}` \
({drill_test_class_name(number, spec.key)}), importing the class under test."""


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


def _log_id(number: int, language: str | None = None) -> str:
    """Process-log `entity_id` for a drill: "<language>:<number>".

    Qualified because each language numbers its own track, so a bare "9" now
    matches two different drills. Nothing reads these events back — the log is a
    write-only audit trail — but an ambiguous id would make it unusable for the
    one thing it exists for."""
    return f"{drill_lang(language).key}:{number}"


def generate_drill(language: str = "java") -> dict:
    """Generate a multi-part drill series and return the record.

    Regenerating rerolls the current drill IN PLACE, at the same number, only
    while none of its parts are complete — once a sitting has been banked, a
    reroll would throw that work away, so generation moves on to the next
    number instead (as it also does when the drill is finished or the store is
    empty). Every generated series — including rerolls — is logged to the
    process log, so there's a durable record of which drills were created even
    though the store keeps only the latest version of an active drill.

    Scoped to ONE language: the reroll check, the numbering and the
    already-used list all come from that language's track, so generating a
    Python drill can neither reroll nor renumber an in-flight Java series."""
    spec   = drill_lang(language)
    drills = load_drills()
    cur    = current_drill(drills, spec.key)
    banked = drill_part_progress(cur)[0] if cur else 0
    # Work exists before it's marked complete. Code written against this drill's
    # prompts counts as banked even with zero parts ticked off, or a reroll
    # silently orphans the file the operator has been writing all session.
    if cur and not banked and _has_written_code(int(cur.get("number", 0)),
                                                spec.key):
        banked = 1
    if cur and cur.get("status") == "active" and banked == 0:
        number, regen = int(cur["number"]), True
    else:
        number, regen = next_drill_number(spec.key), False

    # Only this language's drills — a Java theme is not "already used" for
    # Python, and listing it would rule out a perfectly good first Python drill.
    existing = [f"Drill {d.get('number')}: {d.get('title','')}"
                for d in drills if drill_language_of(d) == spec.key]
    if existing:
        used = "\n".join(existing)
    elif spec.key == "java":
        used = ("(none yet — Drill 1 was a multi-level flag store, "
                "Drill 2 a word-frequency counter; avoid those shapes)")
    else:
        used = f"(none yet — this is the first {spec.label} drill)"
    user = (f"This is Drill {number}. Drills already used:\n{used}\n\n"
            f"Create a new, distinct {spec.label} drill series.")
    # Targeting is omitted entirely until something has been graded ON THIS
    # TRACK, so the first drill in a new language isn't aimed at another
    # language's weaknesses.
    brief = drill_proficiency_brief(drills, spec.key)
    if brief:
        user += f"\n\n## Targeting\n{brief}"
    # Optional per-language steer at profile/drill_focus_<language>.md.
    focus = drill_focus(spec.key)
    if focus:
        user += f"\n\n## Focus\n{focus}"

    raw  = _call_claude(_generate_system(spec.key), user)
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
        "language":     spec.key,
        "title":        (data.get("title") or f"Drill {number}").strip(),
        "premise":      (data.get("premise") or "").strip(),
        "parts":        parts,
        "status":       "active",
        "created_at":   now_utc(),
        "completed_at": None,
    }
    if regen:
        drills = [record if (int(d.get("number", 0)) == number
                             and drill_language_of(d) == spec.key) else d
                  for d in drills]
    else:
        drills.append(record)
    save_drills(drills)

    verb = "Regenerated" if regen else "Generated"
    _append_log({"event_type": "drill_generated", "entity_type": "drill",
                 "entity_id": _log_id(number, spec.key),
                 "entity_name": record["title"], "language": spec.key,
                 "detail": f"{verb} Drill {number} ({spec.key}): {record['title']} "
                           f"— {len(parts)} parts",
                 "premise": record["premise"], "parts": parts,
                 "focused": bool(focus), "regenerated": regen})
    return record


_CLARIFY_MAX_TOKENS = 600

_CLARIFY_SYSTEM = """You are the interviewer who set this drill, answering a \
question from the candidate mid-exercise.

You can see the WHOLE series, including parts the candidate has not been shown. \
They can see only the drill overview and the current part. Your answer must \
respect that: give them the CONSTRAINT they need, never a later part's methods, \
title or twist. "Passengers are identified by name for this whole drill" is \
fine; "part 4 hands you swap(String, String)" is not.

Decide which of three cases the question falls into, and answer accordingly.

1. THEIRS TO DECIDE — the answer is one of the ambiguities the drill \
deliberately leaves open (tie-breaking, case sensitivity, null/empty handling, \
what to return on a clash). Do NOT decide it for them. Say it's their call, name \
the trade-off in one line, and tell them to record the decision.

2. A CONSTRAINT THEY CANNOT SEE — the design they're considering is foreclosed \
by something later in the series. Say so plainly and state the constraint, so \
they stop exploring a dead end. Do not say which part imposes it or how.

3. A GENUINE FLAW IN THE DRILL — the given interface cannot express something \
the part actually requires, or the prompt contradicts itself. Say plainly that \
it's a flaw rather than a puzzle, tell them how to proceed (usually: change the \
return type or add what's missing, and note it), and tell them it will count in \
their favour at review. Do not defend a bad interface.

Be brief — a short paragraph, no preamble, no headings. Never write their \
implementation for them, and never hand over a decision that is case 1."""


def _full_series_context(record: dict, current: dict) -> str:
    """The WHOLE series — including parts the candidate hasn't reached — marked
    up so the model knows what they can and cannot see. For `clarify_part` only:
    judging whether an interface is genuinely broken requires the parts that
    constrain it. Never used for review or solve, which must stay blind to what
    comes next."""
    parts = drill_parts(record)
    idx   = int(current.get("part", 1))
    out   = [f"## Drill {record.get('number')}: {record.get('title','')} "
             f"({len(parts)} parts)"]
    if record.get("premise"):
        out.append(f"## Overview (the candidate HAS seen this)\n{record['premise']}")
    for p in parts:
        i    = int(p.get("part", 0))
        seen = ("HAS SEEN" if i <= idx else
                "HAS NOT SEEN — do not reveal")
        out.append(f"## Part {i}: {p.get('title','')} [{seen}]\n"
                   f"{p.get('prompt','')}\n\n"
                   f"Methods it introduces:\n{_iface_lines(p)}")
    out.append(f"The candidate is working on part {idx}.")
    return "\n\n".join(out)


def clarify_part(number: int, question: str, part: int | None = None,
                 language: str | None = None) -> tuple:
    """Answer a candidate's question about the part they're on, and store the
    exchange. Returns ``(part number, answer)``.

    This exists because hiding later parts — which is what keeps a sitting to an
    hour — means a design decision made in part 1 can be silently foreclosed by
    an interface the candidate isn't allowed to see. Without a release valve
    they burn the sitting on a dead end. Real case: `claim(String, int)` can't
    distinguish two passengers with the same name, so returning a UUID looks
    right, but nothing later in the series accepts one.

    Cheap by design — the series and the question, never the candidate's code."""
    drills, record, target = _load_for_part(number, part, language)
    lang = drill_language_of(record)
    pnum = int(target.get("part", 1))
    q    = (question or "").strip()
    if not q:
        raise ValueError("Ask an actual question.")

    user = (f"{_full_series_context(record, target)}\n\n"
            f"## The candidate's question about part {pnum}\n{q}")
    answer = _call_claude(_CLARIFY_SYSTEM, user,
                          max_tokens=_CLARIFY_MAX_TOKENS).strip()

    target.setdefault("clarifications", []).append(
        {"at": now_utc(), "question": q, "answer": answer})
    save_drills(drills)
    _append_log({"event_type": "drill_clarified", "entity_type": "drill",
                 "entity_id": _log_id(number, lang),
                 "entity_name": record.get("title", ""), "language": lang,
                 "part": pnum, "question": q,
                 "detail": f"Answered a question on Drill {number} "
                           f"({lang}) part {pnum}."})
    return pnum, answer


def _iface_lines(part: dict) -> str:
    return "\n".join(f"- {s}" for s in part.get("interface", [])) or "- (none new)"


def _task_lines(part: dict) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(part.get("tasks") or [], 1))


def _has_written_code(number: int, language: str | None = None) -> bool:
    """True if the operator has written anything of their own into this drill's
    impl file. An appended reference solution doesn't count — it's ours, not
    theirs — so it's stripped before the check."""
    path = drill_impl_path(number, language)
    if not path.exists():
        return False
    body = strip_correct_code(path.read_text(encoding="utf-8", errors="replace"))
    return bool(body.strip())


def _code_block_re(language: str | None = None) -> re.Pattern:
    """Matcher for the solve response's fenced blocks in one language."""
    return re.compile(rf"```{re.escape(drill_lang(language).fence)}\s*\n(.*?)```",
                      re.S)


def _split_solution_blocks(markdown: str,
                           language: str | None = None) -> tuple[str, str, str]:
    """``(design notes, impl code, test code)`` from a solve response, which is
    a notes section followed by two fenced blocks. Missing blocks come back
    empty rather than raising — a partial solution is still worth storing."""
    blocks = _code_block_re(language).findall(markdown)
    notes  = markdown.split("```", 1)[0].strip()
    impl   = blocks[0].strip() if blocks else ""
    test   = blocks[1].strip() if len(blocks) > 1 else ""
    return notes, impl, test


_PACKAGE_RE = re.compile(r"^\s*package\s+[\w.]+\s*;", re.M)


def _preamble(path, language: str | None = None) -> str:
    """The declaration a rewritten file must keep in order to still build.

    Java: the existing `package …;`. The generated reference carries imports but
    never a package line — it doesn't know the project layout — so it has to be
    re-attached from what was already there, or the rewrite produces a file that
    doesn't compile.

    Python: nothing. The flat per-language directory has no package, and
    inventing one would break the test's import."""
    if drill_lang(language).key != "java":
        return ""
    if path.exists():
        m = _PACKAGE_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(0).strip()
    return "package drills;"


def capture_attempt(number: int, language: str | None = None) -> dict:
    """Snapshot the operator's own code before it is replaced, so a finish is
    reversible and their work is never simply destroyed. Any legacy appended
    reference block is stripped first — that part was never theirs."""
    out = {"at": now_utc()}
    for key, path in (("impl", drill_impl_path(number, language)),
                      ("test", drill_test_path(number, language))):
        out[key] = (strip_correct_code(
            path.read_text(encoding="utf-8", errors="replace"))
            if path.exists() else "")
    return out


def install_reference_code(number: int, part_no: int, solution_md: str,
                           language: str | None = None) -> list:
    """REPLACE this drill's impl + test files with the reference solution as
    real, runnable code, and return the paths rewritten.

    Replacing rather than appending is the point. The file used to accumulate
    one pasted instruction block per sitting plus a commented copy of the
    reference: by part 6 that is ~150 lines of stale prose above any code, in a
    class the operator has to work in. Rewriting leaves a clean, correct base
    for the next sitting and keeps exactly one instruction block in the file —
    the one they paste for the part in hand.

    The design notes ride along as a header comment (they explain the choices
    the code embodies, and are short). Anything the file needs in order to still
    build — Java's package line — is re-attached by `_preamble`."""
    spec = drill_lang(language)
    notes, impl, test = _split_solution_blocks(solution_md, spec.key)
    c = spec.comment
    written = []
    for path, code, with_notes in (
            (drill_impl_path(number, spec.key), impl, True),
            (drill_test_path(number, spec.key), test, False)):
        if not code:
            continue
        pre    = _preamble(path, spec.key)
        header = (f"{c} ===== Reference solution — Drill {number}, "
                  f"parts 1-{part_no} =====\n"
                  f"{c} Generated by next-role after finishing part {part_no}. "
                  f"Your own attempt is\n"
                  f"{c} saved in data/drills.json and restored by "
                  f"`drills.py revert`.\n")
        if with_notes and notes:
            header += f"{c}\n" + "\n".join(
                f"{c} {l}" if l.strip() else c
                for l in notes.splitlines()) + "\n"
        # The per-language directory may not exist yet — the first Python drill
        # creates it.
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"{pre}\n\n{header}\n{code}\n" if pre else f"{header}\n{code}\n"
        path.write_text(body, encoding="utf-8")
        written.append(path)
    return written


def _split_assessment(text: str,
                      language: str | None = None) -> tuple[str, dict | None]:
    """Split a review into ``(prose, assessment | None)`` on the sentinel.

    Anything unparseable degrades to ``(whole text, None)``: a malformed grade
    must never cost the operator their written feedback, and an ungraded part
    simply doesn't contribute to the profile. Unknown skill/idiom keys are
    dropped and scores clamped, so a hallucinated dimension can't enter the
    vocabulary through the back door.

    The vocabularies are the LANGUAGE's — validating a Python grade against
    Java's keys would drop every skill and silently leave the part ungraded."""
    skills_vocab = drill_skills(language)
    idioms_vocab = drill_idioms(language)
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
        if key not in skills_vocab:
            continue
        try:
            skills[key] = max(0, min(DRILL_SKILL_MAX, int(round(float(val)))))
        except (TypeError, ValueError):
            continue
    idioms = [s for s in (data.get("idioms_used") or [])
              if isinstance(s, str) and s in idioms_vocab]
    if not skills:
        return prose.strip(), None
    return prose.strip(), {"at": now_utc(), "skills": skills,
                           "idioms_used": sorted(set(idioms))}


def _load_for_part(number: int, part: int | None,
                   language: str | None = None) -> tuple[list, dict, dict]:
    """Resolve ``(all drills, record, part)`` for drill ``number`` on one
    language's track, defaulting to its current part. Materializes a legacy
    record's adapted part into ``parts`` so the caller's mutation of the returned
    part persists."""
    drills = load_drills()
    lang   = drill_lang(language).key
    record = find_drill(drills, number, lang)
    if not record:
        raise ValueError(f"Drill {number} ({lang}) not found in the store.")
    record["parts"] = drill_parts(record)
    target = find_drill_part(record, part)
    if target is None:
        raise ValueError(f"Drill {number} ({lang}) has no part {part}.")
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


def review_drill(number: int, part: int | None = None,
                 language: str | None = None) -> tuple[int, str]:
    """Review the operator's attempt at ONE part of drill ``number`` (defaults
    to the current part). Reads the shared impl + test file from the drills
    project — every part extends the same class, so the review sees the whole
    file and can flag regressions in earlier parts — calls Claude, stores the
    feedback on the part and returns ``(part number, feedback)``."""
    drills, record, target = _load_for_part(number, part, language)
    lang = drill_language_of(record)
    spec = drill_lang(lang)
    pnum = int(target.get("part", 1))

    impl_p, test_p = (drill_impl_path(number, lang),
                      drill_test_path(number, lang))
    if not impl_p.exists():
        raise FileNotFoundError(
            f"No attempt found at {impl_p}. Write "
            f"{impl_p.name} first (in the manual-code-drills project).")
    # Strip any previously appended reference solution FIRST — otherwise Claude
    # reads its own correct answer as the candidate's work and grades it as
    # theirs, which would silently inflate the whole proficiency profile.
    impl_code = strip_correct_code(
        impl_p.read_text(encoding="utf-8", errors="replace"))
    test_code = (strip_correct_code(
                     test_p.read_text(encoding="utf-8", errors="replace"))
                 if test_p.exists() else "(no test file written yet)")

    # Finishing a part REWRITES the files with the reference solution, so from
    # part 2 on the file legitimately contains code the candidate was given.
    # Hand that baseline over explicitly, or the reviewer credits them for it and
    # the proficiency profile inflates in the flattering direction.
    baseline = ""
    earlier = [p for p in drill_parts(record)
               if int(p.get("part", 0)) < pnum and p.get("solution")]
    if earlier:
        last = max(earlier, key=lambda p: int(p.get("part", 0)))
        _, b_impl, b_test = _split_solution_blocks(last["solution"]["text"],
                                                  lang)
        baseline = (
            f"## Baseline they were GIVEN, not code they wrote\n"
            f"After part {last.get('part')} the files were replaced with this "
            f"reference solution. Anything still unchanged from it is NOT the "
            f"candidate's work and must not be credited to them — judge only "
            f"what they added or altered on top of it.\n\n"
            f"```{spec.fence}\n{b_impl}\n```\n\n"
            f"```{spec.fence}\n{b_test}\n```\n\n")

    # Anything they asked and were told this sitting. Without this the reviewer
    # can penalise an assumption it sanctioned — marking down "you assumed names
    # are unique" when that is precisely what the clarification instructed.
    clar = ""
    rows = target.get("clarifications") or []
    if rows:
        clar = "## Clarifications the candidate asked for, and the answers given\n"
        for c in rows:
            clar += f"\nQ: {c.get('question','')}\nA: {c.get('answer','')}\n"
        clar += ("\nDo not penalise them for following these answers. If one of "
                 "their questions identified a genuine gap in the given "
                 "interface, treat that as senior signal and say so.\n\n")

    user = (
        f"{_series_context(record, target)}\n\n"
        f"{baseline}"
        f"{clar}"
        f"## Candidate's {impl_p.name}\n"
        f"```{spec.fence}\n{impl_code}\n```\n\n"
        f"## Candidate's {test_p.name}\n"
        f"```{spec.fence}\n{test_code}\n```\n")

    raw = _call_claude(_review_system(lang), user, max_tokens=REVIEW_MAX_TOKENS)
    feedback, assessment = _split_assessment(raw, lang)

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
                 "entity_id": _log_id(number, lang),
                 "entity_name": record.get("title", ""), "language": lang,
                 "part": pnum, "assessment": assessment,
                 "detail": f"Reviewed Drill {number} ({lang}) part {pnum} "
                           f"({graded})."})
    return pnum, feedback


def solve_drill(number: int, part: int | None = None,
                language: str | None = None) -> tuple[int, str]:
    """Generate a senior/staff-level reference solution for drill ``number`` as
    of ONE part (defaults to the current part), from the prompts + interfaces
    alone — it does NOT read the operator's attempt. The solution is cumulative
    (this part plus every earlier one) because the parts share a class. Stores
    it on the part as ``solution`` and returns ``(part number, markdown)``."""
    drills, record, target = _load_for_part(number, part, language)
    lang = drill_language_of(record)
    pnum = int(target.get("part", 1))

    user = (
        f"{_series_context(record, target)}\n\n"
        f"Write the reference solution as class "
        f"`{drill_class_name(number, lang)}` in "
        f"`{drill_impl_path(number, lang).name}` (tests in "
        f"`{drill_test_path(number, lang).name}`), implementing this part and "
        f"every earlier part shown above.")

    solution = _call_claude(_solve_system(number, lang), user,
                            max_tokens=SOLVE_MAX_TOKENS).strip()

    target["solution"] = {"at": now_utc(), "text": solution}
    save_drills(drills)
    # Deliberately does NOT touch the source files. Installing the reference is
    # part of FINISHING a sitting (finish_part); overwriting an attempt just
    # because the answer was generated would destroy work mid-session.
    _append_log({"event_type": "drill_solved", "entity_type": "drill",
                 "entity_id": _log_id(number, lang),
                 "entity_name": record.get("title", ""), "language": lang,
                 "part": pnum,
                 "detail": f"Generated reference solution for Drill {number} "
                           f"({lang}) part {pnum}."})
    return pnum, solution


def restore_attempt(number: int, part: dict,
                    language: str | None = None) -> list:
    """Put the operator's own code back after a reverted finish, from the
    snapshot `finish_part` took before overwriting it. Returns paths rewritten.

    Without this the revert would be destructive rather than an undo: finishing
    replaces the files with the reference, so a bare "reopen the part" would
    leave the reference sitting where their attempt used to be, and they would
    have nothing to revise."""
    attempt = part.get("attempt") or {}
    written = []
    for key, path in (("impl", drill_impl_path(number, language)),
                      ("test", drill_test_path(number, language))):
        code = attempt.get(key)
        if not code:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code.rstrip("\n") + "\n", encoding="utf-8")
        written.append(path)
    return written


def revert_part(number: int, part: int | None = None,
                language: str | None = None) -> dict:
    """Undo a finish: restore the part in the store AND put the operator's own
    code back over the installed reference. ``part=None`` reverts the most
    recently completed part.

    Safe to run: it's logged as `drill_part_reverted`, and it only ever removes
    things this tool added — the operator's own code is never touched."""
    lang   = drill_lang(language).key
    before = find_drill(None, number, lang)
    if not before:
        raise ValueError(f"Drill {number} ({lang}) not found in the store.")

    record = revert_drill_part(number, part, lang)
    if record is None:
        raise ValueError(
            f"Nothing to revert on Drill {number} ({lang})"
            + (f" part {part}" if part is not None else "")
            + " — no completed part found.")

    reverted = next((p for p in drill_parts(record)
                     if p.get("status") != "complete"), None)
    pnum  = int(reverted.get("part", 0)) if reverted else 0
    files = restore_attempt(number, reverted or {}, lang)

    _append_log({"event_type": "drill_part_reverted", "entity_type": "drill",
                 "entity_id": _log_id(number, lang),
                 "entity_name": record.get("title", ""), "language": lang,
                 "part": pnum, "files": [str(f) for f in files],
                 "detail": f"Reverted Drill {number} ({lang}) part {pnum} to "
                           f"active (grade, reference answer and last review "
                           f"dropped)."})
    return {"part": pnum, "files": files, "title": record.get("title", "")}


def finish_part(number: int, part: int | None = None,
                language: str | None = None) -> dict:
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
    lang = drill_lang(language).key
    pnum, feedback = review_drill(number, part, lang)

    solution, written = None, []
    try:
        _, solution = solve_drill(number, pnum, lang)
    except Exception as e:  # noqa: BLE001 — never block completion on the bonus
        print(f"WARNING: reference answer failed ({e}); grading still stands.")

    # Snapshot their work BEFORE overwriting it, then rewrite the files with the
    # reference so the next sitting starts from a clean, correct base instead of
    # a class buried under one instruction block per part.
    if solution:
        attempt = capture_attempt(number, lang)
        written = install_reference_code(number, pnum, solution, lang)
        drills_now = load_drills()
        tgt = find_drill_part(find_drill(drills_now, number, lang), pnum)
        if tgt is not None:
            tgt["attempt"] = attempt
            save_drills(drills_now)

    record = mark_drill_part_complete(number, pnum, lang)
    return {"part": pnum, "feedback": feedback, "solution": solution,
            "written": written, "completed": bool(record)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate / review code drills.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Every subcommand takes --language: each language is its own numbering
    # track, so (language, number) is what identifies a drill.
    def _lang_arg(p):
        p.add_argument("--language", default=DEFAULT_DRILL_LANGUAGE,
                       choices=sorted(DRILL_LANGUAGES),
                       help="Drill language track "
                            f"(default: {DEFAULT_DRILL_LANGUAGE}).")
        return p

    g = _lang_arg(sub.add_parser("generate",
                                 help="Generate the next drill series."))

    r = _lang_arg(sub.add_parser("review",
                                 help="Review a manual attempt at one part."))
    r.add_argument("--number", type=int, required=True)
    r.add_argument("--part", type=int, default=None,
                   help="Part to review (default: the current part).")

    s = _lang_arg(sub.add_parser("solve",
                                 help="Generate the reference (correct) solution."))
    s.add_argument("--number", type=int, required=True)
    s.add_argument("--part", type=int, default=None,
                   help="Part to solve up to (default: the current part).")

    c = _lang_arg(sub.add_parser("clarify",
                                 help="Ask a question about the current part."))
    c.add_argument("--number", type=int, required=True)
    c.add_argument("--part", type=int, default=None)
    c.add_argument("--question", required=True)

    v = _lang_arg(sub.add_parser("revert",
                                 help="Undo a finish/complete for one part."))
    v.add_argument("--number", type=int, required=True)
    v.add_argument("--part", type=int, default=None,
                   help="Part to revert (default: the most recently completed).")

    f = _lang_arg(sub.add_parser("finish",
                                 help="Grade + reference answer + mark complete."))
    f.add_argument("--number", type=int, required=True)
    f.add_argument("--part", type=int, default=None,
                   help="Part to finish (default: the current part).")

    args = parser.parse_args()
    lang = args.language
    try:
        if args.cmd == "generate":
            rec = generate_drill(lang)
            print(f"Generated Drill {rec['number']} ({lang}): {rec['title']} "
                  f"({len(rec['parts'])} parts)")
            print(f"Write {drill_impl_path(rec['number'], lang)}")
            print(f"GENERATED: {rec['number']}")
        elif args.cmd == "review":
            pnum, fb = review_drill(args.number, args.part, lang)
            print(fb)
            print(f"REVIEWED: {args.number}.{pnum}")
        elif args.cmd == "solve":
            pnum, sol = solve_drill(args.number, args.part, lang)
            print(sol)
            print(f"SOLVED: {args.number}.{pnum}")
        elif args.cmd == "clarify":
            pnum, ans = clarify_part(args.number, args.question, args.part, lang)
            print(ans)
            print(f"CLARIFIED: {args.number}.{pnum}")
        elif args.cmd == "revert":
            r = revert_part(args.number, args.part, lang)
            print(f"Reverted Drill {args.number} ({lang}) part {r['part']} "
                  f"to active.")
            print("Dropped: grade, reference answer, last review.")
            for f_ in r["files"]:
                print(f"Updated {f_}")
            print(f"REVERTED: {args.number}.{r['part']}")
        elif args.cmd == "finish":
            r = finish_part(args.number, args.part, lang)
            print(r["feedback"])
            if r["solution"]:
                print(f"\n{r['solution']}")
            for p in r["written"]:
                print(f"Rewrote with reference solution: {p}")
            print(f"FINISHED: {args.number}.{r['part']}")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
