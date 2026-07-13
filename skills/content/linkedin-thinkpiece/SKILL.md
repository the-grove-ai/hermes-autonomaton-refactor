---
created_by: bundled
zone: green
tier: T1
name: linkedin-thinkpiece
description: Transform source material into Jim Calhoun's strategic tech commentary for LinkedIn. Use when the user says "write a LinkedIn piece about...", "turn this into a LinkedIn thinkpiece", "LinkedIn draft from...", "thinkpiece on...", or requests LinkedIn long-form content on technology, AI, or infrastructure topics. Produces 800-2,500 word thought leadership with claim-based headers, specific evidence, and reveal-and-reframe architecture. Supports three modes — standard analysis (1,500-2,500 words), short reaction (800-1,200 words), and thread-seed (150-300 words).
---

# LinkedIn Thinkpiece

Produce LinkedIn thought leadership establishing Jim Calhoun as a credible voice on AI infrastructure, concentration risk, and the strategic implications of technical decisions most people treat as routine.

## The Author

Jim Calhoun writes as a software veteran who watched the open internet become platform dependency. His authority comes from **pattern recognition across tech cycles**, not credentials or titles. Journalism training (University of Missouri), Silicon Valley experience during the open internet era, AI experimentation since 2017-2018. Reads research papers, regulatory filings, and technical documentation — not just headlines.

**He is:** A technologist who's seen this movie before. Specific about mechanisms, not vague about "dangers." Willing to say what the implications are when others stop at description.

**He is not:** A doomsayer, a crypto evangelist, a founder pitching his product, an academic, or a consultant packaging frameworks.

## Voice (Summary)

Active voice. Present tense. Short sentences carrying complex ideas. 8th-grade reading level, graduate-level thinking.

**Lead with the implication, not the event:**
- ❌ "OpenAI announced a new identity system at their developer conference."
- ✅ "OpenAI just made a play for the identity layer of the internet — and almost nobody noticed."

**Be specific about mechanisms.** Name the company, the product, the number. "Significant growth" is not an observation. "31% year-over-year on a $4.2B base" is an observation.

For the complete rhetorical toolkit, reframe techniques, and anti-patterns list, see [references/voice-rules.md](references/voice-rules.md).

## Structure: Reveal-and-Reframe Architecture

The reader arrives thinking they understand a topic. They leave seeing it differently.

### 1. The Hook (1-3 paragraphs)

Anchor in something specific and recent — a product launch, research paper, earnings call, policy change. Establish credibility and currency in the first sentence. Within three paragraphs, pivot from what happened to what it *means*.

### 2. The Build (3-5 headed sections)

Each section advances the argument one rung. No section repeats another's claim.

**Section headers must be claims, not topics:**
- ❌ "Enterprise Implications"
- ✅ "Your Enterprise Agreement Won't Protect You"

**Evidence layering:** Start specific (one company's decision), expand outward (industry pattern → structural dynamic). Never start abstract.

### 3. The Cascade (1-2 paragraphs, optional)

Fan implications across audiences: "If you're a [role], this means..." Makes the piece shareable. Skip when the point is universal.

### 4. The Close (1-2 paragraphs)

End with urgency, not summary. Never restate the argument. Leave the reader with a question they'll keep thinking about, a window that's closing, or a choice they didn't realize they were making.

**Never end with:** a call to action, a product mention, or "What do you think?"

## The Grove Dial

Most pieces sit in Zone 1 or 2. Default to Zone 1 unless source material naturally pulls toward Zone 2.

| Zone | Description | Frequency |
|------|-------------|-----------|
| Zone 1: Pure Analysis | No mention of alternatives or distributed infrastructure. Credibility comes from the analysis itself. | Most common |
| Zone 2: Thesis-Adjacent | Analysis naturally raises questions about infrastructure ownership or concentration risk. Reader connects dots. May mention "distributed alternatives" as a category, never a product. | Frequent |
| Zone 3: Thesis-Central | Explicitly about centralized vs. distributed infrastructure. Grove mentioned as one approach among several. | Rare (~1 in 10) |

## Process

### Step 1: Identify the Source Signal

Find the specific, concrete anchor. It should be recent (last 2-4 weeks), specific (named event/paper/announcement), and underanalyzed (the obvious take has been written — what hasn't?).

- For research papers: the implication the authors didn't emphasize
- For news events: the second-order effect — what it enables or forecloses
- For a thesis: the most counterintuitive version that makes people stop scrolling

### Step 2: Find the Angle Nobody's Written

The angle that would make a thoughtful CTO nod and say "I hadn't thought about it that way." Must pass two tests: (1) non-obvious — can't be reached from reading the headline alone, and (2) defensible — supported by specific evidence, not vibes.

### Step 3: Map the Build

Outline 3-5 sections. Check that no two make the same point, each has specific evidence, and they build in scope (personal → institutional → structural, or present → near-term → systemic).

### Step 4: Draft

Write the full piece. Target 1,500-2,500 words standard, 800-1,200 words for lighter topics. 150-300 words for thread-seeds (single sharp observation, no headed sections).

### Step 5: Voice Check

Verify against these questions:
- Does the first sentence earn the second?
- Could you cut the first paragraph and lose nothing? (If yes, cut it.)
- Is every section header a claim, not a topic?
- Did you use the same rhetorical device twice? (Vary one.)
- Does the close summarize? (Rewrite it.)
- Does it read like a pitch at any point? (Strip it back to analysis.)

For structural examples from published pieces, see [references/examples.md](references/examples.md).

## Quality Standard

The finished piece should pass this test: if a CTO, a policy researcher, and a mid-career developer all read it, each should find something that makes them reconsider an assumption. If it only works for one audience, the build sections aren't varied enough. If it works for none, the angle isn't sharp enough.
