"""Server-generated notification stream.

Notifications are derived deterministically from canonical lifecycle events (and
the money-correction audit log), so they survive reloads and deployments: the
cron that runs the pipeline regenerates notifications.json, which the dashboard
reads like the other committed state. Per-viewer read/unread state lives in the
browser (the store is single-operator today; see docs/BLOCKERS.md for why true
per-user server persistence is out of scope without a database).

Each notification has a stable id (a uuid5 of its event signature) so marking
one read does not shift when the file is regenerated, and re-runs never create
duplicates.

Kinds are kept distinguishable so the inbox never mixes a real procurement
change with a historical import or a data correction:
    procurement        -- a real award / status change observed in a live run
    historical_import  -- an award record discovered during the initial backfill
    data_correction    -- a value fixed by the money backfill (not a new award)
    source_health      -- a failed/partial collection notice
"""

from __future__ import annotations

import json
import uuid

import timez

from .store import Store

_NS = uuid.UUID("6f9b1e00-0000-4000-8000-000000000002")


def _nid(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "|".join(p or "" for p in parts)))


def _award_message(title: str, tid: str, prev: str, contractor: str,
                   value_fmt: str, contract_date: str, detected: str,
                   discovered: bool) -> str:
    if discovered:
        head = "Award record discovered: %s (%s)." % (title or tid, tid)
        line2 = "Awarded to %s (this tender was first seen already awarded)." \
            % contractor
    else:
        head = "Tender awarded: %s (%s)." % (title or tid, tid)
        line2 = "%s -> Awarded to %s." % (prev or "Open", contractor)
    return "\n".join([
        head, line2,
        "Award value: %s." % (value_fmt or "Unknown"),
        "Contract date: %s." % (contract_date or "not stated"),
        "Detected: %s." % detected,
        "Now available in Awards and under this contractor.",
    ])


def from_events(store: Store) -> list[dict]:
    rows = store.query(
        "SELECT te.id, te.tender_id, te.event_type, te.detail, te.event_at,"
        " te.source, t.title, t.source_tender_id AS stid"
        " FROM tender_events te JOIN tenders t ON t.id = te.tender_id"
        " WHERE te.event_type IN ('awarded','award_record_discovered')")
    out = []
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            d = {}
        discovered = r["event_type"] == "award_record_discovered"
        detected = timez.fmt_ist(r["event_at"]) or r["event_at"] or ""
        title = r["title"] or ""
        contractor = d.get("contractor", "")
        out.append({
            "id": _nid("event", str(r["id"]), r["event_type"]),
            "type": "award_discovered" if discovered else "award",
            "kind": "historical_import" if discovered else "procurement",
            "tender_id": r["stid"],
            "title": title,
            "contractor": contractor,
            "value_inr": d.get("award_value_inr"),
            "value_fmt": d.get("award_value_fmt", ""),
            "contract_date": d.get("award_date", ""),
            "detected_at": timez.iso_ist(timez.to_ist(r["event_at"])),
            "detected_label": detected,
            "message": _award_message(
                title, r["stid"], d.get("previous_status", ""), contractor,
                d.get("award_value_fmt", ""), d.get("award_date", ""),
                detected, discovered),
        })
    return out


def from_corrections(corrections: dict | None) -> list[dict]:
    """Data-correction notices from the money backfill audit log. Labelled as
    corrections, never as new awards."""
    if not corrections:
        return []
    out = []
    for c in corrections.get("corrections", []):
        out.append({
            "id": _nid("correction", c.get("tender_id", ""),
                       str(c.get("corrected_value"))),
            "type": "data_correction",
            "kind": "data_correction",
            "tender_id": c.get("tender_id", ""),
            "title": "Value corrected for %s" % c.get("tender_id", ""),
            "contractor": c.get("contractor", ""),
            "value_inr": c.get("corrected_value"),
            "value_fmt": c.get("corrected_display", ""),
            "contract_date": "",
            "detected_at": timez.iso_ist(),
            "detected_label": timez.fmt_ist(timez.now_ist()),
            "message": ("Data correction (not a new award): %s value re-parsed "
                        "from %r to %s. Decimal point was previously dropped."
                        % (c.get("tender_id", ""), c.get("raw_source_text", ""),
                           c.get("corrected_display", ""))),
        })
    return out


def build(store: Store, corrections: dict | None = None) -> dict:
    items = from_events(store) + from_corrections(corrections)
    # Newest first by detection time; stable for equal timestamps by id.
    items.sort(key=lambda x: (x["detected_at"], x["id"]), reverse=True)
    counts: dict = {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    return {
        "generated": timez.iso_ist(),
        "count": len(items),
        "by_kind": counts,
        "notifications": items,
    }
