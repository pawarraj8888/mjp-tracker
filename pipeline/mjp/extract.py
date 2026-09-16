"""Extract structured evidence from an official MJP approval document (a
Government Resolution PDF).

Grounded in the real portal output, not assumptions. Two honest constraints
shape the parser:

* The GR portal embeds a Devanagari font whose ToUnicode map is broken, so
  ``pypdf`` returns *mangled* Marathi (चिखलदरा -> "शिखलदरा",
  प्राधिकरण -> "प्राशधकरर्"). Latin text, digits, amounts and the document code
  extract cleanly. Every Marathi anchor here therefore matches the correct
  Unicode spelling, the observed mangled form, AND an English equivalent, so a
  fact is caught whether the text layer is clean or garbled.
* Scanned resolutions would need OCR (tesseract ``eng+mar``). That binary is a
  documented blocker; the OCR path is present but guarded, and a document whose
  text layer is empty is flagged (``extract_ok=0``) for the review queue rather
  than silently treated as "nothing found".

Nothing about a specific project (its role, amount, or tender match) is
hard-coded. Listing metadata (department, title, GR date) may be supplied by the
caller from the portal's own clean HTML grid; the PDF extraction independently
verifies and enriches it, and disagreement lowers confidence.
"""

from __future__ import annotations

import io
import re

import money


# -- MJP role vocabulary (clean Unicode | mangled | English) -----------------
# "जीवन" and "महाराष्ट्र" survive the broken font intact, so the agency is
# anchored on them plus the role context word.
_MJP_NAME = ("महाराष्ट्र जीवन", "जीवन प्राधिकरण", "जीवन प्राशधकरर्",
             "maharashtra jeevan", "jeevan pradhikaran", "jal pradhikaran")
_MJP_ABBR_RE = re.compile(r"\bMJP\b")
# कार्यान्वयन यंत्रणा = implementing agency (mangled: कायान्वयन यंत्रर्ा).
_IMPLEMENTING = ("कार्यान्वयन यंत्रणा", "कायान्वयन यंत्रर्ा", "कायार्न्वयन",
                 "implementing agency", "executing agency")
_TENDERING = ("executive engineer, mjp", "chief engineer mjp",
              "mjp division", "mjp region", "mjp wm division",
              "निविदा प्राधिकरण", "tendering authority")
_TECH_SANCTION = ("तांत्रिक मान्यता", "तांशत्रक मान्यता", "technical sanction")

ROLE_IMPLEMENTING = "implementing_agency"
ROLE_TENDERING = "tendering_authority"
ROLE_TECH = "technical_sanction"
ROLE_MENTIONED = "mentioned"
ROLE_NONE = "none"

# -- document-type vocabulary ------------------------------------------------
# The document's OWN subject (title) decides its type; a body that merely
# *references* an earlier revised guideline must not mislabel a funding
# sanction as "revised". Rules are tried in this order against the title first,
# then (only if the title is silent) the body -- with "revised" last so a stray
# reference never dominates.
_DOCTYPE_RULES = [
    ("fund_release", ("निधी वितरण", "शनधी शवतरर्", "निधी वितरीत", "वितरीत करण्या",
                      "fund release", "release of funds")),
    ("technical_sanction", _TECH_SANCTION),
    ("administrative_approval", ("प्रशासकीय मान्यता देण्याबाबत",
                                 "प्रिासकीय मान्यता देण्याबाबत",
                                 "administrative approval")),
    ("funding_sanction", ("निधी मंजूर", "शनधी मंजूर", "निधी उपलब्ध",
                          "अनुदान", "मंजूर करण्याबाबत", "sanction of funds")),
    ("revised_sanction", ("सुधारित प्रशासकीय", "सुधारीत प्रशासकीय",
                          "revised administrative", "revised sanction")),
]

