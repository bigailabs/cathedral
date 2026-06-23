"""Verify-by-receipt provenance — the Hermes/Pi shell model, made cryptographic.

A REAL agent does not hand the arena a trust-me dict; it emits a **signed
run-receipt**: the ordered workflow steps (tool_calls / model_calls, per
bundle/AGENTS.md), the submitted artifact, and the Operator/Polaris attestation,
all **hash-chained** step-to-step and **Ed25519-signed by the agent's own key**.
The arena VERIFIES the receipt; it never trusts a claim.

This is the provenance backbone Cathedral's "verify-by-receipt" thesis needs:

    step[i].prev_hash == hash(step[i-1])              (chain intact, tamper-evident)
    receipt.head == hash(last step)                   (artifact bound to the chain)
    Ed25519.verify(agent_pubkey, sig, canonical(receipt without sig))   (real agent)
    attestation receipt present + valid (if required) (trusted execution)

Grades:
    A  signed + chain intact + replay ok + attested
    B  signed + chain intact + replay ok   (no attestation required/why)
    C  signed + chain intact               (not yet replayed)
    F  any broken link (unsigned, wrong key, forged step, replayed receipt)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)


def _h(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


# -- the agent's on-host identity --------------------------------------------

class AgentKey:
    """An agent's own Ed25519 identity. A real submitted agent owns this key on
    its host; the arena only ever sees the public half + signatures."""

    def __init__(self, seed: bytes | None = None):
        if seed is not None:
            self._sk = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed).digest())
        else:
            self._sk = Ed25519PrivateKey.generate()

    @property
    def pub_hex(self) -> str:
        return self._sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    def sign(self, payload: dict) -> str:
        return self._sk.sign(_h({k: v for k, v in payload.items() if k != "sig"}).encode()).hex()


@dataclass
class Step:
    """One step of the agent's workflow (a tool_call or model_call)."""
    idx: int
    kind: str                  # tool_call | model_call
    name: str                  # e.g. code-fetch.clone_repo, solver.search
    input_digest: str          # sha256 of the step input
    output_digest: str         # sha256 of the step output
    prev_hash: str             # hash of the previous step (the chain link)

    def as_dict(self) -> dict:
        return {"idx": self.idx, "kind": self.kind, "name": self.name,
                "input_digest": self.input_digest, "output_digest": self.output_digest,
                "prev_hash": self.prev_hash}


def chain_steps(raw_steps: list[dict]) -> list[Step]:
    """Hash-chain a list of {kind,name,input,output} into linked Steps."""
    steps: list[Step] = []
    prev = "GENESIS"
    for i, s in enumerate(raw_steps):
        step = Step(idx=i, kind=s.get("kind", "tool_call"), name=s["name"],
                    input_digest=_h(s.get("input", "")), output_digest=_h(s.get("output", "")),
                    prev_hash=prev)
        prev = _h(step.as_dict())
        steps.append(step)
    return steps


# -- the receipt the agent submits -------------------------------------------

@dataclass
class Receipt:
    agent_id: str
    agent_pubkey: str
    miner_hotkey: str
    mission_id: str
    nonce: str
    environment: str
    steps: list[Step]
    artifact: dict[str, Any]              # {challenge_id, cnf_sha256, assignment, ...}
    attestation: dict[str, Any] | None    # Operator/Polaris receipt (or None)
    head: str = ""                        # hash of the last step (binds the chain)
    sig: str = ""                         # Ed25519 over the whole receipt

    def body(self) -> dict:
        return {"agent_id": self.agent_id, "agent_pubkey": self.agent_pubkey,
                "miner_hotkey": self.miner_hotkey, "mission_id": self.mission_id,
                "nonce": self.nonce, "environment": self.environment,
                "steps": [s.as_dict() for s in self.steps], "artifact": self.artifact,
                "attestation": self.attestation, "head": self.head}


def receipt_to_dict(r: Receipt) -> dict:
    """Serialize a signed receipt to cross the process boundary (agent -> arena)."""
    return {**r.body(), "sig": r.sig}


