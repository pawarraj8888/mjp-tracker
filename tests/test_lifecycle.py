"""Tender lifecycle events, notifications and data-status."""

from pipeline import ingest, notifications, status


def test_award_creates_event_and_sets_status(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    # Every awarded tender is marked awarded and has exactly one award event.
    awarded = store.query("SELECT id FROM tenders WHERE status='awarded'")
    assert len(awarded) == 2
    events = store.query("SELECT event_type FROM tender_events")
    assert len(events) == 2
    # One award was on a tender we had seen before (a real open->awarded
    # transition); the other was award-only (discovered). We never fabricate a
    # prior open state for the discovered one.
    kinds = {e["event_type"] for e in events}
    assert kinds == {"awarded", "award_record_discovered"}


def test_events_are_idempotent(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    first = store.counts()["tender_events"]
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    assert store.counts()["tender_events"] == first  # no duplicates on re-run


def test_notifications_stable_and_labelled(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    a = notifications.build(store)
    b = notifications.build(store)
    ids_a = [n["id"] for n in a["notifications"]]
    ids_b = [n["id"] for n in b["notifications"]]
    assert ids_a == ids_b and len(ids_a) == len(set(ids_a))  # stable + unique
    kinds = {n["kind"] for n in a["notifications"]}
    # A discovered award is historical_import; a real transition is procurement.
    assert kinds == {"historical_import", "procurement"}
    discovered = next(n for n in a["notifications"]
                      if n["kind"] == "historical_import")
    assert "Award record discovered" in discovered["message"]
    assert "Now available in Awards" in discovered["message"]
    transition = next(n for n in a["notifications"]
                      if n["kind"] == "procurement")
    assert "Tender awarded:" in transition["message"]


def test_correction_notices_are_not_awards():
    corrections = {"corrections": [{
        "tender_id": "2026_X_1_1", "contractor": "ABC",
        "raw_source_text": "INR 1,255,710.93", "corrected_value": "1255710.93",
        "corrected_display": "12.56 L"}]}
    out = notifications.from_corrections(corrections)
    assert len(out) == 1
    assert out[0]["type"] == "data_correction"
    assert out[0]["kind"] == "data_correction"
    assert "not a new award" in out[0]["message"]


def test_data_status_reports_ist_and_overall(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    st = status.build(store)
    assert st["overall_status"] in (
        "up_to_date", "partial", "stale", "failed", "unknown")
    assert st["cadence_minutes"] == 15
    assert "+05:30" in st["generated"]  # timezone-aware IST
    assert isinstance(st["sources"], list)