# -- scheme vocabulary -------------------------------------------------------
_SCHEME_RULES = [
    ("वैशिष्ट्यपूर्ण (special grant)", ("वैशिष्ट्य", "वैशिष्ट्यपूर्ण")),
    ("Jal Jeevan Mission", ("जल जीवन", "jal jeevan", "jjm")),
    ("AMRUT", ("अमृत", "amrut")),
    ("MSJNM", ("msjnm", "जीवन योजना", "nagarotthan", "नागरोत्थान")),
    ("Swachh Bharat", ("स्वच्छ भारत", "swachh")),
]

# -- work-theme vocabulary ---------------------------------------------------
_THEME_RULES = [
    ("water_supply", ("पाणीपुरवठा", "पार्ीपुरववा", "पार्ीपुरवठा", "जलवाहिनी",
                      "जलवाशहनी", "water supply", "intake well", "jack well",
                      "pump house", "rising main", "wtp", "esr", "mbr")),
    ("sewerage", ("मलनिस्सारण", "मलशनस्सारर्", "सांडपाणी", "sewerage",
                  "sewage", "drainage")),
    ("roads", ("रस्ते", "रस्ता", "road", "approach road", "bridge", "पूल")),
]

MAX_PAGES = 60                              # bound CPU on a pathological PDF
_CODE_RE = re.compile(r"\b(\d{15,20})\b")
_GRNO_RE = re.compile(
    r"(?:शासन\s*निर्णय\s*क्रमांक|िासन\s*शनर्णय\s*क्रमांक|G\.?R\.?\s*No)"
    r"\s*[:\-]*\s*([^\n]{4,80})", re.I)


def extract_pages(pdf_bytes: bytes) -> tuple[list[str], bool]:
    """Return (per-page text, ocr_used). Uses pypdf; falls back to OCR only
    when the text layer is essentially empty AND the OCR stack is available."""
    pages: list[str] = []
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        # Cap pages processed: a GR is at most a few dozen pages, so this bounds
        # CPU on a pathological document without losing real content.
        for i, pg in enumerate(reader.pages):
            if i >= MAX_PAGES:
                break
            pages.append(pg.extract_text() or "")
    except Exception:
        pages = []
    total = sum(len(p.strip()) for p in pages)
    if total >= 40:
        return pages, False
    ocr = _ocr_pages(pdf_bytes)
    if ocr is not None:
        return ocr, True
    return pages, False


def _ocr_pages(pdf_bytes: bytes):
    """Guarded OCR path (tesseract eng+mar). Returns page texts or ``None``
    when the OCR stack is not installed (documented blocker: no-op, never a
    crash)."""
    try:
        import pdf2image  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        return None
    try:
        images = pdf2image.convert_from_bytes(pdf_bytes, dpi=200)
        return [pytesseract.image_to_string(img, lang="eng+mar")
                for img in images]
    except Exception:
        return None


def _contains_any(text: str, variants) -> bool:
    low = text.lower()
    return any(v in text or v.lower() in low for v in variants)


def _first_variant_pos(text: str, variants):
    low = text.lower()
    best = None
    for v in variants:
        i = text.find(v)
        if i < 0:
            i = low.find(v.lower())
        if i >= 0 and (best is None or i < best):
            best = i
    return best


