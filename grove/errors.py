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


class GovernanceError(GroveError):
    """Raised by the dispatch-primitive lock (``ToolRegistry.dispatch``) when a
    tool dispatch arrives WITHOUT a valid single-use Stage-04 approval token.

    GRV-010 C1c-i — the primitive is a dumb cryptographic lock: every effecting
    dispatch during a governed turn must be preceded by a classify-and-mint at
    its call site (executor on disposition; sandbox RPC / plugin via
    ``Dispatcher.classify_and_mint``; T0 / internal housekeeping via a verified
    internal mint). A call that reaches the primitive with no consumable token
    never originated from classify and is refused fail-closed — closing the
    in-process classifier-skip paths (B6 T0 / B-NEW sandbox / B7 plugin).
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


class TierUnavailableError(GroveError):
    """The model bound to the current cognitive tier could not be reached.

    GRV-010 C2d — raised at the network-execution boundary (run_agent's retry
    loop) when the tier's bound model fails with a connection drop, timeout,
    429, or exhausted credential pool AND the tier declares a ``fallback_tier``
    in ``routing.config.yaml``. It carries the failed tier + provider/model so
    the Dispatcher (DECIDE HIGH) can apply the governed downshift policy:
    re-route through the Cognitive Router at the declared fallback tier, or —
    when none is declared/valid — fail loud via ``TerminalGovernanceHalt``.

    This replaces the ungoverned, silent ``fallback_model`` substitution: tier
    substitution is the Cognitive Router's decision, made through the router,
    never a blind in-loop model swap. (C2d-1 gates this on a declared
    ``fallback_tier``; the legacy chain stays live for undeclared configs and
    is severed in C2d-2.)
    """

    def __init__(
        self,
        message: str = "",
        *,
        tier: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.tier = tier
        self.provider = provider
        self.model = model
        self.reason = reason
        if not message:
            message = (
                f"tier {tier!r} unavailable "
                f"(provider={provider!r}, model={model!r}, reason={reason!r})"
            )
        super().__init__(message)
