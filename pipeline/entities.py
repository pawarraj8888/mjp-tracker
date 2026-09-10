"""Contractor entity resolution.

Names are normalised (legal suffixes, honorifics, punctuation) and clustered
with a token-set ratio: auto-merge at >= 95, queue 85..95 for manual review,
never auto-merge below 85 (per docs/ASSUMPTIONS and the brief). Identical
normalised keys collapse naturally because the store keys contractors by that
key.
"""

from __future__ import annotations

from .store import Store
from .text import clean, normalize_name, token_set_ratio

AUTO_MERGE = 95.0
REVIEW_LOW = 85.0


def _candidates(store: Store, key: str) -> list:
    """Existing contractors worth comparing against ``key``: the exact key, or
    any whose match_key shares the first token. This blocks the fuzzy scan so
    resolution does not become O(n^2) over the whole table."""
    rows = store.query("SELECT * FROM contractors WHERE match_key=?", (key,))
    first = key.split(" ")[0] if key else ""
    if first:
        rows += store.query(
            "SELECT * FROM contractors WHERE match_key LIKE ? AND match_key != ?",
            ("%" + first + "%", key))
    return rows


def resolve_contractor(store: Store, name_raw: str, seen: str = "") -> str | None:
    """Return the contractor id for a raw awarded-bidder name, creating or
    merging as needed. None for an empty name."""
    key = normalize_name(name_raw)
    if not key:
        return None

    best = None
    best_score = 0.0
    for c in _candidates(store, key):
        if c["match_key"] == key:
            best, best_score = c, 100.0
            break
        score = token_set_ratio(key, c["match_key"])
        if score > best_score:
            best, best_score = c, score

    if best is not None and best_score >= AUTO_MERGE:
        # Same firm under a spelling/suffix variant: merge in as an alias.
        return store.upsert_contractor(
            best["canonical_name"], best["match_key"],
            alias=clean(name_raw), seen=seen)

    cid = store.upsert_contractor(clean(name_raw), key,
                                  alias=clean(name_raw), seen=seen)
    if best is not None and REVIEW_LOW <= best_score < AUTO_MERGE \
            and best["match_key"] != key:
        store.queue_review(clean(name_raw), best["id"],
                           best["canonical_name"], round(best_score, 1))
    return cid
