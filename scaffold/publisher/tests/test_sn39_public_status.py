from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import publish_sn39_validator_status as status


def _timestamp(offset_seconds: int = 0) -> str:
    value = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _event(
    event: str,
    event_status: str,
    *,
    detail: str = "",
    offset_seconds: int = 0,
) -> dict[str, object]:
    return {
        "ts": _timestamp(offset_seconds),
        "event": event,
        "stage": "submit",
        "mode": "thin",
        "status": event_status,
        "detail": detail,
    }


def _startup(authority: str, provenance: str) -> dict[str, object]:
    return {
        "ts": _timestamp(),
        "event": "STARTUP",
        "stage": "startup",
        "mode": authority,
        "status": "INFO",
        "detail": (
            f"submission_authority={authority} provenance={provenance} "
            "policy_pin=validated_supply_v1 network=finney netuid=39"
        ),
        "authority": authority,
        "provenance_mode": provenance,
    }


def test_exact_launch_boundary_is_pass_but_all_burn_is_not_proven() -> None:
    launch = status.clean_event(
        _event(
            "WEIGHTS_SUBMITTED",
            "PASS",
            detail=(
                "authority=thin uids=2 burn_uid=204 burn_share=0.100000 "
                "vector=163:0.900000,204:0.100000"
            ),
        )
    )
    assert status.is_launch_weight_boundary(launch)
    assert status.build_status([launch])["authority"]["status"] == "PASS"

    all_burn = status.clean_event(
        _event(
            "WEIGHTS_SUBMITTED",
            "PASS",
            detail=(
                "authority=thin uids=1 burn_uid=204 burn_share=1.000000 "
                "vector=204:1.000000"
            ),
        )
    )
    assert not status.is_launch_weight_boundary(all_burn)
    all_burn_status = status.build_status([all_burn])
    assert all_burn_status["authority"]["status"] == "NOT_PROVEN"
    assert all_burn_status["authority"]["burn_share"] is None


def test_launch_boundary_tracks_dynamic_uids_and_failed_tick_stays_ambiguous() -> None:
    moved = status.clean_event(
        _event(
            "WEIGHTS_SUBMITTED",
            "PASS",
            detail=(
                "authority=thin uids=2 burn_uid=7 burn_share=0.100000 "
                "vector=7:0.100000,241:0.900000"
            ),
        )
    )
    assert status.is_launch_weight_boundary(moved)

    failed = status.clean_event(_event("TICK_FAILED", "FAIL", detail="rpc timeout"))
    assert failed is not None
    assert "may have finalized" in failed["detail"]
    assert "automatic retry remains blocked" in failed["remediation"]
    document = status.build_status([moved, failed])
    assert document["authority"]["status"] == "FAIL"


def test_pending_receipt_unavailability_is_not_mislabeled_as_failure() -> None:
    pending = status.clean_event(
        _event(
            "PENDING_RECEIPT_NOT_PROVEN",
            "NOT_PROVEN",
            detail="archive temporarily unavailable",
        )
    )
    assert pending is not None
    assert pending["status"] == "NOT_PROVEN"
    assert "no replacement was submitted" in pending["detail"]
    assert "never submit a replacement" in pending["remediation"]
    document = status.build_status([pending])
    assert document["authority"]["status"] == "NOT_PROVEN"
    assert document["authority"]["latest_event"] == "PENDING_RECEIPT_NOT_PROVEN"


def test_exact_recovered_boundary_is_pass_without_claiming_second_write() -> None:
    recovered = status.clean_event(
        _event(
            "PENDING_RECEIPT_RECOVERED",
            "PASS",
            detail=(
                "authority=thin uids=2 burn_uid=204 burn_share=0.100000 "
                "vector=163:0.900000,204:0.100000"
            ),
        )
    )
    assert recovered is not None
    assert status.is_launch_weight_boundary(recovered)
    assert recovered["detail"] == (
        "exact journaled transaction re-proven; no second chain write"
    )
    assert "never retry" in recovered["remediation"]

    document = status.build_status([recovered])
    assert document["authority"]["status"] == "PASS"
    assert document["authority"]["latest_event"] == "PENDING_RECEIPT_RECOVERED"
    assert document["authority"]["burn_share"] == "0.10"


