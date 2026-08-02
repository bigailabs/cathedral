"""Raw validator events must never widen the public status stream."""

from __future__ import annotations

import grp
import json
import os
import stat

import pytest

from scaffold import events as event_stream
from scaffold.events import STATUS_FIELDS, EventLogger, sanitized_status_record


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_arbitrary_event_fields_never_enter_the_status_stream(tmp_path):
    raw = tmp_path / "validator-events.jsonl"
    status = tmp_path / "validator-status.jsonl"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(status),
        tty=None,
    )
    logger.event(
        "CHAIN_SUBMITTED",
        stage="submit",
        status="PASS",
        hotkey="5FF6FtDUhn7XdPYmEdH5XjLAmLfmwLTCNVBgcrj3A4sstwaw",
        artifact="sha256:" + "a" * 64,
        detail="uids=2",
        receipt_body="SECRET-RECEIPT-PAYLOAD",
        evidence_blob="SECRET-EVIDENCE",
        future_unreviewed_field={"nested": "SECRET-FUTURE-VALUE"},
    )
    logger.close()

    assert _mode(raw) == 0o600
    assert _mode(status) == 0o600

    raw_record = json.loads(raw.read_text(encoding="utf-8"))
    status_records = [
        json.loads(line) for line in status.read_text(encoding="utf-8").splitlines()
    ]
    assert status_records[0]["event"] == "STATUS_PUBLICATION_PENDING"
    assert status_records[0]["status"] == "NOT_PROVEN"
    assert status_records[0]["target_event"] == "PRIVATE_EVENT"
    status_record = status_records[-1]
    assert status_record["publication_phase"] == "COMMITTED"
    assert raw_record["receipt_body"] == "SECRET-RECEIPT-PAYLOAD"
    assert raw_record["future_unreviewed_field"]["nested"] == "SECRET-FUTURE-VALUE"

    assert set(status_record) <= set(STATUS_FIELDS)
    for field in (
        "hotkey",
        "receipt_body",
        "evidence_blob",
        "future_unreviewed_field",
    ):
        assert field not in status_record
    serialized = json.dumps(status_record)
    for value in (
        "SECRET-RECEIPT-PAYLOAD",
        "SECRET-EVIDENCE",
        "SECRET-FUTURE-VALUE",
        "5FF6FtDUhn7XdPYmEdH5XjLAmLfmwLTCNVBgcrj3A4sstwaw",
    ):
        assert value not in serialized


def test_projection_shape_does_not_expand_with_the_raw_schema():
    projected = sanitized_status_record(
        {
            "ts": "2026-07-27T00:00:00.000Z",
            "event": "STARTUP",
            "stage": "startup",
            "mode": "thin",
            "status": "INFO",
            "hotkey": "5xxxx",
            "unknown_later_field": "leak",
        }
    )
    assert projected == {
        "ts": "2026-07-27T00:00:00.000Z",
        "event": "STARTUP",
        "stage": "startup",
        "mode": "thin",
        "status": "INFO",
    }


def test_group_readable_status_does_not_make_raw_journal_group_readable(tmp_path):
    raw = tmp_path / "validator-events.jsonl"
    status = tmp_path / "validator-status.jsonl"
    group = grp.getgrgid(os.getegid()).gr_name
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(status),
        status_group=group,
        tty=None,
    )
    logger.event("STARTUP", stage="startup", status="INFO", arbitrary="private")
    logger.close()

    assert _mode(raw) == 0o600
    assert _mode(status) == 0o640
    status_records = [
        json.loads(line) for line in status.read_text(encoding="utf-8").splitlines()
    ]
    assert all("arbitrary" not in row for row in status_records)


def test_existing_group_readable_raw_journal_is_refused_without_explicit_group(
    tmp_path,
):
    raw = tmp_path / "validator-events.jsonl"
    raw.touch()
    raw.chmod(0o640)
    with pytest.raises(ValueError, match="private \\(0600\\) without a reader group"):
        EventLogger(mode="thin", jsonl_path=str(raw), tty=None)


