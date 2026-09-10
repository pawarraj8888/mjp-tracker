# Portal research: mahatenders.gov.in (NIC GePNIC / Tapestry)

Maharashtra state e-procurement, built on NIC GePNIC (Apache Tapestry front
end). This is Tier 1 and the backbone of the pipeline. Findings below are from
direct inspection this session (the MJP tracker and the awards import run
against the live portal).

## Access model
- **Tenders by Organisation** (`page=FrontEndTendersByOrganisation`) is
  **captcha-free**. It lists every organisation with live tenders and links to
  each org's live tender list. This is the reliable watch route and already
  powers the tracker (all ~170 orgs are scanned).
- Detail pages use **session-scoped DirectLinks** (`component=$DirectLink`,
  `sp=<token>`); links are only valid inside the session that produced the
  listing, so each listing must be followed within one session.
- Full-text **search, archive, tenders-by-location, corrigendum, status and the
  Results-of-Tenders section are all captcha-gated.** The MIS site
  (gepnicreports.gov.in) is login-gated. There is no RSS/JSON feed.
- SSL: the portal intermittently serves an incomplete cert chain; the client
  retries once with verification disabled.

## Tender listing structure
Org tender-list rows parse to: tender id (`^\d{4}_[A-Z0-9]+_\d+_\d+$`), title,
ref no, published / closing / opening datetimes (`%d-%b-%Y %I:%M %p`), org
chain (`A||B||C`), and the detail DirectLink. Title/ref/id come in one cell as
`[title] [ref][tenderid]` and are split by anchoring on the trailing tender id.

## Results of Tenders / AOC (awards)
- The Results section **requires a search term** (keyword ≥ 4 chars, or an exact
  tender id) **plus a captcha**. An empty search returns the server error
  *"Please Enter a Valid Tender ID or Keyword."* (rendered in
  `table.message_box` / `td.alerttext` — the reliable error signal; the page's
  inline JS also contains `alert("Invalid Captcha!...")` which must be ignored).
- A keyword search returns a flat results table:
  `S.No | AOC Date | e-Published Date | Title (DirectLink) | Organisation Chain | AOC (DirectLinks)`.
  Each row's **Title link** opens the **AOC Summary** page; the AOC column has
  further links (an AOC summary and the signed Letter-of-Award page).
- The **AOC Summary** page carries the award facts as caption/field pairs
  (`Total Contract Value :`, `Contract Date :`, `Work Completion Period ...`,
  `AOC document : <tenderid>.pdf`) plus an **"Awarded Bids List"** table:
  `S.No | Bid Number | Bidder Name | Awarded Currency | Awarded Value`.
  The Bidder Name is the winning contractor. Only the winner is listed here;
  losing bidders are not exposed on this page.
- Import is human-in-the-loop: a person solves the portal's captcha at
  `/unlock`, then the crawl walks the matching rows in that session, extracts
  the award facts, and writes `awards.json`. Every fetched page is dumped to
  `results_raw/` so extraction can be rebuilt offline (`tracker.py --reparse`).
  Each import covers one keyword (portal rule); run again per keyword to widen.

## Generalisation plan (Tier 1)
- Live + corrigenda: already covered for all orgs via the captcha-free listing.
- Archive (by org/year) and full AOC coverage: captcha-gated → same
  human-in-the-loop unlock, iterated per keyword/org, with `results_raw/` dumps
  feeding a deterministic reparser. Metadata for everything; PDFs only above the
  backfill value threshold.
- Canonical mapping: org chain → {department, agency, region/circle, division};
  district/taluka from Location + org chain + title heuristics (already
  implemented in `normalize_city` / `derive_city`).

## Rate / politeness
1 req / 2s, backoff on 429/5xx, one-hour cool-off if blocked. Captcha never
auto-solved (see COMPLIANCE.md).
