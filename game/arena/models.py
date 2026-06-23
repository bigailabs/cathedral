"""Typed arena objects. Targets and proof tasks are loaded from the REAL
audit-hunter corpus (corpus.py); missions, agent runs, and gate outcomes are the
live game layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# -- real corpus objects ------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """A subnet in the arena — one of the 17 registered targets."""
    netuid: int
    name: str
    repo: str
    our_uid: int | None            # our registered UID on that subnet
    candidate_title: str           # the candidate finding (the objective)
    severity: int                  # 0-10 candidate severity (display only, NOT live score)
    location: str                  # file:line of the suspected weakness
    exploit_steps: str             # how the finding would be triggered (proof recipe)
    exploit_resource: str          # what's needed to EARN live (gating resource)
    risk_level: str                # local-replay | sandbox | testnet | read-only-mainnet

    @property
    def target_class(self) -> str:
        """What you're breaking: subnet | contract | chain. The 17 registered
        targets are subnets; the money-math CNF vaults are contracts/chains."""
        return "subnet"

    @property
    def family(self) -> str:
        # coarse attack class for the heat map
        t = self.candidate_title.lower()
        if "score" in t or "inflation" in t or "coverage" in t:
            return "G_scoring"
        if "replay" in t or "reference" in t or "ownership" in t:
            return "H_trust"
        if "emission" in t or "reward" in t or "payout" in t:
            return "F_emission"
        if "conservation" in t or "split" in t:
            return "A_conservation"
        return "B_bounds"


@dataclass(frozen=True)
class ProofTask:
    """A subtensor money-math CNF: SAT iff an input violates a financial
    invariant (solving it = finding the bug-triggering input)."""
    cnf_id: str
    model: str                     # subtensor-amm | subtensor-root-reborn
    rule: str
    family: str
    invariant: str
    vars: int
    clauses: int
    result: str                    # sat | unsat | unknown (independently verified)
    tier: str                      # trivial | board | mid | hard
    role: str
    triage: str                    # honest reachability judgment
    file: str
    witness: dict[str, Any] | None


# -- live game objects --------------------------------------------------------

@dataclass
class Mission:
    """A concrete objective assigned to one agent for one round."""
    mission_id: str
    target: Target
    objective: str                 # what to prove
    proof_kind: str                # encoded_invariant | replay | benchmark
    attestation_required: bool
    nonce: str                     # fresh per (agent, mission, round)
    tier: int                      # 1 floor / 2 premium (drives weight + attest gate)
    bounty: bool = False           # higher-value verified objective
    replay_target_id: str = ""     # the REAL pinned money-math invariant to reproduce
    nonce_issued_tick: int = 0     # round tick the nonce was issued (for TTL/expiry)


@dataclass
class AgentRun:
    """A miner-operated agent's full trace — becomes training data later."""
    agent_id: str
    miner_hotkey: str
    coldkey: str
    environment: str               # local-untrusted | stitch-runner | polaris-tee-cpu |
                                   # polaris-tee-gpu | mocked-tee
    mission_id: str
    target_netuid: int
    commands: list[str] = field(default_factory=list)      # tool calls the agent ran
    files_inspected: list[str] = field(default_factory=list)
    hypothesis: str = ""           # invariant/exploit hypothesis
    encoder: str = ""              # encoder/solver used
    artifact: dict[str, Any] = field(default_factory=dict)  # submitted proof
    replay_result: dict[str, Any] | None = None
    attestation: dict[str, Any] | None = None
    trace_sha256: str = ""

    def status_line(self) -> str:
        return f"{self.agent_id}@{self.environment} -> sn{self.target_netuid}"


@dataclass
class GateOutcome:
    """The full boolean gate set for one submission. reward is gated by ALL."""
    valid_identity: bool = False
    assigned_mission: bool = False
    fresh_nonce: bool = False
    no_replay: bool = False
    correct_owner: bool = False
    complete_artifact: bool = False
    cnf_hash_matches: bool = False
    decode_map_present: bool = False  # the proof carries the witness decode map
    witness_verifies: bool = False
    replay_succeeds: bool = False
    compute_profile_honest: bool = False  # advertised compute matches attestable reality
    attestation_valid: bool = False   # True when not required OR required+valid
    agent_signature_valid: bool = False   # the agent really signed its receipt
    provenance_chain_intact: bool = False  # tamper-evident hash chain unbroken
    hypothesis_aligned: bool = False  # the family the agent COMMITTED == the invariant it proved
    reasons: list[str] = field(default_factory=list)
    provenance_grade: str = "F"

    GATES = ("valid_identity", "assigned_mission", "fresh_nonce", "no_replay",
             "correct_owner", "complete_artifact", "cnf_hash_matches",
             "decode_map_present", "witness_verifies", "replay_succeeds",
             "compute_profile_honest", "attestation_valid",
             "agent_signature_valid", "provenance_chain_intact",
             "hypothesis_aligned")

    def passed(self) -> bool:
        return all(getattr(self, g) for g in self.GATES)

    def first_failure(self) -> str | None:
        for g in self.GATES:
            if not getattr(self, g):
                return g
        return None

    def as_dict(self) -> dict[str, bool]:
        return {g: getattr(self, g) for g in self.GATES}


@dataclass
class TargetState:
    """Live status of a target in the arena, for the UI grid."""
    netuid: int
    status: str = "untouched"      # untouched|probing|encoded|solved|replayed|verified|rejected
    agents: list[str] = field(default_factory=list)
    verified_proofs: int = 0
    rejected: int = 0
