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

import grp
import json
import math
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
# Full credential grammar: bare and QUOTED values (single/double quotes,
# JSON-serialized and Python-repr forms, values containing spaces),
# scheme-prefixed opaque header values, and URL-safe tokens.
_SECRET_RE = re.compile(
    r"(?i)([\"']?)(bearer|basic|token|secret|hmac|api_key|authorization|"
    r"password|private_key)\1((\s*[=:]\s*)|\s+)"
    r"(?:(?:bearer|basic)\s+)?"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
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


_PATH_RE = re.compile(
    r"(?:file |path )?"
    r"(?:(?<![A-Za-z0-9:/])/(?!/)|~[A-Za-z0-9_-]*(?:/|\\)|"
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/])"
    r"[^\s'\"]*"
)
# Greedy through the FINAL `@` in the authority. A malformed raw password may
# itself contain `@`; stopping at the first one would leak the credential
# suffix as a fake hostname in the public event stream.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s]+@")
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+")
# If a malformed URL contains authority credentials, query, or fragment and
# then whitespace, token boundaries are no longer trustworthy. Redact the
# remainder of that line rather than leaking a whitespace-separated suffix.
_SENSITIVE_URL_LINE_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\r\n'\"]*[@?#][^\r\n'\"]*"
)


def _scrub_url(match: re.Match[str]) -> str:
    """Keep a URL useful for diagnosis without retaining credentials.

    Query strings and fragments are removed wholesale rather than trying to
    enumerate every signed-URL vocabulary (``apikey``, ``sig``, ``X-Amz-*``,
    and vendor-specific variants). Userinfo is greedy through the final ``@``
    so malformed passwords cannot leak a suffix as a fake hostname.
    """
    raw = match.group(0)
    base = raw.split("#", 1)[0].split("?", 1)[0]
    return _URL_USERINFO_RE.sub(r"\1[REDACTED]@", base)


def _neutralize(value: str) -> str:
    """Strip ANSI/control characters, redact secrets and absolute
    filesystem paths/usernames, bound the length."""
    cleaned = _CONTROL_RE.sub(" ", value)
    cleaned = _SENSITIVE_URL_LINE_RE.sub("<redacted-url>", cleaned)
    cleaned = _URL_RE.sub(_scrub_url, cleaned)
    cleaned = _SECRET_RE.sub(
        lambda match: (match.group(2) or "credential") + "=[REDACTED]", cleaned
    )
    cleaned = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", cleaned)
    cleaned = _PATH_RE.sub("<path>", cleaned)
    return cleaned[:2048]


def stable_error(exc: BaseException) -> str:
    """A stable, redacted error code for output surfaces: OS errors become
    errno codes (no paths, no usernames), everything else keeps its type
    name with a neutralized message."""
    if isinstance(exc, OSError) and exc.errno is not None:
        import errno as errno_module

        code = errno_module.errorcode.get(exc.errno, str(exc.errno))
        return f"OSError[{code}]"
    return f"{type(exc).__name__}: {_neutralize(str(exc))}"[:200]


def _scrub(value):
    """Recursive scrub of every string in nested dict/list payloads."""
    if isinstance(value, str):
        return _neutralize(value)
    if isinstance(value, dict):
        # Sensitive FIELD NAMES redact the entire value regardless of shape.
        return {
            _neutralize(str(key)): (
                "[REDACTED]" if _SENSITIVE_FIELD_RE.match(str(key)) else _scrub(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "[NON_FINITE]"
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
        jsonl_group: str | None = None,
        tty: IO[str] | None = None,
        color: bool | None = None,
    ) -> None:
        self.mode = _neutralize(mode)[:32]
        self._jsonl = jsonl
        self._jsonl_file: IO[str] | None = None
        if jsonl_path:
            group_gid = (
                grp.getgrnam(jsonl_group).gr_gid if jsonl_group is not None else None
            )
            # Secure append: refuse symlinks/non-regular files, create 0600,
            # and permit 0640 only when an explicit reader group is pinned by
            # the service. This lets a separate sanitizer read the private
            # source without giving the validator access to the public tree.
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(jsonl_path, flags, 0o600)
            import stat as _stat

            try:
                opened = os.fstat(descriptor)
                opened_mode = _stat.S_IMODE(opened.st_mode)
                if (
                    not _stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or opened_mode & 0o007
                    or opened_mode not in (0o600, 0o640)
                ):
                    raise ValueError(
                        "event log must be an owner-controlled regular file"
                    )
                if group_gid is None:
                    if opened_mode & 0o070:
                        raise ValueError(
                            "event log must be private (0600) without a reader group"
                        )
                else:
                    os.fchown(descriptor, -1, group_gid)
                    os.fchmod(descriptor, 0o640)
                    secured = os.fstat(descriptor)
                    if (
                        secured.st_gid != group_gid
                        or _stat.S_IMODE(secured.st_mode) != 0o640
                    ):
                        raise ValueError("event log reader-group setup failed")
            except BaseException:
                os.close(descriptor)
                raise
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
            parsed_duration = float(duration_ms)
            if math.isfinite(parsed_duration):
                record["duration_ms"] = round(parsed_duration, 3)
        if artifact is not None:
            record["artifact"] = _redact(str(artifact))
        if detail is not None:
            record["detail"] = _redact(str(detail))
        if remediation is not None:
            record["remediation"] = _redact(str(remediation))
        for key, value in fields.items():
            if key not in record:
                record[key] = _scrub(value)
        line = json.dumps(record, separators=(",", ":"), allow_nan=False)
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
            parts.append(
                hotkey if len(hotkey) <= 12 else f"{hotkey[:6]}..{hotkey[-4:]}"
            )
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
