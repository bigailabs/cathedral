from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scaffold import events as event_stream
from scaffold.events import EventLogger
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


def test_writer_to_publisher_preserves_startup_runtime_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = tmp_path / "raw.jsonl"
    public = tmp_path / "status.jsonl"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(public),
        tty=None,
    )
    logger.event(
        "STARTUP",
        stage="startup",
        status="INFO",
        detail=(
            "submission_authority=thin provenance=shadow "
            "policy_pin=validated_supply_v1 network=finney netuid=39"
        ),
        authority="thin",
        provenance_mode="shadow",
        private_hotkey="5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC",
    )
    logger.close()

    monkeypatch.setattr(status, "SOURCE", public)
    rows = status.tail_events()
    assert len(rows) == 1
    assert rows[0]["event"] == "STARTUP"
    assert rows[0]["authority"] == "thin"
    assert rows[0]["provenance_mode"] == "shadow"
    assert rows[0]["detail"] == (
        "thin authority and concurrent provenance shadow started"
    )
    assert "private_hotkey" not in rows[0]


def test_interrupted_raw_to_status_transition_overrides_stale_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = tmp_path / "raw.jsonl"
    public = tmp_path / "status.jsonl"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(public),
        tty=None,
    )
    logger.event(
        "STARTUP",
        stage="startup",
        status="INFO",
        authority="thin",
        provenance_mode="shadow",
    )
    weights = {"163": 0.9, "204": 0.1}
    logger.event(
        "WEIGHTS_SUBMITTED",
        stage="submit",
        status="PASS",
        authority="thin",
        uid_count=2,
        burn_uid=204,
        burn_share=0.1,
        uid_weights=weights,
    )
    monkeypatch.setattr(status, "SOURCE", public)
    assert status.build_status(status.tail_events())["authority"]["status"] == "PASS"

    original_write = event_stream._durable_jsonl_write

    def fail_before_status_commit(target, record):
        if (
            target is logger._status_file
            and record.get("publication_phase") == "COMMITTED"
        ):
            raise OSError("injected status commit failure")
        original_write(target, record)

    monkeypatch.setattr(
        event_stream,
        "_durable_jsonl_write",
        fail_before_status_commit,
    )
    with pytest.raises(OSError, match="injected"):
        logger.event(
            "TICK_FAILED",
            stage="result",
            status="FAIL",
            detail="private failure",
        )
    logger.close()

    rows = status.tail_events()
    assert rows[-1]["event"] == "STATUS_PUBLICATION_PENDING"
    assert rows[-1]["status"] == "NOT_PROVEN"
    document = status.build_status(rows)
    assert document["authority"]["status"] == "NOT_PROVEN"
    assert document["authority"]["latest_event"] == ("STATUS_PUBLICATION_PENDING")
    assert document["provenance"]["current_whole_epoch_full"] == "NOT_PROVEN"


def test_unknown_private_event_commit_closes_its_publication_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = tmp_path / "raw.jsonl"
    public = tmp_path / "status.jsonl"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(public),
        tty=None,
    )
    logger.event(
        "CHAIN_SUBMITTED",
        stage="submit",
        status="PASS",
        detail="private event outside the public allowlist",
    )
    logger.close()

    monkeypatch.setattr(status, "SOURCE", public)
    assert status.tail_events() == []


def test_unknown_private_event_commit_requires_safe_common_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = tmp_path / "status.jsonl"
    publication_id = "c" * 32
    rows = [
        {
            "ts": _timestamp(),
            "event": "STATUS_PUBLICATION_PENDING",
            "stage": "status",
            "mode": "thin",
            "status": "NOT_PROVEN",
            "publication_id": publication_id,
            "publication_phase": "PENDING",
            "target_event": "PRIVATE_EVENT",
        },
        {
            "ts": _timestamp(),
            "event": "CHAIN_SUBMITTED",
            "stage": "submit",
            "mode": "caller-controlled-mode",
            "status": "PASS",
            "publication_id": publication_id,
            "publication_phase": "COMMITTED",
            "target_event": "PRIVATE_EVENT",
        },
    ]
    public.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(status, "SOURCE", public)

    published = status.tail_events()
    assert [row["event"] for row in published] == ["STATUS_PUBLICATION_PENDING"]


def test_malformed_or_mismatched_commit_does_not_clear_pending_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = tmp_path / "status.jsonl"
    publication_id = "a" * 32
    rows = [
        {
            "ts": _timestamp(),
            "event": "STATUS_PUBLICATION_PENDING",
            "stage": "status",
            "mode": "thin",
            "status": "NOT_PROVEN",
            "publication_id": publication_id,
            "publication_phase": "PENDING",
            "target_event": "STARTUP",
        },
        {
            "ts": _timestamp(),
            "event": "VECTOR_ACCEPTED",
            "stage": "policy",
            "mode": "thin",
            "status": "PASS",
            "publication_id": publication_id,
            "publication_phase": "COMMITTED",
            "target_event": "VALIDATOR_RESULT",
        },
        {
            "ts": _timestamp(),
            "event": "STARTUP",
            "stage": "startup",
            "mode": "thin",
            "status": "INFO",
            "publication_id": publication_id,
            "publication_phase": "COMMITTED",
            "target_event": "STARTUP",
            # The required structured mode pair is intentionally absent.
        },
    ]
    public.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(status, "SOURCE", public)

    events = status.tail_events()
    assert [event["event"] for event in events] == [
        "STATUS_PUBLICATION_PENDING",
        "VECTOR_ACCEPTED",
    ]
    document = status.build_status(events)
    assert document["authority"]["status"] == "NOT_PROVEN"
    assert document["authority"]["latest_event"] == "STATUS_PUBLICATION_PENDING"


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


