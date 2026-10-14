"""MJP portal tenders ingest (network-free).

Pins the parse of MJP's own tenders page (mjp.maharashtra.gov.in/tenders), the
dd/mm/yyyy -> ISO date conversion, PDF-host validation, idempotent merge, and
the honest keep-existing-rows behaviour on a fetch failure.
"""

import json

import pytest

from pipeline.mjp import mjp_portal

_HTML = """
<table class="rwd-table">
  <tr><th>Name</th><th>Description</th><th>Region</th><th>Start Date</th>
      <th>End Date</th><th>Download</th></tr>
  <tr><td>E Tender Notice No. 4</td><td>Water works notice</td><td>Pune</td>
      <td>02/08/2021</td><td>01/09/2021</td>
      <td><a href="https://mjp.maharashtra.gov.in/wp-content/uploads/a.pdf">PDF</a></td></tr>
  <tr><td>Tender for Manpower</td><td>Manpower</td><td>All</td>
      <td>28/05/2021</td><td>08/06/2021</td>
      <td><a href="https://evil.example/x.pdf">PDF</a></td></tr>
</table>
"""


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # _valid_pdf_url does a live DNS lookup; make it a pure host check offline.
    from urllib.parse import urlparse
    monkeypatch.setattr(mjp_portal, "_valid_pdf_url",
                        lambda u: urlparse(u or "").hostname == mjp_portal.PORTAL_HOST
                        and urlparse(u).scheme == "https")


class TestParse:
    def test_fields_and_iso_dates(self):
        rows = mjp_portal.parse_tenders(_HTML)
        assert len(rows) == 2
        r = rows[0]
        assert r["name"] == "E Tender Notice No. 4"
        assert r["region"] == "Pune"
        assert r["start"] == "2021-08-02" and r["end"] == "2021-09-01"
        assert r["pdf_url"].endswith("a.pdf")
        assert r["id"]

    def test_off_host_pdf_is_dropped(self):
        rows = mjp_portal.parse_tenders(_HTML)
        assert rows[1]["pdf_url"] == ""   # evil.example link not kept

    def test_bad_date_is_blank(self):
        assert mjp_portal._iso("31/02/2021") == ""   # not a real date
        assert mjp_portal._iso("2021-01-01") == ""   # wrong format
        assert mjp_portal._iso("1/1/2020") == "2020-01-01"

    def test_no_table_returns_empty(self):
        assert mjp_portal.parse_tenders("<html>no table</html>") == []


class TestRun:
    def test_merge_idempotent_and_durable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mjp_portal, "FEED_FILE", tmp_path / "pt.json")
        monkeypatch.setattr(mjp_portal, "fetch_tenders",
                            lambda session=None: mjp_portal.parse_tenders(_HTML))
        r1 = mjp_portal.run(now="2026-10-14T00:00:00+05:30")
        assert r1 == {"fetched": 2, "added": 2, "total": 2, "ok": True}
        r2 = mjp_portal.run(now="2026-10-14T01:00:00+05:30")
        assert r2["added"] == 0 and r2["total"] == 2   # same rows, no dupes
        feed = json.loads((tmp_path / "pt.json").read_text())
        assert feed["tenders"][0]["first_seen"] == "2026-10-14T00:00:00+05:30"

    def test_fetch_failure_keeps_existing(self, tmp_path, monkeypatch):
        fp = tmp_path / "pt.json"
        monkeypatch.setattr(mjp_portal, "FEED_FILE", fp)
        monkeypatch.setattr(mjp_portal, "fetch_tenders",
                            lambda session=None: mjp_portal.parse_tenders(_HTML))
        mjp_portal.run(now="2026-10-14T00:00:00+05:30")

        def boom(session=None):
            raise RuntimeError("timeout")
        monkeypatch.setattr(mjp_portal, "fetch_tenders", boom)
        res = mjp_portal.run(now="2026-10-14T02:00:00+05:30")
        assert res["ok"] is False and res["total"] == 2   # rows kept, not wiped
        feed = json.loads(fp.read_text())
        assert len(feed["tenders"]) == 2 and feed["last_error"] == "timeout"
