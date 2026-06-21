"""Test isolation for the memory package.

Fix 1 (telemetry debounce) keeps a module-level per-session pending-access
registry in grove.memory.store. Clear it between tests so served-id state
from one test cannot leak into another.
"""

import pytest

from grove.memory import store as _store


@pytest.fixture(autouse=True)
def _clear_pending_access():
    _store._reset_pending_access()
    yield
    _store._reset_pending_access()
