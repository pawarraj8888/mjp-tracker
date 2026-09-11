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

import money
import requests
import timez
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
    """Portal timestamps are '%d-%b-%Y %I:%M %p' on tender pages, but award /
    result pages often carry a date only. Try the full form first, then
    date-only fallbacks, so contract dates still sort."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M",
                "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
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
    """Exact amount parse. Returns a Decimal, or None when the text has no
    parseable number (unknown -- never 0, which is a real value). See money.py
    for the grouping/decimal rules and why the old digit-strip was a bug."""
    return money.parse_amount(value_text)


def parse_inr_str(value_text):
    """As parse_inr but serialized to the canonical JSON form (exact decimal
    string, or None)."""
    return money.dec_to_str(money.parse_amount(value_text))


def format_inr(n):
    """1.51 Cr / 45 L / 59,000, from a Decimal, a decimal string, or None.
    Rounds only for display; unknown -> ''."""
    return money.format_inr(n)


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
        # Estimated (pre-bid) value only. Unknown stays unknown (None).
        est = money.parse_amount(d.get("Tender Value in ₹", ""))
        award_obj = awards.get(tid, {}).get("award") if awarded else None
        award_val = money.str_to_dec(award_obj.get("awarded_value")) \
            if award_obj else None
        contractor = display_contractor(award_obj.get("contractor", "")) \
            if award_obj else ""
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
            "contractor": contractor,
            "city": city,
            "cityGroup": city_group(city),
            # Estimated value (exact string | null) + sortable numeric.
            "value": money.dec_to_str(est),
            "valueFmt": format_inr(est) or "",
            "valueNum": float(est) if est is not None else -1,
            # Awarded value kept strictly separate from the estimate.
            "awardValue": money.dec_to_str(award_val),
            "awardValueFmt": format_inr(award_val) or "",
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
    awarded_ct = sum(1 for t in tenders if t["awarded"])
    sources = sorted({s for t in tenders for s in t["sources"]})
    cities = sorted({t["cityGroup"] for t in tenders if t["cityGroup"]},
                    key=str.casefold)
    return {
        "generated": now.strftime("%d-%b-%Y %I:%M %p"),
        "generatedIso": timez.iso_ist(),
        "refreshSeconds": DASHBOARD_CACHE_SECONDS,
        "stats": {"live": len(live_part), "soon": soon,
                  "awarded": awarded_ct, "total": len(tenders)},
        "sources": sources,
        "cities": cities,
        "tenders": tenders,
    }


# ---------------------------------------------------------------------------
# Contractor profiles (who is winning the contracts)
#
# Built entirely from awards.json, which the human-in-the-loop results
# import (/unlock) populates. Live tender pages never name bidders, so a
# contractor only appears here once the results of a tender it won have been
# imported. Every award page is also dumped raw (results_raw/), so the
# extraction below can be refined later without another captcha.
# ---------------------------------------------------------------------------

ENRICHMENT_FILE = ROOT / "enrichment.json"

# Detail-page captions that carry the winning bidder / contract facts on the
# GePNIC "Award of Contract" (AOC) pages. Matched case-insensitively, exact
# caption first, then a loose "contains" pass.
BIDDER_NAME_KEYS = [
    "Name of the Selected Bidder", "Selected Bidder Name", "Selected Bidder",
    "Name of Selected Bidder", "Awarded Bidder", "Successful Bidder",
    "Name of the Contractor", "Contractor Name", "Name of Contractor",
    "Bidder Name", "L1 Bidder", "Awarded to", "Name of Bidder",
]
AWARD_VALUE_KEYS = [
    "Awarded Value in ₹", "Contract Value in ₹", "Awarded Value",
    "Contract Value", "Contract Amount", "Value of Contract",
    "Awarded Price", "Contract Price",
]
CONTRACT_DATE_KEYS = [
    "Contract Date", "Award Date", "Date of Award", "AOC Date",
    "Contract Award Date", "Date of Contract",
]
_AWARDED_STATUS_RE = re.compile(
    r"award|accept|selected|successful|\bl-?1\b|\bl1\b", re.I)
_BIDDER_HEAD_RE = re.compile(
    r"bidder|company|firm|agency|contractor|vendor|name of", re.I)
_STATUS_HEAD_RE = re.compile(r"status|rank|result|remark|position", re.I)
_VALUE_HEAD_RE = re.compile(r"value|amount|price|quoted", re.I)


def _first_pair(pairs, keys):
    """First non-empty value for any caption in keys, exact match then loose."""
    low = {k.strip().casefold(): v.strip()
           for k, v in pairs.items() if v and v.strip()}
    for k in keys:
        v = low.get(k.casefold())
        if v:
            return v
    for k in keys:
        kc = k.casefold()
        for pk, pv in low.items():
            if kc in pk:
                return pv
    return ""


def _cells(tr):
    return [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]


def parse_bidder_tables(soup):
    """Pull the bidder list off a GePNIC AOC / result page. Returns
    (bidders, awarded_name) where bidders is a list of {name, status, value}.

    GePNIC uses deeply nested table layout, so the header row is located by
    finding a row of short label cells that actually contains a 'Bidder Name'
    (or company/agency) column, ignoring the concatenated outer wrapper rows.
    The data rows are then read from that same table. On an 'Awarded Bids
    List' every listed bidder is a winner, so the first data row is the
    awarded contractor."""
    header_row = None
    name_idx = value_idx = status_idx = None
    for tr in soup.find_all("tr"):
        cells = _cells(tr)
        # Real header cells are short labels, never the whole-page blob that
        # the nested wrapper <tr>s stringify to.
        if not cells or len(cells) < 2 or any(len(c) > 40 for c in cells):
            continue
        low = [c.casefold() for c in cells]
        idx = next((i for i, c in enumerate(low)
                    if c in ("bidder name", "company name", "name of bidder",
                             "name of the bidder", "agency name", "firm name",
                             "contractor name")), None)
        if idx is None:
            continue
        name_idx = idx
        value_idx = next((i for i, c in enumerate(low)
                          if "awarded value" in c or "bid value" in c
                          or c in ("value", "amount")), None)
        status_idx = next((i for i, c in enumerate(low)
                           if c in ("status", "result", "rank", "remarks")), None)
        header_row = tr
        break
    if header_row is None:
        return [], ""

    table = header_row.find_parent("table") or header_row
    bidders, awarded, started = [], "", False
    for tr in table.find_all("tr"):
        if tr is header_row:
            started = True
            continue
        if not started:
            continue
        cells = _cells(tr)
        if len(cells) <= name_idx:
            continue
        name = cells[name_idx].strip()
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        if name.casefold() in ("bidder name", "company name", "s.no", "total",
                               "name of bidder", "contractor name"):
            continue
        if len(name) > 120:  # a merged/blob cell slipped through
            continue
        status = cells[status_idx].strip() if (
            status_idx is not None and len(cells) > status_idx) else ""
        value = cells[value_idx].strip() if (
            value_idx is not None and len(cells) > value_idx) else ""
        bidders.append({"name": name, "status": status, "value": value})
        if status and _AWARDED_STATUS_RE.search(status) and not awarded:
            awarded = name
    # On the "Awarded Bids List" table there is no status column and every row
    # is a winner, so fall back to the first listed bidder.
    if not awarded and bidders:
        awarded = bidders[0]["name"]
    return bidders, awarded


def extract_award_info(pairs, html):
    """Structured award facts from one imported result page: the winning
    contractor, the awarded value, the contract date and every bidder seen."""
    raw_value = _first_pair(pairs, AWARD_VALUE_KEYS)
    info = {"contractor": "", "awarded_value": None, "awarded_value_raw": "",
            "contract_date": "", "bidders": []}
    info["contractor"] = _first_pair(pairs, BIDDER_NAME_KEYS)
    info["awarded_value"] = parse_inr_str(raw_value)
    info["awarded_value_raw"] = raw_value
    info["contract_date"] = _first_pair(pairs, CONTRACT_DATE_KEYS)
    if html:
        try:
            bidders, awarded = parse_bidder_tables(BeautifulSoup(html, "lxml"))
        except Exception as exc:
            log.warning("Bidder table parse failed: %s", exc)
            bidders, awarded = [], ""
        info["bidders"] = bidders
        if not info["contractor"]:
            info["contractor"] = awarded or (
                bidders[0]["name"] if len(bidders) == 1 else "")
        if not money.is_known(info["awarded_value"]):
            for b in bidders:
                if b["name"] == info["contractor"] and b.get("value"):
                    info["awarded_value"] = parse_inr_str(b["value"])
                    if not info["awarded_value_raw"]:
                        info["awarded_value_raw"] = b["value"]
                    break
    return info


_CONTRACTOR_LEAD_RE = re.compile(
    r"^(m\s*/?\s*s\.?|messrs\.?|shri\.?|sri\.?|smt\.?|mr\.?|mrs\.?)\s+", re.I)


def display_contractor(name):
    """The contractor name exactly as the portal shows it, only tidied for
    whitespace and punctuation. Used for what the dashboard displays."""
    return re.sub(r"\s+", " ", sanitize(name or "")).strip(" .,-")


def normalize_contractor(name):
    """Name with honorifics and M/S dropped, for building the grouping key."""
    s = display_contractor(name)
    s = _CONTRACTOR_LEAD_RE.sub("", s)
    return s.strip(" .,-")


def contractor_key(name):
    """Grouping key that folds spelling and legal-suffix variants together
    ('M/s ABC Constructions Pvt. Ltd.' and 'ABC Constructions' -> the same
    key) while keeping genuinely different firms apart."""
    s = normalize_contractor(name).casefold()
    s = re.sub(r"[.,&'\"()/\\-]", " ", s)
    s = re.sub(r"\b(pvt|private|ltd|limited|co|company|corporation|corp)\b",
               " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or normalize_contractor(name).casefold()


def load_enrichment():
    """Externally researched public info per contractor (enrichment.json),
    keyed by contractor_key. Authored offline; the server only reads it."""
    return _load_state(ENRICHMENT_FILE, {})


ANALYTICS_FILE = ROOT / "analytics.json"


def load_analytics():
    """Pipeline analytics bundle (analytics.json), produced by
    `python -m pipeline export`. Read like the other state files so the hosted
    dashboard shows it without a redeploy."""
    return _load_state(ANALYTICS_FILE, {})


NOTIFICATIONS_FILE = ROOT / "notifications.json"
DATA_STATUS_FILE = ROOT / "data_status.json"
CORRECTIONS_FILE = ROOT / "corrections.json"


def load_notifications():
    """Server-generated notification stream (notifications.json)."""
    return _load_state(NOTIFICATIONS_FILE, {"notifications": [], "count": 0,
                                            "by_kind": {}})


def load_data_status():
    """Data-freshness / source-health snapshot (data_status.json)."""
    return _load_state(DATA_STATUS_FILE, {"overall_status": "unknown"})


def _award_of(entry):
    ai = entry.get("award")
    if not ai or not ai.get("contractor"):
        ai = extract_award_info(entry.get("fields", {}), "")
    return ai


def contractors_data():
    """Aggregate awards.json into one profile per contractor."""
    awards = load_awards()
    details_all = load_details_cache()
    enrich = load_enrichment()
    groups = {}

    for tid, entry in awards.items():
        pairs = entry.get("fields", {})
        detail = details_all.get(tid, {})
        ai = _award_of(entry)
        name = display_contractor(ai.get("contractor", ""))
        if not name:
            continue
        key = contractor_key(name)
        # Awarded value only. We never substitute an estimate for a missing
        # award amount; a missing award value stays unknown (None).
        value = money.str_to_dec(ai.get("awarded_value"))
        org_chain = detail.get("Organisation Chain") or \
            pairs.get("Organisation Chain") or entry.get("org", "")
        dept = publisher(org_chain) or entry.get("org", "")
        title = detail.get("Title") or pairs.get("Title", "")
        city = derive_city(detail.get("Location") or pairs.get("Location"),
                           org_chain, title)
        sector = (detail.get("Product Category") or pairs.get("Product Category")
                  or detail.get("Tender Category") or pairs.get("Tender Category", ""))
        cdate = ai.get("contract_date") or detail.get("Bid Opening Date") \
            or pairs.get("Published Date", "")

        g = groups.setdefault(key, {
            "key": key, "names": {}, "contracts": [], "value": money.add([]),
            "known_count": 0, "departments": {}, "cities": {}, "sectors": {},
            "competitors": {}, "dates": []})
        g["names"][name] = g["names"].get(name, 0) + 1
        if value is not None:
            g["value"] += value
            g["known_count"] += 1
        if dept:
            g["departments"][dept] = g["departments"].get(dept, 0) + 1
        if city:
            g["cities"][city] = g["cities"].get(city, 0) + 1
        if sector:
            g["sectors"][sector] = g["sectors"].get(sector, 0) + 1
        if cdate:
            g["dates"].append(cdate)
        for b in ai.get("bidders", []):
            bname = display_contractor(b.get("name", ""))
            if bname and contractor_key(bname) != key:
                ck = contractor_key(bname)
                comp = g["competitors"].setdefault(ck, {"name": bname, "count": 0})
                comp["count"] += 1
        g["contracts"].append({
            "id": tid, "title": title, "org": dept, "city": city,
            "sector": sector, "value": money.dec_to_str(value),
            "valueFmt": format_inr(value) or "Unknown",
            "valueKnown": value is not None,
            "date": cdate, "dateTs": portal_ts(cdate),
        })

    contractors = []
    for key, g in groups.items():
        name = max(g["names"], key=lambda n: (g["names"][n], len(n)))
        contracts = sorted(g["contracts"], key=lambda c: c["dateTs"], reverse=True)
        date_ts = [portal_ts(d) for d in g["dates"] if portal_ts(d)]
        competitors = sorted(g["competitors"].values(),
                             key=lambda c: c["count"], reverse=True)
        known = g["known_count"]
        avg = (g["value"] / known) if known else None
        contractors.append({
            "key": key,
            "name": name,
            "count": len(contracts),
            "knownValueCount": known,
            "value": money.dec_to_str(g["value"]),
            "valueFmt": format_inr(g["value"]),
            "avgFmt": format_inr(avg) if avg is not None else "",
            "departments": sorted(g["departments"], key=lambda d: -g["departments"][d]),
            "cities": sorted(g["cities"], key=lambda c: -g["cities"][c]),
            "sectors": sorted(g["sectors"], key=lambda s: -g["sectors"][s]),
            "competitors": competitors,
            "contracts": contracts,
            "lastDate": max(g["dates"], key=portal_ts) if g["dates"] else "",
            "lastTs": max(date_ts) if date_ts else 0,
            "enrichment": enrich.get(key),
        })

    contractors.sort(
        key=lambda c: (money.str_to_dec(c["value"]) or money.add([]), c["count"]),
        reverse=True)
    total_value = money.add(c["value"] for c in contractors)
    depts = sorted({d for c in contractors for d in c["departments"]},
                   key=str.casefold)
    cities = sorted({ct for c in contractors for ct in c["cities"]},
                    key=str.casefold)
    sectors = sorted({s for c in contractors for s in c["sectors"]},
                     key=str.casefold)
    return {
        "stats": {
            "contractors": len(contractors),
            "contracts": sum(c["count"] for c in contractors),
            "value": money.dec_to_str(total_value),
            "valueFmt": format_inr(total_value),
            "knownValueContracts": sum(c["knownValueCount"] for c in contractors),
            "enriched": sum(1 for c in contractors
                            if c["enrichment"] and c["enrichment"].get("verified")),
        },
        "departments": depts,
        "cities": cities,
        "sectors": sectors,
        "contractors": contractors,
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
label {{ display: block; font-size: 12px; color: #475569; margin: 12px 0 4px;
 font-weight: 500; }}
.row {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end;
 margin-top: 6px; }}
button {{ font: inherit; background: #1E40AF; color: #fff; border: none;
 border-radius: 8px; padding: 10px 18px; cursor: pointer; }}
button:hover {{ background: #17346C; }}
.err {{ background: #FEE2E2; color: #B91C1C; padding: 9px 12px;
 border-radius: 8px; font-size: 13px; }}
.ok {{ background: #DCFCE7; color: #15803D; padding: 9px 12px;
 border-radius: 8px; font-size: 13px; }}
a {{ color: #1E40AF; }}
</style></head><body><main>
<h1>Import Results of Tenders</h1>
<p>The portal's results section needs a search term plus a captcha. Enter a
keyword (at least 4 letters, e.g. <b>jalgaon</b>, <b>jeevan</b>,
<b>water supply</b>) or an exact tender id, type the captcha code shown, and
the import crawls the award pages for every matching published result in this
one authorized session. Run it again with other keywords to widen coverage.</p>
{error}
<form method="post" action="/unlock">
 <label for="kw">Search keyword (title or work), at least 4 letters</label>
 <input type="text" id="kw" name="keyword" value="{keyword}" autofocus
  autocomplete="off" placeholder="e.g. jalgaon">
 <label for="tid">or exact Tender ID (optional)</label>
 <input type="text" id="tid" name="tender_id" autocomplete="off"
  placeholder="2026_XXXXX_000000_0">
 <img class="cap" src="{captcha}" alt="Portal captcha image">
 <div class="row">
  <input type="text" name="captcha" autocomplete="off"
   aria-label="Captcha code" placeholder="Captcha code">
  <button type="submit">Unlock and import</button>
 </div>
</form>
<p><a href="/">Back to dashboard</a></p>
</main></body></html>"""


