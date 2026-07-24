"""Lane S — the solver arena. The v4 flagship: a standing world-record bounty.

Miners publish an OPEN-SOURCE solver (source + a container pinned by digest) and
get paid only by provably BEATING the reigning champion on a fresh hidden batch
of diverse instances, under competition conditions, on the eval host's standard
hardware. Every scored result carries its certificate (a SAT witness re-checked
against the CNF, or a DRAT proof re-checked by drat-trim); a TIMEOUT is the eval
host's own observation, never a miner claim.

This extends `solver_docker` (Lane B, attested single-solve) into the full
solver-commit mechanism the design calls for:

  * SOLVER REGISTRY — (source_url, container_digest, source_sha256). Dedup on
    source hash (a copy of the champion can never beat the champion — it dedups
    to the same commitment), one-eval-per-commitment (a hotkey can't farm weight
    by re-submitting an already-evaluated solver).
  * EVAL BATCH RUNNER — run a solver over N seeded instances under the eval
    host's containment (scaffold.lanes.sandbox: network-isolated, rlimit-bounded;
    salvaged from the monolith's oracle jail). Collect per-instance
    (outcome, wall_ms, certificate) and VERIFY EVERY CERTIFICATE before crediting.
  * CHAMPION STATE MACHINE — current champion, challenger evaluation, a STRICT
    dominance margin (a challenger must beat the champion's PAR-2 by at least
    DETHRONE_MARGIN_MS to take the crown), and a record-fall EVENT that the money
    layer consumes (jackpot to the new champion + burn steps down one notch).
  * SCORING — PAR-2 (penalized average runtime, the production v6 metric) plus a
    MARGINAL-VBS bonus (extra weight for instances ONLY your solver closes that
    the champion could not). Built ON TOP of grading.py via the lane's score();
    grading.py is NOT forked.

Sybil property (must hold in every lane): k identities submitting the SAME solver
all dedup to one source-hash commitment -> one evaluation, one merit. k copies of
the champion -> zero marginal merit. Enforced by the registry, asserted in
rc_verify.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable

from ..contract import (
    GenerateCtx, HiddenMetadata, Outcome, PublicProblem, ScoreResult,
    Submission, VerifierResult,
)
from ..dimacs import gen_planted_3sat, verify_witness
from ..verify import verify_unsat_cert
from .. import grading
from . import sandbox

FAMILY_ID = "solver_arena_v1"
SCHEMA_VERSION = 1

# A challenger must beat the champion's PAR-2 by at least this many ms to take
# the crown. A fixed margin makes dethroning robust to eval-host noise — a tie or
# a within-noise win does NOT topple the record (the locked "fixed dethrone
# margin" decision).
DETHRONE_MARGIN_MS = 1000.0
# PAR-k penalty: an unsolved (timeout/abstain/bad-cert) instance contributes
# PAR_K * timeout to the average. PAR-2 is the SAT-competition standard.
PAR_K = 2.0
# Per-instance batch wall limit (ms). Server-measured; the eval host's, not the
# solver's. Scales with tier in mint, recorded on the batch.
_TIERS = {0: (20, 80, 5_000), 1: (60, 255, 20_000), 2: (120, 510, 60_000)}


# --------------------------------------------------------------------------
# Solver registry — the price of getting paid is publishing.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SolverSpec:
    """A published solver commitment. Identity is the SOURCE HASH: two miners who
    publish the same source produce the same commitment_id and dedup to one."""
    source_url: str
    container_digest: str            # pinned by content digest (sha256:...), per pinning.py
    source_sha256: str               # hash of the open-source tarball — the dedup key
    owner_hotkey: str = ""

    @property
    def commitment_id(self) -> str:
        # Identity is the source hash ALONE: a copy of the champion (same source)
        # is the same commitment, so it can never "beat" the champion, and k
        # sybil identities publishing one solver collapse to one commitment.
        return self.source_sha256


class SolverRegistry:
    """Source-hash-deduped registry with one-eval-per-commitment accounting."""

    def __init__(self) -> None:
        self._by_commitment: dict[str, SolverSpec] = {}
        self._evaluated: set[str] = set()    # commitment_ids that have been evaluated

    def register(self, spec: SolverSpec) -> tuple[bool, str]:
        """Register a solver. Returns (accepted, reason). Re-registering the same
        source hash is a DEDUP no-op (not an error): the first publisher owns the
        commitment; later identical submissions add zero marginal merit."""
        cid = spec.commitment_id
        if cid in self._by_commitment:
            return False, "duplicate_source_hash"
        self._by_commitment[cid] = spec
        return True, "registered"

    def get(self, commitment_id: str) -> SolverSpec | None:
        return self._by_commitment.get(commitment_id)

    def all(self) -> list[SolverSpec]:
        return list(self._by_commitment.values())

    def needs_eval(self, commitment_id: str) -> bool:
        """One-eval-per-commitment: True only the first time a commitment is seen
        for evaluation. A re-submission of an already-evaluated solver is dropped
        (no weight farming by replay)."""
        return commitment_id in self._by_commitment and commitment_id not in self._evaluated

    def mark_evaluated(self, commitment_id: str) -> None:
        self._evaluated.add(commitment_id)


# --------------------------------------------------------------------------
# Eval batch — N seeded instances + per-instance certified results.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Instance:
    """One seeded eval instance: its CNF and the host's per-instance wall limit."""
    task_id: str
    cnf: str
    timeout_ms: float


