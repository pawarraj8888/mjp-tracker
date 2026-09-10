"""Adapter contract.

An adapter turns one portal into canonical records. Tier 2/3 adapters (Playwright
based) implement ``fetch_live``/``fetch_awards`` against their portal; the
mahatenders adapter additionally exposes ``*_from_state`` helpers that reuse the
existing tracker's already-scraped state so no re-scrape is needed to populate
the store.
"""

from __future__ import annotations

from typing import Protocol


class Adapter(Protocol):
    portal: str
    funding_level_default: str

    def fetch_live(self) -> list[dict]:
        """Canonical tender dicts for currently-open tenders."""

    def fetch_awards(self, keyword: str = "") -> list[dict]:
        """Canonical award dicts (award-of-contract records)."""


def empty_tender(portal: str, source_tender_id: str) -> dict:
    """A canonical tender dict with every field present and defaulted."""
    return {
        "source_portal": portal, "source_tender_id": source_tender_id,
        "publishing_org": "", "org_hierarchy": None, "category": "",
        "title": "", "description": "", "estimated_value_inr": 0,
        "emd_inr": 0, "tender_fee_inr": 0, "publish_date": "",
        "bid_submission_end": "", "bid_opening_date": "", "pre_bid_date": "",
        "district": "", "taluka": "", "location_raw": "",
        "funding_source": "unknown", "funding_scheme": "",
        "funding_level": "state", "status": "unknown", "raw": None,
        "first_seen_at": "", "last_seen_at": "",
    }
