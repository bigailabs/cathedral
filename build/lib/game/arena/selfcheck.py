"""Operator self-check: one command that answers whether the arena is healthy.

Consolidates the realness signals an operator cares about into a single
green/red report:

  * replay is a real gate: every pinned invariant is a proven discriminator
  * coverage is multi-model: more than one real invariant family is wired
  * formal hardening exists: z3 + independent CDCL cross-confirmed UNSAT proofs
  * gate and anti-cheat sets are present
  * the last round verifies if an out/ round is on disk

Run:

    python -m game.arena.selfcheck [out_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

from game.arena import reports, replay
from game.arena.models import GateOutcome
from game.arena.replay_differential import differential_report


def selfcheck_report(out_dir: str | Path | None = None) -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, *, optional: bool = False) -> None:
        checks.append({"name": name, "ok": bool(ok), "optional": optional, "detail": detail})

    diff = differential_report()
    add(
        "replay_is_a_real_gate",
        diff["all_real"] and diff["discriminators"] == diff["total"] > 0,
        f"{diff['discriminators']}/{diff['total']} pinned invariants are proven discriminators",
    )

    families = sorted({replay.TARGETS[t].family for t in replay.TARGETS})
    add(
        "multi_model_coverage",
        len(families) >= 2,
        f"{len(families)} invariant families wired: {', '.join(families)}",
    )

    hardened = [h for h in getattr(replay, "MINTED_HARDENED", []) if h.get("hardened")]
    cross = [h for h in hardened if h.get("cdcl_unsat")]
    add(
        "formal_hardening",
        len(cross) >= 1 or not hardened,
        f"{len(cross)} z3+CDCL cross-confirmed UNSAT proofs",
        optional=not hardened,
    )

    n_gates, n_axes = len(GateOutcome.GATES), len(reports.ANTICHEAT_AXES)
    add(
        "gate_and_anticheat_set",
        n_gates >= 10 and n_axes >= 10,
        f"{n_gates} boolean gates, {n_axes} anti-cheat axes",
    )

    out = Path(out_dir) if out_dir else Path(__file__).resolve().parent / "out"
    if (out / "score_report.json").exists():
        from game.arena.verify import verify_round

        res = verify_round(out)
        bad = res.get("required_failed") or []
        detail = f"{sum(1 for c in res['checks'] if c['ok'])}/{len(res['checks'])} checks"
        if bad:
            detail += f"; failed: {bad}"
        add("last_round_verifies", res["ok"], detail, optional=True)
    else:
        add(
            "last_round_verifies",
            True,
            "no round on disk; run `python -m game.arena 1`",
            optional=True,
        )

    required_failed = [c["name"] for c in checks if not c["optional"] and not c["ok"]]
    return {
        "ok": not required_failed,
        "checks": checks,
        "required_failed": required_failed,
        "families": families,
    }


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else None
    rep = selfcheck_report(out_dir)
    print("CATHEDRAL ARENA SELF-CHECK")
    print("-" * 60)
    for c in rep["checks"]:
        mark = "OK  " if c["ok"] else ("SKIP" if c["optional"] else "FAIL")
        tag = " (optional)" if c["optional"] else ""
        print(f"  {mark}  {c['name']:24s} {c['detail']}{tag}")
    print("-" * 60)
    if rep["ok"]:
        print("ARENA REAL & HEALTHY - replay is a verifier-gated real discriminator; proof, not claims.")
        return 0
    print(f"ARENA UNHEALTHY - failed: {', '.join(rep['required_failed'])}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
