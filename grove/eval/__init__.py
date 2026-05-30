"""GRV-008 § I evaluation harness — hero-prompts regression gate.

Public surface for Sprint 46's :mod:`hero_runner` and the Sprint 47
``gate_proposal`` entry point. Importing from this package is the
canonical way to reach the runner; the internal modules are not
considered public stable API.
"""

from grove.eval.hero_runner import (
    AssertionFailure,
    EvalReport,
    GateResult,
    HeroPrompt,
    PromptResult,
    evaluate,
    gate_proposal,
    load_hero_prompts,
)

__all__ = [
    "AssertionFailure",
    "EvalReport",
    "GateResult",
    "HeroPrompt",
    "PromptResult",
    "evaluate",
    "gate_proposal",
    "load_hero_prompts",
]
