#!/usr/bin/env python3
"""Reversible, audited money backfill for awards.json.

The old parser stored award values with the decimal point deleted
(``INR 4,291,550.826`` -> ``4291550826``). This rewrites each award's
``awarded_value`` from the preserved raw text using the correct decimal-aware
parser (money.py), and:

* preserves the raw source text in ``awarded_value_raw``,
* represents a genuinely missing amount as ``null`` (unknown), not ``0``,
* writes an audit log (corrections.json) of every old -> new change, and
* snapshots the pre-correction file (awards.pre_money_fix.json) once, so the
  whole operation is reversible.

Idempotent: re-running after a clean pass produces no new corrections.

    python backfill_money.py            # apply, writing awards.json in place
    python backfill_money.py --dry-run  # report what would change, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import money
import tracker

ROOT = Path(__file__).resolve().parent
AWARDS_FILE = ROOT / "awards.json"
SNAPSHOT_FILE = ROOT / "awards.pre_money_fix.json"
CORRECTIONS_FILE = ROOT / "corrections.json"


def _raw_value(entry: dict) -> str:
    """The raw contract-value text for an award entry, from the stored fields."""
    fields = entry.get("fields", {})
    for key in ("Total Contract Value :", "Total Contract Value"):
        if fields.get(key):
            return fields[key]
    return tracker._first_pair(fields, tracker.AWARD_VALUE_KEYS)


def backfill(dry_run: bool = False) -> dict:
    awards = json.loads(AWARDS_FILE.read_text(encoding="utf-8"))
    corrections = []
    unknown = 0
    unchanged = 0

    for tid, entry in awards.items():
        award = entry.get("award")
        if not isinstance(award, dict):
            continue
        raw = _raw_value(entry)
        parsed = money.parse_amount(raw)
        corrected = money.dec_to_str(parsed)  # exact string or None
        old = award.get("awarded_value")
        old_str = None if old in (None, "", 0) else str(old)

        award["awarded_value_raw"] = raw
        if corrected == old_str:
            unchanged += 1
            award["awarded_value"] = corrected
            continue
        # Record the correction with enough context to audit and reverse it.
        corrections.append({
            "tender_id": tid,
            "contractor": award.get("contractor", ""),
            "raw_source_text": raw,
            "old_stored_value": old,
            "corrected_value": corrected,
            "corrected_display": money.format_inr(corrected) or "Unknown",
            "old_display_would_have_been": tracker.format_inr(
                money.str_to_dec(old)) if old_str else "Unknown",
            "kind": "data_correction",
        })
        award["awarded_value"] = corrected
        if corrected is None:
            unknown += 1

    result = {
        "entries": len(awards),
        "corrected": len(corrections),
        "unchanged": unchanged,
        "now_unknown": unknown,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    if dry_run:
        return {**result, "corrections": corrections}

    if not SNAPSHOT_FILE.exists():
        # One-time pre-correction snapshot for full reversibility.
        SNAPSHOT_FILE.write_text(AWARDS_FILE.read_text(encoding="utf-8"),
                                 encoding="utf-8")
    AWARDS_FILE.write_text(
        json.dumps(awards, ensure_ascii=False, indent=1), encoding="utf-8")
    CORRECTIONS_FILE.write_text(
        json.dumps({**result, "corrections": corrections},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    res = backfill(dry_run=args.dry_run)
    print(json.dumps({k: v for k, v in res.items() if k != "corrections"},
                     indent=1))
    if args.dry_run:
        for c in res.get("corrections", [])[:10]:
            print("  %s  %r  %s -> %s (%s)" % (
                c["tender_id"], c["raw_source_text"], c["old_stored_value"],
                c["corrected_value"], c["corrected_display"]))
        n = len(res.get("corrections", []))
        if n > 10:
            print("  ... and %d more" % (n - 10))
    return 0


if __name__ == "__main__":
    sys.exit(main())
