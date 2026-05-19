# Cover Letter Rules

These rules are injected into the Claude system prompt when generating cover letters.
Be specific — vague rules produce generic output.

## Format

- Single page — hard requirement
- Sections use headings drawn from the JD's own language
- 3-4 body sections plus a locked visa/work-authorization section (added automatically)
- Total body word count: 400-450 words (opening + sections + closing)

## Voice and Tone

- [Describe your preferred tone: e.g. "direct and technical, not corporate"]
- [Any phrases or constructions to avoid: e.g. "never use 'passionate about'"]
- Em dashes (—) throughout, not double hyphens (--)
- No degree gap mention
- No forced company admiration — genuine problem-domain interest only in opening

## Opening Paragraph

- Hook: what drew you to this specific role or problem domain
- 2-3 sentences maximum
- Reference the technical challenge, not the company brand

## Section Structure

Each section heading should mirror the JD's language. Claude will select headings
that match the job posting's emphasis. Typical sections include:

- **[Technical domain section]** — e.g. "Streaming Infrastructure", "Data Platform"
  Lead with the most relevant project(s). Include one specific metric.

- **[Leadership or collaboration section]** — e.g. "Technical Leadership", "Cross-team Influence"
  Describe scope of influence: team, org, or company-wide.

- **[Secondary technical section]** — e.g. "Observability", "Migration Experience"
  Supporting evidence that addresses a secondary JD requirement.

## Project Selection Guide

When selecting which projects to reference, prefer:
1. Projects that directly match the JD's primary technical domain
2. Projects with specific, verifiable metrics
3. Projects that demonstrate the seniority level the JD requires

Projects available (update with your own):
- [Project A] — [one-line description of what it demonstrates]
- [Project B] — [one-line description]
- [Project C] — [one-line description]

## Closing Paragraph

- Express readiness to discuss further
- 1-2 sentences
- No "I look forward to hearing from you" clichés

## What to Never Include

- [List anything specific to avoid: e.g. "never mention [X technology] — not on resume"]
- Fabricated experience or stack claims
- Degree or education references
- Salary expectations

## Locked Visa / Work Authorization Paragraphs

Define one `### <Country>` subsection per situation. The subsection heading must match
one of the country names recognized by `scripts/generate_cl.js:COUNTRY_NAME_TO_CODE`
(`Canada`, `Ireland`, `United Kingdom` / `UK`). The body is one paragraph; soft-wrap as
you like — it will be re-flowed into a single line.

If this section is absent, or no subsection matches the job's country, the Work
Authorization section is omitted from the letter entirely. Use `[Company Name]` as a
placeholder if your paragraph needs to reference the employer — it will be substituted
automatically.

### Canada

I am a [nationality] relocating to Canada. I will require a work permit and expect
to qualify under the Global Talent Stream (Category A), which carries a two-week
processing target. I am ready to provide all required documentation.

### Ireland

I am a [nationality]. I will require visa sponsorship to work in Ireland. Based on
my profile, I expect to qualify for the Critical Skills Employment Permit. I am ready
to work with [Company Name] to complete any required documentation efficiently.

### United Kingdom

I am a [nationality]. I will require a Skilled Worker visa to work in the United
Kingdom. I meet the eligibility requirements and am prepared to support [Company Name]
through the sponsorship process.

<!-- Add, remove, or modify subsections to match your situation. To support a new
     country, add the name to COUNTRY_NAME_TO_CODE in scripts/generate_cl.js. Remove
     this entire section if you are a local candidate who needs no sponsorship. -->