def unlock_page_html(error="", keyword="jalgaon"):
    captcha = unlock_form_state()
    err = '<p class="err">%s</p>' % xml_escape(error) if error else ""
    return UNLOCK_PAGE.format(error=err, captcha=xml_escape(captcha),
                             keyword=xml_escape(keyword))


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


RESULT_FETCH_CAP = 400


def _active_error(html):
    """The portal's currently-shown error message, from the alert box it
    renders server-side (table.message_box / td.alerttext). Returns '' when
    no error is active. This is the reliable signal; the page's inline
    JavaScript also contains captcha alert() strings that must be ignored."""
    soup = BeautifulSoup(html, "lxml")
    box = soup.find(class_="alerttext") or soup.find(class_="message_box")
    if box:
        return box.get_text(" ", strip=True)
    return ""


LINKS_PER_ROW = 2  # per results row: the Title detail link + the first AOC link
_result_seq = [0]


def _results_rows(html):
    """Parse a Results-of-Tenders search table into one dict per matching
    tender: {title, org, links}. The table columns are S.No, AOC Date,
    e-Published Date, Title (DirectLink), Organisation Chain, AOC (DirectLinks).
    The Title link opens the full tender detail; the AOC link opens the award
    detail with the winning bidder."""
    soup = BeautifulSoup(html, "lxml")
    dl = soup.find("a", href=re.compile("DirectLink"))
    if dl is None:
        return []
    table = dl.find_parent("table")
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        if not cells[0].get_text(strip=True).rstrip(".").isdigit():
            continue  # skip header / spacer rows
        title, org, links = "", "", []
        for c in cells:
            for a in c.find_all("a", href=True):
                if "directlink" in a["href"].lower():
                    full = (a["href"] if a["href"].startswith("http")
                            else BASE_URL + a["href"])
                    links.append(full)
                    txt = a.get_text(" ", strip=True).strip("[] ")
                    if txt and not title:
                        title = txt
            ctext = c.get_text(" ", strip=True)
            if "||" in ctext and not org:
                org = ctext
        if links:
            rows.append({"title": title, "org": org, "links": links})
    return rows


