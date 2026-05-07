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
const CL_MODEL   = "claude-sonnet-4-5-20250929";  // Sonnet for cover letters
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
  return new Date().toISOString().split("T")[0];
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

function parseVisaParagraphs(rulesText) {
  const match = rulesText.match(/##\s+Work Authorization Paragraphs\s*\n([\s\S]*?)(?=\n##\s|$)/);
  if (!match) return {};
  const entries = {};
  const paragraphs = match[1].split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
  for (const para of paragraphs) {
    const m = para.match(/^([A-Z]{2,4}):\s*([\s\S]+)$/);
    if (m) entries[m[1]] = m[2].replace(/\s+/g, " ").trim();
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
  "date": "<today's date as Month DD, YYYY>",
  "re_line": "<job title for Re: line>",
  "opening": "<opening paragraph — hook + genuine technical interest in problem domain>",
  "sections": [
    {
      "heading": "<section heading in JD's own language>",
      "body": "<section paragraph>"
    }
  ],
  "closing": "<closing paragraph before Sincerely>"
}

Rules:
- sections array must have 3-4 entries
- Do NOT include a Work Authorization section in the sections array — it will be appended automatically if configured in your profile
- Use em dashes (—) not double hyphens (--)
- Never fabricate experience
- Map section headings to JD's own language
- Select 2-3 projects that best match JD emphasis per Project Selection Guide
- Do not mention degree gap
- No forced company admiration — problem-domain interest only in opening
- Total word count for opening + all section bodies + closing combined must be 400-450 words. Be concise — cut padding, not substance. Single page is a hard requirement.`;
}

// ── docx assembly ─────────────────────────────────────────────────────────────

async function buildDocx(content, outputPath, visaText = null) {
  const {
    Document, Packer, Paragraph, TextRun, BorderStyle,
    AlignmentType, HeadingLevel, WidthType,
  } = require("docx");

  // Colors per spec
  const NAVY = "1F3864";
  const BLUE = "2E75B6";

  // Font sizes (half-points: spec says 20pt renders as 10pt body, 34pt for name)
  const BODY_SIZE = 20;   // 10pt
  const NAME_SIZE = 34;   // 17pt
  const HEAD_SIZE = 20;   // 10pt — same as body, color differentiates

  // Section heading with blue color and bottom border rule
  function sectionHeading(text) {
    return new Paragraph({
      spacing: { before: 120, after: 40 },
      border: {
        bottom: { style: BorderStyle.SINGLE, size: 6, color: BLUE, space: 1 },
      },
      children: [
        new TextRun({
          text,
          font: "Calibri",
          size: HEAD_SIZE,
          color: BLUE,
          bold: true,
        }),
      ],
    });
  }

  // Body paragraph
  function bodyPara(text, opts = {}) {
    return new Paragraph({
      spacing: { before: 80, after: 80 },
      children: [
        new TextRun({
          text,
          font: "Calibri",
          size: BODY_SIZE,
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

  // ── Header: Name ──────────────────────────────────────────────────────────
  children.push(new Paragraph({
    spacing: { before: 0, after: 0 },
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

  // ── Header: Contact line ──────────────────────────────────────────────────
  children.push(new Paragraph({
    spacing: { before: 0, after: 120 },
    children: [
      new TextRun({
        text: "+1 210 980 2220  |  blantonjohnny3@gmail.com  |  linkedin.com/in/johnny-blanton",
        font: "Calibri",
        size: BODY_SIZE,
      }),
    ],
  }));

  // ── Date ─────────────────────────────────────────────────────────────────
  children.push(bodyPara(content.date || todayLong()));
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

  // ── Body sections ─────────────────────────────────────────────────────────
  for (const section of content.sections) {
    children.push(sectionHeading(section.heading));
    children.push(bodyPara(section.body));
  }

  // ── Visa section (optional) ───────────────────────────────────────────────
  if (visaText) {
    children.push(sectionHeading("Work Authorization"));
    children.push(bodyPara(visaText));
  }

  // ── Closing ───────────────────────────────────────────────────────────────
  children.push(spacer());
  children.push(bodyPara(content.closing));
  children.push(spacer());
  children.push(bodyPara("Sincerely,"));
  children.push(spacer());
  children.push(bodyPara("Johnny Ray Blanton III", { bold: true }));

  const doc = new Document({
    sections: [{
      properties: {
        page: {
          size:   { width: 12240, height: 15840 },  // US Letter
          margin: { top: 900, bottom: 900, left: 1260, right: 1260 },
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
  let country = countryArg || null;
  if (!country) {
    const loc = (job.location || "").toLowerCase();
    if (loc.includes("ireland") || loc.includes(" ie")) country = "IE";
    else if (loc.includes("canada") || loc.includes(" ca")) country = "CA";
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
  let content;
  try {
    let cleaned = raw.trim();
    if (cleaned.includes("```json")) cleaned = cleaned.split("```json")[1].split("```")[0].trim();
    else if (cleaned.includes("```")) cleaned = cleaned.split("```")[1].split("```")[0].trim();
    else if (cleaned.includes("{")) cleaned = cleaned.slice(cleaned.indexOf("{"), cleaned.lastIndexOf("}") + 1);
    content = JSON.parse(cleaned);
  } catch (e) {
    console.error("Error: Claude returned non-JSON response:\n", raw);
    process.exit(1);
  }

  content.company_name = job.company_name;

  // ── Build docx ────────────────────────────────────────────────────────────
  const version    = (job.cover_letter_version || 0) + 1;
  const filename   = `${slugify(job.company_name)}_${slugify(job.title)}_CoverLetter_v${version}.docx`;
  const outputPath = path.join(OUTPUT_DIR, filename);

  console.log("  Assembling .docx...");
  await buildDocx(content, outputPath, visaText);
  console.log(`  Written to: ${outputPath}`);

  // ── Update pipeline record ────────────────────────────────────────────────
  const updatedJobs = jobs.map(j => j.job_id === jobId
    ? { ...j, cover_letter_generated: true, cover_letter_version: version,
               pipeline_status: "cover_letter_ready" }
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
  const visaLine = visaText ? `  Work auth: ${country} paragraph applied` : `  Work auth: none`;
  console.log("\n── Post-generation checklist ─────────────────────────────────");
  console.log(`  File:     ${filename}`);
  console.log(`  Sections: ${content.sections.map(s => s.heading).join(", ")}`);
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
