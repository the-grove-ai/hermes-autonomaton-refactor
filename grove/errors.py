"""Grove Autonomaton — shared exception types.

Per the Architectural Prime Directive (no silent degradation),
unrecoverable structural mismatches surface as named, catchable
exceptions instead of falling back to a guess. These types make
the failure mode visible at the call site and at the operator's
prompt.
"""

from __future__ import annotations


class GroveError(ValueError):
    """Base class for every Grove-level structural exception."""


class ProviderDetectionError(GroveError):
    """The provider's wire protocol cannot be positively identified.

    Raised by ``AIAgent.__init__`` when the combination of
    ``provider``, ``api_mode``, and ``base_url`` does not match any
    recognized branch in the detection chain. The message names the
    inputs and instructs the operator to declare ``api_mode``
    explicitly in ``routing.config.yaml``.

    Sprint 47 hotfix: this replaces a silent default to
    ``chat_completions`` that constructed an OpenAI client against
    ``api.anthropic.com``, producing a 404 with no surfaced error.
    Andon over fallback — the system MUST NOT guess.
    """


class SchemaConfigurationError(GroveError):
    """A schema file the runtime depends on contains a structural fault.

    Raised by ``grove.zones._build_tool_entry`` when a per-rule
    safety check (length cap, catch-all rejection, nested
    quantifier detection, alternation-branch cap, or syntactic
    validity) fails on a rule in ``zones.schema.yaml``.

    Sprint 32 Phase 3b: replaces the v1.0 "log error + drop the
    bad rule + keep loading" graceful-degradation path. Schema
    faults are now load-time failures — the agent does not start
    on malformed governance. The error message names the offending
    tool, the rule pattern, and the specific safety check that
    failed so the operator can locate and fix without reading code.
    """
