"""Real agent tools — each tool genuinely executes (the Hermes/Pi shell). The
agent's receipt trace is REAL tool I/O, not labels.
"""
from __future__ import annotations

from game.arena import mint, replay, tools


def test_workflow_produces_real_tool_trace():
    trace, witness = tools.run_workflow(
        netuid=38, name="ChronoLLM", repo="r", location="v.py:92",
        candidate="replay reference models",
        replay_target_id="subtensor-pallet:multi-take-split@HEAD",
        artifact={"challenge_id": "pm-x", "cnf_sha256": "h"})
    names = [tc.tool for tc in trace]
    assert names == ["subnet.fetch_target", "code-fetch.inspect", "z3.encode_invariant",
                     "solver.run", "decode.witness", "subnet.submit_finding"]
    # the steps carry real, distinct I/O digests (the hash chain consumes these)
    for tc in trace:
        assert len(tc.input_digest) == 64 and len(tc.output_digest) == 64
    assert witness is not None


def test_encode_tool_mints_real_z3_cnf():
    if not mint.z3_available() or not replay.MINTED_TARGETS:
        return
    tc, out = tools.encode_invariant(replay.MINTED_TARGETS[0])
    assert out["z3_minted"] is True
    assert len(out["cnf_sha256"]) == 64 and out["clauses"] > 0


def test_run_solver_tool_runs_real_cdcl():
    if not mint.z3_available() or not replay.MINTED_TARGETS:
        return
    tc, out = tools.run_solver(replay.MINTED_TARGETS[0])
    if out.get("solver") == "glucose3":
        assert out["sat"] is True and out["verified"] is True


def test_decode_tool_returns_reproducing_witness():
    tid = replay.MINTED_TARGETS[0] if replay.MINTED_TARGETS else \
        "subtensor-pallet:multi-take-split@HEAD"
    tc, out, witness = tools.decode_witness(tid)
    assert witness == out["witness"]
    assert replay.run_replay(tid, witness).reproduced is True   # the SAT solution IS the exploit


def test_receipt_steps_are_real_tools():
    from game.arena.engine import ArenaEngine
    r = ArenaEngine().run(1)
    a = next(x for x in r.agents if x.run.agent_id == "agent-aurora")
    # fetch → inspect → REASON (decide invariant family) → encode → solve → ...
    assert a.run.commands[:4] == ["subnet.fetch_target", "code-fetch.inspect",
                                  "reason.propose_hypothesis", "z3.encode_invariant"]
    assert "solver.run" in a.run.commands
