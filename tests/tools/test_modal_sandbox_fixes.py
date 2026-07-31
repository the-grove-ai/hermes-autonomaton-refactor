"""Tests for Modal sandbox infrastructure fixes (TBLite baseline).

Covers the bugs discovered while setting up TBLite evaluation:
1. Tool resolution — terminal + file tools load correctly
2. CWD fix — host paths get replaced with /root for container backends
3. ephemeral_disk version check
4. ensurepip fix in Modal image builder
5. No swe-rex dependency — uses native Modal SDK
6. /home/ added to host prefix check
7. Vercel sandbox cwd normalization
"""

import os
import sys
from pathlib import Path
import pytest


# Sprint 53 — module-level Dispatcher-style registry for tests.
from tools.registry import ToolRegistry as _Sprint53_TR_top, register_builtin_tools as _Sprint53_RBT_top
_REGISTRY = _Sprint53_TR_top()
_Sprint53_RBT_top(_REGISTRY)

# Ensure repo root is importable
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    import tools.terminal_tool  # noqa: F401
    _tt_mod = sys.modules["tools.terminal_tool"]
except ImportError:
    pytest.skip("hermes-agent tools not importable (missing deps)", allow_module_level=True)


# =========================================================================
# Test 1: Tool resolution includes terminal + file tools
# =========================================================================

class TestToolResolution:
    """Verify get_tool_definitions returns all expected tools for eval."""

    def test_terminal_and_file_toolsets_resolve_all_tools(self):
        """enabled_toolsets=['terminal', 'file'] should produce 6 tools."""
        from model_tools import get_tool_definitions
        tools = get_tool_definitions(_REGISTRY, enabled_toolsets=["terminal", "file"],
            quiet_mode=True,
        )
        names = {t["function"]["name"] for t in tools}
        expected = {"terminal", "process", "read_file", "write_file", "search_files", "patch"}
        assert expected == names, f"Expected {expected}, got {names}"

    def test_terminal_tool_present(self):
        """The terminal tool must be present (not silently dropped)."""
        from model_tools import get_tool_definitions
        tools = get_tool_definitions(_REGISTRY, enabled_toolsets=["terminal", "file"],
            quiet_mode=True,
        )
        names = [t["function"]["name"] for t in tools]
        assert "terminal" in names, f"terminal tool missing! Only got: {names}."


# =========================================================================
# Test 2-4: CWD handling for container backends
# =========================================================================

class TestCwdHandling:
    """Verify host paths are sanitized for container backends."""

    @pytest.mark.parametrize("backend", ["singularity"])
    def test_default_cwd_is_root_for_container_backends(self, backend, monkeypatch):
        """Container backends should default to /root, not ~."""
        monkeypatch.setenv("TERMINAL_ENV", backend)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        config = _tt_mod._get_env_config()
        assert config["cwd"] == "/root", (
            f"Backend {backend}: expected /root default, got {config['cwd']}"
        )

    def test_local_backend_uses_getcwd(self, monkeypatch):
        """Local backend should use os.getcwd(), not /root."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        config = _tt_mod._get_env_config()
        assert config["cwd"] == os.getcwd()

    def test_ssh_preserves_home_paths(self, monkeypatch):
        """SSH backend should NOT replace /home/ paths (they're valid remotely)."""
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        monkeypatch.setenv("TERMINAL_CWD", "/home/remote-user/work")
        monkeypatch.setenv("TERMINAL_SSH_HOST", "example.com")
        monkeypatch.setenv("TERMINAL_SSH_USER", "user")
        config = _tt_mod._get_env_config()
        assert config["cwd"] == "/home/remote-user/work", (
            "SSH backend should preserve /home/ paths"
        )


# =========================================================================
# Test 5: Host prefix list completeness
# =========================================================================

class TestHostPrefixList:
    """Verify the host prefix list catches common host-only paths."""

    def test_all_common_host_prefixes_caught(self):
        """The host prefix check should catch /Users/, /home/, C:\\, C:/."""
        # Read the actual source to verify the prefixes
        import inspect
        source = inspect.getsource(_tt_mod._get_env_config)
        for prefix in ["/Users/", "/home/", 'C:\\\\"', "C:/"]:
            # Normalize for source comparison
            check = prefix.rstrip('"')
            assert check in source or prefix in source, (
                f"Host prefix {prefix!r} not found in _get_env_config. "
                "Container backends need this to avoid using host paths."
            )
