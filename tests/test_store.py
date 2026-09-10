from pipeline.store import Store, uid


def test_schema_initialised(store):
    c = store.counts()
    assert set(c) >= {"tenders", "awards", "contractors", "bids"}
    assert all(v == 0 for v in c.values())


def test_upsert_tender_idempotent(store):
    rec = {"source_portal": "mahatenders", "source_tender_id": "T1",
           "title": "A", "estimated_value_inr": 100}
    tid1, new1 = store.upsert_tender(rec)
    tid2, new2 = store.upsert_tender({**rec, "title": "A updated"})
    assert tid1 == tid2 == uid("tender", "mahatenders", "T1")
    assert new1 is True and new2 is False
    assert store.counts()["tenders"] == 1
    row = store.query("SELECT title FROM tenders WHERE id=?", (tid1,))[0]
    assert row["title"] == "A updated"


def test_memory_backend_selected():
    s = Store(":memory:")
    assert s.path == ":memory:"
    s.close()


def test_unknown_backend_rejected(monkeypatch):
    monkeypatch.setenv("PIPELINE_DB", "supabase")
    import pytest
    with pytest.raises(NotImplementedError):
        Store()
    monkeypatch.delenv("PIPELINE_DB")
