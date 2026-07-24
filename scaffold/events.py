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
from datetime import UTC, datetime
from typing import IO, Any

PASS = "PASS"
FAIL = "FAIL"
NOT_PROVEN = "NOT_PROVEN"
INFO = "INFO"
_STATUSES = (PASS, FAIL, NOT_PROVEN, INFO)

_EVENT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
# Full credential grammar: key=value / key: value forms AND scheme-prefixed
# header values ("Authorization: Bearer <secret>", "Basic <secret>").
_SECRET_RE = re.compile(
    r"(?i)(bearer|basic|token|secret|hmac|api_key|authorization|password|"
    r"private_key)((\s*[=:]\s*)|\s+)(?:(?:bearer|basic)\s+)?\S+"
)
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)^(authorization|.*(token|secret|password|credential|api_key|"
    r"private_key|hmac).*)$"
)

_COLORS = {
    PASS: "\x1b[32m",
    FAIL: "\x1b[31;1m",
    NOT_PROVEN: "\x1b[33m",
    INFO: "\x1b[2m",
}
_RESET = "\x1b[0m"


def _now_iso() -> str:
    dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _neutralize(value: str) -> str:
    """Strip ANSI/control characters, redact secrets, bound the length."""
    cleaned = _CONTROL_RE.sub(" ", value)
    cleaned = _SECRET_RE.sub(
        lambda match: (match.group(1) or "credential") + "=[REDACTED]", cleaned
    )
    return cleaned[:2048]


def _scrub(value):
    """Recursive scrub of every string in nested dict/list payloads."""
    if isinstance(value, str):
        return _neutralize(value)
    if isinstance(value, dict):
        # Sensitive FIELD NAMES redact the entire value regardless of shape.
        return {
            _neutralize(str(key)): (
                "[REDACTED]"
                if _SENSITIVE_FIELD_RE.match(str(key))
                else _scrub(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _neutralize(str(value))


def _redact(value: str) -> str:
    return _neutralize(value)


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
        self.mode = _neutralize(mode)[:32]
        self._jsonl = jsonl
        self._jsonl_file: IO[str] | None = None
        if jsonl_path:
            # Secure append: refuse symlinks/non-regular files, create 0600,
            # refuse group/world-accessible existing logs.
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(jsonl_path, flags, 0o600)
            import stat as _stat

            opened = os.fstat(descriptor)
            if not _stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o077:
                os.close(descriptor)
                raise ValueError(
                    "event log must be a private (0600) regular file"
                )
            self._jsonl_file = os.fdopen(descriptor, "a", encoding="utf-8")
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
            "stage": _neutralize(stage)[:32],
            "mode": self.mode,
            "status": status,
        }
        if hotkey is not None:
            record["hotkey"] = _neutralize(hotkey)
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
                record[key] = _scrub(value)
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
