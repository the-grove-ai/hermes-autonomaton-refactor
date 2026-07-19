"""model-catalog-v1 P1 ratchet suite — the structural invariants (M-6 / G-6 / G-1b).

Three CI ratchets, all GREEN on the current tree:
  * M-6  no model-ID literals in grove/ CODE — a model slug is a config value,
    never a hardcoded string. Comments are documentation and are exempt by
    construction (only string-literal tokens are inspected).
  * G-6  repo coherence — every model bound in config/routing.config.yaml's
    tier_preferences exists in the repo model catalog.
  * G-1b dispatch isolation — the dispatch path never imports/reads the catalog,
    so a Yellow catalog write can never alter Red-walled execution semantics.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_GROVE = _REPO / "grove"

# provider/model slash-form. The provider set is the known prefix space; a bare
# "foo/bar" without a known provider is not a model id and is ignored.
_SLUG_RE = re.compile(
    r"\b(openai|anthropic|deepseek|z-ai|zhipu|moonshotai|google|x-ai|xiaomi|"
    r"qwen|meta-llama|mistralai|minimax|cohere|ollama)/[A-Za-z0-9._-]+"
)

# Legitimate model-slug string literals in grove/ code, if any ever arise. Empty
# today — the catalog is the sole birthplace. Entries are (relative_path, slug)
# and must carry a justification comment. This allowlist IS the audit trail.
_CODE_LITERAL_ALLOWLIST: set[tuple[str, str]] = set()


def _model_slug_string_literals() -> list[tuple[str, int, str]]:
    """Every provider/model slug appearing in a STRING literal under grove/.

    Uses ``tokenize`` so COMMENT tokens are excluded by construction — a model
    id in a comment is documentation, not code, and never trips the ratchet.
    """
    hits: list[tuple[str, int, str]] = []
    for path in sorted(_GROVE.rglob("*.py")):
        rel = str(path.relative_to(_REPO))
        try:
            src = path.read_text(encoding="utf-8")
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type != tokenize.STRING:
                    continue
                for m in _SLUG_RE.finditer(tok.string):
                    slug = m.group(0)
                    if (rel, slug) in _CODE_LITERAL_ALLOWLIST:
                        continue
                    hits.append((rel, tok.start[0], slug))
        except (SyntaxError, tokenize.TokenError):
            continue
    return hits


def test_no_model_id_literals_in_grove_code():
    hits = _model_slug_string_literals()
    assert not hits, (
        "model ID string literal(s) found in grove/ code — model ids belong in "
        "the catalog / routing config, never hardcoded. Move to config or, if "
        "genuinely unavoidable, add to _CODE_LITERAL_ALLOWLIST with justification:\n"
        + "\n".join(f"  {rel}:{line} — {slug}" for rel, line, slug in hits)
    )


def test_repo_tier_preferences_are_all_cataloged():
    routing = yaml.safe_load((_REPO / "config" / "routing.config.yaml").read_text("utf-8"))
    catalog = yaml.safe_load((_REPO / "config" / "model-catalog.yaml").read_text("utf-8"))
    cat_slugs = {m["slug"] for m in catalog["models"]}

    prefs = (routing.get("routing", {}) or {}).get("tier_preferences", {}) or {}
    bound = {
        entry["model"]
        for entry in prefs.values()
        if isinstance(entry, dict) and entry.get("model")
    }
    missing = bound - cat_slugs
    assert not missing, (
        f"tier_preferences bind model(s) absent from the repo catalog: "
        f"{sorted(missing)} — add them to config/model-catalog.yaml (G-6 coherence)"
    )


def test_dispatch_path_never_reads_the_catalog():
    # G-1b — the modules on the route()/dispatch path must not import or read
    # grove.config.model_catalog. Same family as the zero-producer-names ratchet
    # (test_canonicalize_core_is_producer_blind): source-level, structural.
    dispatch_modules = [
        "grove/router.py",
        "grove/router_merge.py",
        "grove/dispatcher.py",
        "grove/tier_budget.py",
        "grove/providers.py",
    ]
    offenders = []
    for rel in dispatch_modules:
        src = (_REPO / rel).read_text(encoding="utf-8")
        if "model_catalog" in src:
            offenders.append(rel)
    assert not offenders, (
        "dispatch-path module(s) reference model_catalog — dispatch must never "
        f"read the catalog (metadata-only isolation, G-1b): {offenders}"
    )
