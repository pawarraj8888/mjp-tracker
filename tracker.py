#!/usr/bin/env python3
"""
MJP tender tracker.

Watches mahatenders.gov.in for tenders published under the organisation
"Member Secretary(WSSD),Mumbai" (Maharashtra Jeevan Pradhikaran / WSSD).

Every run:
  1. Opens the Tenders-by-Organisation listing, follows the WSSD row and
     collects the live tender list (id, title, ref no, closing date, link).
  2. Diffs against seen.json. For each NEW tender it fetches the detail
     page and parses all label/value fields.
  3. Builds TWO work-details PDFs with reportlab:
       MJP_<tenderid>_EN.pdf  (English, as scraped)
       MJP_<tenderid>_MR.pdf  (Marathi, values machine translated with
                               deep-translator, labels from a hardcoded
                               dictionary, Noto Sans Devanagari fonts)
  4. Sends both PDFs to WhatsApp via the Meta Cloud API (media upload,
     then a document message). English first with a full caption, then
     the Marathi one with a short Marathi caption.
  5. Marks the tender as seen only if the English send succeeded. A
     Marathi-side failure is logged but never blocks the English send.

Besides the MJP WSSD organisation, additional watches are tracked (see
ORG_WATCHES and KEYWORD_WATCHES): full tracking of Zilla Parishad Jalgaon
(RDD-CEO-JALGAON, where DPDC and Amdar Nidhi works are published) and the
Collector Jalgaon office; a keyword scan of RDD-CEO-* and COLLECTOR *
organisations for Amdar Nidhi / DPDC related tenders; and a portal wide
scan of every organisation for anything mentioning Jalgaon (which also
covers PWD and irrigation publishers in the district).

Usage:
    python tracker.py            normal run (needs delivery env vars)
    python tracker.py --dry-run  scrape one tender, build both PDFs into
                                 out/ and skip all delivery
    python tracker.py --serve    local dashboard on http://localhost:8765
                                 (change with --port)

Delivery channels (each used when its env vars are set; at least one is
required for a real run):
    Email:    SMTP_USER, SMTP_PASS (and optionally SMTP_HOST, SMTP_PORT,
              EMAIL_TO); defaults to Gmail SMTP, sending to SMTP_USER
    WhatsApp: WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_TO

State: seen.json (committed back by .github/workflows/tracker.yml).
"""

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

log = logging.getLogger("mjp-tracker")

BASE_URL = "https://mahatenders.gov.in"
ORG_LIST_URL = BASE_URL + "/nicgep/app?page=FrontEndTendersByOrganisation&service=page"
ORG_NAME = "Member Secretary(WSSD),Mumbai"

# ---------------------------------------------------------------------------
# Watches. Two kinds:
#  - ORG_WATCHES: every tender published by a matching organisation node is
#    tracked ("org" is an exact name, "org_re" a regex on the org name).
#  - KEYWORD_WATCHES: the tender lists of all orgs matching org_re are scanned
#    and only rows whose title or org chain contains one of the keywords
#    (case insensitive) are tracked. Used for Amdar Nidhi / DPDC works,
#    which are published by Zilla Parishad (RDD-CEO-*) and Collector
#    offices around the state.
# The portal's own full-text search is captcha protected, so watching is
# done through the captcha-free Tenders-by-Organisation listing.
# ---------------------------------------------------------------------------

ORG_WATCHES = [
    {"name": "MJP WSSD", "org": ORG_NAME},
    {"name": "ZP Jalgaon DPDC", "org_re": r"^RDD-CEO-JALGAON$"},
    {"name": "Collector Jalgaon", "org_re": r"^COLLECTOR\s+JALGAON$"},
]

KEYWORD_WATCHES = [
    {
        # Scanned across every organisation: these fund markers appear in
        # ZP, Collector, and municipal tenders alike.
        "name": "Amdar Nidhi / DPDC",
        "org_re": r".",
        "keywords": [
            # MLA / MP local area funds
            "amdar nidhi", "aamdar nidhi", "amdar fund", "mla fund",
            "mla local", "आमदार", "khasdar", "खासदार", "mp fund", "mplad",
            # District Planning Committee and its common schemes
            "dpdc", "d.p.d.c", "d.p.c", "जिल्हा नियोजन", "district planning",
            "planning committee", "vitta ayog", "vitt ayog",
            "dalit vasti", "dalitvasti", "dalit wasti", "nagari dalit",
            "vishesh ghatak", "2515",
        ],
    },
    {
        # Everything Jalgaon, from any publisher on the portal (PWD, WRD,
        # municipal bodies, universities and so on). Matches the title and
        # the full organisation chain, so "EE PWD Division Jalgaon" style
        # publishers are caught even when the title does not say Jalgaon.
        "name": "Jalgaon statewide",
        "org_re": r".",
        "keywords": ["jalgaon", "jalgaav", "jalgoan", "जळगाव"],
    },
]

# Safety valve: at most this many new tenders are sent per run; the rest
# stay unseen and go out on the next scheduled run.
MAX_SENDS_PER_RUN = 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HTTP_TIMEOUT = 90

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen.json"
FONTS_DIR = ROOT / "fonts"
OUT_DIR = Path(os.environ.get("OUT_DIR") or (ROOT / "out"))

GRAPH_URL = "https://graph.facebook.com/v21.0"
SEND_SLEEP_SECONDS = 3

TENDER_ID_RE = re.compile(r"^\d{4}_\w+_\d+_\d+$")

# ---------------------------------------------------------------------------
# Field layout of the detail PDF. Labels must match the portal captions
# exactly (including the rupee sign used on the site).
# ---------------------------------------------------------------------------

SECTIONS = [
    ("Basic Details", [
        "Organisation Chain",
        "Tender Reference Number",
        "Tender ID",
        "Tender Type",
        "Tender Category",
        "Form Of Contract",
        "Contract Type",
        "Payment Mode",
        "Withdrawal Allowed",
        "No. of Covers",
        "Online Bankers",
    ]),
    ("Fee Details", [
        "Tender Fee in ₹",
        "Processing Fee in ₹",
        "Tender Fee Exemption Allowed",
        "EMD Amount in ₹",
        "EMD Fee Type",
        "EMD Exemption Allowed",
    ]),
    ("Work Details", [
        "Title",
        "Work Description",
        "NDA/Pre Qualification",
        "Tender Value in ₹",
        "Product Category",
        "Sub category",
        "Bid Validity(Days)",
        "Period Of Work(Days)",
        "Location",
        "Pincode",
    ]),
    ("Meeting Details", [
        "Pre Bid Meeting Place",
        "Pre Bid Meeting Address",
        "Pre Bid Meeting Date",
        "Bid Opening Place",
    ]),
    ("Critical Dates", [
        "Published Date",
        "Document Download / Sale Start Date",
        "Document Download / Sale End Date",
        "Clarification Start Date",
        "Clarification End Date",
        "Bid Submission Start Date",
        "Bid Submission End Date",
        "Bid Opening Date",
    ]),
    ("Documents", [
        "NIT Document",
        "Work Item Documents",
    ]),
    ("Tender Inviting Authority", [
        "Name",
        "Address",
    ]),
]

# Field values that get machine translated in the Marathi PDF. Everything
# not listed here (ids, reference numbers, amounts, dates, day counts,
# pincodes) is kept verbatim.
TRANSLATE_VALUE_FIELDS = {
    "Organisation Chain",
    "Tender Type",
    "Tender Category",
    "Form Of Contract",
    "Contract Type",
    "Payment Mode",
    "Withdrawal Allowed",
    "Tender Fee Exemption Allowed",
    "EMD Fee Type",
    "EMD Exemption Allowed",
    "Title",
    "Work Description",
    "NDA/Pre Qualification",
    "Product Category",
    "Sub category",
    "Location",
    "Pre Bid Meeting Place",
    "Pre Bid Meeting Address",
    "Bid Opening Place",
    "Name",
    "Address",
}

# Hardcoded English to Marathi dictionary for labels and section headings,
# so labels never depend on machine translation.
LABELS_MR = {
    "Maharashtra Jeevan Pradhikaran Tender Details":
        "महाराष्ट्र जीवन प्राधिकरण निविदा तपशील",
    "Basic Details": "मूलभूत तपशील",
    "Fee Details": "शुल्क तपशील",
    "Work Details": "कामाचा तपशील",
    "Meeting Details": "बैठकीचा तपशील",
    "Critical Dates": "महत्त्वाच्या तारखा",
    "Tender Inviting Authority": "निविदा आमंत्रित करणारे प्राधिकरण",
    "Organisation Chain": "संस्था साखळी",
    "Tender Reference Number": "निविदा संदर्भ क्रमांक",
    "Tender ID": "निविदा ओळख क्रमांक",
    "Tender Type": "निविदा प्रकार",
    "Tender Category": "निविदा वर्ग",
    "Form Of Contract": "कराराचे स्वरूप",
    "Contract Type": "करार प्रकार",
    "Payment Mode": "देयक पद्धत",
    "Tender Fee in ₹": "निविदा शुल्क (रु.)",
    "Processing Fee in ₹": "प्रक्रिया शुल्क (रु.)",
    "Tender Fee Exemption Allowed": "निविदा शुल्क सूट परवानगी",
    "Withdrawal Allowed": "माघार घेण्याची परवानगी",
    "No. of Covers": "लिफाफ्यांची संख्या",
    "Online Bankers": "ऑनलाइन बँका",
    "Documents": "दस्तऐवज",
    "NIT Document": "निविदा सूचना दस्तऐवज",
    "Work Item Documents": "कामाचे दस्तऐवज",
    "EMD Amount in ₹": "इसारा रक्कम (रु.)",
    "EMD Fee Type": "इसारा शुल्क प्रकार",
    "EMD Exemption Allowed": "इसारा सूट परवानगी",
    "Title": "शीर्षक",
    "Work Description": "कामाचे वर्णन",
    "NDA/Pre Qualification": "पूर्व पात्रता",
    "Tender Value in ₹": "निविदा मूल्य (रु.)",
    "Product Category": "उत्पादन वर्ग",
    "Sub category": "उपवर्ग",
    "Bid Validity(Days)": "बोली वैधता (दिवस)",
    "Period Of Work(Days)": "कामाचा कालावधी (दिवस)",
    "Location": "स्थान",
    "Pincode": "पिनकोड",
    "Pre Bid Meeting Place": "बोलीपूर्व बैठकीचे ठिकाण",
    "Pre Bid Meeting Address": "बोलीपूर्व बैठकीचा पत्ता",
    "Pre Bid Meeting Date": "बोलीपूर्व बैठकीची तारीख",
    "Bid Opening Place": "बोली उघडण्याचे ठिकाण",
    "Published Date": "प्रकाशन तारीख",
    "Document Download / Sale Start Date":
        "दस्तऐवज डाउनलोड / विक्री प्रारंभ तारीख",
    "Document Download / Sale End Date":
        "दस्तऐवज डाउनलोड / विक्री अंतिम तारीख",
    "Clarification Start Date": "स्पष्टीकरण प्रारंभ तारीख",
    "Clarification End Date": "स्पष्टीकरण अंतिम तारीख",
    "Bid Submission Start Date":
        "बोली सादर करण्याची प्रारंभ तारीख",
    "Bid Submission End Date":
        "बोली सादर करण्याची अंतिम तारीख",
    "Bid Opening Date": "बोली उघडण्याची तारीख",
    "Name": "नाव",
    "Address": "पत्ता",
    "New MJP Tender": "नवीन एमजेपी निविदा",
    "New Tender": "नवीन निविदा",
    "Closing Date": "अंतिम तारीख",
    "Source": "स्रोत",
}

