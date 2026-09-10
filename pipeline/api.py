"""Read-only analytics API.

Implemented on the Python standard library ``http.server`` so it runs with no
extra dependencies (the brief names FastAPI; FastAPI/uvicorn are not installed
in this environment, recorded in docs/ASSUMPTIONS.md - the routes and JSON shape
are the same and can be lifted into FastAPI when that dependency is added).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import analytics
from .store import Store


def _limit(q, default: int) -> int:
    try:
        v = int(q.get("limit", [str(default)])[0])
    except (ValueError, TypeError):
        return default
    return v if v >= 0 else default


ROUTES = {
    "/health": lambda s, q: {"ok": True, "counts": s.counts()},
    "/summary": lambda s, q: analytics.summary(s),
    "/analytics/top-contractors":
        lambda s, q: analytics.top_contractors(s, _limit(q, 25)),
    "/analytics/floated-value": lambda s, q: analytics.floated_value_by_org_month(s),
    "/analytics/award-ratio": lambda s, q: analytics.award_ratio(s),
    "/analytics/single-bidder": lambda s, q: analytics.single_bidder(s),
    "/analytics/coverage": lambda s, q: analytics.coverage(s),
    "/analytics/retender-rate": lambda s, q: analytics.retender_rate(s),
    "/analytics/time-to-award": lambda s, q: analytics.time_to_award(s),
    "/analytics/contractor-pairs": lambda s, q: analytics.contractor_pairs(s),
    "/contractors": lambda s, q: s.query(
        "SELECT canonical_name, aliases, gstin, home_district FROM contractors"
        " ORDER BY canonical_name"),
    "/tenders": lambda s, q: s.query(
        "SELECT source_tender_id, publishing_org, title, estimated_value_inr,"
        " district, status FROM tenders ORDER BY estimated_value_inr DESC"
        " LIMIT ?", (_limit(q, 100),)),
}


def make_handler(db_path: str | None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            fn = ROUTES.get(u.path)
            if fn is None:
                self._send(404, {"error": "not found", "routes": sorted(ROUTES)})
                return
            store = Store(db_path)
            try:
                self._send(200, fn(store, parse_qs(u.query)))
            except Exception as exc:  # keep the endpoint honest about failures
                self._send(500, {"error": str(exc)[:200]})
            finally:
                store.close()

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


def serve(port: int = 8790, db_path: str | None = None) -> None:
    print("Analytics API on http://127.0.0.1:%d (routes: %s)"
          % (port, ", ".join(sorted(ROUTES))))
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path)).serve_forever()