def _merge_award(old, new):
    """Combine award facts from two pages of the same tender, preferring a
    found contractor and value and unioning the bidder list."""
    out = dict(old or {})
    for k in ("contractor", "contract_date", "awarded_value_raw"):
        if new.get(k) and not out.get(k):
            out[k] = new[k]
    if money.is_known(new.get("awarded_value")) and \
            not money.is_known(out.get("awarded_value")):
        out["awarded_value"] = new["awarded_value"]
    bidders = {b["name"]: b for b in (out.get("bidders") or [])}
    for b in new.get("bidders") or []:
        bidders.setdefault(b["name"], b)
    out["bidders"] = list(bidders.values())
    out.setdefault("contractor", "")
    out.setdefault("awarded_value", None)
    out.setdefault("awarded_value_raw", "")
    out.setdefault("contract_date", "")
    return out


def _store_result_page(session, url, awards, org_name="", title_hint="",
                       tid_hint=""):
    """Fetch one result page, parse it, and merge it into the award entry for
    its tender. Non-HTML responses (AOC PDFs) are skipped. Returns the tender
    id touched, or '' when the page could not be attached to a tender."""
    try:
        resp = portal_get(session, url)
    except Exception as exc:
        log.warning("Result page fetch failed: %s", exc)
        return ""
    time.sleep(0.25)
    if "html" not in (resp.headers.get("Content-Type", "").lower()):
        return tid_hint  # e.g. an AOC PDF; nothing to parse, keep the hint
    page = resp.text
    pairs = _extract_pairs(BeautifulSoup(page, "lxml"))
    tid = pairs.get("Tender ID") or ""
    if not TENDER_ID_RE.match(tid):
        m = TENDER_ID_RE.search(
            " ".join(re.findall(r"\d{4}_\w+_\d+_\d+", page)[:1]))
        tid = m.group(0) if m else (tid_hint or "")
    if not TENDER_ID_RE.match(tid or ""):
        _result_seq[0] += 1
        _dump_page("unparsed_%d" % _result_seq[0], page)
        return ""
    _result_seq[0] += 1
    _dump_page("tender_%s_%d" % (tid, _result_seq[0]), page)
    entry = awards.get(tid, {"fields": {}})
    entry["fields"].update(pairs)
    if org_name and not entry.get("org"):
        entry["org"] = org_name
    if title_hint and not entry["fields"].get("Title"):
        entry["fields"]["Title"] = title_hint
    entry["fetched"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entry["award"] = _merge_award(entry.get("award"),
                                  extract_award_info(entry["fields"], page))
    awards[tid] = entry
    return tid


def _crawl_results_orgs(session, root_html, awards):
    """Fallback for an organisation listing: drill into orgs of interest and
    fetch each tender result row inside them."""
    soup = BeautifulSoup(root_html, "lxml")
    org_rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=True)
        if len(tds) >= 3 and a and tds[0].get_text(strip=True).isdigit():
            org_rows.append((tds[1].get_text(strip=True), BASE_URL + a["href"]))
    picked = [(n, u) for n, u in org_rows if _org_of_interest(n)][:40]
    log.info("Results listing looks org-level: %d orgs, crawling %d of interest",
             len(org_rows), len(picked))
    fetched = 0
    for name, url in picked:
        try:
            html = portal_get(session, url).text
            _dump_page("org_" + name, html)
        except Exception as exc:
            log.error("Results org %s failed: %s", name, exc)
            continue
        time.sleep(0.4)
        for row in _results_rows(html):
            if fetched >= RESULT_FETCH_CAP:
                break
            tid_hint = ""
            for link in row["links"][:LINKS_PER_ROW]:
                tid = _store_result_page(session, link, awards,
                                         org_name=publisher(row["org"]) or name,
                                         title_hint=row["title"],
                                         tid_hint=tid_hint)
                if tid:
                    tid_hint = tid
            if tid_hint:
                fetched += 1
    save_awards(awards)
    return awards, fetched


