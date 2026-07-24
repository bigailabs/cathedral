"""Portable proof bundle — the capstone of "rewards proof, not claims".

A bundle is a SELF-CONTAINED artifact for one winning agent that a third party
can verify END-TO-END with no access to the engine or the other agents' data:

  1. the agent's Ed25519-signed run-receipt (verify-by-receipt),
  2. the REAL invariant replay (run the pinned harness on the witness -> reproduces),
  3. a Merkle INCLUSION proof that this receipt was in the round commitment,
  4. the Ed25519-signed round anchor (the commitment),
  5. the agent's signed weight-vector entry,
  6. the proven invariant's PROVENANCE (z3-minted / audit_lane / arena-port,
     content-addressed) — tied to the verifier's own replay registry, and
  7. corroborating round evidence: the real Stitch cross-solver proof + its
     solve commitment (offline-recomputable, tamper-evident).

`verify_bundle` re-derives every check from scratch using only the verification
primitives (provenance, replay, anchor) — never the engine. Run standalone:

    python -m game.arena.bundle out/proof_bundle.json
"""
from __future__ import annotations

import json
import sys

from . import anchor as _anchor
from . import replay as _replay
from .provenance import Receipt, receipt_from_dict, receipt_to_dict, verify_receipt


def build_bundle(result, agent_id: str) -> dict:
    """Assemble a portable proof bundle for one agent in a round."""
    ar = next(a for a in result.agents if a.run.agent_id == agent_id)
    receipt: Receipt = ar.receipt
    head = receipt.head

    # Merkle inclusion of this receipt head in the round anchor.
    leaves = _anchor.round_leaves(result.agents, result.signed_vector)
    idx = leaves.index(head)
    inclusion = {"leaf": head, "path": _anchor.merkle_proof(leaves, idx),
                 "root": result.anchor["merkle_root"]}

    rt = next((t for t in result.replay_theater if t["agent"] == agent_id), None)
    vec = next((w for w in result.signed_vector["weights"]
                if w["miner_hotkey"] == ar.run.miner_hotkey), None)

    # WHAT the proven invariant is: z3-minted, the real audit_lane pinned code, or
    # an arena port — content-addressed, so a verifier sees the proof's provenance.
    rtarget = _replay.TARGETS.get(rt["target_id"]) if rt else None
    proof_provenance = ({"target_id": rt["target_id"], "source": rtarget.source,
                         "code_sha256": rtarget.code_sha256, "family": rtarget.family,
                         "property": rtarget.property_desc} if rtarget else None)

    # corroborating round evidence: the REAL pre-existing audit CNF cross-proof
    # (kissat on Stitch vs a local CDCL solver) + its solve commitment. Carried so
    # the bundle's commitment is offline-recomputable (internal-consistency proof).
    sr = (result.operator_console.get("stitch_runner") or {}) if hasattr(result, "operator_console") else {}
    corroborating = None
    if sr.get("available") and sr.get("real_cnf"):
        from .stitch import solve_commitment
        ev = {k: sr.get(k) for k in
              ("real_cnf", "cnf_sha256", "host", "solver", "remote_status",
               "remote_wall_ms", "local_solver", "cross_solver_agree", "hardened_proof")}
        ev.update({"kind": "stitch-real-cnf-cross-proof",
                   "solver_race": sr.get("solver_race"),
                   "solve_commitment": solve_commitment(sr)})
        corroborating = ev

    return {
        "schema": "cathedral-arena-proof-bundle/v2",
        "agent_id": agent_id, "miner_hotkey": ar.run.miner_hotkey,
        "season": result.season, "round": result.round_no,
        "environment": ar.run.environment, "target_netuid": ar.run.target_netuid,
        "mission_id": ar.mission.mission_id, "nonce": ar.mission.nonce,
        "receipt": receipt_to_dict(receipt),
        "expected_artifact": dict(receipt.artifact),
        "replay": ({"target_id": rt["target_id"], "witness": rt["witness"],
                    "expect_reproduced": rt["reproduced"]} if rt else None),
        "proof_provenance": proof_provenance,
        "corroborating_evidence": corroborating,
        "merkle_inclusion": inclusion,
        "anchor": result.anchor,
        "weight_entry": vec,
        "gates_passed": ar.gates.passed(),
        "provenance_grade": ar.gates.provenance_grade,
    }


