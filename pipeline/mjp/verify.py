"""Verification harness for the MJP feature, anchored on the real Chikhaldara
document and its potential tender (brief section 5).

Runs a fresh ingest and asserts the extracted facts against the reference
values, and asserts the cautious-matching behaviour (a possible match, never an
auto-confirmed one). Every check reports what was compared and what came back,
so a failure is legible.
"""

from __future__ import annotations

import json
from pathlib import Path

import money

from ..store import Store
from . import export, ingest

ROOT = Path(__file__).resolve().parent.parent.parent

CHIKHALDARA_CODE = "202606251057023925"
CHIKHALDARA_TENDER = "2026_COJAL_1337629_1"


def _load_state() -> dict:
    try:
        live = json.loads((ROOT / "live.json").read_text(encoding="utf-8"))
        details = json.loads((ROOT / "details_cache.json").read_text(encoding="utf-8"))
        return {"live_rows": live.get("rows", []), "details": details}
    except (OSError, ValueError):
        return {}


def run(store: Store | None = None, fetch_live: bool = False) -> dict:
    store = store or Store(":memory:")
    ingest.run(store, fetch_live=fetch_live, state=_load_state())
    feed = export.build_feed(store)
    proj = next((p for p in feed["projects"]
                 if p.get("primary_doc_code") == CHIKHALDARA_CODE), None)

    checks = []

    def check(name, got, expected, ok=None):
        passed = (got == expected) if ok is None else ok
        checks.append({"check": name, "expected": expected, "got": got,
                       "pass": bool(passed)})

    if proj is None:
        checks.append({"check": "project_present", "expected": True,
                       "got": False, "pass": False})
        return {"pass": False, "checks": checks, "reason": "no Chikhaldara project"}

    check("document_code", proj["primary_doc_code"], CHIKHALDARA_CODE)
    check("approved_cost_inr",
          money.str_to_dec(proj["approved_cost"]["inr"]),
          money.Decimal("249925000"))
    check("approved_cost_display", proj["approved_cost"]["display"], "24.99 Cr")
    check("district", proj["district"], "Amravati")
    check("municipality", proj["municipality"], "Chikhaldara")
    check("mjp_role", proj["mjp_role"], "implementing_agency")
    check("doc_type",
          proj["documents"][0]["doc_type"] if proj["documents"] else None,
          "funding_sanction")
    check("has_intake_well_component",
          any("intake well" in w.lower() for w in proj["components"]), True)

    # Cautious matching: the tender must be a SUGGESTED (possible) match, never
    # auto-linked, given the scheme/amount differences.
    link = next((l for l in proj["links"]
                 if l["tender_id"] == CHIKHALDARA_TENDER), None)
    check("tender_is_suggested", link["status"] if link else None, "suggested",
          ok=bool(link) and link["status"] == "suggested")
    check("tender_not_auto_linked", link["status"] if link else None,
          "not 'linked'", ok=bool(link) and link["status"] != "linked")
    check("match_notes_amount_not_gst",
          "GST" in (link["amount_note"] if link else ""), True,
          ok=bool(link) and "not assumed to be GST" in link["amount_note"])

    all_pass = all(c["pass"] for c in checks)
    return {"pass": all_pass, "checks": checks,
            "project_id": proj["id"],
            "status_label": proj["status_label"],
            "tender_match_status": proj["tender_match_status"]}
