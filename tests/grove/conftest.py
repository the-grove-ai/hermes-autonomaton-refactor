"""Grove-scoped test fixtures.

Opt-in hermetic isolation for tests that classify tools or gate proposal
pushes against per-operator state. See ``hermetic_grove_home``.
"""

from __future__ import annotations

import os
import time

import pytest


@pytest.fixture
def hermetic_grove_home(tmp_path, monkeypatch):
    """Close the two live-state leaks the autouse ``_hermetic_environment``
    (tests/conftest.py) leaves open: it isolates GROVE_HOME but deliberately
    NOT HOME, and sets ``TZ=UTC`` without calling ``time.tzset()``.

    * ``grove.zones._resolve_overlay_path()`` reads
      ``Path.home() / ".grove" / "zones.autonomaton.yaml"`` — the operator's
      flywheel zone overlay (write_file / approve_proposal / add_write_workspace
      promoted to green). Keyed on HOME, it survives GROVE_HOME isolation and
      merges into the classifier, so pristine-policy assertions see the
      promoted (green) zones instead of the repo schema's yellow.

    * ``run_agent.AIAgent._append_pending_offer`` converts a *naive* session
      anchor via ``session_start.astimezone(timezone.utc)``. With TZ unset (or
      set without ``tzset()``) on a non-UTC host — the VM runs
      ``America/New_York`` — the naive anchor is read as local and shifted
      forward, so a "current-session" proposal reads as pre-session and never
      pushes.

    This fixture redirects HOME to a per-test tempdir (overlay absent ->
    ``_resolve_overlay_path()`` -> None -> no merge) and makes ``TZ=UTC``
    effective via ``time.tzset()`` (naive-local == UTC, honoring the tests'
    documented clock). Assertions are unchanged — they assert pristine policy,
    which is correct against the repo schema.
    """
    import grove.zones as _zones

    monkeypatch.setenv("HOME", str(tmp_path))
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    # ``grove.zones`` caches a module-level classifier singleton. A test earlier
    # in the same worker builds it with the real ``~/.grove`` overlay merged
    # (write_file / approve_proposal / ... promoted to green); isolating HOME
    # alone does not rebuild that cache, so classification would still read the
    # promoted map. Rebuild the singleton under the now-isolated HOME (overlay
    # absent -> pristine repo policy). Restore the prior classifier on teardown
    # so sibling tests are unaffected; when there was none (this fixture is the
    # first to touch zones in the worker), leave the freshly-initialized one in
    # place — never reset zones to the uninitialized state that raises on
    # classify().
    old_singleton = _zones._singleton
    _zones.initialize()
    try:
        yield
    finally:
        if old_singleton is not None:
            _zones._singleton = old_singleton
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
