"""Discover MJP-relevant Government Resolutions from the state GR portal.

Only the parts of the portal that are actually reachable without defeating an
access control are used:

* The default listing grid (``SitePH_dgvDocuments``) is served by a plain GET
  and carries, per row, the department, title, unique document code, GR date and
  a direct PDF link. This is polled each run to catch newly published GRs.
* Any GR PDF is downloadable directly by its URL (no session, no signature).

The department/date **search** and the result **pagination** are ASP.NET
postbacks that fail here for two honest reasons -- the portal sits behind a load
balancer without sticky sessions (viewstate MAC validation fails across nodes),
and the search additionally requires a CAPTCHA (``txtimgcode``). Those are NOT
bypassed. Deep history therefore comes from an explicit seed list of document
codes/URLs (resumable backfill); the coverage limit is reported, never hidden.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://gr.maharashtra.gov.in/1145/Government-Resolutions"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
# Government GRs are a few hundred KB; cap the download so a hostile or
# misbehaving response cannot exhaust memory/disk (resource-exhaustion guard).
MAX_PDF_BYTES = 40 * 1024 * 1024

# SSRF guard: a document URL is untrusted (it comes from portal HTML or the
# seeds file), so downloads are pinned to an allowlist of official government
# hosts, forced to https, blocked from resolving to any non-public address, and
# never allowed to follow a redirect to an off-allowlist host.
_DEFAULT_HOSTS = "gr.maharashtra.gov.in"
ALLOWED_HOSTS = {h.strip().lower() for h in
                 os.environ.get("MJP_ALLOWED_HOSTS", _DEFAULT_HOSTS).split(",")
                 if h.strip()}


def validate_url(url: str) -> None:
    """Raise ``requests.RequestException`` unless the URL is an https URL to an
    allowlisted host that resolves only to public IP addresses."""
    p = urlparse(url or "")
    if p.scheme != "https":
        raise requests.RequestException("rejected non-https URL: %r" % url)
    host = (p.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise requests.RequestException(
            "rejected URL host %r (not in allowlist %s)"
            % (host, sorted(ALLOWED_HOSTS)))
    try:
        infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise requests.RequestException("DNS resolution failed for %r: %s"
                                        % (host, exc))
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise requests.RequestException(
                "rejected URL: %r resolves to non-public address %s"
                % (host, ip))

# Departments most likely to sanction MJP water works. Matched as substrings so
# portal spelling variants still hit.
RELEVANT_DEPARTMENTS = ("नगर विकास", "पाणीपुरवठा")
# MJP / water-supply signals in a title (clean HTML, so correct Unicode).
RELEVANT_TITLE_TERMS = (
    "जीवन प्राधिकरण", "महाराष्ट्र जीवन", "जीवन प्राशधकरर्", "mjp",
    "पाणीपुरवठा", "पाणी पुरवठा", "नळ पाणी", "जलवाहिनी", "जल जीवन",
    "पाणी पुरवठा योजना", "water supply", "वैशिष्ट्यपूर्ण")


def _get(url: str, session: requests.Session | None = None,
         tries: int = 3, timeout: int = 40) -> requests.Response:
    sess = session or requests.Session()
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, headers={"User-Agent": _UA}, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last  # surfaces as a source failure (never a false "no new projects")


def parse_listing(html: str) -> list[dict]:
    """Parse the default GR grid into structured rows."""
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find("table", id="SitePH_dgvDocuments")
    if grid is None:
        return []
    rows: list[dict] = []
    for tr in grid.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 7:
            continue
        link = tr.find("a", href=True)
        if not link or "/Upload/" not in link["href"]:
            continue
        href = urljoin(LISTING_URL, link["href"])
        rows.append({
            "sn": cells[0].get_text(strip=True),
            "department": cells[1].get_text(" ", strip=True),
            "title": cells[2].get_text(" ", strip=True),
            "doc_code": cells[3].get_text(strip=True),
            "gr_date": cells[4].get_text(strip=True),   # dd-mm-yyyy
            "size_kb": cells[5].get_text(strip=True),
            "url": href,
            "language": "en" if "/English/" in href else "mr",
        })
    return rows


def fetch_listing(session: requests.Session | None = None) -> list[dict]:
    """The most recent GRs across all departments (captcha-free)."""
    r = _get(LISTING_URL, session)
    return parse_listing(r.text)


def relevance(row: dict) -> tuple[bool, str]:
    """Is this GR worth downloading? Department OR title signal. The PDF is the
    real arbiter of MJP's role; this is only a cheap pre-filter."""
    dept = row.get("department", "")
    if any(d in dept for d in RELEVANT_DEPARTMENTS):
        return True, "department:%s" % dept.strip()
    title = (row.get("title", "") or "").lower()
    for term in RELEVANT_TITLE_TERMS:
        if term.lower() in title:
            return True, "title:%s" % term
    return False, ""


def normalize_gr_date(dmy: str) -> str:
    """``25-06-2026`` -> ISO ``2026-06-25``; passes through anything else."""
    parts = (dmy or "").strip().split("-")
    if len(parts) == 3 and len(parts[2]) == 4:
        d, m, y = parts
        return "%s-%s-%s" % (y, m.zfill(2), d.zfill(2))
    return dmy or ""


def download_pdf(url: str, session: requests.Session | None = None) -> dict:
    """Download an official PDF; return its bytes, sha256 and content type.
    Raises on network failure so the caller records the failure honestly."""
    validate_url(url)                       # SSRF guard (raises if disallowed)
    sess = session or requests.Session()
    last = None
    for i in range(3):
        try:
            r = sess.get(url, headers={"User-Agent": _UA}, timeout=90,
                         stream=True, allow_redirects=False)
            if r.status_code >= 300:
                # Do not follow a redirect to a possibly off-allowlist host.
                raise requests.RequestException(
                    "unexpected %d (redirect not followed)" % r.status_code)
            r.raise_for_status()
            chunks, total = [], 0
            for chunk in r.iter_content(chunk_size=65536):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise requests.RequestException(
                        "response exceeds %d bytes" % MAX_PDF_BYTES)
            content = b"".join(chunks)
            ctype = r.headers.get("Content-Type", "")
            is_pdf = content[:5] == b"%PDF-" or "pdf" in ctype.lower()
            return {
                "bytes": content,
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_type": ctype,
                "size": len(content),
                "is_pdf": is_pdf,
            }
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 * (i + 1))
    raise last
