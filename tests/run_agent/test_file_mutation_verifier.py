"""Tests for the per-turn file-mutation verifier footer.

Covers the three moving pieces:

1. ``_extract_file_mutation_targets`` — pulls file paths from write_file /
   patch (replace + V4A) tool-call argument dicts.
2. ``AIAgent._record_file_mutation_result`` — builds the per-turn state
   dict, removing entries when a later success supersedes an earlier
   failure for the same path.
3. ``AIAgent._format_file_mutation_failure_footer`` — renders the dict
   as a user-visible advisory.

Regression target: the "Ben Eng llm-wiki" session where grok-4.1-fast
batched parallel patches, half failed, and the model summarised the
turn claiming every file was edited.  This verifier makes over-claiming
structurally impossible past the model: the user always sees the real
list of files that did NOT change.
"""

from __future__ import annotations

import json

import pytest

from run_agent import (
    AIAgent,
    _FILE_MUTATING_TOOLS,
    _extract_error_preview,
    _extract_file_mutation_targets,
)


# ---------------------------------------------------------------------------
# _extract_file_mutation_targets
# ---------------------------------------------------------------------------


class TestExtractFileMutationTargets:
    def test_non_mutating_tool_returns_empty(self):
        assert _extract_file_mutation_targets("read_file", {"path": "/x"}) == []
        assert _extract_file_mutation_targets("terminal", {"command": "ls"}) == []

    def test_write_file_returns_single_path(self):
        out = _extract_file_mutation_targets("write_file", {"path": "/tmp/a.md", "content": "x"})
        assert out == ["/tmp/a.md"]

    def test_write_file_missing_path_returns_empty(self):
        assert _extract_file_mutation_targets("write_file", {"content": "x"}) == []

    def test_patch_replace_mode_returns_path(self):
        args = {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"}
        assert _extract_file_mutation_targets("patch", args) == ["/tmp/a.md"]

    def test_patch_default_mode_is_replace(self):
        # Mode omitted — schema default is ``replace``.
        args = {"path": "/tmp/a.md", "old_string": "x", "new_string": "y"}
        assert _extract_file_mutation_targets("patch", args) == ["/tmp/a.md"]

    def test_patch_v4a_single_file(self):
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/a.md\n"
            "@@ ctx @@\n"
            " line1\n"
            "-bad\n"
            "+good\n"
            "*** End Patch\n"
        )
        args = {"mode": "patch", "patch": body}
        assert _extract_file_mutation_targets("patch", args) == ["/tmp/a.md"]

    def test_patch_v4a_multi_file(self):
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/a.md\n"
            "@@ @@\n-a\n+b\n"
            "*** Add File: /tmp/new.md\n"
            "+fresh\n"
            "*** Delete File: /tmp/old.md\n"
            "*** End Patch\n"
        )
        args = {"mode": "patch", "patch": body}
        paths = _extract_file_mutation_targets("patch", args)
        assert paths == ["/tmp/a.md", "/tmp/new.md", "/tmp/old.md"]

    def test_patch_v4a_missing_body_returns_empty(self):
        assert _extract_file_mutation_targets("patch", {"mode": "patch"}) == []
        assert _extract_file_mutation_targets("patch", {"mode": "patch", "patch": ""}) == []


# ---------------------------------------------------------------------------
# _extract_error_preview
# ---------------------------------------------------------------------------


class TestExtractErrorPreview:
    def test_json_error_field_preferred(self):
        raw = json.dumps({"success": False, "error": "Could not find old_string in /tmp/x"})
        assert _extract_error_preview(raw) == "Could not find old_string in /tmp/x"

    def test_plain_string_falls_through(self):
        assert _extract_error_preview("Error executing tool: boom") == "Error executing tool: boom"

    def test_long_preview_truncated(self):
        long = "x" * 500
        out = _extract_error_preview(long, max_len=50)
        assert len(out) <= 50
        assert out.endswith("…")

    def test_none_returns_empty(self):
        assert _extract_error_preview(None) == ""


# ---------------------------------------------------------------------------
# _record_file_mutation_result — state transitions
# ---------------------------------------------------------------------------


