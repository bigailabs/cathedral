"""Raw validator events must never widen the public status stream."""

from __future__ import annotations

import grp
import json
import os
import stat

import pytest

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
    status_record = json.loads(status.read_text(encoding="utf-8"))
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
    assert "arbitrary" not in json.loads(status.read_text(encoding="utf-8"))


def test_existing_group_readable_raw_journal_is_refused_without_explicit_group(
    tmp_path,
):
    raw = tmp_path / "validator-events.jsonl"
    raw.touch()
    raw.chmod(0o640)
    with pytest.raises(ValueError, match="private \\(0600\\) without a reader group"):
        EventLogger(mode="thin", jsonl_path=str(raw), tty=None)
