"""Validator event streams: stable JSONL plus an ergonomic TTY line.

Field-compatible with ``cathedral.events`` in the cathedralconfidential repo
(one emitter, two views), but dependency-light: a thin-only validator install
must not need the provenance extra to produce structured logs.

Every event carries a UTC timestamp, a stable UPPER_SNAKE event code, a
stage, the emitting validator mode, a PASS/FAIL/NOT_PROVEN/INFO status, and
optionally: miner hotkey, duration, an evidence/artifact reference, and
remediation guidance. Credential-shaped values are redacted defensively.

Watch commands (documented in VALIDATOR.md):

    journalctl -fu cathedral-validator -o cat        # TTY view
    tail -f ~/.cathedral/validator-events.jsonl | jq  # JSONL view
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import timezone, datetime
from typing import Any, IO

PASS = "PASS"
FAIL = "FAIL"
NOT_PROVEN = "NOT_PROVEN"
INFO = "INFO"
_STATUSES = (PASS, FAIL, NOT_PROVEN, INFO)

_EVENT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SECRET_RE = re.compile(
    r"(?i)(bearer|token|secret|hmac|api_key|authorization|password|private_key)"
    r"\s*[=:]\s*\S+"
)

_COLORS = {
    PASS: "\x1b[32m",
    FAIL: "\x1b[31;1m",
    NOT_PROVEN: "\x1b[33m",
    INFO: "\x1b[2m",
}
_RESET = "\x1b[0m"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _redact(value: str) -> str:
    return _SECRET_RE.sub(lambda match: match.group(1) + "=[REDACTED]", value)


class EventLogger:
    def __init__(
        self,
        *,
        mode: str,
        jsonl: IO[str] | None = None,
        jsonl_path: str | None = None,
        tty: IO[str] | None = None,
        color: bool | None = None,
    ) -> None:
        self.mode = mode
        self._jsonl = jsonl
        self._jsonl_file: IO[str] | None = None
        if jsonl_path:
            self._jsonl_file = open(jsonl_path, "a", encoding="utf-8")  # noqa: SIM115
        self._tty = tty if tty is not None else sys.stdout
        if color is None:
            color = (
                hasattr(self._tty, "isatty")
                and self._tty.isatty()
                and not os.environ.get("NO_COLOR")
            )
        self._color = bool(color)
        self._is_tty = bool(hasattr(self._tty, "isatty") and self._tty.isatty())

    def close(self) -> None:
        if self._jsonl_file is not None:
            self._jsonl_file.close()
            self._jsonl_file = None

    def event(
        self,
        code: str,
        *,
        stage: str,
        status: str = INFO,
        hotkey: str | None = None,
        duration_ms: float | None = None,
        artifact: str | None = None,
        remediation: str | None = None,
        detail: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        if _EVENT_CODE_RE.fullmatch(code) is None:
            raise ValueError(f"unstable event code {code!r}")
        if status not in _STATUSES:
            raise ValueError(f"unknown status {status!r}")
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "event": code,
            "stage": stage,
            "mode": self.mode,
            "status": status,
        }
        if hotkey is not None:
            record["hotkey"] = hotkey
        if duration_ms is not None:
            record["duration_ms"] = round(float(duration_ms), 3)
        if artifact is not None:
            record["artifact"] = _redact(str(artifact))
        if detail is not None:
            record["detail"] = _redact(str(detail))
        if remediation is not None:
            record["remediation"] = _redact(str(remediation))
        for key, value in fields.items():
            if key not in record:
                record[key] = _redact(value) if isinstance(value, str) else value
        line = json.dumps(record, separators=(",", ":"))
        for target in (self._jsonl, self._jsonl_file):
            if target is not None:
                target.write(line + "\n")
                target.flush()
        self._write_tty(record)
        return record

    def _write_tty(self, record: dict[str, Any]) -> None:
        if self._tty is None or not self._is_tty:
            return
        status = record["status"]
        badge = f"{status:<10}"
        if self._color:
            badge = _COLORS[status] + badge + _RESET
        clock = record["ts"][11:23]
        parts = [f"{clock} {badge} {record['event']:<28} [{record['mode']}]"]
        if "hotkey" in record:
            hotkey = record["hotkey"]
            parts.append(hotkey if len(hotkey) <= 12 else f"{hotkey[:6]}..{hotkey[-4:]}")
        if "duration_ms" in record:
            parts.append(f"{record['duration_ms']:.0f}ms")
        if "detail" in record:
            parts.append(str(record["detail"]))
        if "artifact" in record:
            parts.append(f"ref={record['artifact']}")
        line = "  ".join(parts)
        if record.get("remediation"):
            line += f"\n{'':>13}↳ {record['remediation']}"
        self._tty.write(line + "\n")
        self._tty.flush()
