"""Retender linking.

When a tender is cancelled/retendered and a new one with the same org, a
fuzzy-matching title and a close value appears within a window (default 180
days), the new tender's ``retender_of`` is set to the original. Floated-value
analytics can then exclude superseded originals.
"""

from __future__ import annotations

from .dedupe import _day, _values_close
from .store import Store
from .text import token_set_ratio

WINDOW_DAYS = 180
TITLE_MATCH = 90.0


def link_retenders(store: Store, window_days: int = WINDOW_DAYS) -> int:
    originals = store.query(
        "SELECT * FROM tenders WHERE status IN ('cancelled','retendered')")
    linked = 0
    for old in originals:
        od = _day(old["publish_date"]) or _day(old["bid_submission_end"])
        cands = store.query(
            "SELECT * FROM tenders WHERE publishing_org=? AND id!=?"
            " AND retender_of IS NULL",
            (old["publishing_org"] or "", old["id"]))
        for new in cands:
            if token_set_ratio(old["title"] or "", new["title"] or "") < TITLE_MATCH:
                continue
            nd = _day(new["publish_date"]) or _day(new["bid_submission_end"])
            if od and nd:
                delta = (nd - od).days
                if not (0 <= delta <= window_days):
                    continue
            if not _values_close(old["estimated_value_inr"],
                                 new["estimated_value_inr"]):
                continue
            store.conn.execute(
                "UPDATE tenders SET retender_of=? WHERE id=?",
                (old["id"], new["id"]))
            linked += 1
            break
    store.conn.commit()
    return linked
