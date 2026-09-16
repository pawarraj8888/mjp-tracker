# Upcoming MJP Projects

Tracks Maharashtra Jeevan Pradhikaran (MJP) projects from official approval and
funding Government Resolutions **before** their tenders are published, and links
each approval to a tender as it appears.

## What was implemented

| Piece | Module |
|-------|--------|
| Devanagari-digit + lakh/crore exact-decimal parsing | `money.py` (`normalize_digits`, `parse_indian_amount`) |
| Data model (projects, documents, approval events, tender links, review queue, alert log) | `pipeline/schema.sql` (`mjp_*` tables) |
| PDF evidence extraction (pypdf; guarded OCR) | `pipeline/mjp/extract.py` |
| GR discovery + direct PDF download | `pipeline/mjp/gr_source.py` |
| Persistence (idempotent, deterministic ids) | `pipeline/mjp/store.py` |
| Project↔tender matching (cautious) | `pipeline/mjp/match.py` |
| Orchestration + durability | `pipeline/mjp/ingest.py` |
| Dashboard feed | `pipeline/mjp/export.py` → `mjp_projects.json` |
| Email alerts with PDF attachments | `pipeline/mjp/alerts.py` |
| Verification harness (Chikhaldara) | `pipeline/mjp/verify.py` |
| CLI | `python -m pipeline mjp [ingest\|export\|alerts\|verify]` |
| Dashboard section | `dashboard_page.py` ("Upcoming MJP" nav/list/detail), injected by `tracker.py` |
| Scheduled job | `.github/workflows/tracker.yml` ("Refresh MJP projects") |

## Sources checked

* **Government Resolutions portal** — `https://gr.maharashtra.gov.in/1145/Government-Resolutions`.
  * The default listing grid (department, title, code, GR date, PDF link for the
    most-recent GRs across all departments) is fetched by a plain GET — **no
    captcha** — and filtered to Urban Development (`नगर विकास`) + Water Supply &
    Sanitation (`पाणीपुरवठा व स्वच्छता`), plus any title mentioning MJP / water
    supply. Verified reachable and parsed.
  * Any GR PDF is downloadable directly by its URL — verified on the real
    Chikhaldara GR (`202606251057023925`, HTTP 200, 343 KB).
* MJP's role in each project is established from the PDF body (not the listing),
  with the supporting passage and page retained.

## Monitoring schedule

* Runs inside the existing GitHub Actions cron, **every 15 minutes** (target:
  hourly; the actual cadence is shown on the dashboard and in `data_status`).
* Runs unattended (no browser needed). Discovery is idempotent and resumable
  from `mjp_store/index.json`; each run re-extracts from the stored originals,
  so the derived state is always reproducible.

## Where files are stored (durable)

The hosted dashboard is stateless (reads committed JSON via
`raw.githubusercontent`), so durability = git, mirroring the rest of the app:

* `mjp_store/documents/<code>.pdf` — **original** government PDFs, unchanged,
  with a stored sha256 (durable file storage, not a temp dir).
* `mjp_store/index.json` — discovery ledger (code, url, dept, title, date,
  sha256, first-detected, last-checked).
* `mjp_store/seeds.json` — explicit GR codes for the resumable backfill and the
  verification case (listing metadata only; findings are extracted from the PDF).
* `mjp_store/alerts.json` — per-recipient/per-event delivery log (dedup + retry).
* `mjp_store/link_decisions.json` — human confirm/reject overrides for matches.
* `mjp_projects.json` (repo root) — the derived dashboard feed.

## How matching works

`match.py` scores each (project, tender) pair on **auditable** signals:
GR/document-code reference (decisive), the project's municipality named in the
tender, MJP as the tender's agency, distinctive work-component overlap, district,
and an exact scheme-name match. A match is **auto-confirmed ("Tender linked")**
only on decisive evidence (a GR/code reference, or town + MJP-agency +
distinctive-component overlap). Anything weaker is a **"Possible tender match"**
for human confirmation. A similar town name alone, or an approximately matching
amount alone, is never enough; an amount difference is **never** assumed to be
GST. Approved cost, revised cost, funds released and tender estimate are kept as
separate fields.

## Alerts

Recipients `pawarraj8888@gmail.com`, `sppawar.sachin7777@gmail.com` (override via
`MJP_ALERT_TO`). An alert fires for a project whose **own** documented value is
**strictly above ₹1 crore** (unknown-value projects stay visible but never
qualify). The **original official PDFs are attached** (named `OFFICIAL-GR-*`),
not linked. The initial import is a single clearly-labelled **backfill digest**
per recipient, not a flood; thereafter each new qualifying approval / revision /
confirmed tender link alerts individually. Delivery is tracked per
(event, recipient); repeated runs never re-send. An alert is recorded "sent"
only when its attachments were actually attached — otherwise it stays **pending**
and retries.

## Coverage gaps / honest limits

* **Deep 12-month departmental history is not auto-crawlable.** The portal's
  department/date **search** requires a CAPTCHA (`txtimgcode`), and its result
  **pagination** fails viewstate-MAC validation because the site is load-balanced
  without sticky sessions. Neither is bypassed. History therefore comes from the
  seed list (`mjp_store/seeds.json`) or manual assist; incremental discovery of
  newly published GRs (the primary goal) is fully automated.
* **Scanned GRs need OCR.** pypdf handles text-layer PDFs (the common case). A
  scanned GR needs Tesseract `eng+mar`; the OCR path exists but is a no-op until
  the binary is installed, and such a document is flagged for review rather than
  treated as empty.
* **Broken embedded Marathi font.** Some GR PDFs render Devanagari with a broken
  ToUnicode map; the extractor matches both the correct and mangled spellings
  plus English, and low-confidence cases go to the review queue.

## Credentials / configuration still required

| Secret | Enables | Without it |
|--------|---------|-----------|
| `SMTP_USER`, `SMTP_PASS` (repo secrets) | Actual email delivery | Alerts are prepared and recorded **pending**, never falsely "sent" |
| `SMTP_HOST`/`SMTP_PORT` (optional) | Non-Gmail SMTP | Defaults to Gmail SSL |
| `MJP_ALERT_TO` (optional) | Override recipients | Falls back to the two addresses above |
| `MJP_ALLOWED_HOSTS` (optional) | Extend the download allowlist to other official government hosts | Downloads are pinned to `gr.maharashtra.gov.in` |
| Tesseract `eng+mar` (optional) | OCR for scanned GRs | Scanned docs flagged for review |

**Download safety.** A document URL is untrusted input, so `gr_source.download_pdf`
pins the host to an allowlist (default `gr.maharashtra.gov.in`), forces https,
rejects any host that resolves to a non-public address, does not follow
redirects, and caps the body at 40 MB. `doc_code` is validated to digits before
any file path is built from it.

Email delivery is **not** claimed active: it activates only once `SMTP_USER` /
`SMTP_PASS` are configured and a real send is verified.

## Verification

`python -m pipeline mjp verify` runs a fresh ingest and asserts the Chikhaldara
reference facts (code, ₹24,99,25,000, 25-06-2026, implementing agency, funding
sanction, water-supply works) **and** that tender `2026_COJAL_1337629_1` is a
*possible* match (never auto-linked, with the scheme/amount caveats). All checks
pass. Unit tests: `tests/test_mjp_*.py`.
