"""Sprint 49 — T0 Pattern Cache live integration (T21-T23).

These run the REAL ``hermes`` binary as a subprocess and prove the T0
execution path end-to-end: a cache hit serves a compiled pattern with NO
model call, a demoted pattern falls back to a real T1 inference, and the
operator stats surface reflects the cache.

Isolation: each test points ``GROVE_HOME`` at a per-test tempdir (the binary
reads provider credentials from the inherited ``ANTHROPIC_API_KEY`` env, not
from the home), so nothing touches the operator's real ``~/.grove``. Token
cost is nominal and accepted (one T1 Haiku turn in T21 + T22's fallback).

The proof is a canary: the seeded ``cached_response`` is a fixed sentinel
string no model would ever emit for the query. If stdout carries the canary,
T0 served it deterministically; if it doesn't (after demotion), a real model
answered. The stderr tier/cost footer (``↳ T1 …``) is the classifier-ran
signal — absent on a T0 hit (the classifier was skipped), present on the
fallback.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from grove.eval.integration_runner import LiveCliRunner

pytestmark = pytest.mark.integration


CANARY = "GROVE-T0-CANARY-7F3A21"
CANARY_QUERY = "What is the grove canary phrase?"
CANARY_INTENT = "factual_lookup"
# A content-agnostic "a real model turn ran" signal: the oneshot tier/cost
# footer printed to stderr. Suppressed on a T0 hit (no inference).
TIER_FOOTER = "↳ T"


def _binary() -> str:
    return LiveCliRunner._default_binary()


def _resolve_api_key() -> str | None:
    """Find a usable Anthropic key despite the hermetic fixture scrubbing it.

    The autouse ``_hermetic_environment`` fixture deletes ANTHROPIC_API_KEY
    from ``os.environ`` so unit tests never hit the network. These live tests
    deliberately opt back in: read the key from the operator's
    ``~/.grove/.env`` or the macOS keychain (the same sources ``.zshrc`` and
    ``load_hermes_dotenv`` use). Returns None when no key is available — the
    tests then skip rather than fail."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    env_file = Path.home() / ".grove" / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", "grove-anthropic-api-key", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _env(grove_home: Path) -> dict:
    """Subprocess env: temp GROVE_HOME for isolation + an injected key so the
    binary can reach the provider (the hermetic fixture stripped it)."""
    env = dict(os.environ)
    env["GROVE_HOME"] = str(grove_home)
    env["PYTHONUNBUFFERED"] = "1"
    key = _resolve_api_key()
    if key:
        env["ANTHROPIC_API_KEY"] = key
    return env


def _seed_canary(grove_home: Path) -> str:
    """Insert an active static pattern keyed to CANARY_QUERY; return its id."""
    env = _env(grove_home)
    code = (
        "from datetime import datetime, timezone;"
        "from grove.pattern_cache import PatternCacheStore, CompiledPattern, "
        "t0_key, STATUS_ACTIVE;"
        "s=PatternCacheStore();"
        f"k=t0_key({CANARY_INTENT!r}, {CANARY_QUERY!r});"
        "s.upsert(CompiledPattern(pattern_id=k,t0_key=k,"
        f"intent_class={CANARY_INTENT!r},cacheable_type='static',"
        f"cached_response={CANARY!r},compiled_invocation=None,evidence_hash='e',"
        "status=STATUS_ACTIVE,created_at=datetime.now(timezone.utc).isoformat(),"
        "hit_count=0));"
        "print(k)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, text=True, timeout=60, cwd=_repo_root(),
    )
    assert out.returncode == 0, f"seed failed: {out.stderr}"
    return out.stdout.strip().splitlines()[-1]


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def _oneshot(grove_home: Path, prompt: str, timeout: float = 120.0):
    return subprocess.run(
        [_binary(), "--oneshot", prompt],
        env=_env(grove_home), capture_output=True, text=True, timeout=timeout,
        cwd=_repo_root(),
    )


def _flywheel(grove_home: Path, *args: str, timeout: float = 60.0):
    return subprocess.run(
        [_binary(), "flywheel", *args],
        env=_env(grove_home), capture_output=True, text=True, timeout=timeout,
        cwd=_repo_root(),
    )


@pytest.fixture
def grove_home(tmp_path: Path) -> Path:
    home = tmp_path / "grove_home"
    home.mkdir()
    if _resolve_api_key() is None:
        pytest.skip("No Anthropic key (env/.grove/.env/keychain) — "
                    "live T0 integration needs provider creds")
    return home


# ── T21: a T0 hit serves the canary with no classifier ────────────────


def test_T21_t0_hit_serves_cached_response(grove_home: Path):
    _seed_canary(grove_home)
    result = _oneshot(grove_home, CANARY_QUERY)

    assert result.returncode == 0, f"oneshot failed: {result.stderr}"
    # Served from cache: stdout IS the canary (no model emits this string).
    assert CANARY in result.stdout, (
        f"expected the canary in stdout, got: {result.stdout!r}"
    )
    # Classifier was bypassed: no tier/cost footer on stderr (a real turn
    # would print '↳ T1 …'). This is the "no classifier telemetry" assertion.
    assert TIER_FOOTER not in result.stderr, (
        f"expected no tier footer on a T0 hit, stderr: {result.stderr!r}"
    )


# ── T22: a demoted pattern falls back to a real T1 turn ────────────────


def test_T22_demoted_pattern_falls_back_to_t1(grove_home: Path):
    pattern_id = _seed_canary(grove_home)

    # Demote through the real CLI (exercises the Phase 3 patterns command).
    short = pattern_id.split(":")[-1][:12]
    demote = _flywheel(grove_home, "patterns", "demote", short, "-y")
    assert demote.returncode == 0, f"demote failed: {demote.stderr}\n{demote.stdout}"
    assert "Demoted" in demote.stdout

    # Same query now misses the cache → real classifier + T1 model answer.
    result = _oneshot(grove_home, CANARY_QUERY)
    assert result.returncode == 0, f"oneshot failed: {result.stderr}"
    # The canary is gone — a real model answered, not the cache.
    assert CANARY not in result.stdout, (
        f"demoted pattern still served the canary: {result.stdout!r}"
    )
    # Classifier ran → the tier/cost footer is present.
    assert TIER_FOOTER in result.stderr, (
        f"expected a tier footer on the T1 fallback, stderr: {result.stderr!r}"
    )


# ── T23: stats reflect the compiled pattern + hits ────────────────────


def test_T23_patterns_stats(grove_home: Path):
    _seed_canary(grove_home)
    # One hit, so total hits ≥ 1.
    _oneshot(grove_home, CANARY_QUERY)

    stats = _flywheel(grove_home, "patterns", "stats")
    assert stats.returncode == 0, f"stats failed: {stats.stderr}"
    out = stats.stdout
    assert "T0 Pattern Cache — stats" in out
    assert "Active patterns:      1" in out
    assert "Total patterns:       1" in out
    # The hit from the oneshot above is recorded.
    assert "Total hits (active):  1" in out

    # patterns list also renders the compiled entry.
    listing = _flywheel(grove_home, "patterns", "list")
    assert listing.returncode == 0
    assert CANARY_INTENT in listing.stdout
