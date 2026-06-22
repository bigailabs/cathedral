"""Weighted launch-readiness gate model for Cathedral v0.

This is intentionally separate from validator emissions. It answers a different
question: "Can we honestly launch across the product tiers yet?" A 100 score
requires every blocker to be backed by evidence, including real-world gates that
local tests cannot fake.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


REAL_VERIFIER_DIGESTS_ENV = "CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS"
FULL_PROFILE = "full"
CONTROLLED_V0_PROFILE = "controlled-v0"
NO_HARDWARE_V0_PROFILE = "no-hardware-v0"
_CONTROLLED_V0_DEFERRED_GATES = {
    "compute_real_verifier_tested",
    "compute_provider_listing_verified",
    "compute_health_and_revenue_verified",
}


@dataclass(frozen=True)
class LaunchGate:
    id: str
    tier: str
    points: float
    description: str
    evidence: str
    blocker: bool = True


_GATES: tuple[LaunchGate, ...] = (
    LaunchGate(
        "validator_default_safe",
        "T0 safety and validator continuity",
        5,
        "New lanes are default-off and cannot change live validator behavior without env flips.",
        "Env flags off by default; local smoke shows disabled routes return 404.",
    ),
    LaunchGate(
        "signed_weight_vector_verified",
        "T0 safety and validator continuity",
        5,
        "Validators consume one signed weight vector with monotonic policy version and burn snapshot.",
        "weights/publisher/vector tests verify signature, invariants, and rollback fence.",
    ),
    LaunchGate(
        "no_uncontrolled_spend_or_live_mutation",
        "T0 safety and validator continuity",
        5,
        "No launch smoke path rents Polaris compute, touches the live validator, or executes provider handoff by default.",
        "Runbook requires local-first checks and explicit operator execution env.",
    ),
    LaunchGate(
        "rollback_and_stop_conditions_written",
        "T0 safety and validator continuity",
        5,
        "Rollback and stop conditions are explicit enough for an operator to act quickly.",
        "Launch runbook and readiness checklist contain stop conditions.",
    ),
    LaunchGate(
        "proportional_tier_scoring_configured",
        "T1 incentives and scoring",
        4,
        "Proportional mode can weight every challenge tier by importance, not only tier 2.",
        "CATHEDRAL_WEIGHTS_TIER_WEIGHTS tested with tier 1/2/3 examples.",
    ),
    LaunchGate(
        "scoring_default_is_proportional",
        "T1 incentives and scoring",
        5,
        "Default scoring rewards proportional challenge volume with flat fallback only when the solve ledger is empty.",
        "CATHEDRAL_WEIGHTS_MODE defaults to proportional.",
    ),
    LaunchGate(
        "row_score_upgrade_path_verified",
        "T1 incentives and scoring",
        3,
        "Attested or replay-derived row scores have an explicit opt-in path into the signed vector.",
        "row_score_recent tests cover positive finite weighted_score aggregation.",
    ),
    LaunchGate(
        "sat_generator_refill_wired",
        "T1 incentives and scoring",
        4,
        "The live SAT board is wired to Cathedral's local refill generator and exposes the configured tier policy.",
        "active-challenges exposes generator metadata; refill.generator_config uses the same helpers as the mint loop.",
    ),
    LaunchGate(
        "non_emission_lanes_cannot_leak_into_weights",
        "T1 incentives and scoring",
        4,
        "Compute supply records cannot accidentally affect validator emissions.",
        "TEE GPU tests assert no writes to eval_runs, lane_challenge_solves, or per_miner_solves.",
    ),
    LaunchGate(
        "audit_witness_model_exists",
        "T2 SAT audit arena",
        4,
        "Audit tasks have a concrete package, witness, decode-map, and replay-result model.",
        "scaffold/lanes/audit_arena.py and audit_arena_verify.py.",
    ),
    LaunchGate(
        "audit_replay_deterministic",
        "T2 SAT audit arena",
        5,
        "Accepted audit claims are verified by deterministic replay, not miner narrative.",
        "Audit smoke rejects valid SAT witnesses when replay does not reproduce.",
    ),
    LaunchGate(
        "audit_decode_maps_bound",
        "T2 SAT audit arena",
        4,
        "Production decode maps bind replay-critical fields and reject canned static witnesses.",
        "Audit smoke rejects sparse/static production maps.",
    ),
    LaunchGate(
        "audit_live_exploitation_controlled",
        "T2 SAT audit arena",
        4,
        "The launch verifier avoids unauthorized third-party live exploitation.",
        "Docs require shadow replay, Cathedral-owned canaries, opt-in targets, or rule-compliant competition.",
    ),
    LaunchGate(
        "audit_severity_gate_defined",
        "T2 SAT audit arena",
        3,
        "Dust findings are separated from meaningful incentive/security issues.",
        "Readiness checklist requires severity/reachability gates before disclosure or earnings.",
    ),
    LaunchGate(
        "compute_signed_offer_and_consent",
        "T3 secure compute",
        3,
        "Miners must sign capacity offers and authorize operator use.",
        "TEE GPU route tests reject missing consent and bad signatures.",
    ),
    LaunchGate(
        "compute_exact_hardware_profiles",
        "T3 secure compute",
        2,
        "Accepted GPU profiles are narrow and tied to published TEE measurement shapes.",
        "Preflight only accepts current 8x TEE candidate profiles.",
    ),
    LaunchGate(
        "compute_gated_live_intake",
        "T3 secure compute",
        2,
        "Secure Compute public miner entry fails closed unless an allowlist or invite code is configured.",
        "Offer and evidence-request route tests reject unconfigured or missing intake codes.",
    ),
    LaunchGate(
        "compute_crypto_evidence_required",
        "T3 secure compute",
        4,
        "Production hardware asks require cryptographically verified TDX plus NVIDIA GPU evidence.",
        "CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE blocks operator_reviewed evidence.",
    ),
    LaunchGate(
        "compute_real_verifier_tested",
        "T3 secure compute",
        4,
        "A real verifier command has accepted a real machine and rejected bad evidence.",
        "Requires operator evidence from a real TEE GPU machine.",
    ),
    LaunchGate(
        "compute_provider_listing_verified",
        "T3 secure compute",
        3,
        "At least one verified machine was accepted by the provider listing flow.",
        "Requires a real provider-status receipt for a cryptographically verified machine.",
    ),
    LaunchGate(
        "compute_health_and_revenue_verified",
        "T3 secure compute",
        2,
        "Listed compute proves liveness and revenue or usage before miners are asked to scale.",
        "Requires real health-receipt plus usage-receipt evidence.",
    ),
    LaunchGate(
        "distillation_trace_schema_exists",
        "T4 distillation and operations",
        3,
        "Verified and rejected audit outcomes have a concrete trace shape.",
        "build_distillation_trace emits task, witness, supervision, and trace_hash.",
    ),
    LaunchGate(
        "distillation_private_by_default",
        "T4 distillation and operations",
        3,
        "Training traces are private by default.",
        "Audit trace tests assert private=true.",
    ),
    LaunchGate(
        "distillation_replay_label_source",
        "T4 distillation and operations",
        3,
        "Positive labels come from replay/verification, not unverified miner text.",
        "Trace supervision stores accepted/rejected replay result.",
    ),
    LaunchGate(
        "distillation_redaction_gate",
        "T4 distillation and operations",
        3,
        "Trace export has redaction and disclosure controls before public use.",
        "scaffold/distillation.py exports private traces by default and gates public export.",
    ),
    LaunchGate(
        "operator_docs_and_tests_current",
        "T4 distillation and operations",
        8,
        "A new operator can understand launch order, tests, risks, and rollback.",
        "CATHEDRAL_V0_LANES, LAUNCH_V0_RUNBOOK, and LAUNCH_READINESS_CHECKLIST.",
    ),
)


def gates() -> tuple[LaunchGate, ...]:
    return _GATES


def total_points() -> float:
    return round(sum(g.points for g in _GATES), 6)


def evaluate(evidence: dict[str, bool], *, profile: str = FULL_PROFILE) -> dict[str, Any]:
    """Score launch readiness from explicit gate evidence.

    Missing evidence is false. This keeps the model honest: a local smoke suite
    can prove local gates, but real-machine/provider/revenue gates remain open
    until real evidence is supplied.
    """
    profile = normal_profile(profile)
    deferred_gate_ids = _deferred_gate_ids(profile)
    by_tier: dict[str, dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    achieved_total = 0.0
    for gate in _GATES:
        ok = bool(evidence.get(gate.id, False))
        tier = by_tier.setdefault(gate.tier, {
            "points": 0.0,
            "achieved": 0.0,
            "gates": [],
        })
        tier["points"] += gate.points
        if ok:
            tier["achieved"] += gate.points
            achieved_total += gate.points
        elif gate.id in deferred_gate_ids:
            deferred.append({
                "id": gate.id,
                "tier": gate.tier,
                "points": gate.points,
                "description": gate.description,
                "needed_evidence": gate.evidence,
            })
        elif gate.blocker:
            blockers.append({
                "id": gate.id,
                "tier": gate.tier,
                "points": gate.points,
                "description": gate.description,
                "needed_evidence": gate.evidence,
            })
        tier["gates"].append({"id": gate.id, "ok": ok, "points": gate.points})

    total = total_points()
    for tier in by_tier.values():
        tier["points"] = round(tier["points"], 6)
        tier["achieved"] = round(tier["achieved"], 6)
        tier["percentage"] = round((tier["achieved"] / tier["points"]) * 100, 2)
    return {
        "profile": profile,
        "score": round(achieved_total, 6),
        "total": total,
        "percentage": round((achieved_total / total) * 100, 2),
        "ready": not blockers and (
            achieved_total == total or profile == CONTROLLED_V0_PROFILE
        ),
        "by_tier": by_tier,
        "blockers": blockers,
        "deferred": deferred,
    }


def tee_gpu_runtime_evidence(store: Any) -> dict[str, bool]:
    """Derive compute launch gates from publisher capacity evidence.

    This reads only the off-chain TEE GPU capacity tables. It does not mutate
    validator state and it does not treat local fixtures as real-world proof
    unless the operator points this at the real publisher DB.
    """
    from .publisher import tee_gpu

    rows = tee_gpu.list_capacity(store)
    launch_reports = [
        {
            "capacity_id": str(row["capacity_id"]),
            **tee_gpu.capacity_launch_evidence(store, row["capacity_id"]),
        }
        for row in rows
    ]
    success_events = _event_payloads(store, "attestation_verification_succeeded")
    failed_events = _event_payloads(store, "attestation_verification_failed")
    approved_digests = _approved_verifier_digests()
    approved_success_capacity_ids = {
        str(event.get("capacity_id") or "")
        for event in success_events
        if _event_verifier_digest(event) in approved_digests
    }
    trusted_launch_reports = [
        report
        for report in launch_reports
        if report.get("cryptographically_verified")
        and str(report.get("capacity_id") or "") in approved_success_capacity_ids
    ]
    verifier_accepted_machine = any(
        report.get("cryptographically_verified") for report in trusted_launch_reports
    )
    verifier_rejected_bad_evidence = any(
        bool(event.get("counts_as_bad_evidence_rejection"))
        and _event_verifier_digest(event) in approved_digests
        for event in failed_events
    )
    provider_listing_verified = any(
        report.get("provider_listing_verified") for report in trusted_launch_reports
    )
    health_and_revenue_verified = any(
        report.get("health_verified") and report.get("usage_or_revenue_verified")
        for report in trusted_launch_reports
    )
    return {
        "compute_real_verifier_tested": bool(
            verifier_accepted_machine and verifier_rejected_bad_evidence
        ),
        "compute_provider_listing_verified": bool(provider_listing_verified),
        "compute_health_and_revenue_verified": bool(health_and_revenue_verified),
    }


def evidence_from_store(store: Any) -> dict[str, bool]:
    evidence = local_scaffold_evidence()
    evidence.update(tee_gpu_runtime_evidence(store))
    return evidence


def evaluate_store(store: Any) -> dict[str, Any]:
    return evaluate(evidence_from_store(store))


def local_scaffold_evidence() -> dict[str, bool]:
    """Evidence that the current local scaffold can legitimately claim.

    Real-world gates are deliberately false here. They need proof from actual
    hardware/provider/revenue runs, not local fixtures.
    """
    return {
        "validator_default_safe": True,
        "signed_weight_vector_verified": True,
        "no_uncontrolled_spend_or_live_mutation": True,
        "rollback_and_stop_conditions_written": True,
        "proportional_tier_scoring_configured": True,
        "scoring_default_is_proportional": True,
        "row_score_upgrade_path_verified": True,
        "sat_generator_refill_wired": True,
        "non_emission_lanes_cannot_leak_into_weights": True,
        "audit_witness_model_exists": True,
        "audit_replay_deterministic": True,
        "audit_decode_maps_bound": True,
        "audit_live_exploitation_controlled": True,
        "audit_severity_gate_defined": True,
        "compute_signed_offer_and_consent": True,
        "compute_exact_hardware_profiles": True,
        "compute_gated_live_intake": True,
        "compute_crypto_evidence_required": True,
        "compute_real_verifier_tested": False,
        "compute_provider_listing_verified": False,
        "compute_health_and_revenue_verified": False,
        "distillation_trace_schema_exists": True,
        "distillation_private_by_default": True,
        "distillation_replay_label_source": True,
        "distillation_redaction_gate": True,
        "operator_docs_and_tests_current": True,
    }


def _event_payloads(store: Any, event_type: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT capacity_id, event_json FROM tee_gpu_capacity_events WHERE event_type=?",
        (event_type,),
    )
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["event_json"])
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            payload["capacity_id"] = str(row["capacity_id"])
            payloads.append(payload)
    return payloads


def _approved_verifier_digests() -> set[str]:
    raw = os.environ.get(REAL_VERIFIER_DIGESTS_ENV, "")
    return {
        item.strip().lower()
        for item in raw.replace(";", ",").split(",")
        if item.strip()
    }


def _event_verifier_digest(event: dict[str, Any]) -> str:
    return str(event.get("verifier_command_digest") or "").strip().lower()


def _normal_profile(profile: str) -> str:
    return normal_profile(profile)


def normal_profile(profile: str) -> str:
    value = str(profile or FULL_PROFILE).strip().lower().replace("_", "-")
    aliases = {
        "controlled",
        CONTROLLED_V0_PROFILE,
        "v0",
        "no-hardware",
        NO_HARDWARE_V0_PROFILE,
        "intake",
        "intake-v0",
    }
    if value in aliases:
        return CONTROLLED_V0_PROFILE
    return FULL_PROFILE


def _deferred_gate_ids(profile: str) -> set[str]:
    if profile == CONTROLLED_V0_PROFILE:
        return set(_CONTROLLED_V0_DEFERRED_GATES)
    return set()
