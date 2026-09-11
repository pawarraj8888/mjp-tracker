"""Read-only analytics over the canonical store.

Every function returns plain dicts/lists so it can back a CLI, an API, or the
dashboard. Monetary aggregation is done in Python with :mod:`decimal` for exact
arithmetic (values are stored as exact decimal strings); results are serialized
as exact decimal strings and formatted only for presentation. Estimated value
(pre-bid) and awarded value are kept strictly separate and never substituted
for one another.
"""

from __future__ import annotations

from collections import defaultdict

import money

from .store import Store


def _sum(rows, col) -> str:
    return money.dec_to_str(money.add(r.get(col) for r in rows))


def top_contractors(store: Store, limit: int = 25) -> list[dict]:
    rows = store.query(
        "SELECT c.canonical_name AS contractor, c.id AS cid,"
        " a.award_value_inr AS v FROM awards a"
        " JOIN contractors c ON c.id = a.contractor_id")
    agg: dict = {}
    for r in rows:
        cell = agg.setdefault(r["contractor"], {"contracts": 0, "vals": [],
                                                "known": 0})
        cell["contracts"] += 1
        if money.is_known(r["v"]):
            cell["vals"].append(r["v"])
            cell["known"] += 1
    out = []
    for name, cell in agg.items():
        total = money.add(cell["vals"])
        out.append({
            "contractor": name,
            "contracts": cell["contracts"],
            "known_value_contracts": cell["known"],
            "total_value_inr": money.dec_to_str(total),
            "total_value_fmt": money.format_inr(total),
        })
    out.sort(key=lambda x: money.str_to_dec(x["total_value_inr"]) or money.add([]),
             reverse=True)
    return out[:limit]


def floated_value_by_org_month(store: Store, limit: int = 100) -> list[dict]:
    """Estimated (floated) value per org per publish-month, exact. Retendered
    tenders are excluded so a superseded original is not double counted."""
    rows = store.query(
        "SELECT publishing_org org, publish_date pd, estimated_value_inr v"
        " FROM tenders WHERE status != 'retendered' OR status IS NULL")
    agg: dict = defaultdict(lambda: {"tenders": 0, "vals": []})
    for r in rows:
        pd = r["pd"] or ""
        month = pd[6:10] + "-" + pd[3:6] if len(pd) >= 10 else "(unknown)"
        cell = agg[(r["org"] or "", month)]
        cell["tenders"] += 1
        cell["vals"].append(r["v"])
    out = [{"org": k[0], "month": k[1], "tenders": c["tenders"],
            "floated_value_inr": money.dec_to_str(money.add(c["vals"]))}
           for k, c in agg.items()]
    out.sort(key=lambda x: money.str_to_dec(x["floated_value_inr"]) or money.add([]),
             reverse=True)
    return out[:limit]


def contractor_pairs(store: Store, limit: int = 25) -> list[dict]:
    """Pairs of bidders that repeatedly appear on the same tenders (a cartel
    smell). Sparse until portals that publish full bid lists are ingested."""
    return store.query(
        "SELECT b1.bidder_name a, b2.bidder_name b, COUNT(*) shared"
        " FROM bids b1 JOIN bids b2"
        "   ON b1.tender_id = b2.tender_id AND b1.bidder_name < b2.bidder_name"
        " GROUP BY a, b HAVING shared > 1 ORDER BY shared DESC LIMIT ?", (limit,))


def award_ratio(store: Store, limit: int = 100) -> list[dict]:
    """Awarded value vs. the tender's own estimate, per award. Only rows where
    BOTH the estimate and the award value are known are returned."""
    rows = store.query(
        "SELECT t.publishing_org AS org, a.contractor_name_raw AS contractor,"
        " t.estimated_value_inr AS estimated, a.award_value_inr AS awarded"
        " FROM awards a JOIN tenders t ON t.id = a.tender_id")
    out = []
    for r in rows:
        est = money.str_to_dec(r["estimated"])
        aw = money.str_to_dec(r["awarded"])
        if est is None or aw is None or est <= 0:
            continue
        out.append({
            "org": r["org"], "contractor": r["contractor"],
            "estimated": money.dec_to_str(est), "awarded": money.dec_to_str(aw),
            "ratio": round(float(aw / est), 4),
        })
    out.sort(key=lambda x: x["ratio"], reverse=True)
    return out[:limit]


