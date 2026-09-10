"""Maharashtra public-tender pipeline.

A canonical store (SQLite by default, Supabase/Postgres pluggable), portal
adapters, contractor entity resolution, tender dedupe, and analytics, built on
top of the existing single-file MJP tracker (``tracker.py``) which remains the
mahatenders scraper and dashboard.

See ``docs/ASSUMPTIONS.md`` and ``docs/BLOCKERS.md`` for scope and decisions.
"""

__all__ = ["store", "text", "entities", "dedupe", "ingest", "analytics"]
