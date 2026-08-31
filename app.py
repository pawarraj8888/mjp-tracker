"""Vercel entry point for the dynamic tender dashboard (Flask).

Thin wrapper around tracker.py. State (seen.json, live.json,
details_cache.json) is read from the GitHub repo raw URLs via
STATE_REMOTE_BASE, so every 15-minute tracker run updates the hosted
dashboard without a redeploy. Live org-watch tenders are scraped from the
portal on demand with a 15 minute in-memory cache."""

import os
import tempfile

if os.environ.get("VERCEL"):
    os.environ.setdefault(
        "STATE_REMOTE_BASE",
        "https://raw.githubusercontent.com/pawarraj8888/mjp-tracker/main")
    os.environ.setdefault(
        "OUT_DIR", os.path.join(tempfile.gettempdir(), "tender-out"))

import tracker  # noqa: E402

from flask import Flask, Response, jsonify, request  # noqa: E402

app = Flask(__name__)


@app.get("/")
def index():
    tracker.refresh_live(force="refresh" in request.args)
    return Response(tracker.build_dashboard_page(),
                    mimetype="text/html; charset=utf-8")


@app.get("/api/detail")
def detail():
    tid = request.args.get("id", "")
    if not tracker.TENDER_ID_RE.match(tid):
        return jsonify({"ok": False, "error": "bad tender id"})
    details = tracker.fetch_detail_for_tid(tid)
    if details is None:
        return jsonify({"ok": False, "error":
                        "This tender is no longer on the portal and no "
                        "cached details exist for it."})
    return jsonify({"ok": True, "details": details})


@app.get("/pdf/<tid>/<lang>")
def pdf(tid, lang):
    if lang not in ("en", "mr") or not tracker.TENDER_ID_RE.match(tid):
        return Response("Not found", status=404)
    path = tracker.dashboard_pdf(tid, lang)
    if path is None:
        return Response("No details available for this tender", status=404)
    with open(path, "rb") as fh:
        body = fh.read()
    return Response(body, mimetype="application/pdf", headers={
        "Content-Disposition": 'inline; filename="%s"' % path.name})
