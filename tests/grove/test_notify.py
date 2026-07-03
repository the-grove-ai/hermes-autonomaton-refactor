"""portal-action-error-surfacing-v1 (Phase 1) — broadcast_to_operator.

Standalone: no gateway process, no deploy. The gateway runner is resolved
through ``grove.notify._resolve_gateway_runner``, which these tests
monkeypatch — so nothing here imports ``gateway`` and every case runs in
isolation. ``asyncio.run`` drives the coroutine so the suite does not
depend on any pytest-asyncio mode config.
"""

from __future__ import annotations

import asyncio
import logging

from grove import notify


# ── Test doubles ─────────────────────────────────────────────────────


class _P:
    """A stand-in Platform with a ``.value`` (exercises _platform_name)."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _P) and other.value == self.value


class _Home:
    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id


class _Result:
    def __init__(self, success: bool = True, error: str | None = None) -> None:
        self.success = success
        self.error = error


class _Adapter:
    def __init__(self, *, raises: bool = False, success: bool = True) -> None:
        self._raises = raises
        self._success = success
        self.calls: list[tuple] = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append((chat_id, content, metadata))
        if self._raises:
            raise RuntimeError("adapter boom")
        return _Result(success=self._success)


class _Config:
    def __init__(self, platforms, homes) -> None:
        self._platforms = platforms
        self._homes = homes

    def get_connected_platforms(self):
        return list(self._platforms)

    def get_home_channel(self, platform):
        return self._homes.get(platform)


class _Runner:
    def __init__(self, config, adapters) -> None:
        self.config = config
        self.adapters = adapters


def _run(coro):
    return asyncio.run(coro)


# ── Leg 1: the always-on log fires; Leg 2: each platform delivered ───


def test_log_fires_and_each_connected_platform_sent(monkeypatch, caplog):
    tg, wa = _P("telegram"), _P("whatsapp")
    tg_ad, wa_ad = _Adapter(), _Adapter()
    runner = _Runner(
        _Config([tg, wa], {tg: _Home("111"), wa: _Home("222")}),
        {tg: tg_ad, wa: wa_ad},
    )
    monkeypatch.setattr(notify, "_resolve_gateway_runner", lambda: runner)

    with caplog.at_level(logging.ERROR, logger="grove.notify"):
        summary = _run(notify.broadcast_to_operator("publish failed"))

    # Leg 1 — the always-on CLI/substrate line.
    assert "[ACTION FAILURE] publish failed" in caplog.text
    assert summary["logged"] is True
    # Leg 2 — one send per connected platform, to its home chat_id.
    assert tg_ad.calls == [("111", "publish failed", None)]
    assert wa_ad.calls == [("222", "publish failed", None)]
    assert sorted(summary["surfaces_reached"]) == ["telegram", "whatsapp"]
    assert summary["surfaces_failed"] == []


def test_metadata_and_severity_are_threaded(monkeypatch, caplog):
    tg = _P("telegram")
    ad = _Adapter()
    runner = _Runner(_Config([tg], {tg: _Home("c")}), {tg: ad})
    monkeypatch.setattr(notify, "_resolve_gateway_runner", lambda: runner)

    with caplog.at_level(logging.WARNING, logger="grove.notify"):
        _run(
            notify.broadcast_to_operator(
                "heads up", severity="warning", metadata={"thread_id": "T"}
            )
        )

    assert ad.calls == [("c", "heads up", {"thread_id": "T"})]
    rec = [r for r in caplog.records if "heads up" in r.getMessage()]
    assert rec and rec[0].levelno == logging.WARNING


# ── Fail-safe: no runner → log fires, no raise ───────────────────────


def test_none_runner_logs_and_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(notify, "_resolve_gateway_runner", lambda: None)

    with caplog.at_level(logging.ERROR, logger="grove.notify"):
        summary = _run(notify.broadcast_to_operator("something broke"))

    assert "[ACTION FAILURE] something broke" in caplog.text
    assert summary == {
        "logged": True,
        "surfaces_reached": [],
        "surfaces_failed": [],
    }


def test_no_adapters_no_raise(monkeypatch):
    tg = _P("telegram")
    # Connected per config, but no live adapter registered for it.
    runner = _Runner(_Config([tg], {tg: _Home("c")}), {})
    monkeypatch.setattr(notify, "_resolve_gateway_runner", lambda: runner)

    summary = _run(notify.broadcast_to_operator("no adapters"))
    assert summary["surfaces_reached"] == []
    assert summary["surfaces_failed"] == ["telegram"]


# ── Fail-safe: one adapter raises → the others still deliver ─────────


def test_one_adapter_raises_others_still_called(monkeypatch, caplog):
    bad, good = _P("telegram"), _P("whatsapp")
    bad_ad = _Adapter(raises=True)
    good_ad = _Adapter()
    runner = _Runner(
        _Config([bad, good], {bad: _Home("1"), good: _Home("2")}),
        {bad: bad_ad, good: good_ad},
    )
    monkeypatch.setattr(notify, "_resolve_gateway_runner", lambda: runner)

    with caplog.at_level(logging.ERROR, logger="grove.notify"):
        summary = _run(notify.broadcast_to_operator("mixed"))

    # The raising adapter did NOT abort the fan-out — the good one still ran.
    assert good_ad.calls == [("2", "mixed", None)]
    assert summary["surfaces_reached"] == ["whatsapp"]
    assert summary["surfaces_failed"] == ["telegram"]
    assert "delivery to telegram raised" in caplog.text


def test_unsuccessful_result_recorded_as_failed(monkeypatch):
    tg = _P("telegram")
    ad = _Adapter(success=False)
    runner = _Runner(_Config([tg], {tg: _Home("c")}), {tg: ad})
    monkeypatch.setattr(notify, "_resolve_gateway_runner", lambda: runner)

    summary = _run(notify.broadcast_to_operator("nope"))
    assert summary["surfaces_reached"] == []
    assert summary["surfaces_failed"] == ["telegram"]


def test_config_resolution_error_is_swallowed(monkeypatch, caplog):
    class _BadConfig:
        def get_connected_platforms(self):
            raise RuntimeError("config exploded")

    runner = _Runner(_BadConfig(), {})
    monkeypatch.setattr(notify, "_resolve_gateway_runner", lambda: runner)

    with caplog.at_level(logging.ERROR, logger="grove.notify"):
        summary = _run(notify.broadcast_to_operator("still fine"))

    # Log leg stood; fan-out failure never propagated.
    assert "[ACTION FAILURE] still fine" in caplog.text
    assert summary["logged"] is True
    assert summary["surfaces_reached"] == []
