"""CLI for the pipeline.

    python -m pipeline init-db
    python -m pipeline ingest [--portal mahatenders]
    python -m pipeline stats
    python -m pipeline analytics [top|floated|ratio|single-bidder|coverage|
                                  retender|time-to-award|pairs] [--limit N]
    python -m pipeline serve-api [--port 8790]
    python -m pipeline backfill --portal mahatenders [--from-year 2013]
"""

from __future__ import annotations

import argparse
import json

from . import analytics, backfill, ingest
from .store import Store


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--portal", default="mahatenders")
    sub.add_parser("stats")
    p_an = sub.add_parser("analytics")
    p_an.add_argument("view", nargs="?", default="top")
    p_an.add_argument("--limit", type=int, default=25)
    p_api = sub.add_parser("serve-api")
    p_api.add_argument("--port", type=int, default=8790)
    p_bf = sub.add_parser("backfill")
    p_bf.add_argument("--portal", default="mahatenders")
    p_bf.add_argument("--from-year", type=int, default=None)
    args = ap.parse_args(argv)

    if args.cmd == "init-db":
        store = Store()
        _print({"initialised": store.path, "counts": store.counts()})
        return 0
    if args.cmd == "ingest":
        store = Store()
        _print(ingest.ingest_from_state(store, args.portal))
        return 0
    if args.cmd == "stats":
        store = Store()
        _print(analytics.summary(store))
        return 0
    if args.cmd == "analytics":
        store = Store()
        views = {
            "top": lambda: analytics.top_contractors(store, args.limit),
            "floated": lambda: analytics.floated_value_by_org_month(store, args.limit),
            "ratio": lambda: analytics.award_ratio(store, args.limit),
            "single-bidder": lambda: analytics.single_bidder(store),
            "coverage": lambda: analytics.coverage(store),
            "retender": lambda: analytics.retender_rate(store),
            "time-to-award": lambda: analytics.time_to_award(store),
            "pairs": lambda: analytics.contractor_pairs(store, args.limit),
        }
        fn = views.get(args.view)
        if fn is None:
            ap.error("unknown analytics view %r; choose %s"
                     % (args.view, ", ".join(views)))
        _print(fn())
        return 0
    if args.cmd == "serve-api":
        from . import api
        api.serve(args.port)
        return 0
    if args.cmd == "backfill":
        _print(backfill.run(args.portal, args.from_year))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
