"""mahatenders.gov.in (NIC GePNIC) adapter - Tier 1.

Reuses the existing single-file tracker (``tracker.py``) rather than
re-scraping: the tracker already produces live/seen/details/awards state for
all watched organisations, and this adapter maps that state into the canonical
schema. ``fetch_live`` can additionally scrape live via the tracker.
"""

from __future__ import annotations

import re

import money

from .. import text
from .base import empty_tender

PORTAL = "mahatenders"


def _tracker():
    import tracker  # heavy (reportlab etc.); imported lazily
    return tracker


def _org_hierarchy(org_chain: str) -> dict:
    parts = [p.strip() for p in (org_chain or "").split("||") if p.strip()]
    return {
        "chain": parts,
        "department": parts[0] if parts else "",
        "agency": parts[1] if len(parts) > 1 else "",
        "region": parts[-2] if len(parts) > 2 else "",
        "division": parts[-1] if parts else "",
    }


def _category(details: dict) -> str:
    c = (details.get("Tender Category") or "").casefold()
    for key in ("works", "goods", "services", "consultancy"):
        if key in c:
            return key
    return ""


def _to_int(s) -> int | None:
    if not s:
        return None
    m = re.search(r"\d+", str(s))
    return int(m.group(0)) if m else None


def _status(tid: str, live_row, seen_entry, awards: dict) -> str:
    if live_row is not None:
        return "live"
    if tid in awards:
        return "awarded"
    if seen_entry:
        return "closed"
    return "unknown"


def tender_records(live_rows, seen, details_all, awards=None) -> list[dict]:
    tr = _tracker()
    awards = awards or {}
    seen = seen or {}
    details_all = details_all or {}
    live_map = {r["tender_id"]: r for r in (live_rows or [])}
    recs = []
    for tid in set(live_map) | set(seen) | set(awards):
        r = live_map.get(tid)
        e = seen.get(tid, {})
        d = details_all.get(tid) or (awards.get(tid, {}) or {}).get("fields", {}) or {}
        org_chain = (r or e).get("org_chain") or d.get("Organisation Chain") or ""
        title = (r or e).get("title") or d.get("Title") or d.get("Tender Title :") or ""
        desc = d.get("Work Description") or ""
        location = d.get("Location") or ""
        ctx = " ".join([title, desc, org_chain])
        rec = empty_tender(PORTAL, tid)
        rec.update({
            "publishing_org": tr.publisher(org_chain),
            "org_hierarchy": _org_hierarchy(org_chain),
            "category": _category(d),
            "title": text.clean(title),
            "description": text.clean(desc),
            # Estimated (pre-bid) value only -- never the award amount. Exact
            # decimal string; unknown stays None.
            "estimated_value_inr": tr.parse_inr_str(
                d.get("Tender Value in ₹") or ""),
            "emd_inr": tr.parse_inr_str(d.get("EMD Amount in ₹") or ""),
            "tender_fee_inr": tr.parse_inr_str(d.get("Tender Fee in ₹") or ""),
            "publish_date": (r.get("published") if r else e.get("published", ""))
            or d.get("Published Date", ""),
            "bid_submission_end": (r or e).get("closing")
            or d.get("Bid Submission End Date", ""),
            "bid_opening_date": (r.get("opening") if r else e.get("opening", ""))
            or d.get("Bid Opening Date", ""),
            "pre_bid_date": d.get("Pre Bid Meeting Date", ""),
            "district": tr.derive_city(location, org_chain, title),
            "taluka": tr.normalize_city(location) if location else "",
            "location_raw": location,
            "funding_scheme": text.funding_scheme(ctx),
            "funding_source": text.funding_source(ctx),
            "funding_level": text.funding_level(ctx, PORTAL),
            "status": _status(tid, r, e, awards),
            "first_seen_at": e.get("first_seen", ""),
            "raw": d or None,
        })
        recs.append(rec)
    return recs


def award_records(awards, details_all=None) -> list[dict]:
    tr = _tracker()
    details_all = details_all or {}
    out = []
    for tid, entry in (awards or {}).items():
        ai = entry.get("award") or tr.extract_award_info(entry.get("fields", {}), "")
        name = tr.display_contractor(ai.get("contractor", ""))
        if not name:
            continue
        d = details_all.get(tid) or entry.get("fields", {}) or {}
        est = money.str_to_dec(tr.parse_inr_str(d.get("Tender Value in ₹") or ""))
        val = money.str_to_dec(ai.get("awarded_value"))  # Decimal | None
        bidders = ai.get("bidders") or []
        # The AOC "Awarded Bids List" names only the winner, so a count of 1 is
        # not evidence of a single participant. Mark coverage so the analytics
        # never reads competition into winner-only data.
        has_loser = any(
            b.get("status") and not tr._AWARDED_STATUS_RE.search(b["status"])
            for b in bidders)
        coverage = "full" if has_loser else "winner_only"
        l1 = None
        if est and val is not None and est > 0:
            l1 = round(float(val / est) * 100.0, 2)
        out.append({
            "source_tender_id": tid,
            "contractor_name_raw": name,
            "award_value_inr": money.dec_to_str(val),  # exact string | None
            "award_date": ai.get("contract_date", ""),
            "completion_period_days": _to_int(
                entry.get("fields", {}).get("Work Completion Period (in days) :")),
            "bidder_count": len(bidders) or None,
            "bidder_coverage": coverage,
            "l1_pct_vs_estimate": l1,
            "source_url": "",
            "bidders": bidders,
            "raw": ai,
        })
    return out


def fetch_live(session=None) -> list[dict]:
    """Scrape live tenders across all watches via the tracker, then map to
    canonical records. Network call; used by the polling/backfill runners."""
    tr = _tracker()
    session = session or tr.make_session()
    rows = tr.fetch_all_watch_rows(session, include_keyword_scan=False)
    tr.cache_missing_details(session, rows)
    return tender_records(rows, tr.load_seen(), tr.load_details_cache(),
                          tr.load_awards())
