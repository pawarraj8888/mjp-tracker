from pipeline import analytics, ingest
from pipeline.retender import link_retenders


def _t(portal, stid, **kw):
    base = {"source_portal": portal, "source_tender_id": stid,
            "publishing_org": "PWD Division X",
            "title": "Construction of bridge at village Y",
            "status": "live", "publish_date": "01-Mar-2026 10:00 AM",
            "estimated_value_inr": 5000000}
    base.update(kw)
    return base


def test_retender_links_new_to_cancelled(store):
    store.upsert_tender(_t("m", "OLD", status="cancelled",
                           publish_date="01-Jan-2026 10:00 AM"))
    store.upsert_tender(_t("m", "NEW", estimated_value_inr=5050000))
    assert link_retenders(store) == 1
    old_id = store.query(
        "SELECT id FROM tenders WHERE source_tender_id='OLD'")[0]["id"]
    new = store.query(
        "SELECT retender_of FROM tenders WHERE source_tender_id='NEW'")[0]
    assert new["retender_of"] == old_id


def test_retender_respects_window(store):
    store.upsert_tender(_t("m", "OLD", status="cancelled",
                           publish_date="01-Jan-2026 10:00 AM"))
    store.upsert_tender(_t("m", "NEW", publish_date="01-Dec-2026 10:00 AM"))
    assert link_retenders(store) == 0  # > 180 days apart


def test_retender_needs_value_and_title_match(store):
    store.upsert_tender(_t("m", "OLD", status="cancelled",
                           publish_date="01-Jan-2026 10:00 AM"))
    store.upsert_tender(_t("m", "NEW", title="Completely different water work",
                           estimated_value_inr=5000000))
    assert link_retenders(store) == 0


def test_export_bundle(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    b = analytics.export_bundle(store)
    assert set(b) >= {"summary", "top_contractors", "by_district",
                      "value_bands", "by_funding_scheme", "coverage"}
    assert b["summary"]["counts"]["awards"] == 2
    assert any(d["district"] == "Jalgaon" for d in b["by_district"])
    assert sum(v["count"] for v in b["value_bands"]) == b["summary"]["counts"]["tenders"]
