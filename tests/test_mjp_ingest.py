"""MJP ingest orchestration (network-free): grouping into projects, event
history, repeated-ingestion idempotency, and cautious tender matching.

Extraction is stubbed so these exercise the orchestration, not PDF parsing
(covered in test_mjp_extract). Stored files use absolute local_paths, which
resolve regardless of ingest.ROOT (``ROOT / "/abs"`` == ``/abs``).
"""

import pytest

from pipeline.store import Store
from pipeline.mjp import export, ingest, store as mstore


def _doc(code, muni, district, doc_type="funding_sanction",
         approved="249925000", role="implementing_agency"):
    return {
        "doc_code": code, "gr_number": "GR-" + code[:4], "department": "नगर विकास",
        "doc_type": doc_type, "title_original": "%s निधी मंजूर" % muni,
        "language": "mr", "issue_date": "2026-06-25",
        "url": "https://x/%s.pdf" % code,
        "mjp_role": role,
        "mjp_role_evidence": "कार्यान्वयन यंत्रणा महाराष्ट्र जीवन प्राधिकरण",
        "mjp_role_page": 1, "mjp_role_confidence": 0.9,
        "approved_cost_inr": approved, "components_inr": ["2875000"],
        "components_sum_inr": "2875000", "scheme": "वैशिष्ट्यपूर्ण",
        "work_theme": "water_supply", "works_en": ["Intake Well @ Bagling Dam"],
        "municipality": muni, "district": district, "ocr_used": False,
        "extract_ok": True, "missing": [], "uncertainties": [], "page_count": 5,
    }


@pytest.fixture
def wired(tmp_path, monkeypatch):
    docs = tmp_path / "documents"
    docs.mkdir()
    table = {}

    def fake_extract(pdf_bytes, meta):
        return table[meta["url"].split("/")[-1].replace(".pdf", "")]

    monkeypatch.setattr(ingest.extract, "extract_document", fake_extract)
    return docs, table


def _prep(docs, table, code, doc):
    (docs / ("%s.pdf" % code)).write_bytes(b"%PDF-1.4")
    table[code] = doc


def _index(docs, *codes):
    idx = {}
    for c in codes:
        idx[c] = {"doc_code": c, "url": "https://x/%s.pdf" % c,
                  "department": "नगर विकास", "title": "t", "gr_date": "25-06-2026",
                  "local_path": str(docs / ("%s.pdf" % c)), "sha256": "abc",
                  "first_detected_at": "2026-06-25",
                  "last_checked_at": "2026-06-25"}
    return idx


def test_two_towns_stay_separate_projects(wired):
    docs, table = wired
    s = Store(":memory:")
    _prep(docs, table, "code_chikh", _doc("code_chikh", "Chikhaldara", "Amravati"))
    _prep(docs, table, "code_achal", _doc("code_achal", "Achalpur", "Amravati"))
    built = ingest.build_projects(s, _index(docs, "code_chikh", "code_achal"),
                                  "2026-09-17T00:00:00+05:30")
    assert built["projects"] == 2                # similar district, not merged


def test_multi_doc_project_has_dated_history(wired):
    docs, table = wired
    s = Store(":memory:")
    _prep(docs, table, "c_fund",
          _doc("c_fund", "Chikhaldara", "Amravati", "funding_sanction"))
    _prep(docs, table, "c_rev",
          _doc("c_rev", "Chikhaldara", "Amravati", "revised_sanction",
               approved="300000000"))
    ingest.build_projects(s, _index(docs, "c_fund", "c_rev"),
                          "2026-09-17T00:00:00+05:30")
    projs = mstore.all_projects(s)
    assert len(projs) == 1                       # same town+theme -> one project
    types = {e["event_type"] for e in mstore.events_for(s, projs[0]["id"])}
    assert {"project_discovered", "funding_sanctioned", "revised_sanction"} <= types
    assert projs[0]["revised_cost_inr"] == "300000000"   # kept separate


def test_repeated_ingestion_is_idempotent(wired):
    docs, table = wired
    s = Store(":memory:")
    _prep(docs, table, "c1", _doc("c1", "Beed", "Beed"))
    idx = _index(docs, "c1")
    ingest.build_projects(s, idx, "2026-09-17T00:00:00+05:30")
    ingest.build_projects(s, idx, "2026-09-17T00:15:00+05:30")
    assert len(mstore.all_projects(s)) == 1
    assert s.query("SELECT COUNT(*) c FROM mjp_documents")[0]["c"] == 1