# Small fixed dictionary for very common short values, so Yes/No/NA style
# fields stay stable and never burn a translation call.
VALUES_MR = {
    "Yes": "होय",
    "No": "नाही",
    "NA": "लागू नाही",
    "Nil": "निरंक",
    "Online": "ऑनलाइन",
    "Offline": "ऑफलाइन",
    "Open Tender": "खुली निविदा",
    "Limited Tender": "मर्यादित निविदा",
    "Works": "बांधकाम कामे",
    "Goods": "वस्तू",
    "Services": "सेवा",
    "Percentage": "टक्केवारी",
    "Item Rate": "बाब दर",
    "Item Wise": "बाबनिहाय",
    "Lump-sum": "एकरकमी",
    "Tender": "निविदा",
    "fixed": "निश्चित",
    "Please refer Tender documents.":
        "कृपया निविदा दस्तऐवज पहा.",
}

DEV_FONT = "NotoDevanagari"
DEV_FONT_BOLD = "NotoDevanagariBold"
LATIN_FONT = "NotoSansLatin"
LATIN_FONT_BOLD = "NotoSansLatinBold"
_fonts_registered = False
_translation_cache = {}
_translator = None


# ---------------------------------------------------------------------------
# Scraping (portal structure: Tapestry app, session scoped DirectLinks)
# ---------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def portal_get(session, url):
    """GET with one automatic fallback to unverified SSL, since NIC portals
    intermittently serve an incomplete certificate chain."""
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, verify=session.verify)
        r.raise_for_status()
    except requests.exceptions.SSLError:
        if session.verify:
            log.warning("SSL verification failed for %s, retrying unverified", url)
            import urllib3
            urllib3.disable_warnings()
            session.verify = False
            return portal_get(session, url)
        raise
    r.encoding = "utf-8"
    return r


def fetch_org_index(session):
    """Map of organisation name to its (session scoped) tender list url,
    for every org that currently has live tenders."""
    r = portal_get(session, ORG_LIST_URL)
    soup = BeautifulSoup(r.text, "lxml")
    index = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 3:
            a = tr.find("a", href=True)
            name = tds[1].get_text(strip=True)
            if a and name and tds[0].get_text(strip=True).isdigit():
                index[name] = BASE_URL + a["href"]
    if not index:
        raise RuntimeError("No organisations parsed from listing page")
    return index


def parse_title_block(block):
    """Split '[title] [ref no][tender id]' from the list page. Anchors on the
    tender id pattern at the end, then peels the ref no off as the trailing
    bracket balanced group, so brackets inside titles or refs cannot break
    the tender id parse."""
    block = block.strip()
    m = re.search(r"\[(\d{4}_\w+_\d+_\d+)\]\s*$", block)
    if not m:
        return block.strip("[] \n"), "", ""
    tender_id = m.group(1)
    rest = block[: m.start()].rstrip()
    ref_no = ""
    if rest.endswith("]"):
        depth = 0
        for i in range(len(rest) - 1, -1, -1):
            if rest[i] == "]":
                depth += 1
            elif rest[i] == "[":
                depth -= 1
                if depth == 0:
                    ref_no = rest[i + 1:-1].strip()
                    rest = rest[:i]
                    break
    title = rest.strip().strip("[]").strip()
    return title, ref_no, tender_id


def rows_from_list_html(html):
    """Parse an organisation tender list page into row dicts with tender_id,
    title, ref_no, published, closing, opening, org_chain and detail url."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=True)
        if not a or "FrontEndViewTender" not in a["href"]:
            continue
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 6 or not cells[0].isdigit():
            continue
        title, ref_no, tender_id = parse_title_block(cells[4])
        if not TENDER_ID_RE.match(tender_id):
            log.warning("Skipping row with unparseable tender id: %r", cells[4][:120])
            continue
        rows.append({
            "tender_id": tender_id,
            "title": title,
            "ref_no": ref_no,
            "published": cells[1],
            "closing": cells[2],
            "opening": cells[3],
            "org_chain": cells[5],
            "url": BASE_URL + a["href"],
        })
    return rows


def fetch_rows_for_org(session, org_name, index):
    """Tender rows for one organisation, or [] if it has no live tenders.
    Session scoped links can go stale, so one retry re-fetches the index."""
    for attempt in range(2):
        if attempt:
            index.clear()
            index.update(fetch_org_index(session))
        url = index.get(org_name)
        if not url:
            return []
        rows = rows_from_list_html(portal_get(session, url).text)
        if rows:
            return rows
    log.warning("Org %s listed but no rows parsed", org_name)
    return []


def org_matches(name, watch):
    if "org" in watch:
        return name == watch["org"]
    return re.search(watch["org_re"], name) is not None


def keyword_hit(row, keywords):
    hay = (row["title"] + " " + row["org_chain"]).casefold()
    return any(k.casefold() in hay for k in keywords)


def fetch_all_watch_rows(session, include_keyword_scan=True):
    """All live tenders across every watch. Each row carries a 'sources'
    list of every watch it matches (org watches and keyword watches), and
    'source' set to the first for display. A tender is fetched once even
    when several watches point at its organisation."""
    index = fetch_org_index(session)
    by_tid = {}
    order = []

    def add(row, source):
        tid = row["tender_id"]
        existing = by_tid.get(tid)
        if existing is None:
            row = dict(row, sources=[source], source=source)
            by_tid[tid] = row
            order.append(tid)
        elif source not in existing["sources"]:
            existing["sources"].append(source)

    # Decide, per organisation, which watches apply, then fetch that org's
    # tender list at most once and tag each row with every matching watch.
    kw_watches = KEYWORD_WATCHES if include_keyword_scan else []
    for org_name in sorted(index):
        org_watch_hits = [w for w in ORG_WATCHES if org_matches(org_name, w)]
        kw_applicable = [w for w in kw_watches
                         if re.search(w["org_re"], org_name)]
        if not org_watch_hits and not kw_applicable:
            continue
        try:
            rows = fetch_rows_for_org(session, org_name, index)
        except Exception as exc:
            log.error("Org %s scan failed: %s", org_name, exc)
            continue
        for row in rows:
            for w in org_watch_hits:
                add(row, w["name"])
            for w in kw_applicable:
                if keyword_hit(row, w["keywords"]):
                    add(row, w["name"])
        time.sleep(0.35)
    return [by_tid[t] for t in order]


def keyword_watch_names():
    return {w["name"] for w in KEYWORD_WATCHES}


def fetch_tender_rows(session):
    """The MJP WSSD organisation's live tenders (used by --dry-run)."""
    rows = fetch_rows_for_org(session, ORG_NAME, fetch_org_index(session))
    if not rows:
        raise RuntimeError("No tender rows parsed from organisation listing")
    return rows


def fetch_tender_details(session, url):
    """Parse the detail page into {caption: value}. Captions and values are
    td.td_caption / td.td_field pairs in document order; only the first
    td_field after each td_caption is taken so nested tables stay out."""
    r = portal_get(session, url)
    soup = BeautifulSoup(r.text, "lxml")
    details = {}
    pending = None
    for td in soup.find_all("td"):
        cls = td.get("class") or []
        if "td_caption" in cls:
            pending = td.get_text(" ", strip=True)
        elif "td_field" in cls and pending is not None:
            details.setdefault(pending, td.get_text(" ", strip=True))
            pending = None
    return details


# ---------------------------------------------------------------------------
# Translation (deep-translator GoogleTranslator, cached, with fallback)
# ---------------------------------------------------------------------------

def _get_translator():
    global _translator
    if _translator is None:
        from deep_translator import GoogleTranslator
        _translator = GoogleTranslator(source="en", target="mr")
    return _translator


def _split_chunks(text, limit=4500):
    if len(text) <= limit:
        return [text]
    chunks = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def translate_text(text):
    """English to Marathi with 2 retries and cache. Falls back to the
    original English text on failure so a run never crashes on translation."""
    s = (text or "").strip()
    if not s:
        return text
    if s in VALUES_MR:
        return VALUES_MR[s]
    if not re.search(r"[A-Za-z]", s):
        return s
    if s in _translation_cache:
        return _translation_cache[s]
    from deep_translator.exceptions import TranslationNotFound
    result = None
    for attempt in range(3):
        try:
            parts = [_get_translator().translate(c) for c in _split_chunks(s)]
            if any(p is None for p in parts):
                raise ValueError("translator returned None")
            result = " ".join(p.strip() for p in parts)
            break
        except TranslationNotFound:
            # Google echoes untranslatable strings (acronyms, proper nouns)
            # back unchanged and deep-translator reports that as not found.
            result = s
            break
        except Exception as exc:
            log.warning("Translate attempt %d failed (%s): %r", attempt + 1, exc, s[:60])
            time.sleep(1.5 * (attempt + 1))
    if not result:
        log.error("Translation failed, keeping English: %r", s[:80])
        result = s
    _translation_cache[s] = result
    return result


def translate_org_chain(value):
    parts = [p.strip() for p in value.split("||") if p.strip()]
    return " || ".join(translate_text(p) for p in parts)


def translate_value(label, value):
    if label not in TRANSLATE_VALUE_FIELDS:
        return value
    if label == "Organisation Chain":
        return translate_org_chain(value)
    return translate_text(value)


# ---------------------------------------------------------------------------
# PDF building
# ---------------------------------------------------------------------------

