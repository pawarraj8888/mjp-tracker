import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from pipeline import api, ingest
from pipeline.store import Store


def _serve(db_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), api.make_handler(db_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_api_server_end_to_end(tmp_path, sample_state):
    dbp = str(tmp_path / "api.sqlite")
    s = Store(dbp)
    ingest.ingest_from_state(s, "mahatenders", sample_state)
    s.close()

    srv, port = _serve(dbp)
    try:
        base = "http://127.0.0.1:%d" % port
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert json.load(r)["ok"] is True
        with urllib.request.urlopen(base + "/analytics/top-contractors?limit=5",
                                    timeout=5) as r:
            assert json.load(r)[0]["contracts"] == 2
        with urllib.request.urlopen(base + "/contractors", timeout=5) as r:
            assert json.load(r)
        got_404 = False
        try:
            urllib.request.urlopen(base + "/nope", timeout=5)
        except urllib.error.HTTPError as e:
            got_404 = e.code == 404
        assert got_404
    finally:
        srv.shutdown()