def _bare_agent() -> AIAgent:
    """Skip __init__ and only attach the per-turn state dict.

    AIAgent.__init__ takes ~60 parameters and touches network, auth, and
    the filesystem.  For these tests we only need the two methods —
    ``_record_file_mutation_result`` and ``_format_file_mutation_failure_footer``.
    Using ``object.__new__`` mirrors the gateway-test pattern documented in
    the agent pitfalls list.
    """
    agent = object.__new__(AIAgent)
    agent._turn_failed_file_mutations = {}
    return agent


class TestRecordFileMutationResult:
    def test_non_mutating_tool_ignored(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "read_file", {"path": "/tmp/x"}, "{}", is_error=True,
        )
        assert agent._turn_failed_file_mutations == {}

    def test_failure_recorded(self):
        agent = _bare_agent()
        result = json.dumps({"success": False, "error": "Could not find old_string"})
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            result, is_error=True,
        )
        state = agent._turn_failed_file_mutations
        assert "/tmp/a.md" in state
        assert state["/tmp/a.md"]["tool"] == "patch"
        assert "Could not find old_string" in state["/tmp/a.md"]["error_preview"]

    def test_success_removes_prior_failure(self):
        agent = _bare_agent()
        # First attempt fails
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "not found"}), is_error=True,
        )
        assert "/tmp/a.md" in agent._turn_failed_file_mutations
        # Second attempt with corrected old_string succeeds
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "real", "new_string": "fixed"},
            json.dumps({"success": True, "diff": "..."}), is_error=False,
        )
        assert agent._turn_failed_file_mutations == {}

    def test_write_file_with_lint_error_counts_as_landed(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": "/tmp/a.py", "content": "bad"},
            json.dumps({"error": "write failed"}),
            is_error=True,
        )
        assert "/tmp/a.py" in agent._turn_failed_file_mutations

        result = json.dumps({
            "bytes_written": 24,
            "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
        })

        agent._record_file_mutation_result(
            "write_file",
            {"path": "/tmp/a.py", "content": "def nope(:\n"},
            result,
            is_error=True,
        )

        assert agent._turn_failed_file_mutations == {}

    def test_patch_with_lsp_diagnostics_counts_as_landed(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "/tmp/a.py", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "Could not find old_string"}),
            is_error=True,
        )
        assert "/tmp/a.py" in agent._turn_failed_file_mutations

        result = json.dumps({
            "success": True,
            "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
            "files_modified": ["/tmp/a.py"],
            "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
        })

        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "/tmp/a.py", "old_string": "x", "new_string": "y"},
            result,
            is_error=True,
        )

        assert agent._turn_failed_file_mutations == {}

    def test_repeated_failure_keeps_first_error(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "v1", "new_string": "y"},
            json.dumps({"error": "first error"}), is_error=True,
        )
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "v2", "new_string": "y"},
            json.dumps({"error": "second error"}), is_error=True,
        )
        # Keep the original error — swapping to the latest would obscure
        # the initial root cause.
        assert "first error" in agent._turn_failed_file_mutations["/tmp/a.md"]["error_preview"]

    def test_v4a_multi_file_all_tracked(self):
        agent = _bare_agent()
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/a.md\n@@ @@\n-a\n+b\n"
            "*** Update File: /tmp/b.md\n@@ @@\n-a\n+b\n"
            "*** End Patch\n"
        )
        agent._record_file_mutation_result(
            "patch", {"mode": "patch", "patch": body},
            json.dumps({"error": "parse failure"}), is_error=True,
        )
        assert set(agent._turn_failed_file_mutations) == {"/tmp/a.md", "/tmp/b.md"}

    def test_no_state_dict_silent_noop(self):
        """When called outside run_conversation the state dict is absent.

        The record helper must never raise — a tool dispatched from, say,
        a direct ``chat()`` call should not blow up the call site just
        because the verifier state hasn't been initialised.
        """
        agent = object.__new__(AIAgent)  # no state attached
        # Should not raise
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md"},
            json.dumps({"error": "x"}), is_error=True,
        )

    def test_missing_path_arg_recorded_nowhere(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch", {"mode": "replace"},  # no path
            json.dumps({"error": "path required"}), is_error=True,
        )
        # No path → nothing to key on, state stays empty.  The per-turn
        # state is about file paths, not individual tool-call IDs.
        assert agent._turn_failed_file_mutations == {}


