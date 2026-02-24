"""Beneficiary name matching for customer lookup verification."""

import re
import unicodedata


def normalize_name(name: str) -> list[str]:
    """Normalize a name into lowercase tokens, stripping accents and punctuation."""
    # Strip accents
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase and split on non-alpha
    tokens = re.split(r"[^a-zA-Z]+", ascii_name.lower())
    return [t for t in tokens if t]


def name_match_score(name_a: str, name_b: str) -> float:
    """Compute token-overlap similarity between two names.

    Returns a float between 0.0 and 1.0.
    Score = |intersection| / |union| (Jaccard index on name tokens).
    """
    if not name_a or not name_b:
        return 0.0
    tokens_a = set(normalize_name(name_a))
    tokens_b = set(normalize_name(name_b))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# Threshold: >= this score is a match, below is a mismatch
NAME_MATCH_THRESHOLD = 0.6
