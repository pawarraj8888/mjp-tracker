from pipeline.entities import resolve_contractor


def test_variants_merge_to_one_contractor(store):
    a = resolve_contractor(store, "M/s Shree Constructions Pvt Ltd")
    b = resolve_contractor(store, "Shree Constructions")
    assert a == b
    rows = store.all_contractors()
    assert len(rows) == 1
    aliases = rows[0]["aliases"]
    assert "Shree Constructions" in aliases
    assert "Shree Constructions Pvt Ltd" in aliases


def test_distinct_firms_stay_separate(store):
    resolve_contractor(store, "Shree Constructions")
    resolve_contractor(store, "Patil Infra")
    assert len(store.all_contractors()) == 2


def test_empty_name_returns_none(store):
    assert resolve_contractor(store, "   ") is None
    assert len(store.all_contractors()) == 0


def test_fuzzy_band_queues_review(store):
    # Two names close enough to be suspicious (85..95) but not auto-merged.
    resolve_contractor(store, "Siddheshwar Majur Sahakari Sanstha Jalgaon")
    resolve_contractor(store, "Siddheshwar Majur Sahakari Sanstha Chalisgaon")
    review = store.query("SELECT * FROM contractors_review")
    # both distinct contractors kept
    assert len(store.all_contractors()) == 2
    # and the near-duplicate was flagged for a human
    assert len(review) >= 1
    assert 85.0 <= review[0]["score"] < 95.0
