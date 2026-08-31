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
ORG_WATCHES and KEYWORD_WATCH): full tracking of Zilla Parishad Jalgaon
(RDD-CEO-JALGAON, where DPDC and Amdar Nidhi works are published) and the
Collector Jalgaon office, plus a keyword scan of all RDD-CEO-* and
COLLECTOR * organisations for Amdar Nidhi / DPDC related tenders.

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
#  - KEYWORD_WATCH: the tender lists of all orgs matching org_re are scanned
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

KEYWORD_WATCH = {
    "name": "Amdar Nidhi / DPDC",
    "org_re": r"^(RDD-CEO-|COLLECTOR\s)",
    "keywords": [
        "amdar nidhi", "aamdar nidhi", "amdar fund", "mla fund",
        "mla local development", "आमदार",
        "dpdc", "जिल्हा नियोजन", "district planning",
    ],
}

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
OUT_DIR = ROOT / "out"

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
    """All live tenders across every watch, each row tagged with a 'source'
    watch name. A tender matched by several watches keeps the first one."""
    index = fetch_org_index(session)
    out = []
    taken = set()

    def add(row, source):
        if row["tender_id"] not in taken:
            taken.add(row["tender_id"])
            out.append(dict(row, source=source))

    watched_orgs = set()
    for watch in ORG_WATCHES:
        for org_name in sorted(index):
            if not org_matches(org_name, watch):
                continue
            watched_orgs.add(org_name)
            try:
                for row in fetch_rows_for_org(session, org_name, index):
                    add(row, watch["name"])
            except Exception as exc:
                log.error("Watch %s org %s failed: %s", watch["name"], org_name, exc)
            time.sleep(0.5)

    if include_keyword_scan and KEYWORD_WATCH:
        kw = KEYWORD_WATCH
        for org_name in sorted(index):
            if org_name in watched_orgs or not re.search(kw["org_re"], org_name):
                continue
            try:
                for row in fetch_rows_for_org(session, org_name, index):
                    if keyword_hit(row, kw["keywords"]):
                        add(row, kw["name"])
            except Exception as exc:
                log.error("Keyword scan org %s failed: %s", org_name, exc)
            time.sleep(0.5)
    return out


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

def load_seen():
    if SEEN_FILE.exists():
        with open(SEEN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_seen(seen):
    tmp = SEEN_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, SEEN_FILE)


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
        "source": row.get("source", "MJP WSSD"),
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


def run_real(session):
    if not (smtp_configured() or whatsapp_configured()):
        raise RuntimeError(
            "No delivery channel configured: set SMTP_USER/SMTP_PASS for "
            "email or WHATSAPP_TOKEN/WHATSAPP_PHONE_ID/WHATSAPP_TO")
    seen = load_seen()
    rows = fetch_all_watch_rows(session)
    new_rows = [r for r in rows if r["tender_id"] not in seen]
    log.info("%d live tenders across watches, %d new", len(rows), len(new_rows))
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

DASHBOARD_CACHE_SECONDS = 300


def parse_portal_datetime(text):
    try:
        return datetime.strptime(text.strip(), "%d-%b-%Y %I:%M %p")
    except (ValueError, AttributeError):
        return None


def closing_badge(closing_text, now):
    dt = parse_portal_datetime(closing_text)
    if dt is None:
        return "", "unknown"
    days = (dt - now).total_seconds() / 86400.0
    if days < 0:
        return "closed", "Deadline passed"
    if days < 1:
        return "urgent", "Closes today"
    if days < 3:
        return "soon", "%d day(s) left" % int(days)
    return "open", "%d day(s) left" % int(days)