def crawl_results(session, root_html, progress=None):
    """Walk the unlocked results section. A keyword or tender-id search
    returns a flat table of matching tenders; for each row the Title link
    (full tender detail) and the first AOC link (award detail with the
    winning bidder) are fetched and merged by tender id. If instead an
    organisation listing came back, fall back to drilling into orgs of
    interest. Every fetched page is dumped raw so parsing can be refined
    without another captcha. progress(done, total) is called as it goes."""
    _dump_page("results_root", root_html)
    awards = load_awards()
    _result_seq[0] = 0
    rows = _results_rows(root_html)
    if not rows:
        return _crawl_results_orgs(session, root_html, awards)
    log.info("Results search: %d matching tender(s)", len(rows))
    if progress:
        progress(0, len(rows))
    fetched = 0
    for row in rows:
        if fetched >= RESULT_FETCH_CAP:
            log.info("hit result fetch cap of %d", RESULT_FETCH_CAP)
            break
        tid_hint = ""
        for link in row["links"][:LINKS_PER_ROW]:
            tid = _store_result_page(session, link, awards,
                                     org_name=publisher(row["org"]),
                                     title_hint=row["title"],
                                     tid_hint=tid_hint)
            if tid:
                tid_hint = tid
        if tid_hint:
            fetched += 1
        if progress:
            progress(fetched, len(rows))
        if fetched and fetched % 25 == 0:
            save_awards(awards)  # checkpoint a long crawl
            log.info("... %d/%d tenders imported", fetched, len(rows))
    save_awards(awards)
    return awards, fetched


