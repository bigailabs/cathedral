"""Scoring self-audit — the deterministic-scoring pillar made self-verifying.

`reward = linear_metric x boolean_gate` is the whole scoring rule (Const's
structure). This module INDEPENDENTLY re-checks that the rule actually held for a
round — over the engine's own outputs, using only verification primitives — so the
arena can prove its scores rather than assert them:

  * reward = metric x gate    — an agent contributes/earns iff ALL its gates pass
  * cheaters zeroed           — every gated-out agent has weight 0 AND emission 0
  * honest earn               — passing agents have positive weight + emission
  * gate consistency          — passed() iff every one of the boolean gates is true
  * signed vector verifies     — the Ed25519 weight vector signature checks out
  * anchor verifies            — the Merkle round commitment re-derives + verifies

Run standalone (re-runs a round and audits it):  python -m game.arena.audit
"""
from __future__ import annotations

import json
import sys

from .models import GateOutcome


def audit_scoring(result) -> dict:
    """Re-verify the scoring invariants for one ArenaResult. Returns
    {ok, checks{...}, violations[...]}. Pure over the result; no re-run."""
    checks: dict[str, bool] = {}
    violations: list[dict] = []

    emissions = result.emissions
    weights = result.weights

    # 1. reward = metric x gate: contribution AND emission are positive IFF the
    #    full boolean gate set passed — never one without the other.
    rule_ok = True
    for a in result.agents:
        hk = a.run.miner_hotkey
        passed = a.gates.passed()
        contrib_pos = a.credit.contrib > 0
        emit_pos = emissions.get(hk, 0.0) > 0
        if not (contrib_pos == passed and emit_pos == passed):
            rule_ok = False
            violations.append({"agent": a.run.agent_id, "check": "reward_is_metric_times_gate",
                               "passed": passed, "contrib_pos": contrib_pos, "emit_pos": emit_pos})
    checks["reward_is_metric_times_gate"] = rule_ok

    # 2. cheaters zeroed: a gated-out agent earns nothing and carries no weight.
    cz = True
    for a in result.agents:
        if not a.gates.passed():
            hk = a.run.miner_hotkey
            if emissions.get(hk, 0.0) != 0.0 or weights.get(hk, 0.0) != 0.0:
                cz = False
                violations.append({"agent": a.run.agent_id, "check": "cheaters_zeroed",
                                   "emit": emissions.get(hk), "weight": weights.get(hk)})
    checks["cheaters_zeroed"] = cz

    # 3. honest earn: at least one passing agent, and every passing agent earns.
    passers = [a for a in result.agents if a.gates.passed()]
    he = bool(passers) and all(
        weights.get(a.run.miner_hotkey, 0.0) > 0 and emissions.get(a.run.miner_hotkey, 0.0) > 0
        for a in passers)
    if not he:
        violations.append({"check": "honest_earn", "passers": len(passers)})
    checks["honest_earn"] = he

    # 4. gate consistency: passed() is exactly "every boolean gate is True".
    gc = True
    for a in result.agents:
        explicit = all(getattr(a.gates, g) for g in GateOutcome.GATES)
        if explicit != a.gates.passed():
            gc = False
            violations.append({"agent": a.run.agent_id, "check": "gate_consistency"})
    checks["gate_consistency"] = gc

    # 5. the signed weight vector verifies (Ed25519 over the canonical payload).
    from ..reward import verify_vector
    checks["signed_vector_verifies"] = bool(verify_vector(result.signed_vector))

    # 6. the Merkle round anchor re-derives + verifies.
    from .anchor import verify_anchor
    try:
        checks["anchor_verifies"] = bool(
            verify_anchor(result.anchor, result.agents, result.signed_vector))
    except Exception:
        checks["anchor_verifies"] = False

    ok = all(checks.values())
    return {"ok": ok, "checks": checks, "violations": violations,
            "round": result.round_no, "season": result.season,
            "agents": len(result.agents), "passers": len(passers)}


def main() -> int:
    from .engine import ArenaEngine
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    bad = 0
    for r in range(1, rounds + 1):
        verdict = audit_scoring(ArenaEngine().run(r))
        status = "PASS ✓" if verdict["ok"] else "FAIL ✗"
        print(f"round {r}: scoring audit {status}  "
              f"({verdict['passers']}/{verdict['agents']} earn) "
              f"{json.dumps(verdict['checks'])}")
        if not verdict["ok"]:
            bad += 1
            print("  violations:", json.dumps(verdict["violations"]))
    print(f"\nSCORING AUDIT {'CLEAN ✓' if bad == 0 else f'{bad} ROUND(S) FAILED ✗'} "
          f"— reward = linear_metric x boolean_gate, verified independently")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