def build_dashboard_html(live, seen):
    now = datetime.now()
    live_ids = {r["tender_id"] for r in live}
    kw_name = KEYWORD_WATCH["name"] if KEYWORD_WATCH else None

    def esc(t):
        return xml_escape(str(t or ""))

    def sort_key(closing_text):
        dt = parse_portal_datetime(closing_text)
        return dt or datetime.max

    def row_html(tid, title, source, closing, extra=""):
        cls, badge = closing_badge(closing, now)
        return (
            '<tr class="st-%s"><td class="tid">%s</td><td>%s</td>'
            '<td>%s</td><td class="nowrap">%s</td>'
            '<td><span class="badge %s">%s</span>%s</td></tr>'
            % (cls, esc(tid), esc(title), esc(source), esc(closing),
               cls, esc(badge), extra))

    live_rows = "".join(
        row_html(r["tender_id"], r["title"], r["source"], r["closing"])
        for r in sorted(live, key=lambda r: sort_key(r["closing"])))

    kw_entries = [(tid, e) for tid, e in seen.items()
                  if e.get("source") == kw_name]
    kw_rows = "".join(
        row_html(tid, e.get("title", ""), e.get("source", ""),
                 e.get("closing", ""))
        for tid, e in sorted(kw_entries,
                             key=lambda te: sort_key(te[1].get("closing", ""))))

    gone_entries = [(tid, e) for tid, e in seen.items()
                    if tid not in live_ids and e.get("source") != kw_name]
    gone_rows = "".join(
        row_html(tid, e.get("title", ""), e.get("source", ""),
                 e.get("closing", ""))
        for tid, e in sorted(gone_entries,
                             key=lambda te: sort_key(te[1].get("closing", "")),
                             reverse=True)[:150])

    watch_names = ", ".join(w["name"] for w in ORG_WATCHES)
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="600">
<title>Tender Dashboard</title>
<style>
 body {{ font-family: -apple-system, "Segoe UI", Arial, sans-serif;
        margin: 0; background: #f4f6fa; color: #1c2733; }}
 header {{ background: #1a3e6e; color: #fff; padding: 14px 24px; }}
 header h1 {{ margin: 0; font-size: 18px; }}
 header p {{ margin: 4px 0 0; font-size: 12px; opacity: .8; }}
 main {{ padding: 18px 24px 60px; max-width: 1200px; margin: 0 auto; }}
 h2 {{ font-size: 15px; margin: 26px 0 8px; color: #1a3e6e; }}
 input#q {{ width: 320px; padding: 6px 10px; margin: 14px 0 2px;
           border: 1px solid #bcc7d6; border-radius: 6px; font-size: 13px; }}
 table {{ width: 100%; border-collapse: collapse; background: #fff;
         border: 1px solid #dbe2ec; font-size: 13px; }}
 th {{ background: #eef2f7; text-align: left; padding: 7px 9px;
      border-bottom: 1px solid #dbe2ec; position: sticky; top: 0; }}
 td {{ padding: 7px 9px; border-bottom: 1px solid #eef1f6;
      vertical-align: top; }}
 td.tid {{ font-family: ui-monospace, Menlo, monospace; font-size: 12px;
          white-space: nowrap; }}
 td.nowrap {{ white-space: nowrap; }}
 .badge {{ padding: 2px 8px; border-radius: 10px; font-size: 11px;
          white-space: nowrap; }}
 .badge.open {{ background: #e2f4e6; color: #1d6f34; }}
 .badge.soon {{ background: #fdf0d7; color: #8a6100; }}
 .badge.urgent {{ background: #fddddd; color: #a11212; }}
 .badge.closed, .badge.unknown {{ background: #e8e8ec; color: #555; }}
 tr.st-closed td {{ color: #8b93a0; }}
 .empty {{ color: #8b93a0; font-size: 13px; padding: 10px 2px; }}
</style></head>
<body>
<header>
 <h1>Tender Dashboard</h1>
 <p>Watches: {watch_names} plus keyword watch "{kw_name}" |
    generated {stamp} | auto refreshes every 10 minutes</p>
</header>
<main>
<input id="q" placeholder="Filter by any text..." onkeyup="flt()">
<h2>Live tenders ({n_live})</h2>
<table id="t-live"><tr><th>Tender ID</th><th>Title</th><th>Watch</th>
<th>Closing Date</th><th>Status</th></tr>{live_rows}</table>
{live_empty}
<h2>Keyword watch matches on record ({n_kw})</h2>
<table id="t-kw"><tr><th>Tender ID</th><th>Title</th><th>Watch</th>
<th>Closing Date</th><th>Status</th></tr>{kw_rows}</table>
{kw_empty}
<h2>Previously tracked, no longer listed ({n_gone})</h2>
<table id="t-gone"><tr><th>Tender ID</th><th>Title</th><th>Watch</th>
<th>Closing Date</th><th>Status</th></tr>{gone_rows}</table>
{gone_empty}
</main>
<script>
function flt() {{
  var q = document.getElementById('q').value.toLowerCase();
  ['t-live','t-kw','t-gone'].forEach(function(id) {{
    var rows = document.getElementById(id).rows;
    for (var i = 1; i < rows.length; i++) {{
      rows[i].style.display =
        rows[i].innerText.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
    }}
  }});
}}
</script>
</body></html>""".format(
        watch_names=esc(watch_names), kw_name=esc(kw_name or "none"),
        stamp=now.strftime("%d-%b-%Y %I:%M %p"),
        n_live=len(live), live_rows=live_rows,
        live_empty="" if live else '<p class="empty">Nothing live right now.</p>',
        n_kw=len(kw_entries), kw_rows=kw_rows,
        kw_empty="" if kw_entries else
            '<p class="empty">No keyword matches recorded yet.</p>',
        n_gone=len(gone_entries), gone_rows=gone_rows,
        gone_empty="" if gone_entries else
            '<p class="empty">Nothing has dropped off yet.</p>')


def serve_dashboard(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cache = {"html": "", "ts": 0.0}

    def render():
        if cache["html"] and time.time() - cache["ts"] < DASHBOARD_CACHE_SECONDS:
            return cache["html"]
        session = make_session()
        live = fetch_all_watch_rows(session, include_keyword_scan=False)
        cache["html"] = build_dashboard_html(live, load_seen())
        cache["ts"] = time.time()
        return cache["html"]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                body = render().encode("utf-8")
            except Exception as exc:
                log.error("Dashboard refresh failed: %s", exc)
                if cache["html"]:
                    body = cache["html"].encode("utf-8")
                else:
                    self.send_error(502, "Portal scrape failed: %s" % exc)
                    return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.info("dashboard: " + fmt, *args)

    log.info("Dashboard at http://localhost:%d (first load scrapes the "
             "portal, takes a moment)", port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


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
