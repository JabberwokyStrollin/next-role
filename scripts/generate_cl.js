#!/usr/bin/env node
/**
 * generate_cl.js — Cover letter generator
 *
 * Fills profile/cover_letter_template.txt — a fixed, operator-written letter —
 * and assembles a formatted .docx. Only the template's <...> placeholders vary
 * per job: <Date>, <Job Title>, <Company> are filled mechanically; every other
 * placeholder is written by one small Claude call from the JD. The locked visa
 * paragraph still comes from profile/cover_letter_rules.md.
 *
 * Usage:
 *   node scripts/generate_cl.js --job-id <uuid>
 *   node scripts/generate_cl.js --job-id <uuid> --country IE
 *
 * Output: output/<CompanyName>_<Title>_CoverLetter_v<N>.docx
 */

const fs   = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

// ── Paths ─────────────────────────────────────────────────────────────────────

const ROOT          = path.resolve(__dirname, "..");
const DATA_DIR      = path.join(ROOT, "data");
const OUTPUT_DIR    = path.join(ROOT, "output");
const PROFILE_DIR    = path.join(ROOT, "profile");

const PIPELINE_PATH  = path.join(DATA_DIR, "job_pipeline.json");
const REGISTRY_PATH  = path.join(DATA_DIR, "company_registry.json");
const LOG_PATH       = path.join(DATA_DIR, "process_log.json");
const CL_TEMPLATE_PATH = path.join(PROFILE_DIR, "cover_letter_template.txt");
const CL_RULES_PATH  = path.join(PROFILE_DIR, "cover_letter_rules.md");

if (!fs.existsSync(OUTPUT_DIR)) fs.mkdirSync(OUTPUT_DIR, { recursive: true });

// ── Config ────────────────────────────────────────────────────────────────────

const API_KEY    = process.env.ANTHROPIC_API_KEY;
const CL_MODEL   = "claude-sonnet-4-6";  // Sonnet 4.6 for cover letters
const MAX_TOKENS = 1000;  // fills are a sentence or two each