def test_unexpected_fence_field_cannot_be_used_as_free_form_public_text() -> None:
    document = _event("VECTOR_ACCEPTED", "PASS")
    document["target_event"] = "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"
    assert status.clean_event(document) is None

    pending = {
        "ts": _timestamp(),
        "event": "STATUS_PUBLICATION_PENDING",
        "stage": "status",
        "mode": "caller-controlled-mode",
        "status": "NOT_PROVEN",
        "publication_id": "b" * 32,
        "publication_phase": "PENDING",
        "target_event": "PRIVATE_EVENT",
    }
    assert status.clean_event(pending) is None


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
    raw_provenance = _event(
        "PROVENANCE_AUDIT_NOT_PROVEN",
        "NOT_PROVEN",
        detail="private validator-local diagnostics",
    )
    raw_provenance["positive_raw_replay"] = True
    provenance = status.clean_event(raw_provenance)
    document = status.build_status([rewarded, provenance])
    assert document["provenance"]["rewarded_set_full"] == "PASS"
    assert document["provenance"]["positive_tdx_raw_replay"] == "PASS"
    assert document["provenance"]["whole_epoch_full"] == "NOT_PROVEN"

    prose_only = status.clean_event(
        _event(
            "PROVENANCE_AUDIT_NOT_PROVEN",
            "NOT_PROVEN",
            detail="positive raw evidence replayed for a private identifier",
        )
    )
    assert (
        status.build_status([prose_only])["provenance"]["positive_tdx_raw_replay"]
        == "NOT_PROVEN"
    )


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
    published_status = json.loads((logs / "status.json").read_text(encoding="utf-8"))
    publication = published_status["publication"]
    assert publication["phase"] == "COMMITTED"
    assert publication["events_jsonl_sha256"] == (
        "sha256:"
        + hashlib.sha256((logs / "validator-events.jsonl").read_bytes()).hexdigest()
    )
    assert publication["events_log_sha256"] == (
        "sha256:"
        + hashlib.sha256((logs / "validator-events.log").read_bytes()).hexdigest()
    )


def test_interrupted_public_generation_replaces_stale_pass_with_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = tmp_path / "raw.jsonl"
    source = tmp_path / "status-source.jsonl"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(source),
        tty=None,
    )
    logger.event(
        "STARTUP",
        stage="startup",
        status="INFO",
        authority="thin",
        provenance_mode="shadow",
    )
    logger.event(
        "WEIGHTS_SUBMITTED",
        stage="submit",
        status="PASS",
        authority="thin",
        uid_count=2,
        burn_uid=204,
        burn_share=0.1,
        uid_weights={"163": 0.9, "204": 0.1},
    )
    root = tmp_path / "public"
    logs = root / "logs"
    logs.mkdir(parents=True, mode=0o755)
    (root / "index.json").write_text('{"recent":[]}', encoding="utf-8")
    (root / "index.json").chmod(0o600)
    monkeypatch.setattr(status, "SOURCE", source)
    monkeypatch.setattr(status, "INDEX", root / "index.json")
    monkeypatch.setattr(status, "RELEASE", root / "release.json")
    monkeypatch.setattr(status, "LOG_ROOT", logs)

    assert status.main() == 0
    prior = json.loads((logs / "status.json").read_text(encoding="utf-8"))
    assert prior["publication"]["phase"] == "COMMITTED"
    assert prior["authority"]["status"] == "PASS"

    durable_write = event_stream._durable_jsonl_write

    def fail_status_commit(target, record):
        if (
            target is logger._status_file
            and record.get("publication_phase") == "COMMITTED"
        ):
            raise OSError("injected source status commit failure")
        durable_write(target, record)

    monkeypatch.setattr(event_stream, "_durable_jsonl_write", fail_status_commit)
    with pytest.raises(OSError, match="injected source"):
        logger.event(
            "TICK_FAILED",
            stage="result",
            status="FAIL",
            detail="validator-local failure",
        )
    logger.close()

    atomic_write = status.atomic_write

    def fail_between_public_views(path: Path, data: bytes) -> None:
        if path.name == "validator-events.log":
            raise OSError("injected public view failure")
        atomic_write(path, data)

    monkeypatch.setattr(status, "atomic_write", fail_between_public_views)
    with pytest.raises(OSError, match="injected public view"):
        status.main()

    interrupted = json.loads((logs / "status.json").read_text(encoding="utf-8"))
    assert interrupted["publication"]["phase"] == "PENDING"
    assert (
        interrupted["publication"]["generation"] != (prior["publication"]["generation"])
    )
    assert interrupted["authority"]["status"] == "NOT_PROVEN"
    assert interrupted["authority"]["burn_share"] is None
    assert interrupted["authority"]["latest_event"] == ("STATUS_PUBLICATION_PENDING")
    assert interrupted["provenance"]["current_whole_epoch_full"] == "NOT_PROVEN"
    assert (
        "trust event views only when phase is COMMITTED"
        in (interrupted["publication"]["reader_rule"])
    )
    assert (
        b"STATUS_PUBLICATION_PENDING" in (logs / "validator-events.jsonl").read_bytes()
    )


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
