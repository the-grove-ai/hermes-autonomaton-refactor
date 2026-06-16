"""Effect signatures + the dispatch-primitive approval gate — GRV-010 C1c-i.

The dispatch primitive (``ToolRegistry.dispatch``) is a dumb cryptographic lock:
during a governed turn, an effecting dispatch must present a valid single-use
token for its EXACT canonicalized effect signature, or it is refused
(``GovernanceError``). Tokens are minted only by classify-and-mint at the call
sites — closing the in-process classifier-skip paths (T0 / sandbox RPC / plugin)
that previously reached the primitive without a classified intent.

Two unforgeability properties are realised here:

* **Realpath-canonical signature.** ``canonical_effect_signature`` realpath-
  resolves every path-bearing argument (and folds the C1a AST effect signature
  for shell commands). The gate re-derives the signature at consume time, so a
  symlink swapped in AFTER approval resolves to a different real target →
  different signature → the token does not match → fail-closed (TOCTOU-symlink).

* **Ephemeral HMAC token, single-use, turn-scoped.** The gate holds a per-turn
  random secret (parent-process memory, out of subprocess/sandbox reach). A
  minted "token" is ``HMAC(secret, signature)``; to forge one you need the
  secret. Each token is consumed on first match (single-use → no replay within
  a turn) and the whole approved set is flushed when the turn yields (turn-
  scoped → no cross-turn/session replay).
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import Optional

__all__ = ["canonical_effect_signature", "ApprovalGate"]


# Argument keys whose values are filesystem targets — realpath-resolved so the
# signature binds to the REAL target, and a post-approval symlink swap fails.
_PATH_ARG_KEYS = frozenset({
    "path", "filepath", "file_path", "target", "target_file", "dst",
    "destination", "old_path", "new_path", "source", "src", "dir", "directory",
})

_FS = "\x1f"  # unit separator — unambiguous field delimiter in the signature


def _realpath(value: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(value))
    except (OSError, ValueError):
        return value


def canonical_effect_signature(tool_name: str, args: object) -> str:
    """Return a stable, realpath-canonical effect signature for ``(tool, args)``.

    Deterministic and recomputed identically at mint time and consume time.
    Path-bearing args are realpath-resolved (re-resolved at consume → symlink
    swap caught). Shell commands fold in the C1a AST effect signature so the
    bound effect matches the classified effect, not the raw string.
    """
    a = dict(args) if isinstance(args, dict) else {}
    norm = {}
    for k, v in a.items():
        if k in _PATH_ARG_KEYS and isinstance(v, str) and v:
            norm[k] = _realpath(v)
        else:
            norm[k] = v

    shell_sig = ""
    if tool_name in ("terminal", "execute_code"):
        cmd = a.get("command")
        if isinstance(cmd, str) and cmd.strip():
            try:
                from grove.shell_effects import classify_shell_effect
                shell_sig = classify_shell_effect(cmd).pattern_key or ""
            except Exception:
                shell_sig = ""

    try:
        payload = json.dumps(norm, sort_keys=True, default=str)
    except Exception:
        payload = str(sorted(norm.items()))
    return f"{tool_name}{_FS}{shell_sig}{_FS}{payload}"


class ApprovalGate:
    """Per-Dispatcher cryptographic approval gate guarding the dispatch primitive.

    Enforces only while ``active`` (set for the duration of a governed
    ``dispatch_turn``). Mint adds one single-use token for a signature; consume
    removes one on match. The secret is rotated and the approved set flushed on
    every ``activate``/``flush`` so no token survives the turn that minted it.
    """

    def __init__(self) -> None:
        self._secret: bytes = secrets.token_bytes(32)
        self._approved: "collections.Counter[str]" = collections.Counter()
        self._active: bool = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def activate(self) -> None:
        """Arm for a turn: rotate the secret, clear any residue, enable enforcement."""
        with self._lock:
            self._secret = secrets.token_bytes(32)
            self._approved.clear()
            self._active = True

    def flush(self) -> None:
        """Disarm + drop every minted token (turn yield). No cross-turn replay."""
        with self._lock:
            self._approved.clear()
            self._active = False

    def _digest(self, signature: str) -> str:
        return hmac.new(
            self._secret, signature.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def mint(self, signature: str) -> None:
        """Authorize ONE dispatch of *signature* (a single-use token)."""
        with self._lock:
            self._approved[self._digest(signature)] += 1

    def consume(self, signature: str) -> bool:
        """Spend one token for *signature*. True if one was present (single-use)."""
        with self._lock:
            d = self._digest(signature)
            if self._approved[d] > 0:
                self._approved[d] -= 1
                if self._approved[d] == 0:
                    del self._approved[d]
                return True
            # Counter[d] auto-created the key with 0; remove it to avoid growth.
            if d in self._approved and self._approved[d] == 0:
                del self._approved[d]
            return False
