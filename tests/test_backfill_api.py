from pipeline import api, backfill, ingest
from pipeline.adapters import mahatenders


def test_backfill_checkpoints(store, monkeypatch):
    monkeypatch.setattr(mahatenders, "fetch_live", lambda session=None: [{
        "source_portal": "mahatenders", "source_tender_id": "B1",
        "publishing_org": "X Dept", "title": "test work",
        "estimated_value_inr": 1000, "bid_submission_end": "01-Jan-2026 03:00 PM",
    }])
    res = backfill.run("mahatenders", 2025, store=store)
    assert res["new"] == 1
    live = store.checkpoint_get("mahatenders", "live-metadata")
    assert live and live["done"] == 1
    aoc = store.checkpoint_get("mahatenders", "aoc-archive")
    assert aoc and aoc["done"] == 0  # captcha-gated, left pending
    # resuming skips the finished cursor
    assert "skipped" in backfill.run("mahatenders", 2025, store=store)


def test_api_routes(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    assert api.ROUTES["/health"](store, {})["ok"] is True
    top = api.ROUTES["/analytics/top-contractors"](store, {"limit": ["5"]})
    assert top and "contractor" in top[0]
    assert api.ROUTES["/contractors"](store, {})
    assert api.ROUTES["/tenders"](store, {"limit": ["10"]})
    assert "counts" in api.ROUTES["/summary"](store, {})
