"""Deliverable reports - the score report and anti-cheat report, enriched so they
are comprehensive, self-auditing artifacts (not bare tables). The score report
carries a `verification` block (the round's whole real-evidence state, independently
re-checked); the anti-cheat report maps every rejection to its gate and lists the
full anti-cheat axis taxonomy. Pure functions over an ArenaResult - testable.
"""
from __future__ import annotations

# The 12 anti-cheat axes map to the gate or mechanism that catches each.
ANTICHEAT_AXES = {
    "copied_witness": "witness_verifies",
    "wrong_owner": "correct_owner",
    "stale_replay": "fresh_nonce",
    "fake_attestation": "attestation_valid",
    "fake_compute_profile": "compute_profile_honest",
    "spam": "no_replay",
    "invalid_cnf": "cnf_hash_matches",
    "missing_decode_map": "decode_map_present",
    "invalid_replay_harness": "replay_succeeds",
    "hotkey_stacking": "coldkey_collapse",
    "trace_forgery": "agent_signature_valid",
    "mislabeled_finding": "hypothesis_aligned",
}


def trace_training_row(a, weight: float, archetype: str, round_no: int) -> dict:
    """Build one ML-ready labeled trace row from an agent result.

    Features are the real agent operation: commands, files, hypothesis, encoder,
    gates, and method. Labels are archetype, honest/cheat class, cheat type,
    pass/fail state, and the gate or mechanism that caught it.
    """
    passed = a.gates.passed()
    is_cheat = archetype != "honest"
    # How the trace resolved. Most bad behavior is caught by a boolean gate.
    # Hotkey stacking can pass per-submission gates, then lose at coldkey collapse.
    # If another cheat ever passes, label it explicitly instead of hiding it.
    if not is_cheat:
        caught_by = "n/a"
    elif not passed:
        caught_by = "gate"
    elif archetype == "hotkey_stacking":
        caught_by = "coldkey_collapse"
    else:
        caught_by = "uncaught_passed_cheat"
    outcome_label = (
        "accepted_honest" if not is_cheat else
        "rejected_cheat" if not passed else
        "sybil_collapsed" if archetype == "hotkey_stacking" else
        "accepted_cheat"
    )
    return {
        **a.run.__dict__,
        "gates": a.gates.as_dict(),
        "weight": weight, "round": round_no,
        # --- supervised labels ---
        "archetype": archetype,
        "label": "honest" if not is_cheat else "cheat",
        "cheat_type": None if not is_cheat else archetype,
        "passed": passed,
        "rejected_by_gate": None if passed else a.gates.first_failure(),
        "caught_by": caught_by,
        "outcome_label": outcome_label,
    }


_TRACE_FEATURE_COLUMNS = [
    "agent_id", "environment", "target_netuid", "commands", "files_inspected",
    "hypothesis", "encoder", "gates", "method", "weight",
]
_TRACE_LABEL_COLUMNS = [
    "archetype", "label", "cheat_type", "passed", "rejected_by_gate",
    "caught_by", "outcome_label",
]


def dataset_card(rows: list) -> dict:
    """A self-describing manifest for the traces.jsonl training set: schema (feature
    vs label columns), the label taxonomy, and the actual class distribution. Makes
    the 'traces become training data' artifact documented + trainability-checkable."""
    import collections
    cheat_types = sorted({r["cheat_type"] for r in rows if r.get("cheat_type")})
    return {
        "schema": "cathedral.arena.traces.v1",
        "task": "cheat detection from a miner-agent trace (binary label + multiclass cheat_type)",
        "n_rows": len(rows),
        "feature_columns": _TRACE_FEATURE_COLUMNS,
        "label_columns": _TRACE_LABEL_COLUMNS,
        "label_taxonomy": {
            "label": ["honest", "cheat"],
            "outcome_label": ["accepted_honest", "rejected_cheat",
                              "sybil_collapsed", "accepted_cheat"],
            "cheat_type": cheat_types,
        },
        "class_distribution": {
            "label": dict(collections.Counter(r["label"] for r in rows)),
            "outcome_label": dict(collections.Counter(r["outcome_label"] for r in rows)),
            "caught_by": dict(collections.Counter(r["caught_by"] for r in rows)),
        },
    }


