"""Regression tests for defects found in the pipeline code review."""

from pipeline import analytics, entities, ingest, text
from pipeline.store import Store


def test_stdlib_fallback_does_not_over_merge(monkeypatch):
    # Force the rapidfuzz-absent path (a shipped configuration).
    monkeypatch.setattr(text, "_rf_fuzz", None)
    # Devanagari tokens must survive tokenisation, so distinct names differ.
    assert text.token_set_ratio("ABC आमदार निधी", "ABC खासदार रस्ता") < 100
    # Two different strings that both tokenise to nothing are NOT a match.
    assert text.token_set_ratio("!!!", "???") == 0.0
    assert text.token_set_ratio("!!!", "!!!") == 100.0


def test_fallback_keeps_devanagari_firms_distinct(monkeypatch):
    monkeypatch.setattr(text, "_rf_fuzz", None)
    s = Store(":memory:")
    try:
        a = entities.resolve_contractor(s, "ABC आमदार निधी बांधकाम")
        b = entities.resolve_contractor(s, "ABC खासदार रस्ता विकास")
        assert a != b
        assert len(s.all_contractors()) == 2
    finally:
        s.close()


def test_time_to_award_is_numeric(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    rows = analytics.time_to_award(store)
    assert rows, "expected at least one org with a computable time-to-award"
    r = next(x for x in rows if x["org"] == "EE PWD Division Jalgaon")
    # bid close 10-Jun-2026 -> award 01-Sep-2026 is ~83 days
    assert isinstance(r["avg_days"], (int, float))
    assert 80 <= r["avg_days"] <= 86


def test_coverage_survives_off_format_date(store, sample_state):
    # Inject a tender whose bid_submission_end is ISO, not dd-Mon-yyyy.
    state = dict(sample_state)
    state["seen"] = dict(sample_state["seen"])
    state["seen"]["2026_ODD_1_1"] = {
        "title": "Odd date work", "closing": "2026-07-01",
        "org_chain": "Maharashtra||PWD||EE PWD Division Jalgaon",
        "source": "Jalgaon statewide", "first_seen": "2026-07-01T00:00:00",
    }
    # Must not raise (previously int(substr(...)) crashed the whole ingest).
    result = ingest.ingest_from_state(store, "mahatenders", state)
    assert result["counts"]["tenders"] == 4
    assert analytics.coverage(store) is not None


def test_dedupe_hash_value_dimension_is_live():
    from pipeline.dedupe import dedupe_hash
    near = dedupe_hash({"publishing_org": "X", "title": "t",
                        "bid_submission_end": "01-Jan-2026", "estimated_value_inr": 1_000_000})
    far = dedupe_hash({"publishing_org": "X", "title": "t",
                       "bid_submission_end": "01-Jan-2026", "estimated_value_inr": 100_000_000})
    assert near != far  # value now actually affects the hash
