"""Smoke test: the documented UI surface loads on a fresh arena server.

This is the codified version of "make sure it loads and is running": one test
boots the arena server and asserts every human-facing route plus the health probe
responds, so a regression that breaks any single view is caught immediately.
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener, urlopen

from game.arena.serve import ArenaServer, _handler


# The four views plus the plain hub and how-to page.
PAGE_ROUTES = [
    ("/home", "Cathedral Arena"),
    ("/game", "SUBNET BREAKER"),
    ("/howto", "How to Play"),
    ("/proofs", "Proof Board"),
    ("/arena", "CATHEDRAL ARENA"),
]


def test_every_documented_page_route_loads(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for route, needle in PAGE_ROUTES:
            page = urlopen(base + route, timeout=10).read().decode()
            assert needle in page, f"{route} did not render (missing {needle!r})"

        assert urlopen(base + "/healthz", timeout=10).status == 200

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *a):
                return None

        try:
            build_opener(NoRedirect).open(base + "/", timeout=10)
        except HTTPError as e:
            assert e.code == 302 and e.headers["location"] == "/game"
    finally:
        httpd.shutdown()
        httpd.server_close()
