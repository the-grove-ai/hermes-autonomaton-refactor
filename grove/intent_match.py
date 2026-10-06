"""Deterministic request matching by token overlap.

Two requests mean the same thing here when their CONTENT words overlap enough:
both are normalized, filler words are dropped, and the two token sets are
scored by Jaccard overlap (shared / total). No model, no embeddings, no
learned weights — the same input always gives the same score, and a reader
can check any score by hand.

Ported from the Atlas prototype's intent hashing (``intent-hash.ts``), with one
deliberate omission: Atlas adds a bonus when two requests share a verb. That
bonus is left out. Matching here can route a request to a path with no model
behind it, and "do X" and "do X as Y" share a verb but are not the same
request; the extra words must count against the match.

Pipeline stage: Recognition, for the one case where recognition must happen
without a model (a T0 lookup, or deciding what a session's first message
opens). Thresholds are declared by whoever calls this — a goal's config, the
pattern cache's config — never fixed here.
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, Optional, Tuple

from grove.pattern_cache import t0_normalize

# Words that carry no intent. Kept small and general on purpose: every word
# added here is a word that can no longer tell two requests apart.
FILLER = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "it", "its",
    "i", "me", "my", "we", "us", "our", "you", "your",
    "is", "are", "was", "were", "be", "am", "do", "does", "did",
    "can", "could", "would", "will", "shall", "should",
    "to", "of", "for", "on", "in", "at", "by", "with", "from", "up",
    "and", "or", "so", "then", "now", "just", "also",
    "please", "pls", "thanks", "thank", "ok", "okay", "hey", "hi",
    "one", "go", "lets", "let",
})


def _stem(word: str) -> str:
    """Fold a plain plural onto its singular ("invoices" → "invoice"). No
    other stemming: anything more aggressive merges words that differ."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> FrozenSet[str]:
    """The content-word set of a request."""
    words = t0_normalize(text or "").split()
    return frozenset(_stem(w) for w in words if w and w not in FILLER)


def verb_of(example: str) -> Optional[str]:
    """An example request's verb: its first content word ("Code the next
    invoice" → "code"). Read off the example itself, so nothing about any
    domain's verbs is written down here."""
    for word in t0_normalize(example or "").split():
        if word and word not in FILLER:
            return _stem(word)
    return None


def overlap(a: str, b: str, *, verb_bonus: float = 0.0) -> float:
    """Jaccard overlap of a message ``a`` and an example ``b``, 0.0–1.0. Two
    requests with no content words at all do not match (0.0): there is
    nothing to agree on.

    ``verb_bonus`` is added when the message uses the example's verb AND adds
    no content word the example lacks — a shorter way of saying the same
    thing ("code next" for "Code the next invoice"). Missing words are
    forgivable; extra words are not: a message that adds anything ("…as
    6800") gets no bonus, because the extra words may be an instruction the
    matched path would never see."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    score = len(ta & tb) / len(ta | tb)
    if verb_bonus and ta <= tb and verb_of(b) in ta:
        score = min(1.0, score + float(verb_bonus))
    return score


def best_match(
    message: str, examples: Iterable[str], *, verb_bonus: float = 0.0,
) -> Tuple[float, Optional[str]]:
    """The highest-scoring example for ``message`` as ``(score, example)``."""
    best: Tuple[float, Optional[str]] = (0.0, None)
    for example in examples:
        score = overlap(message, example, verb_bonus=verb_bonus)
        if score > best[0]:
            best = (score, example)
    return best


def matches(
    message: str, examples: Iterable[str], threshold: float, *, verb_bonus: float = 0.0,
) -> bool:
    """Whether ``message`` matches any example at or above ``threshold``."""
    return best_match(message, examples, verb_bonus=verb_bonus)[0] >= float(threshold)
