"""Sprint 51 — Live CLI integration runner.

Spawns the real ``hermes`` binary as a subprocess and drives it as-if a
human were typing. Two transport modes:

* ``mode="pipe"`` — ``subprocess.Popen`` with ``stdin=DEVNULL`` and
  ``stdout/stderr=PIPE``. Suitable for ``hermes chat -q '<query>'
  --quiet`` invocations which bypass ``prompt_toolkit.Application``
  entirely (see Sprint 51 GATE-A § D2). Drainer threads pump output
  into thread-safe buffers.

* ``mode="pty"`` — ``pty.openpty()`` master/slave pair. ``prompt_toolkit``
  detects a TTY via ``sys.stdin.isatty()`` and starts its
  ``Vt100Input``; raw mode + status bar + key bindings all activate.
  Required for any test that interacts with the interactive CLI
  (Kaizen disposition prompt, ``/exit`` slash command, SIGINT during
  streaming). The ``_sovereign_prompt_callback`` bridge that shipped in
  commit 75bac02dd routes the Kaizen ``input()`` through
  ``run_in_terminal``, which puts stdin into cooked mode — operator
  keystrokes written to the PTY master arrive at that ``input()``
  cleanly.

Every wait operation enforces a hard wall-clock timeout. On expiry,
``kill_and_dump`` SIGTERMs and then SIGKILLs the entire process group
(``os.killpg``) so MCP children spawned via ``npm exec`` and their
grandchildren get reaped, not just the CLI itself. The raised
``LiveCliTimeout`` carries the last 50 lines of stdout and stderr so
test failures point straight at the stalling step.

Environment: the harness always overrides ``GROVE_HOME`` to the real
``~/.grove/`` (the test suite's global conftest hermetics that to a
per-test tempdir; the live binary needs the operator's real config,
.env, and provider credentials). All other env vars pass through.
"""

from __future__ import annotations

import os
import pty
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Literal, Optional, Union


class LiveCliTimeout(RuntimeError):
    """Raised when a harness I/O operation exceeds its hard timeout.

    The message includes a diagnostic dump (returncode, mode, last 50
    lines of stdout and stderr) so failures don't require attaching a
    debugger to figure out where the binary stalled.
    """


