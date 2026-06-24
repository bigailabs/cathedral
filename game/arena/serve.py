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
                            mode: str = "valid", task_id: str = "") -> dict:
        """Run the local demo agent for one scanner task without scoring it."""

        task = scanner.task_by_id(task_id) if task_id else scanner.issue_task(index)
        if task is None:
            raise KeyError(f"unknown scanner task: {task_id}")
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

    def scanner_benchmark(self) -> dict:
        return scanner.benchmark(self.scanner_ledger_path)

    def scanner_request(self, payload: dict) -> dict:
        return scanner.intake_scan_request(payload)

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
            claim=payload.get("claim") or {},
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

    def _attestation_bound_submission(
        self, sub: scanner.ScannerSubmission
    ) -> scanner.ScannerSubmission:
        """Canonical proof payload for receipts, excluding mutable prose."""

        return replace(sub, report="")

    def scanner_attest(self, payload: dict) -> dict:
        """Issue a local simulated attestation receipt for a replayable proof.

        This is a game/dev receipt, not production TEE evidence. It exists so
        the playable loop has the same shape as Cathedral's proof path:
        replay first, bind a receipt, then seal.
        """

        task, sub = self._scanner_submission_from_payload(payload)
        verdict = scanner.verify_submission(task, sub).as_dict()
        verdict["ledger_written"] = False
        verdict["scored"] = False
        if not verdict["accepted"]:
            verdict["attested"] = False
            verdict["receipt"] = None
            return verdict

        bound_sub = self._attestation_bound_submission(sub)
        bound_verdict = scanner.verify_submission(task, bound_sub).as_dict()
        artifact = bound_sub.as_artifact()
        receipt = {
            "schema": "cathedral.scanner.local_attestation.v1",
            "mode": "local_game_simulated_tee",
            "production_tee": False,
            "task_id": task.task_id,
            "miner_hotkey": sub.miner_hotkey,
            "replay_target_id": task.replay_target_id,
            "artifact_sha256": bound_verdict["artifact_sha256"],
            "claim_sha256": artifact["claim_sha256"],
            "observed_sha256": scanner._sha(bound_verdict["observed"]),
            "statement": "replay accepted by local verifier before ledger seal",
        }
        verdict["attested"] = True
        verdict["receipt"] = receipt
        verdict["gates"] = dict(verdict["gates"])
        verdict["gates"]["attestation_receipt"] = True
        return verdict

    def _attestation_errors(self, task: scanner.ScannerTask,
                            sub: scanner.ScannerSubmission) -> list[str]:
        artifact = sub.as_artifact()
        claim = artifact["claim"] if isinstance(artifact.get("claim"), dict) else {}
        receipt = claim.get("attestation") if isinstance(claim, dict) else None
        if not isinstance(receipt, dict):
            return ["missing_attestation_receipt"]

        claim_before_attest = dict(claim)
        claim_before_attest.pop("attestation", None)
        base_sub = self._attestation_bound_submission(
            replace(sub, claim=claim_before_attest)
        )
        base_verdict = scanner.verify_submission(task, base_sub).as_dict()
        base_artifact = base_sub.as_artifact()

        expected = {
            "schema": "cathedral.scanner.local_attestation.v1",
            "mode": "local_game_simulated_tee",
            "production_tee": False,
            "task_id": task.task_id,
            "miner_hotkey": sub.miner_hotkey,
            "replay_target_id": task.replay_target_id,
            "artifact_sha256": base_verdict["artifact_sha256"],
            "claim_sha256": base_artifact["claim_sha256"],
            "observed_sha256": scanner._sha(base_verdict["observed"]),
        }
        errors: list[str] = []
        for key, value in expected.items():
            if receipt.get(key) != value:
                errors.append(f"attestation_{key}_mismatch")
        return errors

    def scanner_submit(self, payload: dict) -> dict:
        """Verify one scanner submission from JSON payload.

        `task_id` selects a deterministic task from the local catalog. Prose is
        accepted for humans but ignored by scoring in scanner.verify_submission.
        """
        task, sub = self._scanner_submission_from_payload(payload)
        return scanner.record_submission(self.scanner_ledger_path, task, sub)

    def scanner_attested_submit(self, payload: dict) -> dict:
        """Score a game submission only if replay and attestation both pass."""

        task, sub = self._scanner_submission_from_payload(payload)
        entries = scanner.read_ledger(self.scanner_ledger_path)
        verdict = scanner.ledger_gate(scanner.verify_submission(task, sub), sub, entries)
        verdict["gates"] = dict(verdict["gates"])
        errors = self._attestation_errors(task, sub)
        verdict["gates"]["attestation_receipt"] = not errors
        if errors:
            verdict["accepted"] = False
            verdict["score"] = 0.0
            verdict["reasons"] = list(verdict["reasons"]) + errors
        entry = scanner.append_ledger(self.scanner_ledger_path, task, sub, verdict)
        verdict["ledger_entry"] = entry
        verdict["ledger_written"] = True
        verdict["scored"] = bool(verdict["accepted"])
        return verdict

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
                task_id = (qs.get("task_id") or [""])[0]
                try:
                    _send_json(self, 200, server.scanner_agent_solve(
                        _int_param(qs, "index", 0), miner, mode, task_id
                    ))
                except KeyError as e:
                    _send_json(self, 404, {"error": str(e)})
                return
            if parsed.path == "/api/scanner/leaderboard":
                _send_json(self, 200, server.scanner_leaderboard())
                return
            if parsed.path == "/api/scanner/benchmark":
                _send_json(self, 200, server.scanner_benchmark())
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
            if parsed.path not in {
                "/api/scanner/request",
                "/api/scanner/replay",
                "/api/scanner/attest",
                "/api/scanner/submit-attested",
                "/api/scanner/submit",
            }:
                _send_json(self, 404, {"ok": False, "error": "not_found"})
                return
            try:
                n = min(int(self.headers.get("content-length", "0")), 1_000_000)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                if parsed.path == "/api/scanner/request":
                    verdict = server.scanner_request(payload)
                elif parsed.path == "/api/scanner/replay":
                    verdict = server.scanner_replay(payload)
                elif parsed.path == "/api/scanner/attest":
                    verdict = server.scanner_attest(payload)
                elif parsed.path == "/api/scanner/submit-attested":
                    verdict = server.scanner_attested_submit(payload)
                else:
                    verdict = server.scanner_submit(payload)
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
    # WSL loopback is not consistently reachable from the Windows in-app browser
    # when bound to 127.0.0.1 inside Linux. Bind all interfaces for the local
    # dev server while advertising the localhost URL.
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _handler(srv))
    print(f"Cathedral Arena LIVE at http://127.0.0.1:{port}  (ticks a fresh round every "
          f"{MIN_TICK_SECS:.0f}s; auto-refreshes)")
    print(f"Subnet Breaker game: http://127.0.0.1:{port}/game")
    print("Scanner API: GET /api/scanner/task, GET /api/scanner/catalog, "
          "GET /api/scanner/example, GET /api/scanner/agent/solve, "
          "POST /api/scanner/request, "
          "POST /api/scanner/replay, POST /api/scanner/attest, "
          "POST /api/scanner/submit-attested, POST /api/scanner/submit, "
          "GET /api/scanner/leaderboard, GET /api/scanner/benchmark, "
          "GET /api/scanner/state")
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
:root{color-scheme:dark;--bg:#03070d;--panel:#09111c;--line:#29435f;--text:#eef7ff;--muted:#89a0b6;--green:#4cff95;--blue:#57c7ff;--red:#ff4d66;--yellow:#ffd45a;--violet:#b991ff}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;overflow:hidden;color:var(--text);font:14px/1.4 Inter,Segoe UI,Arial,sans-serif;background:radial-gradient(circle at 50% 40%,rgba(87,199,255,.16),transparent 32%),radial-gradient(circle at 15% 20%,rgba(76,255,149,.12),transparent 24%),radial-gradient(circle at 82% 12%,rgba(255,77,102,.13),transparent 22%),repeating-linear-gradient(90deg,rgba(87,199,255,.035) 0 1px,transparent 1px 96px),repeating-linear-gradient(0deg,rgba(76,255,149,.025) 0 1px,transparent 1px 96px),var(--bg)}
button{font:inherit;color:inherit}.game{height:100vh;display:grid;grid-template-rows:76px 1fr 112px;gap:12px;padding:14px}.hud,.arena,.mission,.log,.command,.node,.core{border:1px solid var(--line);border-radius:8px;background:rgba(8,14,22,.9);box-shadow:inset 0 0 0 1px rgba(255,255,255,.025)}
.hud{display:grid;grid-template-columns:270px 1fr 260px;gap:12px;align-items:center;padding:12px}.brand h1{margin:0;font-size:28px;letter-spacing:.06em}.brand div,.muted{color:var(--muted);font-size:12px}.meters{display:grid;grid-template-columns:repeat(8,1fr);gap:8px}.meter{border:1px solid #203248;border-radius:8px;background:#07101a;padding:8px}.meter span{display:block;color:var(--muted);font-size:10px;letter-spacing:.11em;text-transform:uppercase}.meter b{display:block;margin-top:4px;font-size:22px}.bar{height:8px;border:1px solid #22354a;border-radius:999px;background:#02060a;overflow:hidden;margin-top:7px}.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--yellow),var(--red));transition:.2s}
.stage{min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:12px}.arena{position:relative;overflow:hidden;background:linear-gradient(135deg,rgba(255,77,102,.08),transparent 28%),linear-gradient(180deg,rgba(87,199,255,.08),rgba(76,255,149,.07)),var(--bg)}.arena:before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(87,199,255,.04) 1px,transparent 1px),linear-gradient(0deg,rgba(87,199,255,.035) 1px,transparent 1px);background-size:96px 96px;opacity:.45}.arena:after{content:"";position:absolute;inset:0;pointer-events:none;opacity:0}.arena.strike:after{animation:strike .45s ease}.arena.reject:after{background:rgba(255,77,102,.18);animation:jam .55s ease}.arena.breached:after{background:rgba(76,255,149,.18);animation:jam .75s ease}@keyframes spin{to{transform:rotate(360deg)}}@keyframes strike{0%{box-shadow:inset 0 0 0 0 rgba(87,199,255,.0)}45%{box-shadow:inset 0 0 160px rgba(87,199,255,.36)}100%{box-shadow:inset 0 0 0 rgba(87,199,255,.0)}}@keyframes jam{0%,100%{opacity:0}30%{opacity:1}}.core{--progress:0%;position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:2;width:280px;height:280px;border:2px solid transparent;border-radius:50%;display:grid;place-items:center;text-align:center;background:radial-gradient(circle,rgba(16,35,55,.96),rgba(6,12,20,.82) 68%,rgba(6,12,20,.15)) padding-box,conic-gradient(var(--green) var(--progress),rgba(44,68,95,.42) 0) border-box;box-shadow:0 0 48px rgba(87,199,255,.12)}.core:before,.core:after{content:"";position:absolute;inset:22px;border:1px dashed rgba(87,199,255,.4);border-radius:50%;animation:spin 13s linear infinite}.core:after{inset:62px;border-color:rgba(255,212,90,.4);animation-duration:8s;animation-direction:reverse}.core h2{margin:0;font-size:36px}.core p{margin:8px auto 0;color:var(--muted);max-width:196px}.phase{display:inline-block;margin-top:10px;border:1px solid var(--yellow);border-radius:6px;padding:5px 9px;color:var(--yellow);font-weight:900;text-transform:uppercase}.breachPct{margin-top:8px;color:var(--green);font-weight:900}.coreAction{position:relative;z-index:3;margin-top:10px;border:1px solid var(--yellow);border-radius:8px;background:#251c05;color:var(--yellow);font-weight:900;padding:9px 14px;cursor:pointer;text-transform:uppercase}.coreAction:hover{border-color:var(--green);color:var(--green)}
.node{position:absolute;z-index:3;width:132px;height:82px;padding:8px;cursor:pointer;overflow:hidden;transition:.16s}.node:hover{transform:translateY(-4px) scale(1.03);border-color:var(--blue)}.node.active{border-color:var(--yellow);box-shadow:0 0 0 1px rgba(255,212,90,.25),0 0 28px rgba(255,212,90,.18)}.node.cleared{border-color:rgba(76,255,149,.75);background:rgba(10,36,23,.9)}.node.rejected{border-color:rgba(255,77,102,.75);background:rgba(38,13,20,.9)}.node .sn{font-size:9px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.node strong{display:block;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.node small{display:block;margin-top:4px;color:var(--muted);font-size:10px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tag{display:inline-block;margin:5px 4px 0 0;border:1px solid #294058;border-radius:999px;padding:1px 5px;color:var(--muted);font-size:9px;text-transform:uppercase}.cash{color:var(--yellow);font-weight:900}.ship{position:absolute;z-index:4;left:50%;top:50%;width:26px;height:26px;border:2px solid var(--green);border-radius:50%;background:#06150d;box-shadow:0 0 18px var(--green);transform:translate(-50%,-50%);transition:.35s}.beam{position:absolute;z-index:1;left:50%;top:50%;width:2px;height:150px;background:linear-gradient(var(--blue),transparent);transform-origin:top center;opacity:0;box-shadow:0 0 20px var(--blue)}.beam.fire{opacity:.85;animation:pulse .35s ease 2}@keyframes pulse{50%{filter:brightness(2)}}
.sidebar{min-height:0;display:grid;grid-template-rows:minmax(0,1fr) minmax(132px,160px);gap:12px}.mission{min-height:0;overflow:auto;padding:12px}.mission h2{margin:0;font-size:22px}.mission p{margin:8px 0 10px;color:var(--muted)}.targetStats,.planner,.strategy{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:10px 0}.strategy{grid-template-columns:repeat(3,minmax(0,1fr))}.targetStats div,.campaign{border:1px solid #263b53;border-radius:8px;background:#07101a;padding:7px;min-width:0}.targetStats span,.campaign span{display:block;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.1em}.targetStats b,.campaign b{display:block;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.planner button,.strategy button{min-height:42px;border:1px solid #2b435e;border-radius:8px;background:#101b29;color:var(--text);cursor:pointer;text-align:left;padding:7px}.planner button:hover,.strategy button:hover{border-color:var(--blue)}.planner button:disabled{opacity:.45;cursor:default}.strategy button.active{border-color:var(--yellow);background:#251c05;box-shadow:0 0 20px rgba(255,212,90,.1)}.planner b,.strategy b{display:block;font-size:11px}.planner span,.strategy span{display:block;color:var(--muted);font-size:9px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.campaign{margin:10px 0}.campaign small{display:block;margin-top:6px;color:var(--muted)}.risk-clean{color:var(--green)}.risk-live{color:var(--blue)}.risk-hot{color:var(--yellow)}.risk-critical{color:var(--red)}.intake{border:1px solid #263b53;border-radius:8px;background:#07101a;padding:8px;margin:10px 0}.intake span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.11em}.intake b{display:block;margin-top:3px;color:var(--yellow)}.intake small{display:block;margin-top:3px;color:var(--muted)}.slots{display:grid;grid-template-columns:repeat(auto-fit,minmax(48px,1fr));gap:6px;margin-top:10px}.slot{height:34px;border:1px solid #263b53;border-radius:7px;background:#07101a;display:grid;place-items:center;color:var(--muted);font-size:9px;text-transform:uppercase}.slot.done{border-color:var(--green);color:var(--green);background:rgba(76,255,149,.08)}.slot.active{border-color:var(--yellow);color:var(--yellow)}.gates{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}.gate{border:1px solid #263b53;border-radius:7px;background:#07101a;padding:6px 7px;color:var(--muted);font-size:9px;text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.gate.pass{border-color:rgba(76,255,149,.7);color:var(--green);background:rgba(76,255,149,.08)}.gate.fail{border-color:rgba(255,77,102,.7);color:var(--red);background:rgba(255,77,102,.08)}.gate.attest{border-color:rgba(185,145,255,.75);color:var(--violet);background:rgba(185,145,255,.08)}.log{min-height:0;padding:12px;overflow:auto;font-family:Consolas,ui-monospace,monospace;font-size:12px}.log div{margin-bottom:5px}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--yellow)}.attestLog{color:var(--violet)}
.command{display:grid;grid-template-columns:240px 1fr 260px;gap:12px;padding:12px}.operator{border:1px solid #203248;border-radius:8px;background:#07101a;padding:10px;overflow:hidden}.operator span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.11em}.operator b{display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.actions{display:grid;grid-template-columns:repeat(9,minmax(64px,1fr));gap:8px}.action{min-height:58px;text-align:left;cursor:pointer;border:1px solid #2b435e;border-radius:8px;background:#101b29;padding:8px;opacity:.68;position:relative;transition:.14s}.action:hover{border-color:var(--blue);opacity:1}.action.primary{background:linear-gradient(180deg,#143322,#0b1912);border-color:rgba(76,255,149,.55)}.action.attest{background:linear-gradient(180deg,#27194a,#100a1d);border-color:rgba(185,145,255,.7);color:#ddceff}.action.danger{border-color:rgba(255,77,102,.5);color:#ffb8c2}.action.armed{opacity:1;transform:translateY(-3px);border-color:var(--yellow);box-shadow:0 0 0 1px rgba(255,212,90,.22),0 0 24px rgba(255,212,90,.18)}.action.armed:after{content:"ARMED";position:absolute;right:7px;top:5px;color:var(--yellow);font-size:8px;font-weight:900;letter-spacing:.08em}.action.locked{filter:saturate(.55);opacity:.42}.action b{display:block;font-size:12px}.action span{display:block;color:var(--muted);font-size:10px;margin-top:3px}.action .cost{color:var(--yellow);font-weight:900}.end{position:absolute;inset:0;display:none;place-items:center;background:rgba(2,5,9,.8);z-index:8}.end.show{display:grid}.modal{width:min(560px,90vw);border:1px solid var(--yellow);border-radius:10px;background:#09111c;padding:22px;text-align:center;box-shadow:0 0 60px rgba(255,212,90,.24)}.modal h2{font-size:42px;margin:0}.modal p{color:var(--muted)}
@media(max-width:1100px){body{overflow:auto}.game{height:auto}.hud,.stage,.command{grid-template-columns:1fr}.arena{height:640px}.actions{grid-template-columns:repeat(2,minmax(0,1fr))}.meters{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
</head>
<body>
<div class="game">
  <header class="hud">
    <div class="brand"><h1>SUBNET BREAKER</h1><div>break the target, replay the proof, attest the run, seal the reward</div></div>
    <div class="meters">
      <div class="meter"><span>score</span><b id="score">0</b></div>
      <div class="meter"><span>bounty</span><b id="bounty">0</b></div>
      <div class="meter"><span>sealed</span><b id="proofs">0</b></div>
      <div class="meter"><span>kill rate</span><b id="killRate">0%</b></div>
      <div class="meter"><span>focus</span><b id="combo">x1.0</b></div>
      <div class="meter"><span>sweep</span><b id="sweepText">0%</b><div class="bar"><i id="sweepFill"></i></div></div>
      <div class="meter"><span>blocks</span><b id="blocked">0</b></div>
      <div class="meter"><span>target</span><b id="targetNo">-</b></div>
    </div>
    <div><b id="clock">04:31</b><div class="muted">Round clock</div><div class="bar"><i id="timeFill" style="width:100%"></i></div></div>
  </header>
  <main class="stage">
    <section class="arena" id="arena">
      <div class="beam" id="beam"></div><div class="ship" id="ship"></div>
      <div class="core" id="core"><div><h2 id="coreVerb">LOAD</h2><p id="coreSub">Pick a subnet target. Reports do not score. Replayed witnesses do.</p><span id="phaseBadge" class="phase">target</span><div id="breachPct" class="breachPct">0% breached</div><button id="coreAction" class="coreAction" type="button">Probe</button></div></div>
      <div id="nodes"></div>
      <div id="end" class="end"><div class="modal"><h2 id="endTitle">SEASON CLEARED</h2><p id="endBody">Accepted proofs were written to the scanner ledger.</p><button class="action primary" onclick="newRun()"><b>Run it back</b><span>new local player</span></button></div></div>
    </section>
    <aside class="sidebar">
      <div class="mission">
        <h2 id="targetName">Loading</h2><p id="targetBrief"></p>
        <div class="targetStats">
          <div><span>risk</span><b id="riskText">-</b></div>
          <div><span>family</span><b id="familyText">-</b></div>
          <div><span>witness bits</span><b id="fieldsText">-</b></div>
          <div><span>route</span><b id="routeText">-</b></div>
        </div>
        <div class="planner"><button id="bestBounty" type="button"><b>Highest bounty</b><span>-</span></button><button id="safestTarget" type="button"><b>Lowest risk</b><span>-</span></button></div>
        <div class="strategy"><button data-strategy="stealth" type="button"><b>Stealth</b><span>-heat +energy</span></button><button data-strategy="balanced" type="button"><b>Balanced</b><span>normal costs</span></button><button data-strategy="overclock" type="button"><b>Overclock</b><span>-energy +heat</span></button></div>
        <div class="campaign"><span>Contract</span><b id="objectiveText">Seal 5 proofs before 5 blocks</b><div class="bar"><i id="objectiveFill"></i></div><small id="failureText">validator blocks remaining: 5</small></div>
        <div class="intake"><span>Scan Request</span><b id="intakeId">routing</b><small id="intakeMeta">POST /api/scanner/request</small></div>
        <div class="bar"><i id="heatFill"></i></div><p><b id="heatText">0%</b> validator heat / <b id="energyText">100%</b> agent energy</p>
        <div class="bar"><i id="energyFill" style="width:100%"></i></div><div id="slots" class="slots"></div>
        <div class="muted" style="margin-top:12px;text-transform:uppercase;letter-spacing:.11em">Verifier gates</div>
        <div id="gates" class="gates"></div>
      </div>
      <div id="log" class="log"></div>
    </aside>
  </main>
  <footer class="command">
    <div class="operator"><span>Miner Agent</span><b id="agentName">loading</b><small id="agentMeta" class="muted"></small></div>
    <div class="actions">
      <button class="action primary" data-action="probe"><b>1 Probe</b><span class="hint">read task</span><span class="cost"></span></button>
      <button class="action primary" data-action="encode"><b>2 Encode</b><span class="hint">family gate</span><span class="cost"></span></button>
      <button class="action primary" data-action="solve"><b>3 Solve</b><span class="hint">use witness</span><span class="cost"></span></button>
      <button class="action primary" data-action="replay"><b>4 Replay</b><span class="hint">local verifier</span><span class="cost"></span></button>
      <button class="action attest" data-action="attest"><b>5 Attest</b><span class="hint">bind receipt</span><span class="cost"></span></button>
      <button class="action primary" data-action="submit"><b>6 Seal</b><span class="hint">POST submit</span><span class="cost"></span></button>
      <button class="action danger" data-action="report"><b>7 Report</b><span class="hint">no witness</span><span class="cost"></span></button>
      <button class="action danger" data-action="forge"><b>8 Forge</b><span class="hint">bad witness</span><span class="cost"></span></button>
      <button class="action danger" data-action="decoy"><b>9 Cooldown</b><span class="hint">drop heat</span><span class="cost"></span></button>
    </div>
    <div class="operator"><span>Verifier</span><b>/replay -> /attest -> /submit-attested</b><small class="muted">attest is local simulated TEE; seal writes ledger</small></div>
  </footer>
</div>
<script>
const phases = ['probe','encode','solve','replay','attest','seal','sealed'];
const playerParam = new URLSearchParams(location.search).get('player');
const player = playerParam || localStorage.scannerPlayer || ('hk_player_' + Math.random().toString(16).slice(2));
if(!playerParam)localStorage.scannerPlayer = player;
const state = {tasks:[], intake:null, selected:0, phase:0, score:0, proofs:0, killRate:0, blocked:0, energy:100, heat:0, sweep:0, turns:0, time:271, goal:5, maxBlocks:5, campaignEnded:false, cleared:new Set(), rejected:new Set(), pendingProofs:{}, replayOk:{}, attested:{}, lastGates:null, combo:1, strategy:'balanced'};
let actionQueue = Promise.resolve();
const $ = id => document.getElementById(id);
const strategies = {stealth:{label:'stealth',energy:1.25,heat:.7},balanced:{label:'balanced',energy:1,heat:1},overclock:{label:'overclock',energy:.75,heat:1.35}};
function newRun(){const p='hk_player_'+Math.random().toString(16).slice(2);localStorage.scannerPlayer=p;location.href=location.pathname+'?player='+encodeURIComponent(p)}
function log(msg, cls='muted'){const d=document.createElement('div');d.className=cls;d.textContent='[t-'+String(271-state.time).padStart(3,'0')+'] '+msg;$('log').prepend(d)}
function current(){return state.tasks[state.selected]}
function pos(i,n){const a=(-90+i*(360/n))*Math.PI/180;return {left:50+Math.cos(a)*42,top:50+Math.sin(a)*41}}
function fire(){const b=$('beam');b.classList.remove('fire');void b.offsetWidth;b.classList.add('fire');flash('strike')}
function flash(cls){const a=$('arena');a.classList.remove('strike','reject','breached');void a.offsetWidth;a.classList.add(cls);setTimeout(()=>a.classList.remove(cls),800)}
function nextAction(){return state.phase===0?'probe':state.phase===1?'encode':state.phase===2?'solve':state.phase===3?'replay':state.phase===4?'attest':state.phase===5?'submit':'decoy'}
function actionLabel(){return state.phase===0?'Probe target':state.phase===1?'Encode invariant':state.phase===2?'Solve witness':state.phase===3?'Replay proof':state.phase===4?'Attest run':state.phase===5?'Seal reward':'Next target'}
function targetRisk(t=current()){return Math.min(42,Math.round(8+(t.required_fields||[]).length*4+(t.bounty_weight||1)*8))}
function riskBand(v){return v>=34?'critical':v>=26?'hot':v>=18?'live':'clean'}
function focusHeat(name,base){if(base<=0)return 0;return ['report','forge'].includes(name)?base:Math.max(1,Math.ceil(base/state.combo))}
function strategyCost(name,base){const s=strategies[state.strategy]||strategies.balanced;if(name==='report'||name==='forge'||name==='decoy')return base;return [Math.max(1,Math.ceil(base[0]*s.energy)),Math.ceil(base[1]*s.heat)]}
function actionCost(name){const t=current();const fields=(t.required_fields||[]).length;const r=targetRisk(t);const base={probe:[6,Math.ceil(r*.35)],encode:[8+fields,Math.ceil(r*.45)],solve:[12+fields*2,Math.ceil(r*.62)],report:[5,Math.ceil(r*.7)],forge:[7,Math.ceil(r*.85)],replay:[8,Math.ceil(r*.5)],attest:[5,Math.ceil(r*.28)],submit:[4,0]}[name]||[0,0];const tuned=strategyCost(name,base);return [tuned[0],focusHeat(name,tuned[1])]}
function actionCostText(name){if(name==='decoy')return '+E12 / -H28';const c=actionCost(name);return 'E'+c[0]+' / H'+c[1]}
function sweepDelta(name){if(name==='decoy')return -26;if(name==='submit')return 3;if(name==='report')return 28;if(name==='forge')return 36;const route=strategies[state.strategy]||strategies.balanced;const mod=route.label==='overclock'?7:route.label==='stealth'?-4:0;return Math.max(2,Math.ceil(4+targetRisk(current())*.18+state.phase*2+mod))}
function advanceSweep(name){state.turns++;state.sweep=Math.max(0,Math.min(120,state.sweep+sweepDelta(name)));if(state.sweep>=100){state.blocked++;state.sweep=38;state.heat=Math.min(95,state.heat+18);state.phase=Math.max(0,state.phase-1);state.combo=1;flash('reject');log('validator sweep caught noisy route: one gate burned','bad');return true}return false}
function finishCampaign(title,body){if(state.campaignEnded)return;state.campaignEnded=true;$('endTitle').textContent=title;$('endBody').textContent=body;$('end').classList.add('show')}
function renderCampaign(){const pct=Math.min(100,state.proofs/state.goal*100);$('objectiveText').textContent='Seal '+state.goal+' proofs before '+state.maxBlocks+' blocks';$('objectiveFill').style.width=pct+'%';$('failureText').textContent='validator blocks remaining: '+Math.max(0,state.maxBlocks-state.blocked)+' / sweep '+Math.round(state.sweep)+'%'}
function openTargetIndexes(){return state.tasks.map((_,i)=>i).filter(i=>!state.cleared.has(i))}
function bestTarget(mode){const xs=openTargetIndexes();if(!xs.length)return -1;xs.sort((a,b)=>{const ta=state.tasks[a],tb=state.tasks[b];const ra=targetRisk(ta),rb=targetRisk(tb);if(mode==='safe')return ra-rb || tb.bounty_weight-ta.bounty_weight;return tb.bounty_weight-ta.bounty_weight || ra-rb});return xs[0]}
function routeLabel(i){if(i<0)return 'none';const t=state.tasks[i];return 'SN'+t.target.netuid+' '+t.target.name+' / +'+Math.round(t.bounty_weight*1000)+' / risk '+targetRisk(t)}
function renderPlanner(){[['bestBounty','bounty'],['safestTarget','safe']].forEach(([id,mode])=>{const b=$(id),i=bestTarget(mode);b.disabled=i<0||i===state.selected;b.querySelector('span').textContent=routeLabel(i)})}
function selectBest(mode){const i=bestTarget(mode);if(i<0||state.campaignEnded)return;select(i);log('route planner selected '+routeLabel(i),'warn')}
function setStrategy(name){if(!strategies[name]||state.campaignEnded)return;state.strategy=name;log('strategy set: '+name,'warn');render()}
function renderStrategy(){document.querySelectorAll('[data-strategy]').forEach(b=>b.classList.toggle('active',b.dataset.strategy===state.strategy))}
function renderNodes(){const root=$('nodes');root.innerHTML='';state.tasks.forEach((t,i)=>{const p=pos(i,state.tasks.length);const risk=targetRisk(t);const n=document.createElement('div');n.className='node'+(i===state.selected?' active':'')+(state.cleared.has(i)?' cleared':'')+(state.rejected.has(i)?' rejected':'');n.style.left=`calc(${p.left}% - 66px)`;n.style.top=`calc(${p.top}% - 41px)`;n.title=t.objective;n.onclick=()=>select(i);n.innerHTML=`<div class="sn">SN${t.target.netuid} / ${t.expected_family}</div><strong>${t.target.name}</strong><small>+${Math.round(t.bounty_weight*1000)} bounty / risk ${risk}</small><span class="tag">${state.cleared.has(i)?'sealed':state.rejected.has(i)?'blocked':'target'}</span>`;root.appendChild(n)})}
function renderSlots(){const root=$('slots');root.innerHTML='';phases.forEach((p,i)=>{const s=document.createElement('div');s.className='slot'+(i<state.phase?' done':i===state.phase?' active':'');s.textContent=p;root.appendChild(s)})}
function renderGates(){const root=$('gates');root.innerHTML='';const gates=state.lastGates||{task_matches:null,nonce_matches:null,family_aligned:null,decode_map_present:null,replay_succeeds:null,attestation_receipt:null};Object.entries(gates).forEach(([k,v])=>{const g=document.createElement('div');g.className='gate '+(v===true?'pass':v===false?'fail':'')+(k.includes('attestation')?' attest':'');g.textContent=(v===true?'PASS ':v===false?'FAIL ':'WAIT ')+k.replaceAll('_',' ');root.appendChild(g)})}
function validActions(){if(state.phase===0)return new Set(['probe','decoy']);if(state.phase===1)return new Set(['encode','decoy']);if(state.phase===2)return new Set(['solve','report','forge','decoy']);if(state.phase===3)return new Set(['replay','decoy']);if(state.phase===4)return new Set(['attest','decoy']);if(state.phase===5)return new Set(['submit','decoy']);return new Set(['decoy'])}
function renderActions(){const valid=validActions();const next=nextAction();document.querySelectorAll('[data-action]').forEach(b=>{const a=b.dataset.action;b.classList.toggle('armed',a===next||(['report','forge'].includes(a)&&state.phase===2));b.classList.toggle('locked',!valid.has(a));const cost=b.querySelector('.cost');if(cost)cost.textContent=actionCostText(a)})}
function select(i){state.selected=i;state.phase=state.cleared.has(i)?6:0;state.lastGates=null;log('target locked: SN'+current().target.netuid+' '+current().target.name,'warn');render()}
function renderIntake(){if(!state.intake)return;const r=state.intake.request||{};$('intakeId').textContent=r.request_id||'request';$('intakeMeta').textContent=(r.repo||'repo')+' / '+(state.intake.routed_count||0)+' routed / scored='+state.intake.scored}
function render(){if(!state.tasks.length)return;const t=current();const risk=targetRisk(t);const band=riskBand(risk);const breach=Math.min(100,Math.max(0,Math.round(state.phase/5*100)));$('targetName').textContent='SN'+t.target.netuid+': '+t.target.name;$('targetBrief').textContent=t.objective;$('riskText').textContent=band.toUpperCase()+' '+risk;$('riskText').className='risk-'+band;$('familyText').textContent=t.expected_family;$('fieldsText').textContent=(t.required_fields||[]).join(', ');$('routeText').textContent=risk>=26?'high bounty / high heat':'standard replay';$('phaseBadge').textContent=phases[state.phase];$('coreVerb').textContent=phases[state.phase].toUpperCase();$('coreSub').textContent='Break '+t.expected_family+' on SN'+t.target.netuid+' with '+state.strategy+' routing; validator sweep punishes noisy routes.';$('breachPct').textContent=breach+'% breached';$('core').style.setProperty('--progress',breach+'%');$('coreAction').textContent=actionLabel();$('score').textContent=state.score;$('bounty').textContent=Math.round(t.bounty_weight*1000);$('proofs').textContent=state.proofs;$('killRate').textContent=Math.round(state.killRate*100)+'%';$('combo').textContent='x'+state.combo.toFixed(1);$('sweepText').textContent=Math.round(state.sweep)+'%';$('sweepFill').style.width=Math.min(100,state.sweep)+'%';$('blocked').textContent=state.blocked;$('targetNo').textContent=(state.selected+1)+'/'+state.tasks.length;$('heatText').textContent=Math.round(state.heat)+'%';$('heatFill').style.width=state.heat+'%';$('energyText').textContent=Math.round(state.energy)+'%';$('energyFill').style.width=state.energy+'%';const p=pos(state.selected,state.tasks.length);$('ship').style.left=p.left+'%';$('ship').style.top=p.top+'%';$('beam').style.transform=`rotate(${Math.atan2(p.top-50,p.left-50)*180/Math.PI+90}deg)`;$('agentName').textContent=player;$('agentMeta').textContent='local game identity; '+state.strategy+' route; simulated attestation; no chain writes';renderIntake();renderCampaign();renderPlanner();renderStrategy();renderNodes();renderSlots();renderGates();renderActions();if(state.proofs>=state.goal)finishCampaign('CONTRACT WON','You sealed enough replayed proofs before validator blocks burned the route.');if(state.blocked>=state.maxBlocks)finishCampaign('TRACE BURNED','Too many rejected gates. Start a new run and route cleaner proofs.')}
function spend(e,h){state.energy=Math.max(0,state.energy-e);state.heat=Math.min(100,state.heat+h);if(state.heat>=100){state.blocked++;state.heat=58;state.energy=Math.max(15,state.energy-18);state.phase=Math.max(0,state.phase-1);state.combo=1;flash('reject');log('validator countermeasure fired: chain rolled back one gate','bad')}}
function nextOpen(){for(let step=1;step<=state.tasks.length;step++){const i=(state.selected+step)%state.tasks.length;if(!state.cleared.has(i))return i}return -1}
function resumeOpenTarget(){if(!state.tasks.length)return;const n=nextOpen();if(n<0){state.phase=6;$('end').classList.add('show');return}if(state.cleared.has(state.selected)){state.selected=n;state.phase=0;state.lastGates=null;log('resumed next uncleared target','warn')}}
async function syncState(announce=false){const s=await fetch('/api/scanner/state?miner_hotkey='+encodeURIComponent(player)).then(r=>r.json());const accepted=new Set(s.accepted_task_ids||[]);const rejected=new Set(s.rejected_task_ids||[]);state.cleared=new Set();state.rejected=new Set();state.tasks.forEach((t,i)=>{if(accepted.has(t.task_id))state.cleared.add(i);else if(rejected.has(t.task_id))state.rejected.add(i)});state.score=Math.round((s.score||0)*1000);state.proofs=s.accepted||0;state.blocked=Math.max(state.blocked,s.rejected||0);if(announce&&s.attempts)log('restored ledger: '+s.accepted+' accepted / '+s.rejected+' rejected / score '+s.score,'warn')}
async function syncBenchmark(){const b=await fetch('/api/scanner/benchmark').then(r=>r.json());const row=(b.miners||[]).find(m=>m.miner_hotkey===player);state.killRate=row?Number(row.kill_rate||0):0}
async function runAgentSolve(mode='valid'){const t=current();const solved=await fetch('/api/scanner/agent/solve?task_id='+encodeURIComponent(t.task_id)+'&miner_hotkey='+encodeURIComponent(player)+'&mode='+encodeURIComponent(mode)).then(r=>r.json());if(!solved.submission){throw new Error(solved.error||'agent solve failed')}state.pendingProofs[t.task_id]=solved.submission;delete state.replayOk[t.task_id];delete state.attested[t.task_id];const cls=mode==='valid'?'ok':'bad';const label=mode==='valid'?'agent produced proof artifact ':mode==='report_only'?'agent wrote report-only decoy ':'agent forged corrupt artifact ';log(label+solved.artifact_sha256.slice(0,12),cls)}
async function runReplay(){const t=current();const proof=state.pendingProofs[t.task_id];if(!proof){state.blocked++;state.combo=1;state.phase=2;state.rejected.add(state.selected);state.lastGates={task_matches:null,nonce_matches:null,family_aligned:null,decode_map_present:false,replay_succeeds:false,attestation_receipt:null};flash('reject');log('REPLAY FAILED: no pending proof artifact; run Solve first','bad');return false}const payload={...proof,miner_hotkey:player,report:'dry-run replay through Subnet Breaker UI'};const verdict=await fetch('/api/scanner/replay',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());state.lastGates={...(verdict.gates||{}),attestation_receipt:false};if(verdict.accepted){state.replayOk[t.task_id]=true;state.rejected.delete(state.selected);log('REPLAY ACCEPTED: '+verdict.replay_target_id+' / no ledger write','ok');return true}state.blocked++;state.combo=1;state.phase=2;state.rejected.add(state.selected);delete state.pendingProofs[t.task_id];delete state.replayOk[t.task_id];delete state.attested[t.task_id];flash('reject');log('REPLAY REJECTED: '+(verdict.reasons||[verdict.error||'unknown']).join(', '),'bad');return false}
async function runAttestation(){const t=current();const proof=state.pendingProofs[t.task_id];if(!proof){state.blocked++;state.combo=1;flash('reject');log('ATTEST FAILED: no proof artifact; run Solve first','bad');return false}if(!state.replayOk[t.task_id]){state.blocked++;state.combo=1;flash('reject');log('ATTEST FAILED: replay gate has not passed','bad');return false}const verdict=await fetch('/api/scanner/attest',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({...proof,miner_hotkey:player,report:'local attestation dry-run through Subnet Breaker UI'})}).then(r=>r.json());state.lastGates={...(verdict.gates||{}),attestation_receipt:!!verdict.attested};if(verdict.attested&&verdict.receipt){state.attested[t.task_id]=verdict.receipt;proof.claim={...(proof.claim||{}),attestation:verdict.receipt};log('ATTESTED: simulated local TEE receipt '+verdict.receipt.artifact_sha256.slice(0,12)+' bound to claim','attestLog');return true}state.blocked++;state.combo=1;flash('reject');log('ATTEST REJECTED: '+(verdict.reasons||[verdict.error||'unknown']).join(', '),'bad');return false}
async function seal(){const t=current();const proof=state.pendingProofs[t.task_id];if(!proof){state.blocked++;state.combo=1;flash('reject');log('REJECTED: no pending proof artifact; run Solve first','bad');return}if(!state.replayOk[t.task_id]){state.blocked++;state.combo=1;flash('reject');log('REJECTED: replay gate has not passed','bad');return}if(!state.attested[t.task_id]){state.blocked++;state.combo=1;flash('reject');log('REJECTED: attestation receipt has not been bound','bad');return}const payload={...proof,miner_hotkey:player,report:'sealed with local attestation receipt through Subnet Breaker UI'};const verdict=await fetch('/api/scanner/submit-attested',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());delete state.pendingProofs[t.task_id];delete state.replayOk[t.task_id];delete state.attested[t.task_id];if(verdict.accepted){state.phase=6;state.heat=Math.max(0,state.heat-20);state.combo=Math.min(2.5,state.combo+.15);flash('breached');log('SEALED: backend accepted '+verdict.replay_target_id+' / focus '+state.combo.toFixed(1),'ok')}else{state.blocked++;state.combo=1;flash('reject');log('REJECTED: '+(verdict.reasons||[verdict.error||'unknown']).join(', '),'bad')}await syncState(false);await syncBenchmark();const board=await fetch('/api/scanner/leaderboard').then(r=>r.json());log('your ledger score '+(state.score/1000).toFixed(1)+' / kill '+Math.round(state.killRate*100)+'%','warn');if(board.miners&&board.miners[0]&&board.miners[0].miner_hotkey!==player)log('global #1 '+board.miners[0].miner_hotkey+' score '+board.miners[0].score,'warn');if(verdict.accepted){const n=nextOpen();if(n<0){$('end').classList.add('show')}else{state.selected=n;state.phase=0;state.lastGates=null;log('advanced to next subnet target','warn')}}}
async function act(name){if(!state.tasks.length||state.campaignEnded)return;if(name==='decoy'){state.heat=Math.max(0,state.heat-28);state.energy=Math.min(100,state.energy+12);advanceSweep('decoy');log('cooldown route deployed: heat and sweep dumped, tempo lost','ok');render();return}if(state.cleared.has(state.selected)){const n=nextOpen();if(n<0){finishCampaign('SEASON CLEARED','Accepted proofs were written to the scanner ledger.');return}select(n);return}const needed={probe:0,encode:1,solve:2,replay:3,attest:4,submit:5,report:2,forge:2}[name];if(state.phase!==needed){state.blocked++;state.combo=1;state.sweep=Math.min(100,state.sweep+18);flash('reject');log('gate rejected: run the chain in order','bad');render();return}const c=actionCost(name);if(state.energy<=c[0]){state.blocked++;state.sweep=Math.min(100,state.sweep+14);flash('reject');log('agent exhausted: cooldown before continuing','bad');render();return}fire();if(name==='probe'){spend(c[0],c[1]);state.phase=1;log('task fetched; target risk '+targetRisk(current())+' now prices every gate','ok')}if(name==='encode'){spend(c[0],c[1]);state.phase=2;log('family gate aligned: '+current().expected_family,'ok')}if(name==='solve'){spend(c[0],c[1]);await runAgentSolve();state.phase=3;log('witness fields prepared: '+current().required_fields.join(', '),'ok')}if(name==='report'){spend(c[0],c[1]);await runAgentSolve('report_only');state.phase=3;log('report-only claim prepared; replay should reject it','warn')}if(name==='forge'){spend(c[0],c[1]);await runAgentSolve('bad_witness');state.phase=3;log('forged witness prepared; replay should catch this','warn')}if(name==='replay'){spend(c[0],c[1]);if(await runReplay())state.phase=4}if(name==='attest'){spend(c[0],c[1]);if(await runAttestation())state.phase=5}if(name==='submit'){spend(c[0],c[1]);await seal()}advanceSweep(name);render()}
function enqueue(name){actionQueue=actionQueue.then(()=>act(name)).catch(e=>log('action failed: '+e.message,'bad'))}
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>enqueue(b.dataset.action));document.querySelectorAll('[data-strategy]').forEach(b=>b.onclick=()=>setStrategy(b.dataset.strategy));$('coreAction').onclick=()=>enqueue(nextAction());$('bestBounty').onclick=()=>selectBest('bounty');$('safestTarget').onclick=()=>selectBest('safe');document.addEventListener('keydown',e=>{const m={'1':'probe','2':'encode','3':'solve','4':'replay','5':'attest','6':'submit','7':'report','8':'forge','9':'decoy'};if(m[e.key])enqueue(m[e.key]);if(e.key==='s')setStrategy('stealth');if(e.key==='b')setStrategy('balanced');if(e.key==='o')setStrategy('overclock')});
function tick(){state.time=Math.max(0,state.time-1);if(state.time<=0){state.blocked++;state.time=271;state.heat=Math.min(95,state.heat+20);log('round clock expired','bad')}state.energy=Math.min(100,state.energy+.2);state.heat=Math.max(0,state.heat-.05);const m=String(Math.floor(state.time/60)).padStart(2,'0'),s=String(state.time%60).padStart(2,'0');$('clock').textContent=m+':'+s;$('timeFill').style.width=Math.max(0,state.time/271*100)+'%';render()}
async function boot(){const intake=await fetch('/api/scanner/request',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({requester:'subnet-breaker-operator',repo:'https://github.com/cathedralai/subnet-targets',commit:'local-round',objective:'Find replayable money-math or incentive bugs.',scope:['validator','rewards','weights'],requested_families:['money_math','subnet_incentive'],max_tasks:12})}).then(r=>r.json());state.intake=intake;state.tasks=intake.routed_tasks||[];await syncState(true);await syncBenchmark();resumeOpenTarget();log('scan request '+intake.request.request_id+' routed '+state.tasks.length+' replay tasks','warn');log('keys: 1 probe, 2 encode, 3 solve, 4 replay, 5 attest, 6 seal, 7 report-only, 8 forge, 9 cooldown','warn');render();setInterval(tick,1000)}
boot().catch(e=>log('boot failed: '+e.message,'bad'));
</script>
</body>
</html>"""


def main() -> None:
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8800)


if __name__ == "__main__":
    main()