def bidding_summary(store: Store) -> dict:
    """Honest bidding coverage. The AOC page lists only the winner, so most
    awards are 'winner_only' -- a bidder_count of 1 there is NOT a single
    participant. Single-bidder rate is computed ONLY over awards with a full
    observed bidder list; the denominator and the unknown count are exposed."""
    rows = store.query(
        "SELECT bidder_coverage AS cov, bidder_count AS n FROM awards")
    total = len(rows)
    full = [r for r in rows if r["cov"] == "full"]
    winner_only = total - len(full)
    single = sum(1 for r in full if (r["n"] or 0) == 1)
    return {
        "awards_total": total,
        "with_full_bidder_list": len(full),
        "winner_only": winner_only,
        "single_bidder_measurable": len(full),
        "single_bidder_count": single,
        "single_bidder_rate": (round(single / len(full), 4) if full else None),
        "note": ("Single-bidder rate is measurable only where the full bidder "
                 "list was observed. Winner-only records (%d of %d) are "
                 "excluded; one known winner is not evidence of one bidder."
                 % (winner_only, total)),
    }


def single_bidder(store: Store) -> list[dict]:
    """Per-org single-bidder rate, over full-coverage awards only (usually
    empty until a portal that publishes full bid lists is ingested)."""
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
    """Average days from bid close to award (contract date), per org. Computed
    in Python because the portal dates are '%d-%b-%Y' style."""
    from .dedupe import _day
    rows = store.query(
        "SELECT t.publishing_org org, a.award_date ad, t.bid_submission_end be"
        " FROM awards a JOIN tenders t ON t.id = a.tender_id"
        " WHERE a.award_date != '' AND t.bid_submission_end != ''")
    agg: dict = defaultdict(list)
    for r in rows:
        d1, d2 = _day(r["ad"]), _day(r["be"])
        if d1 is None or d2 is None:
            continue
        agg[r["org"]].append((d1 - d2).days)
    out = [{"org": org, "n": len(days),
            "avg_days": round(sum(days) / len(days), 1)}
           for org, days in agg.items() if days]
    out.sort(key=lambda x: x["avg_days"], reverse=True)
    return out


def by_district(store: Store, limit: int = 40) -> list[dict]:
    rows = store.query(
        "SELECT COALESCE(NULLIF(district,''),'(unknown)') district,"
        " estimated_value_inr v FROM tenders")
    agg: dict = defaultdict(lambda: {"tenders": 0, "vals": []})
    for r in rows:
        cell = agg[r["district"]]
        cell["tenders"] += 1
        cell["vals"].append(r["v"])
    out = [{"district": k, "tenders": c["tenders"],
            "floated_inr": money.dec_to_str(money.add(c["vals"])),
            "floated_fmt": money.format_inr(money.add(c["vals"]))}
           for k, c in agg.items()]
    out.sort(key=lambda x: money.str_to_dec(x["floated_inr"]) or money.add([]),
             reverse=True)
    return out[:limit]


def by_funding_scheme(store: Store) -> list[dict]:
    rows = store.query(
        "SELECT COALESCE(NULLIF(funding_scheme,''),'(unclassified)') scheme,"
        " estimated_value_inr v FROM tenders")
    agg: dict = defaultdict(lambda: {"tenders": 0, "vals": []})
    for r in rows:
        cell = agg[r["scheme"]]
        cell["tenders"] += 1
        cell["vals"].append(r["v"])
    out = [{"scheme": k, "tenders": c["tenders"],
            "floated_inr": money.dec_to_str(money.add(c["vals"])),
            "floated_fmt": money.format_inr(money.add(c["vals"]))}
           for k, c in agg.items()]
    out.sort(key=lambda x: x["tenders"], reverse=True)
    return out


