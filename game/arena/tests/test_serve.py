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
    assert "/api/scanner/request" in html
    assert "Scan Request" in html
    assert 'id="intakeId"' in html
    assert "routed_tasks" in html
    assert "subnet-breaker-operator" in html
    assert "https://github.com/cathedralai/subnet-targets" in html
    assert "/api/scanner/agent/solve" in html
    assert "task_id='+encodeURIComponent(t.task_id)" in html
    assert "/api/scanner/replay" in html
    assert "/api/scanner/attest" in html
    assert "/api/scanner/submit-attested" in html
    assert "/api/scanner/submit" in html
    assert "/api/scanner/state" in html
    assert "/api/scanner/benchmark" in html
    assert 'id="killRate"' in html
    assert 'id="combo"' in html
    assert 'id="sweepText"' in html
    assert 'id="sweepFill"' in html
    assert 'id="coreAction"' in html
    assert 'id="breachPct"' in html
    assert 'id="objectiveText"' in html
    assert 'id="objectiveFill"' in html
    assert 'id="failureText"' in html
    assert 'id="bestBounty"' in html
    assert 'id="safestTarget"' in html
    assert 'data-strategy="stealth"' in html
    assert 'data-strategy="balanced"' in html
    assert 'data-strategy="overclock"' in html
    assert "const playerParam = new URLSearchParams(location.search).get('player');" in html
    assert "if(!playerParam)localStorage.scannerPlayer = player" in html
    assert "function newRun(){const p='hk_player_'+Math.random().toString(16).slice(2)" in html
    assert 'onclick="newRun()"' in html
    assert "data-action=\"report\"" in html
    assert "REPLAY REJECTED" in html and "state.phase=2" in html
    assert "state.heat=Math.max(0,state.heat-20)" in html
    assert "if(name==='attest'){spend(c[0],c[1]);if(await runAttestation())state.phase=5}" in html
    assert "if(name==='submit'){spend(c[0],c[1]);await seal()}" in html
    assert "attest is local simulated TEE; seal writes ledger" in html
    assert "$('coreAction').onclick=()=>enqueue(nextAction())" in html
    assert "flash('breached')" in html


