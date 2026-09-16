"""Build the ``mjp_projects.json`` dashboard feed from the store.

Amounts are emitted as exact decimal strings plus a display string; unknown is
``null`` (never ``0``). Labels are precise and evidence-bound: "Funding
sanctioned", "Administrative approval recorded", "Possible tender match",
"Tender linked". When no tender matches, the feed says so *as of the last
successful check*, never that a tender definitely does not exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import money
import timez

from ..store import Store
from . import store as mstore

ROOT = Path(__file__).resolve().parent.parent.parent
FEED_FILE = ROOT / "mjp_projects.json"
ALERTS_LOG = ROOT / "mjp_store" / "alerts.json"

_EVENT_LABEL = {
    "project_discovered": "Project discovered",
    "funding_sanctioned": "Funding sanctioned",
    "administrative_approval_recorded": "Administrative approval recorded",
    "technical_sanction_recorded": "Technical sanction recorded",
    "funds_released": "Funds released",
    "revised_sanction": "Revised sanction recorded",
    "tender_suggested": "Possible tender match",
    "tender_linked": "Tender linked",
}
_DOCTYPE_STATUS = [
    ("fund_release", "Funds released"),
    ("funding_sanction", "Funding sanctioned"),
    ("administrative_approval", "Administrative approval recorded"),
    ("technical_sanction", "Technical sanction recorded"),
    ("revised_sanction", "Revised sanction recorded"),
]


def _amt(value) -> dict:
    return {"inr": value, "display": money.format_inr(value) or None,
            "plain": money.format_plain(value) if money.is_known(value) else None}


def _loads(v, default):
    if not v:
        return default
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return default


def _status_label(docs: list[dict], links: list[dict]) -> str:
    if any(l["link_status"] == "linked" for l in links):
        return "Tender linked"
    if any(l["link_status"] == "suggested" for l in links):
        return "Possible tender match"
    doc_types = {d["doc_type"] for d in docs}
    for key, label in _DOCTYPE_STATUS:
        if key in doc_types:
            return label
    return "Approval recorded"


def _tender_match_status(links: list[dict], last_checked: str) -> str:
    linked = [l for l in links if l["link_status"] == "linked"]
    suggested = [l for l in links if l["link_status"] == "suggested"]
    if linked:
        return "Tender linked: " + ", ".join(l["source_tender_id"] for l in linked)
    if suggested:
        return "Possible tender match: " + ", ".join(
            l["source_tender_id"] for l in suggested)
    when = timez.fmt_ist(last_checked) or last_checked or "last check"
    return "No matching tender found as of %s" % when


# Only genuine approval/funding events count as "documented approvals" -- a
# tender suggestion or the discovery marker is not an approval.
_APPROVAL_EVENTS = ("funding_sanctioned", "administrative_approval_recorded",
                    "technical_sanction_recorded", "funds_released",
                    "revised_sanction")


def _latest_approval(events: list[dict]) -> dict:
    dated = [e for e in events
             if e.get("event_date") and e["event_type"] in _APPROVAL_EVENTS]
    dated.sort(key=lambda e: e["event_date"])
    if not dated:
        return {"label": "", "date": ""}
    e = dated[-1]
    return {"label": _EVENT_LABEL.get(e["event_type"], e["event_type"]),
            "date": e["event_date"]}


def build_project(store: Store, p: dict) -> dict:
    docs = mstore.documents_for(store, p["id"])
    events = mstore.events_for(store, p["id"])
    links = mstore.links_for(store, p["id"])
    raw = _loads(p.get("raw"), {})

    doc_out = [{
        "doc_code": d["doc_code"], "gr_number": d["gr_number"],
        "department": d["department"], "doc_type": d["doc_type"],
        "title_original": d["title_original"], "language": d["language"],
        "issue_date": d["issue_date"], "publication_date": d["publication_date"],
        "first_detected_at": d["first_detected_at"],
        "url": d["url"], "sha256": d["sha256"],
        "ocr_used": bool(d["ocr_used"]), "extract_ok": bool(d["extract_ok"]),
        "components": _loads(d.get("raw"), {}).get("components_inr", []),
    } for d in docs]

    event_out = [{
        "event_type": e["event_type"],
        "label": _EVENT_LABEL.get(e["event_type"], e["event_type"]),
        "date": e["event_date"], "detail": _loads(e.get("detail"), {}),
        "source_doc_code": e["source_doc_code"],
    } for e in events]

    link_out = [{
        "tender_id": l["source_tender_id"], "status": l["link_status"],
        "confidence": l["confidence"], "reasons": _loads(l.get("reasons"), []),
        "amount_note": l["amount_note"],
        "confirmed_by": l["confirmed_by"], "confirmed_at": l["confirmed_at"],
    } for l in links]

    return {
        "id": p["id"],
        "title_en": p["title_en"], "title_original": p["title_original"],
        "municipality": p["municipality"], "district": p["district"],
        "scheme": p["scheme"], "mjp_office": p["mjp_office"],
        "work_theme": p["work_theme"],
        "mjp_role": p["mjp_role"], "mjp_role_evidence": p["mjp_role_evidence"],
        "mjp_role_page": p["mjp_role_page"],
        "mjp_role_confidence": p["mjp_role_confidence"],
        "approved_cost": _amt(p["approved_cost_inr"]),
        "revised_cost": _amt(p["revised_cost_inr"]),
        "funds_released": _amt(p["funds_released_inr"]),
        "tender_estimate": _amt(p["tender_estimate_inr"]),
        "status_label": _status_label(docs, links),
        "tender_match_status": _tender_match_status(links, p["last_checked_at"]),
        "latest_approval": _latest_approval(events),
        "primary_doc_code": p["primary_doc_code"],
        "document_date": docs[0]["issue_date"] if docs else "",
        "first_detected_at": p["first_detected_at"],
        "last_checked_at": p["last_checked_at"],
        "components": raw.get("works_en", []),
        "missing": _loads(p.get("missing"), []),
        "uncertainties": _loads(p.get("uncertainties"), []),
        "documents": doc_out, "events": event_out, "links": link_out,
    }


def build_feed(store: Store, discovery: dict | None = None) -> dict:
    projects = [build_project(store, p) for p in mstore.all_projects(store)]
    # Newest documented approval first.
    projects.sort(key=lambda p: (p["latest_approval"]["date"] or "",
                                 p["last_checked_at"] or ""), reverse=True)
    review = [{
        "item_type": r["item_type"], "ref": r["ref"], "reason": r["reason"],
        "detail": _loads(r.get("detail"), {}), "score": r["score"],
    } for r in mstore.review_items(store)]

    alerts_log = {}
    if ALERTS_LOG.exists():
        try:
            alerts_log = json.loads(ALERTS_LOG.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            alerts_log = {}

    counts = mstore.mjp_counts(store)
    linked = sum(1 for p in projects for l in p["links"] if l["status"] == "linked")
    suggested = sum(1 for p in projects for l in p["links"]
                    if l["status"] == "suggested")

    return {
        "generated": timez.iso_ist(),
        "discovery": discovery or {},
        "counts": {**counts, "linked_tenders": linked,
                   "suggested_tenders": suggested},
        "review_queue": review,
        "alerts": {"delivered": len(alerts_log.get("delivered", [])),
                   "pending": len(alerts_log.get("pending", [])),
                   "recipients": alerts_log.get("recipients", [])},
        "projects": projects,
    }


def write_feed(store: Store, discovery: dict | None = None,
               path: Path | None = None) -> dict:
    feed = build_feed(store, discovery)
    out = path or FEED_FILE
    out.write_text(json.dumps(feed, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(out), "projects": len(feed["projects"]),
            "review": len(feed["review_queue"])}