def register_devanagari_fonts():
    global _fonts_registered
    if _fonts_registered:
        return
    names = {
        DEV_FONT: "NotoSansDevanagari-Regular.ttf",
        DEV_FONT_BOLD: "NotoSansDevanagari-Bold.ttf",
        LATIN_FONT: "NotoSans-Regular.ttf",
        LATIN_FONT_BOLD: "NotoSans-Bold.ttf",
    }
    for font_name, file_name in names.items():
        path = FONTS_DIR / file_name
        if not path.exists():
            raise RuntimeError("Font missing: " + str(path))
        pdfmetrics.registerFont(TTFont(font_name, str(path)))
    _fonts_registered = True


def sanitize(text):
    """Normalise punctuation that either font may lack. Also enforces the
    no em dash rule on everything that reaches a PDF or caption."""
    if not text:
        return ""
    replacements = {
        "—": "-", "–": "-", "−": "-",
        "‘": "'", "’": "'",
        "“": '"', "”": '"',
        " ": " ",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def xml_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


DEVANAGARI_CHAR_RE = re.compile(r"[ऀ-ॿ]")
LATIN_CHAR_RE = re.compile(r"[A-Za-z@&]")


def devanagari_markup(text, latin_font):
    """Noto Sans Devanagari has no Latin letters (nor @ or &), so tokens
    containing them (tender ids, dates, untranslated fallbacks) are wrapped
    in an inline font tag pointing at the vendored Noto Sans Latin font.

    Font choice is per whole whitespace token: reportlab shapes each word
    with the font of its first fragment, so a word must never mix fonts.
    A token containing any Devanagari stays in the Devanagari font. Pure
    digit or punctuation tokens also stay, those glyphs exist there."""
    out = []
    for token in re.split(r"(\s+)", text):
        if (not token or token.isspace()
                or DEVANAGARI_CHAR_RE.search(token)
                or not LATIN_CHAR_RE.search(token)):
            out.append(xml_escape(token))
        else:
            out.append('<font name="%s">%s</font>'
                       % (latin_font, xml_escape(token)))
    return "".join(out)


def build_tender_pdf(details, row, lang, out_path):
    """Build the work details PDF for one tender. lang is 'en' or 'mr'."""
    if lang == "mr":
        register_devanagari_fonts()
        base_font, bold_font = DEV_FONT, DEV_FONT_BOLD
        title_text = LABELS_MR["Maharashtra Jeevan Pradhikaran Tender Details"]
    else:
        base_font, bold_font = "Helvetica", "Helvetica-Bold"
        title_text = "Maharashtra Jeevan Pradhikaran Tender Details"

    # reportlab only applies uharfbuzz shaping when the style asks for it;
    # without it Devanagari conjuncts and matras render in codepoint order.
    shaping = 1 if lang == "mr" else 0

    title_style = ParagraphStyle(
        "title", fontName=bold_font, fontSize=13, leading=20, shaping=shaping,
        spaceAfter=2, textColor=colors.HexColor("#1a3e6e"))
    sub_style = ParagraphStyle(
        "sub", fontName=base_font, fontSize=9, leading=14, shaping=shaping,
        textColor=colors.HexColor("#555555"), spaceAfter=8)
    section_style = ParagraphStyle(
        "section", fontName=bold_font, fontSize=10.5, leading=16, shaping=shaping,
        spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1a3e6e"))
    label_style = ParagraphStyle(
        "label", fontName=bold_font, fontSize=8.5, leading=13, shaping=shaping)
    value_style = ParagraphStyle(
        "value", fontName=base_font, fontSize=8.5, leading=13, shaping=shaping)

    def para(text, style, bold=False):
        text = sanitize(text)
        if lang == "mr":
            markup = devanagari_markup(
                text, LATIN_FONT_BOLD if bold else LATIN_FONT)
        else:
            markup = xml_escape(text)
        return Paragraph(markup, style)

    story = [para(title_text, title_style, bold=True)]
    if lang == "mr":
        sub = "%s: %s | %s: %s" % (
            LABELS_MR["Tender ID"], row["tender_id"],
            LABELS_MR["Closing Date"], row["closing"])
    else:
        sub = "Tender ID: %s | Closing Date: %s" % (
            row["tender_id"], row["closing"])
    story.append(para(sub, sub_style))

    table_style = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bcc7d6")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])

    for section, fields in SECTIONS:
        data = []
        for label in fields:
            value = details.get(label, "")
            if not value:
                continue
            if lang == "mr":
                disp_label = LABELS_MR.get(label, label)
                disp_value = translate_value(label, value)
            else:
                disp_label = label.replace("in ₹", "in Rs.")
                disp_value = value
            data.append([
                para(disp_label, label_style, bold=True),
                para(disp_value, value_style),
            ])
        if not data:
            continue
        heading = LABELS_MR.get(section, section) if lang == "mr" else section
        story.append(para(heading, section_style, bold=True))
        story.append(Table(data, colWidths=[5.4 * cm, 11.6 * cm], style=table_style))

    footer_label = LABELS_MR["Source"] if lang == "mr" else "Source"
    footer_style = ParagraphStyle(
        "footer", fontName=base_font, fontSize=8, leading=12,
        spaceBefore=12, textColor=colors.HexColor("#777777"))
    story.append(para("%s: mahatenders.gov.in" % footer_label, footer_style))

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title="MJP Tender %s" % row["tender_id"])
    doc.build(story)
    return out_path


def verify_devanagari(pdf_path):
    """Sanity check: the Marathi PDF must actually contain Devanagari text.
    Extracts text with pypdf and asserts at least one codepoint in the
    U+0900 to U+097F block is present."""
    from pypdf import PdfReader
    text = "".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    count = sum(1 for ch in text if "ऀ" <= ch <= "ॿ")
    if count == 0:
        raise RuntimeError("No Devanagari codepoints found in " + str(pdf_path))
    log.info("Devanagari check passed for %s (%d chars)", pdf_path, count)
    return count


# ---------------------------------------------------------------------------
# WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------------------

def whatsapp_configured():
    return all(os.environ.get(k) for k in
               ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_TO"))


def whatsapp_config():
    token = os.environ.get("WHATSAPP_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_ID")
    to = os.environ.get("WHATSAPP_TO")
    if not all([token, phone_id, to]):
        raise RuntimeError(
            "WHATSAPP_TOKEN, WHATSAPP_PHONE_ID and WHATSAPP_TO must be set")
    return token, phone_id, to


def wa_upload_media(pdf_path):
    token, phone_id, _ = whatsapp_config()
    url = "%s/%s/media" % (GRAPH_URL, phone_id)
    with open(pdf_path, "rb") as fh:
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer " + token},
            data={"messaging_product": "whatsapp", "type": "application/pdf"},
            files={"file": (Path(pdf_path).name, fh, "application/pdf")},
            timeout=HTTP_TIMEOUT,
        )
    if resp.status_code >= 300:
        raise RuntimeError("Media upload failed %s: %s" % (resp.status_code, resp.text[:400]))
    media_id = resp.json().get("id")
    if not media_id:
        raise RuntimeError("Media upload returned no id: " + resp.text[:400])
    return media_id


def wa_send_document(media_id, filename, caption):
    token, phone_id, to = whatsapp_config()
    url = "%s/%s/messages" % (GRAPH_URL, phone_id)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "document",
        "document": {"id": media_id, "filename": filename, "caption": caption},
    }
    resp = requests.post(
        url,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code >= 300:
        raise RuntimeError("Message send failed %s: %s" % (resp.status_code, resp.text[:400]))
    log.info("Sent %s", filename)


CAPTION_LIMIT = 1024  # WhatsApp document caption hard limit


def clamp_caption(caption):
    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 3] + "..."
    return caption


def send_pdf(pdf_path, caption):
    media_id = wa_upload_media(pdf_path)
    wa_send_document(media_id, Path(pdf_path).name, clamp_caption(caption))


# ---------------------------------------------------------------------------
# Email (SMTP; the "for now" channel until WhatsApp secrets are configured).
# One message per tender with both PDFs attached.
# ---------------------------------------------------------------------------

def smtp_configured():
    return all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS"))


def send_email_tender(row, details, en_path, mr_path):
    import smtplib
    from email.message import EmailMessage

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO", user)

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    subject = "%s: %s (closing %s)" % (
        caption_header(row), row["title"][:120], row["closing"])
    msg["Subject"] = sanitize(subject)
    body = english_caption(row, details)
    if mr_path is not None:
        body += "\n\n" + marathi_caption(row)
    else:
        body += "\n\nMarathi PDF could not be generated for this tender."
    msg.set_content(body)
    for path in (en_path, mr_path):
        if path is None:
            continue
        with open(path, "rb") as fh:
            msg.add_attachment(
                fh.read(), maintype="application", subtype="pdf",
                filename=Path(path).name)
    with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    log.info("Emailed %s to %s", row["tender_id"], to)


def caption_header(row):
    source = row.get("source", "MJP WSSD")
    if source == "MJP WSSD":
        return "New MJP Tender"
    return "New Tender - " + source


def english_caption(row, details):
    lines = [
        caption_header(row),
        "Title: " + row["title"][:300],
        "Tender ID: " + row["tender_id"],
        "Ref No: " + row["ref_no"],
        "Closing Date: " + row["closing"],
    ]
    emd = details.get("EMD Amount in ₹")
    value = details.get("Tender Value in ₹")
    location = details.get("Location")
    if emd:
        lines.append("EMD: Rs. " + emd)
    if value:
        lines.append("Tender Value: Rs. " + value)
    if location:
        lines.append("Location: " + location)
    return sanitize("\n".join(lines))


def marathi_caption(row):
    title_mr = translate_text(row["title"])[:300]
    if row.get("source", "MJP WSSD") == "MJP WSSD":
        header = LABELS_MR["New MJP Tender"]
    else:
        header = LABELS_MR["New Tender"]
    return sanitize("%s\n%s: %s\n%s: %s" % (
        header,
        LABELS_MR["Title"], title_mr,
        LABELS_MR["Closing Date"], row["closing"]))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# State files live in the repo and are committed back by the workflow.
# When STATE_REMOTE_BASE is set (the hosted dashboard on Vercel), state is
# read from that URL base instead, so the dashboard always sees the
# freshest committed state without a redeploy.

LIVE_FILE = ROOT / "live.json"
STATE_REMOTE_BASE = os.environ.get("STATE_REMOTE_BASE", "").rstrip("/")
STATE_REMOTE_TTL = 180
_remote_state = {}


