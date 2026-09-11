from pipeline import analytics, ingest
from pipeline.adapters import mahatenders


def test_ingest_from_state(store, sample_state):
    result = ingest.ingest_from_state(store, "mahatenders", sample_state)
    # 3 distinct tenders (1 live, 1 seen+award, 1 award-only)
    assert result["tenders"] == 3
    assert result["awards"] == 2
    c = store.counts()
    assert c["tenders"] == 3
    assert c["awards"] == 2
    # the two awards were won by the same firm under a name variant -> 1 contractor
    assert c["contractors"] == 1


def test_ingest_maps_fields(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    t = store.query(
        "SELECT * FROM tenders WHERE source_tender_id='2026_JALGA_9001_1'")[0]
    assert t["publishing_org"] == "RDD-CEO-JALGAON"
    assert t["district"] == "Jalgaon"
    # Amounts are stored as exact decimal strings now (not lossy ints).
    assert t["estimated_value_inr"] == "14500000"
    assert t["funding_scheme"] == "DPDC / District Planning"
    assert t["status"] == "live"


def test_award_links_and_ratio(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    a = store.query(
        "SELECT * FROM awards WHERE source_tender_id='mahatenders:2026_JALGA_9002_1'")[0]
    assert a["award_value_inr"] == "5900000"
    assert a["contractor_id"] is not None
    # The AOC list names only the winner, so coverage must be winner_only.
    assert a["bidder_coverage"] == "winner_only"
    # l1 pct vs estimate: 59,00,000 / 60,00,000
    assert 95 <= a["l1_pct_vs_estimate"] <= 100


def test_analytics_after_ingest(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    top = analytics.top_contractors(store)
    assert top and top[0]["contracts"] == 2
    assert top[0]["total_value_inr"] == "8300000"
    assert analytics.summary(store)["total_awarded_value_inr"] == "8300000"


def test_adapter_award_records_skip_unnamed():
    recs = mahatenders.award_records({
        "T": {"fields": {"Tender ID": "T"}, "award": {"contractor": ""}}})
    assert recs == []