def test_raw_and_status_streams_cannot_alias_the_same_inode(tmp_path):
    raw = tmp_path / "validator-events.jsonl"
    raw.touch(mode=0o600)
    with pytest.raises(ValueError, match="must be distinct"):
        EventLogger(
            mode="thin",
            jsonl_path=str(raw),
            status_path=str(raw),
            tty=None,
        )


def test_event_stream_refuses_hardlinked_journal(tmp_path):
    raw = tmp_path / "validator-events.jsonl"
    raw.touch(mode=0o600)
    alias = tmp_path / "alias.jsonl"
    os.link(raw, alias)
    with pytest.raises(ValueError, match="owner-controlled"):
        EventLogger(mode="thin", jsonl_path=str(raw), tty=None)


def test_free_form_fields_and_embedded_hotkeys_never_enter_status(tmp_path):
    raw = tmp_path / "validator-events.jsonl"
    status = tmp_path / "validator-status.jsonl"
    hotkey = "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(status),
        tty=None,
    )
    logger.event(
        "TICK_FAILED",
        stage="result",
        status="FAIL",
        hotkey=hotkey,
        artifact=f"private-{hotkey}",
        detail=f"failure for {hotkey}",
        remediation=f"inspect {hotkey}",
        nested={"identifier": hotkey},
    )
    logger.close()

    public = status.read_text(encoding="utf-8")
    assert hotkey not in public
    assert "failure for" not in public
    assert "inspect" not in public
    assert "nested" not in public
    assert "artifact" not in json.loads(public.splitlines()[-1])


def test_caller_controlled_stage_cannot_cross_the_public_boundary(tmp_path):
    raw = tmp_path / "validator-events.jsonl"
    status = tmp_path / "validator-status.jsonl"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(status),
        tty=None,
    )
    caller_value = "secret_token_0123456789"
    logger.event(
        "VECTOR_ACCEPTED",
        stage=caller_value,
        status="PASS",
    )
    logger.close()

    public = status.read_text(encoding="utf-8")
    assert caller_value not in public
    assert json.loads(public.splitlines()[-1])["stage"] == "unknown"


def test_event_logger_rejects_unreviewed_mode_before_opening_outputs(tmp_path):
    raw = tmp_path / "validator-events.jsonl"
    with pytest.raises(ValueError, match="reviewed authority mode"):
        EventLogger(
            mode="5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC",
            jsonl_path=str(raw),
            tty=None,
        )
    assert not raw.exists()


def test_crash_fence_maps_caller_event_and_never_preserves_private_text(
    tmp_path,
    monkeypatch,
):
    raw = tmp_path / "validator-events.jsonl"
    status = tmp_path / "validator-status.jsonl"
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(raw),
        status_path=str(status),
        tty=None,
    )
    caller_event = "CALLER_CONTROLLED_PRIVATE_EVENT"
    hotkey = "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"
    durable_write = event_stream._durable_jsonl_write

    def fail_commit(target, record):
        if (
            target is logger._status_file
            and record.get("publication_phase") == "COMMITTED"
        ):
            raise OSError("injected commit crash")
        durable_write(target, record)

    monkeypatch.setattr(event_stream, "_durable_jsonl_write", fail_commit)
    with pytest.raises(OSError, match="injected commit"):
        logger.event(
            caller_event,
            stage="result",
            status="FAIL",
            detail=f"private result for {hotkey}",
            hotkey=hotkey,
        )
    logger.close()

    fence = json.loads(status.read_text(encoding="utf-8"))
    assert fence["event"] == "STATUS_PUBLICATION_PENDING"
    assert fence["mode"] == "thin"
    assert fence["target_event"] == "PRIVATE_EVENT"
    serialized = json.dumps(fence)
    assert caller_event not in serialized
    assert hotkey not in serialized
    assert "private result" not in serialized
