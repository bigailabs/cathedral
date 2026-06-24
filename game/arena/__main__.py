"""python -m game.arena [rounds] - run the arena E2E, write arena.html + reports.

Outputs (in game/arena/out/):
  arena.html            the live visual arena (open in a browser)
  score_report.json     per-agent weights + gate booleans
  anticheat_report.json rejected submissions + the gate each tripped
  round_anchor.json     Merkle round commitment
  proof_bundle.json     portable winner proof bundle
  scanner_contract.json product-facing task/submission/verdict example
  scanner_request.json organic scan-request intake routed to replay tasks
  scanner_benchmark.json replay-kill-rate metric artifact
  scanner_playthrough.json  scoreful game-loop verifier artifact
  scanner_game_screenshot.json  optional /game screenshot manifest with --shot
  traces.jsonl          full agent traces (future training data)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .engine import ArenaEngine
from . import playthrough, reports, scanner
from .ui import write_html

OUT = Path(__file__).resolve().parent / "out"


def main() -> None:
    args = [a for a in sys.argv[1:]]
    submitted = "--submitted" in args
    season = "--season" in args
    shot = "--shot" in args
    pos = [a for a in args if not a.startswith("--")]
    rounds = int(pos[0]) if pos else (3 if season else 1)
    OUT.mkdir(exist_ok=True)
    eng = ArenaEngine()
    mode = ("REAL external-agent submission (verify-by-receipt)" if submitted else "in-process")
    arch_by_hk = {s.hotkey: s.archetype for s in eng.roster}
    traces = []
    if season:
        mode += " / SEASON"
        result, _state = eng.run_season(rounds, submitted=submitted,
                                        state_path=str(OUT / "season_state.json"))
        for a in result.agents:
            traces.append(reports.trace_training_row(
                a, result.weights.get(a.run.miner_hotkey, 0.0),
                arch_by_hk.get(a.run.miner_hotkey, "honest"), rounds))
    else:
        result = None
        for rnd in range(1, rounds + 1):
            result = eng.run_submitted(rnd) if submitted else eng.run(rnd)
            for a in result.agents:
                traces.append(reports.trace_training_row(
                    a, result.weights.get(a.run.miner_hotkey, 0.0),
                    arch_by_hk.get(a.run.miner_hotkey, "honest"), rnd))

    html_path = write_html(result, str(OUT / "arena.html"))

    # Optional self-verifying screenshot deliverables. `arena.png` captures the
    # report artifact; `scanner_game.png` captures the playable /game surface.
    # Best-effort - never fails the run if Edge is absent.
    shot_manifest = {"requested": shot}
    game_shot_manifest = {"requested": shot}
    if shot:
        from . import screenshot
        png = OUT / "arena.png"
        res = screenshot.capture(html_path, png)
        shot_manifest = {"requested": True, "captured_from": str(html_path), **res}
        (OUT / "screenshot.json").write_text(json.dumps(shot_manifest, indent=2, default=str))
        game_png = OUT / "scanner_game.png"
        game_res = screenshot.shoot_scanner_game(game_png)
        game_shot_manifest = {
            "requested": True,
            "captured_from": "/game",
            **game_res,
        }
        (OUT / "scanner_game_screenshot.json").write_text(
            json.dumps(game_shot_manifest, indent=2, default=str)
        )

    score = reports.score_report(result, eng.roster)      # enriched: + verification block
    (OUT / "score_report.json").write_text(json.dumps(score, indent=2, default=str))
    (OUT / "anticheat_report.json").write_text(
        json.dumps(reports.anticheat_report(result), indent=2, default=str))
    (OUT / "round_anchor.json").write_text(json.dumps(result.anchor, indent=2))
    # Product-facing contract: the simple Scanner/Hunter request-response shape,
    # with a deterministic verdict proving prose/category alone cannot score.
    scan_task = scanner.issue_task(0)
    scan_good = scanner.example_accepted_submission(scan_task)
    scan_report_only = scanner.ScannerSubmission(
        task_id=scan_task.task_id,
        miner_hotkey="hk_report_only",
        nonce=scan_task.nonce,
        proof_family=scan_task.expected_family,
        witness=None,
        claim={
            "schema": scanner.SCHEMA_CLAIM,
            "title": "Correct-looking but unverified claim",
            "category": scan_task.expected_family,
            "severity": "critical",
            "impact": "High-severity prose is not enough without replay.",
        },
        report="Correct-looking vulnerability explanation. Ignored by scoring.",
    )
    (OUT / "scanner_contract.json").write_text(json.dumps({
        "task": scan_task.manifest(),
        "accepted_submission": scan_good.as_artifact(),
        "accepted_verdict": scanner.verify_submission(scan_task, scan_good).__dict__,
        "report_only_submission": scan_report_only.as_artifact(),
        "report_only_verdict": scanner.verify_submission(scan_task, scan_report_only).__dict__,
        "rule": "reports and structured claims are metadata; only replayable witnesses score",
    }, indent=2, default=str))
    scan_request = scanner.intake_scan_request({
        "requester": "operator-demo",
        "repo": "https://github.com/example/subnet",
        "commit": "demo-commit",
        "objective": "Find replayable money-math or incentive bugs.",
        "scope": ["validator", "rewards", "weights"],
        "requested_families": ["subnet_incentive", "money_math"],
        "max_tasks": 3,
    })
    (OUT / "scanner_request.json").write_text(
        json.dumps(scan_request, indent=2, default=str)
    )
    bench_ledger = OUT / "scanner_benchmark_ledger.jsonl"
    if bench_ledger.exists():
        bench_ledger.unlink()
    scanner.record_submission(bench_ledger, scan_task, scan_good)
    scanner.record_submission(bench_ledger, scan_task, scan_report_only)
    (OUT / "scanner_benchmark.json").write_text(
        json.dumps(scanner.benchmark(bench_ledger), indent=2, default=str)
    )
    playthrough_ledger = OUT / "scanner_playthrough_ledger.jsonl"
    playthrough_ledger.unlink(missing_ok=True)
    playthrough_report = playthrough.run_playthrough(
        ledger_path=str(playthrough_ledger),
        miner_hotkey="hk_playthrough",
    )
    (OUT / "scanner_playthrough.json").write_text(
        json.dumps(playthrough_report, indent=2, default=str)
    )
    # export a portable, independently-verifiable proof bundle for EVERY winner —
    # not just the top earner — so each breaching miner ships its own re-checkable proof.
    from .bundle import build_bundle, verify_bundle
    winners = [a for a in result.agents if a.gates.passed()]
    _bv = {"ok": False}
    if winners:
        top = max(winners, key=lambda a: result.emissions.get(a.run.miner_hotkey, 0))
        pb = build_bundle(result, top.run.agent_id)
        (OUT / "proof_bundle.json").write_text(json.dumps(pb, indent=2))   # top earner (back-compat)
        _bv = verify_bundle(pb)
        all_bundles = [build_bundle(result, a.run.agent_id) for a in winners]
        (OUT / "proof_bundles.json").write_text(json.dumps({
            "round": result.round_no, "count": len(all_bundles),
            "bundles": all_bundles}, indent=2, default=str))
    with (OUT / "traces.jsonl").open("w") as fh:
        for t in traces:
            fh.write(json.dumps(t, default=str) + "\n")
    # a self-describing dataset card next to the labeled traces (training-data manifest)
    (OUT / "traces_dataset.json").write_text(
        json.dumps(reports.dataset_card(traces), indent=2, default=str))
    # self-verify: re-check the whole round we just wrote, OFFLINE + no engine, and
    # persist the verdict — every E2E independently re-checks its own artifacts.
    from . import verify
    verdict = verify.verify_round(OUT)
    (OUT / "round_verdict.json").write_text(json.dumps(verdict, indent=2, default=str))

    # console summary
    print(f"\nCATHEDRAL ARENA - {result.season} round {result.round_no}  [{mode}]  "
          f"({result.corpus_summary['targets']} targets, "
          f"{result.corpus_summary['proof_tasks']} CNFs)")
    print("-" * 72)
    for a in sorted(result.agents, key=lambda a: -result.emissions.get(a.run.miner_hotkey, 0.0)):
        hk = a.run.miner_hotkey
        emit = result.emissions.get(hk, 0.0)
        arch = next(s.archetype for s in eng.roster if s.hotkey == hk)
        tag = "BREACH" if a.gates.passed() else "REJECT"
        why = f"prov {a.gates.provenance_grade} / {result.ranks.get(hk,'')}" if a.gates.passed() else a.gates.first_failure()
        print(f"  {tag:6s} {a.run.agent_id:16s} {arch:12s} {emit:7.0f} tau  {why}")
    print("-" * 72)
    print(f"  wrote {html_path}")
    print(f"  wrote {OUT/'score_report.json'}, {OUT/'anticheat_report.json'}, "
          f"{OUT/'scanner_contract.json'}, {OUT/'scanner_request.json'}, "
          f"{OUT/'scanner_benchmark.json'}, "
          f"{OUT/'scanner_playthrough.json'}, {OUT/'traces.jsonl'}")
    print(f"  playthrough: {'OK' if playthrough_report['ok'] else 'FAILED'} "
          f"[artifact: {OUT/'scanner_playthrough.json'}]")
    if winners:
        print(f"  proof bundle: {OUT/'proof_bundle.json'}  (verify: python -m game.arena.bundle "
              f"{OUT/'proof_bundle.json'}  -> {'OK' if _bv['ok'] else 'INVALID'})")
    _vn = sum(1 for c in verdict["checks"] if c["ok"])
    print(f"  round verify: {'VERIFIED ✓' if verdict['ok'] else 'INVALID ✗ ' + ','.join(verdict['required_failed'])}"
          f"  ({_vn}/{len(verdict['checks'])} checks, offline)  "
          f"[re-run: python -m game.arena.verify {OUT}]")
    if shot:
        if shot_manifest.get("ok"):
            print(f"  screenshot: {shot_manifest['png']} ({shot_manifest['bytes']} bytes) "
                  f"[manifest: {OUT/'screenshot.json'}]")
        else:
            print(f"  screenshot: SKIPPED ({shot_manifest.get('reason')}) "
                  f"[manifest: {OUT/'screenshot.json'}]")
        if game_shot_manifest.get("ok"):
            print(f"  game screenshot: {game_shot_manifest['png']} "
                  f"({game_shot_manifest['bytes']} bytes) "
                  f"[manifest: {OUT/'scanner_game_screenshot.json'}]")
        else:
            print(f"  game screenshot: SKIPPED ({game_shot_manifest.get('reason')}) "
                  f"[manifest: {OUT/'scanner_game_screenshot.json'}]")
    print(f"  open: file://{html_path}")


if __name__ == "__main__":
    main()