# ---------------------------------------------------------------------------
# _format_file_mutation_failure_footer
# ---------------------------------------------------------------------------


class TestFormatFooter:
    def test_empty_returns_empty_string(self):
        assert AIAgent._format_file_mutation_failure_footer({}) == ""

    def test_single_failure(self):
        out = AIAgent._format_file_mutation_failure_footer(
            {"/tmp/a.md": {"tool": "patch", "error_preview": "Could not find old_string"}},
        )
        assert "1 file(s) were NOT modified" in out
        assert "/tmp/a.md" in out
        assert "Could not find old_string" in out
        assert "git status" in out  # user-actionable hint

    def test_truncation_at_10_entries(self):
        failed = {
            f"/tmp/f{i}.md": {"tool": "patch", "error_preview": "err"}
            for i in range(15)
        }
        out = AIAgent._format_file_mutation_failure_footer(failed)
        assert "15 file(s) were NOT modified" in out
        assert "… and 5 more" in out
        # Ten file bullets + header + "and X more" line
        lines = out.split("\n")
        bullet_lines = [ln for ln in lines if ln.lstrip().startswith("•")]
        assert len(bullet_lines) == 11  # 10 shown + 1 summary


# ---------------------------------------------------------------------------
# _file_mutation_verifier_enabled — env + config precedence
# ---------------------------------------------------------------------------


class TestVerifierEnabled:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("GROVE_FILE_MUTATION_VERIFIER", raising=False)
        agent = _bare_agent()
        # With no env and no config present, safe default is True.
        # load_config may surface a user config.yaml in some envs — stub it.
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {})
        assert agent._file_mutation_verifier_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
    def test_env_disables(self, monkeypatch, value):
        monkeypatch.setenv("GROVE_FILE_MUTATION_VERIFIER", value)
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is False

    def test_env_enables_over_config(self, monkeypatch):
        monkeypatch.setenv("GROVE_FILE_MUTATION_VERIFIER", "1")
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(
            _cfg_mod, "load_config",
            lambda: {"display": {"file_mutation_verifier": False}},
        )
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is True

    def test_config_disables_when_no_env(self, monkeypatch):
        monkeypatch.delenv("GROVE_FILE_MUTATION_VERIFIER", raising=False)
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(
            _cfg_mod, "load_config",
            lambda: {"display": {"file_mutation_verifier": False}},
        )
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is False


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


def test_file_mutating_tools_set_shape():
    """write_file + patch are the only tools the verifier tracks.

    Guard rail: if someone adds a third file-mutating tool (e.g. a new
    ``append_file``), they should also audit whether the verifier should
    track it.  This test fails loudly on unilateral additions.
    """
    assert _FILE_MUTATING_TOOLS == frozenset({"write_file", "patch"})


# ---------------------------------------------------------------------------
# governance-representation-v1 — Option C wholesale replacement on a
# governed-path block.  The verifier already DETECTED the blocked write; this
# sprint makes the honest outcome the PRIMARY operator-facing text instead of
# a footer appended after the model's (false) success claim.
# ---------------------------------------------------------------------------


from run_agent import _is_governed_block  # noqa: E402
from grove.utils.fs_utils import GOVERNED_PATH_MESSAGE  # noqa: E402


def _verifier_agent() -> AIAgent:
    """Bare agent with both per-turn ledgers + the verifier forced on."""
    agent = object.__new__(AIAgent)
    agent._turn_failed_file_mutations = {}
    agent._turn_succeeded_tools = []
    agent._file_mutation_verifier_enabled = lambda: True  # type: ignore[method-assign]
    return agent


class TestIsGovernedBlock:
    def test_true_for_wall_message(self):
        result = json.dumps({"error": GOVERNED_PATH_MESSAGE})
        assert _is_governed_block(result) is True

    def test_false_for_generic_write_error(self):
        assert _is_governed_block(json.dumps({"error": "No space left on device"})) is False
        assert _is_governed_block(json.dumps({"error": "Could not find old_string"})) is False

    def test_false_for_success_and_non_json(self):
        assert _is_governed_block(json.dumps({"bytes_written": 12})) is False
        assert _is_governed_block("not json at all") is False
        assert _is_governed_block(None) is False


