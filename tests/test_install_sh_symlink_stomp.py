"""Regression for #21454: re-running install.sh on a symlinked prior install.

Older versions of ``install.sh`` created ``$command_link_dir/<name>`` as a
symlink to the pip-generated entry point at ``$GROVE_BIN`` (i.e.
``venv/bin/hermes``). When ``setup_path()`` later switched to writing a bash
shim with ``cat > "$command_link_dir/<name>" <<EOF``, the redirect followed
the existing symlink and overwrote the pip entry point with the shim. The
shim's ``exec "$GROVE_BIN" "$@"`` then self-recursed and the launcher hung on
every invocation.

These tests pin the fix: ``setup_path()`` must remove each shim path before
writing through the redirect, so the shim is created as a regular file in
``command_link_dir`` and the venv entry point is left intact. The shim loop
installs both CLI names — ``autonomaton`` (primary) and ``hermes`` (alias).
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _extract_setup_path_shim_block() -> str:
    """Return the install.sh shim-write loop used by setup_path()."""
    text = INSTALL_SH.read_text()
    match = re.search(
        r'(?P<block>mkdir -p "\$command_link_dir".*?\n    done)',
        text,
        re.DOTALL,
    )
    assert match is not None, (
        "Could not locate the setup_path shim-write block in scripts/install.sh"
    )
    return match["block"]


def test_setup_path_shim_block_removes_old_link_before_writing() -> None:
    """Static guard: the rm must precede the cat heredoc, not follow it."""
    block = _extract_setup_path_shim_block()
    rm_idx = block.find('rm -f "$command_link_dir/$_cli_name"')
    cat_idx = block.find('cat > "$command_link_dir/$_cli_name" <<EOF')
    assert rm_idx != -1, (
        "setup_path() must `rm -f` $command_link_dir/$_cli_name before the "
        "`cat >` heredoc, otherwise an existing symlink (left by older "
        "installs) will be followed and the pip entry point overwritten. "
        "See #21454."
    )
    assert cat_idx != -1, "expected `cat >` heredoc still present"
    assert rm_idx < cat_idx, (
        "`rm -f` must come *before* the `cat >` heredoc, not after."
    )


def test_shim_block_installs_both_cli_names() -> None:
    """The shim loop must cover autonomaton (primary) and hermes (alias)."""
    block = _extract_setup_path_shim_block()
    assert "for _cli_name in autonomaton hermes; do" in block


def test_re_running_setup_path_block_preserves_pip_entry_point(tmp_path: Path) -> None:
    """Behavioral repro: simulate prior-install symlink + new-install heredoc.

    Layout mirrors a real install:

        tmp/
          venv/bin/hermes        <- pip entry point (the one we must preserve)
          local_bin/hermes       <- symlink → ../venv/bin/hermes  (old install)

    Then we run the exact shim-write loop from setup_path() with ``GROVE_BIN``
    and ``command_link_dir`` pointed at this fixture. After the run both the
    ``autonomaton`` and ``hermes`` shims must be regular files, and the pip
    entry point must be untouched.
    """
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    pip_entry = venv_bin / "hermes"
    pip_marker = "#!/usr/bin/env python\n# pip-generated entry point — must not be overwritten\n"
    pip_entry.write_text(pip_marker)
    pip_entry.chmod(pip_entry.stat().st_mode | stat.S_IXUSR)

    command_link_dir = tmp_path / "local_bin"
    command_link_dir.mkdir()
    # Reproduce the prior-install state: the hermes shim path is a symlink
    # to the pip-generated entry point.
    shim_path = command_link_dir / "hermes"
    shim_path.symlink_to(pip_entry)
    assert shim_path.is_symlink()

    block = _extract_setup_path_shim_block()
    # Drive the loop with the env vars setup_path() sets; stub log_success.
    script = (
        "set -e\n"
        "log_success() { :; }\n"
        f"GROVE_BIN={pip_entry!s}\n"
        f"command_link_dir={command_link_dir!s}\n"
        f"command_link_display_dir={command_link_dir!s}\n"
        f"{block}\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, (
        f"shim-write block failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # The pip entry point must still be the original pip script — not a
    # re-written self-recursing bash shim.
    assert pip_entry.read_text() == pip_marker, (
        "venv/bin/hermes was overwritten by setup_path() — symlink-stomp "
        "regression (#21454)."
    )

    # Both shims must now be regular files holding the launcher.
    for name in ("autonomaton", "hermes"):
        path = command_link_dir / name
        assert path.exists(), f"{name} shim was not created"
        assert not path.is_symlink(), (
            f"command_link_dir/{name} must be a regular file, not a symlink "
            "— otherwise the next install will stomp again."
        )
        shim_text = path.read_text()
        assert "unset PYTHONPATH" in shim_text
        assert "unset PYTHONHOME" in shim_text
        assert f'exec "{pip_entry}"' in shim_text
        assert path.stat().st_mode & stat.S_IXUSR, f"{name} shim must be executable"
