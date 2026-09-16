"""Extraction from GR text: MJP role evidence, incidental-mention handling,
doc type from the subject, and honest handling of an empty text layer."""

from pipeline.mjp import extract


def _pages_implementing():
    return [
        "शासन निर्णय ... चिखलदरा नगरपरिषद, जि. अमरावती करीता निधी मंजूर.\n"
        "सांकेतांक 202606251057023925\n"
        "सदर शासन निर्णयातील कामाची कार्यान्वयन यंत्रणा "
        "“महाराष्ट्र जीवन प्राधिकरण” राहील.\n"
        "एकूण रक्कम रु. 24,99,25,000/- (Intake Well @ Bagling Dam)"]


def test_role_implementing_agency_with_evidence():
    role = extract.detect_mjp_role(_pages_implementing())
    assert role["role"] == "implementing_agency"
    assert role["confidence"] >= 0.85
    assert "जीवन" in role["evidence"]
    assert role["page"] == 1


def test_incidental_mention_is_not_enough():
    # MJP named but with no role context -> "mentioned", low confidence.
    pages = ["A general circular that references महाराष्ट्र जीवन प्राधिकरण "
             "in passing, with no role stated."]
    role = extract.detect_mjp_role(pages)
    assert role["role"] == "mentioned"
    assert role["confidence"] < 0.5


def test_no_mjp_mention():
    role = extract.detect_mjp_role(["A road-works resolution, no agency named."])
    assert role["role"] == "none"


def test_doc_type_from_subject_not_stray_reference():
    # Subject is a funding sanction; body references a "revised" guideline.
    title = "निधी मंजूर करण्याबाबत"
    body = "संदर्भाधीन सुधारीत निकष ... प्रशासकीय मान्यता ..."
    assert extract.detect_doc_type(title, body) == "funding_sanction"


def test_date_from_code():
    assert extract.date_from_code("202606251057023925") == "2026-06-25"
    assert extract.date_from_code("bad") == ""
    assert extract.date_from_code("209913011057023925") == ""  # month 13


def test_empty_text_layer_flagged_for_review():
    # A scanned PDF (no extractable text) must be flagged, not silently empty.
    doc = extract.extract_document(b"%PDF-1.4 scanned image only",
                                   {"title": "x"})
    assert doc["extract_ok"] is False
    assert any("review" in u.lower() or "scanned" in u.lower()
               for u in doc["uncertainties"])


def test_detect_language_from_content():
    assert extract.detect_language("नगर विकास") == "mr"
    assert extract.detect_language("Urban Development") == "en"


def test_crore_total_not_displaced_by_smaller_rupee_instalment():
    # A crore-worded project total must win over a smaller rupee instalment on
    # the same page (regression: the ~1000x under-report).
    pages = ["मुख्य किंमत 24.9925 कोटी आहे. तसेच किरकोळ रक्कम रु. 2,50,000/- मंजूर."]
    amt = extract.parse_amounts(pages)
    assert amt["approved_cost_inr"] == "249925000"


def test_english_rupee_total_is_parsed():
    pages = ["The approved cost is Rs. 24,99,25,000/- for the scheme."]
    amt = extract.parse_amounts(pages)
    from decimal import Decimal
    import money
    assert money.str_to_dec(amt["approved_cost_inr"]) == Decimal("249925000")


def test_components_only_on_schedule_pages():
    # Whole-line decimals on a NON-schedule page must not be scaled as lakh.
    pages = ["Some narrative.\n245.50\n1,234.00\nMore text, no schedule column."]
    amt = extract.parse_amounts(pages)
    assert amt["components_inr"] == []      # no "लक्ष"/"रक्कम रु" marker -> ignored


def test_components_extracted_on_schedule_page():
    pages = ["अ.क्र. कामाचे नाव रक्कम रु. लक्ष\n28.75\n171.81\n(Intake Well)"]
    amt = extract.parse_amounts(pages)
    import money
    got = {money.str_to_dec(c) for c in amt["components_inr"]}
    from decimal import Decimal
    assert Decimal("2875000") in got and Decimal("17181000") in got


def test_mjp_office_detected_when_stated_else_empty():
    assert extract.detect_mjp_office(
        ["The Executive Engineer, MJP WM Division Amravati"]).startswith(
        "Executive Engineer")
    assert extract.detect_mjp_office(["महाराष्ट्र जीवन प्राधिकरण (no division)"]) == ""
