"""slow-turn-report-v1 — the turn stack sampler + its config."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from grove.turn_sampler import (
    SlowTurnConfig,
    TurnSampler,
    load_slow_turn_config,
)


def _busy_wait_in_repo_code(stop: threading.Event) -> None:
    # A frame in THIS repo (the test file lives under the repo root) whose
    # innermost frame is time.sleep — a non-idle leaf the sampler must report.
    while not stop.is_set():
        time.sleep(0.01)


def test_sampler_reports_where_a_thread_sat():
    stop = threading.Event()
    worker = threading.Thread(
        target=_busy_wait_in_repo_code, args=(stop,), name="slow-worker"
    )
    worker.start()
    sampler = TurnSampler(interval=0.02).start()
    time.sleep(0.4)
    elapsed = sampler.stop()
    stop.set()
    worker.join()

    assert elapsed >= 0.4
    report = "\n".join(sampler.report_lines())
    assert "slow-worker" in report
    assert "_busy_wait_in_repo_code" in report
    assert "test_turn_sampler.py" in report


def test_idle_threads_are_not_reported():
    parked = threading.Event()
    idle = threading.Thread(target=parked.wait, name="parked-thread")
    idle.start()
    sampler = TurnSampler(interval=0.02).start()
    time.sleep(0.2)
    sampler.stop()
    parked.set()
    idle.join()
    assert "parked-thread" not in "\n".join(sampler.report_lines())


def test_log_report_is_one_warning(caplog):
    import logging

    sampler = TurnSampler(interval=0.02).start()
    time.sleep(0.1)
    elapsed = sampler.stop()
    with caplog.at_level(logging.WARNING, logger="grove.turn_sampler"):
        sampler.log_report(elapsed, "turn platform=test")
    assert len(caplog.records) == 1
    assert "[slow-turn] turn platform=test took" in caplog.text


def test_config_defaults_and_validation(tmp_path):
    assert load_slow_turn_config(tmp_path / "missing.yaml") == SlowTurnConfig()
    cfg = tmp_path / "flywheel.config.yaml"
    cfg.write_text("slow_turn_report:\n  enabled: false\n  threshold_seconds: 3\n")
    loaded = load_slow_turn_config(cfg)
    assert loaded.enabled is False and loaded.threshold_seconds == 3.0
    for bad, key in (
        ("enabled: maybe", "enabled"),
        ("threshold_seconds: 0", "threshold_seconds"),
        ("sample_interval_seconds: -1", "sample_interval_seconds"),
    ):
        cfg.write_text("slow_turn_report:\n  " + bad + "\n")
        with pytest.raises(ValueError, match=key):
            load_slow_turn_config(cfg)


def test_repo_template_block_loads():
    template = Path(__file__).resolve().parents[2] / "config" / "flywheel.config.yaml"
    assert load_slow_turn_config(template) == SlowTurnConfig()
