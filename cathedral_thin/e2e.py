"""Deterministic local proof that the thin subnet loop works without services."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bittensor_wallet import Keypair
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scaffold.dimacs import solve_cnf

from .core import (
    StateStore,
    build_challenge,
    coldkey_collapsed_weights,
    decode_cnf,
    encode_assignment,
    gate_scores_by_current_verification,
    mark_pending,
    note_submission_failure,
    note_submission_success,
    response_succeeded,
    update_ema,
)
from .validator import Peer, ValidatorConfig, evaluate_peers, uid_vector
from .score_classes import (
    DecisionStore,
    canonical_json,
    compose_class_decisions,
    decision_document,
    enforce_checkpoint,
    external_class_decision,
    format_time,
    load_score_policy,
    local_class_decision,
    materialize_registered_policy,
    sign_owner_registration,
    sign_report,
    verify_owner_registration,
    verify_report,
)


class Axon:
    def __init__(self, hotkey: str):
        self.hotkey = hotkey
        self.port = 8091
        self.is_serving = True


def wire_response(
    synapse: Any,
    *,
    hotkey: str,
    assignment_b64: str,
    challenge_id: str | None = None,
    miner_hotkey: str | None = None,
    error: str = "",
    reported_ms: float = 0.0,
) -> Any:
    return SimpleNamespace(
        challenge_id=challenge_id if challenge_id is not None else synapse.challenge_id,
        miner_hotkey=miner_hotkey if miner_hotkey is not None else synapse.miner_hotkey,
        assignment_b64=assignment_b64,
        error=error,
        miner_elapsed_ms=reported_ms,
        axon=SimpleNamespace(hotkey=hotkey),
        dendrite=SimpleNamespace(status_code=200),
    )


def solve_wire(synapse: Any) -> str:
    cnf = decode_cnf(
        synapse.cnf_b64,
        expected_sha256=synapse.cnf_sha256,
        expected_vars=synapse.n_vars,
        expected_clauses=synapse.n_clauses,
    )
    assignment = solve_cnf(cnf.to_dimacs())
    if assignment is None:
        raise RuntimeError("reference solver unexpectedly returned UNSAT")
    return encode_assignment(assignment, n_vars=cnf.n_vars)


async def run_e2e(
    *,
    external_report_raw: bytes | None = None,
    external_public_key: bytes | None = None,
) -> dict[str, Any]:
    """Run the local validator, optionally with a real external score report.

    The paired arguments are intentionally bytes-only so the cross-repository
    proof exercises the same parser and signature path as an operator-loaded
    report instead of passing trusted Python objects across the boundary.
    """

    if (external_report_raw is None) != (external_public_key is None):
        raise ValueError(
            "external report bytes and public key must be supplied together"
        )
    if external_public_key is not None and len(external_public_key) != 32:
        raise ValueError("external report public key must be 32 bytes")
    config = ValidatorConfig(
        network="local",
        netuid=1,
        validator_hotkey="validator",
        n_vars=24,
        n_clauses=102,
        timeout_secs=5.0,
        reference_ms=100.0,
        correctness_share=0.8,
        ema_alpha=0.35,
        concurrency=8,
        round_blocks=10,
    )
    peer_rows = [
        (0, "honest-a", "cold-honest", "honest"),
        (1, "honest-a2", "cold-honest", "honest"),
        (2, "honest-b", "cold-rival", "honest"),
        (3, "copier", "cold-copy", "copy"),
        (4, "replayer", "cold-replay", "replay"),
        (5, "swapper", "cold-swap", "swap"),
        (6, "invalid", "cold-invalid", "invalid"),
        (7, "offline", "cold-offline", "offline"),
    ]
    peers = [
        Peer(
            uid=uid,
            hotkey=hotkey,
            coldkey=coldkey,
            axon=Axon(hotkey),
            serviceable=behavior != "offline",
        )
        for uid, hotkey, coldkey, behavior in peer_rows
    ]
    behavior_of = {hotkey: behavior for _, hotkey, _, behavior in peer_rows}

    with tempfile.TemporaryDirectory(prefix="cathedral-thin-e2e-") as tmpdir:
        store = StateStore(
            Path(tmpdir) / "state.json", fingerprint=config.fingerprint()
        )
        state = store.load_or_create()
        state.ema_scores = {"offline": 0.99}
        original_secret = state.master_secret

        source_challenge, source_cnf = build_challenge(
            state.master_secret,
            netuid=config.netuid,
            validator_hotkey=config.validator_hotkey,
            miner_hotkey="honest-a",
            round_id=7,
            issued_at_ms=1_000,
            ttl_ms=10_000,
            n_vars=config.n_vars,
            n_clauses=config.n_clauses,
        )
        source_assignment = solve_cnf(source_cnf.to_dimacs())
        assert source_assignment is not None
        copied_assignment = encode_assignment(source_assignment, n_vars=config.n_vars)

        async def query(peer: Peer, synapse: Any, _timeout: float) -> Any:
            behavior = behavior_of[peer.hotkey]
            if behavior == "honest":
                return wire_response(
                    synapse,
                    hotkey=peer.hotkey,
                    assignment_b64=solve_wire(synapse),
                    reported_ms=-999_999.0,
                )
            if behavior == "copy":
                return wire_response(
                    synapse, hotkey=peer.hotkey, assignment_b64=copied_assignment
                )
            if behavior == "replay":
                return wire_response(
                    synapse,
                    hotkey=peer.hotkey,
                    assignment_b64=solve_wire(synapse),
                    challenge_id=source_challenge.challenge_id,
                )
            if behavior == "swap":
                return wire_response(
                    synapse,
                    hotkey=peer.hotkey,
                    assignment_b64=solve_wire(synapse),
                    miner_hotkey="honest-a",
                )
            if behavior == "invalid":
                return wire_response(synapse, hotkey=peer.hotkey, assignment_b64="AA==")
            raise RuntimeError("offline peer should not be queried")

        results = await evaluate_peers(
            peers,
            state=state,
            config=config,
            round_id=7,
            query=query,
        )
        observations = {hotkey: result.score for hotkey, result in results.items()}
        ema = update_ema(
            state.ema_scores,
            observations,
            current_hotkeys=[peer.hotkey for peer in peers],
            alpha=config.ema_alpha,
        )
        coldkey_of = {peer.hotkey: peer.coldkey for peer in peers}
        eligible = gate_scores_by_current_verification(ema, observations)
        weights = coldkey_collapsed_weights(eligible, coldkey_of)

        # Exercise the federated class path with a Cathedral Confidential-shaped
        # signed report. The source supplies facts and receipt provenance; the
        # validator chooses both the 40% class budget and the work-unit metric.
        score_key = (
            Ed25519PrivateKey.generate() if external_report_raw is None else None
        )
        score_public = (
            score_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            if score_key is not None
            else external_public_key
        )
        assert score_public is not None
        source_owner = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
        source_delegate = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
        report_path = Path(tmpdir) / "confidential-class.json"
        registration_path = Path(tmpdir) / "owner-registration.json"
        policy_path = Path(tmpdir) / "score-policy.json"
        policy_path.write_bytes(
            canonical_json(
                {
                    "schema": "cathedral_score_policy_v1",
                    "network": "local",
                    "netuid": 1,
                    "classes": [
                        {
                            "allocation": "0.6",
                            "class_id": "local_sat",
                            "kind": "local_sat",
                        },
                        {
                            "allocation": "0.4",
                            "assignment": {
                                "cap": "10",
                                "metric": "verified_work_units",
                                "mode": "metric",
                                "required_evidence_kinds": [
                                    "cathedral_assurance_receipt_v2"
                                ],
                                "required_reason_codes": [
                                    "receipt_verified",
                                    "work_verified",
                                ],
                                "transform": "linear",
                            },
                            "class_id": "confidential_compute",
                            "kind": "external",
                            "locations": [
                                "https://source.example/confidential-class.json"
                            ],
                            "max_age_seconds": 600,
                            "max_block_span": 100,
                            "max_future_seconds": 30,
                            "owner_registration": {
                                "locations": [str(registration_path)],
                                "max_age_seconds": 600,
                                "max_block_span": 100,
                                "max_future_seconds": 30,
                                "require_target_registration": True,
                                "source_netuid": 7,
                            },
                            "require_evidence": True,
                            "source_id": "cathedralconfidential",
                        },
                    ],
                }
            )
        )
        score_policy = load_score_policy(policy_path, network="local", netuid=1)
        issued = datetime.now(UTC)
        registration_path.write_bytes(
            sign_owner_registration(
                {
                    "schema": "cathedral_owner_score_registration_v1",
                    "network": "local",
                    "source_netuid": 7,
                    "target_netuid": 1,
                    "owner_coldkey": source_owner.ss58_address,
                    "delegate_hotkey": source_delegate.ss58_address,
                    "source_id": "cathedralconfidential",
                    "class_ids": ["confidential_compute"],
                    "report_locations": [
                        "https://source.example/confidential-class.json"
                    ],
                    "report_keys": {
                        "e2e-score-key": base64.b64encode(score_public).decode("ascii")
                    },
                    "sequence": 0,
                    "previous_registration_id": None,
                    "issued_at": format_time(issued),
                    "expires_at": format_time(issued + timedelta(minutes=5)),
                    "valid_from_block": 70,
                    "valid_until_block": 80,
                },
                source_owner,
            )
        )
        if external_report_raw is None:
            assert score_key is not None
            report_body = {
                "schema": "cathedral_score_class_report_v1",
                "network": "local",
                "netuid": 1,
                "class_id": "confidential_compute",
                "source_id": "cathedralconfidential",
                "source_epoch": 7,
                "generated_at": format_time(issued),
                "valid_until": format_time(issued + timedelta(minutes=5)),
                "valid_from_block": 70,
                "valid_until_block": 80,
                "complete": True,
                "policy_digest": "sha256:" + "11" * 32,
                "verifier_digest": "sha256:" + "22" * 32,
                "previous_report_id": None,
                "entries": [
                    {
                        "miner_hotkey": hotkey,
                        "metrics": {"verified_work_units": units},
                        "asserted_score": None,
                        "reason_codes": ["receipt_verified", "work_verified"],
                        "evidence": [
                            {
                                "kind": "cathedral_assurance_receipt_v2",
                                "id": "receipt-sha256:"
                                + hashlib.sha256(hotkey.encode()).hexdigest(),
                                "digest": "sha256:"
                                + hashlib.sha256(
                                    ("receipt:" + hotkey).encode()
                                ).hexdigest(),
                                "uri": None,
                            }
                        ],
                    }
                    for hotkey, units in (
                        ("honest-a", "1"),
                        ("honest-a2", "1"),
                        ("honest-b", "2"),
                    )
                ],
                "signing_key_id": "e2e-score-key",
            }
            report_path.write_bytes(sign_report(report_body, score_key))
        else:
            report_path.write_bytes(external_report_raw)
        configured_external_policy = score_policy.external_classes[0]
        owner_registration, registration_checkpoint = verify_owner_registration(
            registration_path.read_bytes(),
            configured_external_policy,
            network="local",
            netuid=1,
            current_block=70,
            current_owner_coldkey=source_owner.ss58_address,
            registered_hotkeys={
                source_delegate.ss58_address: source_owner.ss58_address
            },
            now=issued,
        )
        external_policy = materialize_registered_policy(
            configured_external_policy, owner_registration
        )
        report = verify_report(
            report_path.read_bytes(),
            external_policy,
            network="local",
            netuid=1,
            current_block=70,
            now=issued,
        )
        checkpoint = enforce_checkpoint(report, None)
        local_decision = local_class_decision(
            score_policy.local_class,
            eligible,
            coldkey_of=coldkey_of,
            reasons={hotkey: result.reason for hotkey, result in results.items()},
        )
        external_decision = external_class_decision(
            external_policy,
            report,
            coldkey_of=coldkey_of,
            owner_registration=owner_registration,
        )
        composed = compose_class_decisions(
            score_policy, [local_decision, external_decision]
        )
        uids, uid_weights = uid_vector(composed, peers)
        decision, decision_digest = decision_document(
            validator_hotkey=config.validator_hotkey,
            network=config.network,
            netuid=config.netuid,
            round_id=7,
            block=70,
            policy_digest=score_policy.digest,
            decisions=[local_decision, external_decision],
            peers=[
                {
                    "uid": peer.uid,
                    "hotkey": peer.hotkey,
                    "coldkey": peer.coldkey,
                    "serviceable": peer.serviceable,
                }
                for peer in peers
            ],
            uids=uids,
            weights=uid_weights,
        )
        decision_path = DecisionStore(Path(tmpdir) / "decisions").write(decision)

        state.ema_scores = ema
        state.class_checkpoints = {
            external_policy.class_id: {
                "source_epoch": checkpoint.source_epoch,
                "report_id": checkpoint.report_id,
            }
        }
        state.registration_checkpoints = {
            external_policy.class_id: {
                "owner_coldkey": registration_checkpoint.owner_coldkey,
                "delegate_hotkey": registration_checkpoint.delegate_hotkey,
                "sequence": registration_checkpoint.sequence,
                "registration_id": registration_checkpoint.registration_id,
            }
        }
        state.last_completed_round = 7
        hotkey_by_uid = {peer.uid: peer.hotkey for peer in peers}
        pending = mark_pending(
            state,
            uids=uids,
            weights=uid_weights,
            hotkeys=[hotkey_by_uid[uid] for uid in uids],
            provenance_digest=decision_digest,
            registration_ids={
                external_policy.class_id: owner_registration.registration_id
            },
        )
        store.save(state)
        pending_digest = pending.digest

        # First chain response fails. The identical vector survives a restart.
        note_submission_failure(
            state, now_ms=10_000, base_backoff_ms=1, max_backoff_ms=1
        )
        store.save(state)
        reloaded = store.load_or_create()
        retry_same = bool(
            reloaded.pending_vector and reloaded.pending_vector.digest == pending_digest
        )
        secret_stable = reloaded.master_secret == original_secret
        fake_success = SimpleNamespace(success=True)
        assert response_succeeded(fake_success)
        note_submission_success(reloaded)
        store.save(reloaded)
        confirmed = store.load_or_create()

        cold_totals: dict[str, float] = {}
        for hotkey, weight in weights.items():
            coldkey = coldkey_of[hotkey]
            cold_totals[coldkey] = cold_totals.get(coldkey, 0.0) + weight
        lower_sybil = min(("honest-a", "honest-a2"), key=lambda key: ema[key])
        without_extra = dict(eligible)
        without_extra[lower_sybil] = 0.0
        reduced_weights = coldkey_collapsed_weights(without_extra, coldkey_of)
        reduced_honest_total = sum(
            weight
            for hotkey, weight in reduced_weights.items()
            if coldkey_of[hotkey] == "cold-honest"
        )

        evidence = {
            "schema": "cathedral.thin.e2e.v1",
            "owner_hosted_services": 0,
            "round": 7,
            "miners": len(peers),
            "verified": sorted(
                hotkey
                for hotkey, result in results.items()
                if result.reason == "verified"
            ),
            "attacks": {
                hotkey: results[hotkey].reason
                for hotkey in ("copier", "replayer", "swapper", "invalid", "offline")
            },
            "coldkey_totals": {
                key: round(value, 9) for key, value in sorted(cold_totals.items())
            },
            "sybil_no_multiplier": abs(
                cold_totals["cold-honest"] - reduced_honest_total
            )
            < 1e-12,
            "miner_timing_ignored": all(
                results[key].miner_reported_ms < 0 and results[key].score > 0
                for key in ("honest-a", "honest-a2", "honest-b")
            ),
            "historical_offline_gated": ema["offline"] > 0
            and weights.get("offline", 0.0) == 0.0,
            "weight_sum": round(sum(uid_weights), 12),
            "score_classes": {
                "report_origin": (
                    "external_report_bytes"
                    if external_report_raw is not None
                    else "self_contained_fixture"
                ),
                "allocations": {"local_sat": 0.6, "confidential_compute": 0.4},
                "confidential_report_id": report.report_id,
                "confidential_checkpoint": checkpoint.source_epoch,
                "owner_registration_id": owner_registration.registration_id,
                "owner_registration_sequence": owner_registration.sequence,
                "owner_registration_verified": True,
                "delegate_registered": True,
                "validator_assignment": "verified_work_units",
                "external_hotkeys": sorted(external_decision.raw_scores),
                "receipt_evidence_ids": sorted(
                    item["id"]
                    for provenance in external_decision.provenance.values()
                    for item in provenance["evidence"]
                ),
                "decision_digest": decision_digest,
                "decision_record_written": decision_path.exists(),
            },
            "onchain_vector": [
                {"uid": uid, "weight": round(weight, 12)}
                for uid, weight in zip(uids, uid_weights)
            ],
            "pending_digest": pending_digest,
            "retry_identical_after_restart": retry_same,
            "secret_stable_after_restart": secret_stable,
            "confirmed_after_retry": confirmed.confirmed_vector_digest == pending_digest
            and confirmed.confirmed_decision_digest == decision_digest
            and confirmed.pending_vector is None,
        }
        required = [
            evidence["owner_hosted_services"] == 0,
            len(evidence["verified"]) == 3,
            all(evidence["attacks"][key] != "verified" for key in evidence["attacks"]),
            evidence["sybil_no_multiplier"],
            evidence["miner_timing_ignored"],
            evidence["historical_offline_gated"],
            evidence["weight_sum"] == 1.0,
            evidence["score_classes"]["decision_record_written"],
            evidence["score_classes"]["owner_registration_verified"],
            evidence["score_classes"]["delegate_registered"],
            evidence["retry_identical_after_restart"],
            evidence["secret_stable_after_restart"],
            evidence["confirmed_after_retry"],
        ]
        evidence["ok"] = all(required)
        return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exercise the Cathedral thin subnet locally"
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    evidence = asyncio.run(run_e2e())
    print(json.dumps(evidence, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
