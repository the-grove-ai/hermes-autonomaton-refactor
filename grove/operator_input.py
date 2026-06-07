"""Operator-input control-flow signal for store-and-resume governance.

Sprint 67 (kaizen-governance-parity-v1). Some surfaces deliver an
operator decision *across a turn boundary* rather than by blocking the
agent thread: the web ``/v1/chat/completions`` API emits a governance
prompt (or a clarify question) as the turn's response, closes the
connection, and parses the operator's *next* message as the answer.

To make a turn yield control deterministically — without executing the
gated action and without fabricating a clarify answer — the surface
raises :class:`OperatorInputRequired` from inside the agent thread. The
turn ends; the caller persists the :class:`PendingOperatorRequest` and
surfaces ``prompt_text`` as the final response.

``OperatorInputRequired`` subclasses **BaseException**, not Exception,
because it is a control-flow interrupt (like ``GeneratorExit`` /
``KeyboardInterrupt``), not a runtime error. The dispatcher's generator
body is audited to contain zero bare-except / except-BaseException sites
(see ``grove/dispatcher.py`` dispatch_turn, which already lets
``GeneratorExit`` pass through every ``except Exception`` block), so the
signal propagates cleanly past the ~20 ``except Exception`` catches
between the raise site and the surface's terminal catch. The two places
that *do* interact with it (dispatch_turn's ``except BaseException``
ledger guard, and the api_server terminal catch) handle it explicitly;
the tool-boundary catches in ``tools/clarify_tool.py`` and
``grove/tool_executor.py`` carry an explicit ``except
OperatorInputRequired: raise`` so the control-flow contract is visible
where a future maintainer might add a swallow.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


# Pending decisions auto-CANCEL (never auto-ALLOW) after this many
# seconds with no operator response. Enforced lazily on the next inbound
# message — a store-and-resume surface has no parked thread to time out.
TIMEOUT_SECONDS = 300


class OperatorInputRequired(BaseException):
    """Raised inside the agent thread to yield control to the operator.

    Carries the typed :class:`PendingOperatorRequest` describing what is
    needed and the butler-register ``prompt_text`` to surface as the
    turn's final response.
    """

    def __init__(self, pending: "PendingOperatorRequest") -> None:
        self.pending = pending
        super().__init__(pending.prompt_text)


@dataclass
class PendingOperatorRequest:
    """A decision the operator must make, persisted across the turn boundary.

    ``kind`` is ``"governance"`` (a gated action awaiting once/session/
    always/deny) or ``"clarify"`` (an open question awaiting a free-form
    or multiple-choice answer).
    """

    kind: str
    prompt_text: str
    original_user_message: str
    created_at: float
    timeout_at: float
    # governance fields
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    pattern_key: Optional[str] = None
    zone_payload: Optional[Dict[str, Any]] = None
    # clarify fields
    question: Optional[str] = None
    choices: Optional[List[str]] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "PendingOperatorRequest":
        return cls(**json.loads(raw))


# ── SessionDB.state_meta key namespacing ─────────────────────────────
# All keys are scoped by session_id so pending state never leaks between
# sessions (an Andon trigger). Empty-string value == cleared.

def state_key(session_id: str) -> str:
    """Key for the pending operator request (governance or clarify)."""
    return f"pending_operator_request:{session_id}"


def governance_grant_key(session_id: str) -> str:
    """Key for a primed governance grant consumed by the replay turn.

    Set when the operator approves a pending action; read by the surface's
    governance handler on the replay so the now-approved action returns
    its disposition instead of re-raising.
    """
    return f"governance_grant:{session_id}"


def clarify_answer_key(session_id: str) -> str:
    """Key for the seeded clarify answer consumed by the replay turn.

    Set to the operator's reply when a clarify is pending; read by the
    surface's clarify callback on the replay so the tool returns the
    answer instead of re-raising.
    """
    return f"clarify_answer:{session_id}"
