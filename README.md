# mjp-tracker

Tracks Maharashtra Jeevan Pradhikaran tenders on mahatenders.gov.in
(organisation node "Member Secretary(WSSD),Mumbai"). For every new tender it
parses the detail page, builds a work-details PDF in English and a second one
translated into Marathi, and sends both to WhatsApp via the Meta Cloud API.

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

Repository secrets needed by `.github/workflows/tracker.yml`:

| Secret | Value |
| --- | --- |
| `WHATSAPP_TOKEN` | Meta Cloud API access token |
| `WHATSAPP_PHONE_ID` | Phone number id of the sending number |
| `WHATSAPP_TO` | Destination number, international format, digits only |

The workflow runs every 30 minutes and commits `seen.json` back to the repo.

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
