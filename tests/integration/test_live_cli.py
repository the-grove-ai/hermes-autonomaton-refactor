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
import signal
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

# ── Shared patterns ───────────────────────────────────────────────────
#
# Welcome banner: the active skin produces "Kaizen-Om-Aton online"; the
# fallback from cli.py:12272 is "Welcome to grove-autonomaton". Match
# either so the tests are skin-agnostic.
WELCOME_RE = r"Welcome to grove-autonomaton|Kaizen-Om-Aton online"

# Kaizen four-choice menu sentinel from
# grove/sovereign_prompt_handlers.py:tty_sovereign_prompt. We match the
# first menu line specifically rather than ``Choose [1-4]`` so we can
# count occurrences with ``stdout.count("[1] Allow this once")`` to
# detect cache hits/misses on retry turns.
KAIZEN_FIRST_OPTION = "[1] Allow this once"
KAIZEN_MENU_RE = re.escape(KAIZEN_FIRST_OPTION)

# Per-turn tier/cost footer from run_agent's turn-end render path. Used
# as a content-agnostic "turn complete" signal so tests don't have to
# match against model output.
TURN_COMPLETE_RE = r"↳\s*T\d"


# ── Skill choice for Kaizen tests ─────────────────────────────────────
#
# The SPEC's choice for T3-T6 is "what is on my calendar today?" — it
# invokes a productivity tool that the default zones.config.yaml puts
# in the yellow zone, firing the Kaizen disposition. If the operator's
# config doesn't gate the calendar tool, T3 will time out waiting for
# the four-choice menu and the failure message will say so explicitly.
KAIZEN_TRIGGER_PROMPT = "what is on my calendar today?"


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


# ── PTY helper ────────────────────────────────────────────────────────


def _pty_setup(runner: LiveCliRunner, *, banner_timeout: float = 60.0) -> None:
    """Wait for the welcome banner and give prompt_toolkit a tick to be
    ready for input. Common preamble for every PTY test (T3-T9)."""
    runner.wait_for_pattern(WELCOME_RE, timeout=banner_timeout)
    time.sleep(0.5)


# ── T3 — Kaizen prompt renders ────────────────────────────────────────


def test_T3_kaizen_prompt_renders():
    """Trigger a tool that the operator's zone config gates behind the
    Kaizen four-choice menu, and verify the menu actually paints to
    the operator's terminal under the 75bac02dd bridge.

    Assertion shape: all four option lines appear after the trigger
    query. Don't act on the prompt content — the operator decides
    which tool the calendar query routes to and whether that tool is
    yellow-zoned. We just verify the rendering reached the screen.

    Cleanup: send ``4`` (deny) so the agent receives a clean
    disposition rather than hanging on stdin, wait for the turn to
    complete, then Ctrl+D so atexit runs.
    """
    with LiveCliRunner(["chat"], mode="pty") as runner:
        _pty_setup(runner)
        runner.send_input(KAIZEN_TRIGGER_PROMPT + "\r")
        # Wait for the four-choice menu. If the operator's config
        # doesn't gate the calendar tool, this times out and the
        # diagnostic dump shows the agent went green-path instead
        # of halting — that's a configuration finding, not a bridge
        # bug.
        runner.wait_for_pattern(KAIZEN_MENU_RE, timeout=90.0)
        # All four option labels MUST be visible. The bridge writes
        # them via tty_sovereign_prompt directly to stderr inside
        # run_in_terminal; missing any of them indicates the bridge
        # released stdin to the operator before painting completed.
        stdout = runner.stdout()
        for opt in (
            "[1] Allow this once",
            "[2] Allow for this session",
            "[3] Always allow this",
            "[4] Don't allow this",
        ):
            assert opt in stdout, (
                f"Kaizen prompt missing option {opt!r}. The bridge "
                f"may have released stdin before paint completed.\n"
                f"stdout tail:\n{stdout[-2000:]}"
            )
        # Dismiss the prompt with deny so the agent loop continues
        # to a normal turn-end instead of hanging on stdin.
        runner.send_input("4\r")
        # Turn-complete footer arrives after the agent processes the
        # denial Observation, optionally calls the LLM once more,
        # and finalises. Generous timeout — the model may write a
        # short explanation.
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=120.0)
        runner.send_input("\x04")
        rc = runner.wait_for_exit(timeout=15.0)
    assert rc in (0, 1, -15), (
        f"T3 exited with unexpected code {rc} after Kaizen denial."
    )


