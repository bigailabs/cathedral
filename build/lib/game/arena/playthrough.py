"""Machine-checkable Subnet Breaker playthrough.

This drives the same scanner flow the browser game uses:
report-only decoy -> forged witness -> valid proof -> replay -> attest -> seal -> duplicate.
It proves the local game loop is interactive and scoreful without adding a
browser-test dependency.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .serve import ArenaServer

SCHEMA_PLAYTHROUGH = "cathedral.arena.playthrough.v1"


def run_playthrough(*, ledger_path: str | None = None,
                    miner_hotkey: str = "hk_playthrough",
                    index: int = 0) -> dict:
    temp = None
    if ledger_path is None:
        temp = tempfile.TemporaryDirectory()
        ledger_path = str(Path(temp.name) / "scanner.jsonl")
    try:
        srv = ArenaServer(scanner_ledger_path=ledger_path)
        task = srv.scanner_task(index)

        report_only = srv.scanner_agent_solve(index, miner_hotkey, mode="report_only")
        report_replay = srv.scanner_replay(report_only["submission"])

        forged = srv.scanner_agent_solve(index, miner_hotkey, mode="bad_witness")
        forged_replay = srv.scanner_replay(forged["submission"])

        solved = srv.scanner_agent_solve(index, miner_hotkey, mode="valid")
        replayed = srv.scanner_replay(solved["submission"])
        unsealed = srv.scanner_attested_submit(solved["submission"])
        attested = srv.scanner_attest(solved["submission"])
        solved["submission"]["claim"] = {
            **(solved["submission"].get("claim") or {}),
            "attestation": attested.get("receipt"),
        }
        solved["submission"]["report"] = (
            "Human-readable seal text changed after attestation; proof binding "
            "must ignore mutable prose."
        )
        sealed = srv.scanner_attested_submit(solved["submission"])
        duplicate = srv.scanner_attested_submit(solved["submission"])

        state = srv.scanner_state(miner_hotkey)
        leaderboard = srv.scanner_leaderboard()
        benchmark = srv.scanner_benchmark()
        top = leaderboard.get("miners", [{}])[0] if leaderboard.get("miners") else {}
        bench_top = benchmark.get("miners", [{}])[0] if benchmark.get("miners") else {}

        checks = {
            "report_only_rejected": (
                report_replay.get("accepted") is False
                and report_replay.get("gates", {}).get("decode_map_present") is False
                and report_replay.get("scored") is False
            ),
            "forged_witness_rejected": (
                forged_replay.get("accepted") is False
                and forged_replay.get("gates", {}).get("replay_succeeds") is False
                and forged_replay.get("scored") is False
            ),
            "dry_replay_accepts_without_scoring": (
                replayed.get("accepted") is True
                and replayed.get("ledger_written") is False
                and replayed.get("scored") is False
            ),
            "attested_submit_requires_receipt": (
                unsealed.get("accepted") is False
                and unsealed.get("gates", {}).get("attestation_receipt") is False
                and "missing_attestation_receipt" in unsealed.get("reasons", [])
            ),
            "local_attestation_bound": (
                attested.get("accepted") is True
                and attested.get("attested") is True
                and attested.get("receipt", {}).get("production_tee") is False
                and solved["submission"]["claim"].get("attestation", {}).get("task_id") == task["task_id"]
            ),
            "seal_scores_once": (
                sealed.get("accepted") is True
                and float(sealed.get("score") or 0) > 0
            ),
            "attestation_ignores_mutable_report": (
                sealed.get("accepted") is True
                and sealed.get("gates", {}).get("attestation_receipt") is True
                and "attestation_artifact_sha256_mismatch" not in sealed.get("reasons", [])
            ),
            "duplicate_is_zeroed": (
                duplicate.get("accepted") is False
                and float(duplicate.get("score") or 0) == 0.0
                and duplicate.get("gates", {}).get("not_duplicate_credit") is False
            ),
            "miner_state_updated": (
                state.get("accepted") == 1
                and state.get("rejected", 0) >= 1
                and float(state.get("score") or 0) == float(sealed.get("score") or -1)
            ),
            "leaderboard_updated": (
                top.get("miner_hotkey") == miner_hotkey
                and top.get("accepted") == 1
                and float(top.get("score") or 0) == float(sealed.get("score") or -1)
            ),
            "benchmark_kill_rate_positive": (
                bench_top.get("miner_hotkey") == miner_hotkey
                and float(bench_top.get("kill_rate") or 0) > 0
            ),
        }

        return {
            "schema": SCHEMA_PLAYTHROUGH,
            "ok": all(checks.values()),
            "checks": checks,
            "miner_hotkey": miner_hotkey,
            "task_id": task["task_id"],
            "target": task["target"],
            "steps": {
                "report_only_replay": {
                    "accepted": report_replay.get("accepted"),
                    "gates": report_replay.get("gates"),
                    "score": report_replay.get("score"),
                },
                "forged_replay": {
                    "accepted": forged_replay.get("accepted"),
                    "gates": forged_replay.get("gates"),
                    "score": forged_replay.get("score"),
                },
                "dry_replay": {
                    "accepted": replayed.get("accepted"),
                    "ledger_written": replayed.get("ledger_written"),
                    "scored": replayed.get("scored"),
                },
                "submit_without_attestation": {
                    "accepted": unsealed.get("accepted"),
                    "score": unsealed.get("score"),
                    "gates": unsealed.get("gates"),
                    "reasons": unsealed.get("reasons"),
                },
                "attest": {
                    "accepted": attested.get("accepted"),
                    "attested": attested.get("attested"),
                    "receipt": attested.get("receipt"),
                },
                "seal": {
                    "accepted": sealed.get("accepted"),
                    "score": sealed.get("score"),
                    "artifact_sha256": sealed.get("artifact_sha256"),
                },
                "duplicate": {
                    "accepted": duplicate.get("accepted"),
                    "score": duplicate.get("score"),
                    "gates": duplicate.get("gates"),
                },
            },
            "state": state,
            "leaderboard_top": top,
            "benchmark_top": bench_top,
        }
    finally:
        if temp is not None:
            temp.cleanup()


def main() -> int:
    out = Path(__file__).resolve().parent / "out" / "scanner_playthrough.json"
    out.parent.mkdir(exist_ok=True)
    ledger = out.parent / "scanner_playthrough_ledger.jsonl"
    ledger.unlink(missing_ok=True)
    report = run_playthrough(ledger_path=str(ledger))
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"playthrough {'OK' if report['ok'] else 'FAILED'}: {out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
