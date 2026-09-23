"""Orchestrate MJP ingestion: discover GRs, store originals durably, extract
evidence, build projects, and link tenders.

Durability follows the app's existing pattern (the SQLite store is rebuilt each
run; committed files are the truth):

* ``mjp_store/documents/<code>.pdf`` -- original PDFs, checksummed (git = durable
  file storage, not a temp dir).
* ``mjp_store/index.json``            -- the discovery ledger (which GRs we have
  seen, when first detected, their checksum). Discovery is idempotent and
  resumable from it.
* ``mjp_store/seeds.json``            -- explicit GR codes/URLs for the resumable
  backfill and the verification case (listing metadata only; findings are still
  extracted from the PDF, never hard-coded).
* ``mjp_projects.json`` (repo root)   -- derived dashboard feed (see export.py).

Every run re-extracts from the stored originals, so the derived state is always
reproducible from the durable inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import money
import timez

from ..store import Store
from . import extract, gr_source, match, search_discovery, store as mstore

ROOT = Path(__file__).resolve().parent.parent.parent
MJP_DIR = ROOT / "mjp_store"
DOCS_DIR = MJP_DIR / "documents"
INDEX_FILE = MJP_DIR / "index.json"
SEEDS_FILE = MJP_DIR / "seeds.json"
# Curated / accumulated GR PDF URLs discovered by search. Ingested through the
# same MJP-role gate as everything else; a plain JSON list of URL strings.
CANDIDATES_FILE = MJP_DIR / "gr_candidates.json"
LINK_DECISIONS_FILE = MJP_DIR / "link_decisions.json"

PORTAL = "gr.maharashtra.gov.in"

# A document code is the portal's सांकेतांक: digits only. It is used to build
# local file paths and attachment names, so it is validated to this safe shape
# before ANY path is derived from it -- untrusted portal HTML can never inject a
# traversal (``../``) or absolute path.
CODE_RE = re.compile(r"^\d{6,25}$")


def safe_code(code: str) -> str:
    return code if code and CODE_RE.match(code) else ""

# A representative subset of Maharashtra districts for locating a tender/GR from
# free text. Not exhaustive; an unrecognised place simply yields "" (unknown),
# never a wrong guess.
DISTRICTS = [
    "Ahmednagar", "Ahilyanagar", "Akola", "Amravati", "Aurangabad",
    "Chhatrapati Sambhajinagar", "Beed", "Bhandara", "Buldhana", "Chandrapur",
    "Dhule", "Gadchiroli", "Gondia", "Hingoli", "Jalgaon", "Jalna", "Kolhapur",
    "Latur", "Mumbai", "Nagpur", "Nanded", "Nandurbar", "Nashik", "Osmanabad",
    "Dharashiv", "Palghar", "Parbhani", "Pune", "Raigad", "Ratnagiri", "Sangli",
    "Satara", "Sindhudurg", "Solapur", "Thane", "Wardha", "Washim", "Yavatmal",
]


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return default
    return default


def _rel_path(local: Path) -> str:
    """Store a repo-relative path when the file lives under ROOT (the normal
    case), else the absolute path. ``ROOT / abs`` resolves back to abs, so both
    round-trip correctly."""
    try:
        return str(local.relative_to(ROOT))
    except ValueError:
        return str(local)


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1,
                              sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def detect_district(text: str) -> str:
    low = (text or "").lower()
    for d in DISTRICTS:
        if d.lower() in low:
            return d
    return ""


def detect_scheme_hint(text: str) -> str:
    low = (text or "").lower()
    for name, key in (("MSJNM", "msjnm"), ("AMRUT", "amrut"),
                      ("Jal Jeevan Mission", "jal jeevan"),
                      ("वैशिष्ट्यपूर्ण", "वैशिष्ट्य")):
        if key in low or key in (text or ""):
            return name
    return ""


# -- discovery ---------------------------------------------------------------

def discover(index: dict, session=None, seeds: list | None = None,
             fetch_live: bool = True, now: str = "",
             candidates: list | None = None) -> dict:
    """Update the durable index with newly published relevant GRs (from the
    captcha-free listing), any seed codes, and any search-discovered candidate
    URLs. Downloads each new original once. Returns a discovery summary
    (including honest coverage notes)."""
    now = now or timez.iso_ist()
    added, refreshed, failures = [], [], []
    listing_ok = True

    entries: list[dict] = []
    if fetch_live:
        try:
            for row in gr_source.fetch_listing(session):
                ok, reason = gr_source.relevance(row)
                if ok:
                    entries.append({**row, "relevance": reason})
        except Exception as exc:
            listing_ok = False
            failures.append({"stage": "listing", "error": str(exc)})

    # Search-discovered GR PDF URLs (see search_discovery). These are untrusted
    # candidates: the code is derived from the URL and each still passes through
    # the same download + MJP-role gate. Unlike a seed, a candidate already known
    # to be non-MJP is not re-downloaded.
    for url in (candidates or []):
        entries.append({
            "doc_code": "", "url": url, "department": "", "title": "",
            "gr_date": "", "district": "", "municipality": "", "scheme": "",
            "language": "en" if "/English/" in url else "mr",
            "relevance": "search",
        })

    for s in (seeds or []):
        entries.append({
            "doc_code": s.get("doc_code", ""), "url": s.get("url", ""),
            "department": s.get("department", ""), "title": s.get("title", ""),
            "gr_date": s.get("gr_date", ""), "district": s.get("district", ""),
            "municipality": s.get("municipality", ""),
            "scheme": s.get("scheme", ""),
            "language": "en" if "/English/" in s.get("url", "") else "mr",
            "relevance": "seed",
        })

    for e in entries:
        code = safe_code(e.get("doc_code") or
                         extract.parse_doc_code([], e.get("url", "")))
        if not code:
            # Missing or malformed code (non-digit / traversal attempt): skip
            # rather than derive an unsafe path from untrusted text.
            if e.get("doc_code"):
                failures.append({"stage": "validate", "code": e.get("doc_code"),
                                 "error": "document code is not digits-only"})
            continue
        prior = index.get(code)
        local = DOCS_DIR / ("%s.pdf" % code)
        # A document already known to be non-MJP (its original was pruned) is not
        # re-downloaded -- unless an operator explicitly re-seeds it.
        non_mjp_known = (prior is not None and prior.get("mjp") is False
                         and e.get("relevance") != "seed")
        if prior and (local.exists() or non_mjp_known):
            prior["last_checked_at"] = now
            # Carry any seed-provided metadata forward without overwriting.
            for k in ("district", "municipality", "scheme"):
                if e.get(k) and not prior.get(k):
                    prior[k] = e[k]
            refreshed.append(code)
            continue
        try:
            dl = gr_source.download_pdf(e["url"], session)
        except Exception as exc:
            failures.append({"stage": "download", "code": code,
                             "error": str(exc)})
            continue
        if not dl["is_pdf"]:
            failures.append({"stage": "download", "code": code,
                             "error": "response was not a PDF (%d bytes)"
                             % dl["size"]})
            continue
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        local.write_bytes(dl["bytes"])
        index[code] = {
            "doc_code": code, "url": e["url"],
            "department": e.get("department", ""), "title": e.get("title", ""),
            "gr_date": gr_source.normalize_gr_date(e.get("gr_date", "")),
            "district": e.get("district", ""),
            "municipality": e.get("municipality", ""),
            "scheme": e.get("scheme", ""), "language": e.get("language", "mr"),
            "relevance": e.get("relevance", ""),
            "sha256": dl["sha256"], "size": dl["size"],
            "local_path": _rel_path(local),
            "first_detected_at": now, "last_checked_at": now,
        }
        added.append(code)

    return {"added": added, "refreshed": refreshed, "failures": failures,
            "listing_ok": listing_ok, "checked_at": now}


# -- project building --------------------------------------------------------

_ROLE_RANK = {"implementing_agency": 4, "tendering_authority": 3,
              "technical_sanction": 2, "mentioned": 1, "none": 0}
# A GR only becomes a tracked MJP project when MJP's role is actually
# established. Documents that merely come from the same department but never
# establish MJP's involvement (metro rail, MMRDA works, etc.) are NOT shown as
# projects; an incidental mention goes to the review queue, and a document with
# no MJP reference at all is dropped entirely (the brief: "A document mentioning
# MJP incidentally is insufficient").
ESTABLISHED_ROLES = {"implementing_agency", "tendering_authority",
                     "technical_sanction"}
_DOCTYPE_EVENT = {
    "funding_sanction": "funding_sanctioned",
    "administrative_approval": "administrative_approval_recorded",
    "technical_sanction": "technical_sanction_recorded",
    "fund_release": "funds_released",
    "revised_sanction": "revised_sanction",
}


def _english_summary(muni: str, district: str, theme: str,
                     works: list) -> str:
    theme_label = {"water_supply": "Water-supply works",
                   "sewerage": "Sewerage / drainage works",
                   "roads": "Road works"}.get(theme, "Infrastructure works")
    where = ", ".join(x for x in (muni, district) if x) or "the listed area"
    head = "%s for %s." % (theme_label, where)
    if works:
        head += " Components include: " + "; ".join(works[:4]) + "."
    return head


def _ingest_one_document(store: Store, entry: dict, now: str) -> dict | None:
    """Extract a single stored GR and upsert its project + document + event."""
    code = entry["doc_code"]
    local = ROOT / entry["local_path"]
    if not local.exists():
        return None
    meta = {
        "url": entry.get("url", ""), "department": entry.get("department", ""),
        "title": entry.get("title", ""), "district": entry.get("district", ""),
        "municipality": entry.get("municipality", ""),
        "scheme": entry.get("scheme", ""),
        "issue_date": gr_source.normalize_gr_date(entry.get("gr_date", "")),
    }
    doc = extract.extract_document(local.read_bytes(), meta)
    municipality = doc.get("municipality") or entry.get("municipality", "")
    district = doc.get("district") or entry.get("district", "") \
        or detect_district(doc.get("mjp_role_evidence", ""))
    pid = mstore.project_uid(district, municipality, doc["work_theme"], code)
    return {"pid": pid, "doc": doc, "district": district,
            "municipality": municipality, "entry": entry}


def build_projects(store: Store, index: dict, now: str) -> dict:
    """Group stored documents into projects and persist them idempotently."""
    groups: dict = {}
    for code, entry in index.items():
        one = _ingest_one_document(store, entry, now)
        if one is None:
            continue
        groups.setdefault(one["pid"], []).append(one)

    n_proj = n_doc = n_evt = n_review = 0
    for pid, items in groups.items():
        items.sort(key=lambda x: x["entry"].get("gr_date", "") or
                   x["doc"].get("issue_date", ""))
        # Aggregate project fields from its documents.
        best_role = max(items, key=lambda x: (
            _ROLE_RANK.get(x["doc"]["mjp_role"], 0),
            x["doc"]["mjp_role_confidence"]))
        muni = next((i["municipality"] for i in items if i["municipality"]), "")
        district = next((i["district"] for i in items if i["district"]), "")
        theme = next((i["doc"]["work_theme"] for i in items
                      if i["doc"]["work_theme"] != "other"),
                     items[0]["doc"]["work_theme"])
        scheme = next((i["doc"]["scheme"] for i in items if i["doc"]["scheme"]), "")
        works = []
        for i in items:
            for w in i["doc"].get("works_en", []):
                if w not in works:
                    works.append(w)

        # Gate: only keep GRs where MJP's role is established. Mark every
        # document's index entry so run() can prune non-MJP originals, and drop
        # (or, for a bare mention, review) the rest instead of showing it. A
        # "mentioned" GR is kept (flag "review", not pruned) so the review queue
        # is reproducible on each fresh-DB run; a GR with no MJP reference at all
        # is marked False and its original pruned.
        role = best_role["doc"]["mjp_role"]
        codes = [i["doc"]["doc_code"] for i in items]
        if role not in ESTABLISHED_ROLES:
            flag = "review" if role == "mentioned" else False
            for c in codes:
                if c in index:
                    index[c]["mjp"] = flag
            if role == "mentioned":
                bd = best_role["doc"]
                mstore.queue_review(
                    store, "project_role_uncertain", codes[0],
                    "MJP is named but its role is not established; not shown "
                    "as a project.",
                    {"municipality": muni, "district": district,
                     "title": (items[0]["doc"].get("title_original") or "")[:160],
                     "doc_code": codes[0],
                     # Retain the supporting passage + page + amount so a human
                     # can adjudicate the candidate straight from the queue.
                     "amount_inr": bd.get("approved_cost_inr"),
                     "evidence": (bd.get("mjp_role_evidence") or "")[:400],
                     "evidence_page": bd.get("mjp_role_page"),
                     "gr_date": items[0]["entry"].get("gr_date", ""),
                     "url": items[0]["entry"].get("url", "")},
                    score=1.0 - best_role["doc"]["mjp_role_confidence"])
                n_review += 1
            continue
        for c in codes:
            if c in index:
                index[c]["mjp"] = True

        approved = revised = released = None
        for i in items:
            dt = i["doc"]["doc_type"]
            amt = i["doc"]["approved_cost_inr"]
            if dt == "revised_sanction" and amt:
                revised = amt
            elif dt == "fund_release" and amt:
                released = money.dec_to_str(money.add([released, amt]))
            elif amt and approved is None:
                approved = amt
            elif amt:
                approved = amt  # most recent non-release sanction wins

        first_detected = min(i["entry"].get("first_detected_at", now)
                             for i in items)
        missing = sorted({m for i in items for m in i["doc"]["missing"]})
        uncert = sorted({u for i in items for u in i["doc"]["uncertainties"]})

        proj = {
            "id": pid,
            "title_original": next((i["doc"]["title_original"] for i in items
                                    if i["doc"]["title_original"]), ""),
            "title_en": _english_summary(muni, district, theme, works),
            "municipality": muni, "district": district, "scheme": scheme,
            "mjp_office": next((i["doc"].get("mjp_office", "") for i in items
                                if i["doc"].get("mjp_office")), ""),
            "mjp_role": best_role["doc"]["mjp_role"],
            "mjp_role_evidence": best_role["doc"]["mjp_role_evidence"],
            "mjp_role_page": best_role["doc"]["mjp_role_page"],
            "mjp_role_confidence": best_role["doc"]["mjp_role_confidence"],
            "approved_cost_inr": approved, "revised_cost_inr": revised,
            "funds_released_inr": released, "tender_estimate_inr": None,
            "work_theme": theme, "status_label": "",
            "primary_doc_code": items[0]["doc"]["doc_code"],
            "missing": missing, "uncertainties": uncert,
            "first_detected_at": first_detected, "last_checked_at": now,
            "raw": {"works_en": works},
        }
        mstore.upsert_project(store, proj)
        n_proj += 1

        # project_discovered event (dated to the first document's issue date).
        if mstore.add_event(store, pid, "project_discovered",
                            {"note": "First official document recorded.",
                             "doc_code": items[0]["doc"]["doc_code"]},
                            event_date=items[0]["doc"].get("issue_date", ""),
                            source_doc_code=items[0]["doc"]["doc_code"],
                            source=PORTAL, created_at=now):
            n_evt += 1

        for i in items:
            doc = i["doc"]
            mstore.upsert_document(store, {
                "project_id": pid, "doc_code": doc["doc_code"],
                "gr_number": doc["gr_number"], "department": doc["department"],
                "doc_type": doc["doc_type"],
                "title_original": doc["title_original"],
                "language": doc["language"], "issue_date": doc["issue_date"],
                "publication_date": extract.date_from_code(doc["doc_code"]),
                "first_detected_at": i["entry"].get("first_detected_at", now),
                "last_checked_at": now, "url": doc["url"],
                "local_path": i["entry"].get("local_path", ""),
                "sha256": i["entry"].get("sha256", ""),
                "page_refs": [doc["mjp_role_page"]] if doc["mjp_role_page"] else [],
                "ocr_used": doc["ocr_used"], "extract_ok": doc["extract_ok"],
                "raw": {"components_inr": doc["components_inr"],
                        "works_en": doc["works_en"]},
            })
            n_doc += 1
            evt = _DOCTYPE_EVENT.get(doc["doc_type"])
            if evt and mstore.add_event(
                    store, pid, evt,
                    {"amount_inr": doc["approved_cost_inr"],
                     "amount_fmt": money.format_inr(doc["approved_cost_inr"])
                     or "Unknown", "doc_code": doc["doc_code"],
                     "evidence": doc["mjp_role_evidence"][:300]},
                    event_date=doc["issue_date"],
                    source_doc_code=doc["doc_code"], source=PORTAL,
                    created_at=now):
                n_evt += 1

        # A kept project whose text layer was empty still needs a human check.
        if any(not i["doc"]["extract_ok"] for i in items):
            mstore.queue_review(
                store, "extraction_low_confidence", pid,
                "A document's text layer was empty (scanned; OCR unavailable).",
                {"municipality": muni}, score=0.9)
            n_review += 1

    return {"projects": n_proj, "documents": n_doc, "events": n_evt,
            "review": n_review}


# -- tender matching ---------------------------------------------------------

def build_candidates(state: dict) -> list[dict]:
    rows = (state or {}).get("live_rows") or []
    details = (state or {}).get("details") or {}
    cands = []
    for row in rows:
        tid = row.get("tender_id")
        if not tid:
            continue
        det = details.get(tid, {}) if isinstance(details, dict) else {}
        org = row.get("org_chain", "")
        address = " ".join(str(det.get(k, "")) for k in
                           ("Address", "Bid Opening Place", "EMD Payable To",
                            "Location"))
        blob = " ".join([row.get("title", ""), org, address])
        cands.append({
            "source_portal": "mahatenders", "source_tender_id": tid,
            "title": row.get("title", ""), "org": org, "address": address,
            "district": detect_district(blob),
            "work_text": row.get("title", ""),
            "scheme_hint": detect_scheme_hint(blob),
            "value_inr": money.dec_to_str(money.parse_amount(
                det.get("Tender Value in ₹") or det.get("Tender Value") or "")),
            "raw_text": blob,
        })
    return cands


def _relevant_candidates(projects: list[dict], cands: list[dict]) -> list[dict]:
    """Cheap pre-filter: keep only tenders that mention MJP, or share a project
    district or municipality. Keeps the fuzzy pass small and focused."""
    districts = {(_norm(p.get("district"))) for p in projects if p.get("district")}
    munis = [_norm(p.get("municipality")) for p in projects if p.get("municipality")]
    out = []
    for c in cands:
        blob = (c["raw_text"] or "").lower()
        if match._MJP_IN_TEXT.search(c["raw_text"] or ""):
            out.append(c); continue
        if _norm(c.get("district")) in districts and districts:
            out.append(c); continue
        if any(m and m in blob for m in munis):
            out.append(c); continue
    return out


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def match_all(store: Store, state: dict, now: str) -> dict:
    projects = mstore.all_projects(store)
    cands = build_candidates(state)
    pool = _relevant_candidates(projects, cands)
    pool_by_id = {c["source_tender_id"]: c for c in pool}
    decisions = _load_json(LINK_DECISIONS_FILE, {})
    n_link = n_suggest = 0
    for p in projects:
        proj = dict(p)
        proj["works_en"] = (json.loads(p["raw"]).get("works_en", [])
                            if p.get("raw") else [])
        matches = match.match_project(proj, pool)
        # If the GR did not name an MJP division but a matched tender does,
        # enrich the project's office from the tender address (evidence-backed).
        if not p.get("mjp_office") and matches:
            for m in matches:
                cand = pool_by_id.get(m["source_tender_id"], {})
                office = extract.detect_mjp_office([cand.get("address", "")])
                if office:
                    store.conn.execute(
                        "UPDATE mjp_projects SET mjp_office=? WHERE id=?",
                        (office, p["id"]))
                    store.conn.commit()
                    break
        for m in matches:
            key = "%s|%s" % (p["id"], m["source_tender_id"])
            forced = decisions.get(key)  # human confirm/reject override
            status = forced or m["link_status"]
            mstore.link_tender(
                store, p["id"], m["source_portal"], m["source_tender_id"],
                status, m["confidence"], m["reasons"], m["amount_note"],
                created_at=now)
            if forced:
                mstore.set_link_decision(store, p["id"], m["source_tender_id"],
                                         status, "operator", now)
            evt = "tender_linked" if status == "linked" else "tender_suggested"
            # Stable (empty) event_date so the UNIQUE(project, type, tender,
            # date) key collapses this across daily re-runs -- a match is a
            # standing relationship, not a per-day event.
            mstore.add_event(store, p["id"], evt,
                             {"tender_id": m["source_tender_id"],
                              "confidence": m["confidence"],
                              "reasons": [r["signal"] for r in m["reasons"]]},
                             event_date="",
                             source_doc_code=m["source_tender_id"],
                             source="mahatenders", created_at=now)
            if status == "linked":
                n_link += 1
            elif status == "suggested":
                n_suggest += 1
                mstore.queue_review(
                    store, "tender_match_suggested",
                    "%s|%s" % (p["id"], m["source_tender_id"]),
                    "Possible tender match for review.",
                    {"confidence": m["confidence"],
                     "reasons": m["reasons"], "note": m["amount_note"]},
                    score=m["confidence"])
    return {"linked": n_link, "suggested": n_suggest,
            "candidates_scanned": len(pool)}


def _prune_non_mjp_docs(index: dict) -> int:
    """Delete the stored original of any document that turned out not to be an
    MJP project, so the durable store keeps only genuine MJP GRs. The index
    entry stays (marked mjp=False) so it is never re-downloaded."""
    n = 0
    for code, entry in index.items():
        if entry.get("mjp") is False:
            local = ROOT / entry.get("local_path", "")
            try:
                if entry.get("local_path") and local.exists():
                    local.unlink()
                    n += 1
            except OSError:
                pass
    return n


# -- search discovery --------------------------------------------------------

def _discover_candidates(session=None) -> tuple[list[str], dict]:
    """Collect GR PDF URLs from the search index (when configured) plus the
    curated candidate file, so the GR portal's captcha-blocked department search
    is replaced by an automatable, honest substitute. Returns (urls, note); the
    note records the backend and any coverage gap without ever masquerading a
    missing backend as a clean 'no results'."""
    curated = _load_json(CANDIDATES_FILE, [])
    curated = [u for u in curated if isinstance(u, str)]
    try:
        res = search_discovery.harvest(session=session, extra_urls=curated)
        return res["urls"], {
            "backend": res["backend"], "queries": res["queries"],
            "found": len(res["urls"]), "rejected": res["rejected"],
            "failures": res["failures"], "curated": len(curated),
        }
    except search_discovery.SearchNotConfigured as exc:
        # No API key and no curated URLs: report the gap, do not pretend success.
        return [], {"backend": "none", "found": 0, "curated": 0,
                    "coverage_gap": str(exc)}
    except Exception as exc:                                # noqa: BLE001
        # Any other discovery failure must degrade to the listing path, not abort
        # the whole MJP run (and never silently report "no new").
        return [], {"backend": "error", "found": 0,
                    "curated": len(curated), "error": str(exc)}


# -- top-level run -----------------------------------------------------------

def run(store: Store, fetch_live: bool = True, session=None,
        state: dict | None = None) -> dict:
    """Full MJP ingest pass. Loads durable state, discovers, extracts, matches,
    persists the index back. Returns a summary; the caller exports the feed."""
    now = timez.iso_ist()
    index = _load_json(INDEX_FILE, {})
    seeds = _load_json(SEEDS_FILE, [])
    candidates, search_note = _discover_candidates(session)
    disc = discover(index, session=session, seeds=seeds,
                    fetch_live=fetch_live, now=now, candidates=candidates)
    disc["search"] = search_note
    built = build_projects(store, index, now)   # sets index[code]["mjp"]
    pruned = _prune_non_mjp_docs(index)
    _save_json(INDEX_FILE, index)               # save AFTER mjp flags + prune
    if state is None:
        try:
            import tracker
            state = {"live_rows": tracker.load_live_snapshot().get("rows", []),
                     "details": tracker.load_details_cache()}
        except Exception:
            state = {}
    matched = match_all(store, state, now)
    sb = (disc.get("search") or {}).get("backend", "none")
    coverage_note = (
        "GRs are discovered three ways, all captcha-free: the portal's latest "
        "listing, a search index over the portal (%s), and curated GR URLs. The "
        "portal's own department/date search is CAPTCHA + load-balancer "
        "protected and is NOT auto-solved. Set GOOGLE_CSE_KEY and GOOGLE_CSE_CX "
        "to widen continuous search; otherwise coverage is the listing plus "
        "curated URLs." % ("search index active" if sb != "none"
                           else "search index inactive: no API key"))
    disc["coverage_note"] = coverage_note
    store.record_run(PORTAL, now, timez.iso_ist(), len(disc["added"]),
                     len(disc["refreshed"]), len(disc["failures"]),
                     note="mjp docs=%d projects=%d links=%d suggested=%d"
                     % (built["documents"], built["projects"],
                        matched["linked"], matched["suggested"]))
    return {"discovery": disc, "build": built, "match": matched,
            "pruned_non_mjp": pruned, "indexed_docs": len(index),
            "coverage_note": coverage_note}