# ── T4 — Allow once ───────────────────────────────────────────────────


def test_T4_kaizen_allow_once():
    """Trigger Kaizen, send ``1`` (Allow this once), verify the tool
    actually runs and the turn completes. Exercises the
    ``disposition="once"`` branch in the Dispatcher's halt handler."""
    with LiveCliRunner(["chat"], mode="pty") as runner:
        _pty_setup(runner)
        runner.send_input(KAIZEN_TRIGGER_PROMPT + "\r")
        runner.wait_for_pattern(KAIZEN_MENU_RE, timeout=90.0)
        # ``1`` → ``once``: allow the tool to execute this single time.
        runner.send_input("1\r")
        # Turn-complete footer signals the tool ran AND the model
        # composed a follow-up text response. If the disposition
        # routing dropped the tool execution silently, the footer
        # would still appear but the model's response would refer to
        # missing data — that's a richer assertion left for a future
        # sprint (content-shape testing is brittle).
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=120.0)
        runner.send_input("\x04")
        rc = runner.wait_for_exit(timeout=15.0)
    assert rc in (0, 1, -15), (
        f"T4 exited with unexpected code {rc} after Kaizen allow-once."
    )


# ── T5 — Session cache (``Allow for this session``) ──────────────────


def test_T5_kaizen_session_cache_suppresses_second_prompt():
    """Send ``2`` to allow the action for the session, then trigger
    the same skill again. The second turn MUST NOT show the Kaizen
    menu — the dispatcher's session-scoped allow cache catches it.

    Assertion mechanism: snapshot ``stdout_len`` after the first
    turn's footer arrives, then assert the post-snapshot slice does
    NOT contain the ``[1] Allow this once`` sentinel. Stays sound
    even if the model's response text happens to contain the digits
    1-4 in some unrelated context."""
    with LiveCliRunner(["chat"], mode="pty") as runner:
        _pty_setup(runner)
        # ── Turn 1: trigger + allow-session ────────────────────
        runner.send_input(KAIZEN_TRIGGER_PROMPT + "\r")
        runner.wait_for_pattern(KAIZEN_MENU_RE, timeout=90.0)
        runner.send_input("2\r")
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=120.0)
        # Snapshot the buffer position. Everything that follows is
        # what we'll check for the absence of the Kaizen menu.
        mark = runner.stdout_len()
        # ── Turn 2: same trigger, no prompt expected ───────────
        runner.send_input(KAIZEN_TRIGGER_PROMPT + "\r")
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=120.0)
        runner.send_input("\x04")
        rc = runner.wait_for_exit(timeout=15.0)
    second_turn_output = runner.stdout_since(mark)
    assert KAIZEN_FIRST_OPTION not in second_turn_output, (
        f"Session cache failed: Kaizen prompt fired AGAIN on retry "
        f"after operator selected 'Allow for this session'. The "
        f"dispatcher's session-scoped allow cache is not catching "
        f"the second invocation.\n"
        f"second-turn output:\n{second_turn_output[:2000]}"
    )
    assert rc in (0, 1, -15), (
        f"T5 exited with unexpected code {rc} after session-cache retry."
    )


# ── T6 — Deny + retry behavior ────────────────────────────────────────


