"""Offline verifier for a complete Cathedral Arena output directory.

Run:

    python -m game.arena.verify [out_dir]

The verifier loads only the JSON artifacts an external auditor would receive and
re-checks the required proof chain without running the arena engine. Optional
artifacts are reported as absent when missing; they do not fail the round.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _load(out: Path, name: str) -> Any:
    path = out / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "UNPARSEABLE"


def verify_season(state: dict | str | Path) -> dict[str, Any]:
    """Verify a persisted SEASON ledger's cumulative cross-round integrity (no engine).
    A season accrues per-agent totals over rounds; this checks they are internally
    impossible to forge cheaply: no negative/impossible counters, earnings imply
    breaches (you only earn by breaching), and streaks are bounded by rounds played.
    `state` is the season_state.json dict (or a path to it). Returns {ok, rounds,
    n_agents, violations[]}."""
    if not isinstance(state, dict):
        p = Path(state)
        loaded = _load(p.parent, p.name)
        state = loaded if isinstance(loaded, dict) else {}
    rounds = state.get("rounds", 0)
    agents = state.get("agents", {}) or {}
    targets = state.get("targets", {}) or {}
    v: list[str] = []
    for hk, s in agents.items():
        te = s.get("total_emissions", 0); br = s.get("breaches", 0)
        rp = s.get("rounds_played", 0); rv = s.get("rounds_verified", 0)
        st = s.get("streak", 0); bs = s.get("best_streak", 0)
        if te < 0: v.append(f"{hk}: negative emissions {te}")
        if min(br, rp, rv, st, bs) < 0: v.append(f"{hk}: negative counter")
        if rp > rounds: v.append(f"{hk}: rounds_played {rp} > season rounds {rounds}")
        if rv > rp: v.append(f"{hk}: rounds_verified {rv} > rounds_played {rp}")
        if br > rp: v.append(f"{hk}: breaches {br} > rounds_played {rp}")
        if st > bs: v.append(f"{hk}: streak {st} > best_streak {bs}")
        if bs > rp: v.append(f"{hk}: best_streak {bs} > rounds_played {rp}")
        if te > 0 and br == 0: v.append(f"{hk}: earned {te} with 0 breaches")
    for n, t in targets.items():
        if t.get("breaches", 0) < 0: v.append(f"target {n}: negative breaches")
        fbr = t.get("first_broken_round")
        if fbr is not None and not (1 <= fbr <= rounds):
            v.append(f"target {n}: first_broken_round {fbr} out of [1,{rounds}]")
    return {"ok": not v, "rounds": rounds, "n_agents": len(agents), "violations": v}


def verify_round(out_dir: str | Path) -> dict[str, Any]:
    """Verify a generated round output directory.

    Returns:
        {
            "ok": bool,
            "dir": str,
            "checks": [{name, status, ok, optional, detail}],
            "required_failed": [name, ...],
        }
    """

    out = Path(out_dir)
    checks: list[dict[str, Any]] = []

    def add(
        name: str,
        ok: bool,
        detail: str,
        *,
        optional: bool = False,
        absent: bool = False,
    ) -> None:
        status = "absent" if absent else ("pass" if ok else "fail")
        checks.append({
            "name": name,
            "status": status,
            "ok": bool(ok),
            "optional": optional,
            "detail": detail,
        })

    # 1. Portable proof bundle: signatures, chain, replay, Merkle inclusion.
    proof_bundle = _load(out, "proof_bundle.json")
    if proof_bundle in (None, "UNPARSEABLE"):
        add("proof_bundle", False, "missing or unparseable proof_bundle.json")
    else:
        from .bundle import verify_bundle

        bundle_verdict = verify_bundle(proof_bundle)
        bundle_checks = bundle_verdict.get("checks", {})
        passed = sum(1 for value in bundle_checks.values() if value)
        add(
            "proof_bundle",
            bool(bundle_verdict.get("ok")),
            f"agent {proof_bundle.get('agent_id', '?')}; checks {passed}/{len(bundle_checks)}",
        )

    # 1b. EVERY winner's bundle verifies (optional — present when proof_bundles.json
    # was exported). Each breaching miner ships an independently re-checkable proof.
    all_bundles = _load(out, "proof_bundles.json")
    if all_bundles in (None, "UNPARSEABLE"):
        add("all_winner_bundles", True, "no per-winner bundles on file", optional=True, absent=True)
    else:
        from .bundle import verify_bundle
        bundles = all_bundles.get("bundles", [])
        verdicts = [verify_bundle(b) for b in bundles]
        ok_n = sum(1 for v in verdicts if v.get("ok"))
        bad = [bundles[i].get("agent_id", "?") for i, v in enumerate(verdicts) if not v.get("ok")]
        add("all_winner_bundles", bool(bundles) and ok_n == len(bundles),
            f"{ok_n}/{len(bundles)} winner bundles verify"
            + ("" if not bad else f"; FAILED: {bad[:3]}"), optional=True)

    score_report = _load(out, "score_report.json")
    if score_report in (None, "UNPARSEABLE"):
        add("signed_vector", False, "missing or unparseable score_report.json")
        add("scoring_audit", False, "missing or unparseable score_report.json")
        add("anchor_consistency", False, "missing or unparseable score_report.json")
    else:
        # 2. Signed weight vector: independent Ed25519 verification.
        from game.reward import verify_vector

        signed_vector = score_report.get("signed_vector") or {}
        try:
            signature_ok = bool(signed_vector) and verify_vector(signed_vector)
        except Exception as exc:  # pragma: no cover - detail is still returned
            signature_ok = False
            signed_vector = {"_err": type(exc).__name__}
        add(
            "signed_vector",
            signature_ok,
            f"netuid {signed_vector.get('netuid', '?')}, "
            f"{len(signed_vector.get('weights', {}))} weights, ed25519",
        )

        # 2b. Weights consistency: the per-agent weights the report DISPLAYS must equal
        # the SIGNED (authoritative) weights — else a forger could alter the shown
        # weights without breaking the signature. (Report rounds to 4 dp.)
        signed_w = {w.get("miner_hotkey"): w.get("weight", 0.0)
                    for w in (signed_vector.get("weights") or []) if isinstance(w, dict)}
        mismatches = []
        for a in score_report.get("agents", []):
            rw = a.get("weight", 0.0)
            sw = signed_w.get(a.get("hotkey"), 0.0)
            if abs(float(rw) - float(sw)) > 1e-3:
                mismatches.append(f"{a.get('hotkey')}({rw}!={round(sw,4)})")
        add("weights_consistent", not mismatches,
            "displayed weights == signed weights" if not mismatches
            else f"MISMATCH: {', '.join(mismatches[:3])}")

        # 3. Scoring self-audit: reward = linear metric x boolean gate.
        audit = (score_report.get("verification") or {}).get("scoring_audit") or {}
        audit_checks = audit.get("checks", {})
        add(
            "scoring_audit",
            bool(audit.get("ok")),
            f"{sum(1 for value in audit_checks.values() if value)}/{len(audit_checks)} invariants",
        )

        # 4. Anchor consistency: report commitment matches round_anchor.json.
        anchor = _load(out, "round_anchor.json")
        committed_root = (score_report.get("verification") or {}).get("anchor_merkle_root")
        if anchor in (None, "UNPARSEABLE"):
            add("anchor_consistency", False, "missing or unparseable round_anchor.json")
        else:
            root = anchor.get("merkle_root")
            add(
                "anchor_consistency",
                bool(root) and root == committed_root,
                f"merkle_root {'matches' if root == committed_root else 'MISMATCH'} "
                f"({str(root)[:16]}...)",
            )

    # 5. Optional off-box receipt: rigorous external CNF satisfaction proof.
    offbox = _load(out, "offbox_stitch_receipt.json")
    if offbox in (None, "UNPARSEABLE"):
        add("offbox_receipt", True, "no off-box receipt on file", optional=True, absent=True)
    else:
        ok = bool(offbox.get("available") and offbox.get("cnf_satisfied") and offbox.get("ok"))
        add(
            "offbox_receipt",
            ok,
            f"{offbox.get('solver', '?')}@{offbox.get('host', '?')} "
            f"cnf_satisfied={offbox.get('cnf_satisfied')} rule={offbox.get('rule_id', 'B2')}",
            optional=True,
        )

    # 5a2. Optional off-box receipt for a SECOND rule (I1) — proves off-box is not
    # hardcoded to B2 but generalizes across minted SAT rules (captured live).
    offbox_i1 = _load(out, "offbox_i1_receipt.json")
    if offbox_i1 in (None, "UNPARSEABLE"):
        add("offbox_i1", True, "no I1 off-box receipt on file", optional=True, absent=True)
    else:
        ok = bool(offbox_i1.get("available") and offbox_i1.get("cnf_satisfied")
                  and offbox_i1.get("ok") and offbox_i1.get("rule_id") == "I1-div-by-zero")
        add("offbox_i1", ok,
            f"{offbox_i1.get('solver', '?')}@{offbox_i1.get('host', '?')} "
            f"cnf_satisfied={offbox_i1.get('cnf_satisfied')} rule={offbox_i1.get('rule_id', '?')} "
            f"({offbox_i1.get('n_lits', '?')} lits)", optional=True)

    # 5b. Optional off-box HARDENED receipt: cross-confirmed UNSAT (defensive proof).
    hardened = _load(out, "offbox_hardened_receipt.json")
    if hardened in (None, "UNPARSEABLE"):
        add("offbox_hardened", True, "no off-box hardened receipt on file", optional=True, absent=True)
    else:
        ok = bool(hardened.get("available") and hardened.get("remote_unsat")
                  and hardened.get("local_unsat") and hardened.get("cross_confirmed"))
        add("offbox_hardened", ok,
            f"{hardened.get('solver', '?')}@{hardened.get('host', '?')} "
            f"{hardened.get('rule_id', '?')} cross_confirmed={hardened.get('cross_confirmed')}",
            optional=True)

    # 6. Optional dataset card: trace class histogram is internally consistent.
    dataset_card = _load(out, "traces_dataset.json")
    if dataset_card in (None, "UNPARSEABLE"):
        add("dataset_card", True, "no dataset card on file", optional=True, absent=True)
    else:
        label_dist = (dataset_card.get("class_distribution") or {}).get("label") or {}
        ok = (
            sum(label_dist.values()) == dataset_card.get("n_rows")
            and set(label_dist) == {"honest", "cheat"}
        )
        add("dataset_card", ok, f"n_rows={dataset_card.get('n_rows')}, labels={label_dist}", optional=True)

    # 7. Optional season ledger: cumulative cross-round integrity.
    season = _load(out, "season_state.json")
    if season in (None, "UNPARSEABLE"):
        add("season_ledger", True, "no season ledger on file", optional=True, absent=True)
    else:
        sv = verify_season(season)
        detail = f"{sv['rounds']} rounds, {sv['n_agents']} agents"
        if not sv["ok"]:
            detail += f"; {len(sv['violations'])} violations: {sv['violations'][:2]}"
        add("season_ledger", sv["ok"], detail, optional=True)

    required_failed = [check["name"] for check in checks if not check["optional"] and not check["ok"]]
    return {
        "ok": not required_failed,
        "dir": str(out),
        "checks": checks,
        "required_failed": required_failed,
    }


def main() -> int:
    default_out = Path(__file__).resolve().parent / "out"
    args = sys.argv[1:]
    as_json = "--json" in args
    positional = [a for a in args if not a.startswith("--")]
    out_dir = Path(positional[0]) if positional else default_out
    result = verify_round(out_dir)

    if as_json:
        # machine-readable verdict for CI / automation: `... --json | jq`
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["ok"] else 1

    print(f"CATHEDRAL ROUND VERIFY - {result['dir']} (offline, no engine)")
    print("-" * 72)
    marker = {"pass": "OK", "fail": "FAIL", "absent": "SKIP"}
    for check in result["checks"]:
        tag = "(optional)" if check["optional"] else ""
        print(
            f"  {marker[check['status']]:4s} {check['name']:20s} "
            f"{check['status']:7s} {tag:10s} {check['detail']}"
        )
    print("-" * 72)
    if result["ok"]:
        print("ROUND VERIFIED - every required proof re-checks independently from the artifacts.")
        return 0
    print(f"ROUND INVALID - failed: {', '.join(result['required_failed'])}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
