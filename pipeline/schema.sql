-- Canonical schema for the Maharashtra public-tender pipeline.
-- Backend-neutral SQL kept to the SQLite subset so the local default store and
-- a future Postgres/Supabase backend can share it. Types are simple; the store
-- layer handles JSON as TEXT.

CREATE TABLE IF NOT EXISTS tenders (
  id                 TEXT PRIMARY KEY,          -- uuid
  source_portal      TEXT NOT NULL,
  source_tender_id   TEXT NOT NULL,
  extra_source_ids   TEXT,                      -- json: other portals' ids after dedupe
  dedupe_hash        TEXT,
  publishing_org     TEXT,
  org_hierarchy      TEXT,                      -- json: {department, agency, region, division}
  category           TEXT,                      -- works|goods|services|consultancy
  title              TEXT,
  description        TEXT,
  estimated_value_inr TEXT,                      -- exact decimal string, NULL=unknown
  emd_inr            TEXT,
  tender_fee_inr     TEXT,
  publish_date       TEXT,
  bid_submission_end TEXT,
  bid_opening_date   TEXT,
  pre_bid_date       TEXT,
  district           TEXT,
  taluka             TEXT,
  location_raw       TEXT,
  funding_source     TEXT,                      -- state|css|world_bank|adb|own_funds|unknown
  funding_scheme     TEXT,                      -- JJM, PMGSY, AMRUT, DPDC, Amdar Nidhi, ...
  funding_level      TEXT,                      -- state|central|local
  status             TEXT,                      -- live|closed|cancelled|retendered|awarded|unknown
  retender_of        TEXT REFERENCES tenders(id),
  raw                TEXT,                      -- json
  first_seen_at      TEXT,
  last_seen_at       TEXT,
  UNIQUE (source_portal, source_tender_id)
);
CREATE INDEX IF NOT EXISTS idx_tenders_org ON tenders(publishing_org);
CREATE INDEX IF NOT EXISTS idx_tenders_district ON tenders(district);
CREATE INDEX IF NOT EXISTS idx_tenders_status ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_tenders_dedupe ON tenders(dedupe_hash);

CREATE TABLE IF NOT EXISTS tender_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_id   TEXT NOT NULL REFERENCES tenders(id),
  event_type  TEXT NOT NULL,                    -- status_change|corrigendum|extension|cancelled
  detail      TEXT,
  event_at    TEXT,
  source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_tender ON tender_events(tender_id);

CREATE TABLE IF NOT EXISTS documents (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_id  TEXT REFERENCES tenders(id),
  doc_type   TEXT,                              -- nit|boq|corrigendum|aoc|agenda|other
  language   TEXT,                              -- en|mr
  url        TEXT,
  local_path TEXT,
  sha256     TEXT,
  ocr_text   TEXT,
  fetched_at TEXT,
  UNIQUE (tender_id, doc_type, url)
);

CREATE TABLE IF NOT EXISTS contractors (
  id                 TEXT PRIMARY KEY,          -- uuid
  canonical_name     TEXT NOT NULL,
  match_key          TEXT UNIQUE,               -- normalized grouping key
  aliases            TEXT,                      -- json array
  pan                TEXT,
  gstin              TEXT,
  registration_class TEXT,
  home_district      TEXT,
  first_seen         TEXT,
  last_seen          TEXT
);

CREATE TABLE IF NOT EXISTS awards (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_id             TEXT REFERENCES tenders(id),
  source_tender_id      TEXT,
  contractor_id         TEXT REFERENCES contractors(id),
  contractor_name_raw   TEXT,
  award_value_inr       TEXT,                     -- exact decimal string, NULL=unknown
  award_date            TEXT,
  work_order_no         TEXT,
  completion_period_days INTEGER,
  bidder_count          INTEGER,                  -- bidders WE observed (see bidder_coverage)
  bidder_coverage       TEXT,                     -- winner_only|full: is bidder_count trustworthy?
  l1_pct_vs_estimate    REAL,
  source_url            TEXT,
  raw                   TEXT,
  UNIQUE (source_tender_id)
);
CREATE INDEX IF NOT EXISTS idx_awards_contractor ON awards(contractor_id);

