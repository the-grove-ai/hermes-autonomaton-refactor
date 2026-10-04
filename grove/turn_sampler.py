"""slow-turn-report-v1 — say WHERE a slow turn spent its time.

Pipeline stage: Telemetry. A turn that takes longer than the operator's
threshold is an abnormality (Digital Jidoka): surface diagnostic context, do
not leave the operator guessing at "it feels slow".

While a gateway turn runs, a daemon thread samples every Python thread's stack
at a fixed interval. When the turn ends over threshold, the samples are folded
into a timeline — for each place the code sat, the seconds it was first and
last seen there, the nearest frame in THIS codebase, and the innermost frame
(the thing actually being waited on: a socket read, a lock, a subprocess).
Under threshold, nothing is logged.

Declarative: the ``slow_turn_report`` block of ``~/.grove/flywheel.config.yaml``
(template in ``config/``). Absent → enabled, 10s threshold, 0.5s interval.

Observation only — the sampler never touches the turn's state, and a sampler
failure is logged and dropped, never raised into the turn.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["SlowTurnConfig", "TurnSampler", "load_slow_turn_config"]

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# Hard stop — a sampler whose turn never reports back must not sample forever.
_MAX_SAMPLE_SECONDS = 600.0

# Report size: places seen for fewer samples than this are noise.
_MIN_SAMPLES = 2
_MAX_LINES = 30

# Innermost frames that mean "parked, waiting for work" — a thread sitting here
# is idle, not slow. A turn thread blocked on a child thread shows up through
# the CHILD's stack instead (the socket read, the subprocess wait).
_IDLE_LEAVES = frozenset({
    ("threading.py", "wait"),
    ("threading.py", "_wait_for_tstate_lock"),
    ("threading.py", "join"),
    ("selectors.py", "select"),
    ("queue.py", "get"),
    ("thread.py", "_worker"),
    ("base_events.py", "_run_once"),
})


@dataclass(frozen=True)
class SlowTurnConfig:
    enabled: bool = True
    threshold_seconds: float = 10.0
    sample_interval_seconds: float = 0.5


def load_slow_turn_config(config_path: Optional[Path] = None) -> SlowTurnConfig:
    """Load ``slow_turn_report`` from ``~/.grove/flywheel.config.yaml``.

    Absent file/block → defaults. A PRESENT key is validated fail-loud."""
    import yaml

    if config_path is None:
        from hermes_constants import get_hermes_home

        config_path = Path(get_hermes_home()) / "flywheel.config.yaml"
    if not config_path.exists():
        return SlowTurnConfig()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    block = raw.get("slow_turn_report")
    if not isinstance(block, dict):
        return SlowTurnConfig()
    where = "flywheel.config.yaml slow_turn_report"

    enabled = block.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled must be true or false, got {enabled!r}")

    def _pos(key: str, default: float) -> float:
        v = block.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"{where}.{key} must be a number > 0, got {v!r}")
        return float(v)

    return SlowTurnConfig(
        enabled=enabled,
        threshold_seconds=_pos("threshold_seconds", 10.0),
        sample_interval_seconds=_pos("sample_interval_seconds", 0.5),
    )


def _short(path: str) -> str:
    """Repo-relative path for our code, bare filename for everything else."""
    if path.startswith(_REPO_ROOT) and "site-packages" not in path:
        return path[len(_REPO_ROOT):].lstrip("/")
    return Path(path).name


def _is_repo(path: str) -> bool:
    return path.startswith(_REPO_ROOT) and "site-packages" not in path


# One sampled place: (thread name, nearest repo frame, innermost frame).
_Key = Tuple[str, str, str]


class TurnSampler:
    """Sample all thread stacks for the life of one turn."""

    def __init__(self, interval: float = 0.5) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._started = 0.0
        # key -> [samples, first_seen_s, last_seen_s]
        self._seen: Dict[_Key, List[float]] = {}
        self._samples = 0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "TurnSampler":
        self._started = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="grove-turn-sampler", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> float:
        """Stop sampling; returns the elapsed seconds."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return time.monotonic() - self._started

    # ── sampling ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        own = threading.get_ident()
        try:
            while not self._stop.wait(self._interval):
                elapsed = time.monotonic() - self._started
                if elapsed > _MAX_SAMPLE_SECONDS:
                    return
                self._sample(own, elapsed)
        except Exception as exc:  # observation must never hurt the turn
            logger.warning("[turn-sampler] sampling stopped: %r", exc)

    def _sample(self, own: int, elapsed: float) -> None:
        names = {t.ident: t.name for t in threading.enumerate()}
        self._samples += 1
        for tid, frame in sys._current_frames().items():
            if tid == own:
                continue
            leaf_code = frame.f_code
            leaf = (Path(leaf_code.co_filename).name, leaf_code.co_name)
            if leaf in _IDLE_LEAVES:
                continue
            repo_frame = ""
            f = frame
            while f is not None:
                if _is_repo(f.f_code.co_filename):
                    repo_frame = (
                        f"{_short(f.f_code.co_filename)}:{f.f_lineno} "
                        f"{f.f_code.co_name}"
                    )
                    break
                f = f.f_back
            if not repo_frame:
                continue  # a thread with none of our code on its stack
            key: _Key = (
                names.get(tid, str(tid)),
                repo_frame,
                f"{_short(leaf_code.co_filename)}:{frame.f_lineno} "
                f"{leaf_code.co_name}",
            )
            entry = self._seen.get(key)
            if entry is None:
                self._seen[key] = [1, elapsed, elapsed]
            else:
                entry[0] += 1
                entry[2] = elapsed

    # ── report ────────────────────────────────────────────────────────────

    def report_lines(self) -> List[str]:
        """The timeline, earliest first. Each line: when it was seen there,
        for roughly how long, on which thread, in which of our functions, and
        what the innermost frame was doing."""
        rows = [
            (first, last, int(n), key)
            for key, (n, first, last) in self._seen.items()
            if n >= _MIN_SAMPLES
        ]
        rows.sort(key=lambda r: (r[0], -r[2]))
        lines = []
        for first, last, n, (thread, repo_frame, leaf) in rows[:_MAX_LINES]:
            lines.append(
                f"  t={first:5.1f}-{last:5.1f}s  ~{n * self._interval:4.1f}s  "
                f"[{thread}]  {repo_frame}  <-  {leaf}"
            )
        if len(rows) > _MAX_LINES:
            lines.append(f"  … {len(rows) - _MAX_LINES} more place(s) not shown")
        return lines

    def log_report(self, elapsed: float, label: str) -> None:
        lines = self.report_lines()
        logger.warning(
            "[slow-turn] %s took %.1fs (%d samples at %.1fs). Where the time "
            "went — time window, approx seconds, [thread], our code  <-  what "
            "it was waiting in:\n%s",
            label, elapsed, self._samples, self._interval,
            "\n".join(lines) if lines else "  (no busy stacks captured)",
        )