def verify_bundle(bundle: dict) -> dict:
    """Independently verify a bundle end-to-end. Returns {ok, checks{...}}.
    Uses ONLY verification primitives — no engine, no other agents' data."""
    checks: dict[str, bool] = {}

    # 1. the agent really signed this receipt, binding the artifact + mission.
    receipt = receipt_from_dict(bundle["receipt"])
    v = verify_receipt(
        receipt, expected_hotkey=bundle["miner_hotkey"],
        expected_mission=bundle["mission_id"], expected_nonce=bundle["nonce"],
        expected_artifact=bundle["expected_artifact"],
        replay_ok=True, attestation_required=False, seen_receipts=set())
    checks["receipt_signature"] = v.signature_ok
    checks["receipt_chain"] = v.chain_intact
    checks["receipt_binds_artifact"] = v.head_binds_artifact

    # 2. the REAL invariant reproduces on the submitted witness.
    rp = bundle.get("replay")
    if rp:
        out = _replay.run_replay(rp["target_id"], rp["witness"])
        checks["replay_reproduces"] = (out.reproduced == rp["expect_reproduced"])
    else:
        checks["replay_reproduces"] = True

    # 3. the receipt was included in the round's Merkle commitment.
    inc = bundle["merkle_inclusion"]
    checks["merkle_inclusion"] = _anchor.verify_merkle_proof(
        inc["leaf"], inc["path"], inc["root"])
    checks["inclusion_matches_anchor"] = (inc["root"] == bundle["anchor"]["merkle_root"])

    # 4. the round anchor signature is valid over the committed root.
    a = bundle["anchor"]
    canon = json.dumps({k: val for k, val in a.items() if k != "signature"},
                       sort_keys=True, separators=(",", ":")).encode()
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(a["pubkey"])).verify(
            bytes.fromhex(a["signature"]), canon)
        checks["anchor_signature"] = True
    except Exception:
        checks["anchor_signature"] = False

    # 5. the receipt head matches the inclusion leaf (the bundle is internally tied).
    checks["receipt_head_is_leaf"] = (receipt.head == inc["leaf"])

    # 6. the proven invariant's provenance is the REAL registered target: its
    #    source (z3-factory-mint | audit_lane | arena-port) AND content hash match
    #    the verifier's own replay registry — the bundle can't claim a fake source.
    pp = bundle.get("proof_provenance")
    if pp:
        t = _replay.TARGETS.get(pp["target_id"])
        checks["proof_source_registered"] = bool(
            t and t.source == pp.get("source") and t.code_sha256 == pp.get("code_sha256"))
    else:
        checks["proof_source_registered"] = True       # v1 bundle / no replay target

    # 7. corroborating Stitch cross-proof evidence is internally consistent: its
    #    solve commitment recomputes from the carried fields (tamper-evident).
    ce = bundle.get("corroborating_evidence")
    if ce and ce.get("kind") == "stitch-real-cnf-cross-proof":
        from .stitch import solve_commitment
        checks["corroborating_commitment"] = (
            solve_commitment(ce) == ce.get("solve_commitment"))
    else:
        checks["corroborating_commitment"] = True

    ok = all(checks.values())
    return {"ok": ok, "checks": checks, "agent_id": bundle.get("agent_id")}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m game.arena.bundle <bundle.json>")
        return 2
    bundle = json.loads(open(sys.argv[1], encoding="utf-8").read())
    verdict = verify_bundle(bundle)
    print(json.dumps(verdict, indent=2))
    print(("PROOF BUNDLE VERIFIED ✓" if verdict["ok"] else "PROOF BUNDLE INVALID ✗")
          + f"  (agent {verdict['agent_id']})")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