CREATE TABLE IF NOT EXISTS bids (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tender_id     TEXT REFERENCES tenders(id),
  source_tender_id TEXT,
  bidder_name   TEXT,
  contractor_id TEXT REFERENCES contractors(id),
  quoted_value_inr TEXT,                          -- exact decimal string, NULL=unknown
  rank          TEXT,
  status        TEXT,
  UNIQUE (source_tender_id, bidder_name)
);

-- Contractor merges in the 85..95 fuzzy band await manual confirmation.
CREATE TABLE IF NOT EXISTS contractors_review (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name_raw      TEXT,
  candidate_id  TEXT REFERENCES contractors(id),
  candidate_name TEXT,
  score         REAL,
  resolved      INTEGER DEFAULT 0,
  UNIQUE (name_raw, candidate_id)
);

CREATE TABLE IF NOT EXISTS metrics_award_coverage (
  org        TEXT,
  year       INTEGER,
  closed     INTEGER,
  awarded    INTEGER,
  coverage   REAL,
  computed_at TEXT,
  PRIMARY KEY (org, year)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  portal    TEXT,
  started   TEXT,
  finished  TEXT,
  new_count INTEGER DEFAULT 0,
  upd_count INTEGER DEFAULT 0,
  errors    INTEGER DEFAULT 0,
  note      TEXT
);

-- Resumable backfill checkpoints (portal + cursor -> last position).
CREATE TABLE IF NOT EXISTS backfill_checkpoints (
  portal     TEXT NOT NULL,
  cursor_key TEXT NOT NULL,
  position   TEXT,
  done       INTEGER DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (portal, cursor_key)
);

-- Analytics views ---------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_floated_value_by_org_month AS
  SELECT publishing_org AS org,
         substr(publish_date, 8, 4) || '-' ||
           substr(publish_date, 4, 3) AS month,
         COUNT(*) AS tenders,
         SUM(COALESCE(estimated_value_inr, 0)) AS floated_value_inr
  FROM tenders
  WHERE status != 'retendered' OR status IS NULL
  GROUP BY org, month;

CREATE VIEW IF NOT EXISTS v_award_ratio AS
  SELECT t.publishing_org AS org,
         a.contractor_name_raw AS contractor,
         t.estimated_value_inr AS estimated,
         a.award_value_inr AS awarded,
         CASE WHEN t.estimated_value_inr > 0
              THEN 1.0 * a.award_value_inr / t.estimated_value_inr END AS ratio
  FROM awards a JOIN tenders t ON t.id = a.tender_id;

CREATE VIEW IF NOT EXISTS v_top_contractors AS
  SELECT c.canonical_name AS contractor,
         COUNT(a.id) AS contracts,
         SUM(COALESCE(a.award_value_inr, 0)) AS total_value_inr
  FROM awards a JOIN contractors c ON c.id = a.contractor_id
  GROUP BY c.id
  ORDER BY total_value_inr DESC;

-- Single-bidder rate is only meaningful where we actually observed the full
-- bidder list. Winner-only imports (the AOC page lists only the winner) are
-- EXCLUDED: one known winner is not evidence of one participant.
CREATE VIEW IF NOT EXISTS v_single_bidder AS
  SELECT t.publishing_org AS org,
         COUNT(*) AS awarded,
         SUM(CASE WHEN a.bidder_count = 1 THEN 1 ELSE 0 END) AS single_bidder,
         1.0 * SUM(CASE WHEN a.bidder_count = 1 THEN 1 ELSE 0 END) / COUNT(*)
           AS single_bidder_rate
  FROM awards a JOIN tenders t ON t.id = a.tender_id
  WHERE a.bidder_coverage = 'full'
  GROUP BY org;
