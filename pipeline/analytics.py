"""Read-only analytics over the canonical store. Every function returns a list
of plain dicts so it can back a CLI, an API, or a notebook."""

from __future__ import annotations

from .store import Store


def top_contractors(store: Store, limit: int = 25) -> list[dict]:
    return store.query(
        "SELECT contractor, contracts, total_value_inr FROM v_top_contractors"
        " LIMIT ?", (limit,))


def floated_value_by_org_month(store: Store, limit: int = 100) -> list[dict]:
    return store.query(
        "SELECT * FROM v_floated_value_by_org_month"
        " ORDER BY floated_value_inr DESC LIMIT ?", (limit,))


def award_ratio(store: Store, limit: int = 100) -> list[dict]:
    return store.query(
        "SELECT org, contractor, estimated, awarded, ratio FROM v_award_ratio"
        " WHERE ratio IS NOT NULL ORDER BY ratio DESC LIMIT ?", (limit,))


def single_bidder(store: Store) -> list[dict]:
    return store.query(
        "SELECT * FROM v_single_bidder ORDER BY single_bidder_rate DESC")


def coverage(store: Store) -> list[dict]:
    return store.query(
        "SELECT org, year, closed, awarded, coverage FROM metrics_award_coverage"
        " ORDER BY year DESC, coverage ASC")


def retender_rate(store: Store) -> list[dict]:
    return store.query(
        "SELECT publishing_org org, COUNT(*) total,"
        " SUM(CASE WHEN retender_of IS NOT NULL THEN 1 ELSE 0 END) retenders,"
        " 1.0 * SUM(CASE WHEN retender_of IS NOT NULL THEN 1 ELSE 0 END)"
        "   / COUNT(*) rate"
        " FROM tenders GROUP BY org HAVING total > 0 ORDER BY rate DESC")


def time_to_award(store: Store) -> list[dict]:
    """Average days from bid close to award, per org. Computed in Python because
    the portal dates are '%d-%b-%Y' style, which SQLite julianday cannot parse."""
    from .dedupe import _day
    rows = store.query(
        "SELECT t.publishing_org org, a.award_date ad, t.bid_submission_end be"
        " FROM awards a JOIN tenders t ON t.id = a.tender_id"
        " WHERE a.award_date != '' AND t.bid_submission_end != ''")
    agg: dict = {}
    for r in rows:
        d1, d2 = _day(r["ad"]), _day(r["be"])
        if d1 is None or d2 is None:
            continue
        bucket = agg.setdefault(r["org"], [])
        bucket.append((d1 - d2).days)
    out = [{"org": org, "n": len(days),
            "avg_days": round(sum(days) / len(days), 1)}
           for org, days in agg.items() if days]
    out.sort(key=lambda x: x["avg_days"], reverse=True)
    return out


def contractor_pairs(store: Store, limit: int = 25) -> list[dict]:
    """Pairs of bidders that repeatedly appear on the same tenders (a cartel
    smell). Sparse until portals that publish full bid lists are ingested."""
    return store.query(
        "SELECT b1.bidder_name a, b2.bidder_name b, COUNT(*) shared"
        " FROM bids b1 JOIN bids b2"
        "   ON b1.tender_id = b2.tender_id AND b1.bidder_name < b2.bidder_name"
        " GROUP BY a, b HAVING shared > 1 ORDER BY shared DESC LIMIT ?", (limit,))


def summary(store: Store) -> dict:
    c = store.counts()
    val = store.query(
        "SELECT SUM(COALESCE(award_value_inr,0)) v FROM awards")[0]["v"] or 0
    return {"counts": c, "total_awarded_value_inr": val}