def test_incomplete_recovered_boundary_is_not_proven() -> None:
    recovered = status.clean_event(
        _event(
            "PENDING_RECEIPT_RECOVERED",
            "PASS",
            detail=(
                "the exact journaled thin receipt was re-proven and finalized; "
                "no second chain write was attempted"
            ),
        )
    )
    assert recovered is not None
    assert not status.is_launch_weight_boundary(recovered)
    assert recovered["detail"] == (
        "exact journaled transaction re-proven; no second chain write"
    )

    document = status.build_status([recovered])
    assert document["authority"]["status"] == "NOT_PROVEN"
    assert document["authority"]["burn_share"] is None


def test_recovered_full_authority_is_not_the_thin_launch_boundary() -> None:
    recovered = status.clean_event(
        _event(
            "PENDING_RECEIPT_RECOVERED",
            "PASS",
            detail=(
                "authority=full_provenance uids=2 burn_uid=204 "
                "burn_share=0.100000 vector=163:0.900000,204:0.100000"
            ),
        )
    )
    assert recovered is not None
    assert recovered["authority"] == "full_provenance"
    assert not status.is_launch_weight_boundary(recovered)

    document = status.build_status([recovered])
    assert document["authority"]["status"] == "NOT_PROVEN"
    assert document["authority"]["burn_share"] is None


def test_full_authority_startup_is_truthful_and_cannot_claim_thin_launch() -> None:
    startup = status.clean_event(_startup("full_provenance", "authority"))
    raw_recovered = _event(
        "PENDING_RECEIPT_RECOVERED",
        "PASS",
        detail=(
            "authority=full_provenance uids=2 burn_uid=204 "
            "burn_share=0.100000 vector=163:0.900000,204:0.100000"
        ),
    )
    raw_recovered["mode"] = "full_provenance"
    recovered = status.clean_event(raw_recovered)

    assert startup is not None
    assert startup["detail"] == "FULL provenance authority started"
    document = status.build_status([startup, recovered])
    assert document["authority"]["mode"] == "full_provenance"
    assert document["provenance"]["mode"] == "authority"
    assert document["authority"]["status"] == "NOT_PROVEN"
    assert document["authority"]["burn_share"] is None


def test_invalid_or_self_contradictory_startup_is_dropped() -> None:
    mismatched = _startup("full_provenance", "authority")
    mismatched["mode"] = "thin"
    invalid_pair = _startup("thin", "authority")

    assert status.clean_event(mismatched) is None
    assert status.clean_event(invalid_pair) is None


def test_pending_receipt_contradiction_overrides_prior_thin_pass() -> None:
    startup = status.clean_event(_startup("thin", "shadow"))
    launch = status.clean_event(
        _event(
            "WEIGHTS_SUBMITTED",
            "PASS",
            detail=(
                "authority=thin uids=2 burn_uid=204 burn_share=0.100000 "
                "vector=163:0.900000,204:0.100000"
            ),
        )
    )
    contradiction = status.clean_event(
        _event(
            "PENDING_RECEIPT_CONTRADICTION",
            "FAIL",
            detail="private contradictory receipt details",
            offset_seconds=1,
        )
    )

    assert contradiction is not None
    assert "positive durable or historical contradiction" in contradiction["detail"]
    document = status.build_status([startup, launch, contradiction])
    assert document["authority"]["mode"] == "thin"
    assert document["authority"]["status"] == "FAIL"
    assert document["authority"]["latest_event"] == ("PENDING_RECEIPT_CONTRADICTION")


def test_event_status_mismatch_is_dropped() -> None:
    assert status.clean_event(_event("WEIGHTS_SUBMITTED", "FAIL")) is None
    assert status.clean_event(_event("PENDING_RECEIPT_RECOVERED", "FAIL")) is None
    assert status.clean_event(_event("PENDING_RECEIPT_NOT_PROVEN", "FAIL")) is None
    assert status.clean_event(_event("PROVENANCE_AUDIT_FAIL", "PASS")) is None
    assert status.clean_event(_event("WEIGHTS_DRY_RUN", "FAIL"))["status"] == "FAIL"


