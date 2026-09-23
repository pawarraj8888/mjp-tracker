"""Tender amount visibility.

The portal publishes no "Tender Value in Rs" for a large minority of tenders
(it shows "NA"). For those we surface a clearly-labelled estimate derived from
the EMD (1% rule) and never present it as the exact figure. These tests pin the
estimate guardrails and the value-source classification that drives the display.
"""

import json

import tracker


class TestEmdValueEstimate:
    def test_one_percent_rule(self):
        # WADAKI: EMD 47,798 -> estimate 47,79,800 (EMD x100).
        assert tracker.emd_value_estimate(
            {"EMD Amount in ₹": "47,798"}) == tracker.money.str_to_dec("4779800")

    def test_token_emd_is_rejected(self):
        # A Re.1 / exempt EMD must not yield a nonsense Rs.100 estimate.
        assert tracker.emd_value_estimate({"EMD Amount in ₹": "1"}) is None

    def test_below_floor_is_rejected(self):
        assert tracker.emd_value_estimate({"EMD Amount in ₹": "500"}) is None

    def test_missing_emd_is_none(self):
        assert tracker.emd_value_estimate({}) is None
        assert tracker.emd_value_estimate({"EMD Amount in ₹": "NA"}) is None


class TestValueSource:
    def _data(self, monkeypatch, live, details, awards=None):
        monkeypatch.setattr(tracker, "load_seen", lambda: {})
        monkeypatch.setattr(tracker, "load_details_cache", lambda: details)
        monkeypatch.setattr(tracker, "load_awards", lambda: awards or {})
        monkeypatch.setattr(tracker, "keyword_watch_names", lambda: [])
        monkeypatch.setitem(tracker._dash, "live", live)
        return {t["id"]: t for t in tracker.dashboard_data()["tenders"]}

    def _row(self, tid, title="T"):
        return {"tender_id": tid, "title": title, "org_chain": "X||Y",
                "source": "s", "sources": ["s"], "published": "01-Sep-2026 10:00 AM",
                "closing": "30-Sep-2026 03:00 PM", "opening": "01-Oct-2026 11:00 AM",
                "url": "https://mahatenders.gov.in/x", "ref_no": "R"}

    def test_exact_value_wins(self, monkeypatch):
        rows = self._data(monkeypatch, [self._row("A")],
                          {"A": {"Tender Value in ₹": "1,45,00,000",
                                 "EMD Amount in ₹": "1,45,000"}})
        assert rows["A"]["valueSource"] == "exact"
        assert rows["A"]["valueFmt"]
        assert rows["A"]["estValueFmt"] == ""     # no estimate when exact known

    def test_emd_estimate_when_value_na(self, monkeypatch):
        rows = self._data(monkeypatch, [self._row("B")],
                          {"B": {"Tender Value in ₹": "NA",
                                 "EMD Amount in ₹": "47,798"}})
        assert rows["B"]["valueSource"] == "emd_estimate"
        assert rows["B"]["valueFmt"] == ""        # exact stays unknown
        assert rows["B"]["estValueFmt"]           # labelled estimate present
        assert rows["B"]["valueNum"] == 4779800.0  # falls back for sort/filter

    def test_not_published_when_no_basis(self, monkeypatch):
        rows = self._data(monkeypatch, [self._row("C")],
                          {"C": {"Tender Value in ₹": "NA", "EMD Amount in ₹": "NA"}})
        assert rows["C"]["valueSource"] == "not_published"
        assert rows["C"]["estValueFmt"] == ""
        assert rows["C"]["valueNum"] == -1        # unknown sorts to the bottom

    def test_no_detail_stays_unknown(self, monkeypatch):
        rows = self._data(monkeypatch, [self._row("D")], {})
        assert rows["D"]["valueSource"] == "no_detail"
        assert rows["D"]["valueNum"] == -1
