"""Link MJP projects to procurement tenders on auditable evidence.

The bar for an automatic link is deliberately high: a similar town name or an
approximately equal amount is NOT enough, and an amount difference is never
assumed to be GST. A match is only auto-confirmed when the tender carries a
decisive, checkable reference (the project's GR/approval number, or the
project's town together with MJP as the tendering agency AND an overlap of
distinctive work components). Everything weaker is surfaced as a *suggested*
match with its reasons, for a human to confirm or reject. One project may match
several tenders and vice versa; nothing here collapses that.
"""

from __future__ import annotations

import re

try:
    from rapidfuzz import fuzz
    _HAVE_FUZZ = True
except Exception:                       # pragma: no cover - fallback path
    _HAVE_FUZZ = False

_MJP_IN_TEXT = re.compile(
    r"\bMJP\b|maharashtra\s+jeevan|jeevan\s+pradhikaran|jal\s+pradhikaran"
    r"|जीवन\s*प्राधिकरण|जीवन\s*प्राशधकरर्|महाराष्ट्र\s*जीवन", re.I)

# Generic engineering words that must not, alone, be treated as a distinctive
# component overlap.
_GENERIC = {"well", "main", "pump", "road", "bridge", "water", "supply",
            "scheme", "works", "work", "house", "tank", "pipe", "pipeline",
            "system", "dam", "rising", "gravity", "intake", "jack", "head",
            "approach", "length", "depth", "dia", "the", "and", "for", "with"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _partial(a: str, b: str) -> int:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0
    if _HAVE_FUZZ:
        return int(fuzz.partial_ratio(a, b))
    return 100 if a in b or b in a else 0


def _distinctive_tokens(text: str) -> set:
    toks = set(re.findall(r"[A-Za-z]{4,}", (text or "").lower()))
    return {t for t in toks if t not in _GENERIC}


def _has_gr_reference(tender_text: str, gr_number: str, doc_code: str) -> str:
    """Return the matched reference string if the tender cites the project's GR
    number or document code -- the strongest, decisive signal."""
    txt = tender_text or ""
    if doc_code and doc_code in re.sub(r"\D", "", txt):
        return "document code %s" % doc_code
    if gr_number:
        # Compare on alphanumerics only (formatting/spacing varies wildly).
        key = re.sub(r"[^0-9A-Za-z]", "", gr_number)[:14]
        if len(key) >= 8 and key.lower() in re.sub(r"[^0-9A-Za-z]", "", txt).lower():
            return "GR number %s" % gr_number
    return ""


def score_match(project: dict, tender: dict) -> dict | None:
    """Score one (project, tender) pair. Returns a match dict or ``None`` when
    there is not enough to even suggest a link."""
    reasons: list[dict] = []
    tender_text = " ".join(str(tender.get(k, "")) for k in
                           ("title", "org", "address", "work_text", "raw_text"))
    strong_categories: set = set()

    # 1) Decisive: explicit GR / document-code reference.
    gr_hit = _has_gr_reference(tender_text, project.get("gr_number", ""),
                               project.get("primary_doc_code", ""))
    decisive = bool(gr_hit)
    if gr_hit:
        reasons.append({"signal": "gr_reference", "detail": gr_hit,
                        "weight": 1.0})

    # 2) Town / municipality named in the tender title or work.
    town = project.get("municipality", "")
    town_hit = town and _partial(town, tender.get("title", "") + " " +
                                 tender.get("work_text", "")) >= 88
    if town_hit:
        reasons.append({"signal": "municipality_in_tender",
                        "detail": "%s named in tender" % town, "weight": 0.4})
        strong_categories.add("town")

    # 3) MJP is the tendering agency (address/org).
    mjp_hit = bool(_MJP_IN_TEXT.search(tender.get("address", "") + " " +
                                       tender.get("org", "")))
    if mjp_hit:
        reasons.append({"signal": "mjp_is_tendering_agency",
                        "detail": "MJP office in tender address/organisation",
                        "weight": 0.3})
        strong_categories.add("mjp_agency")

    # 4) District match.
    dist_hit = (project.get("district") and tender.get("district") and
                _partial(project["district"], tender["district"]) >= 90)
    if dist_hit:
        reasons.append({"signal": "district_match",
                        "detail": "district %s" % tender.get("district"),
                        "weight": 0.15})

    # 5) Distinctive work-component overlap (place/asset names, not generics).
    proj_tokens = set()
    for w in project.get("works_en", []) or []:
        proj_tokens |= _distinctive_tokens(w)
    overlap = proj_tokens & _distinctive_tokens(tender_text)
    if len(overlap) >= 2:
        reasons.append({"signal": "component_overlap",
                        "detail": "shared distinctive terms: %s"
                        % ", ".join(sorted(overlap)[:6]), "weight": 0.3})
        strong_categories.add("component")

    # 6) Scheme: an exact name match supports; a mismatch is only a NOTE.
    p_scheme = _norm(project.get("scheme", ""))
    t_scheme = _norm(tender.get("scheme_hint", ""))
    scheme_note = ""
    if p_scheme and t_scheme:
        if _partial(p_scheme, t_scheme) >= 85:
            reasons.append({"signal": "scheme_match",
                            "detail": "scheme %s" % project.get("scheme"),
                            "weight": 0.25})
            strong_categories.add("scheme")
        else:
            scheme_note = ("Scheme wording differs (project: %s; tender: %s); "
                           "relationship not confirmed on scheme."
                           % (project.get("scheme"), tender.get("scheme_hint")))

    # 7) Amount: reported for context only -- NEVER decisive, and a difference
    # is never assumed to be GST.
    amount_note = ""
    p_amt = project.get("approved_cost_inr")
    t_amt = tender.get("value_inr")
    if p_amt and t_amt:
        amount_note = ("Approved cost %s vs tender estimate %s -- kept as "
                       "separate figures; a difference is not assumed to be "
                       "GST or scope change." % (p_amt, t_amt))

    if not reasons:
        return None

    confidence = min(1.0, sum(r["weight"] for r in reasons))

    # Auto-confirm only on decisive evidence, or town + MJP agency + component.
    auditable = decisive or {"town", "mjp_agency", "component"} <= strong_categories
    # Suggest only with >= 2 independent strong categories (town / mjp_agency /
    # component / scheme). District, theme and amount alone never suggest.
    enough_to_suggest = len(strong_categories) >= 2

    if auditable:
        status = "linked"
    elif enough_to_suggest:
        status = "suggested"
    else:
        return None

    note = " ".join(n for n in (scheme_note, amount_note) if n)
    return {
        "source_portal": tender.get("source_portal", ""),
        "source_tender_id": tender.get("source_tender_id", ""),
        "link_status": status,
        "confidence": round(confidence, 3),
        "reasons": reasons,
        "amount_note": note,
    }


def match_project(project: dict, candidates: list[dict]) -> list[dict]:
    """All qualifying matches for a project, strongest first."""
    out = []
    for t in candidates:
        m = score_match(project, t)
        if m:
            out.append(m)
    out.sort(key=lambda m: (m["link_status"] == "linked", m["confidence"]),
             reverse=True)
    return out
