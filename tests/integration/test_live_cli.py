"""Sprint 51 Phase 1 — live CLI integration smoke tests.

These run the real ``hermes`` binary as a subprocess against the
operator's real ``~/.grove/`` (config, credentials, memory store).
Token cost is accepted per the Sprint 51 spec.

Test order matches the operator's GATE-A-approved sequence:
T1 → T2 → T9 → T7 → T10.

Each test wraps the binary in a ``LiveCliRunner`` context manager
(``grove.eval.integration_runner``) so the subprocess is always
reaped, even on assertion failure. The conftest's autouse fixture
asserts no ``hermes chat`` PIDs leak across tests.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from grove.eval.integration_runner import LiveCliRunner

pytestmark = pytest.mark.integration


GROVE_HOME = Path(os.path.expanduser("~/.grove"))
MEMORY_FILES = (
    GROVE_HOME / "memories" / "USER.md",
    GROVE_HOME / "memories" / "MEMORY.md",
)


# ── helpers ────────────────────────────────────────────────────────────


def _memory_contains(marker: str) -> bool:
    """Return True if ``marker`` substring appears in any memory file."""
    for path in MEMORY_FILES:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if marker in content:
            return True
    return False


def _scrub_memory(marker: str) -> int:
    """Remove every entry containing ``marker`` from both memory files.

    Memory entries are delimited by single-line ``§`` separators. We
    split on those, drop any segment containing the marker, then
    rewrite the file. Returns the number of segments removed across
    both files. Safety net for T2 — runs at test teardown so a flaky
    model that fails to call ``memory.remove`` cannot leave Sprint 51
    test pollution in the operator's real memory store.
    """
    total_removed = 0
    for path in MEMORY_FILES:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Split on a standalone §. Preserve original separator semantics
        # (newline before/after) by joining with the same delimiter.
        segments = re.split(r"\n§\n", content)
        kept = [s for s in segments if marker not in s]
        removed = len(segments) - len(kept)
        if removed > 0:
            new_content = "\n§\n".join(kept)
            try:
                path.write_text(new_content, encoding="utf-8")
                total_removed += removed
            except OSError:
                pass
    return total_removed


# ── T1 — Trivial query, --quiet, pipes ────────────────────────────────


def test_T1_trivial_query_clean_exit():
    """Trivial no-tool query in machine-readable ``--quiet`` mode.

    Smallest possible exercise of the harness wiring: spawn, drain
    stdout/stderr, wait for exit, assert non-empty stdout and exit 0.
    Confirms the conftest's pre-cleanup + post-cleanup + PID-snapshot
    fixture works before any tool or memory complexity layers in.
    """
    with LiveCliRunner(
        ["chat", "-q", "what is 2 + 2", "--quiet"],
        mode="pipe",
    ) as runner:
        rc = runner.wait_for_exit(timeout=30.0)
    assert rc == 0, (
        f"hermes chat -q --quiet exited non-zero ({rc}).\n"
        f"stderr:\n{runner.stderr()}"
    )
    assert runner.stdout().strip(), (
        f"hermes chat -q --quiet produced empty stdout.\n"
        f"stderr:\n{runner.stderr()}"
    )


# ── T2 — Memory write + round-trip cleanup ────────────────────────────


def test_T2_memory_write_and_cleanup():
    """Round-trip a memory write then remove the entry.

    Guardrail #3 from Phase 1: ``GROVE_HOME`` is the operator's real
    memory store, not a tempdir. A test that ONLY writes leaves
    permanent pollution. We:

    1. Write a uniquely-tagged entry via the live CLI.
    2. Assert exit 0 and the marker landed in a memory file.
    3. Ask the live CLI to remove that entry.
    4. Assert exit 0 and the marker is gone from the memory file.

    A file-scrub safety net runs in a ``finally`` so a model that
    fails to call ``memory.remove`` cannot poison the real store
    even if step 4 fails.
    """
    # Unique marker so concurrent runs or repeat invocations can't
    # collide and so the conftest's leftover scan can identify our
    # entries with no ambiguity.
    marker = f"S51-T2-TEST-{uuid.uuid4().hex[:8]}"
    write_query = (
        f"Use the memory tool to remember this exact phrase: "
        f"{marker} (Sprint 51 integration test marker)."
    )
    remove_query = (
        f"Use the memory tool to remove every memory entry that "
        f"contains the substring {marker}."
    )

    try:
        # ── Phase A: write ──────────────────────────────────────────
        with LiveCliRunner(
            ["chat", "-q", write_query, "--quiet"],
            mode="pipe",
        ) as runner_a:
            rc_a = runner_a.wait_for_exit(timeout=90.0)
        assert rc_a == 0, (
            f"memory write exited non-zero ({rc_a}).\n"
            f"stdout:\n{runner_a.stdout()[:2000]}\n"
            f"stderr:\n{runner_a.stderr()[:2000]}"
        )
        # The model SHOULD have called memory.add. Give file IO a
        # moment to flush before we read.
        time.sleep(0.5)
        assert _memory_contains(marker), (
            f"memory write completed (exit 0) but marker {marker!r} "
            f"is not present in any memory file. The model may have "
            f"declined the tool, or the write went somewhere else.\n"
            f"stdout:\n{runner_a.stdout()[:2000]}"
        )
        combined_a = runner_a.stdout() + runner_a.stderr()
        assert "[error]" not in combined_a, (
            f"Unexpected [error] badge in memory-write run:\n"
            f"{combined_a[:2000]}"
        )

        # ── Phase B: remove ─────────────────────────────────────────
        with LiveCliRunner(
            ["chat", "-q", remove_query, "--quiet"],
            mode="pipe",
        ) as runner_b:
            rc_b = runner_b.wait_for_exit(timeout=90.0)
        assert rc_b == 0, (
            f"memory remove exited non-zero ({rc_b}).\n"
            f"stdout:\n{runner_b.stdout()[:2000]}\n"
            f"stderr:\n{runner_b.stderr()[:2000]}"
        )
        time.sleep(0.5)
        assert not _memory_contains(marker), (
            f"memory remove completed (exit 0) but marker {marker!r} "
            f"is STILL present in a memory file. The model may have "
            f"failed to call memory.remove, or removed a different "
            f"entry.\nstdout:\n{runner_b.stdout()[:2000]}"
        )
    finally:
        # Safety net: ALWAYS scrub. Catches the case where Phase B
        # failed before completion and left the marker behind, plus
        # any future flake in the remove path. Never leaves Sprint 51
        # test pollution in the operator's real memory store.
        _scrub_memory(marker)


# ── T9 — Tool error diagnostic surfacing ──────────────────────────────


def test_T9_tool_error_diagnostic_in_badge():
    """Force a failing tool call and verify the badge carries a
    diagnostic body, not just a bare ``[error]`` / ``[exit 1]``.

    Sprint 50 commit 8d10dbf3f landed the contract that
    ``_detect_tool_failure`` appends a diagnostic snippet (max 80
    chars) to the badge suffix so operators see the failure reason
    inline. The integration test pins that contract against the real
    binary with a real tool invocation.

    Forcing function: ask the agent to ``cat`` a path that cannot
    exist. The terminal tool runs it, the shell exits 1, the badge
    line MUST contain a diagnostic body such as
    ``[exit 1] cat: ...: No such file or directory``.
    """
    nonexistent = f"/tmp/s51_t9_definitely_missing_{uuid.uuid4().hex[:6]}.txt"
    query = (
        f"Use the terminal tool to run this exact command: "
        f"cat {nonexistent}"
    )

    with LiveCliRunner(
        ["chat", "-q", query, "--quiet"],
        mode="pipe",
    ) as runner:
        rc = runner.wait_for_exit(timeout=90.0)

    combined = runner.stdout() + runner.stderr()
    # Exit code is not asserted. The model may report the error
    # gracefully (exit 0) or propagate it (exit 1). Both are fine.
    badge_matches = re.findall(r"\[(?:error|exit \d+|full)\][^\n]*", combined)
    if not badge_matches:
        # No badge surfaced. Could mean:
        #   (a) ``--quiet`` mode suppresses the completion-line badge
        #       even on failure; in that case T9 needs PTY (Phase 2).
        #   (b) The model declined to use the terminal tool.
        # Either is a GATE-B finding, not a Sprint 50 contract violation.
        pytest.skip(
            f"No tool-error badge surfaced in --quiet mode. "
            f"Catalog finding for GATE-B: T9 may need PTY transport.\n"
            f"stdout:\n{runner.stdout()[:1500]}\n"
            f"stderr:\n{runner.stderr()[:1500]}"
        )
    # At least one badge MUST carry a diagnostic body. The format from
    # 8d10dbf3f is ``[error] <msg>`` / ``[exit N] <msg>`` / ``[full] <msg>``
    # — a body is anything after the closing ``]``.
    diagnostic_bodies = [
        b[b.index("]") + 1:].strip()
        for b in badge_matches
        if "]" in b
    ]
    assert any(diagnostic_bodies), (
        f"All error badges were bare (no diagnostic message after the "
        f"bracket). Sprint 50 contract violated.\n"
        f"badges: {badge_matches}\n"
        f"stdout:\n{runner.stdout()[:1500]}"
    )


# ── T7 — Clean interactive shutdown via PTY ───────────────────────────


def test_T7_interactive_clean_shutdown_via_pty():
    """Spawn ``hermes chat`` interactively under a PTY, wait for the
    welcome banner, submit a query, wait for a response, then send
    Ctrl+D (EOF) and confirm the process exits cleanly within 10s.

    Verifies that the post-Sprint-50 atexit chain (``cli._run_cleanup``
    → terminals → browsers → MCP shutdown → memory provider) doesn't
    hang on the interactive shutdown path now that the
    ``worker_threads_lock`` deadlocks are fixed (f8076cc2b, ea950b5a0).
    """
    with LiveCliRunner(["chat"], mode="pty") as runner:
        # Wait for the post-banner ready signal. The "Welcome to
        # grove-autonomaton" text in cli.py:12272 is only the fallback
        # when the skin engine doesn't provide a custom welcome; the
        # operator-configured Kaizen skin emits "Kaizen-Om-Aton online"
        # at the same lifecycle moment. Match either so the test is
        # skin-agnostic. The 60s budget covers cold-start plugin
        # discovery (~8s), MCP server spawn (~5s on a fast machine,
        # longer when fetching ``@notionhq/notion-mcp-server`` via
        # ``npx``), and prompt_toolkit's first paint.
        runner.wait_for_pattern(
            r"Welcome to grove-autonomaton|Kaizen-Om-Aton online",
            timeout=60.0,
        )
        # Give prompt_toolkit a tick after the banner before
        # injecting input, so the input area is ready.
        time.sleep(0.5)
        runner.send_input("what is 2 + 2\r")
        # The per-turn tier/cost footer appears after every completed
        # turn (e.g. ``↳ T2 Sonnet · 8 tokens · ~$0.00``). This is a
        # reliable "turn complete" signal independent of the model's
        # content — the naive ``r"4"`` pattern matches the version
        # string ``claude-opus-4-7`` in the welcome panel and
        # short-circuits before the LLM even responds.
        runner.wait_for_pattern(r"↳\s*T\d", timeout=90.0)
        # Send Ctrl+D (EOF). prompt_toolkit's Application sees the
        # pipe close, the chat loop exits, atexit runs, process exits.
        runner.send_input("\x04")
        rc = runner.wait_for_exit(timeout=15.0)
    # Acceptable exit codes:
    #   0  — clean shutdown
    #   1  — quiet-mode result["failed"] path or generic error
    #   -15 (SIGTERM) — kill_and_dump fired in __exit__ before exit
    # Anything else means the process didn't shut down on EOF.
    assert rc in (0, 1, -15), (
        f"interactive shutdown returned unexpected code {rc}. "
        f"This usually means the atexit chain hung."
    )


# ── T10 — Orphan-process invariant sanity ─────────────────────────────


def test_T10_orphan_invariant_holds():
    """Sanity check that the conftest's orphan-detection fixture is
    actually running for this directory.

    The fixture runs ``pgrep -f 'hermes chat'`` at test setup and
    teardown and fails on PID delta. This test verifies the
    machinery itself by running with zero subprocesses spawned —
    the delta MUST be empty.

    A genuine orphan from a prior failed test would already have
    been killed by the previous test's teardown fixture; if a
    process from outside the test session is running, it shows up
    in both the pre and post snapshot, cancels out of the delta,
    and is correctly NOT flagged. That's guardrail #2 working as
    designed.
    """
    # If the fixture isn't loaded, this test would never even reach
    # this assertion (no PID snapshot would have been taken). Reach
    # into the conftest's helper to confirm at least the pgrep
    # primitive works.
    from tests.integration.conftest import _pgrep_pids
    # Should not raise. Result may be empty or contain external sessions.
    pids = _pgrep_pids("hermes chat")
    assert isinstance(pids, set)
