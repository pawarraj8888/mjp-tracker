"""Ingest Maharashtra Jeevan Pradhikaran's OWN published tenders.

Source: https://mjp.maharashtra.gov.in/tenders/ -- MJP's official website (not
the GePNIC procurement portal). It is a plain server-rendered WordPress table
(``table.rwd-table``: Name | Description | Region | Start Date | End Date |
Download-PDF), captcha-free and directly fetchable. There is no pagination; the
whole list is one page.

These are MJP's own notices/RFPs/quotations, kept distinct from the GePNIC live
tenders and from the GR-based upcoming projects. The feed is merged into a
durable JSON file so a transient fetch failure never drops already-known rows
(the coverage is reported honestly instead).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent.parent
FEED_FILE = ROOT / "mjp_store" / "portal_tenders.json"

PORTAL_URL = "https://mjp.maharashtra.gov.in/tenders/"
PORTAL_HOST = "mjp.maharashtra.gov.in"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _valid_pdf_url(url: str) -> bool:
    """A PDF link is kept only when it is an https URL on the MJP host that
    resolves to a public address (the link is semi-trusted page content)."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not (host == PORTAL_HOST
                                   or host.endswith("." + PORTAL_HOST)):
        return False
    try:
        for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
    except socket.gaierror:
        return False
    return True


def _iso(dmy: str) -> str:
    m = _DMY.match((dmy or "").strip())
    if not m:
        return ""
    d, mo, y = (int(x) for x in m.groups())
    try:
        return _dt.date(y, mo, d).isoformat()
    except ValueError:
        return ""


def _key(rec: dict) -> str:
    """Stable id: the PDF URL when present, else name+start (uuid-free, so the
    same tender maps to the same row across runs)."""
    basis = rec.get("pdf_url") or (rec.get("name", "") + "|" + rec.get("start", ""))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def parse_tenders(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="rwd-table")
    if table is None:
        return []
    out: list[dict] = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        a = tr.find("a", href=True)
        pdf = urljoin(PORTAL_URL, a["href"]) if a else ""
        if pdf and not _valid_pdf_url(pdf):
            pdf = ""
        rec = {
            "name": tds[0].get_text(" ", strip=True),
            "description": tds[1].get_text(" ", strip=True),
            "region": tds[2].get_text(" ", strip=True),
            "start": _iso(tds[3].get_text(strip=True)),
            "end": _iso(tds[4].get_text(strip=True)),
            "pdf_url": pdf,
        }
        if not rec["name"]:
            continue
        rec["id"] = _key(rec)
        out.append(rec)
    return out


def fetch_tenders(session: requests.Session | None = None) -> list[dict]:
    sess = session or requests.Session()
    r = sess.get(PORTAL_URL, headers={"User-Agent": _UA}, timeout=40)
    r.raise_for_status()
    return parse_tenders(r.text)


def _load_feed() -> dict:
    try:
        return json.loads(FEED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"generated": "", "source": PORTAL_URL, "tenders": []}


def run(session: requests.Session | None = None, now: str = "") -> dict:
    """Fetch the MJP tenders page and merge it into the durable feed. A fetch
    failure keeps the existing rows and is reported, never silently emptied."""
    now = now or _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    feed = _load_feed()
    by_id = {t["id"]: t for t in feed.get("tenders", [])}
    try:
        rows = fetch_tenders(session)
    except Exception as exc:                                   # noqa: BLE001
        feed["last_error"] = str(exc)
        feed["last_checked"] = now
        FEED_FILE.write_text(json.dumps(feed, ensure_ascii=False, indent=1,
                                        sort_keys=True) + "\n", encoding="utf-8")
        return {"fetched": 0, "total": len(by_id), "ok": False,
                "error": str(exc)}

    added = 0
    for rec in rows:
        rec = dict(rec, first_seen=by_id.get(rec["id"], {}).get("first_seen", now),
                   last_seen=now)
        if rec["id"] not in by_id:
            added += 1
        by_id[rec["id"]] = rec

    tenders = sorted(by_id.values(), key=lambda t: t.get("start", ""),
                     reverse=True)
    FEED_FILE.write_text(json.dumps(
        {"generated": now, "source": PORTAL_URL, "last_checked": now,
         "tenders": tenders}, ensure_ascii=False, indent=1,
        sort_keys=True) + "\n", encoding="utf-8")
    return {"fetched": len(rows), "added": added, "total": len(tenders),
            "ok": True}