def _read_remote_json(name):
    ts, data = _remote_state.get(name, (0.0, None))
    if data is not None and time.time() - ts < STATE_REMOTE_TTL:
        return data
    try:
        r = requests.get("%s/%s" % (STATE_REMOTE_BASE, name), timeout=30)
        r.raise_for_status()
        data = r.json()
        _remote_state[name] = (time.time(), data)
        return data
    except Exception as exc:
        log.warning("Remote state %s unavailable: %s", name, exc)
        return data


def _load_state(path, default):
    if STATE_REMOTE_BASE:
        data = _read_remote_json(path.name)
        if data is not None:
            return data
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError) as exc:
            log.warning("State file %s unreadable: %s", path, exc)
    return default


def _save_state(path, data):
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def load_seen():
    return _load_state(SEEN_FILE, {})


def save_seen(seen):
    _save_state(SEEN_FILE, seen)


def load_live_snapshot():
    return _load_state(LIVE_FILE, {"generated": "", "rows": []})


def save_live(rows):
    _save_state(LIVE_FILE, {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "rows": rows,
    })


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

def build_both_pdfs(session, row):
    """Fetch details and build the English and Marathi PDFs. Returns
    (details, en_path, mr_path, mr_error). A Marathi failure is captured,
    not raised, so the English side always survives."""
    details = fetch_tender_details(session, row["url"])
    if details.get("Tender ID") != row["tender_id"]:
        raise RuntimeError(
            "Detail page for %s did not parse (got Tender ID %r), portal "
            "likely served an error page" % (
                row["tender_id"], details.get("Tender ID")))
    try:
        cache_tender_details(row["tender_id"], details)
    except OSError as exc:
        log.warning("Could not cache details for %s: %s", row["tender_id"], exc)
    OUT_DIR.mkdir(exist_ok=True)
    tid = row["tender_id"]
    en_path = OUT_DIR / ("MJP_%s_EN.pdf" % tid)
    build_tender_pdf(details, row, "en", en_path)
    mr_path = OUT_DIR / ("MJP_%s_MR.pdf" % tid)
    mr_error = None
    try:
        build_tender_pdf(details, row, "mr", mr_path)
        verify_devanagari(mr_path)
    except Exception as exc:
        mr_error = exc
        mr_path = None
        log.error("Marathi PDF pipeline failed for %s: %s", tid, exc)
    return details, en_path, mr_path, mr_error


