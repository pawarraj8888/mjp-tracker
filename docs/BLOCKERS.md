# Blockers

Things that need the repo owner or external accounts. Each has a stub or
default so the rest of the system still runs.

| # | Blocker | Why it's blocked | Stub / default in place |
|---|---------|------------------|-------------------------|
| 1 | **Supabase Postgres** | Owner previously said "no Supabase"; no `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` available | `pipeline/store` interface with local JSON/SQLite backend; Supabase backend stubbed. Provide keys to enable. |
| 2 | **Manual captchas** (mahatenders Results/AOC, and likely other NIC portals) | Captchas must not be auto-solved | Human-in-the-loop `/unlock` import already built; gated docs go to a `documents_pending_manual` queue |
| 3 | **Anthropic API key for LLM extraction** | LLM fallback for field extraction needs `ANTHROPIC_API_KEY` | Regex-first extraction runs without it; LLM step is skipped when the key is absent |
| 4 | **Tesseract OCR** (`eng+mar`) | Scanned Marathi PDFs need the Tesseract binary + Marathi language pack installed | pdfplumber text extraction runs first; OCR path is guarded and no-ops with a logged warning if Tesseract is missing |
| 5 | **WhatsApp Cloud API secrets** | `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_ID` / `WHATSAPP_TO` not set | Email (SMTP) path and dashboard work without them; alerts stay pending until configured (unchanged from today) |
| 6 | **SMTP secrets** | `SMTP_USER` / `SMTP_PASS` not set | Dashboard + state refresh run without delivery; new tenders stay pending |
| 7 | **Tier 2 / Tier 3 portal access** (MSEDCL, MMRDA, BMC, CIDCO, MSRDC, MIDC, PMC/PCMC/NMC etc.; GeM, CPPP, IREPS) | Each needs its own research, some need login or have aggressive bot protection | Config-driven source registry; adapters added one at a time behind the same canonical schema; unbuilt portals simply absent from the registry |
| 8 | **Full 12-month statewide backfill** | Long-running, rate-limited crawl across ~170 orgs plus archive/AOC; not completable in a single session | Resumable backfill runner with DB checkpointing so it can run locally/on a VM over time |
| 9 | **Scale / time** | The full brief is a multi-week program | Delivered in phases; this file + ASSUMPTIONS track real status |
| 10 | **Per-user server-side notification/watch persistence** | Production is stateless Flask on Vercel reading committed JSON; no DB (no Supabase, per standing rule) | Notification/event streams are generated server-side by the cron and committed as JSON (browserless, survive reloads/deploys). Per-viewer read-state and watchlists use localStorage. A DB backend would make these truly multi-user server-persistent. |
| 11 | **MJP alert email delivery** | Needs `SMTP_USER` / `SMTP_PASS` repo secrets | Alerts are fully built (with original-PDF attachments) and recorded **pending**; nothing is falsely reported "sent". Set the secrets to activate delivery. See docs/MJP.md. |
| 12 | **Full 12-month MJP GR backfill** | The GR portal's department/date search is CAPTCHA-gated and its pagination fails viewstate-MAC (load-balanced, no sticky sessions); neither is auto-solved (verified: the search postback throws a NullReference because the server-session captcha value is dropped across load-balancer nodes) | Discovery now has three captcha-free paths: the portal listing, a **search index over the portal** (`pipeline/mjp/search_discovery.py`), and a curated GR-URL list (`mjp_store/gr_candidates.json`). All flow through the same MJP-role gate. |
| 14 | **Continuous GR search index key** | The search-index discovery uses the Google Programmable Search JSON API, which needs a free key + engine id (`GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX`) | Without the key, discovery still runs on the captcha-free listing + curated `gr_candidates.json`; the coverage note in the Upcoming MJP section states plainly whether the search index is active. Add the two repo secrets to widen continuous search. |
| 13 | **OCR for scanned MJP GRs** (`eng+mar`) | Needs the Tesseract binary + Marathi pack | pypdf text extraction handles text-layer PDFs (the common case); the OCR path is guarded and a scanned GR is flagged for review rather than read as empty. |