# Background import state, so a long crawl does not block the browser POST.
_import_status = {"running": False, "done": 0, "total": 0,
                  "keyword": "", "error": "", "finished": False}


def import_status():
    return dict(_import_status)


def _run_import(session, root_html):
    def progress(done, total):
        _import_status["done"] = done
        _import_status["total"] = total
    try:
        _awards, fetched = crawl_results(session, root_html, progress=progress)
        _import_status["done"] = fetched
    except Exception as exc:
        log.error("Background import failed: %s", exc)
        _import_status["error"] = str(exc)[:200]
    finally:
        _import_status["running"] = False
        _import_status["finished"] = True


def reparse_from_dumps():
    """Rebuild awards.json from the raw result pages dumped during the last
    import (results_raw/), so the extraction can be refined offline without
    solving the captcha again."""
    raw = RESULTS_RAW_DIR
    if not raw.exists():
        log.error("No raw result dumps found at %s", raw)
        return 1
    # Rebuild cleanly from all dumps so the result is deterministic and free of
    # any stale entries. The tender id must come from the page content; the
    # dump filename is never used (its trailing _<seq> would slip past the
    # tender-id shape).
    awards = {}
    n = 0
    for path in sorted(raw.glob("tender_*.html")):
        html = path.read_text(encoding="utf-8", errors="replace")
        pairs = _extract_pairs(BeautifulSoup(html, "lxml"))
        tid = (pairs.get("Tender ID") or pairs.get("Tender ID :") or "").strip()
        if not TENDER_ID_RE.match(tid):
            m = re.search(r"\b(\d{4}_[A-Za-z0-9]+_\d+_\d+)\b", html)
            tid = m.group(1) if m else ""
        if not TENDER_ID_RE.match(tid or ""):
            continue  # e.g. a signature-only page with no tender id
        entry = awards.get(tid, {"fields": {}})
        entry["fields"].update(pairs)
        entry["award"] = _merge_award(entry.get("award"),
                                      extract_award_info(entry["fields"], html))
        awards[tid] = entry
        n += 1
    save_awards(awards)
    named = sum(1 for e in awards.values()
                if (e.get("award") or {}).get("contractor"))
    log.info("Reparsed %d result page(s); %d have a contractor name", n, named)
    return 0


