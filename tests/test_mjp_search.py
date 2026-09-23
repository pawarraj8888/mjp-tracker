"""Search-index GR discovery (network-free).

The GR portal's own department search is CAPTCHA + load-balancer protected and is
not bypassed; these tests pin the legitimate substitute: harvest candidate GR PDF
URLs from a pluggable search backend and/or a curated list, filtered to real
portal PDFs and validated by the SSRF allowlist, and report a missing backend
honestly instead of as an empty success.
"""

import pytest

from pipeline.mjp import search_discovery as sd


@pytest.fixture(autouse=True)
def _no_ssrf_dns(monkeypatch):
    # validate_url does a live DNS lookup; stub it so unit tests stay offline.
    monkeypatch.setattr(sd.gr_source, "validate_url", lambda u: None)


class TestIsGrPdf:
    def test_accepts_portal_gr_pdf(self):
        assert sd.is_gr_pdf(
            "https://gr.maharashtra.gov.in/Site/Upload/Government%20Resolutions/"
            "English/202606251057023925.pdf")

    def test_accepts_trailing_dot_naming(self):
        # The portal's own uploads sometimes carry "...pdf".
        assert sd.is_gr_pdf(
            "https://gr.maharashtra.gov.in/Site/Upload/Government%20Resolutions/"
            "Marathi/202401151409206609...pdf")

    def test_rejects_other_host(self):
        assert not sd.is_gr_pdf("https://example.com/x/y.pdf")

    def test_rejects_http(self):
        assert not sd.is_gr_pdf(
            "http://gr.maharashtra.gov.in/Site/Upload/Government%20Resolutions/"
            "English/1.pdf")

    def test_rejects_non_pdf_and_non_gr_path(self):
        assert not sd.is_gr_pdf("https://gr.maharashtra.gov.in/1145/Government-Resolutions")
        assert not sd.is_gr_pdf("https://gr.maharashtra.gov.in/Site/Upload/Other/x.pdf")


class TestHarvest:
    def _backend(self, mapping):
        return lambda q, session=None: mapping.get(q, [])

    def test_filters_dedups_and_counts_rejects(self):
        good = ("https://gr.maharashtra.gov.in/Site/Upload/Government%20"
                "Resolutions/English/111.pdf")
        dupe = good
        junk = "https://evil.example/x.pdf"
        backend = self._backend({sd.QUERIES[0]: [good, dupe, junk]})
        res = sd.harvest(queries=(sd.QUERIES[0],), backend=backend)
        assert res["urls"] == [good]        # deduped, portal-only
        assert res["rejected"] == 1         # the off-host junk
        assert res["backend"] == "google_cse"

    def test_missing_backend_and_no_urls_raises(self, monkeypatch):
        monkeypatch.setattr(sd, "default_backend", lambda: None)
        with pytest.raises(sd.SearchNotConfigured):
            sd.harvest()

    def test_curated_urls_work_without_backend(self, monkeypatch):
        monkeypatch.setattr(sd, "default_backend", lambda: None)
        u = ("https://gr.maharashtra.gov.in/Site/Upload/Government%20"
             "Resolutions/Marathi/222.pdf")
        res = sd.harvest(extra_urls=[u, "not-a-gr-url"])
        assert res["urls"] == [u]
        assert res["backend"] == "none"

    def test_backend_failure_is_recorded_not_raised(self):
        def boom(q, session=None):
            raise RuntimeError("quota exceeded")
        res = sd.harvest(queries=(sd.QUERIES[0],), backend=boom,
                         extra_urls=[])
        assert res["urls"] == []
        assert res["failures"] and "quota" in res["failures"][0]["error"]