def test_public_status_is_time_bounded() -> None:
    stale = status.clean_event(
        _event(
            "WEIGHTS_SUBMITTED",
            "PASS",
            detail=(
                "authority=thin uids=2 burn_uid=204 burn_share=0.100000 "
                "vector=163:0.900000,204:0.100000"
            ),
            offset_seconds=-(status.MAX_EVENT_AGE_SECONDS + 1),
        )
    )
    document = status.build_status([stale])
    assert document["authority"]["status"] == "NOT_PROVEN"
    assert document["authority"]["fresh"] is False
    assert datetime.fromisoformat(document["valid_until"].replace("Z", "+00:00")) > (
        datetime.fromisoformat(document["generated_at"].replace("Z", "+00:00"))
    )


def test_rewarded_set_pass_does_not_claim_whole_epoch_full() -> None:
    rewarded = status.clean_event(_event("LAUNCH_REWARDED_SET_GATE_PASS", "PASS"))
    provenance = status.clean_event(
        _event(
            "PROVENANCE_AUDIT_NOT_PROVEN",
            "NOT_PROVEN",
            detail="positive raw evidence replayed for 1 miners",
        )
    )
    document = status.build_status([rewarded, provenance])
    assert document["provenance"]["rewarded_set_full"] == "PASS"
    assert document["provenance"]["positive_tdx_raw_replay"] == "PASS"
    assert document["provenance"]["whole_epoch_full"] == "NOT_PROVEN"


def test_current_full_audit_does_not_upgrade_receipts_only_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = {
        "launch_submission": {
            "evidence_checkpoint": {"public_assurance": "receipts_only"}
        }
    }
    monkeypatch.setattr(status, "read_signed_release", lambda: release)
    startup = status.clean_event(_startup("thin", "shadow"))
    audit = status.clean_event(_event("PROVENANCE_AUDIT_PASS", "PASS"))

    document = status.build_status([startup, audit])

    assert document["provenance"]["launch_public_assurance"] == "receipts_only"
    assert document["provenance"]["whole_epoch_full"] == "NOT_PROVEN"
    assert document["provenance"]["current_whole_epoch_full"] == "PASS"


def test_unsigned_release_cannot_upgrade_launch_assurance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_path = tmp_path / "release.json"
    signature_path = tmp_path / "release.json.sig"
    keys_path = tmp_path / "release-attestation-keys.json"
    release_path.write_text(
        json.dumps(
            {
                "release_attestation": {"key_id": status.RELEASE_KEY_ID},
                "launch_submission": {
                    "evidence_checkpoint": {"public_assurance": "full"}
                },
            }
        ),
        encoding="utf-8",
    )
    keys_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(status, "RELEASE", release_path)
    monkeypatch.setattr(status, "RELEASE_SIGNATURE", signature_path)
    monkeypatch.setattr(status, "RELEASE_KEYS", keys_path)

    assert status.read_signed_release() == {}
    document = status.build_status([])
    assert document["provenance"]["whole_epoch_full"] == "NOT_PROVEN"
    assert document["provenance"]["launch_public_assurance"] == "NOT_PROVEN"


