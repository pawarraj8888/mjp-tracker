"""Tender dedupe across portals.

The same tender often appears on an agency portal and on mahatenders. A record
matches an existing one when the normalised org matches, the titles have a
token-set ratio >= 90, the bid-end dates are within one day, and the estimated
values are within 2%. The surviving record keeps both source ids.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime

from .store import Store
from .text import normalize_name, normalize_org, token_set_ratio

TITLE_MATCH = 90.0
VALUE_TOLERANCE = 0.02
DAY_TOLERANCE = 1


def _day(text: str | None):
    if not text:
        return None
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M", "%d-%b-%Y",
                "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def dedupe_hash(rec: dict) -> str:
    org = normalize_org(rec.get("publishing_org"))
    title = " ".join(sorted(normalize_name(rec.get("title")).split()))
    day = _day(rec.get("bid_submission_end"))
    value = rec.get("estimated_value_inr") or 0
    # Log-scale bucket so values within VALUE_TOLERANCE land together while
    # far-apart values separate (a flat int(value/(value*tol)) was constant).
    bucket = int(math.log(value) / math.log(1 + VALUE_TOLERANCE)) if value > 0 else 0
    key = "|".join([org, title, str(day or ""), str(bucket)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _values_close(a, b) -> bool:
    a, b = a or 0, b or 0
    if a == 0 and b == 0:
        return True
    hi = max(a, b)
    return hi > 0 and abs(a - b) / hi <= VALUE_TOLERANCE


def _days_close(a, b) -> bool:
    da, db = _day(a), _day(b)
    if da is None or db is None:
        return da == db
    return abs((da - db).days) <= DAY_TOLERANCE


def find_duplicate(store: Store, rec: dict) -> dict | None:
    """An existing tender that is the same work as ``rec`` but from another
    source, or None. Same source_portal + source_tender_id is not a duplicate
    (that is an update, handled by upsert)."""
    org = normalize_org(rec.get("publishing_org"))
    if not org:
        return None
    candidates = store.query(
        "SELECT * FROM tenders WHERE dedupe_hash=? OR publishing_org=?",
        (dedupe_hash(rec), rec.get("publishing_org") or ""))
    for c in candidates:
        if c["source_portal"] == rec["source_portal"] and \
                c["source_tender_id"] == rec["source_tender_id"]:
            continue
        if normalize_org(c["publishing_org"]) != org:
            continue
        if token_set_ratio(rec.get("title") or "", c["title"] or "") < TITLE_MATCH:
            continue
        if not _days_close(rec.get("bid_submission_end"), c["bid_submission_end"]):
            continue
        if not _values_close(rec.get("estimated_value_inr"),
                             c["estimated_value_inr"]):
            continue
        return c
    return None