@dataclass(frozen=True)
class InstanceResult:
    """One solver's certified outcome on one instance, as the eval host saw it."""
    task_id: str
    outcome: Outcome              # SAT / UNSAT / TIMEOUT / INVALID
    wall_ms: float                # host-MEASURED (never the solver's word)
    cert_ok: bool                 # certificate independently re-verified
    cert_kind: str                # "witness" | "drat" | "" (none / timeout)
    reason: str = ""

    @property
    def solved(self) -> bool:
        """A certified close. Only a re-verified SAT/UNSAT counts; an unverified
        certificate is NOT a solve (a forged DRAT or liar witness earns nothing)."""
        return self.cert_ok and self.outcome in (Outcome.SAT, Outcome.UNSAT)


# A solver adapter turns (cnf, timeout_ms) into a sandboxed RunResult + a parsed
# (outcome, certificate). Real solvers shell to kissat/cadical via sandbox; tests
# inject stubs. The adapter NEVER decides correctness — that's the cert check.
SolverAdapter = Callable[[str, float], "AdapterOutput"]


@dataclass(frozen=True)
class AdapterOutput:
    claimed: Outcome             # what the solver reported (SAT/UNSAT/TIMEOUT)
    witness: list[int]           # assignment if it claimed SAT
    drat: str                    # proof text if it claimed UNSAT
    run: sandbox.RunResult       # the host's containment observation (timing, isolation)


def run_batch(adapter: SolverAdapter, instances: list[Instance]) -> list[InstanceResult]:
    """Run a solver over a batch and CERTIFY every result. This is the referee:
    a claimed SAT is credited only if the witness re-checks against the CNF; a
    claimed UNSAT only if drat-trim verifies the proof; a TIMEOUT (or a host-
    observed timeout regardless of claim) is a non-solve. Pure given the adapter.
    """
    out: list[InstanceResult] = []
    for inst in instances:
        ao = adapter(inst.cnf, inst.timeout_ms)
        run = ao.run
        # The eval host's own observation overrides any solver claim: if the host
        # timed it out, it's a TIMEOUT — full stop (the locked TIMEOUT policy).
        if run.timed_out:
            out.append(InstanceResult(inst.task_id, Outcome.TIMEOUT, run.wall_ms,
                                      False, "", "host_observed_timeout"))
            continue
        if ao.claimed == Outcome.SAT:
            ok = verify_witness(inst.cnf, ao.witness)
            out.append(InstanceResult(
                inst.task_id, Outcome.SAT if ok else Outcome.INVALID, run.wall_ms,
                ok, "witness", "" if ok else "witness_does_not_satisfy"))
        elif ao.claimed == Outcome.UNSAT:
            chk = verify_unsat_cert(inst.cnf, ao.drat)
            ok = chk.ok and not chk.stub
            out.append(InstanceResult(
                inst.task_id, Outcome.UNSAT if ok else Outcome.INVALID, run.wall_ms,
                ok, "drat",
                "" if ok else (chk.reason if not chk.stub else "drat_unverified_stub")))
        else:
            out.append(InstanceResult(inst.task_id, Outcome.TIMEOUT, run.wall_ms,
                                      False, "", "solver_abstained"))
    return out


