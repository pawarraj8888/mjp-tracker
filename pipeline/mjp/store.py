"""Persistence for MJP projects, documents, approval events and tender links.

Thin functions over the shared canonical :class:`pipeline.store.Store` (the same
SQLite connection, ids, and idempotency discipline as the tender pipeline). A
project id is a deterministic ``uuid5`` of its natural key so re-ingesting the
same documents never creates a duplicate project, and two genuinely different
projects are never merged just because their names look similar: the key is the
normalized (district, municipality, work theme), and an unknown municipality
yields an unmergeable singleton keyed on the document code.
"""

from __future__ import annotations

import json
import re

from ..store import Store, uid


def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", str(s).strip().lower())
    return re.sub(r"[^\wऀ-ॿ ]", "", s)


def project_uid(district: str, municipality: str, work_theme: str,
                fallback_code: str) -> str:
    muni = _norm(municipality)
    if not muni:
        # Unknown place: keep it a singleton (never merge unknowns together).
        return uid("mjp_project", "doc", fallback_code or "")
    return uid("mjp_project", _norm(district), muni, work_theme or "")


def _j(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def upsert_project(store: Store, proj: dict) -> str:
    pid = proj["id"]
    exists = store.conn.execute(
        "SELECT 1 FROM mjp_projects WHERE id=?", (pid,)).fetchone() is not None
    cols = {
        "id": pid,
        "title_original": proj.get("title_original"),
        "title_en": proj.get("title_en"),
        "municipality": proj.get("municipality"),
        "district": proj.get("district"),
        "scheme": proj.get("scheme"),
        "mjp_office": proj.get("mjp_office"),
        "mjp_role": proj.get("mjp_role"),
        "mjp_role_evidence": proj.get("mjp_role_evidence"),
        "mjp_role_page": proj.get("mjp_role_page"),
        "mjp_role_confidence": proj.get("mjp_role_confidence"),
        "approved_cost_inr": proj.get("approved_cost_inr"),
        "revised_cost_inr": proj.get("revised_cost_inr"),
        "funds_released_inr": proj.get("funds_released_inr"),
        "tender_estimate_inr": proj.get("tender_estimate_inr"),
        "work_theme": proj.get("work_theme"),
        "status_label": proj.get("status_label"),
        "primary_doc_code": proj.get("primary_doc_code"),
        "missing": _j(proj.get("missing")),
        "uncertainties": _j(proj.get("uncertainties")),
        "first_detected_at": proj.get("first_detected_at"),
        "last_checked_at": proj.get("last_checked_at"),
        "raw": _j(proj.get("raw")),
    }
    fields = ",".join(cols)
    ph = ",".join("?" for _ in cols)
    # first_detected_at is preserved on update (set once, when discovered).
    updates = ",".join("%s=excluded.%s" % (k, k) for k in cols
                       if k not in ("id", "first_detected_at"))
    store.conn.execute(
        "INSERT INTO mjp_projects (%s) VALUES (%s) "
        "ON CONFLICT(id) DO UPDATE SET %s" % (fields, ph, updates),
        tuple(cols.values()))
    store.conn.commit()
    return pid


def upsert_document(store: Store, doc: dict) -> None:
    """Idempotent on ``doc_code`` (the unique portal code). First-detected is
    set once; last-checked advances every run."""
    cols = {
        "project_id": doc.get("project_id"),
        "doc_code": doc.get("doc_code"),
        "gr_number": doc.get("gr_number"),
        "department": doc.get("department"),
        "doc_type": doc.get("doc_type"),
        "title_original": doc.get("title_original"),
        "language": doc.get("language"),
        "issue_date": doc.get("issue_date"),
        "publication_date": doc.get("publication_date"),
        "first_detected_at": doc.get("first_detected_at"),
        "last_checked_at": doc.get("last_checked_at"),
        "url": doc.get("url"),
        "local_path": doc.get("local_path"),
        "sha256": doc.get("sha256"),
        "page_refs": _j(doc.get("page_refs")),
        "ocr_used": 1 if doc.get("ocr_used") else 0,
        "extract_ok": 1 if doc.get("extract_ok", True) else 0,
        "raw": _j(doc.get("raw")),
    }
    fields = ",".join(cols)
    ph = ",".join("?" for _ in cols)
    updates = ",".join("%s=excluded.%s" % (k, k) for k in cols
                       if k not in ("doc_code", "first_detected_at"))
    store.conn.execute(
        "INSERT INTO mjp_documents (%s) VALUES (%s) "
        "ON CONFLICT(doc_code) DO UPDATE SET %s" % (fields, ph, updates),
        tuple(cols.values()))
    store.conn.commit()


def add_event(store: Store, project_id: str, event_type: str, detail,
              event_date: str = "", source_doc_code: str = "",
              source: str = "", created_at: str = "") -> bool:
    """Record an approval/lifecycle event idempotently. Returns True if newly
    inserted. The UNIQUE(project, type, doc, date) key means re-ingest and
    retries never duplicate history, and events are never forced into an
    assumed order."""
    existing = store.conn.execute(
        "SELECT 1 FROM mjp_events WHERE project_id=? AND event_type=?"
        " AND COALESCE(source_doc_code,'')=? AND COALESCE(event_date,'')=?",
        (project_id, event_type, source_doc_code or "", event_date or "")
    ).fetchone()
    if existing:
        return False
    store.conn.execute(
        "INSERT INTO mjp_events (project_id, event_type, detail, event_date,"
        " source_doc_code, source, created_at) VALUES (?,?,?,?,?,?,?)",
        (project_id, event_type, _j(detail), event_date, source_doc_code,
         source, created_at))
    store.conn.commit()
    return True


def link_tender(store: Store, project_id: str, source_portal: str,
                source_tender_id: str, link_status: str, confidence: float,
                reasons, amount_note: str = "", created_at: str = "") -> None:
    cols = {
        "project_id": project_id,
        "source_portal": source_portal,
        "source_tender_id": source_tender_id,
        "link_status": link_status,
        "confidence": confidence,
        "reasons": _j(reasons),
        "amount_note": amount_note,
        "created_at": created_at,
    }
    fields = ",".join(cols)
    ph = ",".join("?" for _ in cols)
    # A human decision (confirmed_at/by) is preserved; auto-fields refresh.
    updates = ",".join("%s=excluded.%s" % (k, k) for k in cols
                       if k not in ("project_id", "source_tender_id",
                                    "created_at"))
    store.conn.execute(
        "INSERT INTO mjp_tender_links (%s) VALUES (%s) "
        "ON CONFLICT(project_id, source_tender_id) DO UPDATE SET %s"
        % (fields, ph, updates), tuple(cols.values()))
    store.conn.commit()


def set_link_decision(store: Store, project_id: str, source_tender_id: str,
                      link_status: str, by: str, at: str) -> None:
    """Apply a human confirm/reject to a suggested match (durable override)."""
    store.conn.execute(
        "UPDATE mjp_tender_links SET link_status=?, confirmed_by=?,"
        " confirmed_at=? WHERE project_id=? AND source_tender_id=?",
        (link_status, by, at, project_id, source_tender_id))
    store.conn.commit()


def queue_review(store: Store, item_type: str, ref: str, reason: str,
                 detail=None, score: float = 0.0) -> None:
    store.conn.execute(
        "INSERT INTO mjp_review_queue (item_type, ref, reason, detail, score)"
        " VALUES (?,?,?,?,?) ON CONFLICT(item_type, ref) DO UPDATE SET"
        " reason=excluded.reason, detail=excluded.detail, score=excluded.score",
        (item_type, ref, reason, _j(detail), score))
    store.conn.commit()


# -- alert delivery tracking -------------------------------------------------

def alert_delivered(store: Store, event_key: str, recipient: str) -> bool:
    """True once every part of this event has been sent to this recipient with
    its attachments -- the dedupe guard that stops repeated checks re-sending."""
    rows = store.conn.execute(
        "SELECT status, attachments_ok FROM mjp_alerts"
        " WHERE event_key=? AND recipient=?", (event_key, recipient)).fetchall()
    return bool(rows) and all(
        r["status"] == "sent" and r["attachments_ok"] for r in rows)


def record_alert(store: Store, event_key: str, recipient: str, status: str,
                 attachments_ok: bool, updated_at: str, part_no: int = 1,
                 parts_total: int = 1, is_backfill: bool = False,
                 last_error: str = "", sent_at: str = "") -> None:
    store.conn.execute(
        "INSERT INTO mjp_alerts (event_key, recipient, part_no, parts_total,"
        " status, attachments_ok, is_backfill, attempts, last_error, sent_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,1,?,?,?)"
        " ON CONFLICT(event_key, recipient, part_no) DO UPDATE SET"
        " status=excluded.status, attachments_ok=excluded.attachments_ok,"
        " parts_total=excluded.parts_total, is_backfill=excluded.is_backfill,"
        " attempts=mjp_alerts.attempts+1, last_error=excluded.last_error,"
        " sent_at=COALESCE(NULLIF(excluded.sent_at,''), mjp_alerts.sent_at),"
        " updated_at=excluded.updated_at",
        (event_key, recipient, part_no, parts_total, status,
         1 if attachments_ok else 0, 1 if is_backfill else 0, last_error,
         sent_at, updated_at))
    store.conn.commit()


# -- read helpers (used by export) -------------------------------------------

def all_projects(store: Store) -> list[dict]:
    return store.query("SELECT * FROM mjp_projects ORDER BY last_checked_at DESC")


def documents_for(store: Store, pid: str) -> list[dict]:
    return store.query(
        "SELECT * FROM mjp_documents WHERE project_id=? ORDER BY issue_date", (pid,))


def events_for(store: Store, pid: str) -> list[dict]:
    return store.query(
        "SELECT * FROM mjp_events WHERE project_id=?"
        " ORDER BY COALESCE(event_date,''), id", (pid,))


def links_for(store: Store, pid: str) -> list[dict]:
    return store.query(
        "SELECT * FROM mjp_tender_links WHERE project_id=?"
        " ORDER BY confidence DESC", (pid,))


def review_items(store: Store) -> list[dict]:
    return store.query(
        "SELECT * FROM mjp_review_queue WHERE resolved=0 ORDER BY score DESC")


def mjp_counts(store: Store) -> dict:
    out = {}
    for t in ("mjp_projects", "mjp_documents", "mjp_events",
              "mjp_tender_links", "mjp_review_queue", "mjp_alerts"):
        out[t] = store.conn.execute(
            "SELECT COUNT(*) c FROM %s" % t).fetchone()["c"]
    return out
