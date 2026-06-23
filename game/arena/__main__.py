"""python -m game.arena [rounds]  — run the arena E2E, write arena.html + reports.

Outputs (in game/arena/out/):
  arena.html            the live visual arena (open in a browser)
  score_report.json     per-agent weights + gate booleans
  anticheat_report.json rejected submissions + the gate each tripped
  scanner_contract.json product-facing task/submission/verdict example
  traces.jsonl          full agent traces (future training data)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .engine import ArenaEngine
from . import scanner
from .ui import write_html

OUT = Path(__file__).resolve().parent / "out"


def main() -> None:
    args = [a for a in sys.argv[1:]]
    submitted = "--submitted" in args
    season = "--season" in args
    pos = [a for a in args if not a.startswith("--")]
    rounds = int(pos[0]) if pos else (3 if season else 1)
    OUT.mkdir(exist_ok=True)
    eng = ArenaEngine()
    mode = ("REAL external-agent submission (verify-by-receipt)" if submitted else "in-process")
    traces = []
    if season:
        mode += " · SEASON"
        result, _state = eng.run_season(rounds, submitted=submitted,
                                        state_path=str(OUT / "season_state.json"))
        for a in result.agents:
            traces.append({**a.run.__dict__, "gates": a.gates.as_dict(),
                           "weight": result.weights.get(a.run.miner_hotkey, 0.0), "round": rounds})
    else:
      result = None
      for rnd in range(1, rounds + 1):
        result = eng.run_submitted(rnd) if submitted else eng.run(rnd)
        for a in result.agents:
            traces.append({**a.run.__dict__, "gates": a.gates.as_dict(),
                           "weight": result.weights.get(a.run.miner_hotkey, 0.0),
                           "round": rnd})

    html_path = write_html(result, str(OUT / "arena.html"))

    score = {
        "season": result.season, "round": result.round_no,
        "signed_vector": result.signed_vector,
        "agents": [{
            "agent_id": a.run.agent_id, "hotkey": a.run.miner_hotkey,
            "archetype": next(s.archetype for s in eng.roster if s.hotkey == a.run.miner_hotkey),
            "environment": a.run.environment, "target_netuid": a.run.target_netuid,
            "tier": a.mission.tier, "attestation_required": a.mission.attestation_required,
            "weight": round(result.weights.get(a.run.miner_hotkey, 0.0), 4),
            "emissions_tau": result.emissions.get(a.run.miner_hotkey, 0.0),
            "rank": result.ranks.get(a.run.miner_hotkey, ""),
            "provenance_grade": a.gates.provenance_grade,
            "passed": a.gates.passed(), "gates": a.gates.as_dict(),
        } for a in result.agents],
        "breaks": result.breaks, "total_emissions_tau": sum(result.emissions.values()),
    }
    (OUT / "score_report.json").write_text(json.dumps(score, indent=2))
    (OUT / "anticheat_report.json").write_text(json.dumps(result.anticheat_feed, indent=2))
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
        report="Correct-looking vulnerability explanation. Ignored by scoring.",
    )
    (OUT / "scanner_contract.json").write_text(json.dumps({
        "task": scan_task.manifest(),
        "accepted_submission": scan_good.as_artifact(),
        "accepted_verdict": scanner.verify_submission(scan_task, scan_good).__dict__,
        "report_only_submission": scan_report_only.as_artifact(),
        "report_only_verdict": scanner.verify_submission(scan_task, scan_report_only).__dict__,
        "rule": "reports are metadata; only replayable witnesses score",
    }, indent=2, default=str))
    # export a portable, independently-verifiable proof bundle for the top earner
    from .bundle import build_bundle, verify_bundle
    winners = [a for a in result.agents if a.gates.passed()]
    if winners:
        top = max(winners, key=lambda a: result.emissions.get(a.run.miner_hotkey, 0))
        pb = build_bundle(result, top.run.agent_id)
        (OUT / "proof_bundle.json").write_text(json.dumps(pb, indent=2))
        _bv = verify_bundle(pb)
    with (OUT / "traces.jsonl").open("w") as fh:
        for t in traces:
            fh.write(json.dumps(t, default=str) + "\n")

    # console summary
    print(f"\nCATHEDRAL ARENA — {result.season} round {result.round_no}  [{mode}]  "
          f"({result.corpus_summary['targets']} targets, "
          f"{result.corpus_summary['proof_tasks']} CNFs)")
    print("-" * 72)
    for a in sorted(result.agents, key=lambda a: -result.emissions.get(a.run.miner_hotkey, 0.0)):
        hk = a.run.miner_hotkey
        emit = result.emissions.get(hk, 0.0)
        arch = next(s.archetype for s in eng.roster if s.hotkey == hk)
        tag = "BREACH" if a.gates.passed() else "REJECT"
        why = f"prov {a.gates.provenance_grade} · {result.ranks.get(hk,'')}" if a.gates.passed() else a.gates.first_failure()
        print(f"  {tag:6s} {a.run.agent_id:16s} {arch:12s} {emit:7.0f}τ  {why}")
    print("-" * 72)
    print(f"  wrote {html_path}")
    print(f"  wrote {OUT/'score_report.json'}, {OUT/'anticheat_report.json'}, "
          f"{OUT/'scanner_contract.json'}, {OUT/'traces.jsonl'}")
    if winners:
        print(f"  proof bundle: {OUT/'proof_bundle.json'}  (verify: python -m game.arena.bundle "
              f"{OUT/'proof_bundle.json'}  -> {'OK' if _bv['ok'] else 'INVALID'})")
    print(f"  open: file://{html_path}")


if __name__ == "__main__":
    main()
