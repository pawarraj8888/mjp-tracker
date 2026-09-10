"""Canonical store.

Default backend is local SQLite (stdlib), so the pipeline runs with zero
external services. A Supabase/Postgres backend is intentionally stubbed
(see docs/BLOCKERS.md): set ``PIPELINE_DB=supabase`` with credentials to
implement it against the same schema.

Ids are deterministic uuid5 values derived from natural keys, so re-ingesting
the same source data is idempotent (and tests are stable without random ids).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "pipeline.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_NS = uuid.UUID("6f9b1e00-0000-4000-8000-000000000001")  # fixed namespace


def uid(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(_NS, kind + "|" + "|".join(p or "" for p in parts)))


def _json(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        backend = os.environ.get("PIPELINE_DB", "sqlite")
        if backend not in ("sqlite", ":memory:"):
            raise NotImplementedError(
                "PIPELINE_DB=%s is not implemented; only the local sqlite "
                "backend ships today (see docs/BLOCKERS.md for Supabase)."
                % backend)
        if str(path) == ":memory:":
            self.path = ":memory:"
        else:
            self.path = str(path or os.environ.get("PIPELINE_DB_PATH") or DEFAULT_DB)
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        # Run the schema script only when the DB is empty. It uses IF NOT
        # EXISTS throughout, but skipping the re-run avoids needless work (and
        # any DDL contention) when many short-lived Store() connections open,
        # e.g. one per API request.
        have = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenders'"
        ).fetchone()
        if have:
            return
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    # -- tenders -----------------------------------------------------------
    def upsert_tender(self, rec: dict) -> tuple[str, bool]:
        tid = uid("tender", rec["source_portal"], rec["source_tender_id"])
        cur = self.conn.execute("SELECT id FROM tenders WHERE id=?", (tid,))
        exists = cur.fetchone() is not None
        cols = {
            "id": tid,
            "source_portal": rec["source_portal"],
            "source_tender_id": rec["source_tender_id"],
            "extra_source_ids": _json(rec.get("extra_source_ids")),
            "dedupe_hash": rec.get("dedupe_hash"),
            "publishing_org": rec.get("publishing_org"),
            "org_hierarchy": _json(rec.get("org_hierarchy")),
            "category": rec.get("category"),
            "title": rec.get("title"),
            "description": rec.get("description"),
            "estimated_value_inr": rec.get("estimated_value_inr"),
            "emd_inr": rec.get("emd_inr"),
            "tender_fee_inr": rec.get("tender_fee_inr"),
            "publish_date": rec.get("publish_date"),
            "bid_submission_end": rec.get("bid_submission_end"),
            "bid_opening_date": rec.get("bid_opening_date"),
            "pre_bid_date": rec.get("pre_bid_date"),
            "district": rec.get("district"),
            "taluka": rec.get("taluka"),
            "location_raw": rec.get("location_raw"),
            "funding_source": rec.get("funding_source"),
            "funding_scheme": rec.get("funding_scheme"),
            "funding_level": rec.get("funding_level"),
            "status": rec.get("status"),
            "retender_of": rec.get("retender_of"),
            "raw": _json(rec.get("raw")),
            "first_seen_at": rec.get("first_seen_at"),
            "last_seen_at": rec.get("last_seen_at"),
        }
        fields = ",".join(cols)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(
            "%s=excluded.%s" % (k, k) for k in cols
            if k not in ("id", "first_seen_at"))
        self.conn.execute(
            "INSERT INTO tenders (%s) VALUES (%s) "
            "ON CONFLICT(id) DO UPDATE SET %s" % (fields, placeholders, updates),
            tuple(cols.values()))
        self.conn.commit()
        return tid, not exists

    # -- contractors -------------------------------------------------------
    def upsert_contractor(self, canonical_name: str, match_key: str,
                          alias: str = "", seen: str = "",
                          gstin: str = "", home_district: str = "",
                          registration_class: str = "") -> str:
        cid = uid("contractor", match_key)
        row = self.conn.execute(
            "SELECT aliases FROM contractors WHERE id=?", (cid,)).fetchone()
        aliases = set(json.loads(row["aliases"]) if row and row["aliases"] else [])
        if alias:
            aliases.add(alias)
        if row is None:
            self.conn.execute(
                "INSERT INTO contractors (id, canonical_name, match_key, aliases,"
                " gstin, home_district, registration_class, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, canonical_name, match_key, _json(sorted(aliases)),
                 gstin or None, home_district or None,
                 registration_class or None, seen, seen))
        else:
            self.conn.execute(
                "UPDATE contractors SET aliases=?, last_seen=?,"
                " gstin=COALESCE(NULLIF(?,''), gstin),"
                " home_district=COALESCE(NULLIF(?,''), home_district)"
                " WHERE id=?",
                (_json(sorted(aliases)), seen or "", gstin, home_district, cid))
        self.conn.commit()
        return cid

    def all_contractors(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM contractors ORDER BY canonical_name").fetchall()

    def queue_review(self, name_raw: str, cand_id: str, cand_name: str,
                     score: float) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO contractors_review "
            "(name_raw, candidate_id, candidate_name, score) VALUES (?,?,?,?)",
            (name_raw, cand_id, cand_name, score))
        self.conn.commit()

    # -- awards / bids -----------------------------------------------------
    def upsert_award(self, rec: dict) -> None:
        cols = {
            "tender_id": rec.get("tender_id"),
            "source_tender_id": rec.get("source_tender_id"),
            "contractor_id": rec.get("contractor_id"),
            "contractor_name_raw": rec.get("contractor_name_raw"),
            "award_value_inr": rec.get("award_value_inr"),
            "award_date": rec.get("award_date"),
            "work_order_no": rec.get("work_order_no"),
            "completion_period_days": rec.get("completion_period_days"),
            "bidder_count": rec.get("bidder_count"),
            "l1_pct_vs_estimate": rec.get("l1_pct_vs_estimate"),
            "source_url": rec.get("source_url"),
            "raw": _json(rec.get("raw")),
        }
        fields = ",".join(cols)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join("%s=excluded.%s" % (k, k) for k in cols)
        self.conn.execute(
            "INSERT INTO awards (%s) VALUES (%s) "
            "ON CONFLICT(source_tender_id) DO UPDATE SET %s"
            % (fields, placeholders, updates), tuple(cols.values()))
        self.conn.commit()

    def upsert_bid(self, rec: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO bids (tender_id, source_tender_id,"
            " bidder_name, contractor_id, quoted_value_inr, rank, status)"
            " VALUES (?,?,?,?,?,?,?)",
            (rec.get("tender_id"), rec.get("source_tender_id"),
             rec.get("bidder_name"), rec.get("contractor_id"),
             rec.get("quoted_value_inr"), rec.get("rank"), rec.get("status")))
        self.conn.commit()

    def add_document(self, rec: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO documents (tender_id, doc_type, language,"
            " url, local_path, sha256, ocr_text, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rec.get("tender_id"), rec.get("doc_type"), rec.get("language"),
             rec.get("url"), rec.get("local_path"), rec.get("sha256"),
             rec.get("ocr_text"), rec.get("fetched_at")))
        self.conn.commit()

    # -- runs / checkpoints ------------------------------------------------
    def record_run(self, portal: str, started: str, finished: str,
                   new: int, upd: int, errors: int, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO crawl_runs (portal, started, finished, new_count,"
            " upd_count, errors, note) VALUES (?,?,?,?,?,?,?)",
            (portal, started, finished, new, upd, errors, note))
        self.conn.commit()

    def checkpoint_get(self, portal: str, cursor_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT position, done FROM backfill_checkpoints"
            " WHERE portal=? AND cursor_key=?", (portal, cursor_key)).fetchone()
        return dict(row) if row else None

    def checkpoint_set(self, portal: str, cursor_key: str, position: str,
                       done: bool, updated_at: str) -> None:
        self.conn.execute(
            "INSERT INTO backfill_checkpoints (portal, cursor_key, position,"
            " done, updated_at) VALUES (?,?,?,?,?) ON CONFLICT(portal,cursor_key)"
            " DO UPDATE SET position=excluded.position, done=excluded.done,"
            " updated_at=excluded.updated_at",
            (portal, cursor_key, position, 1 if done else 0, updated_at))
        self.conn.commit()

    # -- queries -----------------------------------------------------------
    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def counts(self) -> dict:
        out = {}
        for t in ("tenders", "awards", "contractors", "bids", "documents",
                  "tender_events", "contractors_review", "crawl_runs"):
            out[t] = self.conn.execute(
                "SELECT COUNT(*) c FROM %s" % t).fetchone()["c"]
        return out

    def close(self) -> None:
        self.conn.close()
