"""Cautious project<->tender matching: town+amount alone is not enough, an
amount difference is never assumed to be GST, and only decisive evidence
auto-confirms."""

from pipeline.mjp import match

CHIKHALDARA = {
    "municipality": "Chikhaldara", "district": "Amravati",
    "scheme": "वैशिष्ट्यपूर्ण (special grant)",
    "gr_number": "नपावै-2026/प्र.क्र. 105", "primary_doc_code": "202606251057023925",
    "approved_cost_inr": "249925000",
    "works_en": ["Intake Well @ Bagling Dam", "Jack Well with overhead Pump House"],
}


def _tender(**kw):
    base = {"source_portal": "mahatenders", "source_tender_id": "T1",
            "title": "", "org": "", "address": "", "district": "",
            "work_text": "", "scheme_hint": "", "value_inr": None, "raw_text": ""}
    base.update(kw)
    base["raw_text"] = base["raw_text"] or (base["title"] + " " + base["address"])
    return base


def test_town_alone_is_insufficient():
    t = _tender(title="Chikhaldara Water Supply Scheme")
    assert match.score_match(CHIKHALDARA, t) is None


def test_amount_alone_is_insufficient():
    t = _tender(title="Some water works", value_inr="249925000")
    assert match.score_match(CHIKHALDARA, t) is None


def test_town_plus_mjp_agency_suggests_not_links():
    t = _tender(title="Chikhaldara Water Supply Scheme, under MSJNM",
                address="The Executive Engineer, MJP WM Division Amravati",
                district="Amravati", scheme_hint="MSJNM", value_inr="211804330")
    m = match.score_match(CHIKHALDARA, t)
    assert m is not None
    assert m["link_status"] == "suggested"     # NOT auto-linked
    signals = {r["signal"] for r in m["reasons"]}
    assert "municipality_in_tender" in signals
    assert "mjp_is_tendering_agency" in signals


def test_amount_difference_not_assumed_gst():
    t = _tender(title="Chikhaldara Water Supply Scheme",
                address="Executive Engineer, MJP Division Amravati",
                district="Amravati", value_inr="211804330")
    m = match.score_match(CHIKHALDARA, t)
    assert "not assumed to be GST" in m["amount_note"]


def test_gr_reference_auto_confirms():
    t = _tender(title="Water works",
                work_text="Sanctioned under GR 202606251057023925",
                raw_text="Water works 202606251057023925")
    m = match.score_match(CHIKHALDARA, t)
    assert m["link_status"] == "linked"        # decisive reference


def test_similar_town_name_does_not_match():
    # A different town whose name merely looks similar must not link.
    other = _tender(title="Chandrapur Water Supply Scheme",
                    address="MJP Division Chandrapur", district="Chandrapur")
    m = match.score_match(CHIKHALDARA, other)
    # MJP agency present but town/component differ -> at most one strong signal.
    assert m is None or m["link_status"] != "linked"