def test_valid_detached_release_signature_can_publish_launch_assurance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    release_path = tmp_path / "release.json"
    signature_path = tmp_path / "release.json.sig"
    keys_path = tmp_path / "release-attestation-keys.json"
    release = {
        "release_attestation": {"key_id": status.RELEASE_KEY_ID},
        "launch_submission": {"evidence_checkpoint": {"public_assurance": "full"}},
    }
    release_bytes = json.dumps(
        release,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    signature = {
        "algorithm": "Ed25519",
        "key_id": status.RELEASE_KEY_ID,
        "payload": "release.json exact bytes",
        "payload_sha256": "sha256:" + hashlib.sha256(release_bytes).hexdigest(),
        "signature": base64.b64encode(private.sign(release_bytes)).decode(),
    }
    release_path.write_bytes(release_bytes)
    signature_path.write_text(json.dumps(signature), encoding="utf-8")
    keys_path.write_text(
        json.dumps({status.RELEASE_KEY_ID: base64.b64encode(public).decode()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(status, "RELEASE", release_path)
    monkeypatch.setattr(status, "RELEASE_SIGNATURE", signature_path)
    monkeypatch.setattr(status, "RELEASE_KEYS", keys_path)

    assert status.read_signed_release() == release
    document = status.build_status([])
    assert document["provenance"]["whole_epoch_full"] == "PASS"
    assert document["provenance"]["launch_public_assurance"] == "full"

    release_path.write_bytes(release_bytes + b" ")
    assert status.read_signed_release() == {}


def test_source_reader_rejects_symlink_and_world_writable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "events.jsonl"
    target.write_text(
        json.dumps(_event("VECTOR_ACCEPTED", "PASS")) + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    link = tmp_path / "events-link.jsonl"
    link.symlink_to(target)
    monkeypatch.setattr(status, "SOURCE", link)
    assert status.tail_events() == []

    monkeypatch.setattr(status, "SOURCE", target)
    target.chmod(0o666)
    assert status.tail_events() == []
    target.chmod(0o600)
    assert [row["event"] for row in status.tail_events()] == ["VECTOR_ACCEPTED"]


def test_public_json_reader_rejects_symlink_and_world_writable_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "release.json"
    target.write_text('{"claim":"safe"}', encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "release-link.json"
    link.symlink_to(target)
    assert status.read_public_json(link) == {}
    target.chmod(0o666)
    assert status.read_public_json(target) == {}
    target.chmod(0o600)
    assert status.read_public_json(target) == {"claim": "safe"}


def test_publisher_emits_only_sanitized_bounded_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "validator-events.jsonl"
    source.write_text(
        json.dumps(
            _event(
                "TICK_FAILED",
                "FAIL",
                detail=(
                    "https://name:secret@host/path?api_key=not-public "
                    "/var/lib/private 5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    source.chmod(0o600)
    root = tmp_path / "public"
    logs = root / "logs"
    logs.mkdir(parents=True, mode=0o755)
    (root / "index.json").write_text('{"recent":[]}', encoding="utf-8")
    (root / "release.json").write_text(
        '{"claim":"SN39 mainnet: validated Intel TDX CPU compute."}',
        encoding="utf-8",
    )
    for path in (root / "index.json", root / "release.json"):
        path.chmod(0o600)
    monkeypatch.setattr(status, "SOURCE", source)
    monkeypatch.setattr(status, "INDEX", root / "index.json")
    monkeypatch.setattr(status, "RELEASE", root / "release.json")
    monkeypatch.setattr(status, "LOG_ROOT", logs)

    assert status.main() == 0
    combined = b"".join(path.read_bytes() for path in logs.iterdir())
    assert b"secret" not in combined
    assert b"api_key" not in combined
    assert b"/var/lib/private" not in combined
    assert b"5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC" not in combined
    assert {path.name for path in logs.iterdir()} == {
        "validator-events.jsonl",
        "validator-events.log",
        "status.json",
    }
    assert all((path.stat().st_mode & 0o777) == 0o644 for path in logs.iterdir())


def test_output_directory_must_be_owner_controlled(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o777)
    logs.chmod(0o777)
    with pytest.raises(RuntimeError, match="owner-controlled"):
        status.atomic_write(logs / "status.json", b"{}")


def test_scrubber_removes_multi_at_credentials_queries_and_fragments() -> None:
    raw = (
        "https://user:p@ss@host.example/path?token=secret#fragment "
        "Authorization=Bearer nope /private/path "
        "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"
    )
    clean = status.scrub(raw, 512)
    assert "p@ss" not in clean
    assert "token" not in clean
    assert "fragment" not in clean
    assert "Bearer nope" not in clean
    assert "/private/path" not in clean
    assert "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC" not in clean


def test_source_file_is_not_mutated() -> None:
    assert os.path.isabs(status.SOURCE)