def process_new_tender(session, row, seen):
    """Delivers one new tender over every configured channel (email and/or
    WhatsApp). Returns True if the Marathi side fully succeeded. Raises if
    no channel delivered the English content (the tender then stays unseen
    and is retried next run)."""
    tid = row["tender_id"]
    log.info("Processing new tender %s [%s]: %s",
             tid, row.get("source", "?"), row["title"][:80])
    details, en_path, mr_path, _ = build_both_pdfs(session, row)

    delivered = False
    mr_ok = mr_path is not None

    if smtp_configured():
        try:
            send_email_tender(row, details, en_path, mr_path)
            delivered = True
        except Exception as exc:
            log.error("Email send failed for %s: %s", tid, exc)

    if whatsapp_configured():
        try:
            send_pdf(en_path, english_caption(row, details))
            delivered = True
            if mr_path is not None:
                time.sleep(SEND_SLEEP_SECONDS)
                try:
                    send_pdf(mr_path, marathi_caption(row))
                except Exception as exc:
                    mr_ok = False
                    log.error("Marathi send failed for %s: %s", tid, exc)
        except Exception as exc:
            log.error("WhatsApp send failed for %s: %s", tid, exc)

    if not delivered:
        raise RuntimeError("no delivery channel succeeded for " + tid)

    seen[tid] = {
        "title": row["title"],
        "ref_no": row["ref_no"],
        "closing": row["closing"],
        "opening": row.get("opening", ""),
        "published": row.get("published", ""),
        "org_chain": row.get("org_chain", ""),
        "source": row.get("source", "MJP WSSD"),
        "sources": row.get("sources") or [row.get("source", "MJP WSSD")],
        "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_seen(seen)
    return mr_ok


def run_dry(session, rows):
    row = rows[0]
    log.info("Dry run on tender %s: %s", row["tender_id"], row["title"][:80])
    details, en_path, mr_path, mr_error = build_both_pdfs(session, row)
    print("Parsed %d detail fields" % len(details))
    print("English PDF: %s" % en_path)
    if mr_path is not None:
        print("Marathi PDF: %s" % mr_path)
        print("English caption:\n%s" % english_caption(row, details))
        print("Marathi caption:\n%s" % marathi_caption(row))
    else:
        print("Marathi PDF FAILED: %s" % mr_error)
        return 1
    return 0


def cache_missing_details(session, rows):
    """Fetch and cache detail fields for any live tender not already cached,
    so the dashboard has city and value for everything even when no delivery
    channel is configured. Failures are skipped, not fatal."""
    cache = load_details_cache()
    todo = [r for r in rows if r["tender_id"] not in cache]
    if not todo:
        return
    log.info("Caching details for %d tender(s)", len(todo))
    for r in todo:
        try:
            details = fetch_tender_details(session, r["url"])
            if details.get("Tender ID") == r["tender_id"]:
                cache_tender_details(r["tender_id"], details)
        except Exception as exc:
            log.warning("Detail cache for %s failed: %s", r["tender_id"], exc)
        time.sleep(0.4)


def run_real(session):
    seen = load_seen()
    rows = fetch_all_watch_rows(session)
    save_live(rows)
    cache_missing_details(session, rows)
    new_rows = [r for r in rows if r["tender_id"] not in seen]
    log.info("%d live tenders across watches, %d new", len(rows), len(new_rows))
    if not (smtp_configured() or whatsapp_configured()):
        log.warning(
            "No delivery channel configured (SMTP_USER/SMTP_PASS or the "
            "WHATSAPP_* secrets). live.json refreshed; %d new tender(s) "
            "stay pending until a channel is set up.", len(new_rows))
        return 0
    if not new_rows:
        return 0
    if len(new_rows) > MAX_SENDS_PER_RUN:
        log.warning("Capping this run at %d of %d new tenders, the rest "
                    "go out next run", MAX_SENDS_PER_RUN, len(new_rows))
        new_rows = new_rows[:MAX_SENDS_PER_RUN]
    failures = 0
    mr_failures = 0
    for i, row in enumerate(new_rows):
        try:
            if not process_new_tender(session, row, seen):
                mr_failures += 1
        except Exception as exc:
            failures += 1
            log.error("Tender %s failed, will retry next run: %s",
                      row["tender_id"], exc)
        if i < len(new_rows) - 1:
            time.sleep(SEND_SLEEP_SECONDS)
    if mr_failures:
        log.warning("%d tender(s) went out in English only (Marathi side "
                    "failed and will not be retried)", mr_failures)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Local dashboard (python tracker.py --serve)
# ---------------------------------------------------------------------------

DASHBOARD_CACHE_SECONDS = 900  # re-scrape the portal at most every 15 minutes
DETAILS_CACHE_FILE = ROOT / "details_cache.json"

_dash_lock = threading.Lock()
_dash = {"ts": 0.0, "live": [], "session": None}


_mem_details = {}  # overlay for read-only deployments (Vercel)


def load_details_cache():
    base = _load_state(DETAILS_CACHE_FILE, {})
    if _mem_details:
        base = dict(base, **_mem_details)
    return base


def cache_tender_details(tid, details):
    if STATE_REMOTE_BASE:
        _mem_details[tid] = details
        return
    try:
        cache = _load_state(DETAILS_CACHE_FILE, {})
        cache[tid] = details
        _save_state(DETAILS_CACHE_FILE, cache)
    except OSError as exc:
        log.warning("Could not persist details for %s: %s", tid, exc)
        _mem_details[tid] = details


def parse_portal_datetime(text):
    try:
        return datetime.strptime(text.strip(), "%d-%b-%Y %I:%M %p")
    except (ValueError, AttributeError):
        return None


def tender_status(closing_text, is_live, now):
    """(css_class, label) for one tender row."""
    dt = parse_portal_datetime(closing_text)
    if not is_live:
        if dt and dt < now:
            return "closed", "Closed"
        return "gone", "No longer listed"
    if dt is None:
        return "open", "Live"
    days = (dt - now).total_seconds() / 86400.0
    if days < 0:
        return "closed", "Deadline passed"
    if days < 1:
        return "urgent", "Closes today"
    if days < 3:
        return "soon", "%dd left" % max(int(days), 1)
    return "open", "%dd left" % int(days)


def refresh_live(force=False):
    """Live rows for the dashboard, at most every DASHBOARD_CACHE_SECONDS
    unless forced. Org watches are scraped fresh; keyword watch rows come
    from the live.json snapshot the scheduled runs refresh, since the full
    all-organisations scan is too slow for a page load."""
    with _dash_lock:
        if not force and _dash["live"] and \
                time.time() - _dash["ts"] < DASHBOARD_CACHE_SECONDS:
            return _dash["live"]
        session = make_session()
        live = fetch_all_watch_rows(session, include_keyword_scan=False)
        by_tid = {r["tender_id"]: r for r in live}
        kw_names = keyword_watch_names()
        for row in load_live_snapshot().get("rows", []):
            srcs = row.get("sources") or ([row["source"]] if row.get("source") else [])
            if not any(s in kw_names for s in srcs):
                continue
            existing = by_tid.get(row["tender_id"])
            if existing is None:
                live.append(row)
                by_tid[row["tender_id"]] = row
            else:
                # merge keyword-watch tags onto the freshly scraped row
                merged = existing.setdefault("sources", [existing.get("source", "")])
                for s in srcs:
                    if s in kw_names and s not in merged:
                        merged.append(s)
        _dash["live"] = live
        _dash["session"] = session
        _dash["ts"] = time.time()
        return _dash["live"]


def fetch_detail_for_tid(tid):
    """Full detail dict for a tender: from the local cache first, else
    fetched live (and cached). None when unavailable."""
    cache = load_details_cache()
    if tid in cache:
        return cache[tid]
    live = refresh_live()
    row = next((r for r in live if r["tender_id"] == tid), None)
    if row is None:
        return None
    try:
        details = fetch_tender_details(_dash["session"], row["url"])
        if details.get("Tender ID") != tid:
            raise RuntimeError("stale detail link")
    except Exception:
        refresh_live(force=True)
        row = next((r for r in _dash["live"] if r["tender_id"] == tid), None)
        if row is None:
            return None
        details = fetch_tender_details(_dash["session"], row["url"])
        if details.get("Tender ID") != tid:
            return None
    cache_tender_details(tid, details)
    return details


def sections_spec():
    """SECTIONS for the client: [{name, fields: [[portal_label,
    display_label], ...]}]. The client looks values up by portal label in a
    raw detail dict and shows the display label."""
    return [{"name": name,
             "fields": [[label, label.replace("in ₹", "in Rs.")]
                        for label in fields]}
            for name, fields in SECTIONS]


def publisher(org_chain):
    parts = [p.strip() for p in (org_chain or "").split("||") if p.strip()]
    return parts[-1] if parts else ""


MAHA_DISTRICTS = [
    "Chhatrapati Sambhajinagar", "Ahmednagar", "Akola", "Amravati",
    "Aurangabad", "Beed", "Bhandara", "Buldhana", "Chandrapur", "Dhule",
    "Gadchiroli", "Gondia", "Hingoli", "Jalgaon", "Jalna", "Kolhapur",
    "Latur", "Mumbai", "Nagpur", "Nanded", "Nandurbar", "Nashik",
    "Osmanabad", "Palghar", "Parbhani", "Pune", "Raigad", "Ratnagiri",
    "Sangli", "Satara", "Sindhudurg", "Solapur", "Thane", "Wardha",
    "Washim", "Yavatmal",
]

# Talukas of Jalgaon district. Any location in one of these rolls up to the
# "Jalgaon" district group so one filter shows every Jalgaon taluka. The
# city proper is shown as "Jalgaon City" (still inside the Jalgaon group).
JALGAON_TALUKAS = {
    "amalner", "bhadgaon", "bhusawal", "bhusaval", "bodwad", "chalisgaon",
    "chopda", "dharangaon", "erandol", "jamner", "muktainagar", "edlabad",
    "pachora", "parola", "raver", "yawal",
}
# Talukas that share a name with a district elsewhere are not auto-mapped
# to Jalgaon (there are none here today, but keep the set explicit).

JALGAON_DISTRICT = "Jalgaon"

# Common terse spellings the portal uses in Location fields.
CITY_ABBREV = {"rvr": "raver", "bsl": "bhusawal", "amn": "amalner",
               "jal": "jalgaon", "csn": "chhatrapati sambhajinagar"}


def _tokens(text):
    return set(re.findall(r"[a-z]+", text.casefold()))


def _is_district(city):
    return city in MAHA_DISTRICTS or city == "Jalgaon City"


def normalize_city(location):
    """Collapse the portal's free-text Location into a district-level group
    for the filter, so selecting a district shows all of its talukas.

    'At Lasur Tal chopda Dist Jalgaon' -> 'Jalgaon' (Chopda is a Jalgaon
    taluka). The Jalgaon city proper -> 'Jalgaon City'. Everything not
    resolvable to a district keeps a cleaned short name."""
    s = (location or "").strip()
    if not s:
        return ""
    raw = re.findall(r"[a-z]+", s.casefold())
    exp = [CITY_ABBREV.get(t, t) for t in raw]
    low = " ".join(exp)
    toks = set(exp)

    # An explicit "Dist <name>" wins over any incidental town name.
    m = re.search(r"dist(?:rict)?[\s.:,()-]*([a-z]+)", low)
    named_dist = m.group(1) if m else ""

    jalgaon = (named_dist == "jalgaon"
               or bool(toks & JALGAON_TALUKAS)
               or ("jalgaon" in toks and not named_dist))
    if named_dist and named_dist != "jalgaon":
        jalgaon = False  # an explicit other district wins
    if jalgaon:
        other_taluka = toks & JALGAON_TALUKAS
        if ("jalgaon" in toks and not other_taluka
                and not re.search(r"\bta(?:l|luka)?\b", low)):
            return "Jalgaon City"
        return JALGAON_DISTRICT

    if re.search(r"\b(csn|chh)\b", low):
        return "Chhatrapati Sambhajinagar"
    # Match a district by its last word as a whole token (no substrings,
    # so 'limbejalgaon' does not read as Jalgaon).
    for d in sorted(MAHA_DISTRICTS, key=len, reverse=True):
        if d.split()[-1].casefold() in toks:
            return d
    if named_dist:
        for d in MAHA_DISTRICTS:
            if d.casefold().startswith(named_dist):
                return d
    s = re.sub(r"^(?:at|a/p)[\s.]+", "", s, flags=re.I).strip(" .,")
    return s[:30].strip().title()


def city_group(city):
    """The district a normalized city belongs to, for filter grouping.
    'Jalgaon City' groups under 'Jalgaon'."""
    if city == "Jalgaon City":
        return JALGAON_DISTRICT
    return city


def derive_city(location, org_chain, title):
    """Best city for a tender: the Location field if it resolves to a known
    district, otherwise the publisher name or organisation chain (which
    usually names the town, e.g. 'Municipal Council Raver'), then title."""
    first = normalize_city(location)
    if _is_district(first):
        return first
    for extra in (publisher(org_chain), org_chain, title):
        cand = normalize_city(extra)
        if _is_district(cand):
            return cand
    return first or normalize_city(publisher(org_chain))


def parse_inr(value_text):
    digits = re.sub(r"[^\d]", "", value_text or "")
    return int(digits) if digits else 0


def format_inr(n):
    """1,51,00,067 -> '1.51 Cr'; 45,00,000 -> '45 L'; 59,000 -> '59,000'."""
    if not n:
        return ""
    if n >= 10**7:
        s = "%.2f" % (n / 10**7)
        return s.rstrip("0").rstrip(".") + " Cr"
    if n >= 10**5:
        s = "%.2f" % (n / 10**5)
        return s.rstrip("0").rstrip(".") + " L"
    return "{:,}".format(n)


def portal_ts(text):
    dt = parse_portal_datetime(text)
    return int(dt.timestamp()) if dt else 0


def dashboard_data():
    now = datetime.now()
    live = _dash["live"]
    seen = load_seen()
    details_all = load_details_cache()
    live_map = {r["tender_id"]: r for r in live}
    kw_names = keyword_watch_names()

    awards = load_awards()
    tenders = []
    for tid in set(live_map) | set(seen) | set(awards):
        r = live_map.get(tid)
        e = seen.get(tid, {})
        d = details_all.get(tid) or awards.get(tid, {}).get("fields", {})
        closing = (r or e).get("closing") or d.get("Bid Submission End Date", "")
        published = (r.get("published", "") if r else e.get("published", "")) \
            or d.get("Published Date", "")
        awarded = tid in awards
        if r is None and tid not in seen:
            cls, label = "awarded", "Awarded"
        else:
            cls, label = tender_status(closing, r is not None, now)
        value = parse_inr(d.get("Tender Value in ₹", ""))
        org_chain = (r or e).get("org_chain", "")
        city = derive_city(d.get("Location"), org_chain,
                           (r or e).get("title", ""))
        tenders.append({
            "id": tid,
            "title": (r or e).get("title") or d.get("Title", ""),
            "ref": (r or e).get("ref_no") or
                   d.get("Tender Reference Number", ""),
            "source": (r["source"] if r else e.get("source", "")) or
                      ("Results import" if awarded else ""),
            "sources": (r.get("sources") if r else e.get("sources"))
                       or ([e.get("source")] if e.get("source") else [])
                       or (["Results import"] if awarded else []),
            "org": publisher((r or e).get("org_chain", "")) or
                   awards.get(tid, {}).get("org", ""),
            "awarded": awarded,
            "city": city,
            "cityGroup": city_group(city),
            "value": value,
            "valueFmt": format_inr(value),
            "published": published,
            "publishedTs": portal_ts(published),
            "closing": closing,
            "closingTs": portal_ts(closing),
            "opening": (r.get("opening", "") if r else e.get("opening", ""))
                       or d.get("Bid Opening Date", ""),
            "openingTs": portal_ts(
                (r.get("opening", "") if r else e.get("opening", ""))
                or d.get("Bid Opening Date", "")),
            "first_seen": e.get("first_seen", ""),
            "live": r is not None,
            "detail": tid in details_all or r is not None,
            "st": cls,
            "stLabel": label,
        })

    live_part = sorted([t for t in tenders if t["live"]],
                       key=lambda t: parse_portal_datetime(t["closing"]) or datetime.max)
    gone_part = sorted([t for t in tenders if not t["live"]],
                       key=lambda t: parse_portal_datetime(t["closing"]) or datetime.min,
                       reverse=True)
    tenders = live_part + gone_part

    soon = sum(1 for t in live_part if t["st"] in ("soon", "urgent"))
    kw_count = sum(1 for t in tenders if "Amdar Nidhi / DPDC" in t["sources"])
    sources = sorted({s for t in tenders for s in t["sources"]})
    cities = sorted({t["cityGroup"] for t in tenders if t["cityGroup"]},
                    key=str.casefold)
    return {
        "generated": now.strftime("%d-%b-%Y %I:%M %p"),
        "refreshSeconds": DASHBOARD_CACHE_SECONDS,
        "stats": {"live": len(live_part), "soon": soon,
                  "keyword": kw_count, "total": len(seen)},
        "sources": sources,
        "cities": cities,
        "tenders": tenders,
    }


# ---------------------------------------------------------------------------
# Results of Tenders import (human in the loop captcha unlock)
#
# The portal's Results of Tenders section is captcha protected. The captcha
# is never solved automatically: /unlock shows the portal's own captcha
# image to the person, they type it, and the crawl then runs inside that
# human-authorized session. Award data is stored in awards.json.
# ---------------------------------------------------------------------------

AWARDS_FILE = ROOT / "awards.json"
RESULTS_URL = BASE_URL + "/nicgep/app?page=ResultOfTenders&service=page"
RESULTS_RAW_DIR = Path(os.environ.get("OUT_DIR") or ROOT) / "results_raw"
_mem_awards = {}
_unlock = {"session": None, "fields": None}


def load_awards():
    base = _load_state(AWARDS_FILE, {})
    if _mem_awards:
        base = dict(base, **_mem_awards)
    return base


def save_awards(awards):
    if STATE_REMOTE_BASE:
        _mem_awards.update(awards)
        return
    try:
        _save_state(AWARDS_FILE, awards)
    except OSError as exc:
        log.warning("Could not persist awards: %s", exc)
        _mem_awards.update(awards)


def _dump_page(name, html):
    try:
        RESULTS_RAW_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
        with open(RESULTS_RAW_DIR / (safe + ".html"), "w",
                  encoding="utf-8") as fh:
            fh.write(html)
    except OSError:
        pass


def unlock_form_state():
    """Fresh portal session + the Results search form fields and captcha
    image (inline base64) for the person to solve."""
    session = make_session()
    r = portal_get(session, RESULTS_URL)
    soup = BeautifulSoup(r.text, "lxml")
    form = None
    for f in soup.find_all("form"):
        if f.find("input", {"name": "captchaText"}) is not None:
            form = f
            break
    if form is None:
        raise RuntimeError("Results page did not offer the expected form")
    fields = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value") or ""
    img = soup.find("img", {"id": "captchaImage"})
    captcha_src = img.get("src") if img else ""
    _unlock["session"] = session
    _unlock["fields"] = fields
    return captcha_src


UNLOCK_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Import Tender Results</title>
<style>
body {{ font-family: "Fira Sans", -apple-system, sans-serif; background:
 #F8FAFC; color: #0F172A; margin: 0; }}
main {{ max-width: 560px; margin: 40px auto; background: #fff; border:
 1px solid #DBEAFE; border-radius: 12px; padding: 26px 30px; }}
h1 {{ font-size: 17px; color: #1E3A8A; margin: 0 0 6px; }}
p {{ font-size: 13px; line-height: 1.55; color: #475569; }}
img.cap {{ border: 1px solid #DBEAFE; border-radius: 8px; margin: 10px 0;
 display: block; }}
input[type=text] {{ font: inherit; padding: 9px 12px; border: 1px solid
 #DBEAFE; border-radius: 8px; width: 220px; }}
button {{ font: inherit; background: #1E40AF; color: #fff; border: none;
 border-radius: 8px; padding: 10px 18px; cursor: pointer; margin-left: 8px; }}
button:hover {{ background: #17346C; }}
.err {{ background: #FEE2E2; color: #B91C1C; padding: 9px 12px;
 border-radius: 8px; font-size: 13px; }}
.ok {{ background: #DCFCE7; color: #15803D; padding: 9px 12px;
 border-radius: 8px; font-size: 13px; }}
a {{ color: #1E40AF; }}
</style></head><body><main>
<h1>Import Results of Tenders</h1>
<p>The portal protects its results section with a captcha. Type the code
below exactly as shown; the import then crawls award information for the
watched and Jalgaon related organisations in this one authorized session.
This can take a few minutes.</p>
{error}
<form method="post" action="/unlock">
 <img class="cap" src="{captcha}" alt="Portal captcha image">
 <input type="text" name="captcha" autofocus autocomplete="off"
  aria-label="Captcha code" placeholder="Captcha code">
 <button type="submit">Unlock and import</button>
</form>
<p><a href="/">Back to dashboard</a></p>
</main></body></html>"""


def unlock_page_html(error=""):
    captcha = unlock_form_state()
    err = '<p class="err">%s</p>' % xml_escape(error) if error else ""
    return UNLOCK_PAGE.format(error=err, captcha=xml_escape(captcha))


def _org_of_interest(name):
    low = name.casefold()
    if "jalgaon" in low or name == ORG_NAME:
        return True
    return any(org_matches(name, w) for w in ORG_WATCHES)


def _extract_pairs(soup):
    pairs = {}
    pending = None
    for td in soup.find_all("td"):
        cls = td.get("class") or []
        if "td_caption" in cls:
            pending = td.get_text(" ", strip=True)
        elif "td_field" in cls and pending is not None:
            pairs.setdefault(pending, td.get_text(" ", strip=True))
            pending = None
    return pairs


def crawl_results(session, root_html):
    """Walk the unlocked results listing: org drill-down pages for the
    organisations of interest, then every tender link on them, collecting
    all caption/field pairs. Every fetched page is dumped raw so parsing
    can be improved later without another captcha."""
    _dump_page("results_root", root_html)
    soup = BeautifulSoup(root_html, "lxml")
    org_pages = []
    org_rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=True)
        if len(tds) >= 3 and a and tds[0].get_text(strip=True).isdigit():
            org_rows.append((tds[1].get_text(strip=True), BASE_URL + a["href"]))
    if org_rows:
        picked = [(n, u) for n, u in org_rows if _org_of_interest(n)][:40]
        log.info("Results listing: %d orgs, crawling %d of interest",
                 len(org_rows), len(picked))
        for name, url in picked:
            try:
                html = portal_get(session, url).text
                _dump_page("org_" + name, html)
                org_pages.append((name, html))
            except Exception as exc:
                log.error("Results org %s failed: %s", name, exc)
            time.sleep(0.4)
    else:
        org_pages = [("root", root_html)]

    awards = load_awards()
    fetched = 0
    for org_name, html in org_pages:
        psoup = BeautifulSoup(html, "lxml")
        for a in psoup.find_all("a", href=True):
            if "DirectLink" not in a["href"] or fetched >= 200:
                continue
            text = a.get_text(" ", strip=True)
            if not text or text.isdigit():
                continue
            try:
                page = portal_get(session, BASE_URL + a["href"]).text
            except Exception:
                continue
            fetched += 1
            time.sleep(0.4)
            dsoup = BeautifulSoup(page, "lxml")
            pairs = _extract_pairs(dsoup)
            m = TENDER_ID_RE.search(" ".join(
                [pairs.get("Tender ID", "")] +
                re.findall(r"\d{4}_\w+_\d+_\d+", page)[:1]))
            tid = pairs.get("Tender ID") or (m.group(0) if m else "")
            if not TENDER_ID_RE.match(tid or ""):
                _dump_page("unparsed_%d" % fetched, page)
                continue
            _dump_page("tender_" + tid, page)
            entry = awards.get(tid, {"fields": {}})
            entry["fields"].update(pairs)
            entry["org"] = org_name if org_name != "root" else \
                entry.get("org", "")
            entry["fetched"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            awards[tid] = entry
    save_awards(awards)
    return awards, fetched


def unlock_submit(captcha_text):
    """Submit the person's captcha answer and, on success, crawl."""
    session = _unlock.get("session")
    fields = _unlock.get("fields")
    if session is None or fields is None:
        return None, "The unlock session expired, try again."
    data = dict(fields)
    data["captchaText"] = captcha_text.strip()
    data["submitname"] = "Search"
    resp = session.post(BASE_URL + "/nicgep/app", data=data,
                        timeout=HTTP_TIMEOUT, verify=session.verify)
    html = resp.text
    low = html.casefold()
    if "invalid captcha" in low or "captcha entered" in low or (
            "captchatext" in low and "DirectLink" not in html):
        return None, "The portal rejected that captcha code, try the new one."
    awards, fetched = crawl_results(session, html)
    return (awards, fetched), None


UNLOCK_RESULT_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Results imported</title>
<style>body {{ font-family: "Fira Sans", sans-serif; background: #F8FAFC;
 margin: 0; }} main {{ max-width: 560px; margin: 40px auto; background:
 #fff; border: 1px solid #DBEAFE; border-radius: 12px; padding: 26px 30px;
 font-size: 14px; color: #0F172A; }} a {{ color: #1E40AF; }}</style>
</head><body><main>
<p><strong>Import finished.</strong></p>
<p>{fetched} result pages read in the unlocked session; award records now
exist for {count} tenders. Raw pages were saved for deeper parsing.</p>
<p><a href="/">Back to the dashboard</a> (award details show inside each
tender's popup).</p>
</main></body></html>"""


DASHBOARD_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tender Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@450;600&family=Fira+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --primary: #1E40AF; --primary-dark: #17346C; --on-primary: #FFFFFF;
  --accent: #D97706; --bg: #F8FAFC; --card: #FFFFFF; --border: #DBEAFE;
  --line: #E5EAF3; --fg: #0F172A; --heading: #1E3A8A;
  --muted: #E9EEF6; --muted-fg: #475569; --ring: #1E40AF;
  --ok: #15803D; --ok-bg: #DCFCE7; --warn: #92400E; --warn-bg: #FEF3C7;
  --bad: #B91C1C; --bad-bg: #FEE2E2; --off: #475569; --off-bg: #E2E8F0;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 400 14px/1.5 "Fira Sans", -apple-system, "Segoe UI", sans-serif; }
.mono { font-family: "Fira Code", ui-monospace, Menlo, monospace;
  font-variant-numeric: tabular-nums; }
button, select, input { font: inherit; }
:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

header { background: var(--primary); color: var(--on-primary);
  padding: 14px 22px; display: flex; flex-wrap: wrap; gap: 12px;
  align-items: center; justify-content: space-between; }
header h1 { margin: 0; font-size: 17px; font-weight: 600; letter-spacing: .2px; }
header .sub { font-size: 12px; opacity: .85; margin-top: 2px; }
#refresh { display: inline-flex; align-items: center; gap: 7px;
  background: rgba(255,255,255,.12); color: #fff; border: 1px solid
  rgba(255,255,255,.45); border-radius: 8px; padding: 9px 14px;
  min-height: 40px; cursor: pointer; transition: background .2s; }
#refresh:hover { background: rgba(255,255,255,.24); }
#refresh svg { width: 15px; height: 15px; }

main { max-width: 1280px; margin: 0 auto; padding: 18px 22px 80px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px,1fr));
  gap: 10px; margin-bottom: 14px; }
.tile { background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px; }
.tile .v { font-family: "Fira Code", monospace; font-size: 24px;
  font-weight: 600; color: var(--heading); }
.tile .l { font-size: 12px; color: var(--muted-fg); margin-top: 2px; }

.bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-bottom: 10px; }
.search { position: relative; flex: 1 1 260px; max-width: 420px; }
.search svg { position: absolute; left: 10px; top: 50%;
  transform: translateY(-50%); width: 15px; height: 15px; color: var(--muted-fg); }
.search input { width: 100%; padding: 9px 12px 9px 32px; min-height: 40px;
  border: 1px solid var(--border); border-radius: 8px; background: var(--card); }
.bar select { padding: 9px 10px; min-height: 40px; border: 1px solid
  var(--border); border-radius: 8px; background: var(--card);
  color: var(--fg); cursor: pointer; }
.bar .count { font-size: 12px; color: var(--muted-fg); margin-left: auto; }

.tablewrap { background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; overflow: auto; max-height: calc(100vh - 320px); }
table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 1260px; }
thead th { position: sticky; top: 0; z-index: 1; background: #EFF4FB;
  text-align: left; font-weight: 600; color: var(--heading);
  padding: 9px 12px; border-bottom: 1px solid var(--border);
  white-space: nowrap; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { background: #E3EBF7; }
th.sortable::after { content: "\2195"; opacity: .35; margin-left: 5px;
  font-size: 11px; }
th.sortable.asc::after { content: "\2191"; opacity: 1; }
th.sortable.desc::after { content: "\2193"; opacity: 1; }
tbody td { padding: 9px 12px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
tbody tr { cursor: pointer; transition: background .15s; }
tbody tr:hover { background: #F1F5FF; }
tbody tr.gone-row td { color: var(--muted-fg); }
td.tid { font-family: "Fira Code", monospace; font-size: 12px;
  white-space: nowrap; }
td.nowrap, th.nowrap { white-space: nowrap; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 11px; font-weight: 500; white-space: nowrap; }
.badge.open { background: var(--ok-bg); color: var(--ok); }
.badge.soon { background: var(--warn-bg); color: var(--warn); }
.badge.urgent, .badge.closed { background: var(--bad-bg); color: var(--bad); }
.badge.gone { background: var(--off-bg); color: var(--off); }
.badge.awarded { background: #DBEAFE; color: #1E40AF; }
.src { font-size: 11px; color: var(--muted-fg); white-space: nowrap; }
.pub { font-size: 11px; color: var(--muted-fg); min-width: 140px; }
.more { display: inline-block; background: var(--muted); color: var(--muted-fg);
  border-radius: 6px; padding: 0 5px; font-size: 10px; cursor: help; }
td.val { font-family: "Fira Code", monospace; font-size: 12px;
  text-align: right; white-space: nowrap; }
td.city { font-size: 12px; white-space: nowrap; }
.empty { padding: 26px; text-align: center; color: var(--muted-fg); }

#overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45);
  opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 40; }
#overlay.show { opacity: 1; pointer-events: auto; }
#panel { position: fixed; top: 0; right: 0; bottom: 0;
  width: min(680px, 94vw); background: var(--card); z-index: 50;
  transform: translateX(102%); transition: transform .22s ease;
  display: flex; flex-direction: column;
  box-shadow: -12px 0 32px rgba(15,23,42,.18); }
#panel.show { transform: translateX(0); }
#panel .phead { padding: 16px 20px 12px; border-bottom: 1px solid var(--line);
  display: flex; gap: 12px; align-items: flex-start; }
#panel .phead h2 { margin: 0 0 4px; font-size: 15px; line-height: 1.4;
  color: var(--heading); }
#panel .phead .pid { font-family: "Fira Code", monospace; font-size: 12px;
  color: var(--muted-fg); }
#pclose { margin-left: auto; flex: none; width: 40px; height: 40px;
  display: grid; place-items: center; background: none; border: 1px solid
  var(--border); border-radius: 8px; cursor: pointer; color: var(--muted-fg);
  transition: background .15s; }
#pclose:hover { background: var(--muted); }
#pbody { overflow-y: auto; padding: 6px 20px 20px; flex: 1; }
#pbody h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .7px;
  color: var(--muted-fg); margin: 20px 0 6px; }
.kv { width: 100%; border-collapse: collapse; font-size: 13px; }
.kv td { padding: 6px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
.kv td:first-child { width: 38%; color: var(--muted-fg); }
.kv tr:last-child td { border-bottom: none; }
.pfoot { padding: 12px 20px; border-top: 1px solid var(--line);
  display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.pfoot a { display: inline-flex; align-items: center; gap: 7px;
  padding: 9px 14px; min-height: 40px; border-radius: 8px;
  text-decoration: none; font-weight: 500; font-size: 13px;
  transition: background .15s; }
.pfoot a.en { background: var(--primary); color: var(--on-primary); }
.pfoot a.en:hover { background: var(--primary-dark); }
.pfoot a.mr { border: 1px solid var(--accent); color: var(--accent); }
.pfoot a.mr:hover { background: #FEF7EC; }
.pfoot .note { font-size: 11px; color: var(--muted-fg); }
.spin { margin: 40px auto; width: 26px; height: 26px; border-radius: 50%;
  border: 3px solid var(--muted); border-top-color: var(--primary);
  animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.perr { margin: 30px 10px; color: var(--muted-fg); text-align: center; }
@media (prefers-reduced-motion: reduce) {
  #panel, #overlay, tbody tr, #refresh { transition: none; }
  .spin { animation-duration: 1.6s; }
}
</style></head>
<body>
<header>
 <div>
  <h1>Tender Watch</h1>
  <div class="sub">MJP statewide · ZP Jalgaon DPDC · Collector Jalgaon · Amdar Nidhi / DPDC scan · all Jalgaon tenders statewide
   &nbsp;|&nbsp; updated <span id="stamp"></span> · auto refresh every 15 min</div>
 </div>
 <div style="display:flex;gap:8px;align-items:center">
 <a id="unlock-link" href="/unlock" style="color:#fff;font-size:12px;
  border:1px solid rgba(255,255,255,.45);border-radius:8px;
  padding:9px 14px;text-decoration:none">Import results</a>
 <button id="refresh" aria-label="Refresh data now">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/></svg>
  Refresh
 </button>
 </div>
</header>
<main>
 <div class="tiles">
  <div class="tile"><div class="v" id="s-live"></div><div class="l">Live tenders</div></div>
  <div class="tile"><div class="v" id="s-soon"></div><div class="l">Closing within 3 days</div></div>
  <div class="tile"><div class="v" id="s-kw"></div><div class="l">Amdar Nidhi / DPDC matches</div></div>
  <div class="tile"><div class="v" id="s-total"></div><div class="l">Tracked all time</div></div>
 </div>
 <div class="bar">
  <div class="search">
   <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
   <input id="q" type="search" placeholder="Search title, id, ref no..." aria-label="Search tenders">
  </div>
  <select id="f-src" aria-label="Filter by watch"><option value="">All watches</option></select>
  <select id="f-city" aria-label="Filter by city"><option value="">All cities</option></select>
  <select id="f-st" aria-label="Filter by status">
   <option value="">All statuses</option>
   <option value="live">Live</option>
   <option value="soonish">Closing within 3 days</option>
   <option value="past">Closed / delisted</option>
  </select>
  <span class="count" id="count"></span>
 </div>
 <div class="tablewrap">
  <table aria-label="Tenders">
   <thead><tr>
    <th class="nowrap sortable" data-key="id">Tender ID</th>
    <th class="sortable" data-key="title">Title</th>
    <th class="sortable" data-key="source">Watch</th>
    <th class="sortable" data-key="org">Publisher</th>
    <th class="sortable" data-key="city">City</th>
    <th class="nowrap sortable num" data-key="value">Value</th>
    <th class="nowrap sortable num" data-key="publishedTs">Published</th>
    <th class="nowrap sortable num" data-key="closingTs">Closing</th>
    <th class="nowrap sortable num" data-key="openingTs">Opening</th>
    <th class="nowrap sortable" data-key="stLabel">Status</th>
   </tr></thead>
   <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" hidden>No tenders match the current filters.</div>
 </div>
</main>

<div id="overlay"></div>
<aside id="panel" role="dialog" aria-modal="true" aria-labelledby="ptitle">
 <div class="phead">
  <div>
   <h2 id="ptitle"></h2>
   <div class="pid mono" id="ppid"></div>
  </div>
  <button id="pclose" aria-label="Close details">
   <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
  </button>
 </div>
 <div id="pbody"></div>
 <div class="pfoot">
  <a class="en" id="pdf-en" target="_blank" rel="noopener">English PDF</a>
  <a class="mr" id="pdf-mr" target="_blank" rel="noopener">Marathi PDF</a>
  <span class="note">Marathi PDF is generated on demand and can take up to a minute.</span>
 </div>
</aside>

<script>
var DATA = __DATA_JSON__;
var SECTIONS_SPEC = __SECTIONS_JSON__;
var els = {
  rows: document.getElementById('rows'), q: document.getElementById('q'),
  src: document.getElementById('f-src'), st: document.getElementById('f-st'),
  city: document.getElementById('f-city'),
  count: document.getElementById('count'), empty: document.getElementById('empty'),
  overlay: document.getElementById('overlay'), panel: document.getElementById('panel'),
  pbody: document.getElementById('pbody')
};
var sortState = { key: null, dir: 1 };
document.getElementById('stamp').textContent = DATA.generated;
document.getElementById('s-live').textContent = DATA.stats.live;
document.getElementById('s-soon').textContent = DATA.stats.soon;
document.getElementById('s-kw').textContent = DATA.stats.keyword;
document.getElementById('s-total').textContent = DATA.stats.total;
DATA.sources.forEach(function (s) {
  var o = document.createElement('option'); o.value = s; o.textContent = s;
  els.src.appendChild(o);
});
DATA.cities.forEach(function (c) {
  var o = document.createElement('option'); o.value = c; o.textContent = c;
  els.city.appendChild(o);
});
function esc(t) {
  return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
  });
}
function matches(t) {
  var q = els.q.value.trim().toLowerCase();
  if (q && (t.id + ' ' + t.title + ' ' + t.ref + ' ' + t.source + ' ' +
      t.org + ' ' + t.city).toLowerCase().indexOf(q) < 0) return false;
  if (els.src.value && (t.sources || []).indexOf(els.src.value) < 0) return false;
  if (els.city.value && t.cityGroup !== els.city.value) return false;
  var st = els.st.value;
  if (st === 'live' && !t.live) return false;
  if (st === 'soonish' && ['soon', 'urgent'].indexOf(t.st) < 0) return false;
  if (st === 'past' && t.live && t.st !== 'closed') return false;
  return true;
}
var NUM_KEYS = { value: 1, publishedTs: 1, closingTs: 1, openingTs: 1 };
function sorted() {
  var list = DATA.tenders.map(function (t, i) { t._i = i; return t; });
  var k = sortState.key;
  if (!k) return list;
  return list.slice().sort(function (a, b) {
    var r;
    if (NUM_KEYS[k]) {
      r = (a[k] || 0) - (b[k] || 0);
    } else {
      r = String(a[k] || '').toLowerCase()
        .localeCompare(String(b[k] || '').toLowerCase());
    }
    return r * sortState.dir;
  });
}
document.querySelectorAll('th.sortable').forEach(function (th) {
  th.setAttribute('tabindex', '0');
  function toggle() {
    var k = th.dataset.key;
    if (sortState.key === k) {
      sortState.dir = -sortState.dir;
    } else {
      sortState.key = k;
      sortState.dir = NUM_KEYS[k] && k !== 'closingTs' ? -1 : 1;
    }
    document.querySelectorAll('th.sortable').forEach(function (h) {
      h.classList.remove('asc', 'desc');
      h.removeAttribute('aria-sort');
    });
    th.classList.add(sortState.dir === 1 ? 'asc' : 'desc');
    th.setAttribute('aria-sort', sortState.dir === 1 ? 'ascending' : 'descending');
    render();
  }
  th.addEventListener('click', toggle);
  th.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
});
function render() {
  var shown = 0, html = '';
  sorted().forEach(function (t) {
    var i = t._i;
    if (!matches(t)) return;
    shown++;
    html += '<tr data-i="' + i + '" tabindex="0"' +
      (t.live ? '' : ' class="gone-row"') + '>' +
      '<td class="tid">' + esc(t.id) + '</td>' +
      '<td>' + esc(t.title) + '</td>' +
      '<td class="src">' + esc(t.source) +
        ((t.sources && t.sources.length > 1)
          ? ' <span class="more" title="' + esc(t.sources.join(", ")) + '">+' +
            (t.sources.length - 1) + '</span>' : '') + '</td>' +
      '<td class="pub">' + esc(t.org) + '</td>' +
      '<td class="city">' + esc(t.city) + '</td>' +
      '<td class="val">' + esc(t.valueFmt) + '</td>' +
      '<td class="nowrap">' + esc((t.published || '').split(' ')[0]) + '</td>' +
      '<td class="nowrap">' + esc(t.closing) + '</td>' +
      '<td class="nowrap">' + esc((t.opening || '').split(' ')[0]) + '</td>' +
      '<td><span class="badge ' + t.st + '">' + esc(t.stLabel) + '</span>' +
      (t.awarded && t.st !== 'awarded'
        ? ' <span class="badge awarded">Awarded</span>' : '') + '</td></tr>';
  });
  els.rows.innerHTML = html;
  els.empty.hidden = shown > 0;
  if (shown === 0) {
    var active = [];
    if (els.src.value) active.push('watch "' + els.src.value + '"');
    if (els.city.value) active.push('city "' + els.city.value + '"');
    if (els.st.value) active.push('status');
    if (els.q.value.trim()) active.push('search "' + els.q.value.trim() + '"');
    var msg = 'No tenders match ' +
      (active.length ? active.join(' and ') : 'the current filters') + '.';
    if (els.src.value && els.city.value) {
      var inCity = DATA.tenders.filter(function (t) {
        return t.cityGroup === els.city.value;
      }).reduce(function (set, t) {
        (t.sources || []).forEach(function (s) { set[s] = 1; }); return set;
      }, {});
      var names = Object.keys(inCity);
      if (names.length) msg += ' In ' + els.city.value +
        ' the available watches are: ' + names.join(', ') + '.';
    }
    els.empty.innerHTML = esc(msg) +
      ' <button id="clearf" style="margin-left:8px;padding:6px 12px;' +
      'border:1px solid var(--border);border-radius:8px;background:#fff;' +
      'cursor:pointer">Clear filters</button>';
    var cb = document.getElementById('clearf');
    if (cb) cb.addEventListener('click', function () {
      els.q.value = ''; els.src.value = ''; els.city.value = '';
      els.st.value = ''; render();
    });
  }
  els.count.textContent = shown + ' of ' + DATA.tenders.length + ' tenders';
}
['input', 'change'].forEach(function (ev) {
  [els.q, els.src, els.st, els.city].forEach(function (el) {
    el.addEventListener(ev, render);
  });
});
render();

function openPanel(t) {
  document.getElementById('ptitle').textContent = t.title;
  document.getElementById('ppid').textContent = t.id + (t.ref ? '  ·  ' + t.ref : '');
  document.getElementById('pdf-en').href = '/pdf/' + t.id + '/en';
  document.getElementById('pdf-mr').href = '/pdf/' + t.id + '/mr';
  els.pbody.innerHTML = '<div class="spin" role="status" aria-label="Loading"></div>';
  els.overlay.classList.add('show');
  els.panel.classList.add('show');
  fetch('/api/detail?id=' + encodeURIComponent(t.id))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) {
        els.pbody.innerHTML = '<p class="perr">' + esc(d.error ||
          'Full details are not available for this tender.') + '</p>';
        return;
      }
      var html = '';
      SECTIONS_SPEC.forEach(function (sec) {
        var rows = '';
        sec.fields.forEach(function (f) {
          var v = d.details[f[0]];
          if (v) rows += '<tr><td>' + esc(f[1]) + '</td><td>' + esc(v) + '</td></tr>';
        });
        if (rows) html += '<h3>' + esc(sec.name) + '</h3><table class="kv">' + rows + '</table>';
      });
      if (d.award && d.award.fields) {
        var arows = '';
        Object.keys(d.award.fields).forEach(function (k) {
          if (d.details[k]) return;
          arows += '<tr><td>' + esc(k) + '</td><td>' +
            esc(d.award.fields[k]) + '</td></tr>';
        });
        if (arows) html += '<h3>Result / Award</h3><table class="kv">' +
          arows + '</table>';
      }
      els.pbody.innerHTML = html || '<p class="perr">No fields parsed.</p>';
    })
    .catch(function () {
      els.pbody.innerHTML = '<p class="perr">Could not load details. ' +
        'The portal may be slow; try again.</p>';
    });
  document.getElementById('pclose').focus();
}
function closePanel() {
  els.overlay.classList.remove('show');
  els.panel.classList.remove('show');
}
els.rows.addEventListener('click', function (e) {
  var tr = e.target.closest('tr[data-i]');
  if (tr) openPanel(DATA.tenders[+tr.dataset.i]);
});
els.rows.addEventListener('keydown', function (e) {
  if (e.key === 'Enter') {
    var tr = e.target.closest('tr[data-i]');
    if (tr) openPanel(DATA.tenders[+tr.dataset.i]);
  }
});
document.getElementById('pclose').addEventListener('click', closePanel);
els.overlay.addEventListener('click', closePanel);
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closePanel();
});
document.getElementById('refresh').addEventListener('click', function () {
  location.href = '/?refresh=1';
});
setTimeout(function () { location.reload(); }, DATA.refreshSeconds * 1000);
</script>
</body></html>"""


def build_dashboard_page():
    def js(obj):
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    return DASHBOARD_PAGE.replace(
        "__DATA_JSON__", js(dashboard_data())).replace(
        "__SECTIONS_JSON__", js(sections_spec()))


def dashboard_pdf(tid, lang):
    details = fetch_detail_for_tid(tid)
    if details is None:
        return None
    live_row = next((r for r in _dash["live"] if r["tender_id"] == tid), None)
    if live_row is not None:
        row = live_row
    else:
        e = load_seen().get(tid, {})
        row = {
            "tender_id": tid,
            "title": e.get("title") or details.get("Title", ""),
            "ref_no": e.get("ref_no") or details.get("Tender Reference Number", ""),
            "closing": e.get("closing") or details.get("Bid Submission End Date", ""),
            "opening": e.get("opening", ""),
            "source": e.get("source", ""),
        }
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / ("MJP_%s_%s.pdf" % (tid, lang.upper()))
    build_tender_pdf(details, row, lang, path)
    if lang == "mr":
        verify_devanagari(path)
    return path


def serve_dashboard(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    def send(handler, code, body, ctype):
        handler.send_response(code)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    force = "refresh" in parse_qs(url.query)
                    refresh_live(force=force)
                    send(self, 200, build_dashboard_page().encode("utf-8"),
                         "text/html; charset=utf-8")
                elif url.path == "/api/detail":
                    tid = parse_qs(url.query).get("id", [""])[0]
                    payload = detail_payload(tid)
                    send(self, 200,
                         json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")
                elif url.path == "/unlock":
                    send(self, 200, unlock_page_html().encode("utf-8"),
                         "text/html; charset=utf-8")
                elif url.path.startswith("/pdf/"):
                    parts = url.path.strip("/").split("/")
                    if len(parts) != 3 or parts[2] not in ("en", "mr") \
                            or not TENDER_ID_RE.match(parts[1]):
                        self.send_error(404)
                        return
                    path = dashboard_pdf(parts[1], parts[2])
                    if path is None:
                        self.send_error(404, "No details available")
                        return
                    with open(path, "rb") as fh:
                        body = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Disposition",
                                     'inline; filename="%s"' % path.name)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
            except Exception as exc:
                log.error("dashboard request %s failed: %s", self.path, exc)
                try:
                    self.send_error(502, str(exc)[:150])
                except OSError:
                    pass

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != "/unlock":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = parse_qs(self.rfile.read(length).decode("utf-8"))
                captcha = body.get("captcha", [""])[0]
                result, error = unlock_submit(captcha)
                if error:
                    html = unlock_page_html(error=error)
                else:
                    awards, fetched = result
                    html = UNLOCK_RESULT_PAGE.format(
                        fetched=fetched, count=len(awards))
                send(self, 200, html.encode("utf-8"),
                     "text/html; charset=utf-8")
            except Exception as exc:
                log.error("unlock failed: %s", exc)
                try:
                    self.send_error(502, str(exc)[:150])
                except OSError:
                    pass

        def log_message(self, fmt, *args):
            log.info("dashboard: " + fmt, *args)

    log.info("Dashboard at http://localhost:%d (first load scrapes the "
             "portal, takes a moment)", port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def detail_payload(tid):
    if not TENDER_ID_RE.match(tid):
        return {"ok": False, "error": "bad tender id"}
    details = fetch_detail_for_tid(tid)
    award = load_awards().get(tid)
    if details is None and award is None:
        return {"ok": False, "error":
                "This tender is no longer on the portal and no cached "
                "details exist for it."}
    return {"ok": True, "details": details or {}, "award": award}

def main():
    parser = argparse.ArgumentParser(description="MJP tender tracker")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="scrape one tender, build both PDFs into out/ and skip WhatsApp")
    parser.add_argument(
        "--serve", action="store_true",
        help="serve the local dashboard instead of running the tracker")
    parser.add_argument(
        "--port", type=int, default=8765,
        help="dashboard port for --serve (default 8765)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S")

    # deep-translator issues requests with no timeout; the default socket
    # timeout bounds those so a stalled connection cannot hang the run.
    # Portal and WhatsApp calls pass explicit timeouts and are unaffected.
    socket.setdefaulttimeout(60)

    if args.serve:
        return serve_dashboard(args.port)
    session = make_session()
    if args.dry_run:
        return run_dry(session, fetch_tender_rows(session))
    return run_real(session)


if __name__ == "__main__":
    sys.exit(main())
