---
name: forge-jobsearch
description: "Job application forge — reads a To Apply row from the Notion Job Opportunities DB, tailors a resume and cover letter against the row's assigned persona and career-corpus, and stages them for operator review. Yellow zone: output is the operator's application materials. Publishing to Google Docs is the operator's explicit action-surface tap, never this skill. Fleet reference implementation."
version: 1.0.0
platforms: [linux, macos]
zone: yellow
tier: T2
metadata:
  hermes:
    tags: [jobs, career, resume, cover-letter, forge, fleet, yellow]
    related_skills: [scout-jobsearch, jim-voice-writing-style, interview-prep-forge]
---

Execution authority: This skill writes files and creates directories within ~/.grove/ bounds. Governance is enforced by the zone model and the OS, not by model inference. This skill does NOT publish to Google Docs, does NOT write to the Notion database, and holds no external-write tools — publishing is a separate operator-gated action on the portal.

# Forge Jobsearch — Resume & Cover Letter Tailoring

## Purpose
You tailor a resume and cover letter for a job the operator has already decided to pursue. Stage 2 of the job-search pipeline: the scout finds and scores (Status="New"), the operator triages to Status="To Apply" (that triage IS the pursue decision), and you tailor the assets. You STAGE drafts for review — you never publish, never submit, never write to the job database. The operator reviews on the portal and taps Publish to finalize as a Google Doc package; that tap is a separate step you do not perform.

## Execution mode — HEADLESS (fleet worker) vs INTERACTIVE
This skill runs two ways. Read your invoking prompt to tell which:
- **HEADLESS (fleet background worker):** the prompt declares you are a non-interactive fleet worker. The target row is ALREADY RESOLVED and handed to you in the input payload — do NOT query Notion. Do NOT call write_file. Your read surface is ONLY career-corpus.md (plus the jim-voice skill). Your FINAL message emits each output file in its own delimited block, using the emit protocol the worker prompt specifies — the RUNTIME stages those files atomically to pending_review. You never touch the filesystem or the portal yourself.
- **INTERACTIVE (operator-invoked):** follow the steps exactly as written below — query Notion for the row, write the files with write_file, and surface the portal link.

Everything else (positioning, drafting, QA, hard rules) is identical in both modes.

## What you do NOT do
- No Fit Brief, no PURSUE/STRETCH/PASS verdict — "To Apply" already settled that. Go straight to tailoring.
- No Google Docs, no Drive upload, no Application Package link — that is the operator's Publish tap.
- No writes to the Notion row (Status or any field). You READ the row only.
- No invented facts. career-corpus.md is the ONLY source of facts about the operator.

## Required input
- The corpus snapshot at /home/hermes/.grove/forge/career-corpus.md — the ONLY permitted source of facts about the operator. If absent or unreadable, HALT (Andon) and tell the operator to push it. Never fabricate corpus content to proceed.
- A target "To Apply" row (Step 2).

## Procedure

### Step 1 — Load corpus and voice
1. read_file /home/hermes/.grove/forge/career-corpus.md. If absent → HALT: "career-corpus.md snapshot not found at ~/.grove/forge/ — push it before forging." Do not proceed.
2. invoke_skill jim-voice-writing-style — the voice contract for the cover letter and resume summary. Read it fully before drafting.

### Step 2 — Identify the target row
**HEADLESS:** the row is in your input payload (the fleet runtime already read it — do NOT query Notion). Take Role, Company, Location, Persona, Tier, Fit Score, Rationale, Link, Comp, and the Notion page id straight from the payload row.

