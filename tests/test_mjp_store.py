"""MJP store: deterministic ids, idempotent upserts, no wrong merges."""

from pipeline.store import Store
from pipeline.mjp import store as mstore


def _store():
    return Store(":memory:")


def test_project_id_is_deterministic():
    a = mstore.project_uid("Amravati", "Chikhaldara", "water_supply", "c1")
    b = mstore.project_uid("Amravati", "Chikhaldara", "water_supply", "c1")
    assert a == b


def test_different_towns_do_not_merge():
    a = mstore.project_uid("Amravati", "Chikhaldara", "water_supply", "c1")
    b = mstore.project_uid("Amravati", "Achalpur", "water_supply", "c2")
    assert a != b


def test_unknown_municipality_is_singleton():
    # Two unknown-place documents must NOT collapse into one project.
    a = mstore.project_uid("", "", "water_supply", "codeA")
    b = mstore.project_uid("", "", "water_supply", "codeB")
    assert a != b


def test_upsert_project_idempotent_and_preserves_first_detected():
    s = _store()
    pid = mstore.project_uid("Amravati", "Chikhaldara", "water_supply", "c1")
    proj = {"id": pid, "municipality": "Chikhaldara", "district": "Amravati",
            "approved_cost_inr": "249925000", "first_detected_at": "2026-06-25",
            "last_checked_at": "2026-09-01"}
    mstore.upsert_project(s, proj)
    proj2 = dict(proj, first_detected_at="2026-09-17",
                 last_checked_at="2026-09-17")
    mstore.upsert_project(s, proj2)
    rows = s.query("SELECT first_detected_at, last_checked_at FROM mjp_projects")
    assert len(rows) == 1
    assert rows[0]["first_detected_at"] == "2026-06-25"   # preserved
    assert rows[0]["last_checked_at"] == "2026-09-17"      # advanced


def test_add_event_idempotent():
    s = _store()
    pid = mstore.project_uid("A", "B", "water_supply", "c")
    mstore.upsert_project(s, {"id": pid})
    first = mstore.add_event(s, pid, "funding_sanctioned", {"x": 1},
                             event_date="2026-06-25", source_doc_code="c")
    again = mstore.add_event(s, pid, "funding_sanctioned", {"x": 1},
                             event_date="2026-06-25", source_doc_code="c")
    assert first is True and again is False
    assert s.query("SELECT COUNT(*) c FROM mjp_events")[0]["c"] == 1


def test_document_idempotent_on_code():
    s = _store()
    pid = mstore.project_uid("A", "B", "water_supply", "c")
    mstore.upsert_project(s, {"id": pid})
    doc = {"project_id": pid, "doc_code": "202606251057023925",
           "doc_type": "funding_sanction", "first_detected_at": "2026-06-25"}
    mstore.upsert_document(s, doc)
    mstore.upsert_document(s, dict(doc, first_detected_at="2026-09-17"))
    rows = s.query("SELECT first_detected_at FROM mjp_documents")
    assert len(rows) == 1
    assert rows[0]["first_detected_at"] == "2026-06-25"


def test_link_and_human_decision():
    s = _store()
    pid = mstore.project_uid("A", "B", "water_supply", "c")
    mstore.upsert_project(s, {"id": pid})
    mstore.link_tender(s, pid, "mahatenders", "T1", "suggested", 0.85,
                       [{"signal": "town"}])
    mstore.set_link_decision(s, pid, "T1", "linked", "operator", "2026-09-17")
    row = s.query("SELECT link_status, confirmed_by FROM mjp_tender_links")[0]
    assert row["link_status"] == "linked"
    assert row["confirmed_by"] == "operator"
