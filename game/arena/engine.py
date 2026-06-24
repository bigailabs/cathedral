"""Arena engine — one round of the live miner-agent verification game.

Flow per agent: assign mission (subnet target) -> agent inspects + forms a
hypothesis -> encodes/solves the live deterministic invariant task -> submits
witness + full trace -> the ten boolean gates are evaluated -> attestation gates
premium work -> reward = metric x gate -> target/feed state updated for the UI.

Real primitives reused (not reimplemented):
  per_miner   deterministic, anti-copy encoded SAT task (the live proof)
  dimacs      independent witness verification
  grading     scale-free speed curve (host-measured)
  polaris     TDX attestation (offline stub, real binding recipe)
  reward      Const-rule compose + Sybil collapse + Ed25519 signed vector
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..reward import SolveCredit, compose, sign_vector
from . import corpus, economy, replay
from .models import AgentRun, GateOutcome, Mission, Target, TargetState
from .provenance import (AgentKey, Receipt, build_receipt, receipt_from_dict,
                         verify_receipt)
from .replay import run_replay
from .roster import AgentSpec, default_roster

# the pinned invariants honest agents reproduce (in assignment order): two
# subtensor money-math ports + the REAL audit_lane AuditTargets (actual pinned
# code, content-addressed) — so replay runs verbatim audit-lane invariant code.
REPRODUCING_TARGETS = (["subtensor-amm:recalc-overcharge@HEAD",
                        "subtensor-pallet:multi-take-split@HEAD"]
                       + list(replay.AUDIT_LANE_TARGETS)
                       + list(replay.MINTED_TARGETS))   # z3-minted: solve == replay

from scaffold.dimacs import solve_cnf, verify_witness
from scaffold.grading import speed_bonus
from scaffold.polaris import PolarisClient
from scaffold.publisher import per_miner as PM
from scaffold.verify import verify_attestation

# environments whose execution carries a hardware trust claim (attestable).
TRUSTED_ENVS = {"stitch-runner", "polaris-tee-cpu", "polaris-tee-gpu"}
NONCE_TTL_TICKS = 1          # a mission nonce is valid only within this many rounds
# environment realness for the operator console.
ENV_REALNESS = {
    "local-untrusted": "real (no trust claim)",
    "stitch-runner": "REAL remote exec on polarisserver (kissat, host-measured; CATHEDRAL_ARENA_STITCH=1)",
    "polaris-tee-cpu": "real seam, offline-stub quote (real binding recipe)",
    "polaris-tee-gpu": "real seam, offline-stub quote (gated; one bounded live test only)",
    "mocked-tee": "MOCKED — clearly labeled, never scores as trusted",
}


@dataclass
class Submission:
    spec: AgentSpec
    mission: Mission
    cid: str                 # the agent's own issued challenge id
    issued_cnf_hash: str
    issued_nonce: str
    submitted_cid: str
    submitted_nonce: str
    submitted_cnf_hash: str
    assignment: list[int]
    wall_ms: float
    tier: int
    seq: int
    replay_witness: dict | None = None     # decoded inputs for the money-math replay
    replay_outcome: object | None = None   # ReplayOutcome (set during gating, for UI)
    claimed_family: str = ""               # invariant family the agent COMMITS for its proof


@dataclass
class AgentResult:
    run: AgentRun
    gates: GateOutcome
    credit: SolveCredit
    mission: Mission
    receipt: object | None = None      # the full signed Receipt (for proof bundles)


@dataclass
class ArenaResult:
    season: str
    round_no: int
    epoch: int
    targets: list[Target]
    target_state: dict[int, TargetState]
    agents: list[AgentResult]
    weights: dict
    signed_vector: dict
    proof_feed: list[dict]
    anticheat_feed: list[dict]
    operator_console: dict
    corpus_summary: dict
    emissions: dict = field(default_factory=dict)        # hotkey -> tau this round
    ranks: dict = field(default_factory=dict)            # hotkey -> rank label
    breaks: list = field(default_factory=list)           # breach kill-feed
    chain_vaults: list = field(default_factory=list)     # money-math/chain CNF vaults
    replay_theater: list = field(default_factory=list)   # real money-math replays
    season_pool: float = 0.0
    season_board: list = field(default_factory=list)      # cumulative season leaderboard
    season_rounds: int = 0
    season_targets: dict = field(default_factory=dict)    # netuid -> cumulative status
    season_conquered: int = 0                             # subnets broken this season
    solver_bench: list = field(default_factory=list)      # PAR-2 solver benchmark cards
    sybil_panel: list = field(default_factory=list)       # hotkey-stacking collapse evidence
    anchor: dict = field(default_factory=dict)            # Merkle round commitment
    real_audit_vault: list = field(default_factory=list)  # invariants proven on REAL audit CNFs


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _stitch_enabled() -> bool:
    import os
    return os.environ.get("CATHEDRAL_ARENA_STITCH", "").strip().lower() in {"1", "true", "yes", "on"}


def _hyp_policy() -> str:
    from .hypothesis import LLM_MODEL, llm_available
    return f"LLM:{LLM_MODEL}" if llm_available() else "deterministic-rulebook"


def _real_audit_vault(stitch_status: dict, remote_sat: dict, minted: dict) -> list[dict]:
    """Headline cards: which subtensor invariants have been settled on REAL audit
    CNFs, with the actual solver evidence. CRACKED = an exploit input exists (SAT);
    HARDENED = no exploit exists (UNSAT, two solvers agree). 'real_cnf=True' means
    a pre-existing audit artifact on Stitch (not a freshly-minted one)."""
    vault: list[dict] = []
    # the hardened conservation invariant — real CNF, kissat(Stitch) + local CDCL both UNSAT
    if stitch_status.get("available") and stitch_status.get("real_cnf"):
        vault.append({
            "verdict": "HARDENED", "family": "A_conservation",
            "invariant": "AMM fee/delta conservation (fee+delta==amount, fee<=amount)",
            "real_cnf": True, "cnf": stitch_status.get("real_cnf"),
            "evidence": (f"kissat@{stitch_status.get('host')} "
                         f"{stitch_status.get('remote_wall_ms')}ms UNSAT + "
                         f"{stitch_status.get('local_solver')} local UNSAT (agree)"),
            "cross_confirmed": bool(stitch_status.get("cross_solver_agree")),
            "code_sha256": stitch_status.get("cnf_sha256")})
    # the exploitable recalc-overcharge invariant — real 20MB CNF SAT on Stitch
    if remote_sat.get("available") and remote_sat.get("violable"):
        vault.append({
            "verdict": "CRACKED", "family": "I_safety",
            "invariant": "recalc denominator collapse (delta_in+recalc_fee must stay <= amount)",
            "real_cnf": True, "cnf": remote_sat.get("real_cnf"),
            "evidence": (f"kissat@{remote_sat.get('host')} SAT {remote_sat.get('remote_wall_ms')}ms, "
                         f"{remote_sat.get('n_lits')}-lit witness (model {(remote_sat.get('model_sha256') or '')[:12]})"),
            "cross_confirmed": bool(remote_sat.get("cross_confirmed")),
            "code_sha256": remote_sat.get("model_sha256")})
    # locally z3-minted families that solve==replay (corroborating, not real-CNF)
    for m in (minted.get("sat_minted") or []):
        vault.append({
            "verdict": "CRACKED", "family": m.get("family"),
            "invariant": m.get("target_id"), "real_cnf": False, "cnf": m.get("target_id"),
            "evidence": "z3-minted → CDCL solve (verified) → harness reproduces",
            "cross_confirmed": bool(m.get("reproduced")), "code_sha256": m.get("code_sha256")})
    # z3-minted HARDENED invariants (no exploit exists — z3 + CDCL both UNSAT),
    # spanning two pinned models. Skip the AMM A4 when the real-CNF Stitch card
    # already covers it (no duplicate); the root-staking proofs are new.
    have_real_conservation = any(
        c["verdict"] == "HARDENED" and c["real_cnf"] for c in vault)
    for h in (minted.get("hardened") or []):
        if (h.get("model") == "subtensor-amm"
                and h.get("rule_id") == "A4-fee-split-conservation"
                and have_real_conservation):
            continue
        vault.append({
            "verdict": "HARDENED", "family": h.get("family"),
            "invariant": h.get("invariant"), "real_cnf": False,
            "cnf": f"{h.get('model')}:{h.get('rule_id')}",
            "evidence": f"z3 unsat + {('CDCL unsat' if h.get('cdcl_unsat') else 'unconfirmed')} "
                        f"({h.get('model')})",
            "cross_confirmed": bool(h.get("hardened")), "code_sha256": h.get("cnf_sha256")})
    return vault


def _wrong_family(declared: str) -> str:
    """A rulebook family deterministically DIFFERENT from `declared` — used to
    forge a mislabeled finding (a misclassify cheat) for the alignment gate."""
    from .hypothesis import RULEBOOK
    for fam in sorted(RULEBOOK):
        if fam != declared:
            return fam
    return declared


class ArenaEngine:
    def __init__(self, roster: list[AgentSpec] | None = None,
                 base_epoch: int = config.GAME_EPOCH, season: str = "S1"):
        self.roster = roster if roster is not None else default_roster()
        self.base_epoch = base_epoch
        self.season = season
        self.targets = corpus.load_targets()
        self.polaris = PolarisClient(live=False)
        self._known_hotkeys = {a.hotkey for a in self.roster}
        self._now_tick = 0                           # advances per round (nonce clock)
        self._bench = None                           # PAR-2 solver bench (computed once)
        self._ledger: set[tuple[str, str]] = set()   # (hotkey, cid) season replay ledger
        self._seen_receipts: set[str] = set()        # receipt-id replay set (provenance)
        # each agent owns its on-host Ed25519 identity (deterministic per hotkey).
        self._keys = {a.hotkey: AgentKey(seed=a.hotkey.encode()) for a in self.roster}
        # the registered DELEGATION: hotkey -> authorized agent pubkey. A real
        # submission must be signed by the key the hotkey delegated to (identity
        # binding). An impostor key signs a valid receipt but fails this gate.
        self._delegations = {hk: k.pub_hex for hk, k in self._keys.items()}

    # -- the agent's reasoning policy (deterministic by default; LLM if keyed) -
    def _hypothesis(self, target: Target) -> dict:
        """Form the exploit hypothesis for a target. Uses the gated Claude policy
        when an ANTHROPIC_API_KEY is present, else the deterministic rulebook —
        so the default run never spends. Memoized per netuid within an engine
        instance (one reasoning call per target, not per round)."""
        cache = getattr(self, "_hyp_cache", None)
        if cache is None:
            cache = self._hyp_cache = {}
        if target.netuid not in cache:
            from .hypothesis import form_hypothesis_best
            cache[target.netuid] = form_hypothesis_best(target)
        return cache[target.netuid]

    # -- reasoning drives the proof: pick the invariant family to prove --------
    def _repro_index(self) -> dict[str, list[str]]:
        """reproducing replay targets grouped by invariant family (memoized)."""
        idx = getattr(self, "_repro_by_fam", None)
        if idx is None:
            idx = {}
            for tid in REPRODUCING_TARGETS:
                t = replay.TARGETS.get(tid)
                if t is not None:
                    idx.setdefault(t.family, []).append(tid)
            self._repro_by_fam = idx
        return idx

    def _select_proof_target(self, idx: int, target: Target) -> str:
        """Choose WHICH invariant the agent proves from the family it REASONED for
        this subnet (hypothesis.py). When a reproducing invariant of that family
        exists the agent proves THAT family (coherent: reasoned G_scoring -> prove
        a G_scoring invariant); else it falls back to the rotation (e.g. an
        A_conservation finding has no reproducing exploit — the invariant holds)."""
        fam = self._hypothesis(target)["family"]
        pool = self._repro_index().get(fam)
        if pool:
            return pool[idx % len(pool)]
        return REPRODUCING_TARGETS[idx % len(REPRODUCING_TARGETS)]

    # -- mission assignment ---------------------------------------------------
    def _assign(self, idx: int, spec: AgentSpec, epoch: int) -> Mission:
        target = self.targets[idx % len(self.targets)]
        tier = 2 if idx % 2 == 0 else 1               # alternate floor / premium
        nonce = _sha(("nonce", self.season, epoch, spec.hotkey, idx))[:32]
        # a stale-replay agent is handed a nonce whose validity window has closed.
        issued_tick = (self._now_tick - NONCE_TTL_TICKS - 4
                       if spec.archetype == "stale_nonce" else self._now_tick)
        return Mission(
            mission_id=_sha(("mission", self.season, epoch, spec.agent_id, target.netuid))[:16],
            target=target,
            objective=f"Prove the encoded invariant for {target.name} "
                      f"({target.candidate_title[:60]})",
            proof_kind="encoded_invariant",
            attestation_required=(tier == 2),
            nonce=nonce, tier=tier, bounty=(target.severity >= 7 and tier == 2),
            replay_target_id=self._select_proof_target(idx, target),
            nonce_issued_tick=issued_tick,
        )

    # -- the agent operates: produce a trace + a submission -------------------
    def _operate(self, idx: int, spec: AgentSpec, mission: Mission,
                 epoch: int, honest_witnesses: dict[str, tuple[str, str, list[int]]]) -> Submission:
        tier, seq = mission.tier, 0
        cid, cnf, _planted = PM.generate_instance(spec.hotkey, epoch, tier, seq)
        cnf_hash = hashlib.sha256(cnf.encode()).hexdigest()

        # REAL remote execution: a stitch-runner agent's solve runs on Stitch
        # (polarisserver) with kissat, host-measured — gated by CATHEDRAL_ARENA_STITCH.
        remote = None
        if spec.environment == "stitch-runner" and _stitch_enabled():
            from . import stitch
            if stitch.stitch_available():
                res = stitch.run_on_stitch(cnf, solver="kissat")
                if res.get("ok"):
                    remote = res
        if remote is not None:
            own_solution = remote["assignment"]
            wall = remote["wall_ms"]                 # remote-host-measured
        else:
            t0 = time.perf_counter()
            own_solution = solve_cnf(cnf) or []
            wall = (time.perf_counter() - t0) * 1000.0
            # environment-flavored effort so the speed term separates the field.
            wall += {"polaris-tee-gpu": 5.0, "polaris-tee-cpu": 20.0, "stitch-runner": 40.0,
                     "local-untrusted": 80.0, "mocked-tee": 30.0}.get(spec.environment, 50.0)

        sub = dict(submitted_cid=cid, submitted_nonce=mission.nonce,
                   submitted_cnf_hash=cnf_hash, assignment=own_solution)

        if spec.cheat == "copy_witness":
            # steal the first honest agent's witness for ITS cnf; worthless here.
            victim = honest_witnesses.get("victim")
            if victim:
                sub["assignment"] = victim[2]
        elif spec.cheat == "wrong_owner":
            # submit a DIFFERENT agent's challenge id + their valid witness.
            victim = honest_witnesses.get("victim")
            if victim:
                sub["submitted_cid"] = victim[0]
                sub["assignment"] = victim[2]
        elif spec.cheat == "spam":
            # a duplicate replayed submission — seed the ledger so it collides.
            self._ledger.add((spec.hotkey, cid))
        elif spec.cheat == "bad_encode":
            # an unsound encoding: the submitted CNF hash does not match the
            # issued task (the witness was found against a tampered formula).
            sub["submitted_cnf_hash"] = hashlib.sha256((cnf + "tampered").encode()).hexdigest()
        # fake_attest: solves correctly but its mocked-tee env cannot attest.

        # the decoded witness for the REAL money-math replay. Honest agents submit
        # the reproducing inputs; bad_replay submits a non-reproducing (zero) witness.
        rtgt = replay.TARGETS.get(mission.replay_target_id)
        if spec.cheat == "no_decode_map":
            replay_witness = {}                       # omit the decode map entirely
        elif spec.cheat == "bad_replay" and rtgt:
            replay_witness = {k: 0 for k in rtgt.decode}
        elif rtgt:
            replay_witness = dict(rtgt.known_witness)
        else:
            replay_witness = None

        # the agent COMMITS the invariant family it claims for its proof. An honest
        # agent correctly classifies the invariant it is proving (the replay
        # target's true family); a misclassifier commits a WRONG family.
        declared = rtgt.family if rtgt else ""
        claimed = _wrong_family(declared) if spec.cheat == "misclassify" else declared

        return Submission(
            spec=spec, mission=mission, cid=cid, issued_cnf_hash=cnf_hash,
            issued_nonce=mission.nonce, submitted_cid=sub["submitted_cid"],
            submitted_nonce=sub["submitted_nonce"], submitted_cnf_hash=sub["submitted_cnf_hash"],
            assignment=sub["assignment"], wall_ms=wall, tier=tier, seq=seq,
            replay_witness=replay_witness, claimed_family=claimed)

    # -- the ten gates --------------------------------------------------------
    def _gates(self, s: Submission, epoch: int) -> GateOutcome:
        g = GateOutcome()
        spec, m = s.spec, s.mission

        g.valid_identity = spec.hotkey in self._known_hotkeys
        if not g.valid_identity:
            g.reasons.append("unknown_hotkey")

        g.assigned_mission = bool(m.mission_id)        # acted on its assigned mission
        if not g.assigned_mission:
            g.reasons.append("no_assigned_mission")

        # fresh_nonce: the nonce must match the issued one AND still be inside its
        # validity window (TTL) — a nonce from a closed round is a stale replay.
        nonce_matches = (s.submitted_nonce == s.issued_nonce)
        within_ttl = (self._now_tick - m.nonce_issued_tick) <= NONCE_TTL_TICKS
        g.fresh_nonce = nonce_matches and within_ttl
        if not nonce_matches:
            g.reasons.append("forged_nonce")
        elif not within_ttl:
            g.reasons.append("nonce_expired_stale_replay")

        g.no_replay = (spec.hotkey, s.submitted_cid) not in self._ledger
        if not g.no_replay:
            g.reasons.append("duplicate_submission_replay")

        # ownership: the submitted cid must belong to THIS hotkey's assigned set.
        owner_ok, owner_reason = PM.verify_miner_submission(
            spec.hotkey, epoch, s.submitted_cid, s.assignment) if s.assignment else (False, "no_assignment")
        # verify_miner_submission folds owner+witness; split them for clearer gates.
        recovered = PM.recover_tier_seq_for(spec.hotkey, epoch, s.submitted_cid)
        g.correct_owner = recovered is not None
        if not g.correct_owner:
            g.reasons.append("challenge_id_not_in_miner_set")

        g.complete_artifact = bool(s.assignment) and bool(s.submitted_cnf_hash) and bool(s.submitted_nonce)
        if not g.complete_artifact:
            g.reasons.append("incomplete_artifact")

        g.cnf_hash_matches = (s.submitted_cnf_hash == s.issued_cnf_hash)
        if not g.cnf_hash_matches:
            g.reasons.append("cnf_hash_mismatch_vs_manifest")

        # witness verification against the agent's OWN issued cnf (anti-copy).
        if g.correct_owner and recovered:
            tier, seq = recovered
            wok, wreason = PM.verify_miner_submission_for(
                spec.hotkey, epoch, tier, seq, s.submitted_cid, s.assignment)
            g.witness_verifies = wok
            if not wok:
                g.reasons.append(wreason or "witness_check_failed")
        else:
            g.witness_verifies = False
            if "challenge_id_not_in_miner_set" not in g.reasons:
                g.reasons.append("witness_unverifiable")

        # decode map: the proof must carry the witness decode map (the inputs the
        # replay harness consumes). A submission missing it cannot be replayed.
        rtgt0 = replay.TARGETS.get(m.replay_target_id)
        if rtgt0 is None:
            g.decode_map_present = True
        else:
            g.decode_map_present = bool(s.replay_witness) and all(
                k in (s.replay_witness or {}) for k in rtgt0.decode)
        if not g.decode_map_present:
            g.reasons.append("missing_decode_map")

        # replay: REAL re-execution of the pinned subtensor money-math on the
        # submitted witness — reproduced iff the actual invariant is violated
        # (audit_lane harness/invariant pattern). Also requires the encoded task
        # to verify against the canonical manifest hash.
        ro = run_replay(m.replay_target_id, s.replay_witness)
        s.replay_outcome = ro
        g.replay_succeeds = g.witness_verifies and g.cnf_hash_matches and ro.reproduced
        if not ro.reproduced:
            g.reasons.append(ro.reason or "replay_did_not_reproduce")
        elif not g.replay_succeeds and g.witness_verifies:
            g.reasons.append("replay_failed_unsound_encoding")

        # fake compute profile: the env an agent ADVERTISES must match what it can
        # actually attest. Claiming a TEE profile it can't back is rejected.
        claimed = spec.claimed_env or spec.environment
        claims_trusted = claimed in TRUSTED_ENVS
        can_attest = spec.provisioned and spec.environment in TRUSTED_ENVS
        g.compute_profile_honest = (not claims_trusted) or can_attest
        if not g.compute_profile_honest:
            g.reasons.append("fake_compute_profile")

        # attestation: trusted compute as a boolean gate.
        if not m.attestation_required:
            g.attestation_valid = True
        else:
            # bind the quote to THIS exact proof: the attestation nonce commits to
            # the submitted (cid, cnf hash, nonce), so a quote cannot be replayed
            # against a different submission.
            nonce = "attest:" + hashlib.sha256(
                f"{s.submitted_cid}|{s.submitted_cnf_hash}|{s.submitted_nonce}".encode()).hexdigest()
            image = (f"sha256:agent-{spec.agent_id}" if spec.provisioned
                     and spec.environment in TRUSTED_ENVS else "sha256:unattestable")
            pubkey = (hashlib.sha256(spec.hotkey.encode()).hexdigest()
                      if spec.provisioned and spec.environment in TRUSTED_ENVS else "")
            ok, _res = verify_attestation(
                self.polaris, nonce=nonce, pubkey_b64=pubkey, expected_image=image,
                workload=f"solve:{s.cid}", measured_elapsed_ms=s.wall_ms)
            g.attestation_valid = ok
            if not ok:
                g.reasons.append("attestation_invalid_or_untrusted_env")

        # hypothesis alignment: the invariant family the agent COMMITTED for its
        # proof must match the family of the invariant it actually reproduced. A
        # mislabeled finding (claiming an emission bug while proving a bounds bug)
        # is rejected — the agent must correctly classify its own proof. Empty
        # claim (legacy/submitted envelope) or unknown target -> not penalized.
        declared = replay.TARGETS.get(m.replay_target_id)
        if declared is None or not s.claimed_family:
            g.hypothesis_aligned = True
        else:
            g.hypothesis_aligned = (s.claimed_family == declared.family)
            if not g.hypothesis_aligned:
                g.reasons.append(
                    f"misclassified_invariant:claimed_{s.claimed_family}!={declared.family}")
        return g

    # -- the agent assembles + signs its receipt (real submission) ------------
    def _submit_receipt(self, spec: AgentSpec, m: Mission, s: Submission,
                        artifact: dict, attested: dict | None,
                        hyp: dict | None = None) -> Receipt:
        key = self._keys[spec.hotkey]
        # the agent INVOKES real tools (tools.py): fetch → inspect → REASON
        # (hypothesis) → encode (z3 mint) → solve (Glucose) → decode → submit.
        # raw_steps = the REAL tool I/O trace, so provenance binds to real work
        # (including the reasoning step), not labels.
        from .tools import run_workflow
        trace, _witness = run_workflow(
            netuid=m.target.netuid, name=m.target.name, repo=m.target.repo,
            location=m.target.location, candidate=m.target.candidate_title,
            replay_target_id=m.replay_target_id, artifact=artifact, hypothesis=hyp)
        raw_steps = [tc.as_step() for tc in trace]
        receipt = build_receipt(
            key, agent_id=spec.agent_id, miner_hotkey=spec.hotkey, mission_id=m.mission_id,
            nonce=s.submitted_nonce, environment=spec.environment, raw_steps=raw_steps,
            artifact=artifact, attestation=attested)
        if spec.cheat == "forge_trace":
            # tamper a step AFTER signing — breaks the chain + invalidates the sig.
            receipt.steps[2].output_digest = "forged_" + receipt.steps[2].output_digest[:12]
        return receipt

    def _provenance(self, spec: AgentSpec, m: Mission, s: Submission, artifact: dict,
                    receipt: Receipt, g: GateOutcome) -> None:
        v = verify_receipt(
            receipt, expected_hotkey=spec.hotkey, expected_mission=m.mission_id,
            expected_nonce=s.submitted_nonce, expected_artifact=artifact,
            replay_ok=g.replay_succeeds, attestation_required=m.attestation_required,
            seen_receipts=self._seen_receipts)
        g.agent_signature_valid = v.signature_ok
        g.provenance_chain_intact = v.chain_intact and v.head_binds_artifact and v.not_replayed_receipt
        g.provenance_grade = v.grade
        for r in v.reasons:
            if r not in g.reasons:
                g.reasons.append(r)

    # -- run one round --------------------------------------------------------
    def run(self, round_no: int = 1) -> ArenaResult:
        epoch = self.base_epoch + round_no - 1
        self._now_tick = round_no
        missions = [self._assign(i, a, epoch) for i, a in enumerate(self.roster)]

        # the honest "victim" whose witness the copier/wrong-owner steal.
        victim_spec = next(a for a in self.roster if a.archetype == "honest")
        vmission = missions[self.roster.index(victim_spec)]
        vcid, vcnf, _ = PM.generate_instance(victim_spec.hotkey, epoch, vmission.tier, 0)
        honest_witnesses = {"victim": (vcid, vcnf, solve_cnf(vcnf) or [])}

        results: list[AgentResult] = []
        tstate: dict[int, TargetState] = {t.netuid: TargetState(t.netuid) for t in self.targets}
        proof_feed: list[dict] = []
        anticheat_feed: list[dict] = []
        replay_theater: list[dict] = []

        for i, spec in enumerate(self.roster):
            m = missions[i]
            sub = self._operate(i, spec, m, epoch, honest_witnesses)
            gates = self._gates(sub, epoch)
            self._ledger.add((spec.hotkey, sub.submitted_cid))

            # the agent REASONS about the target first (which invariant family,
            # what to prove), then assembles + signs its run-receipt ON ITS OWN
            # HOST; the arena verifies the receipt, never trusts it.
            hyp = self._hypothesis(m.target)
            attested = ({"required": True, "valid": gates.attestation_valid,
                         "env": spec.environment, "realness": ENV_REALNESS.get(spec.environment)}
                        if m.attestation_required else None)
            artifact = {"challenge_id": sub.submitted_cid, "cnf_sha256": sub.submitted_cnf_hash,
                        "nonce": sub.submitted_nonce, "assignment_len": len(sub.assignment),
                        "proof_family": sub.claimed_family}    # the agent COMMITS its claim (signed)
            receipt = self._submit_receipt(spec, m, sub, artifact, attested, hyp)
            self._provenance(spec, m, sub, artifact, receipt, gates)
            self._seen_receipts.add(_sha(receipt.body()) + receipt.sig)
            self._record_agent(spec, m, sub, receipt, gates, attested,
                               results, tstate, proof_feed, anticheat_feed, replay_theater,
                               hyp=hyp)

        return self._assemble(round_no, epoch, results, tstate,
                              proof_feed, anticheat_feed, replay_theater)

    # -- seasons: run N rounds, accrue cumulative history ---------------------
    def run_season(self, rounds: int = 3, *, submitted: bool = False,
                   state_path: str | None = None):
        """Run a multi-round season; accumulate a persistent leaderboard. Returns
        (last ArenaResult with season_board attached, SeasonState)."""
        from .season import SeasonState
        state = SeasonState.load(state_path) if state_path else SeasonState()
        runner = self.run_submitted if submitted else self.run
        last = None
        for r in range(1, rounds + 1):
            last = runner(r)
            state.update(last)
        if state_path:
            state.save(state_path)
        if last is not None:
            last.season_rounds = state.rounds
            last.season_board = [{
                "agent_id": s.agent_id, "hotkey": s.hotkey, "emissions": s.total_emissions,
                "breaches": s.breaches, "streak": s.streak, "best_streak": s.best_streak,
                "rounds": s.rounds_played, "rank": s.rank,
            } for s in state.leaderboard()]
            last.season_targets = {n: {"status": t.status, "breaches": t.breaches,
                                       "first_broken_round": t.first_broken_round}
                                   for n, t in state.targets.items()}
            last.season_conquered = state.conquered()
        return last, state

    # -- REAL external-agent submission path ----------------------------------
    def _build_packet(self, i: int, spec: AgentSpec, m: Mission, epoch: int,
                      victim: dict) -> dict:
        cid, cnf, _ = PM.generate_instance(spec.hotkey, epoch, m.tier, 0)
        return {
            "behavior": {"honest": "honest", "copier": "copy_witness",
                         "wrong_owner": "wrong_owner", "bad_encoder": "bad_encode",
                         "forge_trace": "forge_trace", "bad_replay": "bad_replay",
                         "impostor": "impostor", "no_decode_map": "no_decode_map",
                         "misclassify": "misclassify",
                         "fake_attest": "honest", "spam": "honest",
                         "stale_nonce": "honest", "fake_compute_profile": "honest",
                         "hotkey_stacking": "honest"}.get(spec.archetype, "honest"),
            "hypothesis": self._hypothesis(m.target),   # the agent RECORDS the reasoning step
            "agent_id": spec.agent_id, "hotkey": spec.hotkey, "environment": spec.environment,
            "mission_id": m.mission_id, "target_netuid": m.target.netuid,
            "target_name": m.target.name,
            "target_repo": m.target.repo, "target_location": m.target.location,
            "target_title": m.target.candidate_title, "target_family": m.target.family,
            "cid": cid, "cnf": cnf, "cnf_sha256": hashlib.sha256(cnf.encode()).hexdigest(),
            "nonce": m.nonce, "tier": m.tier, "attestation_required": m.attestation_required,
            "replay_target_id": m.replay_target_id,
            "env_effort_ms": {"polaris-tee-gpu": 5.0, "polaris-tee-cpu": 20.0,
                              "stitch-runner": 40.0, "local-untrusted": 80.0,
                              "mocked-tee": 30.0}.get(spec.environment, 50.0),
            "victim": victim,
        }

    def _spawn_agent(self, packet: dict) -> dict:
        """Run the agent as a REAL separate OS process; ingest its signed envelope."""
        proc = subprocess.run(
            [sys.executable, "-m", "game.arena.agent_cli"],
            input=json.dumps(packet), capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[2]), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"agent process failed: {proc.stderr[:300]}")
        return json.loads(proc.stdout)

    def _ingest(self, envelope: dict, spec: AgentSpec, m: Mission,
                epoch: int) -> tuple[Submission, Receipt]:
        sub_d = envelope["submission"]
        receipt = receipt_from_dict(envelope["receipt"])
        issued_cid, issued_cnf, _ = PM.generate_instance(spec.hotkey, epoch, m.tier, 0)
        sub = Submission(
            spec=spec, mission=m, cid=issued_cid,
            issued_cnf_hash=hashlib.sha256(issued_cnf.encode()).hexdigest(),
            issued_nonce=m.nonce, submitted_cid=sub_d["submitted_cid"],
            submitted_nonce=sub_d["submitted_nonce"], submitted_cnf_hash=sub_d["submitted_cnf_hash"],
            assignment=sub_d["assignment"], wall_ms=sub_d["wall_ms"], tier=m.tier, seq=0,
            replay_witness=sub_d.get("replay_witness"),
            # the family the external agent COMMITTED in its signed artifact -> the
            # hypothesis_aligned gate now applies to the real-process path too.
            claimed_family=receipt.artifact.get("proof_family", ""))
        return sub, receipt

    def run_submitted(self, round_no: int = 1) -> ArenaResult:
        """Like run(), but agents are REAL separate processes that sign their own
        receipts; the arena only verifies the submitted envelopes (verify-by-
        receipt) + checks the hotkey->agent-key delegation. Cheating happens in
        the agent process and is caught here."""
        epoch = self.base_epoch + round_no - 1
        self._now_tick = round_no
        missions = [self._assign(i, a, epoch) for i, a in enumerate(self.roster)]
        victim_spec = next(a for a in self.roster if a.archetype == "honest")
        vmission = missions[self.roster.index(victim_spec)]
        vcid, vcnf, _ = PM.generate_instance(victim_spec.hotkey, epoch, vmission.tier, 0)
        victim = {"cid": vcid, "assignment": solve_cnf(vcnf) or []}

        results: list[AgentResult] = []
        tstate = {t.netuid: TargetState(t.netuid) for t in self.targets}
        proof_feed, anticheat_feed, replay_theater = [], [], []

        for i, spec in enumerate(self.roster):
            m = missions[i]
            envelope = self._spawn_agent(self._build_packet(i, spec, m, epoch, victim))
            sub, receipt = self._ingest(envelope, spec, m, epoch)
            if spec.archetype == "spam":           # replay/dup is detected arena-side
                self._ledger.add((spec.hotkey, sub.submitted_cid))
            gates = self._gates(sub, epoch)
            artifact = dict(receipt.artifact)
            self._provenance(spec, m, sub, artifact, receipt, gates)

            # IDENTITY DELEGATION: the receipt must be signed by the key the
            # hotkey delegated to. An impostor/unregistered key is rejected.
            delegated = receipt.agent_pubkey == self._delegations.get(spec.hotkey)
            if not delegated:
                gates.valid_identity = False
                gates.reasons.append("agent_key_not_delegated_to_hotkey")

            self._ledger.add((spec.hotkey, sub.submitted_cid))
            self._seen_receipts.add(_sha(receipt.body()) + receipt.sig)
            attested = ({"required": True, "valid": gates.attestation_valid,
                         "env": spec.environment} if m.attestation_required else None)
            self._record_agent(spec, m, sub, receipt, gates, attested,
                               results, tstate, proof_feed, anticheat_feed, replay_theater,
                               hyp=self._hypothesis(m.target))

        return self._assemble(round_no, epoch, results, tstate,
                              proof_feed, anticheat_feed, replay_theater)

    # -- shared: record one graded agent into the result accumulators ---------
    def _record_agent(self, spec, m, sub, receipt, gates, attested,
                      results, tstate, proof_feed, anticheat_feed, replay_theater,
                      hyp=None) -> None:
        tier_w = PM.weight_for(sub.tier) * (1.3 if m.bounty else 1.0)
        spd = speed_bonus(sub.wall_ms, config.TIER_REFERENCE_MS.get(sub.tier, 500.0))
        passed = gates.passed()
        credit = SolveCredit(
            hotkey=spec.hotkey, challenge_id=sub.submitted_cid, tier=sub.tier,
            verified=passed, attest_gate=True, tier_weight=tier_w, speed=spd,
            reason="" if passed else (gates.first_failure() or "gated"))

        hyp = hyp or self._hypothesis(m.target)
        run = AgentRun(
            agent_id=spec.agent_id, miner_hotkey=spec.hotkey, coldkey=spec.coldkey,
            environment=spec.environment, mission_id=m.mission_id,
            target_netuid=m.target.netuid,
            commands=[s.name for s in receipt.steps],
            files_inspected=[m.target.location] if m.target.location else [m.target.repo],
            hypothesis=hyp["rationale"],     # the agent's real reasoning, not a label
            encoder=f"{hyp['family']} ({hyp['source']}): {hyp['rule']}",
            artifact=dict(receipt.artifact),
            replay_result={"succeeded": gates.replay_succeeds},
            attestation=attested)
        run.trace_sha256 = receipt.head    # the receipt head IS the trace digest

        results.append(AgentResult(run=run, gates=gates, credit=credit, mission=m,
                                   receipt=receipt))

        st = tstate[m.target.netuid]
        st.agents.append(spec.agent_id)
        if passed:
            st.status = "verified"
            st.verified_proofs += 1
        else:
            st.status = "rejected" if st.status == "untouched" else st.status
            st.rejected += 1

        proof_feed.append({"agent": spec.agent_id, "hotkey": spec.hotkey, "netuid": m.target.netuid,
                           "subnet": m.target.name, "cnf": sub.submitted_cid, "tier": sub.tier,
                           "env": spec.environment, "passed": passed,
                           "gate_fail": gates.first_failure(), "reasons": gates.reasons,
                           "attest_required": m.attestation_required, "prov_grade": gates.provenance_grade,
                           "attest_valid": gates.attestation_valid, "wall_ms": round(sub.wall_ms, 1),
                           "family": hyp["family"], "policy": hyp["source"],
                           "proof_family": (replay.TARGETS[m.replay_target_id].family
                                            if m.replay_target_id in replay.TARGETS else ""),
                           "reasoning_coherent": (m.replay_target_id in replay.TARGETS and
                                                  replay.TARGETS[m.replay_target_id].family == hyp["family"])})
        if not passed:
            anticheat_feed.append({"agent": spec.agent_id, "archetype": spec.archetype,
                                   "subnet": m.target.name, "rejected_by": gates.first_failure(),
                                   "reasons": gates.reasons})

        ro = sub.replay_outcome
        rt = replay.TARGETS.get(m.replay_target_id)
        if ro is not None and rt is not None:
            replay_theater.append({
                "agent": spec.agent_id, "target_id": rt.target_id, "cls": rt.cls,
                "property": rt.property_desc, "witness": sub.replay_witness,
                "observed": ro.observed, "reproduced": ro.reproduced,
                "reachable": rt.reachable, "severity": rt.severity, "reason": ro.reason,
                "source": rt.source, "code_sha256": rt.code_sha256})

    # -- shared: compose reward + economy + console into a result -------------
    def _assemble(self, round_no, epoch, results, tstate,
                  proof_feed, anticheat_feed, replay_theater) -> ArenaResult:
        # compose reward = metric x gate, Sybil-collapse, sign
        miners_meta = [(a.run.miner_hotkey, a.run.coldkey, a.run.agent_id,
                        a.gates.valid_identity) for a in results]
        composed = compose(miners_meta, [a.credit for a in results])
        signed = sign_vector(composed, policy_version=epoch * 100 + round_no)

        # anchor the whole round into one Merkle commitment (receipt heads +
        # signed vector) — an inclusion-provable, on-chain-ready proof object.
        from .anchor import build_anchor
        anchor = build_anchor(results, signed, season=self.season,
                              round_no=round_no, epoch=epoch)

        # hotkey stacking: a coldkey with >1 hotkey is collapsed to one identity's
        # fair share (the naive/no-collapse total would be ~k x — the stacking gain
        # the collapse removes).
        from ..reward import coldkey_totals, naive_weights
        ck_of = {hk: rr.coldkey for hk, rr in composed.items()}
        members: dict[str, list[str]] = {}
        for hk, ck in ck_of.items():
            members.setdefault(ck, []).append(hk)
        sybil_panel = []
        multi = {ck: hks for ck, hks in members.items() if len(hks) > 1}
        if multi:
            collapsed = {hk: rr.weight for hk, rr in composed.items()}
            naive = naive_weights(composed)
            ct_c = coldkey_totals(collapsed, ck_of)
            ct_n = coldkey_totals(naive, ck_of)
            for ck, hks in multi.items():
                sybil_panel.append({"coldkey": ck, "hotkeys": hks,
                                    "collapsed": round(ct_c[ck], 4), "naive": round(ct_n[ck], 4)})

        # -- emissions economy (the fun layer) --------------------------------
        weights = {hk: r.weight for hk, r in composed.items()}
        emissions: dict[str, float] = {}
        breaks: list[dict] = []
        for a in results:
            hk = a.run.miner_hotkey
            if not a.gates.passed():
                emissions[hk] = 0.0
                continue
            mult = economy.PROVENANCE_MULT.get(a.gates.provenance_grade, 0.0)
            base = weights.get(hk, 0.0) * economy.ROUND_EMISSION_POOL * mult
            bounty = economy.target_bounty(a.mission.target)   # broke the target -> drain bounty
            emissions[hk] = round(base + bounty, 1)
            breaks.append({"agent": a.run.agent_id, "subnet": a.mission.target.name,
                           "netuid": a.mission.target.netuid, "bounty": bounty,
                           "grade": a.gates.provenance_grade,
                           "emit": emissions[hk], "class": a.mission.target.target_class})
        breaks.sort(key=lambda b: -b["emit"])
        ranks = {hk: economy.rank_for(e) for hk, e in emissions.items()}

        # chain/contract vaults from the REAL money-math CNF corpus
        chain_vaults = []
        for pt in sorted(corpus.load_proof_tasks(), key=lambda p: -p.clauses)[:12]:
            status = {"sat": "CRACKED", "unsat": "HARDENED"}.get(pt.result, "OPEN BOUNTY")
            chain_vaults.append({
                "cnf_id": pt.cnf_id, "model": pt.model, "tier": pt.tier, "result": pt.result,
                "status": status, "bounty": economy.chain_vault_bounty(pt.tier, pt.result),
                "invariant": pt.invariant[:70], "vars": pt.vars})

        from .attestation import intel_backend, live_status, round_attest_readiness
        from .stitch import (stitch_status, attest_readiness, remote_sat_status,
                             inventory_status)
        from .mint import minted_proof_status, external_decode_status
        _out = Path(__file__).resolve().parent / "out"
        _live_att = live_status(_out / "real_attest_receipt.json")
        _stitch = stitch_status(_out / "stitch_runner_receipt.json")
        _stitch_attest = attest_readiness(_out / "stitch_runner_receipt.json",
                                          quote_path=_out / "stitch_attest_quote.json")
        _remote_sat = remote_sat_status(_out / "stitch_remote_sat_receipt.json")
        _inventory = inventory_status(_out / "stitch_inventory.json")
        _round_attest = round_attest_readiness(
            anchor.get("merkle_root", ""),
            quote_path=_out / "round_attest_quote.json",
            real_receipt_path=_out / "real_attest_receipt.json")
        _corpus_summary = corpus.corpus_summary()
        operator_console = {
            "live_attestation": _live_att,
            "round_attest": _round_attest,
            "stitch_runner": _stitch,
            "stitch_attest": _stitch_attest,
            "remote_sat": _remote_sat,
            "stitch_inventory": _inventory,
            "minted_proof": minted_proof_status(),
            "external_decode": external_decode_status(),
            "minted_invariants": replay.minted_summary(),
            "execution_environments": ENV_REALNESS,
            "attestation": (
                "Attestation verifier path is wired through scaffold attest.py "
                f"(intel_backend={intel_backend()}). live_attestation reports whether "
                "out/real_attest_receipt.json is present and verifies."),
            "real": [f"{_corpus_summary['targets']} target corpus ({_corpus_summary.get('source', 'audit-hunter')})",
                     f"{_corpus_summary['proof_tasks']} proof tasks / CNFs",
                     "z3-MINTED invariants across MULTIPLE families AND TWO pinned models "
                     "(subtensor-amm + subtensor-root-reborn): B_bounds + I_safety solve==replay; "
                     "A_conservation (AMM fee + root TAO-split) + F_emission (no stranded holders) "
                     "proven HARDENED — z3 unsat + independent CDCL unsat, no exploit exists",
                     "deterministic anti-copy encoded task", "witness verification",
                     "REAL money-math replay (U64F64 fee math, audit_lane harness pattern)",
                     "REAL external agent processes signing their own receipts (--submitted)",
                     "hotkey->agent-key delegation (impostor keys rejected)",
                     "REAL DCAP attestation-verifier path (command-dcap, drop-in)",
                     "TTL nonces + stale-replay gate; attest nonce bound to the proof",
                     "hash-chained tamper-evident provenance", "Ed25519 signed weight vector",
                     "Merkle-anchored round commitment",
                     "portable proof bundle — verifiable end-to-end with NO engine",
                     "Stitch remote solve receipt slot: see out/stitch_runner_receipt.json when available",
                     "Stitch inventory receipt slot: see out/stitch_inventory.json when available",
                     "OFF-BOX decode: z3 mints the CNF + a real bit→var decode map; an EXTERNAL CDCL "
                     "solver solves it and its raw assignment is decoded to the exploit input WITHOUT "
                     "re-running z3 (the decoded input reproduces via the real harness)",
                     "agents run REAL tools (fetch→inspect→REASON→z3 encode→Glucose solve→decode→submit)",
                     "agents form a REAL exploit hypothesis (invariant taxonomy rulebook); "
                     "gated Claude Opus 4.8 policy is a tested drop-in (deterministic by default, "
                     f"LLM-keyed if ANTHROPIC_API_KEY present — currently {_hyp_policy()})",
                     "REASONING IS LOAD-BEARING: the agent commits the invariant family of its "
                     "proof in its SIGNED artifact; the hypothesis_aligned gate rejects a "
                     "mislabeled finding (12th anti-cheat axis)"],
            "mocked": ["mocked-tee env (labeled)",
                       "the tool SEQUENCE is fixed (the 6-step workflow); the reasoning "
                       "(which invariant family) + the tools THEMSELVES are real"],
            "safe": ["local replay only", "no mainnet writes", "no live SN39 validator contact"],
            "risky_todo": ["one bounded Polaris TEE-GPU live attest test (cost-checked first) — "
                           "binding is READY (stitch_attest.commitment); only the live quote fetch "
                           "is gated on Fred's go (no spend until then)",
                           "real submitted agents over MCP"],
        }

        real_audit_vault = _real_audit_vault(_stitch, _remote_sat, replay.minted_summary())

        result = ArenaResult(
            season=self.season, round_no=round_no, epoch=epoch, targets=self.targets,
            target_state=tstate, agents=results, weights=weights,
            signed_vector=signed, proof_feed=proof_feed, anticheat_feed=anticheat_feed,
            operator_console=operator_console, corpus_summary=_corpus_summary,
            emissions=emissions, ranks=ranks, breaks=breaks, chain_vaults=chain_vaults,
            replay_theater=replay_theater, season_pool=economy.ROUND_EMISSION_POOL,
            solver_bench=self._solver_bench(), sybil_panel=sybil_panel, anchor=anchor,
            real_audit_vault=real_audit_vault)
        # the round SELF-AUDITS its scoring: reward = metric x gate, independently
        # re-checked over the engine's own output (proof, not assertion).
        from .audit import audit_scoring
        result.operator_console["scoring_audit"] = audit_scoring(result)
        return result

    def _solver_bench(self) -> list:
        if self._bench is None:
            from .solverbench import run_bench
            self._bench = run_bench()
        return self._bench
