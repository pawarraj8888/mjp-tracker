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

-- =========================================================================
-- Upcoming MJP Projects: official approvals tracked BEFORE tender publication.
-- Projects, the official documents that evidence them, dated approval events,
-- and (suggested/confirmed) links to procurement tenders are kept as separate
-- records so the app represents only what the evidence establishes. A GR may
-- carry several projects; a project may have several GRs and several tenders --
-- these many-to-many relationships are modelled explicitly, never collapsed.
-- =========================================================================

CREATE TABLE IF NOT EXISTS mjp_projects (
  id                 TEXT PRIMARY KEY,          -- uuid5(municipality|theme)
  title_original     TEXT,                      -- Marathi, as published
  title_en           TEXT,                      -- plain-English summary
  municipality       TEXT,
  district           TEXT,
  scheme             TEXT,                      -- वैशिष्ट्यपूर्ण, JJM, AMRUT, MSJNM, ...
  mjp_office         TEXT,                      -- responsible MJP division/region
  mjp_role           TEXT,                      -- implementing_agency|tendering_authority|
                                                --   technical_sanction|mentioned|unknown
  mjp_role_evidence  TEXT,                      -- supporting passage (verbatim)
  mjp_role_page      INTEGER,                   -- page number of the passage
  mjp_role_confidence REAL,                     -- 0..1
  approved_cost_inr  TEXT,                      -- exact decimal string, NULL=unknown
  revised_cost_inr   TEXT,
  funds_released_inr TEXT,
  tender_estimate_inr TEXT,                     -- kept separate from approved cost
  work_theme         TEXT,                      -- water_supply|sewerage|roads|other
  status_label       TEXT,                      -- precise label (see export.py)
  primary_doc_code   TEXT,
  missing            TEXT,                      -- json: fields we could not extract
  uncertainties      TEXT,                      -- json: extraction/matching caveats
  first_detected_at  TEXT,
  last_checked_at    TEXT,
  raw                TEXT
);
CREATE INDEX IF NOT EXISTS idx_mjp_projects_district ON mjp_projects(district);
CREATE INDEX IF NOT EXISTS idx_mjp_projects_scheme ON mjp_projects(scheme);

CREATE TABLE IF NOT EXISTS mjp_documents (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id       TEXT REFERENCES mjp_projects(id),
  doc_code         TEXT UNIQUE,                 -- सांकेतांक (unique GR document code)
  gr_number        TEXT,                        -- शासन निर्णय क्रमांक
  department        TEXT,                        -- issuing department
  doc_type         TEXT,                         -- administrative_approval|technical_sanction|
                                                 --   funding_sanction|revised_sanction|
                                                 --   fund_release|other
  title_original   TEXT,
  language         TEXT,                         -- mr|en
  issue_date       TEXT,                         -- date printed on the document
  publication_date TEXT,                         -- verified portal publish date, if known
  first_detected_at TEXT,                        -- when WE first saw it (never = publication)
  last_checked_at  TEXT,
  url              TEXT,                          -- original official URL
  local_path       TEXT,                          -- durable stored copy
  sha256           TEXT,                          -- checksum of the stored original
  page_refs        TEXT,                          -- json: relevant page numbers
  ocr_used         INTEGER DEFAULT 0,
  extract_ok       INTEGER DEFAULT 1,             -- 0 if text extraction was empty/failed
  raw              TEXT
);
CREATE INDEX IF NOT EXISTS idx_mjp_documents_project ON mjp_documents(project_id);

CREATE TABLE IF NOT EXISTS mjp_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id     TEXT NOT NULL REFERENCES mjp_projects(id),
  event_type     TEXT NOT NULL,                  -- project_discovered|administrative_approval_recorded|
                                                 --   technical_sanction_recorded|funding_sanctioned|
                                                 --   funds_released|revised_sanction|tender_suggested|
                                                 --   tender_linked|tender_published|tender_awarded
  detail         TEXT,                            -- json
  event_date     TEXT,                            -- from the document; order is NOT assumed
  source_doc_code TEXT,
  source         TEXT,
  created_at     TEXT,
  UNIQUE (project_id, event_type, source_doc_code, event_date)
);
CREATE INDEX IF NOT EXISTS idx_mjp_events_project ON mjp_events(project_id);

CREATE TABLE IF NOT EXISTS mjp_tender_links (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id       TEXT NOT NULL REFERENCES mjp_projects(id),
  source_portal    TEXT,
  source_tender_id TEXT,
  link_status      TEXT,                          -- linked|suggested|rejected
  confidence       REAL,                          -- 0..1
  reasons          TEXT,                           -- json: matched signals w/ evidence
  amount_note      TEXT,                           -- never assumes a diff is GST
  created_at       TEXT,
  confirmed_at     TEXT,
  confirmed_by     TEXT,
  UNIQUE (project_id, source_tender_id)
);
CREATE INDEX IF NOT EXISTS idx_mjp_links_project ON mjp_tender_links(project_id);
CREATE INDEX IF NOT EXISTS idx_mjp_links_tender ON mjp_tender_links(source_tender_id);

-- Uncertain extractions / suggested matches for human confirmation.
CREATE TABLE IF NOT EXISTS mjp_review_queue (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_type  TEXT,                                -- project_role_uncertain|tender_match_suggested|
                                                  --   extraction_low_confidence
  ref        TEXT,                                 -- project id or doc code
  reason     TEXT,
  detail     TEXT,                                 -- json
  score      REAL,
  resolved   INTEGER DEFAULT 0,
  UNIQUE (item_type, ref)
);

-- Per-recipient, per-event alert delivery tracking. An alert is only "sent"
-- once its attachments were actually attached; parts are numbered when a large
-- attachment set is split. Mirrored to a committed JSON log for durability so
-- the stateless host never re-sends after a restart.
CREATE TABLE IF NOT EXISTS mjp_alerts (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key      TEXT,                            -- stable: project|event_type|value-signature
  recipient      TEXT,
  part_no        INTEGER DEFAULT 1,
  parts_total    INTEGER DEFAULT 1,
  status         TEXT,                            -- pending|sent|failed
  attachments_ok INTEGER DEFAULT 0,
  is_backfill    INTEGER DEFAULT 0,
  attempts       INTEGER DEFAULT 0,
  last_error     TEXT,
  sent_at        TEXT,
  updated_at     TEXT,
  UNIQUE (event_key, recipient, part_no)
);