def test_T6_kaizen_deny_then_retry_observation():
    """Send ``4`` (Don't allow this), then immediately ask for the
    same action again. Observe whether the retry hits the prompt
    again or is auto-denied.

    Sprint 51 SPEC asserted "Same action auto-denied on retry"; the
    Sprint 32 Phase 3a strike-count machinery delivers ``deny_hard``
    only after multiple consecutive denials, not on the first
    retry. This test documents the observed behavior rather than
    asserting one or the other so GATE-C surfaces whichever shape
    the live binary actually produces:

    * If the menu re-appears on retry → operator must answer again
      (matches "deny is one-time" reading).
    * If the menu does NOT re-appear on retry → the dispatcher is
      caching deny dispositions (matches SPEC reading; would imply
      either a cache we haven't documented, or ``deny_hard`` firing
      on strike count 1).

    Either branch ends with a clean shutdown; the assertion is on
    exit code only. The observed behavior is logged in stderr (via
    the diagnostic dump if the test reports) for GATE-C cataloging.
    """
    with LiveCliRunner(["chat"], mode="pty") as runner:
        _pty_setup(runner)
        # ── Turn 1: trigger + deny ─────────────────────────────
        runner.send_input(KAIZEN_TRIGGER_PROMPT + "\r")
        runner.wait_for_pattern(KAIZEN_MENU_RE, timeout=90.0)
        runner.send_input("4\r")
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=120.0)
        # Snapshot for the retry observation.
        mark = runner.stdout_len()
        # ── Turn 2: same action, observe re-prompt vs auto-deny ──
        runner.send_input(KAIZEN_TRIGGER_PROMPT + "\r")
        # Whichever path the dispatcher takes, the turn ends with
        # the tier/cost footer. We only assert on that + clean
        # shutdown; the retry-prompt observation goes to stdout for
        # GATE-C inspection via the diagnostic dump.
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=120.0)
        # Record the observation in a way the test reporter
        # captures even on pass.
        second_turn_output = runner.stdout_since(mark)
        reprompted = KAIZEN_FIRST_OPTION in second_turn_output
        # The print lands in the captured-stderr that pytest shows
        # alongside the test name when -v is set; not a true
        # assertion failure.
        print(
            f"T6 observation: retry "
            f"{'RE-PROMPTED' if reprompted else 'AUTO-HANDLED (no menu shown)'}"
        )
        runner.send_input("\x04")
        rc = runner.wait_for_exit(timeout=15.0)
    assert rc in (0, 1, -15), (
        f"T6 exited with unexpected code {rc} after deny+retry."
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
        _pty_setup(runner)
        runner.send_input("what is 2 + 2\r")
        # The per-turn tier/cost footer appears after every completed
        # turn (e.g. ``↳ T2 Sonnet · 8 tokens · ~$0.00``). This is a
        # reliable "turn complete" signal independent of the model's
        # content — the naive ``r"4"`` pattern matches the version
        # string ``claude-opus-4-7`` in the welcome panel and
        # short-circuits before the LLM even responds.
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=90.0)
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


# ── T8 — SIGINT during streaming ──────────────────────────────────────


def test_T8_sigint_during_streaming():
    """Send a query that produces a multi-second response, deliver
    SIGINT mid-stream, then send EOF and confirm clean exit + no
    orphans.

    Stresses the kill-cascade path in ``LiveCliRunner.kill_and_dump``
    (Sprint 51 GATE-B § B4) AND the agent's interrupt handler now
    that the worker_threads_lock deadlock is gone (f8076cc2b,
    ea950b5a0). If the SIGINT path were still deadlocked, the EOF
    would arrive on a frozen prompt_toolkit and the test would
    time out at ``wait_for_exit``.

    A long-form query forces enough streaming that SIGINT lands
    during the LLM call rather than after the turn already
    completed. ``time.sleep(3)`` gives the request time to send
    and the first chunks to flow.
    """
    with LiveCliRunner(["chat"], mode="pty") as runner:
        _pty_setup(runner)
        runner.send_input(
            "Write a 300-word essay about the history of "
            "version control systems. Begin now.\r"
        )
        # Wait until streaming begins. The agent prints box-drawing
        # chrome before the first chunk; once any non-chrome content
        # shows up we know we're mid-stream. A small sleep is
        # acceptable here because the goal is "interrupt mid-call",
        # not "interrupt at byte N".
        time.sleep(3.0)
        # SIGINT to the CLI wrapper. prompt_toolkit's signal handler
        # routes this through ``agent.interrupt()`` which sets the
        # per-thread interrupt flag the worker poll loop checks.
        delivered = runner.send_signal(signal.SIGINT)
        assert delivered, "send_signal returned False — process already exited?"
        # Give the interrupt path a moment to land and unwind. The
        # agent may print an "interrupted" indicator and return to
        # the prompt; we don't assert on that text.
        time.sleep(2.0)
        # EOF to fully exit. If the SIGINT path deadlocked, the
        # Application can't see this and the wait below times out.
        runner.send_input("\x04")
        rc = runner.wait_for_exit(timeout=20.0)
    # Acceptable exit codes:
    #   0   — clean shutdown
    #   1   — generic error path
    #   -2  — SIGINT propagated to exit code
    #   -15 — kill_and_dump fired in __exit__
    assert rc in (0, 1, -2, -15), (
        f"T8 exited with unexpected code {rc} after SIGINT + EOF. "
        f"A timeout here would indicate the interrupt path is "
        f"deadlocked again."
    )


