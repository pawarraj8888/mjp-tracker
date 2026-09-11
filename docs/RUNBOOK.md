# Runbook

## Pipeline (canonical store + analytics)

Local SQLite store at `data/pipeline.db` (gitignored; regenerate any time from
the committed `awards.json` / `live.json` / `seen.json` / `details_cache.json`).

```bash
.venv/bin/python -m pipeline init-db          # create schema
.venv/bin/python -m pipeline ingest           # tracker state -> canonical store
.venv/bin/python -m pipeline stats            # counts + total awarded value
.venv/bin/python -m pipeline analytics top    # top contractors by value
.venv/bin/python -m pipeline analytics single-bidder
.venv/bin/python -m pipeline analytics coverage
.venv/bin/python -m pipeline analytics ratio|floated|retender|time-to-award|pairs
.venv/bin/python -m pipeline serve-api --port 8790   # read-only JSON API
.venv/bin/python -m pipeline export-analytics        # write analytics.json for the dashboard
.venv/bin/python -m pipeline review                  # list contractor merges awaiting a human
.venv/bin/python -m pipeline retender                # (via ingest) link cancelled->new tenders
```

`export-analytics` writes `analytics.json` (committed, read via
`STATE_REMOTE_BASE`), which powers the dashboard's **Analytics** tab
(value bands, top districts, funding schemes, single-bidder rate, top
contractors, award coverage). Regenerate after each ingest:
`python -m pipeline ingest && python -m pipeline export-analytics`, then commit
`analytics.json`.

Analytics API routes (stdlib http): `/health`, `/summary`,
`/analytics/top-contractors`, `/analytics/floated-value`,
`/analytics/award-ratio`, `/analytics/single-bidder`, `/analytics/coverage`,
`/analytics/retender-rate`, `/analytics/time-to-award`,
`/analytics/contractor-pairs`, `/contractors`, `/tenders`.

## Restarting the backfill

```bash
.venv/bin/python -m pipeline backfill --portal mahatenders --from-year 2013
```

Backfill checkpoints live in `backfill_checkpoints`; a finished cursor is
skipped on restart. mahatenders live/corrigenda metadata is crawled unattended;
the archive/AOC history is captcha-gated and collected via the human-in-the-loop
import (below), which leaves the `aoc-archive` checkpoint pending.

## Importing award / contractor data (human in the loop)

Run the tracker dashboard (`python tracker.py --serve`), open `/unlock`, enter a
keyword (>= 4 letters) or exact tender id, solve the portal captcha. The crawl
writes `awards.json` and dumps pages to `results_raw/`. Then rebuild and
re-ingest:

```bash
.venv/bin/python tracker.py --reparse    # rebuild awards.json from dumps
.venv/bin/python -m pipeline ingest      # load into the canonical store
```

## Reviewing contractor merges

Fuzzy matches in the 85..95 band are queued, not auto-merged:

```bash
sqlite3 data/pipeline.db \
 "SELECT name_raw, candidate_name, score FROM contractors_review WHERE resolved=0"
```

Confirm a merge by setting the raw name's contractor `match_key` to the
candidate's and marking the row `resolved=1`; reject by marking `resolved=1`
only. (A small review CLI is a planned follow-up.)

## Adding a portal

1. Research it first: write `docs/portals/<portal>.md` (page structure,
   pagination, session/viewstate, rate limits, captcha, any RSS/JSON).
2. Add an entry to `sources/registry.yaml`.
3. Implement `pipeline/adapters/<portal>.py` emitting the canonical dicts
   (`fetch_live`, `fetch_awards`); reuse `pipeline/text` helpers.
4. Save real page samples under `tests/fixtures/<portal>/` and add parser tests.
5. Register the adapter in `pipeline/ingest.ADAPTERS`.

## Switching storage to Supabase/Postgres

Not enabled (see docs/BLOCKERS.md). Provide `SUPABASE_URL` +
`SUPABASE_SERVICE_ROLE_KEY`, implement a Postgres backend in `pipeline/store.py`
against the same `schema.sql`, and set `PIPELINE_DB=supabase`.
