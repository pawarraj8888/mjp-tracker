from pipeline.dedupe import dedupe_hash, find_duplicate


def _rec(portal, stid, **kw):
    base = {
        "source_portal": portal, "source_tender_id": stid,
        "publishing_org": "EE PWD Division Jalgaon",
        "title": "Road strengthening at Bhusawal section 2",
        "bid_submission_end": "10-Jun-2026 03:00 PM",
        "estimated_value_inr": 6000000,
    }
    base.update(kw)
    return base


def test_dedupe_hash_stable_and_value_tolerant():
    a = _rec("mahatenders", "1")
    b = _rec("mahatenders", "1", estimated_value_inr=6050000)  # within 2%
    assert dedupe_hash(a) == dedupe_hash(b)


def test_find_duplicate_matches_cross_portal(store):
    store.upsert_tender(_rec("mahatenders", "T1"))
    # same work on another portal, slightly different title/value/day
    dup = find_duplicate(store, _rec(
        "pwd", "P9", title="Road strengthening Bhusawal section 2",
        estimated_value_inr=6090000,
        bid_submission_end="11-Jun-2026 05:00 PM"))
    assert dup is not None
    assert dup["source_portal"] == "mahatenders"


def test_same_source_is_not_a_duplicate(store):
    store.upsert_tender(_rec("mahatenders", "T1"))
    assert find_duplicate(store, _rec("mahatenders", "T1")) is None


def test_different_value_is_not_a_duplicate(store):
    store.upsert_tender(_rec("mahatenders", "T1"))
    assert find_duplicate(store, _rec(
        "pwd", "P9", estimated_value_inr=99000000)) is None
