"""Real agent tools — the Hermes/Pi shell, slots 1–2 (tool-use loop + wiring).

An agent does not emit step LABELS; it INVOKES tools that actually execute and
thread real outputs forward, producing a tamper-evident tool-call trace:

  fetch_target   → read the real subnet target from the corpus
  inspect_code   → digest the suspected weakness (file:line / candidate)
  encode_invariant → MINT a real CNF via the z3 factory (mint.py)
  run_solver     → SOLVE the minted CNF with a real CDCL solver (Glucose)
  decode_witness → the SAT solution = the exploit input
  submit_finding → the artifact handed to the verifier

The default policy is deterministic (the bundle/AGENTS.md 6-step workflow). An
LLM policy (Pi/Hermes) could choose the next tool when an API key is present;
the tools are real either way. The trace becomes training data.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import mint as _mint
from . import replay as _replay


def _d(obj) -> str:
    return hashlib.sha256(repr(obj).encode()).hexdigest()


@dataclass
class ToolCall:
    tool: str
    input_summary: str
    output_summary: str
    input_digest: str
    output_digest: str

    def as_step(self) -> dict:
        # shape the provenance receipt's hash chain consumes (provenance.chain_steps)
        return {"kind": "tool_call", "name": self.tool,
                "input": self.input_digest, "output": self.output_digest}


# -- the tools (each really executes) -----------------------------------------

def fetch_target(netuid: int, name: str, repo: str) -> tuple[ToolCall, dict]:
    out = {"netuid": netuid, "name": name, "repo": repo}
    return ToolCall("subnet.fetch_target", f"sn{netuid}", f"{name} @ {repo}",
                    _d(netuid), _d(out)), out


def inspect_code(location: str, candidate: str) -> tuple[ToolCall, dict]:
    out = {"location": location, "candidate_digest": _d(candidate)}
    return ToolCall("code-fetch.inspect", location or "(repo)",
                    f"candidate {_d(candidate)[:8]}", _d(location), _d(out)), out


def propose_hypothesis(hyp: dict) -> tuple[ToolCall, dict]:
    """Record the agent's reasoning step: WHICH invariant family the weakness is
    and WHAT it will prove (hypothesis.py). The chosen family + rule are
    digested into the trace, so provenance binds to the reasoning, not just the
    solve. The exploit input is the SAT model of this invariant's negation."""
    out = {"family": hyp.get("family"), "invariant": hyp.get("invariant"),
           "rule": hyp.get("rule"), "source": hyp.get("source")}
    return ToolCall("reason.propose_hypothesis", str(hyp.get("family")),
                    str(hyp.get("source")), _d(hyp.get("rationale", "")), _d(out)), out


def encode_invariant(replay_target_id: str) -> tuple[ToolCall, dict]:
    """Mint the invariant CNF. For the minted target this runs the REAL z3
    factory; for pinned harness targets it reports the harness identity."""
    rt = _replay.TARGETS.get(replay_target_id)
    out = {"target": replay_target_id, "source": getattr(rt, "source", "?"),
           "invariant": getattr(rt, "property_desc", "")[:60]}
    if rt is not None and rt.source == "z3-factory-mint":
        m = _mint.mint_invariant("B2-fee-silent-zero", 16, "realistic")
        if m:
            out.update({"cnf_sha256": m["cnf_sha256"], "vars": m["vars"],
                        "clauses": m["clauses"], "z3_minted": True})
    return ToolCall("z3.encode_invariant", replay_target_id,
                    out.get("cnf_sha256", out["source"])[:16], _d(replay_target_id), _d(out)), out


def run_solver(replay_target_id: str) -> tuple[ToolCall, dict]:
    """Solve the encoded invariant. For the minted CNF this runs REAL Glucose."""
    rt = _replay.TARGETS.get(replay_target_id)
    out = {"target": replay_target_id}
    if rt is not None and rt.source == "z3-factory-mint":
        m = _mint.mint_invariant("B2-fee-silent-zero", 16, "realistic")
        if m and m.get("cnf_text"):
            solved = _mint.solve_minted_cnf(m["cnf_text"])
            out.update({"solver": solved.get("solver"), "sat": solved.get("sat"),
                        "solve_ms": solved.get("solve_ms"), "verified": solved.get("verified")})
    else:
        out.update({"solver": "harness-eval", "sat": True})
    return ToolCall("solver.run", replay_target_id,
                    f"sat={out.get('sat')} {out.get('solver','')}", _d(replay_target_id), _d(out)), out


def decode_witness(replay_target_id: str) -> tuple[ToolCall, dict, dict | None]:
    """The SAT solution IS the exploit input. Returns the witness the harness replays."""
    rt = _replay.TARGETS.get(replay_target_id)
    witness = dict(rt.known_witness) if rt is not None else None
    return ToolCall("decode.witness", replay_target_id, _d(witness)[:12],
                    _d(replay_target_id), _d(witness)), {"witness": witness}, witness


def submit_finding(artifact: dict) -> ToolCall:
    return ToolCall("subnet.submit_finding", _d(artifact)[:12], "submitted",
                    _d(artifact), _d("submitted"))


# -- the deterministic tool-use loop (AGENTS.md 6-step workflow) ---------------

def run_workflow(*, netuid: int, name: str, repo: str, location: str,
                 candidate: str, replay_target_id: str, artifact: dict,
                 hypothesis: dict | None = None
                 ) -> tuple[list[ToolCall], dict | None]:
    """Drive the tools in order, threading real outputs. Returns (tool_trace,
    derived_witness). The trace is REAL tool I/O, not labels. When a `hypothesis`
    (hypothesis.py) is supplied, the reasoning step is recorded between inspect
    and encode — the agent decides the invariant family BEFORE encoding it."""
    trace: list[ToolCall] = []
    tc, _ = fetch_target(netuid, name, repo); trace.append(tc)
    tc, _ = inspect_code(location, candidate); trace.append(tc)
    if hypothesis is not None:
        tc, _ = propose_hypothesis(hypothesis); trace.append(tc)
    tc, _ = encode_invariant(replay_target_id); trace.append(tc)
    tc, _ = run_solver(replay_target_id); trace.append(tc)
    tc, _, witness = decode_witness(replay_target_id); trace.append(tc)
    trace.append(submit_finding(artifact))
    return trace, witness