if (!API_KEY) {
  console.error("Error: ANTHROPIC_API_KEY environment variable is not set.");
  process.exit(1);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function loadJson(p) {
  if (!fs.existsSync(p)) return [];
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

function saveJson(p, data) {
  fs.writeFileSync(p, JSON.stringify(data, null, 2), "utf8");
}

function todayISO() {
  // Build the date string from local-timezone components so it stays in sync
  // with todayLong() (which is also local). toISOString() uses UTC, which can
  // disagree with local time around midnight and produce inconsistent dates
  // between the filename and the letter body.
  const d = new Date();
  const y  = d.getFullYear();
  const m  = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

function todayLong() {
  return new Date().toLocaleDateString("en-US", {
    year: "numeric", month: "long", day: "numeric"
  });
}

function uuidv4() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function appendLog(entry) {
  const log = loadJson(LOG_PATH);
  log.push({ log_id: uuidv4(), timestamp: new Date().toISOString(),
             session_date: todayISO(), ...entry });
  saveJson(LOG_PATH, log);
}

function slugify(str) {
  return str.replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_|_$/g, "");
}

// ── Claude API call ───────────────────────────────────────────────────────────

async function callClaude(system, userMessage) {
  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type":      "application/json",
      "x-api-key":         API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model:      CL_MODEL,
      max_tokens: MAX_TOKENS,
      system,
      messages: [{ role: "user", content: userMessage }],
    }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Claude API error ${resp.status}: ${err}`);
  }

  const data = await resp.json();
  console.log(`  Tokens — in: ${data.usage.input_tokens}  out: ${data.usage.output_tokens}`);
  return data.content.map(b => b.type === "text" ? b.text : "").join("");
}

// ── Visa paragraph parser ─────────────────────────────────────────────────────

// Map subsection heading text to country codes. Add entries as needed when
// new countries are added to the rules file.
// US is intentionally absent: the operator is a US citizen, so US roles get no
// work-authorization paragraph (a US-derived job resolves to no country here).
const COUNTRY_NAME_TO_CODE = {
  canada:          "CA",
  ireland:         "IE",
  "united kingdom": "UK",
  uk:              "UK",
};

function parseVisaParagraphs(rulesText) {
  // Section heading: "## Locked Visa / Work Authorization Paragraphs",
  // followed by per-country "### <country>" subsections.
  const sectionMatch = rulesText.match(
    /##\s+Locked Visa\s*\/\s*Work Authorization Paragraphs\s*\n([\s\S]*?)(?=\n##\s|$)/i
  );
  if (!sectionMatch) return {};
  // Strip HTML comments and standalone Markdown horizontal rules before parsing.
  // The LAST country subsection has no following "### " to bound its capture, so
  // it otherwise runs to the end of the section and swallows any trailing
  // "<!-- ... -->" note or "---" rule that separates it from the next "## "
  // section. That leaked into Ireland letters (Ireland is the last subsection).
  const body = sectionMatch[1]
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/^[ \t]*-{3,}[ \t]*$/gm, "");
  const entries = {};

  const subRegex = /###\s+([^\n]+)\n([\s\S]*?)(?=\n###\s|$)/g;
  let m;
  while ((m = subRegex.exec(body)) !== null) {
    const heading = m[1].trim().toLowerCase();
    const code = COUNTRY_NAME_TO_CODE[heading];
    if (code) entries[code] = m[2].trim().replace(/\s+/g, " ");
  }
  return entries;
}

// ── Template ──────────────────────────────────────────────────────────────────

// The letter is a fixed, operator-written text. Only <...> placeholders change
// per job. Placeholders named here are filled mechanically from the job record;
// any OTHER placeholder is an instruction to Claude, and its inner text is the
// brief for what to write there. Keys are matched case-insensitively.
const PLACEHOLDER_RE = /<([^<>\n]+)>/g;

function staticPlaceholderValues(job) {
  const company = job.company_name || "";
  const title   = cleanTitle(job.title || "");
  return {
    "date":         todayLong(),
    "job title":    title,
    "title":        title,
    "company":      company,
    "company name": company,
  };
}

// Job boards decorate titles with work model / location / requisition IDs
// ("Senior Data Engineer (Remote - US)", "Staff Engineer - Remote"). The old
// Claude-written Re: line silently dropped those; the template fills
// <Job Title> mechanically, so strip them here.
function cleanTitle(title) {
  const LOC = /(remote|hybrid|on-?site|united states|usa|\bus\b|ireland|dublin|canada|anywhere)/i;
  let t = title.trim();
  // Trailing parenthetical / bracket naming a location, work model, or req ID.
  t = t.replace(/\s*[\(\[]([^\)\]]*)[\)\]]\s*$/, (m, inner) =>
    (LOC.test(inner) || /^[A-Z]{0,4}-?\d{3,}$/i.test(inner.trim())) ? "" : m);
  // Trailing ", Remote", then " - Remote" / " | Remote" (comma first, so
  // "Engineer - Data, Remote" keeps "Data").
  t = t.replace(/,\s*([^,]*)$/, (m, tail) => LOC.test(tail) ? "" : m);
  t = t.replace(/\s+[-–—|]\s+([^-–—|]*)$/, (m, tail) => LOC.test(tail) ? "" : m);
  // Em/en dashes in the title read as an AI tell in the letter — use a plain hyphen.
  return (t.trim() || title.trim()).replace(/[—–]/g, "-");
}

// Returns { lines, slots } — lines are the raw template lines; slots are the
// Claude-filled placeholders in order of appearance: { id, raw, brief }.
// A placeholder repeated verbatim is one slot, filled once.
function parseTemplate(text, staticValues) {
  const lines = text.replace(/\r\n/g, "\n").replace(/\s+$/, "").split("\n");
  const slots = [];
  const seen  = new Set();
  for (const line of lines) {
    for (const m of line.matchAll(PLACEHOLDER_RE)) {
      if (m[1].trim().toLowerCase() in staticValues || seen.has(m[0])) continue;
      seen.add(m[0]);
      slots.push({ id: String(slots.length + 1), raw: m[0], brief: m[1].trim() });
    }
  }
  return { lines, slots };
}

// Substitutes static values and per-slot text (keyed by slot id) into the lines.
function renderTemplate(lines, staticValues, slots, fills) {
  const byRaw = new Map(slots.map(s => [s.raw, fills[s.id]]));
  return lines.map(line => line.replace(PLACEHOLDER_RE, (m, inner) => {
    const key = inner.trim().toLowerCase();
    if (key in staticValues) return staticValues[key];
    return byRaw.get(m) || m;
  }));
}

// ── AI-tell screen ────────────────────────────────────────────────────────────

// Deterministic, so the rule holds no matter what the model decides. A fill
// that trips any of these gets one retry with the violations named; if it
// still trips, the letter is written and the checklist flags it.
const AI_TELL_PATTERNS = [
  [/[—–]/,                                                          "em/en dash"],
  [/;/,                                                             "semicolon"],
  [/!/,                                                             "exclamation mark"],
  [/[<>]/,                                                          "angle bracket"],
  [/\bdelv(e|es|ing)\b/i,                                           "\"delve\""],
  [/\bpassion(ate)?\b/i,                                            "\"passion\""],
  [/\b(thrilled|excited|eager)\b/i,                                 "excited/thrilled/eager"],
  [/\bleverag(e|es|ed|ing)\b/i,                                     "\"leverage\""],
  [/\b(robust|seamless(ly)?|cutting-edge|state-of-the-art|world-class)\b/i, "buzzword adjective"],
  [/\b(innovative|transformative|game-?chang\w*)\b/i,               "hype adjective"],
  [/\b(landscape|tapestry|realm|journey|ecosystem)\b/i,             "filler noun"],
  [/\b(ever-evolving|fast-paced|dynamic)\b/i,                       "filler adjective"],
  [/\b(spearhead\w*|empower\w*|elevat\w*|foster\w*|harness\w*|unlock\w*)\b/i, "inflated verb"],
  [/\b(synerg\w*|testament|pivotal|unwavering|paramount)\b/i,       "inflated word"],
  [/\bnot (just|only|merely)\b/i,                                   "\"not just X\" construction"],
  [/\b(truly|deeply|incredibly)\b/i,                                "intensifier"],
];

function findTells(text) {
  return AI_TELL_PATTERNS.filter(([re]) => re.test(text)).map(([, label]) => label);
}

// ── Slot filling (the only Claude call) ───────────────────────────────────────

function buildFillSystem() {
  return `You fill in a few blanks in a cover letter the candidate already wrote. Every word outside the blanks is fixed and you never change it. Return ONLY valid JSON — no preamble, no markdown fences.

## How to write a fill
- Each blank sits inside a sentence. Your text must continue that sentence grammatically from the words right before the blank and lead cleanly into the words right after it. If the blank ends its sentence, end with a period. If more text follows the blank on the same line, do not add a period.
- Follow the blank's brief. A brief may include an example in parentheses. It shows the SHAPE wanted (length, specificity, how it ties to the JD). Never reuse its wording, company, or team name unless this job description says the same thing.
- Be concrete and specific to THIS job description: name the actual team, product, system, or problem it describes. Paraphrase in plain words. Do not lift marketing copy.
- Match the candidate's voice in the surrounding letter: plain, direct, conversational, the way an experienced engineer talks. Short and specific beats impressive.
- Make no claims about the candidate's experience, skills, or results. The rest of the letter covers that. You only describe the role and company side.
- No flattery of the company, no hype, no superlatives.
- Never use: em or en dashes, semicolons, exclamation marks, "delve", "passionate", "excited", "thrilled", "eager", "leverage", "robust", "seamless", "cutting-edge", "innovative", "transformative", "landscape", "ecosystem", "journey", "realm", "dynamic", "fast-paced", "ever-evolving", "spearhead", "empower", "elevate", "foster", "harness", "unlock", "synergy", "testament", "pivotal", "truly", "deeply", "incredibly", or the "not just X, but Y" construction.
- One sentence or clause per blank, roughly 15 to 45 words unless the brief asks otherwise.

## Output format
A JSON object mapping each blank's number (as a string) to its fill text, e.g. {"1": "..."}.`;
}

function buildFillUser(job, preview, slots, feedback = null) {
  const briefs = slots.map(s => `[[BLANK ${s.id}]] brief: ${s.brief}`).join("\n");
  let msg = `## The letter (blanks marked [[BLANK n]])
${preview}

## Blanks to fill
${briefs}

## Job
Company: ${job.company_name}
Title: ${job.title}
Location: ${job.location}

## Job description
${job.jd_text}`;
  if (feedback) msg += `\n\n## Fix these problems in your previous answer\n${feedback}`;
  return msg;
}

function parseJsonObject(text) {
  let cleaned = text.trim();
  if (cleaned.includes("```json")) cleaned = cleaned.split("```json")[1].split("```")[0].trim();
  else if (cleaned.includes("```")) cleaned = cleaned.split("```")[1].split("```")[0].trim();
  else if (cleaned.includes("{")) cleaned = cleaned.slice(cleaned.indexOf("{"), cleaned.lastIndexOf("}") + 1);
  return JSON.parse(cleaned);
}

function normalizeFill(text) {
  return String(text || "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^["'“‘]+|["'”’]+$/g, "")
    .trim();
}