MIN_KEYWORD_LEN = 4  # portal rule: keyword must be at least 4 characters


def unlock_submit(captcha_text, keyword="", tender_id=""):
    """Submit the person's captcha answer plus a search term and, on success,
    crawl. The Results section requires a keyword (>= 4 chars) or an exact
    tender id; an empty search is rejected regardless of the captcha. Errors
    are read from the portal's own alert box, not from the inline JavaScript.
    The raw response is always dumped for inspection."""
    session = _unlock.get("session")
    fields = _unlock.get("fields")
    if session is None or fields is None:
        return None, "The unlock session expired, reload the page and try again."
    keyword = (keyword or "").strip()
    tender_id = (tender_id or "").strip()
    if not keyword and not tender_id:
        return None, ("Enter a search keyword (at least %d letters) or an exact "
                      "tender id before importing." % MIN_KEYWORD_LEN)
    if keyword and not tender_id and len(keyword) < MIN_KEYWORD_LEN:
        return None, ("The keyword needs at least %d characters (a portal rule)."
                      % MIN_KEYWORD_LEN)
    data = dict(fields)
    data["captchaText"] = captcha_text.strip()
    data["Keyword"] = keyword
    data["TenderId"] = tender_id
    data["submitname"] = "Search"
    resp = session.post(BASE_URL + "/nicgep/app", data=data,
                        timeout=HTTP_TIMEOUT, verify=session.verify)
    html = resp.text
    _dump_page("post_response", html)
    err = _active_error(html)
    low = err.casefold()
    log.info("unlock POST: %d bytes, active_error=%r", len(html), err[:90])
    if "captcha" in low:
        return None, ("The portal rejected the captcha code. Reload the page "
                      "for a fresh code and try again.")
    if "tender id or keyword" in low:
        return None, ("The portal did not accept that search term; try a "
                      "different keyword.")
    if "no results" in low:
        return None, ("The captcha worked, but no published results match "
                      "'%s'. Try another keyword." % (tender_id or keyword))
    rows = _results_rows(html)
    if not rows:
        # Organisation listing or an unexpected page: crawl synchronously once
        # (the fallback is small) so nothing is silently lost.
        awards, fetched = crawl_results(session, html)
        if fetched == 0:
            return None, ("The captcha worked and the search ran, but no "
                          "result rows could be read. The raw response was "
                          "saved; try another keyword meanwhile.")
        return {"mode": "done", "fetched": fetched, "count": len(awards)}, None
    if _import_status["running"]:
        return None, ("An import is already running (%d of %d done). Let it "
                      "finish before starting another." %
                      (_import_status["done"], _import_status["total"]))
    _import_status.update({"running": True, "done": 0, "total": len(rows),
                           "keyword": keyword or tender_id, "error": "",
                           "finished": False})
    threading.Thread(target=_run_import, args=(session, html),
                     daemon=True).start()
    return {"mode": "started", "total": len(rows),
            "keyword": keyword or tender_id}, None


