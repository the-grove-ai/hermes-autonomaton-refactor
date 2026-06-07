# Influencer & Institutional Outreach — Goal Context

## Strategic Purpose

Grove's thesis spreads by being independently rediscovered. Outreach
accelerates the surface area for that discovery. The goal is not to
pitch — it's to place the thesis in front of people who will recognize
it as the answer to problems they already have.

## The LinkedIn Community Engagement System

A three-database CRM in Notion for systematic relationship building.

### Database Architecture
```
Grove Community Engagement Hub
├── Contacts (CRM)           — bd1f32be-81c0-4733-bdd6-895439b5ce8b
├── Posts (Content Tracking)  — 5a478634-f3eb-4314-9465-0bbb8f69dd2c
├── Engagements (Log)         — 25e138b54d1645a3a78b266451585de9
│                               (data source: aff42c94-ef28-4a85-8615-8c07de3fca1f)
└── Hub Parent                — 2f4780a78eef8036a475ff6e69145081
```

### Contact Properties
- Name, Headline, LinkedIn URL, Sector, Priority (High/Medium/Standard)
- Connection Status, LinkedIn Degree, Relationship Stage
- Grove Alignment (5-star scale), Strategic Bucket (multi-select)
- Last Interaction, Notes, Sales Nav List Status, Bridge Contact
- Company, Follower Count, Last Active

### Strategic Buckets
- University Pipeline
- Technical Contributors
- Enterprise Contacts
- Influencers
- Potential Investors

### Engagement Workflow
1. Post publication → wait 24-48 hours
2. Capture comments → create Contact + Engagement records
3. Draft responses (stealth-mode principles apply)
4. Review and post → update status
5. Monitor for replies → escalate high-quality exchanges

### Stealth Mode Principles (STILL ACTIVE unless Jim says otherwise)
- Validate and extend their thinking — don't redirect to Grove
- Ask genuine questions — build relationships, not pitches
- Be the thoughtful voice in the room
- Plant seeds, don't harvest
- No Grove mentions until ready to announce

## Target Categories

### Confirmed Advisors (Operational — past recruiting)
- **Clement Mok** — Design strategy review, quality gate. Catches when
  a document is clever instead of clear. Enterprise design leadership
  network.
- **Susan Kare** — Visual language critique, launch participation.
  Design community amplification. "Design is philosophy expressed
  through constraint" personified.
- **Randy Wigginton** — Architecture review, operational credibility.
  Apple II, eBay, Square lineage. Apple alumni network, fintech founders.

### Enterprise Targets
- **Brinqa (Ron Dovich via Erik Cottrell)** — EU AI Act / CRA compliance
  forcing function. August-September 2026 timing. Potential reference
  implementation.
- **Dave Mariani / AtScale** — Semantic layer parallels. "Seeds vs. soil"
  framing. His semantic layer prevents fact hallucination; Grove's
  governance layer prevents action hallucination. OSI coalition validates
  declarative governance thesis.

### Institutional Targets
- **Jamie Merisotis / Lumina Foundation** — Credentials of value,
  Goal 2040 framework. GRV-003 convergence. Introduced via Derek.
- **Bjarne Stroustrup** — Technical review. C++ as governance-over-
  capability convergence evidence. Peer-to-peer framing, not a pitch.
  Jim knows him personally.

### Press/Media
- **Kara Swisher** — Primary target. Via Alison Bushnell.
- **Alison Bushnell / 104 West Partners** — PR partner. Briefed.
  Daughter of Nolan Bushnell.

### Connectors
- **Erik Cottrell** — GTM collaborator. Co-authored "The Ratchet"
  positioning. Connected to Brinqa (Ron Dovich) and Adobe/Lilly accounts.
- **Derek** — Mutual contact. Introduced Jim to Merisotis.

## Outreach Cadence (Target)

- 2-3 meaningful outreach touches per week
- Daily: Check Sales Nav feed for high-priority contact activity
- Twice weekly: Process LinkedIn engagement queue
- Weekly: Lead list maintenance, lookalike discovery
- Monthly: Advisory board check-in cadence

## Engagement Quality Framework

### High Priority
- Academia with relevant research focus
- Actively building in distributed AI space
- Influencers with large, aligned audiences
- Potential institutional partners

### Medium Priority
- Substantive engagement with complementary perspective
- Industry practitioners with implementation experience
- Governance/policy thinkers

### Standard Priority
- Brief but positive engagement
- General alignment without deep expertise

## Automation Stack (Target)
LinkedIn → PhantomBuster export → Google Sheet → Make.com →
Notion API (Contact + Engagement) → Claude API (draft) →
Review Queue → Human Approval → Post
