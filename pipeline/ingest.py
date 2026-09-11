"""Ingest canonical records into the store.

Tenders are deduped across portals (the survivor keeps both source ids); awards
resolve their contractor and link to the tender; bids are recorded where the
portal exposes them; award-coverage metrics are recomputed.
"""

from __future__ import annotations

import time

from .adapters import mahatenders
from .dedupe import dedupe_hash, find_duplicate
from .entities import resolve_contractor
from .store import Store, uid

ADAPTERS = {"mahatenders": mahatenders}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def ingest_tenders(store: Store, portal: str,
                   recs: list[dict]) -> tuple[int, int, dict]:
    """Returns (new, updated, alias_map) where alias_map maps
    (portal, source_tender_id) to the canonical tender id it resolved to
    (its own row, or a survivor's when it deduped) so awards/bids can link."""
    new = upd = 0
    alias_map: dict = {}
    for rec in recs:
        rec["dedupe_hash"] = dedupe_hash(rec)
        rec["last_seen_at"] = _now()
        dup = find_duplicate(store, rec)
        if dup is not None:
            alias_map[(portal, rec["source_tender_id"])] = dup["id"]
            # Same work from another portal already stored: record this portal's
            # id on the survivor rather than inserting a second row.
            extra = dup.get("extra_source_ids")
            ids = set()
            if extra:
                import json
                try:
                    ids = set(json.loads(extra))
                except ValueError:
                    ids = set()
            ids.add("%s:%s" % (portal, rec["source_tender_id"]))
            store.conn.execute(
                "UPDATE tenders SET extra_source_ids=?, last_seen_at=? WHERE id=?",
                (__import__("json").dumps(sorted(ids)), rec["last_seen_at"],
                 dup["id"]))
            store.conn.commit()
            upd += 1
            continue
        _tid, is_new = store.upsert_tender(rec)
        alias_map[(portal, rec["source_tender_id"])] = _tid
        new += int(is_new)
        upd += int(not is_new)
    return new, upd, alias_map


def ingest_awards(store: Store, portal: str, recs: list[dict],
                  alias_map: dict | None = None) -> int:
    alias_map = alias_map or {}
    n = 0
    for rec in recs:
        stid = rec["source_tender_id"]
        tender_id = alias_map.get((portal, stid)) or uid("tender", portal, stid)
        if not store.query("SELECT 1 FROM tenders WHERE id=?", (tender_id,)):
            # No parent tender (award for a tender we never listed); skip
            # rather than violate the foreign key.
            continue
        cid = resolve_contractor(store, rec["contractor_name_raw"], seen=_now())
        store.upsert_award({
            "tender_id": tender_id,
            "source_tender_id": "%s:%s" % (portal, stid),
            "contractor_id": cid,
            "contractor_name_raw": rec["contractor_name_raw"],
            "award_value_inr": rec.get("award_value_inr"),
            "award_date": rec.get("award_date"),
            "work_order_no": rec.get("work_order_no"),
            "completion_period_days": rec.get("completion_period_days"),
            "bidder_count": rec.get("bidder_count"),
            "l1_pct_vs_estimate": rec.get("l1_pct_vs_estimate"),
            "source_url": rec.get("source_url"),
            "raw": rec.get("raw"),
        })
        for b in rec.get("bidders", []):
            bname = b.get("name", "")
            if not bname:
                continue
            store.upsert_bid({
                "tender_id": tender_id,
                "source_tender_id": "%s:%s:%s" % (portal, stid, bname),
                "bidder_name": bname,
                "contractor_id": resolve_contractor(store, bname, seen=_now()),
                "quoted_value_inr": _inr(b.get("value")),
                "rank": "",  # portal AOC list does not expose a numeric rank
                "status": b.get("status", ""),
            })
        n += 1
    return n


def _inr(v) -> int | None:
    import re
    if not v:
        return None
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else None


def recompute_coverage(store: Store) -> None:
    """Per org per year, the share of non-live tenders that have an award.
    Year is parsed in Python (dates are '%d-%b-%Y' style, not ISO), so an
    off-format date is skipped rather than crashing the ingest."""
    from .dedupe import _day
    awarded_ids = {r["tender_id"] for r in
                   store.query("SELECT DISTINCT tender_id FROM awards")}
    rows = store.query(
        "SELECT id, publishing_org org, bid_submission_end be, status"
        " FROM tenders WHERE status != 'live'")
    agg: dict = {}
    for r in rows:
        d = _day(r["be"])
        if d is None:
            continue
        key = (r["org"] or "", d.year)
        cell = agg.setdefault(key, [0, 0])
        cell[0] += 1
        if r["status"] == "awarded" or r["id"] in awarded_ids:
            cell[1] += 1
    now = _now()
    for (org, year), (closed, awarded) in agg.items():
        store.conn.execute(
            "INSERT OR REPLACE INTO metrics_award_coverage"
            " (org, year, closed, awarded, coverage, computed_at)"
            " VALUES (?,?,?,?,?,?)",
            (org, year, closed, awarded,
             round(awarded / closed, 3) if closed else 0.0, now))
    store.conn.commit()


def ingest_from_state(store: Store, portal: str = "mahatenders",
                      state: dict | None = None) -> dict:
    """Ingest an adapter's already-scraped state. ``state`` may be injected
    (tests); otherwise it is read from the tracker's local files."""
    started = _now()
    if state is None:
        import tracker
        state = {
            "live_rows": tracker.load_live_snapshot().get("rows", []),
            "seen": tracker.load_seen(),
            "details": tracker.load_details_cache(),
            "awards": tracker.load_awards(),
        }
    adapter = ADAPTERS[portal]
    trecs = adapter.tender_records(state.get("live_rows"), state.get("seen"),
                                   state.get("details"), state.get("awards"))
    arecs = adapter.award_records(state.get("awards"), state.get("details"))
    new, upd, alias_map = ingest_tenders(store, portal, trecs)
    n_awards = ingest_awards(store, portal, arecs, alias_map)
    from .retender import link_retenders
    retenders = link_retenders(store)
    recompute_coverage(store)
    store.record_run(portal, started, _now(), new, upd, 0,
                     note="tenders=%d awards=%d retenders=%d"
                     % (len(trecs), n_awards, retenders))
    return {"tenders": len(trecs), "new": new, "updated": upd,
            "awards": n_awards, "retenders": retenders, "counts": store.counts()}
