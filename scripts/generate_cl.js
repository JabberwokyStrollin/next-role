#!/usr/bin/env node
/**
 * generate_cl.js — Cover letter generator
 *
 * Calls Claude with the JD + resume + rules to generate structured letter content,
 * then assembles a formatted .docx using the docx library per cover_letter_rules.md.
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
const RESUME_PATH    = path.join(PROFILE_DIR, "resume.md");
const CL_RULES_PATH  = path.join(PROFILE_DIR, "cover_letter_rules.md");

if (!fs.existsSync(OUTPUT_DIR)) fs.mkdirSync(OUTPUT_DIR, { recursive: true });

// ── Config ────────────────────────────────────────────────────────────────────

const API_KEY    = process.env.ANTHROPIC_API_KEY;
const CL_MODEL   = "claude-sonnet-4-6";  // Sonnet 4.6 for cover letters
const MAX_TOKENS = 4000;

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

// ── Letter content generation ─────────────────────────────────────────────────

function buildSystem(resumeText, rulesText) {
  return `You are a cover letter writer. Generate a cover letter and return ONLY valid JSON — no preamble, no markdown fences.

## Resume
${resumeText}

## Cover Letter Rules
${rulesText}

## Required JSON output format
{
  "re_line": "<job title for the Re: line>",
  "opening": "<opening paragraph>",
  "body_paragraphs": ["<body paragraph 1>", "<body paragraph 2>", "..."],
  "closing": "<closing paragraph>"
}

The Cover Letter Rules document above is authoritative — follow it. The visa/work-authorization paragraph is appended automatically server-side after the signature; do not produce it in any field. Return ONLY the JSON object — no preamble, no markdown fences.`;
}

// ── docx assembly ─────────────────────────────────────────────────────────────

async function buildDocx(content, outputPath, visaText = null) {
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

  // Body paragraph
  function bodyPara(text, opts = {}) {
    return new Paragraph({
      spacing: { before: 80, after: 80 },
      alignment: opts.alignment,
      children: [
        new TextRun({
          text,
          font: "Calibri",
          size: opts.size || BODY_SIZE,
          bold: opts.bold || false,
        }),
      ],
    });
  }

  // Empty line spacer
  function spacer() {
    return new Paragraph({
      spacing: { before: 0, after: 0 },
      children: [new TextRun({ text: "", font: "Calibri", size: BODY_SIZE })],
    });
  }

  const children = [];

  // ── Header: Name (centered) ───────────────────────────────────────────────
  children.push(new Paragraph({
    spacing: { before: 0, after: 0 },
    alignment: AlignmentType.CENTER,
    children: [
      new TextRun({
        text: "Johnny Ray Blanton III",
        font: "Calibri",
        size: NAME_SIZE,
        color: NAVY,
        bold: true,
      }),
    ],
  }));

  // ── Header: Contact line (centered, one size smaller) ────────────────────
  children.push(new Paragraph({
    spacing: { before: 0, after: 120 },
    alignment: AlignmentType.CENTER,
    children: [
      new TextRun({
        text: "+1 210 980 2220  |  blantonjohnny3@gmail.com  |  linkedin.com/in/johnny-blanton",
        font: "Calibri",
        size: CONTACT_SIZE,
      }),
    ],
  }));

  // ── Date ─────────────────────────────────────────────────────────────────
  // Always use today's actual system date — never trust Claude to provide it,
  // since model training cutoffs make hallucinated dates likely.
  children.push(bodyPara(todayLong()));
  children.push(spacer());

  // ── Salutation ────────────────────────────────────────────────────────────
  children.push(bodyPara("Hiring Manager"));
  children.push(spacer());

  // ── Re: line ─────────────────────────────────────────────────────────────
  children.push(new Paragraph({
    spacing: { before: 0, after: 80 },
    children: [
      new TextRun({
        text: `Re: ${content.re_line}`,
        font: "Calibri",
        size: BODY_SIZE,
        bold: true,
      }),
    ],
  }));
  children.push(spacer());

  // ── Opening paragraph ─────────────────────────────────────────────────────
  children.push(bodyPara(content.opening));

  // ── Body paragraphs (no headings — prose only) ───────────────────────────
  for (const para of content.body_paragraphs) {
    children.push(bodyPara(para));
  }

  // ── Closing ───────────────────────────────────────────────────────────────
  children.push(bodyPara(content.closing));
  children.push(spacer());
  children.push(bodyPara("Sincerely,"));
  children.push(spacer());
  children.push(bodyPara("Johnny Ray Blanton III", { bold: true }));

  // ── Visa paragraph (after signature, prefixed "Note:") ────────────────────
  if (visaText) {
    children.push(spacer());
    children.push(bodyPara(`Note: ${visaText}`));
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

  // ── Load context files ───────────────────────────────────────────────────
  if (!fs.existsSync(RESUME_PATH)) {
    console.error(`Error: resume not found at ${RESUME_PATH}\nCopy profile.example/ to profile/ and fill in your details.`);
    process.exit(1);
  }
  if (!fs.existsSync(CL_RULES_PATH)) {
    console.error(`Error: cover letter rules not found at ${CL_RULES_PATH}\nCopy profile.example/ to profile/ and fill in your details.`);
    process.exit(1);
  }

  const resumeText = fs.readFileSync(RESUME_PATH, "utf8");
  const rulesText  = fs.readFileSync(CL_RULES_PATH, "utf8");

  // ── Resolve visa paragraph ───────────────────────────────────────────────
  const visaParagraphs = parseVisaParagraphs(rulesText);
  let visaText = country ? (visaParagraphs[country] || null) : null;
  if (visaText) {
    visaText = visaText.replace(/\[Company Name\]/g, job.company_name);
    console.log(`  Work authorization: ${country} paragraph applied`);
  } else if (country && Object.keys(visaParagraphs).length > 0) {
    console.log(`  Note: no Work Authorization entry for "${country}" in profile — section omitted`);
  } else {
    console.log(`  Work authorization: none (section omitted)`);
  }

  // ── Call Claude ──────────────────────────────────────────────────────────
  console.log("  Calling Claude to generate letter content...");
  const system  = buildSystem(resumeText, rulesText);
  const userMsg = `Generate a cover letter for this job.\n\nCompany: ${job.company_name}\nTitle: ${job.title}\nLocation: ${job.location}\n\nJob Description:\n${job.jd_text}`;

  const raw = await callClaude(system, userMsg);

  // ── Parse JSON response ───────────────────────────────────────────────────
  function parseLetterJson(text) {
    let cleaned = text.trim();
    if (cleaned.includes("```json")) cleaned = cleaned.split("```json")[1].split("```")[0].trim();
    else if (cleaned.includes("```")) cleaned = cleaned.split("```")[1].split("```")[0].trim();
    else if (cleaned.includes("{")) cleaned = cleaned.slice(cleaned.indexOf("{"), cleaned.lastIndexOf("}") + 1);
    return JSON.parse(cleaned);
  }

  let content;
  try {
    content = parseLetterJson(raw);
  } catch (e) {
    console.error("Error: Claude returned non-JSON response:\n", raw);
    process.exit(1);
  }

  // ── Word-count enforcement (auto-retry once if over cap) ─────────────────
  function countLetterWords(c) {
    const all = [c.opening, ...c.body_paragraphs, c.closing].join(" ");
    return all.trim().split(/\s+/).filter(Boolean).length;
  }

  const WORD_CAP = 380;
  const initialCount = countLetterWords(content);
  if (initialCount > WORD_CAP) {
    console.log(`  Draft is ${initialCount} words (cap ${WORD_CAP}). Asking Claude to trim...`);
    const trimSystem = `You are trimming a cover letter to fit a hard word cap.

## Hard rules
- Opening + all body paragraph texts + closing combined MUST be ≤ ${WORD_CAP} words.
- body_paragraphs MUST have exactly 2 or 3 entries. Never 4. If the input has more than 3, drop entries.
- The entire body MUST name AT MOST 3 distinct projects total (across all paragraphs combined). A "named project" is one referenced by name (e.g. "HALOC Distilled", "Jailer", "YAML ingestion framework", "HALOC Flink Distilled", "MASS/GPC", "next-role") or by a description that resolves unambiguously to one project. If the input names more than 3, drop the weakest one entirely — do not just compress it. A single paragraph that gives sentence-level treatment to 3 different projects is a violation even if the paragraph count is within the cap.
- Do NOT add new content. Only cut.
- Do NOT introduce any visa, sponsorship, or work-authorization content anywhere — that paragraph is appended automatically server-side and must not appear in opening, body, or closing.
- Preserve narrative arc (problem → decision → outcome). Cut whole sentences, not adjectives.
- Drop a body paragraph entirely (remove that array element) if needed to fit; do not pad the remaining paragraphs.
- Return the SAME JSON shape as input: { re_line, opening, body_paragraphs: [string, ...], closing }.

Return ONLY valid JSON — no preamble, no markdown fences.`;
    const trimUserMsg = `Trim this letter to ≤ ${WORD_CAP} words. Current count: ${initialCount}.\n\n${JSON.stringify(content, null, 2)}`;
    const trimRaw = await callClaude(trimSystem, trimUserMsg);
    try {
      const trimmed = parseLetterJson(trimRaw);
      const trimmedCount = countLetterWords(trimmed);
      if (trimmedCount <= WORD_CAP) {
        console.log(`  Trimmed to ${trimmedCount} words.`);
        content = trimmed;
      } else {
        console.log(`  Trim attempt landed at ${trimmedCount} words — still over cap. Using trimmed version anyway.`);
        content = trimmed;
      }
    } catch (e) {
      console.error("  Warning: trim retry returned non-JSON; keeping original draft.");
    }
  }

  content.company_name = job.company_name;

  // ── Build docx ────────────────────────────────────────────────────────────
  // Filename: YYYY-MM-DD_Company_Title.docx; same-day regen → _v2, _v3, ...
  // Date prefix prevents collisions across different jobs at the same company,
  // and gives same-day regenerations a stable disambiguator.
  const version    = (job.cover_letter_version || 0) + 1;
  const dateStr    = todayISO();
  const slug       = `${slugify(job.company_name)}_${slugify(job.title)}`;
  let filename     = `${dateStr}_${slug}.docx`;
  let collisionN   = 1;
  while (fs.existsSync(path.join(OUTPUT_DIR, filename))) {
    collisionN += 1;
    filename = `${dateStr}_${slug}_v${collisionN}.docx`;
  }
  const outputPath = path.join(OUTPUT_DIR, filename);
  // POSIX-style relative path; serve.py converts to OS-native when opening.
  const relPath    = `output/${filename}`;

  console.log("  Assembling .docx...");
  await buildDocx(content, outputPath, visaText);
  console.log(`  Written to: ${outputPath}`);

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
  const allText  = [content.opening, ...content.body_paragraphs, content.closing].join(" ");
  const realWords = allText.trim().split(/\s+/).filter(Boolean).length;
  const overCap  = realWords > WORD_CAP ? ` ⚠ OVER ${WORD_CAP}-WORD CAP` : "";

  const paraCount = content.body_paragraphs.length;
  const overParas = paraCount > 3 ? ` ⚠ OVER 3-PARAGRAPH CAP` : (paraCount < 2 ? ` ⚠ UNDER 2-PARAGRAPH MIN` : "");

  const visaLine = visaText ? `  Work auth: ${country} paragraph applied (after signature, "Note:" prefix)` : `  Work auth: none`;
  console.log("\n── Post-generation checklist ─────────────────────────────────");
  console.log(`  File:         ${filename}`);
  console.log(`  Word count:   ${realWords}${overCap}`);
  console.log(`  Body paras:   ${paraCount}${overParas}`);
  console.log(visaLine);
  console.log("\n  Before submitting, verify:");
  console.log("  [ ] All project names and metrics are accurate");
  console.log("  [ ] No fabricated stack claims (e.g. AWS vs Azure)");
  console.log("  [ ] Em dashes (—) used throughout, not double hyphens (--)");
  console.log("  [ ] Single page when opened in Word");
  if (visaText) console.log("  [ ] Company name correct in work authorization paragraph");
}

main().catch(err => {
  console.error("Fatal error:", err);
  process.exit(1);
});