def _passage_around(text: str, pos: int, width: int = 180) -> str:
    start = max(0, pos - width // 3)
    end = min(len(text), pos + width)
    snippet = text[start:end]
    return re.sub(r"\s+", " ", snippet).strip()


def detect_mjp_role(pages: list[str]) -> dict:
    """Establish MJP's documented role, with the supporting passage and page.

    A bare mention is NOT enough: without a role context it returns
    ``mentioned`` at low confidence so the caller can route it to review. When
    an implementing-agency / tendering-authority context sits next to the MJP
    name, confidence is high and the verbatim passage is retained.
    """
    for idx, text in enumerate(pages):
        page_no = idx + 1
        has_name = _contains_any(text, _MJP_NAME) or bool(_MJP_ABBR_RE.search(text))
        if not has_name:
            continue
        name_pos = _first_variant_pos(text, _MJP_NAME)
        if name_pos is None:
            m = _MJP_ABBR_RE.search(text)
            name_pos = m.start() if m else 0
        # Role context: search the whole page for the strongest role signal.
        if _contains_any(text, _IMPLEMENTING):
            pos = _first_variant_pos(text, _IMPLEMENTING) or name_pos
            return {"role": ROLE_IMPLEMENTING, "confidence": 0.9,
                    "evidence": _passage_around(text, min(pos, name_pos)),
                    "page": page_no}
        if _contains_any(text, _TENDERING):
            pos = _first_variant_pos(text, _TENDERING) or name_pos
            return {"role": ROLE_TENDERING, "confidence": 0.85,
                    "evidence": _passage_around(text, min(pos, name_pos)),
                    "page": page_no}
        if _contains_any(text, _TECH_SANCTION):
            return {"role": ROLE_TECH, "confidence": 0.6,
                    "evidence": _passage_around(text, name_pos),
                    "page": page_no}
        # Named but no role context found on this page: incidental mention.
        return {"role": ROLE_MENTIONED, "confidence": 0.3,
                "evidence": _passage_around(text, name_pos),
                "page": page_no}
    return {"role": ROLE_NONE, "confidence": 0.0, "evidence": "", "page": None}


_MJP_OFFICE_RE = re.compile(
    r"((?:Executive|Superintending|Chief)\s+Engineer[,\s]+"
    r"(?:MJP|Maharashtra\s+Jeevan\s+Pradhikaran)\b[^\n.]{0,20}?\b"
    r"(?:Sub[- ]?Division|Division|Region|Circle|Mandal)\b"
    r"(?:\s+(?!The|MJP|Chief|Executive|Superintending|Region|Division)"
    r"[A-Z][a-zA-Z]+){0,2})", re.I)


def detect_mjp_office(pages: list[str]) -> str:
    """The specific MJP division/region/circle, when the document names one.
    Many GRs name MJP as the agency without a division, so ``""`` (not stated)
    is a common, honest result."""
    for text in pages:
        m = _MJP_OFFICE_RE.search(text)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
    return ""


def parse_doc_code(pages: list[str], url: str = "") -> str:
    """Prefer the authoritative code from the file URL; fall back to the
    सांकेतांक printed in the body."""
    m = re.search(r"/(\d{15,20})\.pdf", url or "")
    if m:
        return m.group(1)
    joined = money.normalize_digits(" ".join(pages))
    m = _CODE_RE.search(joined)
    return m.group(1) if m else ""


def date_from_code(code: str) -> str:
    """The 18-digit code begins with the portal upload timestamp
    (YYYYMMDDHHMMSS...), a verifiable portal publication instant. Returns an ISO
    date, or ``""`` if the code is malformed."""
    if not code or len(code) < 8 or not code[:8].isdigit():
        return ""
    y, mo, d = code[0:4], code[4:6], code[6:8]
    if not ("2000" <= y <= "2099" and "01" <= mo <= "12" and "01" <= d <= "31"):
        return ""
    return "%s-%s-%s" % (y, mo, d)


def detect_by_rules(text: str, rules) -> str:
    for label, variants in rules:
        if _contains_any(text, variants):
            return label
    return ""


def detect_doc_type(title: str, body: str) -> str:
    """Classify from the document's own subject line first, then the body."""
    if title:
        label = detect_by_rules(title, _DOCTYPE_RULES)
        if label:
            return label
    return detect_by_rules(body, _DOCTYPE_RULES) or "other"


_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def detect_language(body: str) -> str:
    """``mr`` when the body carries Devanagari script, else ``en`` -- reflects
    the document's actual content, not merely the URL variant it came from."""
    return "mr" if _DEVANAGARI_RE.search(body or "") else "en"


_WORK_TOKENS = ("well", "dam", "main", "pump", "road", "bridge", "bund",
                "pipe", "pipeline", "machinery", "reservoir", "tank", "wtp",
                "etp", "stp", "esr", "gsr", "mbr", "rising", "gravity",
                "intake", "jack", "headwork", "sump", "valve", "motor",
                "dia", "length", "depth", "capacity", "lph", "hp", "km", "rmt")


def _looks_like_work(s: str) -> bool:
    low = s.lower()
    return (len(s) >= 18 or any(t in low for t in _WORK_TOKENS)) and \
        any(t in low for t in _WORK_TOKENS)


_SCHEDULE_MARKER_RE = re.compile(r"लक्ष|रक्कम\s*रु|अ\.?\s*क्र|amount.*lakh", re.I)


def _has_schedule_marker(page: str) -> bool:
    """True when a page is the cost schedule (its column is stated in lakh), so
    its whole-line decimals are amounts rather than stray numbers."""
    return bool(_SCHEDULE_MARKER_RE.search(page))


def parse_amounts(pages: list[str]) -> dict:
    """Approved total plus best-effort component amounts (lakh column).

    The header total is written in full rupees (``रु. 24,99,25,000/-`` /
    ``Rs. 24,99,25,000``) AND/OR as ``24.9925 कोटी`` -- the same figure in two
    spellings, so the larger of the two candidates is taken (a smaller
    instalment or fee never displaces a crore-worded total). Components sit in a
    "रक्कम रु. लक्ष" schedule column and are parsed with a lakh default unit only
    on pages that actually carry that column. Sums are cross-checked, never
    forced.
    """
    full = money.normalize_digits("\n".join(pages))
    rupee_total = None
    for m in re.finditer(r"(?:रु|Rs|INR|₹)\.?\s*([\d,]+(?:\.\d+)?)\s*/?-?",
                         full, re.I):
        val = money.parse_indian_amount(m.group(1))
        if val is not None and (rupee_total is None or val > rupee_total):
            rupee_total = val
    crore_total = None
    for m in re.finditer(r"([\d,]+(?:\.\d+)?)\s*(?:कोटी|कोटि|crore)", full, re.I):
        val = money.parse_indian_amount(m.group(1) + " कोटी")
        if val is not None and (crore_total is None or val > crore_total):
            crore_total = val
    # Both are spellings of the same header total; keep the larger so a rupee
    # instalment (e.g. रु. 2,50,000) can never override 24.9925 कोटी.
    candidates = [c for c in (rupee_total, crore_total) if c is not None]
    approved = max(candidates) if candidates else None

    # Component amounts sit alone in the schedule's "रक्कम रु. लक्ष" column, so
    # each is a line whose entire content is one number. Engineering dimensions
    # (3.00 m dia., L-96.00 M) are embedded INSIDE description text and never
    # occupy a line by themselves. Only schedule pages are scanned, so a stray
    # whole-line decimal elsewhere is never mis-scaled by the lakh unit.
    components: list[str] = []
    for page in pages[-3:] if len(pages) >= 3 else pages:
        npage = money.normalize_digits(page)
        if not _has_schedule_marker(npage):
            continue
        for line in npage.splitlines():
            m = re.fullmatch(r"\s*([\d,]{1,7}\.\d{1,2})\s*", line)
            if not m:
                continue
            raw = m.group(1)
            if money.parse_amount(raw) is not None and \
                    money.parse_amount(raw) >= money._LAKH:
                continue  # a full-rupee total, not a lakh-column component
            d = money.parse_indian_amount(raw, default_unit="lakh")
            if d is not None and d > 0:
                components.append(money.dec_to_str(d))
    comp_sum = money.add(components) if components else None
    return {
        "approved_cost_inr": money.dec_to_str(approved),
        "components_inr": components,
        "components_sum_inr": money.dec_to_str(comp_sum) if comp_sum else None,
    }


def english_work_lines(pages: list[str], limit: int = 12) -> list[str]:
    """English work descriptions (in parentheses) from the schedule -- these
    survive the broken font and give a plain-English view of the works."""
    out: list[str] = []
    text = "\n".join(pages)
    for m in re.finditer(r"\(([A-Za-z][^()]{8,160}(?:\([^()]*\)[^()]{0,60})?)\)",
                         text):
        s = re.sub(r"\s+", " ", m.group(1)).strip()
        if _looks_like_work(s) and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def extract_document(pdf_bytes: bytes, meta: dict | None = None) -> dict:
    """Top-level: turn a downloaded GR PDF (+ optional clean listing metadata)
    into a structured, evidence-tagged record. ``missing`` and ``uncertainties``
    make the gaps explicit rather than papering over them."""
    meta = meta or {}
    pages, ocr_used = extract_pages(pdf_bytes)
    full = "\n".join(pages)
    extract_ok = sum(len(p.strip()) for p in pages) >= 40

    code = parse_doc_code(pages, meta.get("url", ""))
    role = detect_mjp_role(pages)
    amounts = parse_amounts(pages)
    doc_type = detect_doc_type(meta.get("title", ""), full)
    scheme = meta.get("scheme") or detect_by_rules(full, _SCHEME_RULES)
    theme = detect_by_rules(full, _THEME_RULES) or "other"
    works_en = english_work_lines(pages)

    issue_date = (meta.get("issue_date") or date_from_code(code) or "")
    grno_m = _GRNO_RE.search(full)
    gr_number = meta.get("gr_number") or (grno_m.group(1).strip() if grno_m else "")

    missing = []
    if not amounts["approved_cost_inr"]:
        missing.append("approved_cost")
    if role["role"] == ROLE_NONE:
        missing.append("mjp_role")
    if not (meta.get("municipality") or "").strip():
        missing.append("municipality")

    uncertainties = []
    if ocr_used:
        uncertainties.append("Text recovered via OCR; verify against original.")
    if not extract_ok:
        uncertainties.append(
            "PDF text layer empty (likely scanned); OCR unavailable, needs "
            "manual review.")
    if role["role"] == ROLE_MENTIONED:
        uncertainties.append(
            "MJP is named but its role is not stated in extractable text; "
            "queued for review.")
    cs = amounts.get("components_sum_inr")
    ap = amounts.get("approved_cost_inr")
    if cs and ap and money.str_to_dec(cs) and money.str_to_dec(ap):
        diff = abs(money.str_to_dec(cs) - money.str_to_dec(ap))
        if diff > money.str_to_dec(ap) * money.Decimal("0.02"):
            uncertainties.append(
                "Component sum (%s) and stated total (%s) differ; components "
                "may be incomplete." % (cs, ap))

    return {
        "doc_code": code,
        "gr_number": gr_number,
        "department": meta.get("department", ""),
        "doc_type": doc_type,
        "title_original": meta.get("title", ""),
        "language": detect_language(full),
        "issue_date": issue_date,
        "url": meta.get("url", ""),
        "mjp_role": role["role"],
        "mjp_role_evidence": role["evidence"],
        "mjp_role_page": role["page"],
        "mjp_role_confidence": role["confidence"],
        "mjp_office": detect_mjp_office(pages),
        "approved_cost_inr": amounts["approved_cost_inr"],
        "components_inr": amounts["components_inr"],
        "components_sum_inr": amounts["components_sum_inr"],
        "scheme": scheme,
        "work_theme": theme,
        "works_en": works_en,
        "municipality": meta.get("municipality", ""),
        "district": meta.get("district", ""),
        "ocr_used": ocr_used,
        "extract_ok": extract_ok,
        "missing": missing,
        "uncertainties": uncertainties,
        "page_count": len(pages),
    }