# --------------------------------------------------------------------------
# PAR-2 + marginal-VBS scoring (built on grading, not forking it).
# --------------------------------------------------------------------------
def par2_ms(results: list[InstanceResult], timeout_ms_by_task: dict[str, float]) -> float:
    """Penalized average runtime: a solved instance contributes its host-measured
    wall_ms; an unsolved one contributes PAR_K * its timeout. Lower is better.
    This is the production v6 PAR-2 metric — the champion ordering is by PAR-2."""
    if not results:
        return float("inf")
    total = 0.0
    for r in results:
        if r.solved:
            total += r.wall_ms
        else:
            total += PAR_K * timeout_ms_by_task.get(r.task_id, r.wall_ms)
    return round(total / len(results), 3)


def marginal_vbs_count(challenger: list[InstanceResult],
                       champion: list[InstanceResult]) -> int:
    """Instances the challenger closes that the champion does NOT — the marginal
    contribution to the Virtual Best Solver. Diversity becomes economically
    rational: closing a hard instance nobody else can is worth a bonus, which is
    the MallobSat/SATzilla portfolio lesson encoded as a reward."""
    champ_solved = {r.task_id for r in champion if r.solved}
    return sum(1 for r in challenger if r.solved and r.task_id not in champ_solved)


# --------------------------------------------------------------------------
# Champion state machine.
# --------------------------------------------------------------------------
@dataclass
class RecordFall:
    """Emitted when the record falls — the money layer consumes this (jackpot to
    the new champion + burn steps down one notch). NOT a chain write here."""
    new_champion: str            # commitment_id
    old_champion: str | None
    old_par2_ms: float
    new_par2_ms: float
    margin_ms: float
    batch_size: int


@dataclass
class ChampionState:
    """The reigning champion + its scoreboard on the last batch."""
    commitment_id: str | None = None
    par2_ms: float = float("inf")
    results: list[InstanceResult] = field(default_factory=list)
    spec: SolverSpec | None = None


class ChampionMachine:
    """Current champion vs challengers, with a STRICT dominance margin.

    Launch champion = the SC2025 winner binary (kissat-sc2025), seeded via
    seed_champion(). A challenger dethrones ONLY if its PAR-2 beats the champion's
    by at least DETHRONE_MARGIN_MS on the FULL batch — a tie or within-noise win
    leaves the record standing. A dethrone emits a RecordFall.
    """

    def __init__(self, dethrone_margin_ms: float = DETHRONE_MARGIN_MS) -> None:
        self.champion = ChampionState()
        self.dethrone_margin_ms = dethrone_margin_ms

    def seed_champion(self, spec: SolverSpec, results: list[InstanceResult],
                      timeout_ms_by_task: dict[str, float]) -> None:
        """Install the launch champion (SC2025 winner) and its baseline scoreboard."""
        self.champion = ChampionState(
            commitment_id=spec.commitment_id, spec=spec,
            par2_ms=par2_ms(results, timeout_ms_by_task), results=results)

    def consider(self, spec: SolverSpec, results: list[InstanceResult],
                 timeout_ms_by_task: dict[str, float]) -> RecordFall | None:
        """Evaluate a challenger against the reigning champion. Returns a
        RecordFall iff the record fell, else None. Updates the champion on a fall.
        """
        chal_par2 = par2_ms(results, timeout_ms_by_task)
        old = self.champion
        # No champion yet (cold start with no seeded SC2025 binary) -> the first
        # solver that closes anything becomes champion (records a fall vs None).
        if old.commitment_id is None:
            fall = RecordFall(spec.commitment_id, None, float("inf"), chal_par2,
                              float("inf"), len(results))
            self.champion = ChampionState(spec.commitment_id, chal_par2, results, spec)
            return fall
        # A copy of the champion has the same commitment_id -> cannot dethrone.
        if spec.commitment_id == old.commitment_id:
            return None
        margin = old.par2_ms - chal_par2
        if margin >= self.dethrone_margin_ms:
            fall = RecordFall(spec.commitment_id, old.commitment_id,
                              old.par2_ms, chal_par2, round(margin, 3), len(results))
            self.champion = ChampionState(spec.commitment_id, chal_par2, results, spec)
            return fall
        return None