def test_non_mjp_gr_is_not_tracked_as_a_project(wired):
    # A department GR with NO MJP role (e.g. a metro-rail GR) must not become a
    # tracked project, must not be reviewed, and must be marked mjp=False.
    docs, table = wired
    s = Store(":memory:")
    metro = _doc("m1", "", "", role="none")
    metro["mjp_role"] = "none"
    metro["mjp_role_confidence"] = 0.0
    metro["title_original"] = "पुणे मेट्रो रेल्वे प्रकल्प"
    _prep(docs, table, "m1", metro)
    index = _index(docs, "m1")
    built = ingest.build_projects(s, index, "2026-09-21T00:00:00+05:30")
    assert built["projects"] == 0                 # dropped, not shown
    assert built["review"] == 0                   # role=none is not "uncertain"
    assert mstore.all_projects(s) == []
    assert index["m1"]["mjp"] is False            # marked so it won't re-download


def test_prune_non_mjp_docs_deletes_original(wired):
    docs, table = wired
    s = Store(":memory:")
    _prep(docs, table, "m1", dict(_doc("m1", "", "", role="none"),
                                  mjp_role="none", mjp_role_confidence=0.0))
    index = _index(docs, "m1")
    ingest.build_projects(s, index, "2026-09-21T00:00:00+05:30")
    assert (docs / "m1.pdf").exists()
    n = ingest._prune_non_mjp_docs(index)
    assert n == 1 and not (docs / "m1.pdf").exists()


def test_incidental_mention_goes_to_review(wired):
    docs, table = wired
    s = Store(":memory:")
    d = _doc("cx", "Testville", "Beed", role="mentioned")
    d["mjp_role_confidence"] = 0.3
    _prep(docs, table, "cx", d)
    built = ingest.build_projects(s, _index(docs, "cx"),
                                  "2026-09-17T00:00:00+05:30")
    assert built["review"] >= 1
    items = mstore.review_items(s)
    assert any(r["item_type"] == "project_role_uncertain" for r in items)


def test_review_item_keeps_amount_evidence_and_pdf(wired):
    # A "mentioned" GR must (a) enrich the review row with the amount, the
    # supporting passage and the source URL, and (b) keep its original PDF (flag
    # "review", not pruned) so the queue survives a fresh-DB run.
    import json
    docs, table = wired
    s = Store(":memory:")
    d = _doc("cx", "Testville", "Beed", role="mentioned")
    d["mjp_role_confidence"] = 0.3
    _prep(docs, table, "cx", d)
    index = _index(docs, "cx")
    ingest.build_projects(s, index, "2026-09-17T00:00:00+05:30")

    item = next(r for r in mstore.review_items(s)
                if r["item_type"] == "project_role_uncertain")
    detail = json.loads(item["detail"])
    assert detail["amount_inr"] == "249925000"
    assert "जीवन" in detail["evidence"]
    assert detail["url"].endswith("cx.pdf")

    assert index["cx"]["mjp"] == "review"          # kept, not False
    assert ingest._prune_non_mjp_docs(index) == 0  # so the PDF is not pruned
    assert (docs / "cx.pdf").exists()


def test_suggested_match_flow(wired):
    docs, table = wired
    s = Store(":memory:")
    _prep(docs, table, "cc", _doc("cc", "Chikhaldara", "Amravati"))
    ingest.build_projects(s, _index(docs, "cc"), "2026-09-17T00:00:00+05:30")
    state = {"live_rows": [{"tender_id": "2026_X_1", "org_chain": "MJP Amravati",
                            "title": "Chikhaldara Water Supply Scheme"}],
             "details": {"2026_X_1": {
                 "Address": "Executive Engineer, MJP WM Division Amravati"}}}
    res = ingest.match_all(s, state, "2026-09-17T00:00:00+05:30")
    assert res["suggested"] == 1 and res["linked"] == 0
    p = export.build_feed(s)["projects"][0]
    assert p["tender_match_status"].startswith("Possible tender match")
    assert p["status_label"] == "Possible tender match"
    # Re-running on a different day must NOT duplicate the tender_suggested event.
    ingest.match_all(s, state, "2026-09-18T00:00:00+05:30")
    n = s.query("SELECT COUNT(*) c FROM mjp_events WHERE event_type='tender_suggested'")
    assert n[0]["c"] == 1
    # And the tender address enriched the (previously empty) MJP office.
    office = mstore.all_projects(s)[0]["mjp_office"]
    assert "MJP" in (office or "")
