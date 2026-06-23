"""Live arena server — each tick runs a fresh round and the season climbs. The
tick core is socket-free + deterministic; an optional HTTP smoke is lenient.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from game.arena import scanner
from game.arena.serve import _handler
from game.arena.serve import ArenaServer


def test_tick_advances_rounds_and_season(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"))
    r1 = s.tick()
    r2 = s.tick()
    r3 = s.tick()
    assert (r1.round_no, r2.round_no, r3.round_no) == (1, 2, 3)
    assert r3.season_rounds == 3
    assert r3.season_board                                  # standings populated


def test_season_accumulates_across_ticks(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"))
    s.tick()
    top1 = s.season.leaderboard()[0].total_emissions
    s.tick()
    top2 = s.season.leaderboard()[0].total_emissions
    assert top2 > top1                                      # emissions grow each round
    assert s.season.leaderboard()[0].streak == 2


def test_html_is_live_arena(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"))
    s.tick()
    html = s.html()
    assert "<!doctype html>" in html
    assert "CATHEDRAL ARENA" in html and "Season" in html
    assert 'http-equiv="refresh"' in html                  # auto-ticks on its own


def test_round_timer_is_real_not_cosmetic(tmp_path):
    """The arena countdown must match the real refresh cadence (a fresh round
    ticks on each reload) and name the next round — not a fake fixed 90s clock."""
    from game.arena.engine import ArenaEngine
    from game.arena.ui import render
    r = ArenaEngine().run(3)
    html = render(r, refresh_secs=6)
    assert 'http-equiv="refresh" content="6"' in html       # meta refresh = cadence
    assert '<span id="rtimer">6</span>' in html             # countdown starts at the cadence
    assert "R4 in" in html                                  # names the real next round (3 -> 4)
    assert '>90</span>' not in html                         # the fake fixed clock is gone
    # the countdown and the meta refresh are coupled to the same number
    assert render(r, refresh_secs=3).count('content="3"') == 1


def test_scanner_game_html_is_api_backed(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    html = s.scanner_game_html()
    assert "SUBNET BREAKER" in html
    assert "/api/scanner/catalog" in html
    assert "/api/scanner/agent/solve" in html
    assert "/api/scanner/replay" in html
    assert "/api/scanner/submit" in html
    assert "/api/scanner/state" in html
    assert "replay verifies; seal writes ledger" in html


def test_season_persists_and_resumes(tmp_path):
    p = str(tmp_path / "season.json")
    s1 = ArenaServer(season_path=p)
    s1.tick(); s1.tick()
    rounds_before = s1.season.rounds
    s2 = ArenaServer(season_path=p)                          # fresh server, same file
    assert s2.season.rounds == rounds_before                # resumed the season
    assert s2.round == rounds_before


def test_scanner_api_methods_verify_proof_not_report(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    task = s.scanner_task(0)
    assert task["schema"] == scanner.SCHEMA_TASK
    assert task["required_fields"]

    example = s.scanner_example(0)
    assert example["verdict"]["accepted"] is True
    assert example["verdict"]["score"] > 0

    report_only = s.scanner_submit({
        "task_id": task["task_id"],
        "miner_hotkey": "hk_report_only",
        "nonce": task["nonce"],
        "proof_family": task["expected_family"],
        "witness": None,
        "report": "Correct category, no proof.",
    })
    assert report_only["accepted"] is False
    assert report_only["score"] == 0.0
    assert report_only["gates"]["family_aligned"] is True
    assert report_only["gates"]["replay_succeeds"] is False

    solved = s.scanner_agent_solve(0, "hk_agent")
    assert solved["schema"] == "cathedral.scanner.agent_solution.v1"
    assert solved["scored"] is False
    assert solved["mode"] == "valid"
    assert solved["submission"]["miner_hotkey"] == "hk_agent"
    assert solved["submission"]["task_id"] == task["task_id"]

    forged = s.scanner_agent_solve(0, "hk_agent", "bad_witness")
    assert forged["mode"] == "bad_witness"
    forged_replay = s.scanner_replay(forged["submission"])
    assert forged_replay["accepted"] is False
    assert forged_replay["scored"] is False
    assert forged_replay["ledger_written"] is False
    assert forged_replay["gates"]["replay_succeeds"] is False

    replayed = s.scanner_replay(solved["submission"])
    assert replayed["accepted"] is True
    assert replayed["scored"] is False
    assert replayed["ledger_written"] is False

    verdict = s.scanner_submit(solved["submission"])
    assert verdict["accepted"] is True
    assert verdict["score"] > 0


def test_scanner_http_endpoints(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        task = json.loads(urlopen(base + "/api/scanner/task?index=0", timeout=10).read())
        assert task["schema"] == scanner.SCHEMA_TASK

        catalog = json.loads(urlopen(base + "/api/scanner/catalog?limit=2", timeout=10).read())
        assert catalog["count"] == 2 and len(catalog["tasks"]) == 2

        game = urlopen(base + "/game", timeout=10).read().decode()
        assert "SUBNET BREAKER" in game
        assert "/api/scanner/agent/solve" in game
        assert "/api/scanner/replay" in game
        assert "/api/scanner/submit" in game
        assert "data-action=\"forge\"" in game

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        try:
            build_opener(NoRedirect).open(base + "/dashboard.html", timeout=10)
            raise AssertionError("legacy dashboard route did not redirect")
        except HTTPError as e:
            assert e.code == 302
            assert e.headers["location"] == "/game"

        alias = urlopen(base + "/dashboard.html", timeout=10).read().decode()
        assert "SUBNET BREAKER" in alias

        solved = json.loads(urlopen(
            base + "/api/scanner/agent/solve?index=0&miner_hotkey=hk_http_agent",
            timeout=10,
        ).read())
        assert solved["scored"] is False
        assert solved["submission"]["miner_hotkey"] == "hk_http_agent"
        assert solved["submission"]["task_id"] == task["task_id"]

        forged = json.loads(urlopen(
            base + "/api/scanner/agent/solve?index=0&miner_hotkey=hk_http_agent&mode=bad_witness",
            timeout=10,
        ).read())
        assert forged["mode"] == "bad_witness"
        forged_req = Request(base + "/api/scanner/replay",
                             data=json.dumps(forged["submission"]).encode(),
                             headers={"content-type": "application/json"},
                             method="POST")
        forged_replay = json.loads(urlopen(forged_req, timeout=10).read())
        assert forged_replay["accepted"] is False
        assert forged_replay["ledger_written"] is False

        replay_req = Request(base + "/api/scanner/replay",
                             data=json.dumps(solved["submission"]).encode(),
                             headers={"content-type": "application/json"},
                             method="POST")
        replayed = json.loads(urlopen(replay_req, timeout=10).read())
        assert replayed["accepted"] is True
        assert replayed["scored"] is False
        assert replayed["ledger_written"] is False
        assert json.loads(urlopen(base + "/api/scanner/submissions?limit=10", timeout=10).read())["count"] == 0

        example = json.loads(urlopen(base + "/api/scanner/example?index=0", timeout=10).read())
        payload = dict(example["submission"])
        payload["report"] = "report text is allowed but ignored by scoring"
        req = Request(base + "/api/scanner/submit",
                      data=json.dumps(payload).encode(),
                      headers={"content-type": "application/json"},
                      method="POST")
        verdict = json.loads(urlopen(req, timeout=10).read())
        assert verdict["accepted"] is True
        assert verdict["score"] > 0

        req2 = Request(base + "/api/scanner/submit",
                       data=json.dumps(payload).encode(),
                       headers={"content-type": "application/json"},
                       method="POST")
        duplicate = json.loads(urlopen(req2, timeout=10).read())
        assert duplicate["accepted"] is False
        assert duplicate["score"] == 0.0
        assert duplicate["gates"]["not_duplicate_credit"] is False

        board = json.loads(urlopen(base + "/api/scanner/leaderboard", timeout=10).read())
        assert board["count"] == 1
        assert board["miners"][0]["score"] == verdict["score"]
        assert board["miners"][0]["accepted"] == 1
        assert board["miners"][0]["rejected"] == 1

        state = json.loads(urlopen(base + "/api/scanner/state?miner_hotkey=hk_example", timeout=10).read())
        assert state["schema"] == scanner.SCHEMA_STATE
        assert state["score"] == verdict["score"]
        assert state["accepted"] == 1
        assert state["rejected"] == 1
        assert task["task_id"] in state["accepted_task_ids"]

        submissions = json.loads(urlopen(base + "/api/scanner/submissions?limit=10", timeout=10).read())
        assert submissions["count"] == 2
        assert "duplicate_task_credit" in submissions["submissions"][0]["reasons"]
    finally:
        httpd.shutdown()
        httpd.server_close()
