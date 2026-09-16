"""GR discovery: listing parse, relevance pre-filter, date normalization, and
the discover() flow (offline) including honest failure handling."""

import pytest

from pipeline.mjp import gr_source, ingest

GRID_HTML = """
<table id="SitePH_dgvDocuments">
<tr><th>क्र</th><th>विभाग</th><th>शीर्षक</th><th>कोड</th><th>दिनांक</th><th>KB</th><th>DL</th></tr>
<tr><td>1</td><td>नगर विकास विभाग</td><td>चिखलदरा पाणीपुरवठा</td>
    <td>202606251057023925</td><td>25-06-2026</td><td>335</td>
    <td><a href="../Site/Upload/Government%20Resolutions/Marathi/202606251057023925.pdf">DL</a></td></tr>
<tr><td>2</td><td>वित्त विभाग</td><td>समिती गठन</td>
    <td>202609161613117905</td><td>16-09-2026</td><td>3637</td>
    <td><a href="../Site/Upload/Government%20Resolutions/Marathi/202609161613117905.pdf">DL</a></td></tr>
</table>
"""


def test_parse_listing_extracts_rows():
    rows = gr_source.parse_listing(GRID_HTML)
    assert len(rows) == 2
    r = rows[0]
    assert r["department"] == "नगर विकास विभाग"
    assert r["doc_code"] == "202606251057023925"
    assert r["gr_date"] == "25-06-2026"
    assert r["url"].endswith("202606251057023925.pdf")
    assert r["url"].startswith("https://gr.maharashtra.gov.in/")


def test_relevance_department_and_title():
    rows = gr_source.parse_listing(GRID_HTML)
    ok0, why0 = gr_source.relevance(rows[0])
    ok1, why1 = gr_source.relevance(rows[1])
    assert ok0 and "department" in why0          # Urban Development
    assert not ok1                                 # Finance dept, no water title


def test_validate_url_ssrf_guard():
    import requests
    # Allowlisted https government URL passes.
    gr_source.validate_url(
        "https://gr.maharashtra.gov.in/Site/Upload/x/202606251057023925.pdf")
    for bad in ("http://gr.maharashtra.gov.in/x.pdf",       # not https
                "https://169.254.169.254/x.pdf",             # off-allowlist IP
                "https://evil.example.com/x.pdf",            # off-allowlist host
                "https://localhost/x.pdf"):                  # loopback host
        with pytest.raises(requests.RequestException):
            gr_source.validate_url(bad)


def test_normalize_gr_date():
    assert gr_source.normalize_gr_date("25-06-2026") == "2026-06-25"
    assert gr_source.normalize_gr_date("2026-06-25") == "2026-06-25"
    assert gr_source.normalize_gr_date("") == ""


def test_discover_downloads_relevant_only(monkeypatch, tmp_path):
    docs = tmp_path / "documents"
    monkeypatch.setattr(ingest, "DOCS_DIR", docs)
    monkeypatch.setattr(ingest.gr_source, "fetch_listing",
                        lambda session=None: gr_source.parse_listing(GRID_HTML))
    downloaded = []

    def fake_download(url, session=None):
        downloaded.append(url)
        return {"bytes": b"%PDF-1.4", "sha256": "a" * 64,
                "content_type": "application/pdf", "size": 8, "is_pdf": True}

    monkeypatch.setattr(ingest.gr_source, "download_pdf", fake_download)
    index = {}
    disc = ingest.discover(index, now="2026-09-17T00:00:00+05:30")
    assert disc["listing_ok"] is True
    assert len(disc["added"]) == 1                 # only the Urban Development GR
    assert "202606251057023925" in index
    assert "202609161613117905" not in index       # Finance filtered out


def test_discover_idempotent(monkeypatch, tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    monkeypatch.setattr(ingest, "DOCS_DIR", docs)
    monkeypatch.setattr(ingest.gr_source, "fetch_listing",
                        lambda session=None: gr_source.parse_listing(GRID_HTML))
    monkeypatch.setattr(ingest.gr_source, "download_pdf",
                        lambda url, session=None: {
                            "bytes": b"%PDF-1.4", "sha256": "a" * 64,
                            "content_type": "application/pdf", "size": 8,
                            "is_pdf": True})
    # Pre-existing index + on-disk file -> refreshed, not re-added.
    (docs / "202606251057023925.pdf").write_bytes(b"%PDF-1.4")
    index = {"202606251057023925": {"doc_code": "202606251057023925",
                                    "first_detected_at": "2026-06-01",
                                    "last_checked_at": "2026-06-01"}}
    disc = ingest.discover(index, now="2026-09-17T00:00:00+05:30")
    assert disc["added"] == []
    assert "202606251057023925" in disc["refreshed"]
    assert index["202606251057023925"]["first_detected_at"] == "2026-06-01"


def test_discover_records_listing_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "DOCS_DIR", tmp_path / "documents")

    def boom(session=None):
        raise RuntimeError("portal down")

    monkeypatch.setattr(ingest.gr_source, "fetch_listing", boom)
    disc = ingest.discover({}, now="2026-09-17T00:00:00+05:30")
    # A failed check is reported honestly, never a silent "no new projects".
    assert disc["listing_ok"] is False
    assert any(f["stage"] == "listing" for f in disc["failures"])


def test_malicious_doc_code_is_rejected(monkeypatch, tmp_path):
    # An untrusted listing row with a traversal code must never write a path
    # outside the store; it is skipped and recorded as a validation failure.
    docs = tmp_path / "documents"
    monkeypatch.setattr(ingest, "DOCS_DIR", docs)
    evil = {"doc_code": "../../../../etc/passwd", "url": "https://x/e.pdf",
            "department": "नगर विकास विभाग", "title": "t", "gr_date": "01-01-2026"}
    monkeypatch.setattr(ingest.gr_source, "fetch_listing",
                        lambda session=None: [dict(evil, relevance="")])
    called = []
    monkeypatch.setattr(ingest.gr_source, "download_pdf",
                        lambda *a, **k: called.append(a) or {
                            "bytes": b"%PDF", "sha256": "x", "size": 4,
                            "is_pdf": True, "content_type": "application/pdf"})
    index = {}
    disc = ingest.discover(index, now="2026-09-17T00:00:00+05:30")
    assert index == {}                       # nothing stored
    assert called == []                      # never even downloaded
    assert any(f["stage"] == "validate" for f in disc["failures"])
    assert not (tmp_path.parent / "etc").exists()


def test_safe_code_helper():
    assert ingest.safe_code("202606251057023925") == "202606251057023925"
    assert ingest.safe_code("../../x") == ""
    assert ingest.safe_code("/abs/path") == ""
    assert ingest.safe_code("12ab34") == ""


def test_discover_rejects_non_pdf_response(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "DOCS_DIR", tmp_path / "documents")
    monkeypatch.setattr(ingest.gr_source, "fetch_listing",
                        lambda session=None: gr_source.parse_listing(GRID_HTML))
    monkeypatch.setattr(ingest.gr_source, "download_pdf",
                        lambda url, session=None: {
                            "bytes": b"<html>captcha</html>", "sha256": "b" * 64,
                            "content_type": "text/html", "size": 20,
                            "is_pdf": False})
    index = {}
    disc = ingest.discover(index, now="2026-09-17T00:00:00+05:30")
    assert index == {}                              # nothing stored
    assert any("not a PDF" in f["error"] for f in disc["failures"])