# --------------------------------------------------------------------------
# The Lane — contract surface over the arena machinery.
# --------------------------------------------------------------------------
class SolverArenaLane:
    """Contract-shaped facade so the validator loop drives Lane S like any lane.

    mint_challenge -> a batch descriptor (the public hidden-batch spec).
    validate_submission -> register + eval a submitted solver, certify the batch,
                           run it past the champion machine.
    score -> PAR-2 (vs champion) folded to [0,1] + marginal-VBS bonus.

    The heavy machinery (registry, champion machine) is stateful and lives on the
    lane instance — the validator constructs ONE lane and reuses it across rounds,
    so the champion persists. The pure contract functions remain total + bounded.
    """
    family_id = FAMILY_ID
    schema_version = SCHEMA_VERSION

    def __init__(self, registry: SolverRegistry | None = None,
                 champion: ChampionMachine | None = None,
                 adapters: dict[str, SolverAdapter] | None = None,
                 batch_size: int = 8) -> None:
        self.registry = registry or SolverRegistry()
        self.champion = champion or ChampionMachine()
        # commitment_id -> adapter. The eval host holds the run capability; the
        # submission carries only the commitment + spec, never executable trust.
        self.adapters = adapters or {}
        self.batch_size = batch_size
        self._last_fall: RecordFall | None = None

    def seed_launch_champion(self, spec: SolverSpec, results: list[InstanceResult],
                             timeout_ms_by_task: dict[str, float]) -> None:
        """Install the launch champion (SC2025 winner binary) AND register it so a
        later copy of it dedups to `already_evaluated_or_duplicate` rather than
        slipping through as a fresh commitment."""
        self.registry.register(spec)
        self.registry.mark_evaluated(spec.commitment_id)
        self.champion.seed_champion(spec, results, timeout_ms_by_task)

    # ---- batch construction (deterministic in seed) ----------------------
    def build_batch(self, seed: int, tier: int, n: int | None = None) -> list[Instance]:
        n_vars, n_clauses, tl = _TIERS.get(tier, _TIERS[1])
        n = n or self.batch_size
        out: list[Instance] = []
        for i in range(n):
            s = seed * 1_000_003 + i
            cnf, _ = gen_planted_3sat(s, n_vars, n_clauses)
            tid = hashlib.sha256(f"{FAMILY_ID}:{s}:{tier}".encode()).hexdigest()[:32]
            out.append(Instance(tid, cnf, float(tl)))
        return out

    def mint_challenge(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        batch = self.build_batch(ctx.seed, ctx.tier)
        n_vars, n_clauses, tl = _TIERS.get(ctx.tier, _TIERS[1])
        task_id = hashlib.sha256(f"{FAMILY_ID}:{ctx.seed}:{ctx.tier}".encode()).hexdigest()[:32]
        # The batch CNFs are HIDDEN (fresh per round, competition conditions); the
        # public problem advertises only the batch SHAPE + the champion to beat.
        problem = PublicProblem(
            task_family=FAMILY_ID, schema_version=SCHEMA_VERSION, task_id=task_id,
            difficulty_tier=ctx.tier,
            public_input={"batch_size": len(batch), "n_vars": n_vars,
                          "n_clauses": n_clauses, "per_instance_timeout_ms": tl,
                          "champion": self.champion.champion.commitment_id,
                          "scoring": "par2+marginal_vbs"},
            time_limit_seconds=int(tl / 1000) * len(batch))
        hidden = HiddenMetadata(
            task_id=task_id, generator_version="arena-batch/1",
            hidden_payload={"seed": ctx.seed, "tier": ctx.tier,
                            "instances": [(b.task_id, b.cnf, b.timeout_ms) for b in batch]})
        return problem, hidden

    def _batch_from_hidden(self, hidden: HiddenMetadata) -> list[Instance]:
        return [Instance(t, c, to) for (t, c, to) in hidden.hidden_payload["instances"]]

    def validate_submission(
        self, problem: PublicProblem, hidden: HiddenMetadata, submission: Submission
    ) -> VerifierResult:
        """Register + evaluate a submitted solver against the hidden batch. TOTAL:
        a malformed submission, an unknown adapter, or a deduped/already-evaluated
        commitment all return a clean INVALID with a reason, never an exception."""
        ans = submission.answer
        src = ans.get("source_url")
        digest = ans.get("container_digest")
        shash = ans.get("source_sha256")
        if not (isinstance(src, str) and isinstance(digest, str) and isinstance(shash, str)
                and src and digest and shash):
            return VerifierResult(False, Outcome.INVALID, 0.0, "malformed_solver_commitment")
        spec = SolverSpec(src, digest, shash, owner_hotkey=submission.miner_hotkey)
        cid = spec.commitment_id

        # registry dedup: a duplicate source hash adds zero marginal merit.
        if self.registry.get(cid) is None:
            self.registry.register(spec)
        if not self.registry.needs_eval(cid):
            return VerifierResult(True, Outcome.INVALID, 0.0,
                                  "already_evaluated_or_duplicate",
                                  {"commitment_id": cid})

        adapter = self.adapters.get(cid)
        if adapter is None:
            return VerifierResult(True, Outcome.INVALID, 0.0, "no_eval_adapter",
                                  {"commitment_id": cid})

        instances = self._batch_from_hidden(hidden)
        results = run_batch(adapter, instances)
        self.registry.mark_evaluated(cid)
        timeouts = {b.task_id: b.timeout_ms for b in instances}
        chal_par2 = par2_ms(results, timeouts)

        # run the challenger past the champion machine (state transition lives in
        # verify so the champion persists across the validator's rounds).
        champ_state = self.champion.champion
        fall = self.champion.consider(spec, results, timeouts)
        self._last_fall = fall

        solved = sum(1 for r in results if r.solved)
        mvbs = marginal_vbs_count(results, champ_state.results)
        outcome = Outcome.SAT if solved > 0 else Outcome.TIMEOUT
        det = {
            "commitment_id": cid, "owner_hotkey": submission.miner_hotkey,
            "par2_ms": chal_par2, "champion_par2_ms": champ_state.par2_ms,
            "batch_size": len(results), "solved": solved,
            "marginal_vbs": mvbs,
            "record_fell": fall is not None,
            "is_champion": self.champion.champion.commitment_id == cid,
            "all_certified": all(r.cert_ok for r in results if r.outcome in
                                 (Outcome.SAT, Outcome.UNSAT)),
        }
        if fall is not None:
            det["record_fall"] = fall.__dict__
        # raw_metric carries the certified solve fraction (bounded [0,1]); score()
        # turns it into PAR-2-relative credit + the marginal-VBS bonus.
        raw = solved / len(results) if results else 0.0
        return VerifierResult(True, outcome, raw,
                              None if solved > 0 else "no_certified_solve", det)

    def score(self, problem: PublicProblem, verifier: VerifierResult,
              *, wall_ms: float | None = None) -> ScoreResult:
        """Bounded [0,1] arena score = PAR-2-relative base + marginal-VBS bonus.

        Built on grading.py (not forked): a non-solve routes through grading.grade
        to carry the standard INVALID/TIMEOUT zero + reason. A certified batch is
        scored on the production metric — PAR-2 relative to the champion — plus a
        bonus for instances only this solver closed. The CHAMPION (a record fall,
        or holding the crown) gets the full base; a challenger that didn't dethrone
        is credited proportionally to how close it ran, so progress is visible
        before the record actually falls.
        """
        d = verifier.details
        if not (verifier.parsed_ok and verifier.outcome == Outcome.SAT
                and verifier.raw_metric > 0):
            sr = grading.grade(verifier, wall_ms=0.0,
                               time_limit_ms=problem.time_limit_seconds * 1000,
                               speed_aware=False)
            return ScoreResult(sr.weighted_score, sr.rejection_reason,
                               {"par2_ms": float(d.get("par2_ms", 0.0))})

        par2 = float(d.get("par2_ms", float("inf")))
        champ_par2 = float(d.get("champion_par2_ms", float("inf")))
        # PAR-2-relative base in (0,1]: a challenger faster than the champion ->
        # >0.5, at the champion's PAR-2 -> 0.5, slower -> <0.5. Scale-free, the
        # same curve shape as grading.speed_bonus (champ_par2 is the half-credit
        # point), so the speed curve is reused, not re-implemented. With no
        # finite champion baseline (cold start) the certified solve fraction
        # (raw_metric) is the base.
        if champ_par2 not in (0.0, float("inf")):
            base = grading.speed_bonus(par2, champ_par2)
        else:
            base = verifier.raw_metric
        # crown bonus: holding the record (a fall this round, or being the reigning
        # champion) lifts the base toward 1.0 — the record-holder is paid most.
        crown = max(base, 1.0 if (d.get("record_fell") or d.get("is_champion")) else base)
        # marginal-VBS bonus: each uniquely-closed instance adds weight, capped so
        # the total stays bounded. Diversity is rewarded directly.
        mvbs = int(d.get("marginal_vbs", 0))
        bonus = min(0.3, 0.1 * mvbs)
        score = round(min(1.0, max(0.0, 0.7 * crown + bonus)), 6)
        return ScoreResult(score, None, {
            "base_par2": round(base, 6), "crown": round(crown, 6),
            "marginal_vbs": float(mvbs), "vbs_bonus": round(bonus, 6),
            "par2_ms": par2, "champion_par2_ms": champ_par2,
            "record_fell": 1.0 if d.get("record_fell") else 0.0,
            "is_champion": 1.0 if d.get("is_champion") else 0.0,
        })


# --------------------------------------------------------------------------
# Real solver adapter (shells to kissat/cadical via the sandbox).
# --------------------------------------------------------------------------
def real_solver_adapter(solver_bin: str) -> SolverAdapter:
    """Adapter that runs an installed solver (kissat/cadical) on each instance in
    the sandbox. Parses DIMACS solver output: 's SATISFIABLE' + 'v ...' -> SAT
    witness; 's UNSATISFIABLE' -> UNSAT (DRAT requires --proof; left to the
    real-container path). A host-observed timeout -> TIMEOUT. Used when a real
    binary is installed; tests inject stub adapters instead."""
    import tempfile
    from pathlib import Path

    def _adapt(cnf: str, timeout_ms: float) -> AdapterOutput:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.cnf"
            p.write_text(cnf)
            run = sandbox.run_solver([solver_bin, str(p)],
                                     timeout_s=timeout_ms / 1000.0)
        if run.timed_out:
            return AdapterOutput(Outcome.TIMEOUT, [], "", run)
        model: list[int] = []
        sat = unsat = False
        for line in run.stdout.splitlines():
            if line.startswith("s ") and "UNSATISFIABLE" in line:
                unsat = True
            elif line.startswith("s ") and "SATISFIABLE" in line:
                sat = True
            elif line.startswith("v "):
                model += [int(x) for x in line[2:].split() if x.strip() and x.lstrip("-").isdigit()
                          and int(x) != 0]
        if sat:
            return AdapterOutput(Outcome.SAT, model, "", run)
        if unsat:
            return AdapterOutput(Outcome.UNSAT, [], "", run)
        return AdapterOutput(Outcome.TIMEOUT, [], "", run)

    return _adapt
