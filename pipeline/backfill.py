"""Resumable backfill runner.

Checkpoints live in ``backfill_checkpoints`` so a long crawl survives
interruption: each unit of work (here, one polling pass) records its cursor and
done flag, and a restart skips finished cursors.

mahatenders live/corrigenda metadata is scraped through the tracker. The
archive and full AOC history are captcha-gated, so they are collected through
the human-in-the-loop import (see docs/portals/mahatenders.md) rather than an
unattended crawl; this runner backfills what is reachable unattended and records
the gated portion as a pending checkpoint.
"""

from __future__ import annotations

import argparse
import time

from . import ingest
from .store import Store


def run(portal: str, from_year: int | None, store: Store | None = None) -> dict:
    store = store or Store()
    cursor = "live-metadata"
    cp = store.checkpoint_get(portal, cursor)
    if cp and cp.get("done"):
        return {"skipped": cursor, "reason": "already done this pass"}

    if portal == "mahatenders":
        from .adapters import mahatenders
        recs = mahatenders.fetch_live()
        new, upd, _alias = ingest.ingest_tenders(store, portal, recs)
        # awards backfill (archive/AOC) is captcha-gated -> mark pending
        store.checkpoint_set(portal, "aoc-archive", str(from_year or ""),
                             done=False, updated_at=_now())
        store.checkpoint_set(portal, cursor, _now(), done=True,
                             updated_at=_now())
        store.record_run(portal, _now(), _now(), new, upd, 0,
                         note="backfill live metadata")
        return {"portal": portal, "new": new, "updated": upd,
                "note": "AOC/archive backfill is captcha-gated; use the "
                        "/unlock import per keyword (checkpoint aoc-archive "
                        "left pending)."}
    raise NotImplementedError("backfill for %s not implemented yet" % portal)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Resumable tender backfill")
    ap.add_argument("--portal", default="mahatenders")
    ap.add_argument("--from-year", type=int, default=None)
    args = ap.parse_args(argv)
    result = run(args.portal, args.from_year)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