_BANDS = [
    ("100 Cr +", money.str_to_dec("1000000000")),
    ("10-100 Cr", money.str_to_dec("100000000")),
    ("1-10 Cr", money.str_to_dec("10000000")),
    ("10 L - 1 Cr", money.str_to_dec("1000000")),
    ("< 10 L", money.str_to_dec("0")),
]


def value_bands(store: Store) -> list[dict]:
    """Estimated-value distribution. Tenders with no known estimate are a
    distinct '(no value)' band, never folded into zero."""
    rows = store.query("SELECT estimated_value_inr v FROM tenders")
    counts = {name: 0 for name, _ in _BANDS}
    unknown = 0
    for r in rows:
        d = money.str_to_dec(r["v"])
        if d is None:
            unknown += 1
            continue
        for name, floor in _BANDS:
            if d >= floor:
                counts[name] += 1
                break
    out = [{"band": name, "count": counts[name]} for name, _ in _BANDS]
    out.append({"band": "(no value)", "count": unknown})
    return out


def summary(store: Store) -> dict:
    c = store.counts()
    awarded = _sum(store.query("SELECT award_value_inr FROM awards"),
                   "award_value_inr")
    floated = _sum(store.query("SELECT estimated_value_inr FROM tenders"),
                   "estimated_value_inr")
    awards_known = store.query(
        "SELECT COUNT(*) n FROM awards WHERE award_value_inr IS NOT NULL"
    )[0]["n"]
    tenders_known = store.query(
        "SELECT COUNT(*) n FROM tenders WHERE estimated_value_inr IS NOT NULL"
    )[0]["n"]
    return {
        "counts": c,
        # Estimated (pre-bid) value floated across tracked tenders.
        "total_floated_value_inr": floated,
        "total_floated_value_fmt": money.format_inr(floated),
        "tenders_with_known_estimate": tenders_known,
        # Awarded (contract) value across tracked awards. A different measure;
        # never compared 1:1 with floated value across different populations.
        "total_awarded_value_inr": awarded,
        "total_awarded_value_fmt": money.format_inr(awarded),
        "awards_with_known_value": awards_known,
    }


def reconciliation(store: Store) -> dict:
    """Explicit definitions for each tender population count, so the dashboard,
    the analytics export, and the raw state files can be reconciled instead of
    silently disagreeing."""
    counts = store.counts()
    live = store.query("SELECT COUNT(*) n FROM tenders WHERE status='live'")[0]["n"]
    awarded = store.query(
        "SELECT COUNT(*) n FROM tenders WHERE status='awarded'")[0]["n"]
    deduped = store.query(
        "SELECT COUNT(*) n FROM tenders WHERE extra_source_ids IS NOT NULL"
    )[0]["n"]
    return {
        "canonical_tenders": counts["tenders"],
        "definition": ("Distinct tenders in the canonical store after "
                       "cross-portal dedupe. This is the authoritative count."),
        "live": live,
        "awarded": awarded,
        "merged_across_portals": deduped,
        "awards": counts["awards"],
        "contractors": counts["contractors"],
    }


def export_bundle(store: Store) -> dict:
    """Compact analytics bundle for the dashboard. Values are exact decimal
    strings plus a preformatted display string; percentages are 0..1."""
    return {
        "summary": summary(store),
        "reconciliation": reconciliation(store),
        "bidding": bidding_summary(store),
        "top_contractors": top_contractors(store, 25),
        "single_bidder": single_bidder(store),
        "coverage": coverage(store),
        "by_district": by_district(store, 25),
        "by_funding_scheme": by_funding_scheme(store),
        "value_bands": value_bands(store),
        "time_to_award": time_to_award(store)[:25],
        "award_ratio": award_ratio(store, 25),
    }
