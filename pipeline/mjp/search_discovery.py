"""Search-index discovery of MJP-relevant Government Resolutions.

Why this exists: the GR portal's own department/date search is protected by a
server-session CAPTCHA behind a load balancer that drops the session between
requests (verified: the search postback throws a NullReference because the
captcha value it validates against is gone). That access control is NOT
bypassed. A legitimate, automatable substitute is to ask a web-search index for
GR PDFs published on the official portal, then download each by its direct
(captcha-free) URL and let the MJP-role gate in ``ingest.py`` decide whether a
document is a real MJP project, a review candidate, or noise.

Backend: the default is the Google Programmable Search JSON API (free tier),
enabled with two environment variables (``GOOGLE_CSE_KEY``, ``GOOGLE_CSE_CX``).
The backend is a plain ``callable(query, session=...) -> list[url]`` so tests
inject a fake and an operator can wire a different index. When no backend is
configured and no curated candidate URLs are supplied, :func:`harvest` raises
:class:`SearchNotConfigured` so the caller records the coverage gap honestly
instead of reporting a clean "no new GRs".
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import requests

from . import gr_source

# Queries aimed at GRs where MJP implements/approves a *named* water scheme (the
# Chikhaldara shape), not incidental policy or funding mentions. Kept short so a
# search index treats them as topical rather than an exact-phrase match.
QUERIES: tuple[str, ...] = (
    '"महाराष्ट्र जीवन प्राधिकरण" पाणी पुरवठा योजना प्रशासकीय मान्यता',
    '"महाराष्ट्र जीवन प्राधिकरण" कार्यान्वयीन यंत्रणा पाणी पुरवठा',
    "नगरोत्थान पाणी पुरवठा योजना महाराष्ट्र जीवन प्राधिकरण मंजूर",
    "वैशिष्ट्यपूर्ण योजना पाणी पुरवठा महाराष्ट्र जीवन प्राधिकरण",
    "अमृत अभियान पाणी पुरवठा महाराष्ट्र जीवन प्राधिकरण प्रकल्प",
    "Maharashtra Jeevan Pradhikaran water supply scheme administrative approval",
)

GR_HOST = "gr.maharashtra.gov.in"
# Portal GR PDFs live under this path; some real URLs carry a trailing "..pdf"
# or "...pdf" (the portal's own upload naming), so match a permissive suffix.
_GR_PDF_PATH = re.compile(
    r"/Site/Upload/Government%20Resolutions/.+\.+pdf$", re.IGNORECASE)


class SearchNotConfigured(RuntimeError):
    """No search backend is configured and no candidate URLs were supplied."""


def is_gr_pdf(url: str) -> bool:
    """True only for an https GR-PDF URL on the official portal host."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    if p.scheme != "https" or (p.hostname or "").lower() != GR_HOST:
        return False
    return bool(_GR_PDF_PATH.search(p.path))


def google_cse(query: str, *, key: str, cx: str,
               session: requests.Session | None = None,
               num: int = 10) -> list[str]:
    """Query the Google Programmable Search JSON API for GR PDFs on the portal.
    Returns the raw result links (filtered/validated by the caller)."""
    sess = session or requests.Session()
    r = sess.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": key, "cx": cx, "num": num,
                "q": "site:%s %s" % (GR_HOST, query)},
        timeout=30)
    r.raise_for_status()
    items = (r.json() or {}).get("items") or []
    return [it.get("link", "") for it in items if it.get("link")]


def default_backend():
    """The env-configured backend callable, or ``None`` when unconfigured."""
    key = os.environ.get("GOOGLE_CSE_KEY", "").strip()
    cx = os.environ.get("GOOGLE_CSE_CX", "").strip()
    if key and cx:
        return lambda q, session=None: google_cse(q, key=key, cx=cx,
                                                   session=session)
    return None


def harvest(queries: tuple[str, ...] = QUERIES, *, backend=None,
            session=None, extra_urls=None) -> dict:
    """Discover candidate GR PDF URLs via the search backend and/or a curated
    URL list. Every URL is filtered to a portal GR PDF and passed through the
    SSRF allowlist (:func:`gr_source.validate_url`) before it is returned.

    Returns ``{"urls", "backend", "queries", "failures", "rejected"}``. Raises
    :class:`SearchNotConfigured` only when a live search was the sole requested
    source (no backend AND no ``extra_urls``)."""
    be = backend if backend is not None else default_backend()
    if be is None and not extra_urls:
        raise SearchNotConfigured(
            "no search backend (set GOOGLE_CSE_KEY and GOOGLE_CSE_CX) and no "
            "candidate URLs supplied")

    urls: list[str] = []
    seen: set[str] = set()
    failures: list[dict] = []
    rejected = 0

    def _add(u: str) -> None:
        nonlocal rejected
        u = (u or "").strip()
        if not u or u in seen:
            return
        if not is_gr_pdf(u):
            rejected += 1
            return
        try:
            gr_source.validate_url(u)          # SSRF allowlist / public-IP guard
        except requests.RequestException:
            rejected += 1
            return
        seen.add(u)
        urls.append(u)

    n_queries = 0
    if be is not None:
        for q in queries:
            n_queries += 1
            try:
                for u in be(q, session=session):
                    _add(u)
            except Exception as exc:                       # noqa: BLE001
                failures.append({"query": q, "error": str(exc)})

    for u in (extra_urls or []):
        _add(u)

    return {
        "urls": urls,
        "backend": ("google_cse" if be is not None else "none"),
        "queries": n_queries,
        "failures": failures,
        "rejected": rejected,
    }