def test_scanner_game_html_preserves_play_loop_contract(tmp_path):
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    html = s.scanner_game_html()

    # The playable loop must stay action-first, not regress into a read-only board.
    assert "const phases = ['probe','encode','solve','replay','attest','seal','sealed'];" in html
    assert "fetch('/api/scanner/request',{method:'POST'" in html
    assert "state.tasks=intake.routed_tasks||[]" in html
    assert "task_id='+encodeURIComponent(t.task_id)" in html
    assert "agent solve failed" in html
    assert "function renderIntake()" in html
    for action in ("probe", "encode", "solve", "replay", "attest", "submit", "report", "forge", "decoy"):
        assert f'data-action="{action}"' in html
    assert "document.addEventListener('keydown'" in html
    assert "function nextAction()" in html
    assert "function validActions()" in html
    assert "function renderActions()" in html
    assert "classList.toggle('armed'" in html
    assert "classList.toggle('locked'" in html
    assert "ARMED" in html
    assert 'id="riskText"' in html
    assert "function targetRisk(t=current())" in html
    assert "function actionCost(name)" in html
    assert "function focusHeat(name,base)" in html
    assert "if(base<=0)return 0" in html
    assert "const strategies = {stealth:{label:'stealth',energy:1.25,heat:.7}" in html
    assert "function strategyCost(name,base)" in html
    assert "function actionCostText(name)" in html
    assert "cost.textContent=actionCostText(a)" in html
    assert '<span class="cost"></span>' in html
    assert "+E12 / -H28" in html
    assert "attested:{}, lastGates:null" in html
    assert "async function runAttestation()" in html
    assert "/api/scanner/attest" in html
    assert "fetch('/api/scanner/submit-attested'" in html
    assert "proof.claim={...(proof.claim||{}),attestation:verdict.receipt}" in html
    assert "REJECTED: attestation receipt has not been bound" in html
    assert "simulated local TEE" in html
    assert "attestation_receipt" in html
    assert "sweep:0, turns:0" in html
    assert "function sweepDelta(name)" in html
    assert "function advanceSweep(name)" in html
    assert "validator sweep caught noisy route: one gate burned" in html
    assert "focus '+state.combo.toFixed(1)" in html
    assert "your ledger score '+(state.score/1000).toFixed(1)" in html
    assert "global #1 '+board.miners[0].miner_hotkey" in html
    assert "<span>focus</span><b id=\"combo\">x1.0</b>" in html
    assert "goal:5, maxBlocks:5" in html
    assert "function finishCampaign(title,body)" in html
    assert "function renderCampaign()" in html
    assert "CONTRACT WON" in html
    assert "TRACE BURNED" in html
    assert "if(!state.tasks.length||state.campaignEnded)return" in html
    assert "validator sweep punishes noisy routes" in html
    assert "sweep '+Math.round(state.sweep)+'%'" in html
    assert "$('sweepText').textContent=Math.round(state.sweep)+'%'" in html
    assert "$('sweepFill').style.width=Math.min(100,state.sweep)+'%'" in html
    assert "target risk '+targetRisk(current())+' now prices every gate" in html
    assert "$('core').style.setProperty('--progress'" in html
    assert ".node{position:absolute;z-index:3;width:132px;height:82px" in html
    assert "return {left:50+Math.cos(a)*42,top:50+Math.sin(a)*41}" in html
    assert "n.style.left=`calc(${p.left}% - 66px)`" in html
    assert "n.title=t.objective" in html
    assert "<small>+${Math.round(t.bounty_weight*1000)} bounty / risk ${risk}</small>" in html
    assert "<small>${t.objective}</small>" not in html
    assert ".sidebar{min-height:0;display:grid;grid-template-rows:minmax(0,1fr) minmax(132px,160px)" in html

    # Strategy modes make route execution a resource decision, while replay remains the verifier.
    assert '<div class="strategy"><button data-strategy="stealth"' in html
    assert "strategy:'balanced'" in html
    assert "function setStrategy(name)" in html
    assert "function renderStrategy()" in html
    assert "renderStrategy();renderNodes()" in html
    assert "document.querySelectorAll('[data-strategy]').forEach(b=>b.onclick=()=>setStrategy(b.dataset.strategy))" in html
    assert "if(e.key==='s')setStrategy('stealth')" in html
    assert "if(e.key==='b')setStrategy('balanced')" in html
    assert "if(e.key==='o')setStrategy('overclock')" in html
    assert "with '+state.strategy+' routing" in html
    assert "'local game identity; '+state.strategy+' route; simulated attestation; no chain writes'" in html

    # The route planner makes target selection an explicit game decision.
    assert '<div class="planner"><button id="bestBounty"' in html
    assert "function openTargetIndexes()" in html
    assert "function bestTarget(mode)" in html
    assert "function routeLabel(i)" in html
    assert "function renderPlanner()" in html
    assert "function selectBest(mode)" in html
    assert "renderPlanner();renderStrategy();renderNodes()" in html
    assert "$('bestBounty').onclick=()=>selectBest('bounty')" in html
    assert "$('safestTarget').onclick=()=>selectBest('safe')" in html
    assert "route planner selected '+routeLabel(i)" in html

    # Report/forge paths are negative controls: they can run, but replay must catch them.
    assert "await runAgentSolve('report_only')" in html
    assert "await runAgentSolve('bad_witness')" in html
    assert "decode_map_present:false" in html
    assert "REPLAY REJECTED" in html
    assert "flash('reject')" in html

    # Seal is gated by prior replay + attestation and accepted seals move to the next subnet.
    assert "if(!state.replayOk[t.task_id])" in html
    assert "log('REJECTED: replay gate has not passed','bad')" in html
    assert "if(!state.attested[t.task_id])" in html
    assert "await fetch('/api/scanner/submit-attested'" in html
    assert "const n=nextOpen();if(n<0){$('end').classList.add('show')}else{state.selected=n;state.phase=0;state.lastGates=null;log('advanced to next subnet target','warn')}" in html

    # Reloads and completed seasons should not strand the player on cleared targets.
    assert "function resumeOpenTarget()" in html
    assert "resumeOpenTarget();" in html
    assert "location.pathname+'?player='+encodeURIComponent(p)" in html
    assert 'onclick="newRun()"' in html


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

    request = s.scanner_request({
        "repo": "https://github.com/acme/subnet",
        "commit": "abc123",
        "objective": "Find reproducible incentive bugs.",
        "max_tasks": 2,
    })
    assert request["schema"] == scanner.SCHEMA_SCAN_INTAKE
    assert request["scored"] is False
    assert request["ledger_written"] is False
    assert request["routed_count"] == 2
    assert request["verifier_policy"]["requires_replay_task"] is True

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

    solved = s.scanner_agent_solve(999, "hk_agent", task_id=task["task_id"])
    assert solved["schema"] == "cathedral.scanner.agent_solution.v1"
    assert solved["scored"] is False
    assert solved["mode"] == "valid"
    assert solved["submission"]["miner_hotkey"] == "hk_agent"
    assert solved["submission"]["task_id"] == task["task_id"]
    assert solved["submission"]["claim"]["schema"] == scanner.SCHEMA_CLAIM

    forged = s.scanner_agent_solve(999, "hk_agent", "bad_witness", task_id=task["task_id"])
    assert forged["mode"] == "bad_witness"
    forged_replay = s.scanner_replay(forged["submission"])
    assert forged_replay["accepted"] is False
    assert forged_replay["scored"] is False
    assert forged_replay["ledger_written"] is False
    assert forged_replay["gates"]["replay_succeeds"] is False

    report_only = s.scanner_agent_solve(999, "hk_agent", "report_only", task_id=task["task_id"])
    assert report_only["mode"] == "report_only"
    report_replay = s.scanner_replay(report_only["submission"])
    assert report_replay["accepted"] is False
    assert report_replay["scored"] is False
    assert report_replay["gates"]["family_aligned"] is True
    assert report_replay["gates"]["decode_map_present"] is False

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

        scan_req = Request(
            base + "/api/scanner/request",
            data=json.dumps({
                "requester": "http-customer",
                "repo": "https://github.com/acme/subnet",
                "commit": "abc123",
                "objective": "Find replayable bugs.",
                "max_tasks": 2,
            }).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        intake = json.loads(urlopen(scan_req, timeout=10).read())
        assert intake["schema"] == scanner.SCHEMA_SCAN_INTAKE
        assert intake["scored"] is False
        assert intake["ledger_written"] is False
        assert intake["routed_count"] == 2

        game = urlopen(base + "/game", timeout=10).read().decode()
        assert "SUBNET BREAKER" in game
        assert "/api/scanner/request" in game
        assert "Scan Request" in game
        assert 'id="intakeMeta"' in game
        assert "routed_tasks" in game
        assert "/api/scanner/agent/solve" in game
        assert "/api/scanner/replay" in game
        assert "/api/scanner/attest" in game
        assert "/api/scanner/submit-attested" in game
        assert "/api/scanner/submit" in game
        assert "/api/scanner/benchmark" in game
        assert 'id="killRate"' in game
        assert 'id="combo"' in game
        assert 'id="gates"' in game
        assert "Verifier gates" in game
        assert "renderGates()" in game
        assert "const phases = ['probe','encode','solve','replay','attest','seal','sealed'];" in game
        assert "resumeOpenTarget()" in game
        assert "resumed next uncleared target" in game
        assert "advanced to next subnet target" in game
        assert "data-action=\"report\"" in game
        assert "data-action=\"forge\"" in game

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        for legacy_route in ("/", "/dashboard.html"):
            try:
                build_opener(NoRedirect).open(base + legacy_route, timeout=10)
                raise AssertionError(f"{legacy_route} did not redirect to game")
            except HTTPError as e:
                assert e.code == 302
                assert e.headers["location"] == "/game"

        alias = urlopen(base + "/dashboard.html", timeout=10).read().decode()
        assert "SUBNET BREAKER" in alias
        root = urlopen(base + "/", timeout=10).read().decode()
        assert "SUBNET BREAKER" in root
        arena = urlopen(base + "/arena", timeout=10).read().decode()
        assert "CATHEDRAL ARENA" in arena and "Attack Map" in arena

        solved = json.loads(urlopen(
            base + "/api/scanner/agent/solve?index=999&task_id=" + task["task_id"] + "&miner_hotkey=hk_http_agent",
            timeout=10,
        ).read())
        assert solved["scored"] is False
        assert solved["submission"]["miner_hotkey"] == "hk_http_agent"
        assert solved["submission"]["task_id"] == task["task_id"]
        assert solved["submission"]["claim"]["schema"] == scanner.SCHEMA_CLAIM

        forged = json.loads(urlopen(
            base + "/api/scanner/agent/solve?task_id=" + task["task_id"] + "&miner_hotkey=hk_http_agent&mode=bad_witness",
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

        report_only = json.loads(urlopen(
            base + "/api/scanner/agent/solve?task_id=" + task["task_id"] + "&miner_hotkey=hk_http_agent&mode=report_only",
            timeout=10,
        ).read())
        assert report_only["mode"] == "report_only"
        report_req = Request(base + "/api/scanner/replay",
                             data=json.dumps(report_only["submission"]).encode(),
                             headers={"content-type": "application/json"},
                             method="POST")
        report_replay = json.loads(urlopen(report_req, timeout=10).read())
        assert report_replay["accepted"] is False
        assert report_replay["gates"]["decode_map_present"] is False

        try:
            urlopen(base + "/api/scanner/agent/solve?task_id=missing", timeout=10)
            raise AssertionError("unknown task_id did not 404")
        except HTTPError as e:
            assert e.code == 404

        replay_req = Request(base + "/api/scanner/replay",
                             data=json.dumps(solved["submission"]).encode(),
                             headers={"content-type": "application/json"},
                             method="POST")
        replayed = json.loads(urlopen(replay_req, timeout=10).read())
        assert replayed["accepted"] is True
        assert replayed["scored"] is False
        assert replayed["ledger_written"] is False
        assert json.loads(urlopen(base + "/api/scanner/submissions?limit=10", timeout=10).read())["count"] == 0

        attest_req = Request(base + "/api/scanner/attest",
                             data=json.dumps(solved["submission"]).encode(),
                             headers={"content-type": "application/json"},
                             method="POST")
        attested = json.loads(urlopen(attest_req, timeout=10).read())
        assert attested["accepted"] is True
        assert attested["attested"] is True
        assert attested["scored"] is False
        assert attested["ledger_written"] is False
        assert attested["receipt"]["production_tee"] is False
        assert attested["receipt"]["mode"] == "local_game_simulated_tee"

        unsealed_req = Request(base + "/api/scanner/submit-attested",
                               data=json.dumps(solved["submission"]).encode(),
                               headers={"content-type": "application/json"},
                               method="POST")
        unsealed = json.loads(urlopen(unsealed_req, timeout=10).read())
        assert unsealed["accepted"] is False
        assert unsealed["score"] == 0.0
        assert unsealed["gates"]["attestation_receipt"] is False
        assert "missing_attestation_receipt" in unsealed["reasons"]

        attested_payload = dict(solved["submission"])
        attested_payload["claim"] = {
            **(attested_payload.get("claim") or {}),
            "attestation": attested["receipt"],
        }
        sealed_req = Request(base + "/api/scanner/submit-attested",
                             data=json.dumps(attested_payload).encode(),
                             headers={"content-type": "application/json"},
                             method="POST")
        sealed = json.loads(urlopen(sealed_req, timeout=10).read())
        assert sealed["accepted"] is True
        assert sealed["score"] > 0
        assert sealed["gates"]["attestation_receipt"] is True

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
        assert verdict["ledger_entry"]["claim_present"] is True
        assert len(verdict["ledger_entry"]["claim_sha256"]) == 64

        req2 = Request(base + "/api/scanner/submit",
                       data=json.dumps(payload).encode(),
                       headers={"content-type": "application/json"},
                       method="POST")
        duplicate = json.loads(urlopen(req2, timeout=10).read())
        assert duplicate["accepted"] is False
        assert duplicate["score"] == 0.0
        assert duplicate["gates"]["not_duplicate_credit"] is False

        board = json.loads(urlopen(base + "/api/scanner/leaderboard", timeout=10).read())
        assert board["count"] == 2
        miners = {m["miner_hotkey"]: m for m in board["miners"]}
        assert miners["hk_example"]["score"] == verdict["score"]
        assert miners["hk_example"]["accepted"] == 1
        assert miners["hk_example"]["rejected"] == 1
        assert miners["hk_example"]["kill_rate"] > 0
        assert miners["hk_http_agent"]["score"] == sealed["score"]
        assert miners["hk_http_agent"]["accepted"] == 1
        assert miners["hk_http_agent"]["rejected"] == 1
        assert miners["hk_http_agent"]["kill_rate"] > 0

        bench = json.loads(urlopen(base + "/api/scanner/benchmark", timeout=10).read())
        assert bench["metric"] == "replay_kill_rate"
        bench_miners = {m["miner_hotkey"]: m for m in bench["miners"]}
        assert bench_miners["hk_example"]["kill_rate"] == miners["hk_example"]["kill_rate"]
        assert bench_miners["hk_http_agent"]["kill_rate"] == miners["hk_http_agent"]["kill_rate"]

        state = json.loads(urlopen(base + "/api/scanner/state?miner_hotkey=hk_example", timeout=10).read())
        assert state["schema"] == scanner.SCHEMA_STATE
        assert state["score"] == verdict["score"]
        assert state["accepted"] == 1
        assert state["rejected"] == 1
        assert task["task_id"] in state["accepted_task_ids"]

        submissions = json.loads(urlopen(base + "/api/scanner/submissions?limit=10", timeout=10).read())
        assert submissions["count"] == 4
        assert "duplicate_task_credit" in submissions["submissions"][0]["reasons"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_howto_route_serves_the_game_instructions_page(tmp_path):
    """GET /howto returns the standalone game instructions page."""
    s = ArenaServer(season_path=str(tmp_path / "season.json"),
                    scanner_ledger_path=str(tmp_path / "scanner.jsonl"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        for route in ("/howto", "/howto.html"):
            page = urlopen(base + route, timeout=10).read().decode()
            assert "How to Play Cathedral Arena" in page
            assert "A short guide to the playable proof loop." in page
            assert "Reports do not score" in page
            assert "Start the game at /game" in page
    finally:
        httpd.shutdown()
        httpd.server_close()
