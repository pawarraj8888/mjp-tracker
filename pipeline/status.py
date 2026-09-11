"""Data-freshness / source-health export (data_status.json).

Answers "how current, complete and trustworthy is this?" from real signals:
the tracker's last live portal scrape (live.json) and the pipeline's crawl-run
log. All timestamps are emitted timezone-aware in IST; the dashboard shows them
and derives relative age and the next scheduled run from the stated cadence.
"""

from __future__ import annotations

import json
from pathlib import Path

import timez

from .store import Store

ROOT = Path(__file__).resolve().parent.parent
LIVE_FILE = ROOT / "live.json"
# The GitHub Actions cron that runs the tracker (.github/workflows/tracker.yml).
CADENCE_MINUTES = 15
STALE_AFTER_MINUTES = CADENCE_MINUTES * 4  # 60 min with no success => stale


def _load_live() -> dict:
    try:
        return json.loads(LIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build(store: Store) -> dict:
    live = _load_live()
    live_gen = timez.to_ist(live.get("generated"))
    runs = store.query(
        "SELECT portal, started, finished, new_count, upd_count, errors, note"
        " FROM crawl_runs ORDER BY id DESC LIMIT 50")

    # Per-source (portal) last run and health.
    sources: dict = {}
    for r in runs:
        if r["portal"] in sources:
            continue
        finished = timez.to_ist(r["finished"])
        status = "failed" if r["errors"] else "ok"
        sources[r["portal"]] = {
            "portal": r["portal"],
            "last_run": timez.iso_ist(finished) if finished else "",
            "last_run_label": timez.fmt_ist(finished) if finished else "unknown",
            "new": r["new_count"], "updated": r["upd_count"],
            "errors": r["errors"], "status": status, "note": r["note"] or "",
        }

    last_success = None
    for r in runs:
        if not r["errors"]:
            last_success = timez.to_ist(r["finished"])
            break
    last_attempt = timez.to_ist(runs[0]["finished"]) if runs else None
    last_change = None
    for r in runs:
        if (r["new_count"] or 0) > 0:
            last_change = timez.to_ist(r["finished"])
            break

    # Source collection freshness is what the user cares about: the tracker's
    # last live portal scrape.
    collect_ref = live_gen or last_success
    age_min = None
    if collect_ref:
        age_min = int((timez.now_ist() - collect_ref).total_seconds() // 60)

    if collect_ref is None:
        overall = "unknown"
    elif any(s["status"] == "failed" for s in sources.values()) and \
            (age_min is not None and age_min > STALE_AFTER_MINUTES):
        overall = "failed"
    elif age_min is not None and age_min > STALE_AFTER_MINUTES:
        overall = "stale"
    elif any(s["errors"] for s in sources.values()):
        overall = "partial"
    else:
        overall = "up_to_date"

    next_run = None
    if collect_ref:
        next_run = timez.iso_ist(
            collect_ref + timez.timedelta(minutes=CADENCE_MINUTES))

    return {
        "generated": timez.iso_ist(),
        "overall_status": overall,
        "last_source_collection": timez.iso_ist(collect_ref) if collect_ref else "",
        "last_source_collection_label":
            timez.fmt_ist(collect_ref) if collect_ref else "never",
        "age_minutes": age_min,
        "last_attempt": timez.iso_ist(last_attempt) if last_attempt else "",
        "last_successful_collection":
            timez.iso_ist(last_success) if last_success else "",
        "last_material_change": timez.iso_ist(last_change) if last_change else "",
        "cadence_minutes": CADENCE_MINUTES,
        "next_scheduled_run": next_run or "",
        "sources": list(sources.values()),
    }
