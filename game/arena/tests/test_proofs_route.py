"""GET /proofs serves the visual Proof Board live (companion to /api/scanner/differential)."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from game.arena.serve import ArenaServer, _handler


def test_proofs_route_serves_the_board(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for route in ("/proofs", "/proofs.html"):
            page = urlopen(base + route, timeout=10).read().decode()
            assert "Proof Board" in page
            assert "proven discriminator" in page
            assert "pinned invariants" in page
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_home_route_serves_the_simple_hub(tmp_path):
    """GET /home and /start serve the clean simple front page."""
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for route in ("/home", "/start"):
            page = urlopen(base + route, timeout=10).read().decode()
            assert "Cathedral Arena" in page and "Play the game" in page
            assert 'href="/game"' in page and 'href="/proofs"' in page
    finally:
        httpd.shutdown()
        httpd.server_close()
