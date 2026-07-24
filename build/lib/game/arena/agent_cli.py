"""A REAL external miner agent — a standalone process the arena spawns and then
VERIFIES. It is the local stand-in for a Pi/Hermes agent on a miner's host: it
receives a mission packet on stdin, runs the workflow, solves the encoded task,
decodes the money-math witness, assembles + **signs its own run-receipt with its
own key**, and emits a submission envelope on stdout. The arena never builds this
receipt — it only checks it (verify-by-receipt).

  echo '<packet json>' | python -m game.arena.agent_cli

Behaviors (the agent decides what to submit; the arena catches misbehavior):
  honest | copy_witness | wrong_owner | bad_encode | forge_trace | bad_replay |
  impostor (sign with a non-delegated key) | no_decode_map |
  misclassify (commit the wrong invariant family for the proof)
"""
from __future__ import annotations

import hashlib
import json
import sys
import time

from .provenance import AgentKey, build_receipt, receipt_to_dict
from .replay import TARGETS as REPLAY_TARGETS
from scaffold.dimacs import solve_cnf, parse_cnf, verify_witness


def run_agent(packet: dict) -> dict:
    beh = packet.get("behavior", "honest")
    cnf = packet["cnf"]
    n_vars, _ = parse_cnf(cnf)

    t0 = time.perf_counter()
    own_solution = solve_cnf(cnf) or []
    wall = (time.perf_counter() - t0) * 1000.0 + packet.get("env_effort_ms", 50.0)

    submitted_cid = packet["cid"]
    submitted_nonce = packet["nonce"]
    submitted_cnf_hash = packet["cnf_sha256"]
    assignment = own_solution

    if beh == "copy_witness":
        assignment = packet.get("victim", {}).get("assignment", [])
    elif beh == "wrong_owner":
        submitted_cid = packet.get("victim", {}).get("cid", submitted_cid)
        assignment = packet.get("victim", {}).get("assignment", [])
    elif beh == "bad_encode":
        submitted_cnf_hash = hashlib.sha256((cnf + "tampered").encode()).hexdigest()
    elif beh == "cheat":
        bad = [abs(v) for v in range(1, n_vars + 1)]
        if verify_witness(cnf, bad):
            bad[0] = -bad[0]
        assignment = bad

    # decode the money-math witness for the real replay
    rtgt = REPLAY_TARGETS.get(packet.get("replay_target_id", ""))
    if beh == "no_decode_map":
        replay_witness = {}                       # omit the decode map entirely
    elif rtgt is None:
        replay_witness = None
    elif beh == "bad_replay":
        replay_witness = {k: 0 for k in rtgt.decode}
    else:
        replay_witness = dict(rtgt.known_witness)

    # the agent COMMITS the invariant family of the proof it submits (the replay
    # target's true family). A misclassifier commits a WRONG family -> the arena's
    # hypothesis_aligned gate rejects it (parity with the in-process path).
    proof_family = rtgt.family if rtgt is not None else ""
    if beh == "misclassify" and proof_family:
        from .hypothesis import RULEBOOK
        proof_family = next((f for f in sorted(RULEBOOK) if f != proof_family), proof_family)

    artifact = {"challenge_id": submitted_cid, "cnf_sha256": submitted_cnf_hash,
                "nonce": submitted_nonce, "assignment_len": len(assignment),
                "proof_family": proof_family}

    attested = ({"required": True, "valid": True, "env": packet["environment"]}
                if packet.get("attestation_required") else None)

    # the agent's on-host identity. impostor signs with a NON-delegated key.
    seed = (packet["hotkey"] + ("|impostor" if beh == "impostor" else "")).encode()
    key = AgentKey(seed=seed)

    # the agent INVOKES real tools (fetch → inspect → REASON → encode/z3 →
    # solve/Glucose → decode → submit); raw_steps = the REAL tool I/O trace. The
    # arena computes the hypothesis (it owns the gated LLM policy) and the agent
    # RECORDS the reasoning step — parity with the in-process path.
    from .tools import run_workflow
    trace, _w = run_workflow(
        netuid=packet["target_netuid"], name=packet.get("target_name", ""),
        repo=packet.get("target_repo", ""), location=packet.get("target_location", ""),
        candidate=packet.get("target_title", ""),
        replay_target_id=packet.get("replay_target_id", ""), artifact=artifact,
        hypothesis=packet.get("hypothesis"))
    raw_steps = [tc.as_step() for tc in trace]
    receipt = build_receipt(
        key, agent_id=packet["agent_id"], miner_hotkey=packet["hotkey"],
        mission_id=packet["mission_id"], nonce=submitted_nonce,
        environment=packet["environment"], raw_steps=raw_steps,
        artifact=artifact, attestation=attested)

    if beh == "forge_trace":
        receipt.steps[2].output_digest = "forged_" + receipt.steps[2].output_digest[:12]

    return {"receipt": receipt_to_dict(receipt),
            "submission": {"submitted_cid": submitted_cid, "submitted_nonce": submitted_nonce,
                           "submitted_cnf_hash": submitted_cnf_hash, "assignment": assignment,
                           "replay_witness": replay_witness, "wall_ms": round(wall, 1)}}


def main() -> int:
    packet = json.loads(sys.stdin.read())
    sys.stdout.write(json.dumps(run_agent(packet)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
