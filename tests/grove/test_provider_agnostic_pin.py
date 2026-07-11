"""dock-detector-provider-agnostic-v1 — class-death pin.

The class this pins dead: a module issuing an anthropic-shape client
construction (``build_anthropic_client(`` / ``Anthropic(``) against a tier
runtime WITHOUT branching on the tier's resolved ``api_mode``. Every such
call against a chat_completions tier POSTs ``/v1/messages`` at an
OpenAI-compatible base_url and gets an HTML 404 back — the failure mode
classifier-provider-agnostic-v1, wiki-pipeline-provider-agnostic-v1,
detector-provider-agnostic-v1, dock-detector-provider-agnostic-v1, and
kaizen-synthesizer-provider-agnostic-v1 each closed for one caller.

Producer-blind by design: the scan matches the CONSTRUCTION PATTERN, not
function names, so a new caller added anywhere under ``grove/`` or ``agent/``
trips the pin regardless of what it calls itself. New constructions belong
inside the transport seam or behind an explicit api_mode / provider branch —
extend the allowlist only with that evidence.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Production trees the pin sweeps. Tests are excluded — fakes construct
# freely.
_SCAN_ROOTS = ("grove", "agent")

# The anthropic-shape construction pattern the provider-agnostic sprints
# replaced. No whitespace before the paren: that is the call form, and it
# keeps prose like "Anthropic (sk-ant-*)" out of scope.
_CONSTRUCTION = re.compile(
    r"build_anthropic_client\("
    r"|\bAnthropic(?:Bedrock)?\("
    r"|\bAsyncAnthropic\w*\("
)

# ── allowlist ────────────────────────────────────────────────────────────

# The transport seam itself — where anthropic-shape construction is the job.
_TRANSPORT_SEAM = frozenset({
    "agent/anthropic_adapter.py",
    "agent/auxiliary_client.py",
    "agent/transports/anthropic.py",
})

# Callers whose construction sits inside an explicit
# ``api_mode == "anthropic_messages"`` arm, with a sibling chat_completions
# arm and a fail-loud else. Asserted structurally below, not just listed.
_API_MODE_BRANCHED = frozenset({
    "grove/classify.py",              # classifier-provider-agnostic-v1
    "grove/t1_call.py",               # wiki-pipeline-provider-agnostic-v1
    "grove/memory/detector.py",       # detector-provider-agnostic-v1
    "grove/dock/detector.py",         # dock-detector-provider-agnostic-v1
    "grove/kaizen/synthesizer.py",    # kaizen-synthesizer-provider-agnostic-v1
})

# Construction gated on ``provider == "anthropic"`` at the call site
# (dispatcher client cache) — asserted structurally below.
_PROVIDER_GATED = frozenset({
    "grove/dispatcher.py",
})

_ALLOWLIST = _TRANSPORT_SEAM | _API_MODE_BRANCHED | _PROVIDER_GATED


def _production_py_files():
    for root in _SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "__pycache__" in rel:
                continue
            yield rel, path


def test_anthropic_construction_confined_to_allowlist():
    """No anthropic-shape client construction outside the allowlist."""
    violations = []
    for rel, path in _production_py_files():
        if rel in _ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _CONSTRUCTION.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{rel}:{line}: {match.group(0)!r}")
    assert not violations, (
        "anthropic-shape client construction outside the provider-agnostic "
        "allowlist — new callers must branch on the tier's api_mode "
        "(template: grove/t1_call.py) or live in the transport seam:\n"
        + "\n".join(violations)
    )


def test_api_mode_branched_callers_carry_both_arms():
    """Each branched caller has the anthropic_messages arm, the
    chat_completions arm, and reads api_mode from its runtime."""
    for rel in sorted(_API_MODE_BRANCHED):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert '"anthropic_messages"' in text, (
            f"{rel}: anthropic_messages arm missing — construction is no "
            f"longer guarded; re-branch or move it to the transport seam"
        )
        assert '"chat_completions"' in text, (
            f"{rel}: chat_completions arm missing — caller regressed to "
            f"Anthropic-only against a swappable tier"
        )
        assert re.search(r"api_mode\s*=", text), (
            f"{rel}: no api_mode read from the resolved runtime"
        )


def test_dispatcher_construction_is_provider_gated():
    """The dispatcher's client cache only builds when the main-loop
    provider IS anthropic — that gate is what keeps it in the seam."""
    text = (REPO_ROOT / "grove/dispatcher.py").read_text(encoding="utf-8")
    assert 'provider == "anthropic"' in text, (
        "grove/dispatcher.py: provider gate on _get_or_build_anthropic_client "
        "call site missing — construction would fire for non-Anthropic "
        "main-loop providers"
    )
