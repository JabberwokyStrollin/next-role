<!--
answer_questions_rules.md — Rules for ad-hoc application question answering.
Retrieved by scripts/answer_questions.py before every generation call.
Two question classes with separate prompt strategies, plus shared constraints.
-->

## Shared Rules (apply to both question classes)

### Accuracy
- Every claim must be grounded in the resume provided. Do not fabricate
  experience, invent projects, or reference technologies the resume does not
  show. Adjacent exposure is not direct experience.
- Do not claim familiarity or hands-on experience with a specific technology
  unless the resume directly backs it.
- If a question cannot be answered from the available resume material and any
  supplemental notes provided, say so clearly rather than stretching.
- Resume entry notes are corrections and constraints — treat them as
  authoritative overrides. If a note says "partial adoption only," the answer
  must reflect that. If a note says "solo work," the answer must not imply
  team leadership.

### Tone
- Conversational, not corporate. Write as if briefing a technically literate peer.
- No first-person emotional or preference claims. This is a CATEGORY ban:
  any phrasing that asserts how the candidate feels about work is forbidden,
  regardless of the specific words used.
  - Forbidden patterns (representative, not exhaustive): any form of
    "I'm proud / I'm passionate / I'm excited / what excites me /
    I love / I enjoy / my favorite / the work I find rewarding."
  - The test: if a sentence asserts a feeling or preference that the resume
    does not state, it is banned. State what the candidate did; let him
    decide how he feels about it.
- Objective-descriptive language about the work is encouraged:
  "the hardest part was...", "the trick was...", "the non-obvious thing here
  was...", "what made this tricky was..." These add conversational rhythm
  without putting words in the candidate's mouth. Any objective ranking
  (hardest, most complex) must match the reality described in the resume
  entry notes.
- Do not insinuate that prior systems, teams, or work were poor. Frame
  contributions as additions, improvements, or designs without passing
  judgment on what came before.
- No defensive phrasing. "These weren't theoretical exercises" implies
  someone thought they were. Let the specifics speak.
- Avoid generic filler: "I am passionate about", "I thrive in", "I am excited
  to", "translating ambiguous problems into production-grade systems."
  Cut on sight.
- Proactively frame gaps honestly. If the question touches a technology
  the candidate doesn't have, name the closest adjacent work and be clear
  about the gap.

### Character caps
- When a char_cap is provided, it is a hard limit — not a target, not an
  approximation. Count precisely and stay under. Do not instruct yourself
  to "aim for" or "approximately" — the answer must be under the cap.
- When no char_cap is provided, aim for a focused, complete answer.
  Behavioral answers: 400-700 characters is a reasonable target unless the
  question implies more depth. Motivation answers: match the register of
  the question (short question = short answer unless a cap implies more).

### Output format
- Return a JSON object: {"answer": "...", "resume_entries_used": ["slug1", "slug2"]}
- `resume_entries_used` must be a list of slugs from the provided
  RESUME_ENTRY_SLUGS registry. Only include entries actually drawn on.
- The answer string must already have sanitized text: no asterisks, no
  angle brackets, no markdown formatting. Plain prose only.
- Do not wrap the JSON in markdown fences.
- **No dashes of any kind.** Em dashes, en dashes, double-hyphens, and
  single hyphens used between spaces are AI tells. Use a comma or a colon
  instead. Hyphens INSIDE words ("well-known", "real-time",
  "client-server") and between digits ("5-10 years") are fine, but never
  use a dash to break up clauses.

---

## Class A — Motivation / "Why this company or role?"

These questions ask why the candidate wants to work at a specific company or
in a specific role. The answer must connect his genuine background to
something concrete in the job description.

### Strategy
1. Identify what in the JD (role responsibilities, technical domain, scale,
   company product) maps to work the candidate has actually done.
2. Anchor the answer in that mapping — not in generic enthusiasm, and not
   in claims about the company's culture or values beyond what the JD states.
3. Do not claim to know things about the company that are not in the JD
   (internal culture, team dynamics, unannounced products, etc.).
4. The interest statement must anchor to something in the candidate's
   actual background — it must not echo the JD's stack as if he's used it
   when he hasn't.

### Anti-patterns
- "I've always admired Miro's culture of innovation." — culture claim not
  grounded in the JD.
- "I'm excited to work on real-time collaboration at scale." — emotional
  claim, and "excited" is forbidden.
- "Miro's approach to distributed systems aligns perfectly with my passion
  for streaming." — two violations: ungrounded company claim + emotional claim.

### Correct pattern
Connect a specific thing the JD describes to a specific thing in the resume,
in an honest, direct tone. Example structure: "The [problem/domain] in this
role maps directly to [what I did at AT&T / Insight Global / etc.] — [brief
grounding]. [One concrete connection to the JD's described work.]"

---

## Class B — Behavioral / "Describe a time you did X"

These questions ask for a specific instance. Pick the single best-fit resume
entry and tell a focused story. Do not stack multiple projects — the question
asks for a time (singular).

### Strategy
Use a build-to-win structure (same discipline as the cover letter body):
1. Open with the problem context or the decision the candidate was facing
   (1 sentence). The reader should know what the situation was before the
   narrative begins.
2. Describe what he observed, owned, or chose (1-2 sentences).
3. Land the outcome — specific metric where applicable — as a natural
   consequence of 1 and 2.

The win should feel earned. The reader should arrive at it, not be handed it.

### Anti-patterns
- Leading with the project name and metric in sentence one: "I architected
  HALOC Distilled, delivering 95% cost reduction." No context for why it
  matters.
- Stacking multiple projects: "I did X at AT&T, and also Y at Insight Global."
  One project per answer.
- Implementation details that belong in a README: field names, config keys,
  specific method names. These are interview depth, not application answers.

### One-project rule
The answer draws on exactly one resume entry as its primary source. Supporting
context from a second entry is allowed as a single-clause reference (not a
parallel story). If the question implies a specific domain, pick the entry
that best matches — do not default to the project with the strongest metrics.
