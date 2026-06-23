"""Live arena server — the arena actually progresses in real time.

`python -m game.arena.serve [port]` serves the visual arena at
http://127.0.0.1:8800 and runs a FRESH round on each load (debounced to one tick
per few seconds), accumulating a persistent season. Refresh and the breach feed,
emissions, season standings, and proof anchor are all NEW — agents re-run, the
season climbs. The page auto-refreshes every 6s, so it ticks on its own.

The tick logic is a plain object (`ArenaServer`) so it is unit-testable without
binding a socket; the HTTP layer is a thin stdlib wrapper.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import scanner
from .engine import ArenaEngine
from .season import SeasonState
from .ui import render

OUT = Path(__file__).resolve().parent / "out"
MIN_TICK_SECS = 4.0          # debounce: at most one fresh round every few seconds


class ArenaServer:
    def __init__(self, *, season_path: str | None = None,
                 scanner_ledger_path: str | None = None,
                 base_epoch: int | None = None):
        from . import config
        self.engine = ArenaEngine(base_epoch=base_epoch or config.GAME_EPOCH)
        self.season_path = season_path
        self.scanner_ledger_path = scanner_ledger_path or str(OUT / "scanner_submissions.jsonl")
        self.season = SeasonState.load(season_path) if season_path else SeasonState()
        self.round = self.season.rounds
        self._last_tick = -1e9
        self._last_html = "<h1>warming up…</h1>"

    def tick(self):
        """Run the next round, fold it into the season, render. Returns the result."""
        self.round += 1
        self.engine._bench = None           # refresh the bench occasionally too
        result = self.engine.run(self.round)
        self.season.update(result)
        if self.season_path:
            self.season.save(self.season_path)
        result.season_rounds = self.season.rounds
        result.season_board = [{
            "agent_id": s.agent_id, "hotkey": s.hotkey, "emissions": s.total_emissions,
            "breaches": s.breaches, "streak": s.streak, "best_streak": s.best_streak,
            "rounds": s.rounds_played, "rank": s.rank,
        } for s in self.season.leaderboard()]
        result.season_targets = {n: {"status": t.status, "breaches": t.breaches,
                                     "first_broken_round": t.first_broken_round}
                                 for n, t in self.season.targets.items()}
        result.season_conquered = self.season.conquered()
        self._last_html = render(result)
        self._last_tick = time.monotonic()
        return result

    def html(self) -> str:
        """Serve cached HTML, ticking a fresh round if the debounce window passed."""
        if time.monotonic() - self._last_tick >= MIN_TICK_SECS:
            try:
                self.tick()
            except Exception as e:        # never 500 the page on a transient error
                self._last_html = f"<h1>arena error</h1><pre>{e}</pre>" + self._last_html
        return self._last_html

    def scanner_catalog(self, limit: int | None = None) -> dict:
        tasks = scanner.benchmark_catalog(limit=limit)
        return {"schema": "cathedral.scanner.catalog.v1",
                "count": len(tasks),
                "tasks": [t.manifest() for t in tasks]}

    def scanner_task(self, index: int = 0) -> dict:
        return scanner.issue_task(index).manifest()

    def scanner_example(self, index: int = 0) -> dict:
        task = scanner.issue_task(index)
        sub = scanner.example_accepted_submission(task)
        verdict = scanner.verify_submission(task, sub)
        return {"task": task.manifest(),
                "submission": sub.as_artifact(),
                "verdict": verdict.as_dict()}

    def scanner_agent_solve(self, index: int = 0, miner_hotkey: str = "hk_local_agent",
                            mode: str = "valid") -> dict:
        """Run the local demo agent for one scanner task without scoring it."""

        task = scanner.issue_task(index)
        sub = scanner.example_accepted_submission(task, miner_hotkey=miner_hotkey)
        mode = mode if mode in {"valid", "bad_witness", "wrong_family", "report_only"} else "valid"
        if mode == "bad_witness":
            sub = replace(sub, witness={k: 0 for k in task.required_fields})
        elif mode == "wrong_family":
            sub = replace(sub, proof_family="Z_forged")
        elif mode == "report_only":
            sub = replace(sub, witness=None, report="Claim without replayable witness.")
        artifact = sub.as_artifact()
        return {
            "schema": "cathedral.scanner.agent_solution.v1",
            "task": task.manifest(),
            "submission": artifact,
            "artifact_sha256": scanner._sha(artifact),
            "scored": False,
            "mode": mode,
            "note": "local demo agent produced a proof artifact; submit separately to score",
        }

    def scanner_leaderboard(self) -> dict:
        return scanner.leaderboard(self.scanner_ledger_path)

    def scanner_submissions(self, limit: int = 50) -> dict:
        entries = scanner.read_ledger(self.scanner_ledger_path)
        return {"schema": scanner.SCHEMA_LEDGER,
                "count": len(entries),
                "submissions": list(reversed(entries[-limit:]))}

    def scanner_state(self, miner_hotkey: str) -> dict:
        return scanner.miner_state(self.scanner_ledger_path, miner_hotkey)

    def _scanner_submission_from_payload(self, payload: dict) -> tuple[scanner.ScannerTask, scanner.ScannerSubmission]:
        task_id = str(payload.get("task_id", ""))
        tasks = scanner.benchmark_catalog()
        task = next((t for t in tasks if t.task_id == task_id), None)
        if task is None:
            raise KeyError(f"unknown scanner task: {task_id}")
        sub = scanner.ScannerSubmission(
            task_id=task_id,
            miner_hotkey=str(payload.get("miner_hotkey", "")),
            nonce=str(payload.get("nonce", "")),
            proof_family=str(payload.get("proof_family", "")),
            witness=payload.get("witness"),
            trace=payload.get("trace") or [],
            report=str(payload.get("report", "")),
        )
        return task, sub

    def scanner_replay(self, payload: dict) -> dict:
        """Dry-run deterministic replay without appending to the ledger."""

        task, sub = self._scanner_submission_from_payload(payload)
        verdict = scanner.verify_submission(task, sub).as_dict()
        verdict["ledger_written"] = False
        verdict["scored"] = False
        return verdict

    def scanner_submit(self, payload: dict) -> dict:
        """Verify one scanner submission from JSON payload.

        `task_id` selects a deterministic task from the local catalog. Prose is
        accepted for humans but ignored by scoring in scanner.verify_submission.
        """
        task, sub = self._scanner_submission_from_payload(payload)
        return scanner.record_submission(self.scanner_ledger_path, task, sub)

    def scanner_game_html(self) -> str:
        """Interactive scanner game backed by the local verifier API."""

        return _SCANNER_GAME_HTML


def _handler(server: ArenaServer):
    def _send_json(req: BaseHTTPRequestHandler, status: int, obj: dict) -> None:
        body = json.dumps(obj, indent=2, default=str).encode("utf-8")
        req.send_response(status)
        req.send_header("content-type", "application/json; charset=utf-8")
        req.send_header("content-length", str(len(body)))
        req.end_headers()
        req.wfile.write(body)

    def _int_param(qs: dict, name: str, default: int) -> int:
        try:
            return int((qs.get(name) or [default])[0])
        except (TypeError, ValueError):
            return default

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/api/scanner/catalog":
                limit = qs.get("limit", [None])[0]
                try:
                    limit_i = int(limit) if limit is not None else None
                except (TypeError, ValueError):
                    limit_i = None
                _send_json(self, 200, server.scanner_catalog(limit_i))
                return
            if parsed.path == "/api/scanner/task":
                _send_json(self, 200, server.scanner_task(_int_param(qs, "index", 0)))
                return
            if parsed.path == "/api/scanner/example":
                _send_json(self, 200, server.scanner_example(_int_param(qs, "index", 0)))
                return
            if parsed.path == "/api/scanner/agent/solve":
                miner = (qs.get("miner_hotkey") or ["hk_local_agent"])[0]
                mode = (qs.get("mode") or ["valid"])[0]
                _send_json(self, 200, server.scanner_agent_solve(_int_param(qs, "index", 0), miner, mode))
                return
            if parsed.path == "/api/scanner/leaderboard":
                _send_json(self, 200, server.scanner_leaderboard())
                return
            if parsed.path == "/api/scanner/submissions":
                _send_json(self, 200, server.scanner_submissions(_int_param(qs, "limit", 50)))
                return
            if parsed.path == "/api/scanner/state":
                miner = (qs.get("miner_hotkey") or [""])[0]
                _send_json(self, 200, server.scanner_state(miner))
                return
            if parsed.path == "/dashboard.html":
                self.send_response(302)
                self.send_header("location", "/game")
                self.end_headers()
                return
            if parsed.path in {"/game", "/game.html"}:
                body = server.scanner_game_html().encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            body = server.html().encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path not in {"/api/scanner/replay", "/api/scanner/submit"}:
                _send_json(self, 404, {"ok": False, "error": "not_found"})
                return
            try:
                n = min(int(self.headers.get("content-length", "0")), 1_000_000)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                verdict = (
                    server.scanner_replay(payload)
                    if parsed.path == "/api/scanner/replay"
                    else server.scanner_submit(payload)
                )
                _send_json(self, 200, verdict)
            except KeyError as e:
                _send_json(self, 404, {"ok": False, "error": str(e)})
            except Exception as e:
                _send_json(self, 400, {"ok": False, "error": type(e).__name__})

        def log_message(self, *a):
            pass
    return H


def serve(port: int = 8800) -> None:
    OUT.mkdir(exist_ok=True)
    srv = ArenaServer(season_path=str(OUT / "season_state.json"))
    srv.tick()
    # WSL loopback is not consistently reachable from the Windows in-app browser
    # when bound to 127.0.0.1 inside Linux. Bind all interfaces for the local
    # dev server while advertising the localhost URL.
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _handler(srv))
    print(f"Cathedral Arena LIVE at http://127.0.0.1:{port}  (ticks a fresh round every "
          f"{MIN_TICK_SECS:.0f}s; auto-refreshes)")
    print(f"Subnet Breaker game: http://127.0.0.1:{port}/game")
    print("Scanner API: GET /api/scanner/task, GET /api/scanner/catalog, "
          "GET /api/scanner/example, GET /api/scanner/agent/solve, "
          "POST /api/scanner/replay, POST /api/scanner/submit, "
          "GET /api/scanner/leaderboard, GET /api/scanner/state")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


_SCANNER_GAME_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subnet Breaker - Cathedral</title>
<style>
:root{color-scheme:dark;--bg:#03070d;--panel:#09111c;--line:#29435f;--text:#eef7ff;--muted:#89a0b6;--green:#4cff95;--blue:#57c7ff;--red:#ff4d66;--yellow:#ffd45a}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;overflow:hidden;color:var(--text);font:14px/1.4 Inter,Segoe UI,Arial,sans-serif;background:radial-gradient(circle at 50% 40%,rgba(87,199,255,.16),transparent 32%),radial-gradient(circle at 15% 20%,rgba(76,255,149,.12),transparent 24%),radial-gradient(circle at 82% 12%,rgba(255,77,102,.13),transparent 22%),repeating-linear-gradient(90deg,rgba(87,199,255,.035) 0 1px,transparent 1px 96px),repeating-linear-gradient(0deg,rgba(76,255,149,.025) 0 1px,transparent 1px 96px),var(--bg)}
button{font:inherit;color:inherit}.game{height:100vh;display:grid;grid-template-rows:76px 1fr 112px;gap:12px;padding:14px}.hud,.arena,.mission,.log,.command,.node,.core{border:1px solid var(--line);border-radius:8px;background:rgba(8,14,22,.9);box-shadow:inset 0 0 0 1px rgba(255,255,255,.025)}
.hud{display:grid;grid-template-columns:270px 1fr 260px;gap:12px;align-items:center;padding:12px}.brand h1{margin:0;font-size:28px;letter-spacing:.06em}.brand div,.muted{color:var(--muted);font-size:12px}.meters{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.meter{border:1px solid #203248;border-radius:8px;background:#07101a;padding:8px}.meter span{display:block;color:var(--muted);font-size:10px;letter-spacing:.11em;text-transform:uppercase}.meter b{display:block;margin-top:4px;font-size:22px}.bar{height:8px;border:1px solid #22354a;border-radius:999px;background:#02060a;overflow:hidden;margin-top:7px}.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--yellow),var(--red));transition:.2s}
.stage{min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:12px}.arena{position:relative;overflow:hidden;background:radial-gradient(circle,rgba(87,199,255,.13),rgba(3,7,13,.92) 58%)}.arena:before{content:"";position:absolute;inset:-35%;background:conic-gradient(transparent,rgba(87,199,255,.12),transparent 20%,rgba(76,255,149,.09),transparent 45%,rgba(255,212,90,.09),transparent 65%,rgba(255,77,102,.09),transparent);animation:spin 26s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.core{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:2;width:300px;height:300px;border-radius:50%;display:grid;place-items:center;text-align:center;background:radial-gradient(circle,rgba(16,35,55,.95),rgba(6,12,20,.74) 70%,transparent)}.core:before,.core:after{content:"";position:absolute;inset:24px;border:1px dashed rgba(87,199,255,.4);border-radius:50%;animation:spin 13s linear infinite}.core:after{inset:68px;border-color:rgba(255,212,90,.4);animation-duration:8s;animation-direction:reverse}.core h2{margin:0;font-size:42px}.core p{margin:8px auto 0;color:var(--muted);max-width:220px}.phase{display:inline-block;margin-top:12px;border:1px solid var(--yellow);border-radius:6px;padding:5px 9px;color:var(--yellow);font-weight:900;text-transform:uppercase}
.node{position:absolute;z-index:3;width:158px;min-height:96px;padding:9px;cursor:pointer;transition:.16s}.node:hover{transform:translateY(-4px) scale(1.02);border-color:var(--blue)}.node.active{border-color:var(--yellow);box-shadow:0 0 0 1px rgba(255,212,90,.25),0 0 28px rgba(255,212,90,.18)}.node.cleared{border-color:rgba(76,255,149,.75);background:rgba(10,36,23,.9)}.node.rejected{border-color:rgba(255,77,102,.75);background:rgba(38,13,20,.9)}.node .sn{font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase}.node strong{display:block;margin-top:3px}.node small{display:block;margin-top:5px;color:var(--muted);font-size:11px;line-height:1.2}.tag{display:inline-block;margin:7px 4px 0 0;border:1px solid #294058;border-radius:999px;padding:2px 6px;color:var(--muted);font-size:10px;text-transform:uppercase}.cash{color:var(--yellow);font-weight:900}.ship{position:absolute;z-index:4;left:50%;top:50%;width:26px;height:26px;border:2px solid var(--green);border-radius:50%;background:#06150d;box-shadow:0 0 18px var(--green);transform:translate(-50%,-50%);transition:.35s}.beam{position:absolute;z-index:1;left:50%;top:50%;width:2px;height:170px;background:linear-gradient(var(--blue),transparent);transform-origin:top center;opacity:0;box-shadow:0 0 20px var(--blue)}.beam.fire{opacity:.85;animation:pulse .35s ease 2}@keyframes pulse{50%{filter:brightness(2)}}
.sidebar{min-height:0;display:grid;grid-template-rows:auto 1fr;gap:12px}.mission{padding:14px}.mission h2{margin:0;font-size:24px}.mission p{margin:8px 0 10px;color:var(--muted)}.slots{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:12px}.slot{height:42px;border:1px solid #263b53;border-radius:7px;background:#07101a;display:grid;place-items:center;color:var(--muted);font-size:10px;text-transform:uppercase}.slot.done{border-color:var(--green);color:var(--green);background:rgba(76,255,149,.08)}.slot.active{border-color:var(--yellow);color:var(--yellow)}.log{padding:12px;overflow:auto;font-family:Consolas,ui-monospace,monospace;font-size:12px}.log div{margin-bottom:5px}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--yellow)}
.command{display:grid;grid-template-columns:260px 1fr 300px;gap:12px;padding:12px}.operator{border:1px solid #203248;border-radius:8px;background:#07101a;padding:10px}.operator span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.11em}.operator b{display:block;margin-top:4px}.actions{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}.action{min-height:66px;text-align:left;cursor:pointer;border:1px solid #2b435e;border-radius:8px;background:#101b29;padding:10px}.action:hover{border-color:var(--blue)}.action.primary{background:linear-gradient(180deg,#143322,#0b1912);border-color:rgba(76,255,149,.55)}.action.danger{border-color:rgba(255,77,102,.5);color:#ffb8c2}.action b{display:block}.action span{display:block;color:var(--muted);font-size:11px;margin-top:3px}.end{position:absolute;inset:0;display:none;place-items:center;background:rgba(2,5,9,.8);z-index:8}.end.show{display:grid}.modal{width:min(560px,90vw);border:1px solid var(--yellow);border-radius:10px;background:#09111c;padding:22px;text-align:center;box-shadow:0 0 60px rgba(255,212,90,.24)}.modal h2{font-size:42px;margin:0}.modal p{color:var(--muted)}
@media(max-width:1100px){body{overflow:auto}.game{height:auto}.hud,.stage,.command{grid-template-columns:1fr}.arena{height:640px}.actions{grid-template-columns:repeat(2,minmax(0,1fr))}.meters{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
</head>
<body>
<div class="game">
  <header class="hud">
    <div class="brand"><h1>SUBNET BREAKER</h1><div>break the target, replay the proof, seal the reward</div></div>
    <div class="meters">
      <div class="meter"><span>score</span><b id="score">0</b></div>
      <div class="meter"><span>bounty</span><b id="bounty">0</b></div>
      <div class="meter"><span>sealed</span><b id="proofs">0</b></div>
      <div class="meter"><span>blocks</span><b id="blocked">0</b></div>
      <div class="meter"><span>target</span><b id="targetNo">-</b></div>
    </div>
    <div><b id="clock">04:31</b><div class="muted">Round clock</div><div class="bar"><i id="timeFill" style="width:100%"></i></div></div>
  </header>
  <main class="stage">
    <section class="arena" id="arena">
      <div class="beam" id="beam"></div><div class="ship" id="ship"></div>
      <div class="core"><div><h2 id="coreVerb">LOAD</h2><p id="coreSub">Pick a subnet target. Reports do not score. Replayed witnesses do.</p><span id="phaseBadge" class="phase">target</span></div></div>
      <div id="nodes"></div>
      <div id="end" class="end"><div class="modal"><h2>SEASON CLEARED</h2><p>Accepted proofs were written to the scanner ledger.</p><button class="action primary" onclick="location.reload()"><b>Run it back</b><span>new local player</span></button></div></div>
    </section>
    <aside class="sidebar">
      <div class="mission">
        <h2 id="targetName">Loading</h2><p id="targetBrief"></p>
        <div class="bar"><i id="heatFill"></i></div><p><b id="heatText">0%</b> validator heat / <b id="energyText">100%</b> agent energy</p>
        <div class="bar"><i id="energyFill" style="width:100%"></i></div><div id="slots" class="slots"></div>
      </div>
      <div id="log" class="log"></div>
    </aside>
  </main>
  <footer class="command">
    <div class="operator"><span>Miner Agent</span><b id="agentName">loading</b><small id="agentMeta" class="muted"></small></div>
    <div class="actions">
      <button class="action primary" data-action="probe"><b>1 Probe</b><span>read task</span></button>
      <button class="action primary" data-action="encode"><b>2 Encode</b><span>family gate</span></button>
      <button class="action primary" data-action="solve"><b>3 Solve</b><span>use witness</span></button>
      <button class="action primary" data-action="replay"><b>4 Replay</b><span>local verifier</span></button>
      <button class="action primary" data-action="submit"><b>5 Seal</b><span>POST submit</span></button>
      <button class="action danger" data-action="forge"><b>6 Forge</b><span>bad witness</span></button>
      <button class="action danger" data-action="decoy"><b>7 Cooldown</b><span>drop heat</span></button>
    </div>
    <div class="operator"><span>Verifier</span><b>/api/scanner/replay -> submit</b><small class="muted">replay verifies; seal writes ledger</small></div>
  </footer>
</div>
<script>
const phases = ['target','probe','encode','solve','replay','seal'];
const player = localStorage.scannerPlayer || ('hk_player_' + Math.random().toString(16).slice(2));
localStorage.scannerPlayer = player;
const state = {tasks:[], selected:0, phase:0, score:0, proofs:0, blocked:0, energy:100, heat:0, time:271, cleared:new Set(), rejected:new Set(), pendingProofs:{}, replayOk:{}, combo:1};
let actionQueue = Promise.resolve();
const $ = id => document.getElementById(id);
function log(msg, cls='muted'){const d=document.createElement('div');d.className=cls;d.textContent='[t-'+String(271-state.time).padStart(3,'0')+'] '+msg;$('log').prepend(d)}
function current(){return state.tasks[state.selected]}
function pos(i,n){const a=(-90+i*(360/n))*Math.PI/180;return {left:50+Math.cos(a)*38,top:50+Math.sin(a)*35}}
function fire(){const b=$('beam');b.classList.remove('fire');void b.offsetWidth;b.classList.add('fire')}
function renderNodes(){const root=$('nodes');root.innerHTML='';state.tasks.forEach((t,i)=>{const p=pos(i,state.tasks.length);const n=document.createElement('div');n.className='node'+(i===state.selected?' active':'')+(state.cleared.has(i)?' cleared':'')+(state.rejected.has(i)?' rejected':'');n.style.left=`calc(${p.left}% - 79px)`;n.style.top=`calc(${p.top}% - 48px)`;n.onclick=()=>select(i);n.innerHTML=`<div class="sn">SN${t.target.netuid} / ${t.expected_family}</div><strong>${t.target.name}</strong><small>${t.objective}</small><span class="tag cash">+${Math.round(t.bounty_weight*1000)}</span><span class="tag">${t.required_fields.length} fields</span>`;root.appendChild(n)})}
function renderSlots(){const root=$('slots');root.innerHTML='';phases.forEach((p,i)=>{const s=document.createElement('div');s.className='slot'+(i<state.phase?' done':i===state.phase?' active':'');s.textContent=p;root.appendChild(s)})}
function select(i){state.selected=i;state.phase=state.cleared.has(i)?5:0;log('target locked: SN'+current().target.netuid+' '+current().target.name,'warn');render()}
function render(){if(!state.tasks.length)return;const t=current();$('targetName').textContent='SN'+t.target.netuid+': '+t.target.name;$('targetBrief').textContent=t.objective;$('phaseBadge').textContent=phases[state.phase];$('coreVerb').textContent=phases[state.phase].toUpperCase();$('coreSub').textContent='Win condition: '+t.expected_family+' witness with fields '+t.required_fields.join(', ')+' must replay before it can seal.';$('score').textContent=state.score;$('bounty').textContent=Math.round(t.bounty_weight*1000);$('proofs').textContent=state.proofs;$('blocked').textContent=state.blocked;$('targetNo').textContent=(state.selected+1)+'/'+state.tasks.length;$('heatText').textContent=Math.round(state.heat)+'%';$('heatFill').style.width=state.heat+'%';$('energyText').textContent=Math.round(state.energy)+'%';$('energyFill').style.width=state.energy+'%';const p=pos(state.selected,state.tasks.length);$('ship').style.left=p.left+'%';$('ship').style.top=p.top+'%';$('beam').style.transform=`rotate(${Math.atan2(p.top-50,p.left-50)*180/Math.PI+90}deg)`;$('agentName').textContent=player;$('agentMeta').textContent='local game identity; no chain writes';renderNodes();renderSlots()}
function spend(e,h){state.energy=Math.max(0,state.energy-e);state.heat=Math.min(100,state.heat+h);if(state.heat>=100){state.blocked++;state.heat=58;state.energy=Math.max(15,state.energy-18);state.phase=Math.max(0,state.phase-1);state.combo=1;log('validator countermeasure fired: chain rolled back one gate','bad')}}
function nextOpen(){for(let step=1;step<=state.tasks.length;step++){const i=(state.selected+step)%state.tasks.length;if(!state.cleared.has(i))return i}return -1}
async function syncState(announce=false){const s=await fetch('/api/scanner/state?miner_hotkey='+encodeURIComponent(player)).then(r=>r.json());const accepted=new Set(s.accepted_task_ids||[]);const rejected=new Set(s.rejected_task_ids||[]);state.cleared=new Set();state.rejected=new Set();state.tasks.forEach((t,i)=>{if(accepted.has(t.task_id))state.cleared.add(i);else if(rejected.has(t.task_id))state.rejected.add(i)});state.score=Math.round((s.score||0)*1000);state.proofs=s.accepted||0;state.blocked=Math.max(state.blocked,s.rejected||0);if(announce&&s.attempts)log('restored ledger: '+s.accepted+' accepted / '+s.rejected+' rejected / score '+s.score,'warn')}
async function runAgentSolve(mode='valid'){const t=current();const solved=await fetch('/api/scanner/agent/solve?index='+state.selected+'&miner_hotkey='+encodeURIComponent(player)+'&mode='+encodeURIComponent(mode)).then(r=>r.json());state.pendingProofs[t.task_id]=solved.submission;delete state.replayOk[t.task_id];const cls=mode==='valid'?'ok':'bad';log((mode==='valid'?'agent produced proof artifact ':'agent forged corrupt artifact ')+solved.artifact_sha256.slice(0,12),cls)}
async function runReplay(){const t=current();const proof=state.pendingProofs[t.task_id];if(!proof){state.blocked++;state.combo=1;state.rejected.add(state.selected);log('REPLAY FAILED: no pending proof artifact; run Solve first','bad');return false}const payload={...proof,miner_hotkey:player,report:'dry-run replay through Subnet Breaker UI'};const verdict=await fetch('/api/scanner/replay',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());if(verdict.accepted){state.replayOk[t.task_id]=true;state.rejected.delete(state.selected);log('REPLAY ACCEPTED: '+verdict.replay_target_id+' / no ledger write','ok');return true}state.blocked++;state.combo=1;state.rejected.add(state.selected);delete state.replayOk[t.task_id];log('REPLAY REJECTED: '+(verdict.reasons||[verdict.error||'unknown']).join(', '),'bad');return false}
async function seal(){const t=current();const proof=state.pendingProofs[t.task_id];if(!proof){state.blocked++;state.combo=1;log('REJECTED: no pending proof artifact; run Solve first','bad');return}if(!state.replayOk[t.task_id]){state.blocked++;state.combo=1;log('REJECTED: replay gate has not passed','bad');return}const payload={...proof,miner_hotkey:player,report:'sealed through Subnet Breaker UI'};const verdict=await fetch('/api/scanner/submit',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());delete state.pendingProofs[t.task_id];delete state.replayOk[t.task_id];if(verdict.accepted){state.phase=5;state.combo=Math.min(2.5,state.combo+.15);log('SEALED: backend accepted '+verdict.replay_target_id,'ok')}else{state.blocked++;state.combo=1;log('REJECTED: '+(verdict.reasons||[verdict.error||'unknown']).join(', '),'bad')}await syncState(false);const board=await fetch('/api/scanner/leaderboard').then(r=>r.json());if(board.miners&&board.miners[0])log('leaderboard #1 '+board.miners[0].miner_hotkey+' score '+board.miners[0].score,'warn');const n=nextOpen();if(n<0)$('end').classList.add('show')}
async function act(name){if(!state.tasks.length)return;if(name==='decoy'){state.heat=Math.max(0,state.heat-28);state.energy=Math.min(100,state.energy+10);log('cooldown route deployed','ok');render();return}if(state.cleared.has(state.selected)){const n=nextOpen();if(n<0){$('end').classList.add('show');return}select(n);return}const needed={probe:0,encode:1,solve:2,replay:3,submit:4,forge:2}[name];if(state.phase!==needed){state.blocked++;state.combo=1;log('gate rejected: run the chain in order','bad');render();return}if(state.energy<=8){state.blocked++;log('agent exhausted: cooldown before continuing','bad');render();return}fire();if(name==='probe'){spend(8,9);state.phase=1;log('task fetched and scoped to pinned replay target','ok')}if(name==='encode'){spend(12,14);state.phase=2;log('family gate aligned: '+current().expected_family,'ok')}if(name==='solve'){spend(17,18);await runAgentSolve();state.phase=3;log('witness fields prepared: '+current().required_fields.join(', '),'ok')}if(name==='forge'){spend(10,24);await runAgentSolve('bad_witness');state.phase=3;log('forged witness prepared; replay should catch this','warn')}if(name==='replay'){spend(12,16);if(await runReplay())state.phase=4}if(name==='submit'){spend(5,8);await seal()}render()}
function enqueue(name){actionQueue=actionQueue.then(()=>act(name)).catch(e=>log('action failed: '+e.message,'bad'))}
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>enqueue(b.dataset.action));document.addEventListener('keydown',e=>{const m={'1':'probe','2':'encode','3':'solve','4':'replay','5':'submit','6':'forge','7':'decoy'};if(m[e.key])enqueue(m[e.key])});
function tick(){state.time=Math.max(0,state.time-1);if(state.time<=0){state.blocked++;state.time=271;state.heat=Math.min(95,state.heat+20);log('round clock expired','bad')}state.energy=Math.min(100,state.energy+.2);state.heat=Math.max(0,state.heat-.05);const m=String(Math.floor(state.time/60)).padStart(2,'0'),s=String(state.time%60).padStart(2,'0');$('clock').textContent=m+':'+s;$('timeFill').style.width=Math.max(0,state.time/271*100)+'%';render()}
async function boot(){const catalog=await fetch('/api/scanner/catalog?limit=12').then(r=>r.json());state.tasks=catalog.tasks;await syncState(true);log('loaded '+catalog.count+' subnet targets from backend','warn');log('keys: 1 probe, 2 encode, 3 solve, 4 replay, 5 seal, 6 forge, 7 cooldown','warn');render();setInterval(tick,1000)}
boot().catch(e=>log('boot failed: '+e.message,'bad'));
</script>
</body>
</html>"""


def main() -> None:
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8800)


if __name__ == "__main__":
    main()
