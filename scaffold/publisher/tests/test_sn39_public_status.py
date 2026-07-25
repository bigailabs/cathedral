from __future__ import annotations

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


def test_event_status_mismatch_is_dropped() -> None:
    assert status.clean_event(_event("WEIGHTS_SUBMITTED", "FAIL")) is None
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
