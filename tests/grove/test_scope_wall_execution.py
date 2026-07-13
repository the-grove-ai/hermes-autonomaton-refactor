"""routing-scope-wall-v1 R-W5 — execution-time TOCTOU re-verification.

The write executor (write_file / patch) re-checks is_scope_defining immediately
before the physical write and halts LOUD (TerminalGovernanceHalt, never
swallowed) unless an operator-approved RED re-dispatch matches this write's
realpath-canonical effect signature (grove.red_execution_context.approved_effect_var
— the same mechanism the terminal guard consumes). The realpath-canonical
signature mismatch — not a path-string compare — is the symlink-swap detector.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    g = tmp_path / "grove"
    g.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(g))
    # pytest's tmp lives under /private/var/folders, which _check_sensitive_path
    # flags as a system path — that unrelated wall would refuse the write before
    # the scope-wall guard runs. Neutralize it here (in production ~/.grove is not
    # a sensitive system path, so the guard is reached). The sensitive-path wall
    # is tested separately.
    import tools.file_tools as _ft
    monkeypatch.setattr(_ft, "_check_sensitive_path", lambda *a, **k: None)
    return g


def test_scope_defining_write_file_without_approval_halts(grove_home):
    from tools.file_tools import write_file_tool
    from grove.governance_halt import TerminalGovernanceHalt
    target = str(grove_home / "routing.config.yaml")
    with pytest.raises(TerminalGovernanceHalt):
        write_file_tool(target, "evil: true")
    assert not Path(target).exists()  # the write did not execute


def test_scope_defining_patch_without_approval_halts(grove_home):
    from tools.file_tools import patch_tool
    from grove.governance_halt import TerminalGovernanceHalt
    target = grove_home / "zones.schema.yaml"
    target.write_text("v: 0\n")
    with pytest.raises(TerminalGovernanceHalt):
        patch_tool(mode="replace", path=str(target), old_string="v: 0", new_string="v: 9")
    assert target.read_text() == "v: 0\n"  # unchanged


def test_scope_defining_write_with_matching_approval_proceeds(grove_home):
    from tools.file_tools import write_file_tool
    from grove.red_execution_context import approved_effect_var
    from grove.effect_signature import canonical_effect_signature
    target = str(grove_home / "routing.config.yaml")
    content = "schema_version: 1\n"
    sig = canonical_effect_signature("write_file", {"path": target, "content": content})
    tok = approved_effect_var.set(sig)
    try:
        result = write_file_tool(target, content)  # must NOT raise TerminalGovernanceHalt
    finally:
        approved_effect_var.reset(tok)
    assert "bytes_written" in result  # guard allowed the approved RED re-dispatch


def test_nonscope_write_unaffected_by_guard(grove_home):
    from tools.file_tools import write_file_tool
    (grove_home / "memory").mkdir()
    target = str(grove_home / "memory" / "note.txt")
    result = write_file_tool(target, "hi")  # guard is a no-op for non-scope targets
    assert "bytes_written" in result


def test_mismatched_approval_still_halts(grove_home):
    from tools.file_tools import write_file_tool
    from grove.red_execution_context import approved_effect_var
    from grove.governance_halt import TerminalGovernanceHalt
    target = str(grove_home / "routing.config.yaml")
    tok = approved_effect_var.set("some-other-effect-signature")
    try:
        with pytest.raises(TerminalGovernanceHalt):
            write_file_tool(target, "x")
    finally:
        approved_effect_var.reset(tok)
    assert not Path(target).exists()


def test_symlink_swap_signature_mismatch_halts(grove_home, tmp_path):
    # The realpath-canonical signature is the detector: an approval minted while
    # the path resolved to routing.config.yaml does NOT authorize a write after
    # the path is swapped to a DIFFERENT scope-defining target (zones.schema.yaml).
    from tools.file_tools import write_file_tool
    from grove.red_execution_context import approved_effect_var
    from grove.effect_signature import canonical_effect_signature
    from grove.governance_halt import TerminalGovernanceHalt
    (grove_home / "routing.config.yaml").write_text("a")
    (grove_home / "zones.schema.yaml").write_text("b")
    link = tmp_path / "cfg-link"  # outside grove; realpath decides scope membership
    link.symlink_to(grove_home / "routing.config.yaml")
    sig = canonical_effect_signature("write_file", {"path": str(link), "content": "x"})
    link.unlink()
    link.symlink_to(grove_home / "zones.schema.yaml")  # late swap after approval
    tok = approved_effect_var.set(sig)
    try:
        with pytest.raises(TerminalGovernanceHalt):
            write_file_tool(str(link), "x")
    finally:
        approved_effect_var.reset(tok)
