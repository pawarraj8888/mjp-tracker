import json

from pipeline import __main__ as cli
from pipeline import ingest
from pipeline.store import Store


def _prime(db_path, state):
    s = Store(db_path)
    ingest.ingest_from_state(s, "mahatenders", state)
    s.close()


def test_cli_commands(tmp_path, monkeypatch, capsys, sample_state):
    dbp = str(tmp_path / "cli.sqlite")
    monkeypatch.setenv("PIPELINE_DB_PATH", dbp)

    assert cli.main(["init-db"]) == 0
    capsys.readouterr()  # discard init-db output
    _prime(dbp, sample_state)

    assert cli.main(["stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["tenders"] == 3

    assert cli.main(["analytics", "top"]) == 0
    top = json.loads(capsys.readouterr().out)
    assert top[0]["contracts"] == 2

    for view in ("floated", "ratio", "single-bidder", "coverage",
                 "retender", "time-to-award", "pairs"):
        assert cli.main(["analytics", view]) == 0
        capsys.readouterr()


def test_cli_unknown_view(tmp_path, monkeypatch, sample_state):
    dbp = str(tmp_path / "cli2.sqlite")
    monkeypatch.setenv("PIPELINE_DB_PATH", dbp)
    _prime(dbp, sample_state)
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["analytics", "does-not-exist"])
