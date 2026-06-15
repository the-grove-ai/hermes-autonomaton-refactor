"""Grove Kaizen — the Recommender.

Per the Grove Autonomaton Pattern (Draft 1.4, Commitment 5.3), Kaizen is the
third element of the Jidoka / Andon / Kaizen triplet:

    Jidoka  — detects deviation (the watcher)
    Andon   — halts execution at sovereignty boundaries (the gate)
    Kaizen  — recommends improvements (the recommender)

Sprint 06b (kaizen-foundation-v1) establishes this package. It brings the
existing Curator into the Kaizen namespace and creates three stub
submodules that later sprints will implement against the Skill Flywheel:

    detector  — IntentPatternDetector (stub). Watches the
                sovereignty_decision telemetry stream for recurring intent
                patterns and surfaces promotion candidates.
    ratchet   — TierRatchet (stub). Promotes/demotes skills across the
                four Cognitive Router tiers based on observed usage.
    refiner   — UsageRefiner (stub). Refines existing skills against
                observed usage patterns.

The three stubs raise NotImplementedError in v0.1 — Sprint 06a produced
the telemetry events; later sprints consume them. See
https://the-grove.ai/standards/001 for the canonical contract.
"""

from grove.kaizen.detector import IntentPatternDetector
from grove.kaizen.ratchet import TierRatchet
from grove.kaizen.refiner import UsageRefiner
from grove.kaizen.synthesizer import run_synthesis_pass

__all__ = [
    "IntentPatternDetector",
    "TierRatchet",
    "UsageRefiner",
    "run_synthesis_pass",
]
