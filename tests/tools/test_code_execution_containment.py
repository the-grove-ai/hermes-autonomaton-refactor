"""execute-code-meta-surface-containment-v1 Phase-1 — unit tests for the
containment classifier (``_classify_containment``).

Pure-function tests: no subprocess, no ledger, no filesystem writes. The autouse
``_hermetic_environment`` fixture (tests/conftest.py) isolates HOME/GROVE_HOME so
``is_scope_defining`` / ``is_secret_path`` read pristine policy. The classifier's
config-root anchor is ``__file__``-derived (the real repo config/), independent of
GROVE_HOME, so governance paths resolve deterministically.
"""

from __future__ import annotations

import os

from tools.code_execution_tool import _classify_containment, _repo_config_root


def _cfg_path(name: str = "zones.schema.yaml") -> str:
    return os.path.join(_repo_config_root(), name)


# ── governance_definition — the surface this epic protects ────────────────────

def test_python_oserror_config_target_is_governance():
    target = _cfg_path()
    stderr = (
        "Traceback (most recent call last):\n"
        "  File \"<string>\", line 1, in <module>\n"
        f"OSError: [Errno 30] Read-only file system: '{target}'\n"
    )
    matched, extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert extracted == target
    assert bclass == "governance_definition"


def test_git_style_fallback_extracts_config_path():
    # git carries no [Errno 30]; the /-rooted fallback must still extract + classify.
    target = _cfg_path("capabilities")
    stderr = f"fatal: cannot mkdir {target}/x: Read-only file system\n"
    matched, extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert extracted.startswith(target)
    assert bclass == "governance_definition"


def test_sed_style_fallback_extracts_config_path():
    target = _cfg_path()
    stderr = (
        f"sed: couldn't open temporary file {os.path.dirname(target)}/sedAB12: "
        "Read-only file system\n"
    )
    matched, extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert bclass == "governance_definition"


# ── system_protect / other_readonly — matched but NOT filed ───────────────────

def test_usr_path_is_system_protect():
    stderr = "OSError: [Errno 30] Read-only file system: '/usr/lib/python3/site.py'"
    matched, extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert bclass == "system_protect"


def test_unrelated_readonly_path_is_other_readonly():
    stderr = "OSError: [Errno 30] Read-only file system: '/mnt/cdrom/data.txt'"
    matched, extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert bclass == "other_readonly"


# ── unresolved — marker present, no extractable path ──────────────────────────

def test_marker_without_path_is_unresolved():
    stderr = "fatal: Read-only file system\n"
    matched, extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert extracted == "unknown"
    assert bclass == "unresolved"


# ── secret redaction — never write a secret path into the ledger ──────────────

def test_secret_target_is_redacted():
    stderr = "OSError: [Errno 30] Read-only file system: '/home/someone/.ssh/id_rsa'"
    matched, extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert extracted == "[redacted]"


# ── negatives — no match, no filing ───────────────────────────────────────────

def test_non_readonly_stderr_no_match():
    stderr = (
        "Traceback (most recent call last):\n"
        "  File \"<string>\", line 1\n"
        "SyntaxError: invalid syntax\n"
    )
    assert _classify_containment(stderr) == (False, "", "")


def test_empty_stderr_no_match():
    assert _classify_containment("") == (False, "", "")
    assert _classify_containment(None) == (False, "", "")


def test_case_insensitive_marker():
    target = _cfg_path()
    stderr = f"OSError: [Errno 30] READ-ONLY FILE SYSTEM: '{target}'"
    matched, _extracted, bclass = _classify_containment(stderr)
    assert matched is True
    assert bclass == "governance_definition"


def test_resolver_failure_never_leaks_target(monkeypatch):
    # If the resolver block raises (e.g. fs_utils import fails) the classifier
    # must file the event but NEVER the raw (possibly secret) path.
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "grove.utils.fs_utils":
            raise ImportError("forced resolver failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    stderr = "OSError: [Errno 30] Read-only file system: '/home/hermes/.grove/.env'"
    matched, extracted, bclass = _classify_containment(stderr)
    assert (matched, extracted, bclass) == (True, "unknown", "unresolved")
