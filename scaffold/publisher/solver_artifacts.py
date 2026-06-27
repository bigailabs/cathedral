"""Solver artifact verification for verifiable SAT challenges.

This module is deliberately separate from `sat_solution.py`.  The live
synthetic SAT lane still expects a satisfying assignment only.  Verifiable
publisher/agent challenges need the larger contract:

* SAT: a DIMACS assignment that satisfies the CNF.
* UNSAT: a DRAT proof that `drat-trim` verifies against the CNF.
* Artifacts: logs/files are carried for provenance but never trusted as proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from .sat_solution import verify_dimacs_solution
from ..verify import verify_unsat_cert


SOLVER_ARTIFACT_SCHEMA = "cathedral.solver_artifact.v1"


@dataclass(frozen=True)
class SolverArtifact:
    outcome: str
    dimacs_solution: str = ""
    drat_proof: str = ""
    stdout: str = ""
    stderr: str = ""
    files: dict[str, str] = field(default_factory=dict)
    schema_version: str = SOLVER_ARTIFACT_SCHEMA

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SolverArtifact":
        if not isinstance(value, dict):
            raise ValueError("solver_artifact_must_be_object")
        return cls(
            schema_version=str(value.get("schema_version") or ""),
            outcome=str(value.get("outcome") or value.get("status") or "").upper(),
            dimacs_solution=str(value.get("dimacs_solution") or ""),
            drat_proof=str(value.get("drat_proof") or value.get("drat") or ""),
            stdout=str(value.get("stdout") or ""),
            stderr=str(value.get("stderr") or ""),
            files={str(k): str(v) for k, v in dict(value.get("files") or {}).items()},
        )

    def hashes(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "dimacs_solution_sha256": _sha_text(self.dimacs_solution),
            "drat_proof_sha256": _sha_text(self.drat_proof),
            "stdout_sha256": _sha_text(self.stdout),
            "stderr_sha256": _sha_text(self.stderr),
            "files_sha256": _sha_obj(self.files),
        }


@dataclass(frozen=True)
class SolverArtifactCheck:
    ok: bool
    outcome: str
    proof_kind: str
    rejection_reason: str | None = None
    assignment: list[int] = field(default_factory=list)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    verifier_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "outcome": self.outcome,
            "proof_kind": self.proof_kind,
            "rejection_reason": self.rejection_reason,
            "assignment": self.assignment,
            "artifact_hashes": self.artifact_hashes,
            "verifier_details": self.verifier_details,
        }


def verify_solver_artifact(cnf_text: str, artifact: SolverArtifact | dict[str, Any]) -> SolverArtifactCheck:
    art = artifact if isinstance(artifact, SolverArtifact) else SolverArtifact.from_dict(artifact)
    hashes = art.hashes()
    if art.schema_version != SOLVER_ARTIFACT_SCHEMA:
        return _reject(art, hashes, "schema_mismatch")

    outcome = _normalize_outcome(art.outcome)
    if outcome == "SAT":
        check = verify_dimacs_solution(cnf_text, art.dimacs_solution)
        if not check.ok:
            return _reject(art, hashes, check.rejection_reason or "sat_assignment_invalid")
        return SolverArtifactCheck(
            ok=True,
            outcome="SAT",
            proof_kind="assignment",
            assignment=check.assignment,
            artifact_hashes=hashes,
            verifier_details={"sat_assignment_verified": True},
        )

    if outcome == "UNSAT":
        unsat = verify_unsat_cert(cnf_text, art.drat_proof)
        if unsat.stub:
            return _reject(art, hashes, unsat.reason, verifier_details={"stub": True})
        if not unsat.ok:
            return _reject(art, hashes, unsat.reason, verifier_details={"stub": False})
        return SolverArtifactCheck(
            ok=True,
            outcome="UNSAT",
            proof_kind="drat",
            artifact_hashes=hashes,
            verifier_details={"drat_trim": unsat.reason, "stub": False},
        )

    return _reject(art, hashes, "unknown_solver_outcome")


def _normalize_outcome(value: str) -> str:
    value = value.strip().upper()
    if "UNSAT" in value:
        return "UNSAT"
    if "SAT" in value:
        return "SAT"
    return value


def _reject(
    artifact: SolverArtifact,
    hashes: dict[str, str],
    reason: str,
    *,
    verifier_details: dict[str, Any] | None = None,
) -> SolverArtifactCheck:
    return SolverArtifactCheck(
        ok=False,
        outcome=_normalize_outcome(artifact.outcome),
        proof_kind="none",
        rejection_reason=reason,
        artifact_hashes=hashes,
        verifier_details=verifier_details or {},
    )


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_obj(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
