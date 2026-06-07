# the-grove.ai — Site Context

## Architecture

Static HTML site deployed via Vercel. CSS is inline per-page (no shared
external stylesheet). CleanURLs enabled in vercel.json.

- Repo: `~/GitHub/grove-launch-site`
- Vercel project: `grove-launch-site`
- Team: `team_odJBQTq9WJT64ceCXCYei9gl`

## Deploy Protocol

Vercel auto-deploy is broken. Always deploy manually:
```bash
cd ~/GitHub/grove-launch-site
npx vercel --prod --yes
```

Verify with curl (never web_fetch — returns stale CDN content).
Two-pass regex for Λ entities (literal Λ first, then `&Lambda;`).

## Published Standards

- GRV-001: The Autonomaton Pattern — five-stage pipeline, Zone Model,
  Skill Flywheel, Cognitive Router
- GRV-002: Architectural theory (TCP/IP structural correspondence)
- GRV-003: Learner Autonomaton — sovereign AI for education
- GRV-004: The Autonomaton Protocol — DNS for polarity-compliant internet

## Key Pages

- `/standards/001` through `/standards/004`
- `/alerts/[slug]` — Λ Watch alerts
- `/research/knowledge-polarity` — three-terminal circuit model
- `/ratchet` — Ratchet thesis
- `/lambda` — Λ methodology
- `/registrar` — operator registration
- `/substrate/jim-calhoun` — operator substrate page

## Navigation Labels (Canonical)

"Published Standards" · "Λ Watch" (mobile: "Λ Watch") · "Run the Pattern ↗"

## Hospitable Graph

The site is structured for AI-agent readability:
- `grove-standards.json` — corpus manifest
- `llms.txt` — LLM-readable site summary
- `sitemap.xml` — standard sitemap
- `robots.txt` — AI crawler welcome blocks

## Design System

- Background: `#080808`
- Text: `#E8E2D9` (warm cream)
- Accent: `#D4621A` (burnt amber), bright variant `#F07030`
- Display font: Instrument Serif
- Body font: DM Sans (300-600)
- Mono font: Fragment Mono (wide letter-spacing)
- Grid texture background
- Asanoha (麻の葉) mark

## Active Backlog

1. protocol-retrofit-v1 (Bicameral Canon) — large
2. manifest-design-v1
3. css-coherence-v1
4. footer-nav-standardization-v1
5. homepage-card-grid-redesign-v1

## Letter Pages

Auth gate at `/substrate/jim-calhoun/letter/` is operator-gated,
indefinite.
