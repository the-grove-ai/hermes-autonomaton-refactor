"""Sprint 36 — declarative prompt composition (GRV-007).

The Dispatcher owns prompt composition. Section providers register
declaratively; the composer assembles the system prompt from registered
sections per ``config/prompt.config.yaml``; the Agent receives the
composed prompt and does not produce it.

Public surface:

* ``PromptComposer`` — the registration + composition entry point.
* ``SectionResult`` — what a provider returns when it has content.
* ``ComposedPrompt`` — what ``compose()`` returns: joined text, per-
  section breakdown, and the three-tier split (backward-compat with
  the pre-Sprint-36 ``_build_system_prompt_parts`` shape).
* ``build_default_composer`` — registers the 17 v0.1 section providers
  that mirror the pre-Sprint-36 ``_build_system_prompt_parts`` output.
"""

from grove.prompt.composer import (
    ComposedPrompt,
    PromptComposer,
    SectionRegistration,
    SectionResult,
    build_default_composer,
)

__all__ = [
    "ComposedPrompt",
    "PromptComposer",
    "SectionRegistration",
    "SectionResult",
    "build_default_composer",
]