class TestGovernanceBlockReplacement:
    def test_governed_block_replaces_false_success_claim(self):
        """SPEC test 1: a governance-blocked write yields operator text that
        states the block reason and does NOT carry the model's false claim."""
        agent = _verifier_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": "~/.grove/research/test-governance.md", "content": "notes"},
            json.dumps({"error": GOVERNED_PATH_MESSAGE}),
            is_error=True,
        )
        # The governed block is recorded and flagged.
        entry = agent._turn_failed_file_mutations["~/.grove/research/test-governance.md"]
        assert entry["governed"] is True

        false_claim = "Saved your research notes to ~/.grove/research/test-governance.md ✓"
        out = agent._apply_mutation_verifier(false_claim)

        # The lie is gone; the honest outcome is primary.
        assert "Saved your research notes" not in out
        assert "Write blocked" in out
        assert "~/.grove/research/test-governance.md" in out
        assert "governed path" in out.lower()
        assert "To proceed" in out

    def test_successful_write_response_unchanged(self):
        """SPEC test 2: a Green-zone / granted-workspace write that lands is
        NOT touched — no false positive, no footer, no replacement."""
        agent = _verifier_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": "~/Documents/notes.md", "content": "notes"},
            json.dumps({"bytes_written": 24}),
            is_error=False,
        )
        text = "Saved your notes to ~/Documents/notes.md"
        out = agent._apply_mutation_verifier(text)
        assert out == text
        assert agent._turn_failed_file_mutations == {}

    def test_grant_authorized_write_reports_success_honestly(self):
        """SPEC test 3: a write that passed the wall (grant token / granted
        workspace) returns a landed result, never enters the failed ledger,
        and its honest success text rides through untouched."""
        agent = _verifier_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": "~/.grove/granted_ws/out.md", "content": "x"},
            json.dumps({"bytes_written": 30}),
            is_error=False,
        )
        text = "Done — wrote 30 bytes to ~/.grove/granted_ws/out.md"
        out = agent._apply_mutation_verifier(text)
        assert out == text
        assert agent._turn_failed_file_mutations == {}

    def test_generic_failure_keeps_footer_not_replacement(self):
        """Regression guard: a NON-governed write failure keeps the existing
        advisory footer (append), never the governance replacement."""
        agent = _verifier_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "Could not find old_string"}),
            is_error=True,
        )
        text = "Patched /tmp/a.md as requested."
        out = agent._apply_mutation_verifier(text)
        assert out.startswith("Patched /tmp/a.md as requested.")  # original preserved
        assert "File-mutation verifier" in out                    # footer appended
        assert "Write blocked" not in out                         # NOT the replacement

    def test_replacement_lists_what_else_succeeded(self):
        """Mixed turn: reads/searches that succeeded are summarised (tool-name
        granularity) from execution outcomes, not the model's narrative."""
        agent = _verifier_agent()
        agent._record_file_mutation_result(
            "search_files", {"query": "zones"}, json.dumps({"matches": []}), is_error=False,
        )
        agent._record_file_mutation_result(
            "read_file", {"path": "/tmp/r.md"}, json.dumps({"content": "..."}), is_error=False,
        )
        agent._record_file_mutation_result(
            "write_file", {"path": "~/.grove/x.md", "content": "y"},
            json.dumps({"error": GOVERNED_PATH_MESSAGE}), is_error=True,
        )
        out = agent._apply_mutation_verifier("Saved and searched everything!")
        assert "Saved and searched everything!" not in out
        assert "This turn also completed" in out
        assert "search_files" in out
        assert "read_file" in out
        assert "write_file" not in out.split("This turn also completed")[1]  # blocked, not "completed"

    def test_verifier_disabled_leaves_text_unchanged(self):
        """When the verifier is off, even a governed block does not rewrite
        the response (operator opted out of the guardrail)."""
        agent = _verifier_agent()
        agent._file_mutation_verifier_enabled = lambda: False  # type: ignore[method-assign]
        agent._record_file_mutation_result(
            "write_file", {"path": "~/.grove/x.md", "content": "y"},
            json.dumps({"error": GOVERNED_PATH_MESSAGE}), is_error=True,
        )
        text = "Saved to ~/.grove/x.md"
        assert agent._apply_mutation_verifier(text) == text