// Human-readable problems (missing blanks, AI tells, overlong fills).
function checkFills(slots, fills) {
  const problems = [];
  for (const s of slots) {
    const f = fills[s.id];
    if (!f) { problems.push(`Blank ${s.id}: missing or empty.`); continue; }
    const tells = findTells(f);
    if (tells.length) problems.push(`Blank ${s.id}: contains ${tells.join(", ")}.`);
    const words = f.split(/\s+/).length;
    if (words > 60) problems.push(`Blank ${s.id}: ${words} words, too long. Keep it to one sentence or clause.`);
  }
  return problems;
}

// Returns { fills, problems }. One retry when the first answer has problems.
async function fillSlots(job, lines, staticValues, slots) {
  if (slots.length === 0) return { fills: {}, problems: [] };

  const markers = Object.fromEntries(slots.map(s => [s.id, `[[BLANK ${s.id}]]`]));
  const preview = renderTemplate(lines, staticValues, slots, markers).join("\n");
  const system  = buildFillSystem();

  let fills = {};
  let problems = [];
  for (let attempt = 1; attempt <= 2; attempt++) {
    const feedback = attempt > 1 ? problems.join("\n") : null;
    const raw = await callClaude(system, buildFillUser(job, preview, slots, feedback));
    let parsed;
    try {
      parsed = parseJsonObject(raw);
    } catch (e) {
      problems = [`Response was not valid JSON: ${raw.slice(0, 200)}`];
      continue;
    }
    fills = Object.fromEntries(slots.map(s => [s.id, normalizeFill(parsed[s.id])]));
    problems = checkFills(slots, fills);
    if (problems.length === 0) break;
    if (attempt === 1) console.log(`  Fill needs another pass: ${problems.join(" ")}`);
  }
  return { fills, problems };
}

