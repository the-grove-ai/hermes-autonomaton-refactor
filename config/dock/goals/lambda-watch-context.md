# Λ Watch — Goal Context

## What It Is

Autonomaton-based subscription research product monitoring the AI
landscape for Λ score shifts. The product demonstrates the Autonomaton
Pattern applied to a real monitoring use case — and is forkable as a
template for any domain.

## MVP Architecture

The MVP runs the Autonomaton pipeline on a monitoring use case:

1. **Telemetry**: Python cron job scans AI landscape sources (news,
   releases, regulatory filings, market moves)
2. **Recognition**: Claude API classifies signals against Λ methodology
   variables (Spreadability, Reliability, Validation, Exogenous
   Incentive, Cognitive Friction)
3. **Compilation**: Deterministic scoring produces Λ score updates
4. **Approval**: Notion-based review queue — Jim approves before publish
5. **Execution**: Report generated and delivered to subscribers

## Λ 2.0 Methodology (Verified — Do Not Approximate)

Formula: `Λ = (S × R × V) / (1 + (β · Fc)²)` with α=2 power law decay

Five variables:
- S: Spreadability
- R: Reliability/Rails
- V: Validation Multiplier
- β: Exogenous Incentive (geometric mean aggregation)
- Fc: Cognitive Friction

Four tiers:
- Structurally Inert: <0.005
- Sub-Critical: 0.005–0.029
- Approaching Critical: 0.03–0.099
- Critical Mass: ≥0.10

Historical calibrations: TCP/IP, Bitcoin, ISO Container, US Metric.
Autonomaton bare baseline: 0.0001 (Structurally Inert, V=0.2).
All scores must be Python-verified before publication.

## Why It Matters

1. Revenue opportunity (subscription model)
2. Discovery vehicle — users experience the Autonomaton Pattern without
   being told they're using it
3. Continuous content generation for the content pipeline
4. Live demonstration of the pattern applied to a non-trivial domain
5. Forkable: the monitoring pattern can be adapted for any domain,
   making it an adoption vehicle for the Autonomaton Pattern itself

## Current Status: Staging

Requirements page exists in GTM Notion. No code written yet.
Dependencies: Hermes Autonomaton stable enough to build on.
