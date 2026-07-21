"""propose-approve-deadlock-v1 Phase 1b-iii — secret-log redaction proof.

Sentinel proof: a governance-write with a SENTINEL secret must NOT appear in any
sink (durable intent feed, console/gateway.log, /v1 SSE stream), and the redaction
marker (sha256 prefix) MUST be present (redacted, not dropped). Covers the
governance-write tool for BOTH the .env-approve path and the YELLOW/grant path
(same tool, same sinks). Plus the T0-pattern fail-closed re-assert.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Any, Dict, List

import pytest

from grove.dispatcher import Dispatcher
from grove.effect_signature import canonical_effect_signature
from grove.intent_store import IntentStore
from grove.intents import Observation, ToolIntent
from grove.secret_redact import (
    GOVERNANCE_WRITE_TOOL,
    redact_governance_args,
    redaction_marker,
)
from tests.grove.test_dispatcher_intent_records import (
    _bare_agent_with_exec,
    _patch_classifier_green,
    _set_current_classification,
    _synthetic_generator,
)

SENTINEL = "hf_SENTINEL_SUPER_SECRET_9Z9Z9Z"


# ── The redaction transform (used at all three sinks) ─────────────────────────
class TestRedactHelper:
    def test_content_redacted_target_kept(self):
        r = redact_governance_args(
            GOVERNANCE_WRITE_TOOL,
            {"target_file": "~/.grove/.env", "content": f"HF_TOKEN={SENTINEL}\n", "rationale": "r"},
        )
        assert SENTINEL not in json.dumps(r)
        assert "redacted sha256=" in r["content"]
        assert r["target_file"] == "~/.grove/.env"    # path kept legible

    def test_diff_or_content_alias(self):
        r = redact_governance_args(GOVERNANCE_WRITE_TOOL, {"diff_or_content": SENTINEL})
        assert SENTINEL not in json.dumps(r) and "redacted sha256=" in r["diff_or_content"]

    def test_other_tool_and_nondict_passthrough(self):
        assert redact_governance_args("write_file", {"content": SENTINEL}) == {"content": SENTINEL}
        assert redact_governance_args(GOVERNANCE_WRITE_TOOL, None) is None

    def test_no_mutation_of_original(self):
        a = {"content": SENTINEL}
        redact_governance_args(GOVERNANCE_WRITE_TOOL, a)
        assert a["content"] == SENTINEL   # defensive copy


# ── SINK 1 — durable intent feed (~/.grove/intent_records.jsonl) ──────────────
class TestSink1IntentFeed:
    def _drive_propose(self, monkeypatch, tmp_path, content: str) -> str:
        """Drive a real turn yielding propose_governance_change(content); return
        the raw intent_records.jsonl text."""
        # capability-mutation-surface-v1 P5 — classify_governance_target is
        # retired; an unrecognized target now classifies YELLOW through the
        # thin-proposer door and the silent handler below drives past the
        # halt. The redaction at capture is classification-independent.
        _patch_classifier_green(monkeypatch)
        _set_current_classification(monkeypatch)
        store = IntentStore(store_path=tmp_path / "records.jsonl")
        # propose_governance_change is a governed tool → an Andon halt fires even
        # with the target unrecognized. Inject the silent "once" handler so the
        # turn drives past the halt and the intent record is persisted; the
        # redaction is applied at the tool_invocation capture site regardless of
        # execution disposition.
        from grove.sovereign_prompt_handlers import silent_allow_handler

        d = Dispatcher(intent_store=store, sovereign_prompt_handler=silent_allow_handler)
        agent = _bare_agent_with_exec([])
        intent = ToolIntent(
            tool_name=GOVERNANCE_WRITE_TOOL,
            arguments={"target_file": "x.txt", "content": content, "rationale": "r"},
            call_id="c1",
        )
        agent._run_turn_generator = (
            lambda **kw: _synthetic_generator([intent], {"final_response": "ok"})
        )
        d.dispatch_turn(agent, user_message="persist a token")
        return (tmp_path / "records.jsonl").read_text()

    def test_env_content_redacted_in_feed(self, monkeypatch, tmp_path):
        raw = self._drive_propose(monkeypatch, tmp_path, f"HF_TOKEN={SENTINEL}\n")
        assert SENTINEL not in raw                       # secret ABSENT from disk
        assert "redacted sha256=" in raw                 # redacted, not dropped
        # the tool_invocation line names the tool (proves the record was written)
        assert GOVERNANCE_WRITE_TOOL in raw


# ── SINK 2 — console / gateway.log (_log_tool_call_line) ──────────────────────
class TestSink2Console:
    def _line(self, verbose: bool) -> str:
        import run_agent

        stub = object.__new__(run_agent.AIAgent)
        stub.verbose_logging = verbose
        stub.log_prefix_chars = 500
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_agent.AIAgent._log_tool_call_line(
                stub, 1, GOVERNANCE_WRITE_TOOL,
                {"target_file": "~/.grove/.env", "content": f"HF_TOKEN={SENTINEL}\n"},
            )
        return buf.getvalue()

    def test_verbose_redacted(self):
        out = self._line(verbose=True)
        assert SENTINEL not in out and "redacted sha256=" in out

    def test_preview_redacted(self):
        out = self._line(verbose=False)
        assert SENTINEL not in out and "redacted sha256=" in out


# ── SINK 3 — /v1 SSE stream (the transform both callbacks apply) ──────────────
class TestSink3Stream:
    def test_streamed_arguments_redacted(self):
        # _on_tool_start / _on_tool_complete queue redact_governance_args(name, args)
        # as the function_call.arguments blob (whole, render-only). Assert the
        # streamed blob carries no sentinel and the marker.
        streamed = redact_governance_args(
            GOVERNANCE_WRITE_TOOL,
            {"target_file": "~/.grove/.env", "content": f"HF_TOKEN={SENTINEL}\n"},
        )
        blob = json.dumps(streamed)  # exactly what _emit_tool_started serializes
        assert SENTINEL not in blob and "redacted sha256=" in blob
        assert "~/.grove/.env" in blob   # target still legible for the client


# ── SINK 4 (guard) — CLI completion preview must not render the payload ───────
class TestSink4CompletionPreviewGuard:
    """The tool-completion line (run_agent._log_tool_complete_line →
    agent.display.get_cute_tool_message → build_tool_preview) renders a one-line
    arg PREVIEW for many tools. propose_governance_change is deliberately NOT in
    the primary-arg map and matches none of the fallback keys, so the preview is
    empty — the .env body never reaches the completion line. This guard fails
    loudly if a future edit wires ``content`` into that preview."""

    def test_cute_message_no_payload(self):
        from agent.display import build_tool_preview, get_cute_tool_message

        args = {"target_file": "~/.grove/.env", "content": f"HF_TOKEN={SENTINEL}\n", "rationale": "r"}
        assert build_tool_preview(GOVERNANCE_WRITE_TOOL, args) is None
        line = get_cute_tool_message(GOVERNANCE_WRITE_TOOL, args, 0.0, result="ok")
        assert SENTINEL not in line


# ── T0 pattern fail-closed re-assert ─────────────────────────────────────────
class TestT0FailClosed:
    def test_redacted_signature_mismatches_live(self):
        """A T0 pattern compiled from the REDACTED tool_invocation computes an
        approved_signature over redacted args; a live serve computes it over the
        REAL args → mismatch → T0 fail-closes (never a blind replay of the write)."""
        real_args = {"target_file": "x.txt", "content": f"HF_TOKEN={SENTINEL}\n", "rationale": "r"}
        redacted_args = redact_governance_args(GOVERNANCE_WRITE_TOOL, real_args)
        stored_sig = canonical_effect_signature(GOVERNANCE_WRITE_TOOL, redacted_args)
        live_sig = canonical_effect_signature(GOVERNANCE_WRITE_TOOL, real_args)
        assert stored_sig != live_sig   # mismatch → _T0SignatureMismatch → Stage-04
        assert SENTINEL not in stored_sig   # the stored signature carries no secret