def _metric_breakdown(a) -> dict:
    """Make `reward = linear_metric x boolean_gate` concrete per agent."""
    c = a.credit
    gate = 1.0 if (c.verified and c.attest_gate) else 0.0
    return {
        "boolean_gate": gate, "all_gates_pass": a.gates.passed(),
        "tier_weight": round(c.tier_weight, 4), "speed": round(c.speed, 4),
        "linear_metric_contrib": round(c.contrib, 4),     # gate x tier_weight x speed
        "formula": "contrib = gate x tier_weight x speed",
    }


def score_report(result, roster) -> dict:
    """The score report + a `verification` block re-checking the whole round's real
    evidence: scoring self-audit, the Merkle anchor, the real audit vault, live
    attestation, the off-box decode, and the minted invariant families."""
    from .audit import audit_scoring
    arch = {s.hotkey: s.archetype for s in roster}
    oc = result.operator_console
    la = oc.get("live_attestation", {}) or {}
    ra = oc.get("round_attest", {}) or {}
    ed = oc.get("external_decode", {}) or {}
    mi = oc.get("minted_invariants", {}) or {}
    return {
        "season": result.season, "round": result.round_no,
        "reward_rule": "reward = linear_metric x boolean_gate",
        "signed_vector": result.signed_vector,
        "agents": [{
            "agent_id": a.run.agent_id, "hotkey": a.run.miner_hotkey,
            "archetype": arch.get(a.run.miner_hotkey, "?"),
            "environment": a.run.environment, "target_netuid": a.run.target_netuid,
            "tier": a.mission.tier, "attestation_required": a.mission.attestation_required,
            "weight": round(result.weights.get(a.run.miner_hotkey, 0.0), 4),
            "emissions_tau": result.emissions.get(a.run.miner_hotkey, 0.0),
            "rank": result.ranks.get(a.run.miner_hotkey, ""),
            "provenance_grade": a.gates.provenance_grade,
            "passed": a.gates.passed(), "gates": a.gates.as_dict(),
            "method": getattr(a.run, "method", ""),
            "metric_breakdown": _metric_breakdown(a),     # reward = linear_metric x gate, shown
        } for a in result.agents],
        "breaks": result.breaks,
        "total_emissions_tau": sum(result.emissions.values()),
        "verification": {
            "scoring_audit": audit_scoring(result),                 # the 6 invariants
            "anchor_merkle_root": result.anchor.get("merkle_root"),
            "real_audit_vault": result.real_audit_vault,
            "attestation": {
                "live_quote_reverified": bool(la.get("ok")),
                "binding_reverified_locally": bool(la.get("binding_reverified")),
                "intel_verified": bool(la.get("intel_verified")),
                "instance": la.get("instance"), "cost_usd": la.get("cost_usd"),
                "round_commitment": ra.get("commitment"),
                "round_attested": bool(ra.get("attested_to_this_round")),
            },
            "off_box_decode": {
                "available": bool(ed.get("available")), "ok": bool(ed.get("ok")),
                "decode": ed.get("decode"), "decoded_input": ed.get("decoded_input"),
            },
            "minted_invariant_families": mi.get("families"),
        },
    }


def anticheat_report(result) -> dict:
    """The anti-cheat report: every rejection mapped to the gate that caught it +
    the full axis taxonomy, so it reads as 'these axes exist; here's what each
    round rejected and why'."""
    feed = result.anticheat_feed
    rejected = [{
        "agent": x.get("agent"), "archetype": x.get("archetype"),
        "subnet": x.get("subnet"), "rejected_by_gate": x.get("rejected_by"),
        "reasons": x.get("reasons", []),
    } for x in feed]
    gates_hit = sorted({x.get("rejected_by") for x in feed if x.get("rejected_by")})
    return {
        "season": result.season, "round": result.round_no,
        "total_rejected": len(feed),
        "anticheat_axes": ANTICHEAT_AXES,
        "axes_count": len(ANTICHEAT_AXES),
        "gates_exercised_this_round": gates_hit,
        "rejected": rejected,
        "rule": "a submission scores only if EVERY boolean gate passes; "
                "category/prose/severity never create score",
        "axis_note": "anti-cheat axes may map to a GateOutcome field or a scoring mechanism",
    }
