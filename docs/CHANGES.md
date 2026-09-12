# Procurement Intelligence Upgrade — Change Summary

Upgrade of Tender Watch (https://tender-watch-eta.vercel.app/) into a
procurement-intelligence dashboard. This documents what changed, the data
correction and backfill results, verification, and the honest remaining limits.

## 1. Data audit and correction (the core fix)

**Root cause.** `parse_inr()` (tracker.py) and its duplicate `_inr()`
(pipeline/ingest.py) parsed money with `re.sub(r"[^\d]", "", text)`, which
deletes the decimal point. `INR 4,291,550.826` was stored as `4291550826`.
Because the number of decimal places varies per record, **no single divisor can
undo it** — it required a decimal-aware parser.

**Fix.** New leaf module `money.py`: exact `Decimal` parsing with Indian and
international grouping, currency/NBSP stripping, accounting negatives, and
unknown → `None` (never `0`). Amounts are serialized as exact decimal strings;
raw source text is preserved; presentation rounds only for display. Every money
path (tracker, adapter, ingest, store, analytics, dashboard) was rewired.

**Reversible, audited backfill** (`backfill_money.py`):
- Entries scanned: **100**; corrected: **38**; unchanged: **62** (whole-rupee
  amounts, which the old parser happened to store correctly).
- Snapshot `awards.pre_money_fix.json` written once (full reversibility).
- Audit log `corrections.json` records every old→new change with raw text.
- Idempotent: a second run makes zero changes.

The three examples from the brief now reconcile across `/api/detail`, the
Awards view, the contractor profiles, and the exports:

| Tender | Raw | Was stored | Now |
|---|---|---|---|
| 2026_CCCA_1284937_1 | `INR 4,291,550.826` | 4291550826 | **4291550.826** (₹42.92 L) |
| 2026_BEED_1268857_1 | `INR 17,497,799.08` | 1749779908 | **17497799.08** (₹1.75 Cr) |
| 2026_JALGA_1294526_16 | `INR 2,252,666.17` | 225266617 | **2252666.17** (₹22.53 L) |

**Recomputed totals** (all exact-arithmetic): awarded value fell from a falsely
inflated **~₹1,434 Cr to ₹35.27 Cr** (≈40× correction); floated (estimated)
value from ~₹1,529 Cr to ₹251 Cr (the estimate parser was also fixed, and award
amounts are no longer substituted for missing estimates). Value bands, district
totals, contractor rankings and averages all recomputed. Changes are labelled
**data corrections**, not new awards, in the notification stream.

## 2. Population reconciliation

The three numbers you saw (132 / 313 / 287) were three different producers
counting different things at different times: `seen.json` (one watch's seen
set), the live+keyword dashboard merge (time-varying), and a frozen pipeline
snapshot. The pipeline now defines the **canonical tender count** (distinct
tenders after cross-portal dedupe) and exposes it with explicit definitions in
`analytics.json → reconciliation` (canonical, live, awarded, merged-across-
portals). The dashboard and Analytics read the same export, so they agree.

## 3. Bidding metrics — made honest

The AOC page lists only the winner, so the old "100% single-bidder rate" was an
artifact of winner-only data. Awards now carry `bidder_coverage`
(`winner_only` | `full`). Single-bidder rate is computed **only over
full-coverage awards**, with the denominator and the winner-only count shown.
Today: 100/100 awards are winner-only, so the rate is reported as **"Insufficient
data"** rather than a false 100%.

## 4. Lifecycle, events, notifications

- An imported award now updates the tender's supported status to `awarded` and
  records a `tender_events` row, idempotently. A genuine transition (`awarded`)
  is distinguished from discovering an already-awarded tender
  (`award_record_discovered`) — no fabricated prior "Open" state.
- `notifications.json` is generated server-side from events + the correction
  log, with stable ids and kinds kept distinct (procurement / historical_import
  / data_correction), so the initial migration never masquerades as new awards.
- `data_status.json` reports freshness in IST: last collection, relative age,
  last attempt, last material change, next scheduled run, per-source health.

## 5. Dashboard

Rewritten as a professional SPA (`dashboard_page.py`): Overview, Tenders,
Awards (new), Contractors, Analytics, plus Watchlist and a notification Inbox +
bell. Freshness top bar (IST + status badge + honest Refresh). KPI tiles carry
definitions and periods. Estimated vs awarded value are always separate.
Analytics uses fixed 0–100% scales, shows zero as an empty bar, and labels
unknowns instead of zeroing them. Detail slide-over shows the award panel (with
raw source amount as evidence), a lifecycle timeline, and EN/MR PDFs.

## 6. Verification

- **67 pytest tests pass** (money parser incl. the 3 brief examples + grouping +
  unknown handling; lifecycle events; notifications; reconciliation; bidding
  denominator), ~85% coverage on the core modules.
- Live production verified: `/api/detail` returns the corrected values with raw
  text; Analytics shows ₹35.27 Cr awarded and "Insufficient data" single-bidder;
  no browser console errors.
- English/Marathi PDFs, the human-in-the-loop award import, and the existing
  source coverage remain functional.

## 7. Remaining limitations (not faked)

- **Server-side persistence** is delivered as cron-generated committed JSON
  (browserless, survives reloads/deploys — correct for this single-operator
  tool). Watchlist and notification read-state are per-browser (localStorage).
  True multi-user server persistence needs a database, which is out of scope per
  the standing "no Supabase / personal Vercel" rule (see BLOCKERS #1, #10).
- **Bidder competition** cannot be measured until a source publishes full bid
  lists (BLOCKERS #7); winner-only data is labelled as such.
- **Tier-2/Tier-3 adapters, OCR, and the 12-month statewide backfill** remain
  as previously scoped (BLOCKERS #4, #7, #8).