UNLOCK_STARTED_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Import running</title>
<style>body {{ font-family: "Fira Sans", sans-serif; background: #F8FAFC;
 margin: 0; }} main {{ max-width: 560px; margin: 40px auto; background: #fff;
 border: 1px solid #DBEAFE; border-radius: 12px; padding: 26px 30px;
 font-size: 14px; color: #0F172A; }} a {{ color: #1E40AF; }}
 .prog {{ background: #EFF4FB; border: 1px solid #DBEAFE; border-radius: 8px;
 padding: 12px 14px; font-size: 14px; margin: 14px 0; }}
 .bar {{ height: 8px; background: #E2E8F0; border-radius: 999px; margin-top: 8px;
 overflow: hidden; }} .bar i {{ display: block; height: 100%; width: 0;
 background: #1E40AF; transition: width .3s; }}</style>
</head><body><main>
<h1 style="color:#1E3A8A;font-size:17px">Import running</h1>
<p>Captcha accepted. Importing award records for the {total} tenders whose
results match "<b>{keyword}</b>". This runs in the background, so you can
leave this page open or head straight to the dashboard.</p>
<div class="prog"><span id="msg">Starting...</span>
 <div class="bar"><i id="bar"></i></div></div>
<p><a href="/#contractors">Open the Contractors tab</a> - it fills in as
records are parsed. Run the import again with another keyword to widen it.</p>
<script>
setInterval(function () {{
  fetch('/import-status').then(function (r) {{ return r.json(); }})
    .then(function (s) {{
      var m = document.getElementById('msg'), b = document.getElementById('bar');
      var pct = s.total ? Math.round(100 * s.done / s.total) : 0;
      b.style.width = pct + '%';
      if (s.error) {{ m.textContent = 'Stopped: ' + s.error; return; }}
      if (s.finished || !s.running) {{
        m.textContent = 'Done. ' + s.done + ' of ' + s.total +
          ' tenders imported.';
      }} else {{
        m.textContent = 'Imported ' + s.done + ' of ' + s.total + '...';
      }}
    }}).catch(function () {{}});
}}, 2000);
</script>
</main></body></html>"""


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


from dashboard_page import DASHBOARD_PAGE  # noqa: E402  (large HTML template)


def build_dashboard_page():
    def js(obj):
        return json.dumps(obj, ensure_ascii=False, default=str).replace(
            "</", "<\\/")
    dash = dashboard_data()
    return (DASHBOARD_PAGE
            .replace("__DATA_JSON__", js(dash))
            .replace("__SECTIONS_JSON__", js(sections_spec()))
            .replace("__CONTRACTORS_JSON__", js(contractors_data()))
            .replace("__ANALYTICS_JSON__", js(load_analytics()))
            .replace("__NOTIFS_JSON__", js(load_notifications()))
            .replace("__STATUS_JSON__", js(load_data_status()))
            .replace("__AWARDS_JSON__", js(awards_feed()))
            .replace("__OVERVIEW_JSON__", js(overview_data(dash))))


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
                elif url.path == "/import-status":
                    send(self, 200,
                         json.dumps(import_status()).encode("utf-8"),
                         "application/json; charset=utf-8")
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
                keyword = body.get("keyword", [""])[0]
                tender_id = body.get("tender_id", [""])[0]
                result, error = unlock_submit(captcha, keyword, tender_id)
                if error:
                    html = unlock_page_html(error=error,
                                            keyword=keyword or "jalgaon")
                elif result.get("mode") == "started":
                    html = UNLOCK_STARTED_PAGE.format(
                        total=result["total"],
                        keyword=xml_escape(result["keyword"]))
                else:
                    html = UNLOCK_RESULT_PAGE.format(
                        fetched=result["fetched"], count=result["count"])
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


def awards_feed():
    """One row per imported award for the Awards view: winner, corrected
    awarded value, contract date, department, city, and estimate (kept
    separate). Newest first by contract date."""
    awards = load_awards()
    details_all = load_details_cache()
    rows = []
    for tid, entry in awards.items():
        ai = _award_of(entry)
        contractor = display_contractor(ai.get("contractor", ""))
        if not contractor:
            continue
        pairs = entry.get("fields", {})
        detail = details_all.get(tid, {})
        org_chain = detail.get("Organisation Chain") or \
            pairs.get("Organisation Chain") or entry.get("org", "")
        title = detail.get("Title") or pairs.get("Title", "")
        cdate = ai.get("contract_date", "")
        est = money.parse_amount(detail.get("Tender Value in ₹")
                                 or pairs.get("Tender Value in ₹", ""))
        aval = money.str_to_dec(ai.get("awarded_value"))
        rows.append({
            "id": tid,
            "title": title,
            "contractor": contractor,
            "contractorKey": contractor_key(contractor),
            "org": publisher(org_chain) or entry.get("org", ""),
            "city": derive_city(detail.get("Location") or pairs.get("Location"),
                                org_chain, title),
            "awardValue": money.dec_to_str(aval),
            "awardValueFmt": format_inr(aval) or "Unknown",
            "awardValueNum": float(aval) if aval is not None else -1,
            "estimate": money.dec_to_str(est),
            "estimateFmt": format_inr(est) or "Unknown",
            "contractDate": cdate,
            "contractDateTs": portal_ts(cdate),
            "bidders": len(ai.get("bidders") or []),
        })
    rows.sort(key=lambda r: r["contractDateTs"], reverse=True)
    return rows


def overview_data(dash=None):
    """KPI tiles for the Overview homepage. Each metric carries an explicit
    definition and period so it can be trusted and drilled into."""
    dash = dash or dashboard_data()
    tenders = dash["tenders"]
    live = [t for t in tenders if t["live"]]
    now_ts = int(datetime.now().timestamp())
    week = 7 * 86400
    closing_soon = [t for t in live
                    if t["closingTs"] and 0 <= t["closingTs"] - now_ts <= week]
    feed = awards_feed()
    recent_awards = feed[:8]
    awarded_known = money.add(r["awardValue"] for r in feed)
    return {
        "tiles": [
            {"key": "live", "label": "Active opportunities",
             "value": len(live),
             "definition": "Tenders currently open for bidding on the portal "
                           "(live listing, bid submission not yet closed).",
             "period": "current state"},
            {"key": "closing", "label": "Closing within 7 days",
             "value": len(closing_soon),
             "definition": "Live tenders whose bid submission deadline is "
                           "within the next 7 days.",
             "period": "next 7 days"},
            {"key": "awards", "label": "Awards on record",
             "value": len(feed),
             "definition": "Tenders for which an Award of Contract has been "
                           "imported (winner known).",
             "period": "all tracked"},
            {"key": "awarded_value", "label": "Known awarded value",
             "value": format_inr(awarded_known),
             "definition": "Sum of awarded contract values across awards with a "
                           "known amount. Not comparable to floated estimate.",
             "period": "all tracked"},
        ],
        "closing_soon": sorted(closing_soon, key=lambda t: t["closingTs"])[:8],
        "recent_awards": recent_awards,
    }


def _tender_events_for(tid):
    """Lifecycle events for one tender from the pipeline DB, if present. The
    hosted app has no DB (and a read-only filesystem), so this returns [] there;
    the award object itself still carries the award facts for the timeline."""
    if os.environ.get("VERCEL"):
        return []
    try:
        from pipeline.store import Store, uid
    except Exception:
        return []
    try:
        store = Store()
    except Exception:
        return []
    try:
        rid = uid("tender", "mahatenders", tid)
        rows = store.query(
            "SELECT event_type, detail, event_at, source FROM tender_events"
            " WHERE tender_id=? ORDER BY event_at", (rid,))
        return rows
    except Exception:
        return []
    finally:
        store.close()


def detail_payload(tid):
    if not TENDER_ID_RE.match(tid):
        return {"ok": False, "error": "bad tender id"}
    details = fetch_detail_for_tid(tid)
    award = load_awards().get(tid)
    if details is None and award is None:
        return {"ok": False, "error":
                "This tender is no longer on the portal and no cached "
                "details exist for it."}
    return {"ok": True, "details": details or {}, "award": award,
            "events": _tender_events_for(tid)}

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
    parser.add_argument(
        "--reparse", action="store_true",
        help="rebuild awards.json from raw result dumps (no captcha needed)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S")

    # deep-translator issues requests with no timeout; the default socket
    # timeout bounds those so a stalled connection cannot hang the run.
    # Portal and WhatsApp calls pass explicit timeouts and are unaffected.
    socket.setdefaulttimeout(60)

    if args.reparse:
        return reparse_from_dumps()
    if args.serve:
        return serve_dashboard(args.port)
    session = make_session()
    if args.dry_run:
        return run_dry(session, fetch_tender_rows(session))
    return run_real(session)


if __name__ == "__main__":
    sys.exit(main())
