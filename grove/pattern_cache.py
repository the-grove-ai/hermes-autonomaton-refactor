"""T0 Pattern Cache substrate (Sprint 48 — pattern-compiler-pt1).

The deterministic key + (Sprint 49) store the Cognitive Router's T0 tier
matches against. T0 is DETERMINISTIC: a T0 hit returns a compiled pattern
with NO model call.

``t0_normalize`` / ``t0_key`` are the shared normalizer used at BOTH compile
time (the scanner / compiler, this sprint) AND execution time (Sprint 49).
Per GATE-A decision 1 the normalization is conservative — it expands a small
safe contraction set and strips cosmetic punctuation + whitespace, but does
NOT stem, remove stopwords, or expand abbreviations. The bias is toward false
negatives (T0 simply doesn't fire) over false positives (a wrong T0 hit would
return a canned answer with no model to catch it). Any residual lexical
collisions a slightly-coarse key produces are caught downstream by the
compiler's response-variance gate: a key whose evidence responses differ is
never promoted as static.

The stored ``pattern_hash`` on ``ClassificationResult`` / ``IntentRecord`` is
left untouched (it is live in the classifier and keys existing records);
``t0_key`` is a separate, more robust grouping key derived from the message.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Small, unambiguous contraction expansions. Each preserves meaning exactly —
# no abbreviation guessing ("fav" → "favorite" is intentionally NOT here).
_CONTRACTIONS = {
    "what's": "what is", "whats": "what is", "who's": "who is",
    "how's": "how is", "where's": "where is", "when's": "when is",
    "why's": "why is", "that's": "that is", "there's": "there is",
    "here's": "here is", "it's": "it is", "let's": "let us",
    "i'm": "i am", "you're": "you are", "we're": "we are",
    "they're": "they are", "i've": "i have", "you've": "you have",
    "we've": "we have", "they've": "they have", "i'll": "i will",
    "you'll": "you will", "we'll": "we will", "i'd": "i would",
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "can not", "won't": "will not", "wouldn't": "would not",
    "shouldn't": "should not", "couldn't": "could not", "isn't": "is not",
    "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "hasn't": "has not", "haven't": "have not", "hadn't": "had not",
}

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def t0_normalize(message: str) -> str:
    """Conservative, deterministic normalization for T0 pattern matching.

    Lowercase + Unicode NFKC + safe contraction expansion + punctuation strip
    + whitespace collapse. Contractions are expanded token-wise BEFORE the
    punctuation strip (the apostrophe is the contraction signal).
    """
    if not message:
        return ""
    text = unicodedata.normalize("NFKC", message).lower()
    text = " ".join(_CONTRACTIONS.get(tok, tok) for tok in text.split())
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def t0_key(intent_class: str, message: str) -> str:
    """The deterministic T0 cache key: SHA-256 of ``intent_class`` + the
    t0-normalized message. Identical intent + normalized message → identical
    key. ``intent_class`` is part of the key so cross-intent collisions are
    structurally impossible."""
    seed = f"{intent_class}:{t0_normalize(message)}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
