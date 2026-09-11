from pipeline import analytics, ingest


def test_all_analytics_views(store, sample_state):
    ingest.ingest_from_state(store, "mahatenders", sample_state)
    assert analytics.floated_value_by_org_month(store) is not None
    assert analytics.award_ratio(store)
    # Winner-only awards are correctly EXCLUDED from single-bidder analysis.
    assert analytics.single_bidder(store) == []
    b = analytics.bidding_summary(store)
    assert b["awards_total"] == 2
    assert b["winner_only"] == 2
    assert b["with_full_bidder_list"] == 0
    assert b["single_bidder_rate"] is None  # not measurable from winner-only data
    assert analytics.coverage(store)
    assert analytics.retender_rate(store)
    assert analytics.time_to_award(store)
    # contractor_pairs is empty with single-bidder data, but must not error
    assert analytics.contractor_pairs(store) == []
    s = analytics.summary(store)
    assert s["counts"]["awards"] == 2