// ── docx assembly ─────────────────────────────────────────────────────────────

// Renders the filled template line by line, so the .docx mirrors the template
// file: each non-empty line is a paragraph, each blank line a spacer. Styling
// is positional — line 1 is the name header, line 2 the contact line, a line
// starting "Re:" is bold, and the last line (the signature) is bold.
async function buildDocx(lines, outputPath, visaText = null) {
  const {
    Document, Packer, Paragraph, TextRun,
    AlignmentType,
  } = require("docx");

  // Colors per spec
  const NAVY = "1F3864";

  // Font sizes (half-points: docx uses half-points so multiply pt by 2)
  const BODY_SIZE    = 21;  // 10.5pt
  const CONTACT_SIZE = 19;  // 9.5pt — one size smaller than body
  const NAME_SIZE    = 28;  // 14pt

  function para(text, { size = BODY_SIZE, bold = false, color, alignment,
                        before = 80, after = 80 } = {}) {
    return new Paragraph({
      spacing: { before, after },
      alignment,
      children: [new TextRun({ text, font: "Calibri", size, bold, color })],
    });
  }

  // Empty line spacer
  function spacer() {
    return new Paragraph({
      spacing: { before: 0, after: 0 },
      children: [new TextRun({ text: "", font: "Calibri", size: BODY_SIZE })],
    });
  }

  let lastIdx = lines.length - 1;
  while (lastIdx >= 0 && !lines[lastIdx].trim()) lastIdx--;

  const children = [];
  lines.forEach((rawLine, i) => {
    const line = rawLine.trim();
    if (!line) children.push(spacer());
    else if (i === 0) children.push(para(line, {
      size: NAME_SIZE, bold: true, color: NAVY,
      alignment: AlignmentType.CENTER, before: 0, after: 0 }));
    else if (i === 1) children.push(para(line, {
      size: CONTACT_SIZE, alignment: AlignmentType.CENTER, before: 0, after: 120 }));
    else if (/^Re:/i.test(line)) children.push(para(line, { bold: true, before: 0, after: 80 }));
    else children.push(para(line, { bold: i === lastIdx }));
  });

  // ── Visa paragraph (after signature, prefixed "Note:") ────────────────────
  if (visaText) {
    children.push(spacer());
    children.push(para(`Note: ${visaText}`));
  }

  const doc = new Document({
    sections: [{
      properties: {
        page: {
          size:   { width: 12240, height: 15840 },  // US Letter
          margin: { top: 1080, bottom: 1080, left: 1260, right: 1260 },
        },
      },
      children,
    }],
  });

  const buffer = await Packer.toBuffer(doc);
  fs.writeFileSync(outputPath, buffer);
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  // ── Parse args ──────────────────────────────────────────────────────────
  const args     = process.argv.slice(2);
  const jobIdIdx = args.indexOf("--job-id");
  const countryIdx = args.indexOf("--country");

  if (jobIdIdx === -1 || !args[jobIdIdx + 1]) {
    console.error("Usage: node scripts/generate_cl.js --job-id <uuid> [--country CA|IE]");
    process.exit(1);
  }

  const jobId       = args[jobIdIdx + 1];
  const countryArg  = countryIdx !== -1 ? args[countryIdx + 1] : null;

  // ── Load job record ──────────────────────────────────────────────────────
  const jobs = loadJson(PIPELINE_PATH);
  const job  = jobs.find(j => j.job_id === jobId);
  if (!job) {
    console.error(`Error: job ID ${jobId} not found in pipeline.`);
    process.exit(1);
  }

  // ── Derive country from location ─────────────────────────────────────────
  // Single source of truth: the canonical path is the --country flag, passed by
  // run.py and serve.py from config.derive_country. When it's absent (direct
  // `node generate_cl.js` calls), delegate to the SAME Python derivation via
  // scripts/geography.py rather than re-deriving in JS — that parallel copy is
  // what kept drifting (California, Galway, Toronto...). geography.py is
  // dependency-free (no API key). CA/IE get a visa paragraph; US/OTHER get none.
  let country = countryArg || null;
  if (!country) {
    try {
      const out = execFileSync(
        "python", [path.join(__dirname, "geography.py"), job.location || ""],
        { encoding: "utf-8" }
      ).trim();
      country = (out === "CA" || out === "IE") ? out : null;
    } catch (e) {
      console.log(`  [warn] country derivation failed (${e.message}); no visa paragraph applied`);
      country = null;
    }
  }

  console.log(`\nGenerating cover letter for: ${job.company_name} — ${job.title}`);

  // ── Load template ────────────────────────────────────────────────────────
  if (!fs.existsSync(CL_TEMPLATE_PATH)) {
    console.error(`Error: cover letter template not found at ${CL_TEMPLATE_PATH}\nCopy profile.example/ to profile/ and fill in your details.`);
    process.exit(1);
  }
  const templateText = fs.readFileSync(CL_TEMPLATE_PATH, "utf8");
  const staticValues = staticPlaceholderValues(job);
  const { lines, slots } = parseTemplate(templateText, staticValues);

  // ── Resolve visa paragraph ───────────────────────────────────────────────
  // The locked per-country paragraphs still live in cover_letter_rules.md; the
  // file is only needed when a country paragraph is actually wanted.
  let visaText = null;
  if (country) {
    const rulesText = fs.existsSync(CL_RULES_PATH) ? fs.readFileSync(CL_RULES_PATH, "utf8") : "";
    visaText = parseVisaParagraphs(rulesText)[country] || null;
    if (visaText) {
      visaText = visaText.replace(/\[Company Name\]/g, job.company_name);
      console.log(`  Work authorization: ${country} paragraph applied`);
    } else {
      console.log(`  Note: no Work Authorization entry for "${country}" in ${CL_RULES_PATH} — section omitted`);
    }
  } else {
    console.log(`  Work authorization: none (section omitted)`);
  }

  // ── Fill the Claude placeholders ─────────────────────────────────────────
  console.log(slots.length
    ? `  Filling ${slots.length} template placeholder(s) with Claude...`
    : "  No Claude placeholders in the template; no API call.");
  const { fills, problems } = await fillSlots(job, lines, staticValues, slots);
  const missing = slots.filter(s => !fills[s.id]);
  if (missing.length) {
    console.error(`Error: Claude did not fill placeholder(s): ${missing.map(s => s.raw).join(", ")}`);
    if (problems.length) console.error(`  ${problems.join("\n  ")}`);
    process.exit(1);
  }

  const filledLines = renderTemplate(lines, staticValues, slots, fills);

  // ── Build docx ────────────────────────────────────────────────────────────
  // Filename: YYYY-MM-DD_Company_Title.docx. A regeneration overwrites the
  // job's existing file in place: the employer sees the filename, and a
  // "_v2"/"_v3" suffix reads as a recycled letter. Only a first generation
  // picks a new name; a clash with ANOTHER job's file (a same-day re-listing
  // of the same title) gets a bare "_2", "_3", ... so neither is clobbered.
  const version    = (job.cover_letter_version || 0) + 1;
  const prevName   = /^output\/[^/\\]+\.docx$/.test(job.cover_letter_path || "")
    ? job.cover_letter_path.slice("output/".length) : null;
  let filename;
  if (prevName) {
    filename = prevName;
  } else {
    const dateStr  = todayISO();
    const slug     = `${slugify(job.company_name)}_${slugify(job.title)}`;
    filename       = `${dateStr}_${slug}.docx`;
    let collisionN = 1;
    while (fs.existsSync(path.join(OUTPUT_DIR, filename))) {
      collisionN += 1;
      filename = `${dateStr}_${slug}_${collisionN}.docx`;
    }
  }
  const outputPath = path.join(OUTPUT_DIR, filename);
  // POSIX-style relative path; serve.py converts to OS-native when opening.
  const relPath    = `output/${filename}`;

  console.log("  Assembling .docx...");
  try {
    await buildDocx(filledLines, outputPath, visaText);
  } catch (e) {
    // Windows refuses to overwrite a .docx that Word has open.
    if (e.code === "EBUSY" || e.code === "EPERM") {
      console.error(`Error: ${filename} is open in another program. Close it in Word and regenerate.`);
      process.exit(1);
    }
    throw e;
  }
  console.log(`  ${prevName ? "Overwrote" : "Written to"}: ${outputPath}`);

  // ── Update pipeline record ────────────────────────────────────────────────
  const updatedJobs = jobs.map(j => j.job_id === jobId
    ? { ...j, cover_letter_generated: true, cover_letter_version: version,
               cover_letter_path: relPath, pipeline_status: "cover_letter_ready" }
    : j
  );
  saveJson(PIPELINE_PATH, updatedJobs);

  appendLog({
    event_type:  "cover_letter_generated",
    entity_type: "job",
    entity_id:   jobId,
    entity_name: `${job.company_name} — ${job.title}`,
    detail:      `Cover letter v${version} generated → ${filename}`,
  });

  // ── Post-generation summary ───────────────────────────────────────────────
  const visaLine = visaText ? `  Work auth: ${country} paragraph applied (after signature, "Note:" prefix)` : `  Work auth: none`;
  console.log("\n── Post-generation checklist ─────────────────────────────────");
  console.log(`  File:         ${filename}`);
  console.log(`  Re: line:     ${staticValues["job title"]}`);
  for (const s of slots) console.log(`  Fill ${s.id}:       ${fills[s.id]}`);
  if (problems.length) console.log(`  ⚠ Fill still flagged after retry: ${problems.join(" ")}`);
  console.log(visaLine);
  console.log("\n  Before submitting, verify:");
  console.log("  [ ] The filled-in text reads naturally and is accurate to the JD");
  console.log("  [ ] Single page when opened in Word");
  if (visaText) console.log("  [ ] Company name correct in work authorization paragraph");
}

// Exported for offline checks (no API call); main() only runs from the CLI.
module.exports = { cleanTitle, parseTemplate, renderTemplate, findTells, staticPlaceholderValues, buildDocx };

if (require.main === module) {
  main().catch(err => {
    console.error("Fatal error:", err);
    process.exit(1);
  });
}