def receipt_from_dict(d: dict) -> Receipt:
    """Reconstruct a Receipt an external agent produced. The arena VERIFIES it;
    deserialization performs no trust."""
    steps = [Step(idx=s["idx"], kind=s["kind"], name=s["name"],
                  input_digest=s["input_digest"], output_digest=s["output_digest"],
                  prev_hash=s["prev_hash"]) for s in d.get("steps", [])]
    return Receipt(
        agent_id=d["agent_id"], agent_pubkey=d["agent_pubkey"], miner_hotkey=d["miner_hotkey"],
        mission_id=d["mission_id"], nonce=d["nonce"], environment=d["environment"],
        steps=steps, artifact=d["artifact"], attestation=d.get("attestation"),
        head=d.get("head", ""), sig=d.get("sig", ""))


def build_receipt(key: AgentKey, *, agent_id: str, miner_hotkey: str, mission_id: str,
                  nonce: str, environment: str, raw_steps: list[dict], artifact: dict,
                  attestation: dict | None) -> Receipt:
    """A real agent assembles + signs its run-receipt on its own host."""
    steps = chain_steps(raw_steps)
    head = _h(steps[-1].as_dict()) if steps else "EMPTY"
    r = Receipt(agent_id=agent_id, agent_pubkey=key.pub_hex, miner_hotkey=miner_hotkey,
                mission_id=mission_id, nonce=nonce, environment=environment, steps=steps,
                artifact=artifact, attestation=attestation, head=head)
    r.sig = key.sign(r.body())
    return r


@dataclass
class ProvenanceVerdict:
    signature_ok: bool = False
    chain_intact: bool = False
    head_binds_artifact: bool = False
    replay_ok: bool = False
    attested: bool = False
    not_replayed_receipt: bool = True     # receipt nonce not seen before
    grade: str = "F"
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # provenance is intact iff the agent really signed an unbroken chain that
        # binds the artifact, and the receipt is fresh.
        return (self.signature_ok and self.chain_intact and self.head_binds_artifact
                and self.not_replayed_receipt)


def verify_receipt(r: Receipt, *, expected_hotkey: str, expected_mission: str,
                   expected_nonce: str, expected_artifact: dict,
                   replay_ok: bool, attestation_required: bool,
                   seen_receipts: set[str]) -> ProvenanceVerdict:
    """Verify-by-receipt: trust the cryptographic chain, not the agent's word."""
    v = ProvenanceVerdict()

    # 1. real agent signature over the exact receipt body
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(r.agent_pubkey))
        pk.verify(bytes.fromhex(r.sig), _h(r.body()).encode())
        v.signature_ok = True
    except Exception:
        v.reasons.append("agent_signature_invalid")

    # 2. hash chain intact + head correct (forged/edited step breaks this)
    prev, intact = "GENESIS", True
    for s in r.steps:
        if s.prev_hash != prev:
            intact = False
            break
        prev = _h(s.as_dict())
    v.chain_intact = intact and bool(r.steps)
    if not v.chain_intact:
        v.reasons.append("provenance_chain_broken")
    expected_head = _h(r.steps[-1].as_dict()) if r.steps else "EMPTY"
    if r.head != expected_head:
        v.reasons.append("head_hash_mismatch")

    # 3. the receipt binds the ACTUAL submitted artifact + assigned mission/nonce
    v.head_binds_artifact = (
        r.head == expected_head
        and r.artifact.get("challenge_id") == expected_artifact.get("challenge_id")
        and r.artifact.get("cnf_sha256") == expected_artifact.get("cnf_sha256")
        and r.miner_hotkey == expected_hotkey
        and r.mission_id == expected_mission
        and r.nonce == expected_nonce)
    if not v.head_binds_artifact:
        v.reasons.append("receipt_does_not_bind_submission")

    # 4. freshness — a re-submitted (replayed) receipt is rejected
    rid = _h(r.body()) + r.sig
    v.not_replayed_receipt = rid not in seen_receipts
    if not v.not_replayed_receipt:
        v.reasons.append("receipt_replayed")

    # 5. execution facts
    v.replay_ok = bool(replay_ok)
    v.attested = bool(r.attestation and r.attestation.get("valid"))
    if attestation_required and not v.attested:
        v.reasons.append("attestation_missing_or_invalid")

    # grade
    if v.ok and v.replay_ok and (v.attested or not attestation_required) and v.attested:
        v.grade = "A"
    elif v.ok and v.replay_ok and not attestation_required:
        v.grade = "B"
    elif v.ok and v.replay_ok:
        v.grade = "B-"            # required attestation absent
    elif v.ok:
        v.grade = "C"
    else:
        v.grade = "F"
    return v