class LiveCliRunner:
    """Drives a live ``hermes`` subprocess for integration tests."""

    def __init__(
        self,
        args: List[str],
        *,
        mode: Literal["pipe", "pty"] = "pipe",
        env_overrides: Optional[dict] = None,
        binary: Optional[str] = None,
    ) -> None:
        self.args = list(args)
        self.mode = mode
        self.env_overrides = dict(env_overrides or {})
        self.binary = binary or self._default_binary()

        self.process: Optional[subprocess.Popen] = None
        self._master_fd: Optional[int] = None
        self._stdout_buf: List[str] = []
        self._stderr_buf: List[str] = []
        self._buf_lock = threading.Lock()
        self._reader_threads: List[threading.Thread] = []

    # ── construction helpers ──────────────────────────────────────────

    @staticmethod
    def _default_binary() -> str:
        """Prefer ``.venv/bin/hermes`` from the repo root; fall back to PATH.

        The repo's project-local virtual env (built per the
        ``grove-autonomaton-hermes`` CLAUDE.md instructions) is the canonical
        binary for live CLI testing. PATH-resolved ``hermes`` is the fallback
        for environments where the venv isn't yet built.
        """
        repo_root = Path(__file__).resolve().parents[2]
        venv_hermes = repo_root / ".venv" / "bin" / "hermes"
        if venv_hermes.exists():
            return str(venv_hermes)
        return "hermes"

    def _subprocess_env(self) -> dict:
        """Compose the env passed to the subprocess.

        The test-suite global conftest sets ``GROVE_HOME`` to a per-test
        tempdir for hermetic isolation. The live CLI integration tests
        explicitly opt out of that isolation — they need the operator's
        real ``~/.grove/`` so ``load_hermes_dotenv`` picks up provider
        credentials and the real ``config.yaml``.
        """
        env = dict(os.environ)
        env["GROVE_HOME"] = os.path.expanduser("~/.grove")
        env["HOME"] = os.path.expanduser("~")
        # Force unbuffered stdio in the child so drainer threads see
        # output as it streams instead of waiting for block flushes.
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self.env_overrides)
        return env

    # ── start / IO ────────────────────────────────────────────────────

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("LiveCliRunner already started")
        argv = [self.binary, *self.args]
        env = self._subprocess_env()

        if self.mode == "pipe":
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                preexec_fn=os.setsid,
                bufsize=1,
                text=True,
            )
            self._spawn_pipe_reader(
                self.process.stdout, self._stdout_buf, "stdout",
            )
            self._spawn_pipe_reader(
                self.process.stderr, self._stderr_buf, "stderr",
            )
        else:
            # PTY mode FD discipline (Sprint 51 Phase 1 guardrail #1):
            # ``pty.openpty`` returns two open FDs. The slave belongs to
            # the child once ``Popen`` dups it onto stdin/stdout/stderr,
            # so the parent MUST ``os.close(slave)`` — otherwise the
            # parent keeps the slave open and reads from the master
            # never return EOF when the child exits. The master FD is
            # closed by ``kill_and_dump`` in ``__exit__``. Both closes
            # live in ``try`` blocks below so a Popen failure on the way
            # to ``os.close(slave)`` doesn't leak the master either.
            master, slave = pty.openpty()
            self._master_fd = master
            try:
                self.process = subprocess.Popen(
                    argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    preexec_fn=os.setsid,
                    close_fds=True,
                )
            except BaseException:
                # Popen raised before forking the child — close both ends
                # so neither FD leaks across the test suite.
                try:
                    os.close(slave)
                except OSError:
                    pass
                try:
                    os.close(master)
                except OSError:
                    pass
                self._master_fd = None
                raise
            # Popen succeeded; the slave is now duped into the child's
            # std fds. Close the parent's copy so master reads see EOF
            # when the child exits.
            try:
                os.close(slave)
            except OSError:
                pass
            self._spawn_master_reader(master, self._stdout_buf)

    def _spawn_pipe_reader(
        self, stream, buf: List[str], name: str,
    ) -> None:
        def _drain():
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    with self._buf_lock:
                        buf.append(line)
            except (OSError, ValueError):
                pass

        t = threading.Thread(target=_drain, daemon=True, name=f"_reader-{name}")
        t.start()
        self._reader_threads.append(t)

    def _spawn_master_reader(self, fd: int, buf: List[str]) -> None:
        """Read PTY master end into the buffer line-by-line.

        ``os.read`` returns raw bytes that may not align with line
        boundaries; we accumulate a partial-line tail across reads so
        ``wait_for_pattern`` sees complete lines.
        """
        def _drain():
            partial = ""
            try:
                while True:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        return
                    if not chunk:
                        return
                    text = partial + chunk.decode("utf-8", errors="replace")
                    parts = text.split("\n")
                    partial = parts[-1]
                    for ln in parts[:-1]:
                        with self._buf_lock:
                            buf.append(ln + "\n")
            finally:
                if partial:
                    with self._buf_lock:
                        buf.append(partial)

        t = threading.Thread(target=_drain, daemon=True, name="_reader-master")
        t.start()
        self._reader_threads.append(t)

    # ── send input ────────────────────────────────────────────────────

    def send_input(self, text: str, *, timeout: float = 10.0) -> None:
        """Write text to the child's stdin.

        Only valid in ``mode="pty"`` — in ``mode="pipe"`` stdin is
        ``DEVNULL`` and there's no way to inject keystrokes (which is
        appropriate for ``--quiet`` mode tests that don't need input).
        Newlines are caller-controlled — ``send_input("1\\r")`` sends
        the digit plus a carriage return, ``send_input("\\x04")`` sends
        Ctrl+D / EOF.
        """
        if self.process is None:
            raise RuntimeError("LiveCliRunner.send_input: not started")
        if self.mode == "pipe":
            raise RuntimeError(
                "LiveCliRunner.send_input: pipe mode has stdin=DEVNULL; "
                "use mode='pty' for tests that send input"
            )
        data = text.encode("utf-8") if isinstance(text, str) else text
        deadline = time.monotonic() + timeout
        while data:
            if time.monotonic() > deadline:
                raise LiveCliTimeout(
                    f"send_input timed out after {timeout:.1f}s. "
                    f"{len(data)} bytes unwritten.\n{self._diag_dump()}"
                )
            try:
                n = os.write(self._master_fd, data)  # type: ignore[arg-type]
            except OSError as exc:
                raise LiveCliTimeout(
                    f"send_input write failed: {exc}\n{self._diag_dump()}"
                )
            data = data[n:]

    # ── waits ─────────────────────────────────────────────────────────

    def wait_for_pattern(
        self,
        pattern: Union[str, re.Pattern],
        *,
        timeout: float = 60.0,
        stream: Literal["stdout", "stderr", "either"] = "either",
    ) -> re.Match:
        """Poll the captured output until ``pattern`` matches or timeout fires.

        ``stream="either"`` is the default because the PTY merges
        stdout/stderr onto the master fd, and ``--quiet`` mode writes
        the session id and tier summary to stderr. Tests don't usually
        care which side carries the marker.
        """
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._buf_lock:
                if stream in ("stdout", "either"):
                    m = regex.search("".join(self._stdout_buf))
                    if m:
                        return m
                if stream in ("stderr", "either"):
                    m = regex.search("".join(self._stderr_buf))
                    if m:
                        return m
            time.sleep(0.1)
        raise LiveCliTimeout(
            f"wait_for_pattern({pattern!r}) timed out after {timeout:.1f}s.\n"
            f"{self._diag_dump()}"
        )

    def wait_for_exit(self, *, timeout: float = 30.0) -> int:
        """Block until the child exits or timeout fires. Returns the exit code."""
        if self.process is None:
            return -1
        try:
            rc = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise LiveCliTimeout(
                f"wait_for_exit timed out after {timeout:.1f}s.\n"
                f"{self._diag_dump()}"
            )
        # Drain readers briefly so post-exit output is captured before
        # the test reads .stdout() / .stderr().
        for t in self._reader_threads:
            t.join(timeout=1.0)
        return rc

    # ── kill + dump ───────────────────────────────────────────────────

    def kill_and_dump(self) -> str:
        """SIGTERM → 2s grace → SIGKILL the process group. Always safe to call.

        Returns the diagnostic dump string for inclusion in test failure
        messages. Idempotent — calling on an already-exited process is
        a no-op for the kill side.
        """
        diag = self._diag_dump()
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        return diag

    def _diag_dump(self) -> str:
        with self._buf_lock:
            stdout_tail = "".join(self._stdout_buf[-50:])
            stderr_tail = "".join(self._stderr_buf[-50:])
        rc = self.process.poll() if self.process else None
        return (
            f"--- LiveCliRunner diagnostic ---\n"
            f"argv: {[self.binary, *self.args]}\n"
            f"mode: {self.mode}\n"
            f"returncode: {rc}\n"
            f"--- stdout (last 50 lines) ---\n{stdout_tail}\n"
            f"--- stderr (last 50 lines) ---\n{stderr_tail}\n"
            f"--- end diagnostic ---\n"
        )

    # ── accessors ─────────────────────────────────────────────────────

    def stdout(self) -> str:
        with self._buf_lock:
            return "".join(self._stdout_buf)

    def stderr(self) -> str:
        with self._buf_lock:
            return "".join(self._stderr_buf)

    # ── context manager ──────────────────────────────────────────────

    def __enter__(self) -> "LiveCliRunner":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Always reap, even on test failure. Never leak the subprocess
        # or the PTY master FD across the test suite (Sprint 51 Phase 1
        # guardrail #1).
        try:
            self.kill_and_dump()
        finally:
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
                self._master_fd = None