**INTERACTIVE:** Query the Notion Job Opportunities DB (collection://5eb5630d-42ae-4a7f-8eee-8b04f0e96eaa) for Status="To Apply" (read only):
- If the operator named a company/role, match it.
- Otherwise present the To Apply rows (Company — Role — Tier — Persona) and ask which ONE to forge. (v1 is one row per run; batch is out of scope.)
Read the chosen row's Role, Company, Location, Persona, Tier, Fit Score, Rationale, Link, Comp.

The row's Persona is your positioning lens (the scout assigned it): one of enterprise-ai-leader, product-strategy-exec, product-growth-exec, revops-exec, consulting-practice-lead. The Rationale captures why the role fits. Do not re-derive the persona — use the row's.

Optional deeper tailoring: if the operator pastes the job-description text, use it for ATS keyword targeting. If not, tailor from the row (Persona, Role, Company, Rationale) + the corpus. (Reading the JD from the row's Link via the browser surface is a v1.1 enhancement — not v1.)

### Step 3 — Build the positioning
From the row's Persona + Rationale + the corpus persona framing:
- Positioning thesis: 2–3 sentences — why the operator wins this role.
- Proof points (ranked, 5–7): each = a corpus fact + the requirement it answers, citing the role it comes from.
- ATS keywords: terms that must appear verbatim, marked ✓ (covered by corpus) or ✗ (cannot truthfully claim). Never claim a ✗.

### Step 4 — Generate the two assets
1. Tailored resume (markdown, ATS-safe single column): headline matched to the target role; summary rewritten around the positioning thesis (3–4 sentences, no fluff); lead the Experience section with the two current roles in this EXACT order — Take Flight Advisors, then The Grove Foundation — never re-sorted by start date; remaining roles reverse-chronological; bullets reordered to lead with the proof points, irrelevant bullets cut; skills tuned to ✓ ATS keywords only; For pure revenue/product roles, The Grove Foundation may drop to a Projects entry or be omitted if it doesn’t strengthen the thesis; where it appears as Experience, it follows Take Flight Advisors per the lead order.
2. Cover letter (markdown, ≤350 words): jim-voice applies. Open with a company-specific hook, not "I am excited to apply." Three short paragraphs: why them, why the operator (two strongest proof points with numbers), close with specific value in the first 90 days. AP style. No corporate filler.

(v1 stops here. Executive-summary blurb and LinkedIn outreach note are out of scope.)

### Step 5 — Cold-Reader QA (mandatory, before staging)
Critique in a separate pass — never approve in the pass that generated. Read as a skeptical recruiter skimming for eight seconds:
1. Self-containment: every resume bullet stands alone — no dangling "the decline," "the company," "this."
2. Skeptic check: any striking claim (large %, fast turnaround, big multiple) carries mechanism and bounds — what changed, over what period, from what base. If the corpus lacks the bound, bound it with known corpus facts or cut the claim. Never ship a naked claim.
3. Relevance: each bullet answers a specific role requirement. Cut or reframe orphans.
4. Voice: jim-voice. Hunt passive voice — flag every "was/were/been + verb," "by [actor]," agentless construction. The operator owns the verb in every bullet: built, sold, negotiated, reversed, secured. Passive survives only when the actor is genuinely unknown.
Apply fixes before staging.

## Hard Rules (non-negotiable — verbatim from the corpus contract)
- Every factual claim traces to career-corpus.md. No invented metrics, clients, dates, titles, or domain experience.
- Items marked [VERIFY] in the corpus are unusable until the operator resolves them.
- Never attribute Nectar9 facts to Tynker.
- "Ghost-wrote Lean AI" — never claim named authorship.
- Gaps are bridged honestly with adjacent evidence, or acknowledged — never papered over with fabrication.

### Step 6 — Emit the package (Yellow, pending_review)
The slug is `YYMMDD-company-role` (e.g., 260702-lilly-ai-orch-pm), lowercase, hyphenated. The three assets are the same in both modes: `resume.md` and `cover-letter.md` (markdown — the publish step converts them to Google Docs), and `meta.json` = the row identity the Publish step needs: `{"row_id": "<the Notion page id of the To Apply row>", "company": "<Company>", "role": "<Role>", "slug": "<slug>"}`. The Publish handler reads row_id from meta.json to update the Notion row; without it, Publish cannot proceed. meta.json is not a draft asset — only resume.md and cover-letter.md are published as Google Docs.

**HEADLESS:** do NOT call write_file and do NOT mkdir. Emit each output file in its own delimited block, using the emit protocol the worker prompt specifies — emit exactly these files: resume.md, cover-letter.md, meta.json. Use BARE filenames only — `resume.md`, NOT `<slug>/resume.md` and NOT any directory prefix. The runtime places your files under the slug directory automatically; you MUST NOT prefix the path. meta.json must be valid JSON carrying a `slug` key plus the routing metadata (row_id, company, role).

**INTERACTIVE:** output location is /home/hermes/.grove/forge/pending_review/<slug>/ — nowhere else. mkdir -p it, then write resume.md, cover-letter.md, and meta.json there with write_file. Do NOT write to the repo, ~/, the canonical ~/.grove/forge/ dir, or any cellar sink. Only pending_review/<slug>/.

These drafts are unapproved output. They stay in pending_review/ and never reach the cellar or Google Drive until the operator taps Publish on the portal. That gate is structural.

### Step 7 — Surface to operator (INTERACTIVE only)
**HEADLESS:** you are done at Step 6 — the fleet runtime records the run and surfaces it. Do NOT emit prose after your delimited file blocks.

**INTERACTIVE:** Respond with:
1. A two-sentence summary: the role, the positioning thesis in one line, and that resume + cover letter are staged for review.
2. The portal link: http://100.102.6.70:8642/portal/fleet/forge-jobsearch/<slug>/
3. "Review the drafts and tap Publish to finalize them as a Google Doc package (it writes the folder link to the row and flips Status to Drafted), or send edits."
Do NOT paste the full assets into the response. Do NOT publish. Do NOT touch the Notion row.

## Composites
- jim-voice-writing-style — voice DNA for the cover letter + resume summary (loaded at Step 1)
- invoke_skill — load jim-voice at Step 1
- read_file — the corpus snapshot (the ONLY declared read surface: corpus_file)
- Notion MCP query_data_sources — INTERACTIVE only: read the To Apply row (read only). HEADLESS receives the row in its payload.
- write_file — INTERACTIVE only: stage drafts to ~/.grove/forge/pending_review/<slug>/. HEADLESS emits delimited file blocks and the runtime stages them.

## Output location
~/.grove/forge/pending_review/<slug>/{resume.md, cover-letter.md}

## Invocation
"Forge the application for [company]", "Tailor the resume for the [role]", "Run the forge on my To Apply list", "Make the application package for [company]."
