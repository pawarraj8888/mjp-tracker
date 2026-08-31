# mjp-tracker

Tracks tenders on mahatenders.gov.in. For every new tender it parses the
detail page, builds a work-details PDF in English and a second one translated
into Marathi, and delivers both by email (SMTP) and/or WhatsApp (Meta Cloud
API), whichever is configured.

Watched sources (see ORG_WATCHES / KEYWORD_WATCH in tracker.py):

- Maharashtra Jeevan Pradhikaran: organisation "Member Secretary(WSSD),Mumbai"
- Zilla Parishad Jalgaon (RDD-CEO-JALGAON) and Collector Jalgaon: everything
  they publish (this is where DPDC and Amdar Nidhi works appear)
- Amdar Nidhi / DPDC keyword scan across all RDD-CEO-* and COLLECTOR *
  organisations statewide (keywords like amdar nidhi, aamdar, MLA fund,
  DPDC, jilha niyojan)

The portal's own search is captcha protected, so all watching goes through
the captcha-free Tenders-by-Organisation listing.

## How it works

1. `tracker.py` scrapes the Tenders-by-Organisation listing, follows the WSSD
   row and diffs the live tender ids against `seen.json`.
2. For each new tender it parses all detail page fields and builds:
   - `MJP_<tenderid>_EN.pdf` (English, full caption on WhatsApp)
   - `MJP_<tenderid>_MR.pdf` (Marathi, short Marathi caption)
3. Marathi field values come from deep-translator (Google Translate, free).
   Field labels and section headings use a hardcoded English-to-Marathi
   dictionary. Numbers, dates, tender ids, reference numbers, pincodes and
   amounts stay verbatim. The Marathi PDF uses the vendored Noto Sans
   Devanagari fonts in `fonts/` and is sanity checked with pypdf for
   Devanagari codepoints before sending.
4. A tender is marked seen only after the English send succeeds. If the
   Marathi pipeline fails, the English PDF still goes out and the error is
   logged.

## Setup

Repository secrets read by `.github/workflows/tracker.yml`. Configure email,
WhatsApp, or both; at least one channel is required.

| Secret | Value |
| --- | --- |
| `SMTP_USER` | Email delivery: sending address, e.g. yourname@gmail.com |
| `SMTP_PASS` | Email delivery: SMTP password. For Gmail create an App Password (Google Account, Security, 2-Step Verification, App passwords) and paste it here |
| `SMTP_HOST` | Optional, default smtp.gmail.com |
| `SMTP_PORT` | Optional, default 465 (SSL) |
| `EMAIL_TO` | Optional destination address, defaults to `SMTP_USER` |
| `WHATSAPP_TOKEN` | Meta Cloud API access token |
| `WHATSAPP_PHONE_ID` | Phone number id of the sending number |
| `WHATSAPP_TO` | Destination number, international format, digits only |

Email sends one message per tender with both PDFs attached. WhatsApp sends
the English PDF first with a full caption, then the Marathi PDF with a short
Marathi caption.

The workflow runs every 30 minutes and commits `seen.json` back to the repo.

## Local dashboard

```
.venv/bin/python tracker.py --serve
```

Serves http://localhost:8765: stat tiles, search plus watch and status
filters over every tender ever tracked (live and historical), published,
closing and opening dates, and closing-soon badges. Clicking a row opens a
detail panel with every parsed field and buttons that generate the English
and Marathi PDFs on demand. The page re-scrapes the portal at most every
15 minutes and auto-refreshes; the Refresh button forces it.

Details are cached in `details_cache.json` (also filled by the GitHub
Actions runs for every notified tender), so historical tenders stay
inspectable after they leave the portal.

`seen.json` is pre-seeded with the tenders that were live when the repo was
created, so the first scheduled run only sends tenders published after that.
Delete entries from `seen.json` (or the whole file) to have those tenders
sent again.

## Local dry run

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tracker.py --dry-run
```

Scrapes one live tender, builds both PDFs into `out/` and skips WhatsApp
entirely, so you can eyeball the output without burning messages.

## Fonts

`fonts/` vendors Noto Sans Devanagari and Noto Sans (Copyright 2022 The Noto
Project Authors), licensed under the SIL Open Font License 1.1, see
`fonts/OFL.txt` and `fonts/OFL-NotoSans.txt`. The Devanagari fonts carry the
Marathi text; the Latin fonts render tender ids, dates and any untranslated
fallbacks inside the Marathi PDF, since Noto Sans Devanagari has no Latin
letter glyphs.
