# Assumptions

Engineering decisions taken without stopping to ask, per the run-to-completion
brief. Each can be overridden.

## Infrastructure
- **Storage is NOT Supabase.** The repo owner previously gave an explicit,
  standing instruction for this project: *"do not use the clearline vercel or
  supabase — use my personal one"* (no Supabase). Supabase also needs
  credentials / a paid project that are not available in this environment.
  Decision: storage goes behind a `pipeline/store` interface with a local
  JSON/SQLite-or-Postgres backend by default; a Supabase backend is stubbed and
  listed in BLOCKERS. Flip by providing `SUPABASE_URL` +
  `SUPABASE_SERVICE_ROLE_KEY` and selecting the supabase backend.
- **ECC is already installed** (`ecc@ecc`, user scope). We use the installed
  plugin rather than re-running `npx ecc-universal install`, to avoid
  overwriting the existing setup.
- Hosted dashboard stays on the owner's **personal Vercel** project
  `tender-watch` (scope rpawar-2515s-projects, region bom1). No new hosting.

## Architecture
- The existing `tracker.py` (single-file MJP tracker + dashboard) is kept
  working as-is; the broader pipeline is added as a `pipeline/` package plus
  `adapters/<portal>/` without breaking the current MJP WhatsApp/email path or
  the Contractors dashboard built this session.
- Canonical schema from the brief is the target; it is introduced with Alembic
  once a real DB backend is selected. Until then the file store mirrors the same
  record shapes as JSON.

## Scope / sequencing
- This is a multi-week program; it is delivered phase-by-phase and verifiably,
  not as a single-turn completion. Definition-of-Done items are only checked
  when actually verified.
- Tier 1 (mahatenders / GePNIC) is completed and verified before Tier 2.
- Award/contractor data on mahatenders is captcha- and keyword-gated, so it is
  collected via the existing human-in-the-loop import (see
  `docs/portals/mahatenders.md`), not fully automated.

## Politeness / legality
- Max 1 request / 2s per portal, exponential backoff on 429/5xx, realistic UA.
- Captchas are never defeated with third-party solvers; gated items go to a
  manual queue. See `docs/COMPLIANCE.md`.
