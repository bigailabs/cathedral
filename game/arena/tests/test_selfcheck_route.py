"""GET /api/selfcheck and /healthz report live arena health.

Returns the operator self-check JSON with HTTP 200 when healthy and 503 when not,
so a monitor can health-check the server with a single request.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from game.arena.serve import ArenaServer, _handler


def test_selfcheck_endpoint_reports_healthy(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for route in ("/api/selfcheck", "/healthz"):
            resp = urlopen(base + route, timeout=10)
            assert resp.status == 200
            payload = json.loads(resp.read())
            assert payload["ok"] is True
            names = {c["name"] for c in payload["checks"]}
            assert "replay_is_a_real_gate" in names and "multi_model_coverage" in names
            assert not payload["required_failed"]
    finally:
        httpd.shutdown()
        httpd.server_close()