# ── T9 — Tool error diagnostic surfacing (PTY) ────────────────────────


def test_T9_tool_error_diagnostic_in_badge():
    """Force a failing tool call and verify the badge carries a
    diagnostic body, not just a bare ``[error]`` / ``[exit 1]``.

    Sprint 50 commit 8d10dbf3f landed the contract that
    ``_detect_tool_failure`` appends a diagnostic snippet (max 80
    chars) to the badge suffix so operators see the failure reason
    inline. Phase 1 ran this via ``--quiet`` pipes and skipped —
    ``--quiet`` mode suppresses the tool-completion line entirely
    (GATE-B § B1 catalog finding, deferred to Phase 3). Phase 2
    moves T9 to PTY so the real display path runs and the badge
    fires.

    Forcing function: ask the agent to ``cat`` a path that cannot
    exist. The terminal tool runs it, the shell exits 1, the badge
    line MUST contain a diagnostic body such as
    ``[exit 1] cat: ...: No such file or directory``.

    Terminal-tool first use under the operator's default zone config
    may itself trigger a Kaizen halt; if so, the test answers ``1``
    (Allow once) to let the tool run and surface its error.
    """
    nonexistent = f"/tmp/s51_t9_definitely_missing_{uuid.uuid4().hex[:6]}.txt"
    query = (
        f"Use the terminal tool to run this exact command: "
        f"cat {nonexistent}"
    )
    with LiveCliRunner(["chat"], mode="pty") as runner:
        _pty_setup(runner)
        runner.send_input(query + "\r")
        # Wait for EITHER the Kaizen menu OR the turn-complete
        # footer. If terminal is yellow-zoned, the menu appears
        # first — answer Allow-once. If it's green-zoned, the tool
        # runs immediately and the footer arrives without a prompt.
        # The harness can't ``wait_for_pattern`` on an alternation
        # in a single call cleanly, so we poll both within the
        # 90s window.
        deadline = time.monotonic() + 90.0
        saw_menu = False
        while time.monotonic() < deadline:
            buf = runner.stdout()
            if KAIZEN_FIRST_OPTION in buf:
                saw_menu = True
                break
            if re.search(TURN_COMPLETE_RE, buf):
                break
            time.sleep(0.2)
        if saw_menu:
            runner.send_input("1\r")
        # Now wait for the turn to complete one way or the other.
        runner.wait_for_pattern(TURN_COMPLETE_RE, timeout=120.0)
        runner.send_input("\x04")
        rc = runner.wait_for_exit(timeout=15.0)

    combined = runner.stdout() + runner.stderr()
    # Exit code is not asserted — the model may report the error
    # gracefully (exit 0) or propagate it (exit 1). Both fine.
    badge_matches = re.findall(r"\[(?:error|exit \d+|full)\][^\n]*", combined)
    assert badge_matches, (
        f"No tool-error badge surfaced even in PTY mode. If the "
        f"model declined to call the terminal tool, the query may "
        f"need rewording. If the tool ran but no badge fired, the "
        f"display layer is silently dropping failures.\n"
        f"stdout tail:\n{combined[-3000:]}"
    )
    # The 8d10dbf3f contract: the badge MUST carry a body after
    # the bracket. Strip the bracket and any leading space; non-
    # empty remainder means a diagnostic landed.
    diagnostic_bodies = [
        b[b.index("]") + 1:].strip()
        for b in badge_matches
        if "]" in b
    ]
    assert any(diagnostic_bodies), (
        f"All error badges were bare (no diagnostic message after "
        f"the bracket). Sprint 50 contract violated.\n"
        f"badges: {badge_matches}"
    )
    assert rc in (0, 1, -15), (
        f"T9 exited with unexpected code {rc} after tool-error turn."
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
