"""The thin v4 validator — the WHOLE validator, ~200 lines.

    fetch signed scores from the orchestrator
    -> verify Ed25519 signature against the pinned key
    -> sanity-check (finite, nonnegative, fresh, right subnet, no rollback)
    -> apply burn FROM THE SAME SIGNED PAYLOAD
    -> map hotkeys to uids against the live metagraph
    -> set weights

No local row database. No backfill. No rolling window. No score buckets.
Every scoring decision (recency, multi-lane composition, burn) lives
orchestrator-side and changes WITHOUT a validator release; this binary only
enforces that what it applies is exactly what the pinned key signed.

Run:  python -m scaffold.validator_thin --publisher-url https://api.cathedral.computer \
          --public-key-hex <pinned hex> [--once] [--broadcast]

Dry-run by default (computes + prints the uid vector, does not submit).
Rollback fence state persists in a small JSON file (--state-file), so a
publisher cannot re-serve an older policy_version after a restart.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import wire_vector as wire
from .chain import CHAIN_ENDPOINT_ENV, connection_target
from .events import FAIL, INFO, NOT_PROVEN, PASS, EventLogger, stable_error
from .provenance_audit import (
    MECHANISM_DEFAULT,
    ProvenanceSettings,
    run_audit,
)

# Cathedral's published weight-policy signing key (kid: cathedral-weight-policy).
# This is a PUBLIC verification key — shipping it as the default means operators
# don't have to pin it by hand; the validator still applies only what this key
# signed. Verify it any time against
# https://api.cathedral.computer/.well-known/cathedral-jwks.json
# Override with --public-key-hex or CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY.
DEFAULT_PUBLIC_KEY_HEX = (
    "10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26"
)

FINNEY_GENESIS_HASH = (
    "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
)

# Every trust-bearing field in the supported SN39 profile is immutable in the
# release, not merely a convenient config default.
SN39_PUBLISHER_URL = "https://api.cathedral.computer"
SN39_EVIDENCE_URL = "https://api.cathedral.computer/v1/evidence"
SN39_WEIGHT_POLICY_KEY_ID = "cathedral-weight-policy"
SN39_REGISTRY_KEYS_DIGEST = (
    "sha256:5fb8f00cd2541606927373f596c2ba77d4ce485df0539f4afd5091858af48512"
)
SN39_REPORT_KEYS_DIGEST = (
    "sha256:30e438fff5b0508402b233eb5eec590a834882801a552edbbf7e62e45cf98c70"
)
SN39_INDEX_KEYS_DIGEST = (
    "sha256:1e35b9ce36b3da3362a88feb93dfa90f1fe03ab7c42e902b13ac3789324f7611"
)
SN39_VERIFIER_DIGEST = (
    "sha256:8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
)
SN39_PRODUCER_REVISION = "655c264421a1f5f2e625a372a40f595aa1e114ab"
SN39_BURN_HOTKEY = "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"
SN39_STATE_FILE = Path("/var/lib/cathedral-validator/thin-state.json")
SN39_LAUNCH_CONTROLLED_DIR = Path("/etc/cathedral/controlled/sn39-launch")
SN39_LAUNCH_VERIFIER_BINARY = Path("/opt/cathedral-sn39/bin/cathedral-tdx-verifier")
SN39_LAUNCH_APPROVAL_FILE = Path("/etc/cathedral/controlled/sn39-launch-approval.json")
SN39_LAUNCH_APPROVAL_SCHEMA = "cathedral_sn39_launch_approval_v1"
SN39_LAUNCH_APPROVAL_LIFETIME_BLOCKS = 64
SN39_LAUNCH_APPROVAL_MAX_BYTES = 256 * 1024
SN39_LAUNCH_APPROVAL_OWNER_UID = 0
SN39_RELEASE_SHA_ENV = "CATHEDRAL_SN39_RELEASE_SHA"
SN39_LAUNCH_CONFIG_DIGEST_ENV = "CATHEDRAL_SN39_LAUNCH_CONFIG_SHA256"


class _RetryablePreSignHeadDrift(wire.VectorError):
    """The proven head changed before any signed intent or broadcast existed."""


class _PendingReceiptNotProven(wire.VectorError):
    """A signed attempt may have finalized, but archive proof is unavailable."""


class _PostSignedSubmissionMismatch(wire.VectorError):
    """A signed attempt has a positive receipt or execution contradiction."""


EXPIRED_WITHOUT_INCLUSION = "expired_without_inclusion"
SN39_PRE_SIGN_HEAD_DRIFT_RETRIES = 1


def _ms_iso_now() -> str:
    dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _lifecycle(event: str, detail: str = "") -> None:
    """Compact timestamped ASCII lifecycle event. No secrets.

    Format: ``<ts> <EVENT> <detail>`` — one line per state transition
    (VECTOR accepted/rejected, MAP complete, WEIGHTS dry-run, CHAIN
    submitted/failed).
    """
    from .events import _neutralize

    line = f"{_ms_iso_now()} {_neutralize(event)}"
    if detail:
        line += f" {_neutralize(detail)}"
    print(line)


def _feed_label(publisher_url: str) -> str:
    """Return a log-safe feed identity without credentials, query, or fragment."""
    parsed = urlsplit(publisher_url)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or "<invalid-host>"
    try:
        port = parsed.port
    except ValueError:
        port = None
    suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{host}{suffix}"


def _safe_endpoint_label(value: Any) -> str | None:
    """Return only a validated endpoint identity for structured telemetry.

    Configuration is logged before the first fetch. Never put the raw value in
    an event: a malformed URL can contain whitespace that defeats ordinary URL
    tokenization while still carrying credentials or a signed query.
    """
    if value is None:
        return None
    if not isinstance(value, str) or any(
        character.isspace() or character == "\\" for character in value
    ):
        return "<invalid-endpoint>"
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            return "<invalid-endpoint>"
        return _feed_label(value)
    except (TypeError, ValueError):
        return "<invalid-endpoint>"


MAX_VECTOR_FETCH_BYTES = 4 * 1024 * 1024


def fetch_vector(publisher_url: str, timeout: float = 30.0) -> dict[str, Any]:
    """Hardened bounded fetch of the thin feed (public HTTPS only).

    Beyond the scheme check: userinfo, query, fragment, and ambiguous URL
    shapes are rejected outright; EVERY resolved peer must be a public
    routable address (pooled bounded DNS); the TCP connection is pinned to
    the validated peer while TLS still verifies the certificate for the
    ORIGINAL hostname via SNI; ONE total deadline spans DNS, connect, TLS,
    request/headers, and every body read; redirects are never followed
    (any non-200 fails); the body is size-bounded; and the strict JSON
    parse rejects duplicate keys and non-finite numbers. Fail closed."""
    import http.client
    import ipaddress
    import socket
    import ssl

    from .provenance_audit import ProvenanceAuditError, _getaddrinfo_bounded

    if not isinstance(publisher_url, str) or any(
        character.isspace() or character == "\\" for character in publisher_url
    ):
        raise wire.VectorError("publisher URL is malformed")
    parsed = urlsplit(publisher_url.rstrip("/"))
    if parsed.scheme != "https":
        raise wire.VectorError("publisher URL must be https")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise wire.VectorError("publisher URL must be credential-free")
    if parsed.query or parsed.fragment:
        raise wire.VectorError("publisher URL must carry no query or fragment")
    host = parsed.hostname
    if not host:
        raise wire.VectorError("publisher URL has no host")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise wire.VectorError("publisher URL port is malformed") from exc
    target_path = (parsed.path or "") + "/v1/validator/weights/next"

    deadline = time.monotonic() + timeout

    def _phase_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise wire.VectorError("vector fetch exceeded its total deadline")
        return remaining

    try:
        infos = _getaddrinfo_bounded(host, port, _phase_timeout())
    except ProvenanceAuditError as exc:
        raise wire.VectorError(f"publisher DNS failed: {exc}") from exc
    if not infos:
        raise wire.VectorError("publisher host does not resolve")
    # EVERY resolved address is validated up front; only this validated,
    # order-preserving public list may ever be dialed (no private retry).
    peer_ips: list[str] = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise wire.VectorError(
                "publisher resolves to a non-public address; the production "
                "endpoint is public HTTPS"
            )
        if info[4][0] not in peer_ips:
            peer_ips.append(info[4][0])

    # ONE aggregate body budget spans every address attempt: a peer that
    # streams most of the cap and then dies cannot reset it by failing over.
    body_budget = {"bytes": MAX_VECTOR_FETCH_BYTES}

    class _PinnedConnection(http.client.HTTPSConnection):
        peer_ip = ""

        def connect(self) -> None:
            raw = socket.create_connection((self.peer_ip, port), _phase_timeout())
            # TLS must not inherit the connect phase's stale allowance; SNI
            # and certificate verification use the ORIGINAL hostname.
            raw.settimeout(_phase_timeout())
            self.sock = self._context.wrap_socket(raw, server_hostname=host)

    def _fetch_via(peer_ip: str) -> bytes:
        connection = _PinnedConnection(
            host, port, timeout=_phase_timeout(), context=ssl.create_default_context()
        )
        connection.peer_ip = peer_ip
        try:
            connection.connect()  # TCP + TLS under freshly computed bounds
            connection.sock.settimeout(_phase_timeout())
            connection.request(
                "GET",
                target_path,
                headers={"Host": host, "User-Agent": "cathedral-thin-validator/1.0"},
            )
            connection.sock.settimeout(_phase_timeout())
            response = connection.getresponse()
            if response.status != 200:
                raise wire.VectorError(
                    f"vector fetch failed with status {response.status} "
                    "(redirects are never followed)"
                )
            chunks: list[bytes] = []
            while True:
                connection.sock.settimeout(_phase_timeout())
                chunk = response.read(min(65536, body_budget["bytes"] + 1))
                if not chunk:
                    break
                body_budget["bytes"] -= len(chunk)
                if body_budget["bytes"] < 0:
                    raise wire.VectorError(
                        "vector response exceeds the bounded size limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            connection.close()

    data: bytes | None = None
    transport_failures: list[str] = []
    # Try every already validated public address under the one total
    # deadline. Only transport failures move on; a served response (any
    # status) is final, and redirects are never followed.
    for candidate_ip in peer_ips:
        _phase_timeout()
        try:
            data = _fetch_via(candidate_ip)
            break
        except OSError as exc:
            transport_failures.append(f"{candidate_ip}: {type(exc).__name__}")
    if data is None:
        raise wire.VectorError(
            "publisher unreachable on every validated address: "
            + "; ".join(transport_failures)
        )

    def _no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise wire.VectorError("vector JSON has duplicate keys")
            result[key] = value
        return result

    def _finite_float(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise wire.VectorError("vector JSON has non-finite numbers")
        return value

    document = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_no_duplicates,
        parse_float=_finite_float,
        parse_constant=lambda _v: (_ for _ in ()).throw(
            wire.VectorError("vector JSON has non-finite numbers")
        ),
    )
    if not isinstance(document, dict):
        raise wire.VectorError("vector payload is not a JSON object")
    return document


# -- rollback fence ------------------------------------------------------------


def _strict_state_document(payload: str) -> dict[str, Any]:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("validator state has duplicate keys")
            result[key] = value
        return result

    def finite_float(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("validator state has non-finite numbers")
        return value

    document = json.loads(
        payload,
        object_pairs_hook=no_duplicates,
        parse_float=finite_float,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("validator state has non-finite numbers")
        ),
    )
    if not isinstance(document, dict):
        raise TypeError("validator state file is corrupt")
    return document


def _canonical_json_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_document(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(document)).hexdigest()


def _strict_launch_approval_bytes(payload: bytes) -> dict[str, Any]:
    """Parse one byte-canonical launch approval with no ambiguous JSON."""

    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise wire.VectorError("launch approval has duplicate keys")
            result[key] = value
        return result

    try:
        text = payload.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_float=lambda raw: (
                float(raw)
                if math.isfinite(float(raw))
                else (_ for _ in ()).throw(
                    wire.VectorError("launch approval has non-finite numbers")
                )
            ),
            parse_constant=lambda _raw: (_ for _ in ()).throw(
                wire.VectorError("launch approval has non-finite numbers")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise wire.VectorError("launch approval is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise wire.VectorError("launch approval must be a JSON object")
    if payload != _canonical_json_bytes(document) + b"\n":
        raise wire.VectorError("launch approval is not byte-canonical JSON")
    return document


def _read_root_launch_approval(path: Path) -> dict[str, Any]:
    """Read one immutable operator approval without following links."""
    import stat as stat_module

    if not path.is_absolute():
        raise wire.VectorError("launch approval path must be absolute")
    try:
        parent = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise wire.VectorError("launch approval directory is unavailable") from exc
    try:
        parent_info = os.fstat(parent)
        if (
            not stat_module.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != SN39_LAUNCH_APPROVAL_OWNER_UID
            or stat_module.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise wire.VectorError("launch approval directory is not root-controlled")
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except OSError as exc:
            raise wire.VectorError("required launch approval is unavailable") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat_module.S_ISREG(info.st_mode)
                or info.st_uid != SN39_LAUNCH_APPROVAL_OWNER_UID
                or stat_module.S_IMODE(info.st_mode) & 0o022
            ):
                raise wire.VectorError(
                    "launch approval is not an immutable root-controlled file"
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                payload = handle.read(SN39_LAUNCH_APPROVAL_MAX_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent)
    if len(payload) > SN39_LAUNCH_APPROVAL_MAX_BYTES:
        raise wire.VectorError("launch approval exceeds its bounded size")
    return _strict_launch_approval_bytes(payload)


def _write_root_launch_approval(path: Path, document: dict[str, Any]) -> None:
    """Atomically emit the sole intended preflight mutation: the approval."""
    import stat as stat_module

    if not path.is_absolute():
        raise wire.VectorError("launch approval output path must be absolute")
    payload = _canonical_json_bytes(document) + b"\n"
    if len(payload) > SN39_LAUNCH_APPROVAL_MAX_BYTES:
        raise wire.VectorError("launch approval exceeds its bounded size")
    try:
        parent = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise wire.VectorError(
            "launch approval output directory is unavailable"
        ) from exc
    tmp_name = path.name + ".tmp"
    try:
        info = os.fstat(parent)
        if (
            not stat_module.S_ISDIR(info.st_mode)
            or info.st_uid != SN39_LAUNCH_APPROVAL_OWNER_UID
            or stat_module.S_IMODE(info.st_mode) & 0o022
        ):
            raise wire.VectorError(
                "launch approval output directory is not root-controlled"
            )
        try:
            os.unlink(tmp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(tmp_name, flags, 0o644, dir_fd=parent)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(tmp_name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(tmp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _open_private_lock(path: Path) -> int:
    import stat as stat_module

    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        mode = stat_module.S_IMODE(info.st_mode)
        if not stat_module.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("validator lock must be owner-controlled mode 0600")
        if mode != 0o600:
            if mode & 0o022:
                raise ValueError("validator lock must be owner-controlled mode 0600")
            os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_private_state_dir(path: Path) -> int:
    """Open one owner-only state directory without following its final link.

    Older validators commonly created ``0755`` state directories. A directory
    owned by this process and not writable by group/other can be tightened
    safely through the already-open descriptor. Writable or foreign-owned
    paths still fail closed.
    """
    import stat as stat_module

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        info = os.fstat(descriptor)
        mode = stat_module.S_IMODE(info.st_mode)
        if not stat_module.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError(
                "validator state directory must be owner-controlled mode 0700"
            )
        if mode != 0o700:
            if mode & 0o022:
                raise ValueError(
                    "validator state directory must be owner-controlled mode 0700"
                )
            os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_state(state_file: Path) -> dict[str, Any]:
    """Read the whole durable state document (fence + provenance chain).

    The file is opened once with ``O_NOFOLLOW`` and validated through that
    same descriptor. This avoids a check/use replacement window and refuses
    state another account can edit.
    """
    import stat as stat_module

    parent = _open_private_state_dir(state_file.parent)
    try:
        try:
            descriptor = os.open(
                state_file.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except FileNotFoundError:
            return {}
        try:
            info = os.fstat(descriptor)
            mode = stat_module.S_IMODE(info.st_mode)
            if not stat_module.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError(
                    "validator state must be an owner-controlled regular file mode 0600"
                )
            if mode != 0o600:
                if mode & 0o022:
                    raise ValueError(
                        "validator state must be an owner-controlled regular file "
                        "mode 0600"
                    )
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return _strict_state_document(handle.read())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent)


def _read_state_without_mutation(state_file: Path) -> dict[str, Any]:
    """Read validator state for root preflight without chmod/mkdir side effects."""
    import stat as stat_module

    if not state_file.is_absolute():
        raise wire.VectorError("validator state path must be absolute")
    try:
        parent = os.open(
            state_file.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise wire.VectorError("validator state directory is unavailable") from exc
    try:
        parent_info = os.fstat(parent)
        if (
            not stat_module.S_ISDIR(parent_info.st_mode)
            or stat_module.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise wire.VectorError("validator state directory is not owner-controlled")
        try:
            descriptor = os.open(
                state_file.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise wire.VectorError("validator state is unavailable") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat_module.S_ISREG(info.st_mode)
                or stat_module.S_IMODE(info.st_mode) != 0o600
            ):
                raise wire.VectorError(
                    "validator state is not an owner-controlled regular file mode 0600"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return _strict_state_document(handle.read())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent)


def _replace_private_state(state_file: Path, document: dict[str, Any]) -> None:
    """Atomically replace state relative to one verified directory descriptor."""
    parent = _open_private_state_dir(state_file.parent)
    tmp_name = state_file.name + ".tmp"
    try:
        try:
            os.unlink(tmp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(tmp_name, flags, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(json.dumps(document, indent=2, allow_nan=False))
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(
            tmp_name,
            state_file.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)
    finally:
        try:
            os.unlink(tmp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _state_policy_fence(document: dict[str, Any]) -> int:
    """Return the highest policy version that may already be on-chain.

    An irreversible call is ambiguous from the instant its intent is fsynced:
    the process may die after the chain accepts it but before final state
    persistence. Consequently the rollback fence includes both finalized
    versions and every thin version ever attempted, not just confirmed
    successes.
    """
    candidates = [-1]
    for key in (
        "last_accepted_policy_version",
        "highest_attempted_policy_version",
    ):
        if key not in document:
            continue
        value = document[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"validator state {key} is malformed")
        candidates.append(value)
    identity = document.get("thin_submission_identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise ValueError("validator state thin submission identity is malformed")
        value = identity.get("policy_version")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                "validator state thin submission policy version is malformed"
            )
        candidates.append(value)
    accepted = document.get("last_accepted_policy_version")
    attempted = document.get("highest_attempted_policy_version")
    if (
        isinstance(accepted, int)
        and isinstance(attempted, int)
        and attempted < accepted
    ):
        raise ValueError(
            "validator state attempted-policy fence regresses below accepted policy"
        )
    return max(candidates)


def _authority_lane_transition_authorized(
    state: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    """Allow only the reviewed one-way thin-to-FULL authority handoff.

    The authority process has already reproduced the root-signed launch seal
    and completed a current FULL audit before it can construct this identity.
    This check binds that authorization back to the durable launch journal.
    There is deliberately no automatic authority-to-thin transition.
    """
    authorization = identity.get("continuous_authorization")
    if not isinstance(authorization, dict):
        return False
    launch_attempt_id = state.get("submission_launch_attempt_id")
    launch_attempt_ids = state.get("submission_launch_attempt_ids")
    return bool(
        identity.get("network") == "finney"
        and identity.get("netuid") == 39
        and identity.get("validator_hotkey") == state.get("submission_validator_hotkey")
        and state.get("submission_genesis_hash") == FINNEY_GENESIS_HASH
        and state.get("provenance_netuid") == 39
        and state.get("submission_launch_status") == "finalized"
        and state.get("submission_continuous_enabled") is True
        and isinstance(launch_attempt_id, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", launch_attempt_id) is not None
        and isinstance(launch_attempt_ids, list)
        and launch_attempt_id in launch_attempt_ids
        and state.get("submission_continuous_launch_attempt_id") == launch_attempt_id
        and authorization.get("launch_attempt_id") == launch_attempt_id
        and authorization.get("release_sha256")
        == state.get("submission_continuous_release_sha256")
        and authorization.get("reproducer_revision")
        == state.get("submission_continuous_reproducer_revision")
        and authorization.get("validator_hotkey")
        == state.get("submission_validator_hotkey")
        and authorization.get("genesis_hash") == state.get("submission_genesis_hash")
        and isinstance(authorization.get("authorization_sha256"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", authorization["authorization_sha256"])
        is not None
        and isinstance(authorization.get("lanes"), list)
        and "authority" in authorization["lanes"]
        and isinstance(authorization.get("max_attempts"), int)
        and not isinstance(authorization["max_attempts"], bool)
        and 0 < authorization["max_attempts"] <= 96
        and isinstance(authorization.get("valid_from_nonce"), int)
        and not isinstance(authorization["valid_from_nonce"], bool)
        and authorization["valid_from_nonce"] >= 0
        and isinstance(authorization.get("valid_until_nonce_exclusive"), int)
        and not isinstance(authorization["valid_until_nonce_exclusive"], bool)
        and authorization["valid_until_nonce_exclusive"]
        == authorization["valid_from_nonce"] + authorization["max_attempts"]
    )


def _write_state_fenced(state_file: Path, updates: dict[str, Any]) -> None:
    """Atomic CHECK-AND-RESERVE under the state lock (authority path).

    The high-water comparison and the write happen inside ONE flock hold:
    a concurrent writer that reserved a newer epoch, an equivocating
    manifest, or a diverging policy/report line makes THIS reservation
    RAISE — a stale read can never overwrite or silently coexist.
    """
    import fcntl

    state_directory = _open_private_state_dir(state_file.parent)
    os.close(state_directory)
    lock_path = state_file.with_suffix(".lock")
    lock_descriptor = _open_private_lock(lock_path)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        current = _read_state(state_file)
        updates = dict(updates)
        receipt_submission_id = updates.pop("_record_receipt_for_submission_id", None)
        if receipt_submission_id is not None:
            if (
                not isinstance(receipt_submission_id, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", receipt_submission_id) is None
                or current.get("submission_pending_id") != receipt_submission_id
            ):
                raise ValueError(
                    "receipt candidate does not match the common pending fence"
                )
        finalize_submission_id = updates.pop("_finalize_submission_id", None)
        if finalize_submission_id is not None:
            if (
                not isinstance(finalize_submission_id, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", finalize_submission_id) is None
                or current.get("submission_pending_id") != finalize_submission_id
            ):
                raise ValueError(
                    "submission finalization does not match the common pending fence"
                )
            updates["submission_pending_id"] = None
        new_common_attempt = updates.get("submission_pending_id")
        if new_common_attempt is not None:
            provisional = updates.pop("_provisional_submission", False)
            allow_authority_transition = updates.pop(
                "_allow_authority_lane_transition",
                False,
            )
            if provisional is not True:
                raise ValueError(
                    "new common submissions must begin as unsigned reservations"
                )
            if not isinstance(allow_authority_transition, bool):
                raise ValueError("submission lane transition marker is malformed")
            if (
                not isinstance(new_common_attempt, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", new_common_attempt) is None
            ):
                raise ValueError("common submission attempt id is malformed")
            pending = current.get("submission_pending_id")
            if pending is not None:
                raise ValueError(
                    "a prior thin/full submission is pending reconciliation"
                )
            raw_common_history = current.get("submission_attempt_ids", [])
            if not isinstance(raw_common_history, list) or any(
                not isinstance(item, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                for item in raw_common_history
            ):
                raise ValueError("common submission attempt journal is corrupt")
            if new_common_attempt in raw_common_history:
                raise ValueError(
                    "this exact thin/full submission was already attempted"
                )
            lane = updates.get("submission_pending_lane")
            if lane not in ("thin", "authority"):
                raise ValueError("common submission lane is malformed")
            identity = updates.get("submission_pending_identity")
            if not isinstance(identity, dict):
                raise ValueError("common submission identity is malformed")
            active_lane = current.get("submission_active_lane")
            lane_transition_from: str | None = None
            if active_lane is not None and active_lane != lane:
                if (
                    allow_authority_transition
                    and active_lane == "thin"
                    and lane == "authority"
                    and _authority_lane_transition_authorized(current, identity)
                ):
                    lane_transition_from = "thin"
                else:
                    raise ValueError(
                        "submission authority lane changed without explicit operator "
                        f"reconciliation ({active_lane!r} -> {lane!r})"
                    )
            elif allow_authority_transition and lane != "authority":
                raise ValueError(
                    "only FULL authority may request a submission lane transition"
                )
            launch_attempt = updates.pop("_launch_attempt", False)
            if not isinstance(launch_attempt, bool):
                raise ValueError("launch attempt marker is malformed")
            budget_scope = updates.pop("_submission_budget_scope", None)
            budget_limit = updates.pop("_submission_budget_limit", None)
            if budget_scope is not None:
                if (
                    not isinstance(budget_scope, str)
                    or re.fullmatch(r"[a-z0-9_]{1,64}", budget_scope) is None
                    or isinstance(budget_limit, bool)
                    or not isinstance(budget_limit, int)
                    or budget_limit <= 0
                ):
                    raise ValueError("submission attempt budget scope is malformed")
                budgets = current.get("submission_attempt_budgets", {})
                if not isinstance(budgets, dict):
                    raise ValueError("submission attempt budgets are corrupt")
                budget = budgets.get(budget_scope, {"limit": budget_limit, "ids": []})
                if (
                    not isinstance(budget, dict)
                    or set(budget) != {"limit", "ids"}
                    or budget.get("limit") != budget_limit
                    or not isinstance(budget.get("ids"), list)
                    or any(
                        not isinstance(item, str)
                        or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                        for item in budget.get("ids", [])
                    )
                ):
                    raise ValueError("submission attempt budget changed or is corrupt")
                if len(budget["ids"]) >= budget_limit:
                    raise ValueError(
                        f"submission attempt budget {budget_limit} is exhausted"
                    )
            configured_limit = updates.pop("_launch_budget_limit", None)
            if launch_attempt:
                if (
                    isinstance(configured_limit, bool)
                    or not isinstance(configured_limit, int)
                    or configured_limit != 1
                ):
                    raise ValueError("launch submission budget must be exactly one")
                launch_history = current.get("submission_launch_attempt_ids", [])
                if not isinstance(launch_history, list) or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                    for item in launch_history
                ):
                    raise ValueError("launch submission attempt journal is corrupt")
                if launch_history:
                    raise ValueError("launch submission attempt budget 1 is exhausted")
            elif configured_limit is not None:
                raise ValueError("non-launch reservation carries a launch budget")
            policy_version = updates.pop("submission_highest_policy_version", None)
            source_epoch = updates.pop("submission_highest_source_epoch", None)
            if lane == "thin":
                if (
                    isinstance(policy_version, bool)
                    or not isinstance(policy_version, int)
                    or policy_version < 0
                ):
                    raise ValueError("common thin policy fence is malformed")
                prior_policy = current.get("submission_highest_policy_version")
                if isinstance(prior_policy, int) and policy_version <= prior_policy:
                    raise ValueError(
                        f"common thin policy rollback {policy_version} <= "
                        f"{prior_policy}"
                    )
            else:
                if (
                    isinstance(source_epoch, bool)
                    or not isinstance(source_epoch, int)
                    or source_epoch < 0
                ):
                    raise ValueError("common authority source-epoch fence is malformed")
                prior_source = current.get("submission_highest_source_epoch")
                if isinstance(prior_source, int) and source_epoch <= prior_source:
                    raise ValueError(
                        f"common authority epoch rollback {source_epoch} <= "
                        f"{prior_source}"
                    )
            # A pre-sign reservation linearizes the in-process decision under the
            # shared submission lock, but it is not yet an irreversible attempt.
            # Histories, rollback fences, and budgets are committed atomically
            # with the exact signed hash immediately before broadcast.
            for stale_key in (
                "submission_pending_broadcast_intent",
                "submission_pending_broadcast_started_at",
                "submission_pending_receipt_candidate",
                "submission_pending_receipt_recorded_at",
                "submission_pending_proof_status",
                "submission_pending_proof_checked_at",
                "submission_pending_lane_transition_from",
            ):
                current.pop(stale_key, None)
            updates.update(
                {
                    "submission_pending_phase": "unsigned_reserved",
                    "submission_pending_launch_attempt": launch_attempt,
                    "submission_pending_launch_budget_limit": configured_limit,
                    "submission_pending_budget_scope": budget_scope,
                    "submission_pending_budget_limit": budget_limit,
                    "submission_pending_policy_version": policy_version,
                    "submission_pending_source_epoch": source_epoch,
                    "submission_pending_lane_transition_from": lane_transition_from,
                }
            )
        if finalize_submission_id is not None:
            finalized_count = current.get("submission_finalized_count", 0)
            if (
                isinstance(finalized_count, bool)
                or not isinstance(finalized_count, int)
                or finalized_count < 0
            ):
                raise ValueError("common finalized-submission count is malformed")
            updates["submission_finalized_count"] = finalized_count + 1
        new_policy_fence = updates.get("highest_attempted_policy_version")
        if new_policy_fence is not None:
            if (
                isinstance(new_policy_fence, bool)
                or not isinstance(new_policy_fence, int)
                or new_policy_fence < 0
            ):
                raise ValueError("attempted policy version is malformed")
            stored_policy_fence = _state_policy_fence(current)
            if new_policy_fence <= stored_policy_fence:
                raise ValueError(
                    f"stale attempted policy version {new_policy_fence} <= "
                    f"durable fence {stored_policy_fence}"
                )
        new_epoch = updates.get("provenance_index_epoch")
        stored_epoch = current.get("provenance_index_epoch")
        if isinstance(new_epoch, int) and isinstance(stored_epoch, int):
            if new_epoch < stored_epoch:
                raise ValueError(
                    f"stale reservation: index epoch {new_epoch} < reserved "
                    f"{stored_epoch}"
                )
            if new_epoch == stored_epoch and current.get(
                "provenance_index_manifest"
            ) != updates.get("provenance_index_manifest"):
                raise ValueError(
                    "reservation equivocation: same epoch, different manifest"
                )
        new_release = updates.get("provenance_policy_release")
        stored_release = current.get("provenance_policy_release")
        if isinstance(new_release, int) and isinstance(stored_release, int):
            if new_release < stored_release:
                raise ValueError(
                    f"stale reservation: policy release {new_release} < "
                    f"reserved {stored_release}"
                )
            if new_release == stored_release and current.get(
                "provenance_policy_digest"
            ) != updates.get("provenance_policy_digest"):
                raise ValueError(
                    "reservation equivocation: same release, different digest"
                )
        new_source = updates.get("provenance_last_source_epoch")
        stored_source = current.get("provenance_last_source_epoch")
        if isinstance(new_source, int) and isinstance(stored_source, int):
            if new_source < stored_source:
                raise ValueError(
                    f"stale reservation: source epoch {new_source} < reserved "
                    f"{stored_source}"
                )
            if new_source == stored_source and current.get(
                "provenance_last_report_id"
            ) != updates.get("provenance_last_report_id"):
                raise ValueError(
                    "reservation equivocation: same source epoch, different report"
                )
        # Append-only attempted-ID journals prevent A -> B -> A replay, not
        # merely immediate duplicate A -> A. They are intentionally unbounded:
        # deleting an old irreversible attempt would reopen its retry window.
        # Existing single-ID state is folded into the journal on first write.
        for lane in ("authority", "thin"):
            attempt_key = f"{lane}_submission_attempt_id"
            history_key = f"{lane}_submission_attempt_ids"
            new_attempt = updates.get(attempt_key)
            if new_attempt is None:
                continue
            if (
                not isinstance(new_attempt, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", new_attempt) is None
            ):
                raise ValueError(f"{lane} submission attempt id is malformed")
            raw_history = current.get(history_key, [])
            if not isinstance(raw_history, list) or any(
                not isinstance(item, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                for item in raw_history
            ):
                raise ValueError(f"{lane} submission attempt journal is corrupt")
            history = list(raw_history)
            previous = current.get(attempt_key)
            if previous is not None:
                if (
                    not isinstance(previous, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", previous) is None
                ):
                    raise ValueError(f"{lane} prior submission attempt id is malformed")
                if previous not in history:
                    history.append(previous)
            if new_attempt in history:
                raise ValueError(
                    f"{lane} submission was already attempted for this exact "
                    "evidence/vector identity"
                )
            history.append(new_attempt)
            updates[history_key] = history
        for key in (
            "provenance_network",
            "provenance_netuid",
            "submission_genesis_hash",
            "submission_validator_hotkey",
        ):
            if key in updates and key in current and updates[key] != current[key]:
                raise ValueError(
                    f"reservation chain-identity mismatch: {key} "
                    f"{updates[key]!r} != reserved {current[key]!r}"
                )
        document = dict(current)
        document.update(updates)
        _replace_private_state(state_file, document)
    finally:
        os.close(lock_descriptor)


def _write_state(state_file: Path, updates: dict[str, Any]) -> None:
    """Locked atomic read-merge-write (0600, fsync, parent fsync) so the
    fence writer and the background shadow auditor never clobber each other
    and a crash mid-write can't corrupt the fail-closed load."""
    import fcntl

    state_directory = _open_private_state_dir(state_file.parent)
    os.close(state_directory)
    lock_path = state_file.with_suffix(".lock")
    lock_descriptor = _open_private_lock(lock_path)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        document = _read_state(state_file)
        document.update(updates)
        _replace_private_state(state_file, document)
    finally:
        os.close(lock_descriptor)


def load_fence(state_file: Path) -> int:
    """FAIL CLOSED: only a genuinely absent state file means 'no fence yet'.
    A corrupt/unreadable file raises (the tick fails) instead of silently
    resetting the fence to -1 and reopening the rollback window."""
    return _state_policy_fence(_read_state(state_file))


def save_fence(state_file: Path, version: int, vector_id: str) -> None:
    _write_state(
        state_file,
        {
            "last_accepted_policy_version": version,
            "last_vector_id": vector_id,
            "accepted_at": _ms_iso_now(),
        },
    )


# -- two-mode provenance --------------------------------------------------------


def _provenance_settings(args) -> ProvenanceSettings:
    mode = getattr(args, "provenance", "shadow") or "shadow"
    evidence_url = getattr(args, "evidence_url", None)
    evidence_dir = getattr(args, "evidence_dir", None)
    if mode != "off" and not evidence_url and not evidence_dir:
        evidence_url = args.publisher_url.rstrip("/") + "/v1/evidence"
    return ProvenanceSettings(
        mode=mode,
        evidence_url=evidence_url,
        evidence_dir=evidence_dir,
        registry_keys=getattr(args, "provenance_registry_keys", None),
        registry_keys_digest=getattr(args, "provenance_registry_keys_digest", None),
        report_keys=getattr(args, "provenance_report_keys", None),
        report_keys_digest=getattr(args, "provenance_report_keys_digest", None),
        index_keys=getattr(args, "provenance_index_keys", None),
        index_keys_digest=getattr(args, "provenance_index_keys_digest", None),
        verifier_digest=getattr(args, "provenance_verifier_digest", None),
        mechanism=getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
        or MECHANISM_DEFAULT,
        controlled_dir=getattr(args, "provenance_controlled_dir", None),
        verifier_binary=getattr(args, "provenance_verifier_binary", None),
        source_revision=getattr(args, "provenance_source_revision", None),
        allow_private_hosts=bool(
            getattr(args, "provenance_allow_private_hosts", False)
        ),
        index_max_age_secs=float(
            getattr(args, "provenance_index_max_age_secs", 3600.0)
        ),
    )


def _runtime_modes(args: Any) -> tuple[str, str]:
    """Return the truthful submission authority and provenance runtime mode."""
    provenance_mode = getattr(args, "provenance", "shadow") or "shadow"
    if provenance_mode not in {"off", "shadow", "authority"}:
        raise wire.VectorError(f"unsupported provenance mode {provenance_mode!r}")
    submission_authority = (
        "full_provenance" if provenance_mode == "authority" else "thin"
    )
    return submission_authority, provenance_mode


def _get_events(args) -> EventLogger:
    """One logger per process; authority is stamped on every record."""
    existing = getattr(args, "_events", None)
    if existing is not None:
        return existing
    authority, _provenance_mode = _runtime_modes(args)
    logger = EventLogger(
        mode=authority,
        jsonl_path=getattr(args, "jsonl", None) or None,
        jsonl_group=os.environ.get("CATHEDRAL_VALIDATOR_JSONL_GROUP") or None,
        tty=sys.stdout,
    )
    try:
        args._events = logger
    except AttributeError:  # frozen namespaces in tests
        pass
    return logger


# The versioned mechanism fixes the burn fraction; the burn DESTINATION is
# the operator's configured pin resolved against the live metagraph. The
# signed Cathedral vector's burn row is comparison input only — authority
# mode never derives allocation from it.
# Every supported mechanism pins its own burn contract. The fraction is always
# looked up by the operator's own pinned mechanism, never by the id the manifest
# claims, so MECHANISM_ACCEPTED widening which evidence is admitted can never let
# a producer move the burn.
MECHANISM_BURN_FRACTION = {
    "validated_supply_v1": 0.10,
    "validated_supply_v2": 0.10,
}


def _provenance_uid_weights(
    recomputed: dict[str, float],
    *,
    mechanism: str,
    burn_hotkey: str,
    hotkey_to_uid: dict[str, int],
) -> dict[int, float]:
    """Authority mode: the COMPLETE UID vector from OUR recomputation.

    Inputs are the pinned versioned mechanism's shares, the operator's
    configured burn hotkey, and the live chain metagraph — nothing from
    Cathedral's signed vector. All-or-nothing mapping; nonfinite, negative,
    duplicate, or unmappable weights reject the whole vector.
    """
    burn_fraction = MECHANISM_BURN_FRACTION.get(mechanism)
    if burn_fraction is None:
        raise wire.VectorError(f"mechanism {mechanism!r} has no pinned burn contract")
    if not isinstance(burn_hotkey, str) or not burn_hotkey:
        raise wire.VectorError(
            "authority mode requires --provenance-burn-hotkey (the configured "
            "burn destination; never taken from Cathedral's vector)"
        )
    if burn_hotkey not in hotkey_to_uid:
        raise wire.VectorError(
            f"configured burn hotkey {burn_hotkey!r} has no current metagraph UID"
        )
    burn_uid = hotkey_to_uid[burn_hotkey]

    scores: dict[int, float] = {}
    seen: set[int] = set()
    total = 0.0
    for hotkey, weight in sorted(recomputed.items()):
        value = float(weight)
        if not math.isfinite(value) or value < 0.0:
            raise wire.VectorError(
                f"recomputed weight for {hotkey!r} is non-finite or negative"
            )
        if value == 0.0:
            continue
        if hotkey not in hotkey_to_uid:
            raise wire.VectorError(
                f"provenance hotkey {hotkey!r} has no current metagraph UID"
            )
        uid = hotkey_to_uid[hotkey]
        if uid == burn_uid:
            raise wire.VectorError(
                f"provenance hotkey {hotkey!r} resolves to the burn UID"
            )
        if uid in seen:
            raise wire.VectorError(f"provenance duplicate UID {uid}")
        seen.add(uid)
        scores[uid] = value
        total += value
    if scores and not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise wire.VectorError(f"recomputed shares sum to {total!r}, expected 1.0")
    out = {uid: value * (1.0 - burn_fraction) for uid, value in scores.items()}
    out[burn_uid] = out.get(burn_uid, 0.0) + (burn_fraction if scores else 1.0)
    norm = math.fsum(out.values())
    return {uid: value / norm for uid, value in out.items()}


class _ShadowAuditor:
    """Single-flight background worker for the shadow provenance audit.

    tick() submits non-blocking; while an audit is in flight further
    submissions are skipped (single-flight). Results are drained and logged
    by the MAIN thread on a later tick, so a slow or broken audit can never
    delay, reorder, or fail the thin submission path.

    Completed results are LOSSLESS and exactly-once: they accumulate in a
    queue under the same lock drain() holds, so an audit that finishes
    between drain() and the next submit() can never be overwritten by a
    later completion — every completed audit is handed to exactly one
    drain() caller, in completion order.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._results: list = []  # completed, unreported (audit, state_file)

    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: float) -> bool:
        """Bounded join of the in-flight audit thread (once-mode drain).
        True when no audit remains in flight afterwards."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def submit(
        self,
        settings,
        *,
        network,
        netuid,
        payload,
        state,
        state_file,
        current_block=None,
        historical_hotkeys_lookup=None,
        block_hash_lookup=None,
    ) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            def _run() -> None:
                audit = run_audit(
                    settings,
                    network=network,
                    netuid=netuid,
                    vector_payload=payload,
                    state=state,
                    current_block=current_block,
                    historical_hotkeys_lookup=historical_hotkeys_lookup,
                    block_hash_lookup=block_hash_lookup,
                )
                # Append (never assign) under the drain lock: a completed
                # result is either queued or already handed out — a later
                # completion cannot overwrite an unreported one.
                with self._lock:
                    self._results.append((audit, state_file))

            self._thread = threading.Thread(
                target=_run, name="cathedral-shadow-audit", daemon=True
            )
            self._thread.start()
            return True

    def drain(self) -> list:
        """Every completed, not-yet-reported result — exactly once, in
        completion order."""
        with self._lock:
            results, self._results = self._results, []
            return results


def _get_shadow_auditor(args) -> _ShadowAuditor:
    existing = getattr(args, "_shadow_auditor", None)
    if existing is not None:
        return existing
    auditor = _ShadowAuditor()
    try:
        args._shadow_auditor = auditor
    except AttributeError:
        pass
    return auditor


def _log_audit_events(args, audit, state_file: Path, *, persist: bool = True) -> bool:
    """Log one completed audit and (for shadow) persist chain state
    observationally — the fence still refuses stale/equivocating writes, but
    a refusal is logged and skipped, never fatal. Authority passes
    persist=False because it has ALREADY reserved under the fence BEFORE any
    PASS event is emitted (main thread only)."""
    events = _get_events(args)
    status_map = {"PASS": PASS, "FAIL": FAIL, "NOT_PROVEN": NOT_PROVEN}
    if audit.status == "PASS" and audit.agrees_with_vector is False:
        # Vector agreement is independent of assurance level. In particular,
        # a receipts-only audit can still prove that Cathedral's signed vector
        # disagrees with the independently recomputed receipts. Emit that
        # structured failure before the partial-assurance early return so
        # shadow-mode reproduction can never mislabel disagreement as PASS.
        events.event(
            "PROVENANCE_VECTOR_MISMATCH",
            stage="provenance",
            status=FAIL,
            detail="; ".join(audit.discrepancies)[:512],
            remediation=audit.remediation,
            vector_agrees=False,
        )
        _lifecycle(
            "PROVENANCE mismatch",
            f"discrepancies={len(audit.discrepancies)}",
        )
        # Disagreement is the terminal aggregate outcome regardless of the
        # assurance level. Never append a later PASS/NOT_PROVEN event that a
        # tail-based consumer could mistake for the final verdict.
        return False
    if (
        audit.status == "PASS"
        and getattr(audit, "assurance", "receipts_only") != "full"
    ):
        # Receipts-only recomputation is PARTIAL provenance. Positive raw
        # evidence may already have replayed successfully while independently
        # anchored non-verified candidates remain unsupported by replayable
        # negative evidence. Never erase that distinction in the operator log.
        # It must not be announced as a provenance PASS or persist the durable
        # reservation state as if it were FULL.
        reasons = list(getattr(audit, "not_proven_reasons", ()) or ())
        raw_replayed = list(getattr(audit, "raw_replayed_hotkeys", ()) or ())
        replay_summary = (
            f"positive raw evidence replayed for {len(raw_replayed)} miner(s)"
            if raw_replayed
            else "no positive raw evidence replayed"
        )
        detail = f"{replay_summary}; whole-epoch FULL assurance is not established" + (
            ": " + "; ".join(reasons) if reasons else ""
        )
        events.event(
            "PROVENANCE_AUDIT_NOT_PROVEN",
            stage="provenance",
            status=NOT_PROVEN,
            duration_ms=audit.duration_ms,
            artifact=audit.manifest_digest,
            detail=detail[:512],
            vector_agrees=audit.agrees_with_vector,
            remediation=(
                "keep thin authority; FULL requires independently replayable "
                "evidence for every anchored candidate outcome"
                if reasons
                else "provide the controlled package and verifier pins for FULL"
            ),
        )
        return False
    if audit.status == "PASS":
        try:
            if persist:
                _write_state_fenced(
                    state_file,
                    {
                        "provenance_last_source_epoch": audit.source_epoch,
                        "provenance_last_report_id": audit.report_id,
                        "provenance_index_epoch": audit.index_source_epoch,
                        "provenance_index_manifest": audit.index_manifest,
                        "provenance_policy_release": audit.policy_release,
                        "provenance_policy_digest": audit.policy_digest,
                    },
                )
        except ValueError as exc:
            events.event(
                "PROVENANCE_STATE_STALE_SKIPPED",
                stage="provenance",
                status=NOT_PROVEN,
                detail=stable_error(exc),
                remediation="a newer reservation exists; shadow stays observational",
            )
            return False
        except Exception as exc:  # noqa: BLE001 - shadow is observational only
            events.event(
                "PROVENANCE_STATE_WRITE_FAILED",
                stage="provenance",
                status=NOT_PROVEN,
                detail=stable_error(exc),
                remediation="fix the state file path/permissions; thin is unaffected",
            )
            return False
        events.event(
            "PROVENANCE_AUDIT_PASS",
            stage="provenance",
            status=PASS,
            duration_ms=audit.duration_ms,
            artifact=audit.manifest_digest,
            detail=(
                f"source_epoch={audit.source_epoch} release={audit.policy_release} "
                f"mechanism={audit.mechanism} verified_miners={len(audit.recomputed)} "
                f"vector_agrees={audit.agrees_with_vector}"
            ),
            vector_agrees=audit.agrees_with_vector,
        )
        return True
    else:
        events.event(
            "PROVENANCE_AUDIT_" + audit.status,
            stage="provenance",
            status=status_map.get(audit.status, FAIL),
            duration_ms=audit.duration_ms,
            artifact=audit.manifest_digest,
            detail=(audit.error or "; ".join(audit.discrepancies))[:512] or None,
            remediation=audit.remediation,
        )
        _lifecycle(
            "PROVENANCE " + audit.status.lower(),
            f"error={audit.error!r}" if audit.error else "",
        )
        return False


def _run_provenance_stage(
    args,
    payload: dict[str, Any],
    state_file: Path,
    current_block: int | None = None,
    historical_hotkeys_lookup=None,
    block_hash_lookup=None,
) -> tuple[str, dict[str, float] | None]:
    """Provenance stage for this tick.

    Shadow: a bounded SINGLE-FLIGHT background worker — the previous audit's
    result is drained and logged, a new audit is submitted without blocking,
    and the thin submission proceeds untouched regardless of audit speed or
    health. Authority: synchronous; the tick refuses to submit anything
    unless the audit PASSes at FULL assurance.
    """
    settings = _provenance_settings(args)
    if settings.mode == "authority":
        from .events import _neutralize

        state = _read_state(state_file)
        audit = run_audit(
            settings,
            network=args.network,
            netuid=args.netuid,
            vector_payload=payload,
            state=state,
            current_block=current_block,
            historical_hotkeys_lookup=historical_hotkeys_lookup,
            block_hash_lookup=block_hash_lookup,
        )
        if audit.status != "PASS":
            _log_audit_events(args, audit, state_file, persist=False)
            raise wire.VectorError(
                f"full-provenance authority audit did not PASS ({audit.status}): "
                f"{audit.error or 'see events'}"
            )
        if getattr(audit, "assurance", "receipts_only") != "full":
            # A receipts-only PASS must not emit a PASS event or reserve.
            _get_events(args).event(
                "PROVENANCE_AUDIT_NOT_PROVEN",
                stage="provenance",
                status=NOT_PROVEN,
                artifact=audit.manifest_digest,
                detail="receipts-only recomputation cannot back authority",
                remediation="provide the controlled package and verifier pins",
            )
            raise wire.VectorError(
                "authority requires FULL assurance (raw-evidence replay); "
                "receipts-only recomputation is NOT PROVEN and never submits"
            )
        # RESERVE FIRST, under ONE flock hold covering the index line, the
        # policy line, the report line, and the chain identity. Only a
        # successful reservation may emit PASS: a concurrently advanced
        # state makes THIS audit fail before any success is visible
        # anywhere, so an older/equivocating writer can neither log PASS
        # nor overwrite the newer reservation.
        try:
            _write_state_fenced(
                state_file,
                {
                    "provenance_network": args.network,
                    "provenance_netuid": args.netuid,
                    "provenance_last_source_epoch": audit.source_epoch,
                    "provenance_last_report_id": audit.report_id,
                    "provenance_index_epoch": audit.index_source_epoch,
                    "provenance_index_manifest": audit.index_manifest,
                    "provenance_policy_release": audit.policy_release,
                    "provenance_policy_digest": audit.policy_digest,
                },
            )
        except (ValueError, OSError) as exc:
            _get_events(args).event(
                "PROVENANCE_RESERVATION_REFUSED",
                stage="provenance",
                status=FAIL,
                artifact=audit.manifest_digest,
                detail=_neutralize(str(exc))[:512],
                remediation=(
                    "a newer reservation exists or the state file is "
                    "unwritable; nothing was submitted"
                ),
            )
            raise wire.VectorError(
                f"authority reservation refused: {_neutralize(str(exc))}"
            ) from exc
        _log_audit_events(args, audit, state_file, persist=False)
        args._authority_full_audit = audit
        return audit.status, dict(audit.recomputed)

    auditor = _get_shadow_auditor(args)
    for finished_audit, finished_state_file in auditor.drain():
        _log_audit_events(args, finished_audit, finished_state_file)
    submitted = auditor.submit(
        settings,
        network=args.network,
        netuid=args.netuid,
        payload=dict(payload),
        state=_read_state(state_file),
        state_file=state_file,
        current_block=current_block,
        historical_hotkeys_lookup=historical_hotkeys_lookup,
        block_hash_lookup=block_hash_lookup,
    )
    if not submitted:
        _get_events(args).event(
            "PROVENANCE_AUDIT_SKIPPED",
            stage="provenance",
            status=INFO,
            detail="previous shadow audit still in flight (single-flight)",
        )
    return "PENDING", None


def _run_launch_rewarded_set_gate(
    args: Any,
    *,
    payload: dict[str, Any],
    uid_weights: dict[int, float],
    hotkey_to_uid: dict[str, int],
    current_block: int,
    state_file: Path,
    state: dict[str, Any] | None = None,
    persist: bool = True,
) -> Any:
    """Synchronously replay every rewarded miner and prove vector agreement.

    This is a launch-only gate. Normal shadow operation remains non-blocking;
    the one bounded mainnet canary opts into this function and cannot reserve
    or submit its thin vector unless controlled raw evidence for every rewarded
    miner independently derives the identical UID allocation.
    """
    settings = replace(_provenance_settings(args), mode="authority")
    audit = run_audit(
        settings,
        network=args.network,
        netuid=args.netuid,
        vector_payload=payload,
        state=dict(state) if state is not None else _read_state(state_file),
        current_block=current_block,
        historical_hotkeys_lookup=_historical_metagraph_lookup(
            args.network, args.netuid
        ),
        block_hash_lookup=_block_hash_lookup(args.network),
    )
    rewarded = set(getattr(audit, "recomputed", {}) or {})
    receipt_hotkeys = set(getattr(audit, "receipt_hotkeys", ()) or ())
    raw_replayed = set(getattr(audit, "raw_replayed_hotkeys", ()) or ())
    if (
        audit.status != "PASS"
        or audit.agrees_with_vector is not True
        or not rewarded
        or rewarded != receipt_hotkeys
        or rewarded != raw_replayed
    ):
        _log_audit_events(args, audit, state_file, persist=False)
        raise wire.VectorError(
            "launch canary requires controlled raw replay of every rewarded "
            "miner and exact agreement with Cathedral's signed vector"
        )
    recomputed_uid_weights = _provenance_uid_weights(
        dict(audit.recomputed),
        mechanism=getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
        or MECHANISM_DEFAULT,
        burn_hotkey=getattr(args, "provenance_burn_hotkey", None),
        hotkey_to_uid=hotkey_to_uid,
    )
    if set(recomputed_uid_weights) != set(uid_weights) or any(
        not math.isclose(
            recomputed_uid_weights[uid],
            uid_weights[uid],
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for uid in uid_weights
    ):
        raise wire.VectorError(
            "launch rewarded-set recomputation does not match the thin UID vector"
        )
    if persist:
        try:
            _write_state_fenced(
                state_file,
                {
                    "provenance_network": args.network,
                    "provenance_netuid": args.netuid,
                    "provenance_last_source_epoch": audit.source_epoch,
                    "provenance_last_report_id": audit.report_id,
                    "provenance_index_epoch": audit.index_source_epoch,
                    "provenance_index_manifest": audit.index_manifest,
                    "provenance_policy_release": audit.policy_release,
                    "provenance_policy_digest": audit.policy_digest,
                },
            )
        except (ValueError, OSError) as exc:
            raise wire.VectorError(
                "launch rewarded-set provenance reservation failed before submission: "
                f"{stable_error(exc)}"
            ) from exc
    _log_audit_events(args, audit, state_file, persist=False)
    _get_events(args).event(
        "LAUNCH_REWARDED_SET_GATE_PASS",
        stage="launch",
        status=PASS,
        artifact=audit.manifest_digest,
        detail=(
            f"source_epoch={audit.source_epoch} report_id={audit.report_id} "
            "all rewarded miners raw-replayed + vector agreement + UID agreement; "
            f"whole_epoch_assurance={audit.assurance}"
        ),
        source_epoch=audit.source_epoch,
        report_id=audit.report_id,
        vector_agrees=True,
    )
    _lifecycle(
        "LAUNCH rewarded-set gate",
        f"source_epoch={audit.source_epoch} vector_agrees=true "
        f"whole_epoch_assurance={audit.assurance}",
    )
    args._launch_rewarded_set_audit = audit
    return audit


def _revalidate_launch_after_rewarded_set_replay(
    args: Any,
    *,
    payload: dict[str, Any],
    audit: Any,
    fence_version: int,
) -> tuple[ChainPreflight, dict[str, int], dict[int, float]]:
    """Refresh every mutable chain/time input immediately before reservation."""
    accept_vector(
        payload,
        public_key_hex=args.public_key_hex,
        key_id=args.key_id,
        network=args.network,
        netuid=args.netuid,
        fence_version=fence_version,
    )
    fresh = chain_preflight(
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
    )
    _validate_resolved_chain_contract(args, fresh, require_sn39_identity=True)
    _bind_submission_identity(args, fresh)
    if fresh.block is None:
        raise wire.VectorError("fresh launch preflight has no finalized block")
    valid_from = getattr(audit, "report_valid_from_block", None)
    valid_until = getattr(audit, "report_valid_until_block", None)
    if (
        isinstance(valid_from, bool)
        or isinstance(valid_until, bool)
        or not isinstance(valid_from, int)
        or not isinstance(valid_until, int)
        or not valid_from <= fresh.block < valid_until
    ):
        raise wire.VectorError(
            "fresh finalized block is outside the provenance report validity window"
        )
    report_generated = wire._parse_canonical_utc(
        getattr(audit, "report_generated_at", None),
        field="provenance report generated_at",
    )
    report_valid_until = getattr(audit, "report_valid_until", None)
    report_expiry = wire._parse_canonical_utc(
        report_valid_until,
        field="provenance report valid_until",
    )
    if datetime.now(UTC) >= report_expiry:
        raise wire.VectorError("provenance report expired during launch replay")
    vector_generated = wire._parse_canonical_utc(
        payload.get("generated_at"),
        field="generated_at",
    )
    vector_expiry = wire._parse_canonical_utc(
        payload.get("expires_at"),
        field="expires_at",
    )
    inclusion_start = max(vector_generated, report_generated)
    inclusion_expiry = min(vector_expiry, report_expiry)
    if inclusion_start >= inclusion_expiry:
        raise wire.VectorError("launch inclusion time window is empty")

    uid_weights = vector_to_uid_weights(
        payload,
        fresh.hotkey_to_uid,
        require_policy=getattr(args, "require_policy", None),
    )
    recomputed_uid_weights = _provenance_uid_weights(
        dict(audit.recomputed),
        mechanism=getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
        or MECHANISM_DEFAULT,
        burn_hotkey=getattr(args, "provenance_burn_hotkey", None),
        hotkey_to_uid=fresh.hotkey_to_uid,
    )
    if set(recomputed_uid_weights) != set(uid_weights) or any(
        not math.isclose(
            recomputed_uid_weights[uid],
            uid_weights[uid],
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for uid in uid_weights
    ):
        raise wire.VectorError(
            "fresh launch UID mapping no longer agrees with rewarded-set recomputation"
        )
    _validate_chain_constraints(uid_weights, fresh)
    signed_rows = payload.get("weights")
    burn_snapshot = payload.get("burn_snapshot")
    if (
        fresh.hotkey_to_uid.get(fresh.validator_hotkey) != fresh.validator_uid
        or not isinstance(signed_rows, list)
        or len(signed_rows) != 1
        or not isinstance(signed_rows[0], dict)
        or not isinstance(burn_snapshot, dict)
    ):
        raise wire.VectorError(
            "fresh launch mapping differs from the immutable SN39 release boundary"
        )
    rewarded_hotkey = signed_rows[0].get("miner_hotkey")
    burn_hotkey = burn_snapshot.get("burn_hotkey")
    rewarded_uid = fresh.hotkey_to_uid.get(rewarded_hotkey)
    burn_uid = fresh.hotkey_to_uid.get(burn_hotkey)
    if (
        not isinstance(rewarded_hotkey, str)
        or not rewarded_hotkey
        or not isinstance(rewarded_uid, int)
        or burn_hotkey != getattr(args, "provenance_burn_hotkey", None)
        or burn_hotkey != fresh.subnet_owner_hotkey
        or not isinstance(burn_uid, int)
        or rewarded_uid == burn_uid
        or fresh.validator_uid in {rewarded_uid, burn_uid}
        or fresh.validator_hotkey in {rewarded_hotkey, burn_hotkey}
        or set(uid_weights) != {rewarded_uid, burn_uid}
        or not math.isclose(
            uid_weights[rewarded_uid],
            0.90,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            uid_weights[burn_uid],
            0.10,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise wire.VectorError(
            "fresh launch allocation is not the dynamically resolved rewarded-hotkey "
            "and subnet-owner burn-hotkey 90/10 release boundary"
        )
    args._launch_inclusion_policy = InclusionPolicy(
        valid_from_block=valid_from,
        valid_until_block=valid_until,
        valid_from_time=inclusion_start,
        valid_until_time=inclusion_expiry,
        expected_next_epoch_start_block=fresh.next_epoch_start_block,
    )
    _require_inclusion_policy_ready(args._launch_inclusion_policy, fresh)
    uid_safety = _require_uid_mapping_stability(
        fresh,
        {
            rewarded_uid: rewarded_hotkey,
            burn_uid: burn_hotkey,
        },
        mortal_period_blocks=args._launch_inclusion_policy.mortal_period_blocks,
    )
    _require_launch_evidence_after_rotations(
        payload=payload,
        audit=audit,
        uid_safety=uid_safety,
    )
    return fresh, fresh.hotkey_to_uid, uid_weights


def _launch_release_config_identity(args: Any) -> dict[str, Any]:
    """Exact immutable release and resolved launch profile under review."""
    release_sha = str(getattr(args, "launch_release_sha", "") or "")
    config_digest = str(getattr(args, "launch_config_sha256", "") or "")
    approval_file = Path(str(getattr(args, "launch_approval_file", "") or ""))
    if re.fullmatch(r"[0-9a-f]{40}", release_sha) is None:
        raise wire.VectorError(
            "launch preflight requires the immutable installed release SHA"
        )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest) is None:
        raise wire.VectorError(
            "launch preflight requires the immutable launch-config digest"
        )
    if approval_file != SN39_LAUNCH_APPROVAL_FILE:
        raise wire.VectorError(
            "launch approval path differs from the immutable SN39 profile"
        )
    try:
        source_digest = (
            "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        )
    except OSError as exc:
        raise wire.VectorError("validator release source is unreadable") from exc
    return {
        "schema": "cathedral_sn39_launch_release_config_v1",
        "release_sha": release_sha,
        "launch_config_sha256": config_digest,
        "validator_source_sha256": source_digest,
        "network": str(args.network).strip().lower(),
        "netuid": int(args.netuid),
        "publisher_url": args.publisher_url,
        "weight_policy_public_key": args.public_key_hex,
        "weight_policy_key_id": args.key_id,
        "required_policy": args.require_policy,
        "state_file": str(Path(args.state_file)),
        "runtime_root": str(_submission_runtime_root(args)),
        "wallet_name": args.wallet_name,
        "wallet_hotkey_alias": args.wallet_hotkey,
        "evidence_url": args.evidence_url,
        "registry_keys": args.provenance_registry_keys,
        "registry_keys_digest": args.provenance_registry_keys_digest,
        "report_keys": args.provenance_report_keys,
        "report_keys_digest": args.provenance_report_keys_digest,
        "index_keys": args.provenance_index_keys,
        "index_keys_digest": args.provenance_index_keys_digest,
        "verifier_digest": args.provenance_verifier_digest,
        "producer_revision": args.provenance_source_revision,
        "mechanism": args.provenance_mechanism,
        "burn_hotkey": args.provenance_burn_hotkey,
        "controlled_dir": str(Path(args.provenance_controlled_dir)),
        "verifier_binary": str(Path(args.provenance_verifier_binary)),
        "approval_file": str(approval_file),
        "max_submissions": int(args.max_submissions),
        "weight_version_key": _weight_version_key(),
    }


def _launch_approval_bindings(
    args: Any,
    *,
    payload: dict[str, Any],
    audit: Any,
    preflight: ChainPreflight,
    uid_weights: dict[int, float],
    hotkey_to_uid: dict[str, int],
) -> dict[str, Any]:
    """Build every non-head fact an operator approves for one launch."""
    signed_vector_sha256 = (
        "sha256:" + hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    )
    signed_rows = payload.get("weights")
    burn_snapshot = payload.get("burn_snapshot")
    rewarded = sorted(set(getattr(audit, "recomputed", {}) or {}))
    replayed = sorted(set(getattr(audit, "raw_replayed_hotkeys", ()) or ()))
    receipt_hotkeys = sorted(set(getattr(audit, "receipt_hotkeys", ()) or ()))
    if (
        not isinstance(signed_rows, list)
        or not isinstance(burn_snapshot, dict)
        or not rewarded
        or rewarded != replayed
        or rewarded != receipt_hotkeys
    ):
        raise wire.VectorError(
            "launch approval requires one exact FULL rewarded-set replay"
        )
    burn_hotkey = burn_snapshot.get("burn_hotkey")
    burn_uid = hotkey_to_uid.get(burn_hotkey)
    rewarded_uid_hotkeys = sorted(
        (hotkey_to_uid[hotkey], hotkey)
        for hotkey in rewarded
        if hotkey in hotkey_to_uid
    )
    ordered = sorted((int(uid), float(weight)) for uid, weight in uid_weights.items())
    uid_hotkeys = sorted(
        (int(uid), str(hotkey))
        for hotkey, uid in hotkey_to_uid.items()
        if uid in uid_weights
    )
    if (
        not isinstance(burn_hotkey, str)
        or not burn_hotkey
        or not isinstance(burn_uid, int)
        or len(rewarded_uid_hotkeys) != len(rewarded)
        or preflight.hotkey_to_uid != hotkey_to_uid
    ):
        raise wire.VectorError(
            "launch approval cannot bind the rewarded and burn hotkeys"
        )
    wire_uids, wire_weights = _wire_weights(
        [uid for uid, _weight in ordered],
        [weight for _uid, weight in ordered],
    )
    release_config = _launch_release_config_identity(args)
    return {
        "release_config": release_config,
        "release_config_digest": _sha256_document(release_config),
        "chain_genesis_hash": str(preflight.genesis_hash).lower(),
        "signer_validator_hotkey": preflight.validator_hotkey,
        "signer_validator_uid": preflight.validator_uid,
        "vector_id": payload.get("vector_id"),
        "policy_version": payload.get("policy_version"),
        "policy_contract": args.require_policy,
        "signed_vector_sha256": signed_vector_sha256,
        "rewarded_hotkeys": rewarded,
        "rewarded_uid_hotkeys": [[uid, hotkey] for uid, hotkey in rewarded_uid_hotkeys],
        "burn_hotkey": burn_hotkey,
        "burn_uid": burn_uid,
        "uid_weights": [[uid, weight] for uid, weight in ordered],
        "uid_hotkeys": [[uid, hotkey] for uid, hotkey in uid_hotkeys],
        "wire_uids": wire_uids,
        "wire_weights": wire_weights,
        "provenance": {
            "source_epoch": getattr(audit, "source_epoch", None),
            "report_id": getattr(audit, "report_id", None),
            "manifest": getattr(audit, "manifest_digest", None),
            "policy_release": getattr(audit, "policy_release", None),
            "policy_digest": getattr(audit, "policy_digest", None),
            "mechanism": getattr(audit, "mechanism", None),
            "whole_epoch_assurance": getattr(audit, "assurance", None),
            "verifier_binary_digest": getattr(audit, "verifier_binary_digest", None),
            "report_signing_key_id": getattr(audit, "report_signing_key_id", None),
            "signed_index": getattr(audit, "signed_index", None),
            "raw_replayed_hotkeys": replayed,
        },
    }


def _build_launch_approval(
    args: Any,
    *,
    payload: dict[str, Any],
    audit: Any,
    preflight: ChainPreflight,
    uid_weights: dict[int, float],
    hotkey_to_uid: dict[str, int],
) -> dict[str, Any]:
    if (
        preflight.block is None
        or preflight.block <= 0
        or _CHAIN_HASH_RE.fullmatch(str(preflight.finalized_hash).lower()) is None
    ):
        raise wire.VectorError(
            "launch approval requires a canonical finalized block and hash"
        )
    body = {
        "schema": SN39_LAUNCH_APPROVAL_SCHEMA,
        "bindings": _launch_approval_bindings(
            args,
            payload=payload,
            audit=audit,
            preflight=preflight,
            uid_weights=uid_weights,
            hotkey_to_uid=hotkey_to_uid,
        ),
        "reviewed_finalized_block": preflight.block,
        "reviewed_finalized_hash": str(preflight.finalized_hash).lower(),
        "approval_valid_until_block": (
            preflight.block + SN39_LAUNCH_APPROVAL_LIFETIME_BLOCKS
        ),
    }
    return {**body, "approval_digest": _sha256_document(body)}


def _validate_launch_approval_envelope(
    args: Any,
    document: dict[str, Any],
) -> dict[str, Any]:
    if set(document) != {
        "schema",
        "bindings",
        "reviewed_finalized_block",
        "reviewed_finalized_hash",
        "approval_valid_until_block",
        "approval_digest",
    }:
        raise wire.VectorError("launch approval fields differ from its schema")
    body = {key: value for key, value in document.items() if key != "approval_digest"}
    reviewed_block = body["reviewed_finalized_block"]
    valid_until = body["approval_valid_until_block"]
    reviewed_hash = body["reviewed_finalized_hash"]
    if (
        body["schema"] != SN39_LAUNCH_APPROVAL_SCHEMA
        or not isinstance(body["bindings"], dict)
        or isinstance(reviewed_block, bool)
        or not isinstance(reviewed_block, int)
        or reviewed_block <= 0
        or isinstance(valid_until, bool)
        or not isinstance(valid_until, int)
        or valid_until != reviewed_block + SN39_LAUNCH_APPROVAL_LIFETIME_BLOCKS
        or not isinstance(reviewed_hash, str)
        or _CHAIN_HASH_RE.fullmatch(reviewed_hash) is None
        or document["approval_digest"] != _sha256_document(body)
    ):
        raise wire.VectorError("launch approval digest or validity is malformed")
    bindings = body["bindings"]
    release_config = bindings.get("release_config")
    if (
        not isinstance(release_config, dict)
        or bindings.get("release_config_digest") != _sha256_document(release_config)
        or release_config != _launch_release_config_identity(args)
    ):
        raise wire.VectorError(
            "launch approval differs from the running release or config"
        )
    return document


def _load_launch_approval(args: Any) -> dict[str, Any]:
    path = Path(str(getattr(args, "launch_approval_file", "") or ""))
    return _validate_launch_approval_envelope(
        args,
        _read_root_launch_approval(path),
    )


def _require_launch_approval(
    args: Any,
    *,
    payload: dict[str, Any],
    audit: Any,
    preflight: ChainPreflight,
    uid_weights: dict[int, float],
    hotkey_to_uid: dict[str, int],
) -> dict[str, Any]:
    """Consume the exact operator-reviewed artifact before any reservation."""
    document = _load_launch_approval(args)
    reviewed_block = document["reviewed_finalized_block"]
    reviewed_hash = document["reviewed_finalized_hash"]
    valid_until = document["approval_valid_until_block"]
    if (
        preflight.block is None
        or preflight.block < reviewed_block
        or preflight.block + SN39_MORTAL_PERIOD_BLOCKS > valid_until
        or _CHAIN_HASH_RE.fullmatch(str(preflight.finalized_hash).lower()) is None
    ):
        raise wire.VectorError(
            "launch approval is stale or outside its finalized-head bound"
        )
    substrate = getattr(preflight.subtensor, "substrate", None)
    try:
        canonical_reviewed_hash = str(substrate.get_block_hash(reviewed_block)).lower()
        canonical_current_hash = str(substrate.get_block_hash(preflight.block)).lower()
    except (AttributeError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "launch approval finalized-head binding is unavailable"
        ) from exc
    if (
        canonical_reviewed_hash != reviewed_hash
        or canonical_current_hash != str(preflight.finalized_hash).lower()
    ):
        raise wire.VectorError(
            "launch approval finalized block/hash is no longer canonical"
        )
    expected_bindings = _launch_approval_bindings(
        args,
        payload=payload,
        audit=audit,
        preflight=preflight,
        uid_weights=uid_weights,
        hotkey_to_uid=hotkey_to_uid,
    )
    if document["bindings"] != expected_bindings:
        raise wire.VectorError(
            "launch approval differs from the fresh vector, signer, mapping, or "
            "FULL provenance"
        )
    args._launch_approval = document
    return document


def _require_launch_journal_available(state: dict[str, Any]) -> None:
    launch_history = state.get("submission_launch_attempt_ids", [])
    if (
        state.get("submission_pending_id") is not None
        or state.get("submission_pending_phase") is not None
        or state.get("submission_launch_status") in {"pending", "finalized"}
        or not isinstance(launch_history, list)
        or bool(launch_history)
    ):
        raise wire.VectorError(
            "launch preflight requires a clear one-shot submission journal"
        )


def run_launch_preflight(args: Any, *, approval_out: Path) -> dict[str, Any]:
    """Run the exact launch gate read-only and emit a bounded operator approval."""
    if approval_out != SN39_LAUNCH_APPROVAL_FILE:
        raise wire.VectorError(
            "launch preflight output differs from the immutable approval path"
        )
    args.launch_preflight = True
    args.broadcast = False
    args.offline = False
    args.once = True
    _validate_runtime_contract(args)
    state = _read_state_without_mutation(Path(args.state_file))
    _require_launch_journal_available(state)
    fence = _state_policy_fence(state)
    payload = fetch_vector(args.publisher_url)
    accept_vector(
        payload,
        public_key_hex=args.public_key_hex,
        key_id=args.key_id,
        network=args.network,
        netuid=args.netuid,
        fence_version=fence,
    )
    preflight = chain_preflight(
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
    )
    _validate_resolved_chain_contract(args, preflight, require_sn39_identity=True)
    _bind_submission_identity(args, preflight)
    uid_weights = vector_to_uid_weights(
        payload,
        preflight.hotkey_to_uid,
        require_policy=args.require_policy,
    )
    _validate_chain_constraints(uid_weights, preflight)
    burn_hotkey = (payload.get("burn_snapshot") or {}).get("burn_hotkey")
    burn_uid = preflight.hotkey_to_uid.get(burn_hotkey)
    _require_no_validator_compute_reward(
        uid_weights,
        preflight=preflight,
        burn_uid=burn_uid,
    )
    audit = _run_launch_rewarded_set_gate(
        args,
        payload=payload,
        uid_weights=uid_weights,
        hotkey_to_uid=preflight.hotkey_to_uid,
        current_block=int(preflight.block),
        state_file=Path(args.state_file),
        state=state,
        persist=False,
    )
    fresh, hotkey_to_uid, uid_weights = _revalidate_launch_after_rewarded_set_replay(
        args,
        payload=payload,
        audit=audit,
        fence_version=fence,
    )
    approval = _build_launch_approval(
        args,
        payload=payload,
        audit=audit,
        preflight=fresh,
        uid_weights=uid_weights,
        hotkey_to_uid=hotkey_to_uid,
    )
    _write_root_launch_approval(approval_out, approval)
    _get_events(args).event(
        "LAUNCH_PREFLIGHT_APPROVED",
        stage="launch",
        status=PASS,
        artifact=approval["approval_digest"],
        detail=(
            f"read_only=true reviewed_finalized_block="
            f"{approval['reviewed_finalized_block']} "
            f"valid_until_block={approval['approval_valid_until_block']}"
        ),
        approval_digest=approval["approval_digest"],
        reviewed_finalized_block=approval["reviewed_finalized_block"],
        reviewed_finalized_hash=approval["reviewed_finalized_hash"],
        approval_valid_until_block=approval["approval_valid_until_block"],
    )
    return approval


# -- burn + uid mapping ---------------------------------------------------------


def apply_burn(
    scores_by_uid: dict[int, float],
    *,
    burn_uid: int | None,
    forced_burn_percentage: float,
) -> dict[int, float]:
    """burn% of total mass to burn_uid, remainder split proportionally across
    miners; normalized to sum 1.0. Empty miner set -> everything to burn_uid."""
    burn_frac = forced_burn_percentage / 100.0
    if burn_uid is not None:
        # burn_uid must never double-collect (miner share + forced burn);
        # any score that mapped onto it is dropped before allocation.
        scores_by_uid = {u: v for u, v in scores_by_uid.items() if u != burn_uid}
    total = sum(scores_by_uid.values())
    if total <= 0 or not scores_by_uid:
        if burn_uid is None:
            raise wire.VectorError("no miner mass and no burn_uid fallback")
        return {burn_uid: 1.0}
    out = {uid: (v / total) * (1.0 - burn_frac) for uid, v in scores_by_uid.items()}
    if burn_uid is not None and burn_frac > 0:
        out[burn_uid] = out.get(burn_uid, 0.0) + burn_frac
    norm = sum(out.values())
    return {uid: v / norm for uid, v in out.items()}


def accept_vector(
    payload: dict[str, Any],
    *,
    public_key_hex: str,
    key_id: str,
    network: str,
    netuid: int,
    fence_version: int,
) -> None:
    """Every check between 'bytes arrived' and 'safe to apply'. Raises on any
    failure — there is deliberately no partial acceptance."""
    wire.verify_signature(
        payload, public_key_hex=public_key_hex, expected_key_id=key_id
    )
    wire.invariant_check(payload, network=network, netuid=netuid, now_iso=_ms_iso_now())
    pv = int(payload["policy_version"])
    if pv <= fence_version:
        raise wire.VectorError(
            f"rollback/replay: vector policy_version {pv} <= last accepted {fence_version}"
        )


def _confidential_tdx_v3_rows(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    metadata = payload.get("policy_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    cap = metadata.get("confidential_tdx_cap") or {}
    if not isinstance(cap, dict) or cap.get("cap_version") != "v3":
        return None

    try:
        configured_fraction = float(cap["configured_fraction"])
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "confidential_tdx v3 missing configured_fraction"
        ) from exc
    if not math.isfinite(configured_fraction) or not 0.0 < configured_fraction <= 0.10:
        raise wire.VectorError(
            f"confidential_tdx v3 invalid configured_fraction {configured_fraction!r}"
        )

    rows = payload.get("weights")
    if not isinstance(rows, list):
        raise wire.VectorError("confidential_tdx v3 weights must be a list")
    hotkeys: set[str] = set()
    weight_mass = 0.0
    base_mass = 0.0
    external_mass = 0.0
    for row in rows:
        if not isinstance(row, dict):
            raise wire.VectorError("confidential_tdx v3 weight row must be an object")
        try:
            weight = float(row["weight"])
            base = float(row["base_component"])
            external = float(row["external_component"])
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError(
                "confidential_tdx v3 row missing or invalid attribution component"
            ) from exc
        if not all(
            math.isfinite(value) and value >= 0.0 for value in (weight, base, external)
        ):
            raise wire.VectorError(
                f"confidential_tdx v3 row {row.get('miner_hotkey')!r} "
                "has non-finite or negative attribution"
            )
        if not math.isclose(weight, base + external, rel_tol=0.0, abs_tol=1e-12):
            raise wire.VectorError(
                f"confidential_tdx v3 row {row.get('miner_hotkey')!r} "
                f"weight {weight!r} != base+external {base + external!r}"
            )
        hotkey = row.get("miner_hotkey")
        if not isinstance(hotkey, str) or not hotkey:
            raise wire.VectorError("confidential_tdx v3 row missing miner_hotkey")
        if hotkey in hotkeys:
            raise wire.VectorError(f"confidential_tdx v3 duplicate hotkey {hotkey!r}")
        hotkeys.add(hotkey)
        weight_mass = math.fsum((weight_mass, weight))
        base_mass = math.fsum((base_mass, base))
        external_mass = math.fsum((external_mass, external))

    component_mass = base_mass + external_mass
    if not math.isclose(weight_mass, component_mass, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError(
            f"confidential_tdx v3 weight mass {weight_mass!r} != "
            f"component mass {component_mass!r}"
        )
    if base_mass <= 0.0 or external_mass <= 0.0:
        raise wire.VectorError(
            "confidential_tdx v3 requires positive base and external mass"
        )
    realized_fraction = external_mass / component_mass
    if abs(realized_fraction - configured_fraction) > 1e-12:
        raise wire.VectorError(
            f"confidential_tdx v3 external fraction {realized_fraction!r} != "
            f"configured_fraction {configured_fraction!r}"
        )
    return rows


def _confidential_primary_meta(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Detect and strictly validate the v1 confidential-primary policy metadata.

    Returns the metadata dict when the signed contract is present, else None.
    Raises VectorError on a malformed/incompatible contract (never falls back).
    """
    metadata = payload.get("policy_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    cp = metadata.get("confidential_primary")
    if cp is None:
        return None
    if not isinstance(cp, dict):
        raise wire.VectorError("confidential_primary metadata must be an object")
    if cp.get("contract_version") != "v1":
        raise wire.VectorError(
            "confidential_primary unsupported contract_version "
            f"{cp.get('contract_version')!r}"
        )
    if cp.get("source") != "cathedral_confidential_tdx":
        raise wire.VectorError(
            f"confidential_primary invalid source {cp.get('source')!r}"
        )
    try:
        base_mass = float(cp["base_mass"])
        confidential_mass = float(cp["confidential_mass"])
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "confidential_primary missing base/confidential mass"
        ) from exc
    if base_mass != 0.0:
        raise wire.VectorError(
            f"confidential_primary base_mass must be 0, got {base_mass!r}"
        )
    if confidential_mass not in (0.0, 1.0):
        raise wire.VectorError(
            "confidential_primary confidential_mass must be 0 or 1, got "
            f"{confidential_mass!r}"
        )
    if not isinstance(cp.get("complete"), bool):
        raise wire.VectorError("confidential_primary complete flag must be a bool")
    # When the signed contract claims positive mass (mass=1), every liveness
    # field must be explicitly asserted. A degraded vector carries mass=0 and
    # these fields may be absent/false; that is the correct signed burn state.
    if confidential_mass == 1.0:
        if cp.get("mode") != "confidential_primary":
            raise wire.VectorError(
                "confidential_primary mass=1 requires mode=confidential_primary, "
                f"got {cp.get('mode')!r}"
            )
        if cp.get("complete") is not True:
            raise wire.VectorError("confidential_primary mass=1 requires complete=true")
        if cp.get("fresh") is not True:
            raise wire.VectorError("confidential_primary mass=1 requires fresh=true")
        if cp.get("confirmed") is not True:
            raise wire.VectorError(
                "confidential_primary mass=1 requires confirmed=true"
            )
    return cp


def _confidential_primary_to_uid_weights(
    payload: dict[str, Any], cp: dict[str, Any], hotkey_to_uid: dict[str, int]
) -> dict[int, float]:
    """Map a signed confidential-primary vector to UID weights, all-or-nothing.

    Every positive signed hotkey MUST map to exactly one current metagraph UID.
    Duplicate hotkeys, duplicate UIDs, nonfinite/negative attribution, and
    metadata/sum drift all reject the whole vector. There is no partial apply
    and no fallback. The signed burn is applied ONLY after a fully successful
    mapping.
    """
    snap = payload["burn_snapshot"]
    confidential_mass = float(cp["confidential_mass"])
    rows = payload.get("weights")
    if not isinstance(rows, list):
        raise wire.VectorError("confidential_primary weights must be a list")

    hotkeys: set[str] = set()
    weight_mass = 0.0
    positive: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise wire.VectorError("confidential_primary weight row must be an object")
        if "base_component" not in row or "external_component" not in row:
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "must carry both base_component and external_component"
            )
        try:
            weight = float(row["weight"])
            base = float(row["base_component"])
            external = float(row["external_component"])
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError(
                "confidential_primary row has invalid attribution"
            ) from exc
        if not all(math.isfinite(v) and v >= 0.0 for v in (weight, base, external)):
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "has non-finite or negative attribution"
            )
        if base != 0.0:
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "base_component must be 0"
            )
        if not math.isclose(weight, external, rel_tol=0.0, abs_tol=1e-12):
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "weight != external_component"
            )
        hotkey = row.get("miner_hotkey")
        if not isinstance(hotkey, str) or not hotkey:
            raise wire.VectorError("confidential_primary row missing miner_hotkey")
        if hotkey in hotkeys:
            raise wire.VectorError(f"confidential_primary duplicate hotkey {hotkey!r}")
        hotkeys.add(hotkey)
        weight_mass = math.fsum((weight_mass, weight))
        if weight > 0.0:
            positive.append((hotkey, weight))

    # Signed metadata mass must agree with the signed rows.
    if confidential_mass == 1.0:
        if not positive:
            raise wire.VectorError(
                "confidential_primary claims mass 1 but has no positive weight"
            )
        if not math.isclose(weight_mass, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise wire.VectorError(
                f"confidential_primary weight mass {weight_mass!r} != 1.0"
            )
    else:  # confidential_mass == 0.0
        if positive:
            raise wire.VectorError(
                "confidential_primary claims mass 0 but has positive weight"
            )
        if weight_mass != 0.0:
            raise wire.VectorError(
                f"confidential_primary weight mass {weight_mass!r} != 0.0"
            )

    # Every positive signed hotkey must map to exactly one current metagraph UID.
    scores: dict[int, float] = {}
    mapped_uids: set[int] = set()
    for hotkey, weight in positive:
        if hotkey not in hotkey_to_uid:
            raise wire.VectorError(
                f"confidential_primary hotkey {hotkey!r} has no current metagraph UID"
            )
        uid = hotkey_to_uid[hotkey]
        if uid == snap.get("burn_uid"):
            raise wire.VectorError(
                f"confidential_primary hotkey {hotkey!r} resolves to burn UID"
            )
        if uid in mapped_uids:
            raise wire.VectorError(
                f"confidential_primary duplicate UID {uid} in signed vector"
            )
        mapped_uids.add(uid)
        scores[uid] = weight

    # Signed burn applied ONLY after a fully successful mapping.
    return apply_burn(
        scores,
        burn_uid=snap.get("burn_uid"),
        forced_burn_percentage=float(snap["forced_burn_percentage"]),
    )


# Supported policy pins. When a validator opts in, ONLY the selected signed
# contract is applied; every other vector shape (legacy, v3 blend) is rejected.
REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1 = "confidential_primary_v1"
REQUIRE_POLICY_VALIDATED_SUPPLY_V1 = "validated_supply_v1"
REQUIRE_POLICY_CHOICES = (
    REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1,
    REQUIRE_POLICY_VALIDATED_SUPPLY_V1,
)


def _validated_supply_meta(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the launch-locked 90% TDX plus 10% fixed-burn contract.

    Contract v2 makes the current launch boundary explicit: only Intel TDX can
    earn the 90% supply allocation and 10% is unconditionally burned. No GPU
    capability or future admission is represented by this signed payload.
    """
    metadata = payload.get("policy_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    policy = metadata.get("validated_supply")
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise wire.VectorError("validated_supply metadata must be an object")
    expected = {
        "contract_version",
        "intel_tdx_allocation",
        "fixed_burn_allocation",
        "burn_hotkey",
    }
    if set(policy) != expected:
        raise wire.VectorError("validated_supply metadata fields mismatch")
    if policy["contract_version"] != "v2":
        raise wire.VectorError("validated_supply unsupported contract_version")
    try:
        tdx = float(policy["intel_tdx_allocation"])
        fixed_burn = float(policy["fixed_burn_allocation"])
    except (TypeError, ValueError) as exc:
        raise wire.VectorError("validated_supply allocations must be numeric") from exc
    if not math.isclose(tdx, 0.90, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply Intel TDX allocation must equal 0.90")
    if not math.isclose(fixed_burn, 0.10, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply fixed burn allocation must equal 0.10")
    if not math.isclose(tdx + fixed_burn, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply allocations must sum to 1")
    burn_hotkey = policy["burn_hotkey"]
    snap = payload.get("burn_snapshot") or {}
    if not isinstance(burn_hotkey, str) or not burn_hotkey:
        raise wire.VectorError("validated_supply burn_hotkey is missing")
    if snap.get("burn_hotkey") != burn_hotkey:
        raise wire.VectorError("validated_supply burn_hotkey does not match snapshot")
    if snap.get("burn_uid") is not None:
        raise wire.VectorError("validated_supply burn destination must not pin a UID")
    try:
        burn_percentage = float(snap["forced_burn_percentage"])
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError("validated_supply burn percentage is missing") from exc
    if not math.isclose(burn_percentage, 10.0, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply fixed burn allocation must burn 10%")
    return policy


def _resolve_burn_hotkey(
    payload: dict[str, Any], hotkey_to_uid: dict[str, int]
) -> dict[str, Any]:
    """Resolve a signed burn hotkey against this tick's metagraph snapshot."""
    snap = payload.get("burn_snapshot") or {}
    burn_hotkey = snap.get("burn_hotkey")
    if burn_hotkey is None:
        return payload
    if burn_hotkey not in hotkey_to_uid:
        raise wire.VectorError(
            f"burn hotkey {burn_hotkey!r} has no current metagraph UID"
        )
    resolved_uid = hotkey_to_uid[burn_hotkey]
    signed_uid = snap.get("burn_uid")
    if signed_uid is not None and int(signed_uid) != resolved_uid:
        raise wire.VectorError("signed burn UID does not match current burn hotkey")
    resolved = dict(payload)
    resolved["burn_snapshot"] = {**snap, "burn_uid": resolved_uid}
    return resolved


def vector_to_uid_weights(
    payload: dict[str, Any],
    hotkey_to_uid: dict[str, int],
    *,
    require_policy: str | None = None,
) -> dict[int, float]:
    original_payload = payload
    validated_supply = _validated_supply_meta(original_payload)
    payload = _resolve_burn_hotkey(original_payload, hotkey_to_uid)
    snap = payload["burn_snapshot"]
    cp = _confidential_primary_meta(payload)
    # Pinned validators apply ONLY confidential_primary v1. A vector without a
    # valid v1 policy block is rejected here; a malformed block already raised
    # in _confidential_primary_meta. The legacy and v3 branches below are
    # unreachable while the pin is active.
    if require_policy == REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1:
        if cp is None:
            raise wire.VectorError(
                "validator pinned to confidential_primary_v1 but vector carries "
                "no confidential_primary policy block"
            )
        return _confidential_primary_to_uid_weights(payload, cp, hotkey_to_uid)
    if require_policy == REQUIRE_POLICY_VALIDATED_SUPPLY_V1:
        if validated_supply is None:
            raise wire.VectorError(
                "validator pinned to validated_supply_v1 but vector carries "
                "no validated_supply policy block"
            )
        if cp is None:
            raise wire.VectorError(
                "validated_supply_v1 requires confidential_primary evidence"
            )
        return _confidential_primary_to_uid_weights(payload, cp, hotkey_to_uid)
    if cp is not None:
        return _confidential_primary_to_uid_weights(payload, cp, hotkey_to_uid)
    v3_rows = _confidential_tdx_v3_rows(payload)
    if v3_rows is not None:
        mapped_uids: set[int] = set()
        missing = False
        for row in v3_rows:
            hotkey = row["miner_hotkey"]
            if hotkey not in hotkey_to_uid:
                missing = True
                continue
            uid = hotkey_to_uid[hotkey]
            if uid in mapped_uids:
                raise wire.VectorError(
                    f"confidential_tdx v3 duplicate UID {uid} in signed vector"
                )
            mapped_uids.add(uid)

        if missing:
            print(
                "  confidential_tdx v3 map incomplete; falling back to signed base components"
            )
        scores: dict[int, float] = {}
        for row in v3_rows:
            hotkey = row["miner_hotkey"]
            if hotkey not in hotkey_to_uid:
                continue
            uid = hotkey_to_uid[hotkey]
            value = row["base_component"] if missing else row["weight"]
            if value > 0.0:
                scores[uid] = value
        return apply_burn(
            scores,
            burn_uid=snap.get("burn_uid"),
            forced_burn_percentage=float(snap["forced_burn_percentage"]),
        )

    scores: dict[int, float] = {}
    skipped = 0
    for w in payload["weights"]:
        uid = hotkey_to_uid.get(w["miner_hotkey"])
        if uid is None:
            skipped += 1  # deregistered since the vector was composed
            continue
        scores[uid] = scores.get(uid, 0.0) + float(w["weight"])
    if skipped:
        print(f"  ({skipped} hotkeys not in metagraph, skipped)")
    return apply_burn(
        scores,
        burn_uid=snap.get("burn_uid"),
        forced_burn_percentage=float(snap["forced_burn_percentage"]),
    )


# -- chain ----------------------------------------------------------------------


@dataclass(frozen=True)
class ChainPreflight:
    wallet: Any
    subtensor: Any
    hotkey_to_uid: dict[str, int]
    validator_hotkey: str
    validator_uid: int
    block: int | None
    min_allowed_weights: int
    max_weight_limit: float
    commit_reveal_enabled: bool = False
    genesis_hash: str = ""
    subnet_owner_hotkey: str = ""
    blocks_until_next_epoch: int | None = None
    next_epoch_start_block: int | None = None
    weights_rate_limit: int | None = None
    validator_blocks_since_last_update: int | None = None
    uid_mapping_stable_until_block: int | None = None
    replacement_safe_hotkeys: frozenset[str] = frozenset()
    subnet_free_uid_slots: int | None = None
    subnet_max_regs_per_block: int | None = None
    subnet_min_nonimmune_uids: int | None = None
    subnet_immunity_period: int | None = None
    subnet_temporally_immune_uids: int | None = None
    subnet_owner_coldkey: str = ""
    subnet_immune_owner_uids_limit: int | None = None
    subnet_owner_immortal_hotkeys: frozenset[str] = frozenset()
    subnet_max_uids: int | None = None
    subnet_registration_blocks: tuple[tuple[int, str, int], ...] = ()
    subnet_owned_hotkeys: tuple[str, ...] = ()
    finalized_hash: str = ""


@dataclass(frozen=True)
class ChainSubmission:
    success: bool
    extrinsic_hash: str | None = None
    block_hash: str | None = None
    block_number: int | None = None
    finalized: bool = False

    def __bool__(self) -> bool:
        return self.success


@dataclass(frozen=True)
class RecoveredSubmission:
    """Exact finalized thin transaction recovered without another write."""

    attempt_id: str
    policy_version: int
    vector_id: str
    signed_vector_sha256: str
    uid_weights: tuple[tuple[int, float], ...]
    burn_uid: int
    burn_share: float
    extrinsic_hash: str
    block_hash: str
    block_number: int

    @property
    def boundary_detail(self) -> str:
        vector = ",".join(f"{uid}:{weight:.6f}" for uid, weight in self.uid_weights)
        return (
            f"authority=thin uids={len(self.uid_weights)} "
            f"burn_uid={self.burn_uid} burn_share={self.burn_share:.6f} "
            f"vector={vector}"
        )


@dataclass(frozen=True)
class RecoveredAuthoritySubmission:
    """Exact finalized FULL-authority transaction recovered without a write."""

    attempt_id: str
    source_epoch: int
    report_id: str
    uid_weights: tuple[tuple[int, float], ...]
    burn_uid: int
    burn_share: float
    extrinsic_hash: str
    block_hash: str
    block_number: int

    @property
    def boundary_detail(self) -> str:
        vector = ",".join(f"{uid}:{weight:.6f}" for uid, weight in self.uid_weights)
        return (
            f"authority=full_provenance uids={len(self.uid_weights)} "
            f"burn_uid={self.burn_uid} "
            f"burn_share={self.burn_share:.6f} vector={vector}"
        )


CHAIN_OPERATION_DEADLINE_SECS = 180.0
# A write is refused unless its evidence remains valid beyond the entire
# synchronous SDK deadline plus an explicit clock/RPC margin. The mortal era
# is intentionally short and is also bounded by the evidence block window.
SN39_MIN_VALIDITY_MARGIN_SECS = 60.0
SN39_MORTAL_PERIOD_BLOCKS = 4
# A launch write must finalize comfortably before the next epoch. UID targets
# are separately proven replacement-safe for the complete mortal era; there is
# deliberately no automatic second/corrective weight write.
SN39_EPOCH_FINALITY_MARGIN_BLOCKS = 32


@dataclass(frozen=True)
class InclusionPolicy:
    """Policy facts that must still hold at the actual inclusion block."""

    valid_from_block: int
    valid_until_block: int
    valid_from_time: datetime
    valid_until_time: datetime
    require_commit_reveal_disabled: bool = True
    mortal_period_blocks: int = SN39_MORTAL_PERIOD_BLOCKS
    expected_next_epoch_start_block: int | None = None


@dataclass(frozen=True)
class ContinuousAuthorization:
    """Separate root-signed recurring-write authorization."""

    authorization_sha256: str
    submission_journal: str
    launch_attempt_id: str
    release_sha256: str
    reproducer_revision: str
    validator_hotkey: str
    genesis_hash: str
    lanes: tuple[str, ...]
    issued_at: str
    valid_from_time: str
    valid_until_time: str
    valid_from_block: int
    valid_until_block: int
    valid_from_nonce: int
    valid_until_nonce_exclusive: int
    max_attempts: int


def _canonical_policy_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise wire.VectorError("inclusion policy time must be timezone-aware")
    moment = value.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{moment.microsecond // 1000:03d}Z"
    )


def _inclusion_policy_identity(policy: InclusionPolicy) -> dict[str, Any]:
    return {
        "valid_from_block": policy.valid_from_block,
        "valid_until_block": policy.valid_until_block,
        "valid_from_time": _canonical_policy_time(policy.valid_from_time),
        "valid_until_time": _canonical_policy_time(policy.valid_until_time),
        "require_commit_reveal_disabled": policy.require_commit_reveal_disabled,
        "mortal_period_blocks": policy.mortal_period_blocks,
        "expected_next_epoch_start_block": policy.expected_next_epoch_start_block,
    }


def _continuous_authorization_identity(
    authorization: ContinuousAuthorization,
) -> dict[str, Any]:
    return {
        "authorization_sha256": authorization.authorization_sha256,
        "submission_journal": authorization.submission_journal,
        "launch_attempt_id": authorization.launch_attempt_id,
        "release_sha256": authorization.release_sha256,
        "reproducer_revision": authorization.reproducer_revision,
        "validator_hotkey": authorization.validator_hotkey,
        "genesis_hash": authorization.genesis_hash,
        "lanes": list(authorization.lanes),
        "issued_at": authorization.issued_at,
        "valid_from_time": authorization.valid_from_time,
        "valid_until_time": authorization.valid_until_time,
        "valid_from_block": authorization.valid_from_block,
        "valid_until_block": authorization.valid_until_block,
        "valid_from_nonce": authorization.valid_from_nonce,
        "valid_until_nonce_exclusive": authorization.valid_until_nonce_exclusive,
        "max_attempts": authorization.max_attempts,
    }


def _require_inclusion_policy_ready(
    policy: InclusionPolicy,
    preflight: ChainPreflight,
    *,
    now: datetime | None = None,
) -> None:
    """Refuse a write whose off-chain evidence can expire during submission."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise wire.VectorError("submission clock must be timezone-aware")
    if preflight.block is None:
        raise wire.VectorError("inclusion policy requires a finalized chain block")
    period = policy.mortal_period_blocks
    if (
        isinstance(period, bool)
        or not isinstance(period, int)
        or period < 4
        or period > 65536
        or period & (period - 1)
    ):
        raise wire.VectorError(
            "inclusion policy mortal period must be a power of two from 4 to 65536"
        )
    if not (
        policy.valid_from_block <= preflight.block < policy.valid_until_block
        and policy.valid_until_block - preflight.block >= period
    ):
        raise wire.VectorError(
            "submission lacks a full mortal era inside the evidence block window"
        )
    if not policy.valid_from_time <= moment < policy.valid_until_time:
        raise wire.VectorError(
            "submission time is outside the evidence inclusion window"
        )
    minimum = CHAIN_OPERATION_DEADLINE_SECS + SN39_MIN_VALIDITY_MARGIN_SECS
    if (policy.valid_until_time - moment).total_seconds() < minimum:
        raise wire.VectorError(
            "evidence validity remaining is shorter than the bounded submission "
            f"window ({minimum:.0f}s required)"
        )
    if policy.require_commit_reveal_disabled and preflight.commit_reveal_enabled:
        raise wire.VectorError(
            "inclusion policy requires commit-reveal disabled before submission"
        )
    rate_limit = preflight.weights_rate_limit
    blocks_since_update = preflight.validator_blocks_since_last_update
    if (
        isinstance(rate_limit, bool)
        or not isinstance(rate_limit, int)
        or rate_limit < 0
        or isinstance(blocks_since_update, bool)
        or not isinstance(blocks_since_update, int)
        or blocks_since_update < 0
    ):
        raise wire.VectorError(
            "submission cannot prove the live validator weight-update cooldown"
        )
    # The runtime currently permits equality while the locked SDK helper uses
    # strict greater-than. This direct extrinsic path conservatively observes
    # the stricter boundary so an SDK/runtime change cannot make launch timing
    # optimistic.
    if blocks_since_update <= rate_limit:
        raise wire.VectorError(
            "submission is inside the live validator weight-update cooldown"
        )
    remaining = preflight.blocks_until_next_epoch
    next_epoch = preflight.next_epoch_start_block
    required_epoch_room = (
        policy.mortal_period_blocks + SN39_EPOCH_FINALITY_MARGIN_BLOCKS
    )
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, int)
        or isinstance(next_epoch, bool)
        or not isinstance(next_epoch, int)
        or next_epoch != preflight.block + remaining
        or policy.expected_next_epoch_start_block != next_epoch
        or remaining < required_epoch_room
    ):
        raise wire.VectorError(
            "submission cannot prove the exact next epoch with enough room for "
            "mortal inclusion and finalized verification"
        )


def _prove_target_hotkey_rotation(
    substrate: Any,
    *,
    block_number: int,
    coldkey: str,
    target_hotkey: str,
) -> dict[str, Any]:
    """Prove the exact successful ``swap_hotkey_v2`` that created a target.

    ``LastHotkeySwapOnNetuid`` is keyed by coldkey and therefore cannot, by
    itself, prove which hotkey was rotated. This archive proof binds the
    cooldown block to the decoded call, signer, successful execution, and the
    pallet event for the exact current target hotkey.
    """
    if (
        isinstance(block_number, bool)
        or not isinstance(block_number, int)
        or block_number <= 0
        or not coldkey
        or not target_hotkey
    ):
        raise wire.VectorError("target hotkey rotation identity is malformed")
    try:
        block_hash = str(substrate.get_block_hash(block_number)).lower()
        canonical_number = int(substrate.get_block_number(block_hash))
        block = substrate.get_block(block_hash=block_hash)
    except Exception as exc:  # noqa: BLE001 - archive proof must fail closed
        raise wire.VectorError(
            "submission cannot retrieve the target hotkey rotation block"
        ) from exc
    if (
        _CHAIN_HASH_RE.fullmatch(block_hash) is None
        or canonical_number != block_number
        or not isinstance(block, dict)
        or not isinstance(block.get("extrinsics"), (list, tuple))
    ):
        raise wire.VectorError("target hotkey rotation block is non-canonical")

    matching: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(block["extrinsics"]):
        observed = getattr(item, "value", None)
        if not isinstance(observed, dict):
            continue
        call = observed.get("call")
        if not isinstance(call, dict):
            continue
        if (
            observed.get("address") == coldkey
            and call.get("call_module") == "SubtensorModule"
            and call.get("call_function") == "swap_hotkey_v2"
            and _chain_call_arg(call, "new_hotkey") == target_hotkey
            and _chain_call_arg(call, "netuid") == 39
        ):
            matching.append((index, observed))
    if len(matching) != 1:
        raise wire.VectorError(
            "target hotkey rotation block has no unique exact swap_hotkey_v2 call"
        )
    extrinsic_index, observed = matching[0]
    call = observed["call"]
    extrinsic_hash = str(observed.get("extrinsic_hash", "")).lower()
    old_hotkey = _chain_call_arg(call, "hotkey")
    keep_stake = _chain_call_arg(call, "keep_stake")
    if (
        _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None
        or not isinstance(old_hotkey, str)
        or not old_hotkey
        or old_hotkey == target_hotkey
        or not isinstance(keep_stake, bool)
    ):
        raise wire.VectorError("target hotkey rotation call is malformed")
    try:
        receipt = substrate.retrieve_extrinsic_by_hash(block_hash, extrinsic_hash)
        receipt_index = getattr(receipt, "extrinsic_idx", None)
        receipt_success = getattr(receipt, "is_success", None)
        receipt_error = getattr(receipt, "error_message", None)
        triggered_events = getattr(receipt, "triggered_events", None)
        timestamp_value = substrate.query(
            module="Timestamp",
            storage_function="Now",
            block_hash=block_hash,
        )
        timestamp_ms = getattr(timestamp_value, "value", timestamp_value)
    except Exception as exc:  # noqa: BLE001 - archive proof must fail closed
        raise wire.VectorError(
            "submission cannot prove target hotkey rotation execution"
        ) from exc
    try:
        receipt_index_value = int(receipt_index)
    except (TypeError, ValueError) as exc:
        raise wire.VectorError(
            "target hotkey rotation has no exact successful execution receipt"
        ) from exc
    if (
        isinstance(receipt_index, bool)
        or receipt_index_value != extrinsic_index
        or receipt_success is not True
        or receipt_error is not None
        or not isinstance(triggered_events, (list, tuple))
        or isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms <= 0
    ):
        raise wire.VectorError(
            "target hotkey rotation has no exact successful execution receipt"
        )
    matching_events = []
    for event in triggered_events:
        event_data = event.get("event") if isinstance(event, dict) else None
        if (
            isinstance(event_data, dict)
            and event_data.get("module_id") == "SubtensorModule"
            and event_data.get("event_id") == "HotkeySwappedOnSubnet"
            and isinstance(event_data.get("attributes"), dict)
            and event_data["attributes"].get("coldkey") == coldkey
            and event_data["attributes"].get("old_hotkey") == old_hotkey
            and event_data["attributes"].get("new_hotkey") == target_hotkey
            and event_data["attributes"].get("netuid") == 39
        ):
            matching_events.append(event_data)
    if len(matching_events) != 1:
        raise wire.VectorError(
            "target hotkey rotation has no unique matching pallet event"
        )
    block_time = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
    return {
        "call": "swap_hotkey_v2",
        "extrinsic_hash": extrinsic_hash,
        "block_hash": block_hash,
        "block_number": block_number,
        "block_timestamp": block_time.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "extrinsic_index": extrinsic_index,
        "coldkey": coldkey,
        "old_hotkey": old_hotkey,
        "new_hotkey": target_hotkey,
        "netuid": 39,
        "keep_stake": keep_stake,
        "event": "HotkeySwappedOnSubnet",
    }


def _require_uid_mapping_stability(
    preflight: ChainPreflight,
    uid_hotkeys: dict[int, str],
    *,
    mortal_period_blocks: int,
) -> dict[str, Any]:
    if (
        isinstance(mortal_period_blocks, bool)
        or not isinstance(mortal_period_blocks, int)
        or mortal_period_blocks != SN39_MORTAL_PERIOD_BLOCKS
        or not uid_hotkeys
    ):
        raise wire.VectorError("UID stability check has an invalid mortal vector")
    safe_hotkeys = preflight.replacement_safe_hotkeys
    if not isinstance(safe_hotkeys, frozenset) or not safe_hotkeys:
        raise wire.VectorError(
            "submission cannot prove any UID mapping replacement-safe"
        )
    unsafe = sorted(set(uid_hotkeys.values()) - safe_hotkeys)
    if unsafe:
        raise wire.VectorError(
            "submission cannot prove UID mappings stable for the complete mortal "
            f"era ({len(unsafe)} target hotkey(s) are not chain-immune)"
        )
    # `set_mechanism_weights` binds only UIDs. A registered hotkey can be
    # replaced at the same UID by `swap_hotkey_v2` during the four-block mortal
    # era, so registration immunity alone is insufficient. On Finney, publish
    # the exact rotation-lock state of every target and refuse any target whose
    # coldkey has a pending ownership transfer. A live rotation lock is proven
    # end to end when one exists, but it is not required to submit: only the
    # target coldkey owner can rotate a target, both SN39 targets are
    # operator-controlled, and requiring a live lock would force a fresh
    # rotation to a new hotkey before every single broadcast.
    finney = str(preflight.genesis_hash).lower() == FINNEY_GENESIS_HASH
    if not finney:
        registration_safety: dict[str, Any] = {
            "status": "not_applicable_non_finney",
            "replacement_safe_hotkeys": sorted(preflight.replacement_safe_hotkeys),
        }
        rotation_safety = {
            "status": "not_applicable_non_finney",
            "mapping_block": preflight.block,
            "mortal_period_blocks": mortal_period_blocks,
            "targets": [],
        }
    else:
        if (
            preflight.block is None
            or preflight.subnet_max_uids is None
            or preflight.subnet_max_regs_per_block is None
            or preflight.subnet_immunity_period is None
            or preflight.subnet_min_nonimmune_uids is None
            or preflight.subnet_free_uid_slots is None
            or preflight.subnet_immune_owner_uids_limit is None
            or not preflight.subnet_owner_coldkey
            or not preflight.subnet_registration_blocks
        ):
            raise wire.VectorError(
                "submission cannot publish the raw UID replacement-safety inputs"
            )
        registration_rows = [
            {
                "uid": uid,
                "hotkey": hotkey,
                "block_at_registration": registered_at,
            }
            for uid, hotkey, registered_at in preflight.subnet_registration_blocks
        ]
        registration_safety = {
            "max_uids": preflight.subnet_max_uids,
            "max_regs_per_block": preflight.subnet_max_regs_per_block,
            "immunity_period": preflight.subnet_immunity_period,
            "min_nonimmune_uids": preflight.subnet_min_nonimmune_uids,
            "block_at_registration": registration_rows,
            "subnet_owner_coldkey": preflight.subnet_owner_coldkey,
            "owned_hotkeys": list(preflight.subnet_owned_hotkeys),
            "immune_owner_uids_limit": preflight.subnet_immune_owner_uids_limit,
            "free_uid_slots": preflight.subnet_free_uid_slots,
            "maximum_era_registrations": (
                preflight.subnet_max_regs_per_block * mortal_period_blocks
            ),
            "owner_immortal_hotkeys": sorted(preflight.subnet_owner_immortal_hotkeys),
            "replacement_safe_hotkeys": sorted(preflight.replacement_safe_hotkeys),
        }
        subtensor = preflight.subtensor
        substrate = getattr(subtensor, "substrate", None)
        if substrate is None:
            raise wire.VectorError(
                "submission cannot prove hotkey-rotation safety without substrate"
            )

        def storage_value(name: str, params: list[Any]) -> Any:
            try:
                observed = subtensor.query_subtensor(
                    name=name,
                    params=params,
                    block=preflight.block,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed at write boundary
                raise wire.VectorError(
                    f"submission cannot query {name} at the proven mapping block"
                ) from exc
            return getattr(observed, "value", observed)

        try:
            block_hash = str(substrate.get_block_hash(preflight.block)).lower()
            if _CHAIN_HASH_RE.fullmatch(block_hash) is None:
                raise ValueError("mapping block hash is malformed")

            def constant_value(name: str) -> Any:
                observed = substrate.get_constant(
                    module_name="SubtensorModule",
                    constant_name=name,
                    block_hash=block_hash,
                )
                return getattr(observed, "value", observed)

            hotkey_swap_interval = int(constant_value("HotkeySwapOnSubnetInterval"))
            coldkey_swap_delay = int(storage_value("ColdkeySwapAnnouncementDelay", []))
        except (AttributeError, TypeError, ValueError) as exc:
            raise wire.VectorError(
                "submission cannot prove the live hotkey/coldkey swap constants"
            ) from exc
        if hotkey_swap_interval < mortal_period_blocks:
            raise wire.VectorError(
                "live hotkey swap cooldown is shorter than the complete mortal era"
            )
        if coldkey_swap_delay < mortal_period_blocks:
            raise wire.VectorError(
                "live coldkey swap announcement delay is shorter than the mortal era"
            )

        era_last_block = preflight.block + mortal_period_blocks - 1
        targets: list[dict[str, Any]] = []
        for uid, hotkey in sorted(uid_hotkeys.items()):
            coldkey = str(storage_value("Owner", [hotkey]) or "")
            if not coldkey:
                raise wire.VectorError(
                    f"submission cannot prove the owner for target UID {uid}"
                )
            raw_last_swap = storage_value(
                "LastHotkeySwapOnNetuid",
                [39, coldkey],
            )
            try:
                last_swap_block = int(raw_last_swap)
            except (TypeError, ValueError) as exc:
                raise wire.VectorError(
                    f"submission cannot prove the last hotkey swap for target UID {uid}"
                ) from exc
            pending_coldkey_swap = storage_value(
                "ColdkeySwapAnnouncements",
                [coldkey],
            )
            if pending_coldkey_swap is not None:
                raise wire.VectorError(
                    f"target UID {uid} has a pending coldkey swap announcement"
                )
            if last_swap_block > 0:
                safe_until_block = last_swap_block + hotkey_swap_interval
                swap_lock = (
                    "active" if era_last_block <= safe_until_block else "expired"
                )
            else:
                safe_until_block = None
                swap_lock = "never_rotated"
            receipt: dict[str, Any] | None = None
            hotkey_root: str | None = None
            if swap_lock == "active":
                # A claimed live lock must still be proven: the exact successful
                # swap_hotkey_v2 at the cooldown block, and the lineage it left.
                receipt = _prove_target_hotkey_rotation(
                    substrate,
                    block_number=last_swap_block,
                    coldkey=coldkey,
                    target_hotkey=hotkey,
                )
                successor = storage_value("HotkeySuccessor", [39, hotkey])
                root = storage_value("HotkeyRoot", [39, hotkey])
                old_successor = storage_value(
                    "HotkeySuccessor",
                    [39, receipt["old_hotkey"]],
                )
                old_root = storage_value(
                    "HotkeyRoot",
                    [39, receipt["old_hotkey"]],
                )
                expected_root = (
                    str(old_root)
                    if old_root not in (None, "")
                    else str(receipt["old_hotkey"])
                )
                if (
                    successor not in (None, "")
                    or root in (None, "")
                    or str(old_successor) != hotkey
                    or str(root) != expected_root
                ):
                    raise wire.VectorError(
                        f"target UID {uid} hotkey lineage does not match its rotation"
                    )
                hotkey_root = str(root)
            targets.append(
                {
                    "uid": uid,
                    "hotkey": hotkey,
                    "coldkey": coldkey,
                    "last_hotkey_swap_block": last_swap_block,
                    "hotkey_swap_safe_until_block": safe_until_block,
                    "swap_lock": swap_lock,
                    "pending_coldkey_swap": None,
                    "hotkey_successor": None,
                    "hotkey_root": hotkey_root,
                    "rotation_receipt": receipt,
                    "registration_replacement_safe": hotkey in safe_hotkeys,
                }
            )
        rotation_safety = {
            "status": PASS,
            "mapping_block": preflight.block,
            "mapping_block_hash": block_hash,
            "mortal_period_blocks": mortal_period_blocks,
            "era_last_block": era_last_block,
            "hotkey_swap_on_subnet_interval": hotkey_swap_interval,
            "coldkey_swap_announcement_delay": coldkey_swap_delay,
            "targets": targets,
        }
    return {
        "schema": "cathedral_sn39_uid_safety_v2",
        # Records the launch assumption this proof rests on: every target
        # coldkey is operator-controlled, so a target hotkey cannot be replaced
        # mid-era by anyone else.
        "stability_basis": "operator_controlled_coldkeys",
        "registration": registration_safety,
        "rotation": rotation_safety,
    }


def _require_launch_evidence_after_rotations(
    *,
    payload: dict[str, Any],
    audit: Any,
    uid_safety: dict[str, Any],
) -> dict[str, Any]:
    """Bind every launch-generation boundary strictly after proven rotations.

    Targets that carry no live rotation lock contribute no receipt and no
    floor. When no target does, there is nothing for the evidence to postdate
    and the boundary records a null floor.
    """
    try:
        targets = uid_safety["rotation"]["targets"]
        receipts = [
            row["rotation_receipt"]
            for row in targets
            if row["rotation_receipt"] is not None
        ]
        rotation_blocks = [int(row["block_number"]) for row in receipts]
        rotation_times = [
            wire._parse_canonical_utc(
                row["block_timestamp"],
                field="rotation block timestamp",
            )
            for row in receipts
        ]
        candidate_block = int(getattr(audit, "candidate_block"))
        candidate_block_hash = str(getattr(audit, "candidate_block_hash"))
        report_valid_from_block = int(getattr(audit, "report_valid_from_block"))
        manifest_generated = wire._parse_canonical_utc(
            getattr(audit, "manifest_generated_at", None),
            field="evidence manifest generated_at",
        )
        report_generated = wire._parse_canonical_utc(
            getattr(audit, "report_generated_at", None),
            field="provenance report generated_at",
        )
        vector_generated = wire._parse_canonical_utc(
            payload.get("generated_at"),
            field="generated_at",
        )
        signed_index = getattr(audit, "signed_index", None)
        index_generated = wire._parse_canonical_utc(
            signed_index["generated_at"],
            field="evidence index generated_at",
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "launch evidence has no exact post-rotation generation boundary"
        ) from exc
    if len(targets) != 2 or _CHAIN_HASH_RE.fullmatch(candidate_block_hash) is None:
        raise wire.VectorError(
            "launch evidence does not name the two canonical targets at a "
            "canonical candidate block"
        )
    rotation_floor_block: int | None = None
    rotation_floor_timestamp: str | None = None
    if receipts:
        rotation_floor_block = max(rotation_blocks)
        rotation_floor_time = max(rotation_times)
        rotation_floor_timestamp = rotation_floor_time.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        if (
            candidate_block <= rotation_floor_block
            or report_valid_from_block <= rotation_floor_block
            or any(
                generated <= rotation_floor_time
                for generated in (
                    manifest_generated,
                    report_generated,
                    vector_generated,
                    index_generated,
                )
            )
        ):
            raise wire.VectorError(
                "launch candidate, evidence, report, vector, and index must all "
                "be generated strictly after every proven target rotation"
            )
    return {
        "schema": "cathedral_sn39_post_rotation_evidence_v2",
        "rotation_floor_block": rotation_floor_block,
        "rotation_floor_timestamp": rotation_floor_timestamp,
        "candidate_block": candidate_block,
        "candidate_block_hash": candidate_block_hash.lower(),
        "manifest_generated_at": getattr(audit, "manifest_generated_at"),
        "report_generated_at": getattr(audit, "report_generated_at"),
        "report_valid_from_block": report_valid_from_block,
        "vector_generated_at": payload["generated_at"],
        "index_generated_at": signed_index["generated_at"],
    }


def _vector_inclusion_policy(
    payload: dict[str, Any],
    preflight: ChainPreflight,
) -> InclusionPolicy:
    if preflight.block is None:
        raise wire.VectorError("signed vector inclusion requires a finalized block")
    policy = InclusionPolicy(
        valid_from_block=preflight.block,
        valid_until_block=preflight.block + SN39_MORTAL_PERIOD_BLOCKS,
        valid_from_time=wire._parse_canonical_utc(
            payload.get("generated_at"),
            field="generated_at",
        ),
        valid_until_time=wire._parse_canonical_utc(
            payload.get("expires_at"),
            field="expires_at",
        ),
        expected_next_epoch_start_block=preflight.next_epoch_start_block,
    )
    _require_inclusion_policy_ready(policy, preflight)
    return policy


def _authority_inclusion_policy(
    audit: Any,
    preflight: ChainPreflight,
) -> InclusionPolicy:
    valid_from_block = getattr(audit, "report_valid_from_block", None)
    valid_until_block = getattr(audit, "report_valid_until_block", None)
    if (
        isinstance(valid_from_block, bool)
        or isinstance(valid_until_block, bool)
        or not isinstance(valid_from_block, int)
        or not isinstance(valid_until_block, int)
    ):
        raise wire.VectorError(
            "authority report has no canonical block-validity window"
        )
    policy = InclusionPolicy(
        valid_from_block=valid_from_block,
        valid_until_block=valid_until_block,
        valid_from_time=wire._parse_canonical_utc(
            getattr(audit, "report_generated_at", None),
            field="provenance report generated_at",
        ),
        valid_until_time=wire._parse_canonical_utc(
            getattr(audit, "report_valid_until", None),
            field="provenance report valid_until",
        ),
        expected_next_epoch_start_block=preflight.next_epoch_start_block,
    )
    _require_inclusion_policy_ready(policy, preflight)
    return policy


@contextlib.contextmanager
def _isolated_argv():
    """Hide sys.argv from bittensor while it builds its own config.

    bittensor parses sys.argv to build a config and defines its OWN `--config`
    flag. When this validator is launched as `cathedral-validator serve --config
    my.toml`, that `--config` leaks into bittensor, which then tries to YAML-load
    our TOML and aborts the tick with `Error loading config` (seen on some
    bittensor versions, not all). Blanking argv around bittensor construction
    keeps the two CLIs from colliding.
    """
    saved = sys.argv
    sys.argv = sys.argv[:1]
    try:
        yield
    finally:
        sys.argv = saved


@contextlib.contextmanager
def _chain_operation_deadline(label: str, seconds: float):
    """Bound one synchronous Bittensor operation in the validator process.

    Releasing a lock while an abandoned worker thread can still submit is not
    safe, so chain calls are not delegated to timeout threads. The production
    validator runs on the main POSIX thread, where ``ITIMER_REAL`` interrupts
    the call in place. Any unsupported execution context or competing process
    timer fails closed before chain access.
    """
    import signal

    if not math.isfinite(seconds) or seconds <= 0.0:
        raise wire.VectorError("chain operation deadline must be positive and finite")
    if threading.current_thread() is not threading.main_thread():
        raise wire.VectorError(
            f"{label} requires the validator main thread for a safe wall-clock deadline"
        )
    if not all(
        hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL", "setitimer")
    ):
        raise wire.VectorError(
            f"{label} cannot establish the required process wall-clock deadline"
        )
    existing = signal.getitimer(signal.ITIMER_REAL)
    if existing[0] > 0.0 or existing[1] > 0.0:
        raise wire.VectorError(
            f"{label} refuses to replace an existing process wall-clock timer"
        )
    prior_handler = signal.getsignal(signal.SIGALRM)

    def expired(_signum, _frame):
        raise wire.VectorError(
            f"{label} exceeded its {seconds:.0f}s wall-clock deadline"
        )

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prior_handler)


def _validate_emission_vector(uid_weights: dict[int, float]) -> None:
    if not uid_weights:
        raise wire.VectorError("chain preflight requires a non-empty vector")
    if any(
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid < 0
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for uid, value in uid_weights.items()
    ):
        raise wire.VectorError("chain preflight vector is invalid")
    total = math.fsum(float(value) for value in uid_weights.values())
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise wire.VectorError(f"chain preflight vector mass {total!r} != 1.0")


def _validate_chain_constraints(
    uid_weights: dict[int, float], preflight: ChainPreflight
) -> None:
    if preflight.commit_reveal_enabled:
        raise wire.VectorError(
            "SN39 release proof requires a directly applied set_mechanism_weights "
            "extrinsic; commit-reveal is enabled, so refusing before submission"
        )
    positive = [float(value) for value in uid_weights.values() if float(value) > 0.0]
    if len(positive) < preflight.min_allowed_weights:
        raise wire.VectorError(
            "chain preflight vector has fewer positives than min_allowed_weights"
        )
    limit = preflight.max_weight_limit
    if not math.isfinite(limit) or not 0.0 < limit <= 1.0:
        raise wire.VectorError("chain preflight max_weight_limit is invalid")
    if any(value > limit + 1e-9 for value in positive):
        raise wire.VectorError("chain preflight vector exceeds max_weight_limit")
    if len(positive) * limit < 1.0 - 1e-9:
        raise wire.VectorError("chain preflight vector cannot conserve mass")


def _require_no_validator_compute_reward(
    uid_weights: dict[int, float],
    *,
    preflight: ChainPreflight,
    burn_uid: int | None,
) -> None:
    """Prevent a validator from assigning validated-compute mass to itself."""
    validator_weight = float(uid_weights.get(preflight.validator_uid, 0.0))
    if validator_weight > 0.0 and preflight.validator_uid != burn_uid:
        raise wire.VectorError(
            "SN39 validator hotkey cannot receive validated-compute weight"
        )


def chain_preflight(
    *,
    network: str,
    netuid: int,
    wallet_name: str,
    wallet_hotkey: str,
    deadline_secs: float = CHAIN_OPERATION_DEADLINE_SECS,
) -> ChainPreflight:
    """Resolve validator identity and constraints under one wall-clock bound."""
    with _chain_operation_deadline("chain preflight", deadline_secs):
        return _chain_preflight_unbounded(
            network=network,
            netuid=netuid,
            wallet_name=wallet_name,
            wallet_hotkey=wallet_hotkey,
        )


def _chain_preflight_unbounded(
    *, network: str, netuid: int, wallet_name: str, wallet_hotkey: str
) -> ChainPreflight:
    with _isolated_argv():
        import bittensor as bt

        wallet = _bt_wallet(bt)(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = _bt_subtensor(bt)(network=connection_target(network))
        finalized_block, _finalized_hash = _finalized_chain_head(subtensor)
        metagraph = subtensor.metagraph(netuid, block=finalized_block)
    raw_uids = (
        metagraph.uids.tolist() if hasattr(metagraph.uids, "tolist") else metagraph.uids
    )
    uids = [int(value) for value in raw_uids]
    hotkeys = [str(value) for value in metagraph.hotkeys]
    permits = [bool(value) for value in metagraph.validator_permit]
    if not (len(uids) == len(hotkeys) == len(permits)):
        raise wire.VectorError("metagraph arrays are inconsistent")
    if len(set(uids)) != len(uids) or len(set(hotkeys)) != len(hotkeys):
        raise wire.VectorError("metagraph contains duplicate UID or hotkey")
    hotkey_to_uid = dict(zip(hotkeys, uids))
    validator_hotkey = str(wallet.hotkey.ss58_address)
    if validator_hotkey not in hotkey_to_uid:
        raise wire.VectorError("validator hotkey is not registered on this subnet")
    validator_uid = hotkey_to_uid[validator_hotkey]
    index = hotkeys.index(validator_hotkey)
    if not permits[index]:
        raise wire.VectorError("validator hotkey lacks validator permit")
    block = _finalized_block(getattr(metagraph, "block", None))
    if block != finalized_block:
        raise wire.VectorError("metagraph did not resolve at the finalized chain head")
    subnet_owner_hotkey = str(
        subtensor.get_subnet_owner_hotkey(netuid, block=finalized_block) or ""
    )
    raw_blocks_until_epoch = subtensor.blocks_until_next_epoch(
        netuid,
        block=finalized_block,
    )
    raw_next_epoch_start = subtensor.get_next_epoch_start_block(
        netuid,
        block=finalized_block,
    )
    raw_weights_rate_limit = subtensor.weights_rate_limit(
        netuid,
        block=finalized_block,
    )
    raw_blocks_since_update = subtensor.blocks_since_last_update(
        netuid,
        validator_uid,
        block=finalized_block,
    )
    try:
        max_uids = int(getattr(metagraph, "max_uids"))
        hparams = getattr(metagraph, "hparams")
        max_regs_per_block = int(getattr(hparams, "max_regs_per_block"))
        immunity_period = int(getattr(hparams, "immunity_period"))
        raw_registration_blocks = getattr(metagraph, "block_at_registration")
        registration_blocks = [int(value) for value in raw_registration_blocks]
        min_nonimmune_value = subtensor.query_subtensor(
            name="MinNonImmuneUids",
            params=[netuid],
            block=finalized_block,
        )
        min_nonimmune_uids = int(
            getattr(min_nonimmune_value, "value", min_nonimmune_value)
        )
        owner_coldkey_value = subtensor.query_subtensor(
            name="SubnetOwner",
            params=[netuid],
            block=finalized_block,
        )
        subnet_owner_coldkey = str(
            getattr(owner_coldkey_value, "value", owner_coldkey_value) or ""
        )
        owned_hotkeys_value = subtensor.query_subtensor(
            name="OwnedHotkeys",
            params=[subnet_owner_coldkey],
            block=finalized_block,
        )
        owned_hotkeys = [
            str(value)
            for value in getattr(
                owned_hotkeys_value,
                "value",
                owned_hotkeys_value,
            )
        ]
        owner_limit_value = subtensor.query_subtensor(
            name="ImmuneOwnerUidsLimit",
            params=[netuid],
            block=finalized_block,
        )
        immune_owner_uids_limit = int(
            getattr(owner_limit_value, "value", owner_limit_value)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "subnet registration and immunity policy is unavailable at finalized head"
        ) from exc
    if (
        max_uids < len(uids)
        or max_regs_per_block < 0
        or immunity_period < 0
        or min_nonimmune_uids < 0
        or len(registration_blocks) != len(uids)
        or not subnet_owner_coldkey
        or immune_owner_uids_limit < 0
        or any(not hotkey for hotkey in owned_hotkeys)
        or len(set(owned_hotkeys)) != len(owned_hotkeys)
    ):
        raise wire.VectorError(
            "subnet registration and owner-immunity policy is malformed at "
            "finalized head"
        )
    uid_registration_by_hotkey = {
        hotkey: (uid, registered_at)
        for hotkey, uid, registered_at in zip(
            hotkeys,
            uids,
            registration_blocks,
        )
    }
    owner_rows: list[tuple[int, int, str]] = []
    for hotkey in owned_hotkeys:
        owner_row = uid_registration_by_hotkey.get(hotkey)
        if owner_row is None:
            continue
        uid, registered_at = owner_row
        owner_rows.append((registered_at, uid, hotkey))
    owner_rows.sort()
    owner_immortal_rows = owner_rows[:immune_owner_uids_limit]
    owner_current_row = uid_registration_by_hotkey.get(subnet_owner_hotkey)
    if owner_current_row is not None and all(
        row[2] != subnet_owner_hotkey for row in owner_immortal_rows
    ):
        owner_uid, owner_registered_at = owner_current_row
        owner_immortal_rows.insert(
            0,
            (owner_registered_at, owner_uid, subnet_owner_hotkey),
        )
        owner_immortal_rows = owner_immortal_rows[:immune_owner_uids_limit]
    owner_immortal_hotkeys = {row[2] for row in owner_immortal_rows}
    free_uid_slots = max_uids - len(uids)
    maximum_era_registrations = max_regs_per_block * SN39_MORTAL_PERIOD_BLOCKS
    if max_regs_per_block == 0:
        uid_mapping_stable_until_block = raw_next_epoch_start
    else:
        uid_mapping_stable_until_block = finalized_block + (
            free_uid_slots // max_regs_per_block
        )
    capacity_protects_all = free_uid_slots >= maximum_era_registrations
    temporally_immune_hotkeys = {
        hotkey
        for hotkey, registered_at in zip(hotkeys, registration_blocks)
        if registered_at + immunity_period
        >= finalized_block + SN39_MORTAL_PERIOD_BLOCKS
    }
    prunable_nonimmune_count = sum(
        registered_at + immunity_period <= finalized_block
        and hotkey not in owner_immortal_hotkeys
        for hotkey, registered_at in zip(hotkeys, registration_blocks)
    )
    nonimmune_buffer_protects_immunity = (
        prunable_nonimmune_count
        > min_nonimmune_uids + max(0, maximum_era_registrations - free_uid_slots)
    )
    replacement_safe_hotkeys = (
        set(hotkeys)
        if capacity_protects_all
        else owner_immortal_hotkeys
        | (temporally_immune_hotkeys if nonimmune_buffer_protects_immunity else set())
    )
    if (
        not subnet_owner_hotkey
        or isinstance(raw_blocks_until_epoch, bool)
        or not isinstance(raw_blocks_until_epoch, int)
        or raw_blocks_until_epoch < 0
        or isinstance(raw_next_epoch_start, bool)
        or not isinstance(raw_next_epoch_start, int)
        or raw_next_epoch_start != finalized_block + raw_blocks_until_epoch
        or isinstance(raw_weights_rate_limit, bool)
        or not isinstance(raw_weights_rate_limit, int)
        or raw_weights_rate_limit < 0
        or isinstance(raw_blocks_since_update, bool)
        or not isinstance(raw_blocks_since_update, int)
        or raw_blocks_since_update < 0
        or max_uids < len(uids)
        or max_regs_per_block < 0
        or immunity_period < 0
        or min_nonimmune_uids < 0
        or len(registration_blocks) != len(uids)
        or not subnet_owner_coldkey
        or immune_owner_uids_limit < 0
    ):
        raise wire.VectorError(
            "subnet owner, exact epoch schedule, registration capacity, or "
            "validator weight cooldown is unavailable at finalized head"
        )
    result = ChainPreflight(
        wallet=wallet,
        subtensor=subtensor,
        hotkey_to_uid=hotkey_to_uid,
        validator_hotkey=validator_hotkey,
        validator_uid=validator_uid,
        block=block,
        min_allowed_weights=int(
            subtensor.min_allowed_weights(netuid=netuid, block=finalized_block)
        ),
        max_weight_limit=float(
            subtensor.max_weight_limit(netuid=netuid, block=finalized_block)
        ),
        commit_reveal_enabled=_strict_commit_reveal_state(
            subtensor.commit_reveal_enabled(netuid=netuid, block=finalized_block)
        ),
        genesis_hash=_canonical_genesis_hash(subtensor),
        subnet_owner_hotkey=subnet_owner_hotkey,
        blocks_until_next_epoch=raw_blocks_until_epoch,
        next_epoch_start_block=raw_next_epoch_start,
        weights_rate_limit=raw_weights_rate_limit,
        validator_blocks_since_last_update=raw_blocks_since_update,
        uid_mapping_stable_until_block=uid_mapping_stable_until_block,
        replacement_safe_hotkeys=frozenset(replacement_safe_hotkeys),
        subnet_free_uid_slots=free_uid_slots,
        subnet_max_regs_per_block=max_regs_per_block,
        subnet_min_nonimmune_uids=min_nonimmune_uids,
        subnet_immunity_period=immunity_period,
        subnet_temporally_immune_uids=len(temporally_immune_hotkeys),
        subnet_owner_coldkey=subnet_owner_coldkey,
        subnet_immune_owner_uids_limit=immune_owner_uids_limit,
        subnet_owner_immortal_hotkeys=frozenset(owner_immortal_hotkeys),
        subnet_max_uids=max_uids,
        subnet_registration_blocks=tuple(
            (uid, hotkey, registered_at)
            for uid, hotkey, registered_at in zip(
                uids,
                hotkeys,
                registration_blocks,
            )
        ),
        subnet_owned_hotkeys=tuple(owned_hotkeys),
        finalized_hash=_finalized_hash,
    )
    _lifecycle(
        "PREFLIGHT complete",
        f"validator_hotkey={validator_hotkey} validator_uid={result.validator_uid} "
        f"block={block if block is not None else 'unknown'} "
        f"min_allowed={result.min_allowed_weights} max_limit={result.max_weight_limit} "
        f"commit_reveal={str(result.commit_reveal_enabled).lower()} "
        f"blocks_until_epoch={result.blocks_until_next_epoch} "
        f"next_epoch={result.next_epoch_start_block} "
        f"weights_rate_limit={result.weights_rate_limit} "
        f"blocks_since_update={result.validator_blocks_since_last_update} "
        f"free_uid_slots={result.subnet_free_uid_slots} "
        f"max_regs_per_block={result.subnet_max_regs_per_block} "
        f"min_nonimmune_uids={result.subnet_min_nonimmune_uids} "
        f"temporally_immune_uids={result.subnet_temporally_immune_uids} "
        f"owner_immortal_uids={len(result.subnet_owner_immortal_hotkeys)} "
        f"owner_immune_limit={result.subnet_immune_owner_uids_limit} "
        f"replacement_safe_uids={len(result.replacement_safe_hotkeys)} "
        f"capacity_stable_until={result.uid_mapping_stable_until_block}",
    )
    return result


def _canonical_genesis_hash(subtensor: Any) -> str:
    """Return the exact genesis hash that namespaces a chain identity."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        raise wire.VectorError("subtensor has no substrate genesis interface")
    try:
        value = str(substrate.get_block_hash(0)).lower()
    except (AttributeError, TypeError, ValueError) as exc:
        raise wire.VectorError("cannot resolve the canonical chain genesis") from exc
    if _CHAIN_HASH_RE.fullmatch(value) is None:
        raise wire.VectorError("canonical chain genesis hash is malformed")
    return value


def _strict_commit_reveal_state(value: Any) -> bool:
    if not isinstance(value, bool):
        raise wire.VectorError("chain commit-reveal state is not an explicit boolean")
    return value


def _finalized_chain_head(subtensor: Any) -> tuple[int, str]:
    """Resolve one canonical finalized chain height/hash pair."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        raise wire.VectorError("subtensor has no substrate finality interface")
    try:
        block_hash = str(substrate.get_chain_finalised_head())
        block_number = _finalized_block(substrate.get_block_number(block_hash))
        canonical_hash = (
            str(substrate.get_block_hash(block_number))
            if block_number is not None
            else ""
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise wire.VectorError("cannot resolve the finalized chain head") from exc
    if (
        block_number is None
        or _CHAIN_HASH_RE.fullmatch(block_hash) is None
        or canonical_hash.lower() != block_hash.lower()
    ):
        raise wire.VectorError("finalized chain head is malformed or non-canonical")
    return block_number, block_hash.lower()


def _canonical_receipt_block_number(subtensor: Any, block_hash: str) -> int:
    """Resolve and reverse-check the canonical height for a finalized receipt."""
    if not isinstance(block_hash, str) or _CHAIN_HASH_RE.fullmatch(block_hash) is None:
        raise wire.VectorError("submission receipt block hash is malformed")
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        raise wire.VectorError("submission receipt has no canonical chain interface")
    try:
        raw_number = substrate.get_block_number(block_hash)
        canonical_number = _finalized_block(raw_number)
        canonical_hash = (
            str(substrate.get_block_hash(canonical_number))
            if canonical_number is not None
            else ""
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "submission receipt block number is unavailable"
        ) from exc
    if (
        canonical_number is None
        or canonical_number <= 0
        or canonical_hash.lower() != block_hash.lower()
    ):
        raise wire.VectorError(
            "submission receipt block hash and canonical height disagree"
        )
    return canonical_number


def _weight_version_key() -> int:
    """The exact SDK version key committed into the weight extrinsic."""
    from bittensor.core.settings import version_as_int

    return int(version_as_int)


def _wire_weights(uids: list[int], weights: list[float]) -> tuple[list[int], list[int]]:
    from bittensor.utils.weight_utils import convert_and_normalize_weights_and_uids

    wire_uids, wire_values = convert_and_normalize_weights_and_uids(uids, weights)
    return [int(value) for value in wire_uids], [int(value) for value in wire_values]


def _chain_call_arg(call: dict[str, Any], name: str) -> Any:
    for item in call.get("call_args") or ():
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("value")
    return None


def _classify_finalized_receipt(
    subtensor: Any,
    *,
    receipt: Any,
    extrinsic_hash: str,
    block_hash: str,
    block_number: int,
    validator_hotkey: str,
    netuid: int,
    version_key: int,
    wire_uids: list[int],
    wire_weights: list[int],
    uid_hotkeys: dict[int, str] | None = None,
    expected_subnet_owner_hotkey: str | None = None,
    inclusion_policy: InclusionPolicy | None = None,
    require_receipt: bool = True,
) -> str:
    """Classify exact finalized-call proof as PASS, FAIL, or NOT_PROVEN.

    An RPC/archive exception is not evidence of a mismatch. Keeping that case
    distinct prevents a transient read failure from authorizing any second
    chain write after a valid irreversible call.
    """
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        return NOT_PROVEN
    if require_receipt:
        receipt_success = (
            getattr(receipt, "is_success", None) if receipt is not None else None
        )
        if not isinstance(receipt_success, bool):
            return NOT_PROVEN
        if (
            receipt_success is not True
            or getattr(receipt, "error_message", None) is not None
        ):
            return FAIL
    try:
        finalized_hash = str(substrate.get_chain_finalised_head())
        finalized_number = _finalized_block(substrate.get_block_number(finalized_hash))
        canonical_finalized_hash = (
            str(substrate.get_block_hash(finalized_number))
            if finalized_number is not None
            else ""
        )
        canonical_hash = str(substrate.get_block_hash(block_number))
        block = substrate.get_block(block_hash=block_hash)
        inclusion_metagraph = (
            subtensor.metagraph(netuid, block=block_number)
            if uid_hotkeys is not None
            else None
        )
        if inclusion_policy is not None:
            commit_reveal_at_inclusion = subtensor.commit_reveal_enabled(
                netuid=netuid,
                block=block_number,
            )
            timestamp_value = substrate.query(
                module="Timestamp",
                storage_function="Now",
                block_hash=block_hash,
            )
            timestamp_ms = getattr(timestamp_value, "value", timestamp_value)
            if (
                isinstance(timestamp_ms, bool)
                or not isinstance(timestamp_ms, int)
                or timestamp_ms <= 0
            ):
                return NOT_PROVEN
            inclusion_time = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
            inclusion_next_epoch = (
                subtensor.get_next_epoch_start_block(
                    netuid,
                    block=block_number,
                )
                if inclusion_policy.expected_next_epoch_start_block is not None
                else None
            )
        else:
            commit_reveal_at_inclusion = None
            inclusion_time = None
            inclusion_next_epoch = None
        inclusion_owner_hotkey = (
            str(subtensor.get_subnet_owner_hotkey(netuid, block=block_number) or "")
            if expected_subnet_owner_hotkey is not None
            else None
        )
        historical_execution = substrate.retrieve_extrinsic_by_hash(
            block_hash,
            extrinsic_hash,
        )
    except Exception:  # noqa: BLE001 - archive/RPC unavailability is inconclusive
        return NOT_PROVEN
    if not isinstance(block, dict):
        return NOT_PROVEN
    extrinsics = block.get("extrinsics")
    if not isinstance(extrinsics, (list, tuple)):
        return NOT_PROVEN
    matching = [
        (index, item.value)
        for index, item in enumerate(extrinsics)
        if isinstance(getattr(item, "value", None), dict)
        and str(item.value.get("extrinsic_hash", "")).lower() == extrinsic_hash.lower()
    ]
    if not matching:
        return NOT_PROVEN
    if len(matching) > 1:
        return FAIL
    extrinsic_index, observed = matching[0]
    if historical_execution is None:
        return NOT_PROVEN
    historical_success = getattr(historical_execution, "is_success", None)
    historical_index = getattr(historical_execution, "extrinsic_idx", None)
    if not isinstance(historical_success, bool) or isinstance(historical_index, bool):
        return NOT_PROVEN
    try:
        historical_execution_index = int(historical_index)
    except (TypeError, ValueError):
        return NOT_PROVEN
    if (
        historical_success is not True
        or getattr(historical_execution, "error_message", None) is not None
        or historical_execution_index != extrinsic_index
    ):
        return FAIL
    historical_execution_ok = True
    call = observed.get("call") or {}
    if (
        _CHAIN_HASH_RE.fullmatch(finalized_hash) is None
        or finalized_number is None
        or _CHAIN_HASH_RE.fullmatch(canonical_finalized_hash) is None
        or _CHAIN_HASH_RE.fullmatch(canonical_hash) is None
        or not isinstance(call, dict)
        or not isinstance(observed.get("address"), str)
        or not isinstance(call.get("call_module"), str)
        or not isinstance(call.get("call_function"), str)
    ):
        return NOT_PROVEN
    if canonical_finalized_hash.lower() != finalized_hash.lower():
        return NOT_PROVEN
    if canonical_hash.lower() != block_hash.lower():
        return FAIL
    if finalized_number < block_number:
        return NOT_PROVEN
    if inclusion_policy is not None:
        if not isinstance(commit_reveal_at_inclusion, bool):
            return NOT_PROVEN
        if inclusion_policy.expected_next_epoch_start_block is not None and (
            isinstance(inclusion_next_epoch, bool)
            or not isinstance(inclusion_next_epoch, int)
        ):
            return NOT_PROVEN
    if expected_subnet_owner_hotkey is not None and not inclusion_owner_hotkey:
        return NOT_PROVEN
    inclusion_bindings_ok = True
    if uid_hotkeys is not None:
        try:
            inclusion_uids = [
                int(value)
                for value in (
                    inclusion_metagraph.uids.tolist()
                    if hasattr(inclusion_metagraph.uids, "tolist")
                    else inclusion_metagraph.uids
                )
            ]
            inclusion_hotkeys = [str(value) for value in inclusion_metagraph.hotkeys]
            inclusion_map = dict(zip(inclusion_uids, inclusion_hotkeys))
            inclusion_permits = (
                [bool(value) for value in inclusion_metagraph.validator_permit]
                if netuid == 39
                else []
            )
            complete_bindings = (
                len(inclusion_uids) == len(inclusion_hotkeys)
                and len(inclusion_map) == len(inclusion_uids)
                and _finalized_block(getattr(inclusion_metagraph, "block", None))
                == block_number
                and (netuid != 39 or len(inclusion_permits) == len(inclusion_hotkeys))
            )
        except (AttributeError, TypeError, ValueError):
            return NOT_PROVEN
        if not complete_bindings:
            return NOT_PROVEN
        inclusion_bindings_ok = all(
            inclusion_map.get(uid) == hotkey for uid, hotkey in uid_hotkeys.items()
        )
        if netuid == 39:
            validator_indexes = [
                index
                for index, hotkey in enumerate(inclusion_hotkeys)
                if hotkey == validator_hotkey
            ]
            if len(validator_indexes) != 1:
                return FAIL
            inclusion_bindings_ok = (
                inclusion_bindings_ok
                and inclusion_permits[validator_indexes[0]] is True
            )
    inclusion_policy_ok = inclusion_policy is None or (
        inclusion_policy.valid_from_block
        <= block_number
        < inclusion_policy.valid_until_block
        and inclusion_policy.valid_from_time
        <= inclusion_time
        < inclusion_policy.valid_until_time
        and (
            not inclusion_policy.require_commit_reveal_disabled
            or commit_reveal_at_inclusion is False
        )
        and (
            inclusion_policy.expected_next_epoch_start_block is None
            or inclusion_next_epoch == inclusion_policy.expected_next_epoch_start_block
        )
    )
    inclusion_owner_ok = (
        expected_subnet_owner_hotkey is None
        or inclusion_owner_hotkey == expected_subnet_owner_hotkey
    )
    proven = (
        historical_execution_ok
        and observed.get("address") == validator_hotkey
        and call.get("call_module") == "SubtensorModule"
        and call.get("call_function") == "set_mechanism_weights"
        and _chain_call_arg(call, "netuid") == netuid
        and _chain_call_arg(call, "mecid") == 0
        and _chain_call_arg(call, "version_key") == version_key
        and _chain_call_arg(call, "dests") == wire_uids
        and _chain_call_arg(call, "weights") == wire_weights
        and inclusion_bindings_ok
        and inclusion_policy_ok
        and inclusion_owner_ok
    )
    return PASS if proven else FAIL


def _prove_finalized_receipt(
    subtensor: Any,
    **kwargs: Any,
) -> bool:
    """Compatibility wrapper for callers that only accept a proven boolean."""
    return _classify_finalized_receipt(subtensor, **kwargs) == PASS


def _reverify_reserved_signed_vector(
    args: Any,
    *,
    identity: dict[str, Any],
    preflight: ChainPreflight,
    uid_weights: dict[int, float],
) -> None:
    """Re-prove the reserved signed vector at the irreversible write boundary.

    Every other check here compares the submission against the durable
    reservation, which makes the reservation the sole authority for what gets
    written. Writing that file needs owner access to the canonical runtime
    root, so this is defense in depth rather than a live exploit, but the
    chain write is irreversible and the evidence to do better is already on
    hand: the reservation carries the signed payload, so the boundary can
    re-derive the policy from Cathedral's signature instead of trusting the
    reservation's own summary of it.
    """
    payload = identity.get("signed_vector")
    if not isinstance(payload, dict):
        raise wire.VectorError(
            "SN39 thin reservation carries no signed vector to re-verify"
        )
    if _sha256_document(payload) != identity.get("signed_vector_sha256"):
        raise wire.VectorError(
            "SN39 reserved signed vector does not hash to its reserved digest"
        )
    # Re-load the fence rather than trusting the reservation's policy_version.
    # The fence only advances after a successful write (_write_state_fenced
    # runs after set_weights_on_chain returns), so at this point it still holds
    # the value the tick accepted against and pv > fence must still hold. The
    # launch path already re-runs accept_vector against that same pre-write
    # fence in _revalidate_launch_after_rewarded_set_replay, so this is the
    # established comparison, not a weakened one.
    fence = load_fence(Path(args.state_file))
    try:
        accept_vector(
            payload,
            public_key_hex=args.public_key_hex,
            key_id=args.key_id,
            network=args.network,
            netuid=args.netuid,
            fence_version=fence,
        )
    except Exception as exc:
        raise wire.VectorError(
            "SN39 reserved signed vector fails signature, wire invariant, or "
            f"rollback re-verification: {stable_error(exc)}"
        ) from exc
    if _validated_supply_meta(payload) is None:
        raise wire.VectorError(
            "SN39 reserved signed vector carries no fixed 90/10 supply contract"
        )
    burn_hotkey = (payload.get("burn_snapshot") or {}).get("burn_hotkey")
    if burn_hotkey != SN39_BURN_HOTKEY or identity.get("burn_hotkey") != burn_hotkey:
        raise wire.VectorError(
            "SN39 reserved signed vector does not burn to the pinned hotkey"
        )
    # Same call the tick used to produce what is about to be written, so a
    # reservation cannot redirect the allocation while keeping a valid payload.
    reverified_uid_weights = vector_to_uid_weights(
        payload,
        preflight.hotkey_to_uid,
        require_policy=getattr(args, "require_policy", None),
    )
    if set(reverified_uid_weights) != set(uid_weights) or any(
        not math.isclose(
            reverified_uid_weights[uid],
            float(uid_weights[uid]),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for uid in reverified_uid_weights
    ):
        raise wire.VectorError(
            "SN39 chain call differs from the allocation its reserved signed "
            "vector derives"
        )


def _authorize_sn39_chain_submission(
    args: Any | None,
    *,
    uid_weights: dict[int, float],
    uid_hotkeys: dict[int, str] | None,
    network: str,
    netuid: int,
    wallet_name: str,
    wallet_hotkey: str,
    preflight: ChainPreflight,
    inclusion_policy: InclusionPolicy | None,
) -> None:
    """Authorize the sole repository SN39 writer at its lowest call boundary.

    Callers cannot reach the irreversible extrinsic by importing this module
    and calling ``set_weights_on_chain`` directly.  A write must carry the
    exact immutable runtime profile, resolved Finney identity, and a durable
    reservation made by the thin/FULL state machine.  The first launch also
    carries its synchronous FULL replay; later writes re-prove the independent
    root-signed launch seal plus a separate bounded recurring-write approval.
    """
    if args is None:
        raise wire.VectorError(
            "SN39 chain submission requires an authorized validator runtime"
        )
    if (
        not bool(getattr(args, "broadcast", False))
        or bool(getattr(args, "offline", False))
        or int(getattr(args, "netuid", -1)) != netuid
        or str(getattr(args, "network", "")).strip().lower()
        != str(network).strip().lower()
        or getattr(args, "wallet_name", None) != wallet_name
        or getattr(args, "wallet_hotkey", None) != wallet_hotkey
    ):
        raise wire.VectorError(
            "SN39 chain call differs from its authorized runtime contract"
        )
    _validate_runtime_contract(args)
    _validate_resolved_chain_contract(args, preflight)
    if (
        getattr(args, "_submission_validator_hotkey", None)
        != preflight.validator_hotkey
        or str(getattr(args, "_submission_genesis_hash", "")).lower()
        != str(preflight.genesis_hash).lower()
    ):
        raise wire.VectorError(
            "SN39 chain call is not bound to the prepared signer and genesis"
        )

    state = _read_state(_submission_state_path(args))
    attempt_id = state.get("submission_pending_id")
    identity = state.get("submission_pending_identity")
    lane = state.get("submission_pending_lane")
    active_lane = state.get("submission_active_lane")
    transition_from = state.get("submission_pending_lane_transition_from")
    ordinary_lane = active_lane in {None, lane} and transition_from is None
    authorized_transition = bool(
        active_lane == "thin"
        and lane == "authority"
        and transition_from == "thin"
        and isinstance(identity, dict)
        and _authority_lane_transition_authorized(state, identity)
    )
    if (
        not isinstance(attempt_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
        or lane not in {"thin", "authority"}
        or state.get("submission_pending_phase") != "unsigned_reserved"
        or not isinstance(identity, dict)
        or not (ordinary_lane or authorized_transition)
    ):
        raise wire.VectorError(
            "SN39 chain submission has no exact durable state-machine reservation"
        )
    try:
        reserved_uid_weights = {
            int(uid): float(weight) for uid, weight in identity["uid_weights"]
        }
        reserved_uid_hotkeys = {
            int(uid): str(hotkey) for uid, hotkey in identity["uid_hotkeys"]
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError("SN39 submission reservation is malformed") from exc
    exact_weights = set(reserved_uid_weights) == set(uid_weights) and all(
        math.isclose(
            reserved_uid_weights[uid],
            float(uid_weights[uid]),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for uid in reserved_uid_weights
    )
    exact_hotkeys = uid_hotkeys is not None and reserved_uid_hotkeys == {
        int(uid): str(hotkey) for uid, hotkey in uid_hotkeys.items()
    }
    if (
        identity.get("network") != "finney"
        or identity.get("netuid") != 39
        or identity.get("validator_hotkey") != preflight.validator_hotkey
        or not exact_weights
        or not exact_hotkeys
    ):
        raise wire.VectorError(
            "SN39 chain call differs from its exact durable reservation"
        )
    if not isinstance(inclusion_policy, InclusionPolicy):
        raise wire.VectorError(
            "SN39 chain submission requires an inclusion-time evidence policy"
        )
    _require_inclusion_policy_ready(inclusion_policy, preflight)
    observed_uid_safety = _require_uid_mapping_stability(
        preflight,
        {int(uid): str(hotkey) for uid, hotkey in uid_hotkeys.items()},
        mortal_period_blocks=inclusion_policy.mortal_period_blocks,
    )
    if identity.get("uid_safety") != observed_uid_safety:
        raise wire.VectorError(
            "SN39 UID/hotkey safety differs from its exact durable reservation"
        )
    if identity.get("inclusion_policy") != _inclusion_policy_identity(inclusion_policy):
        raise wire.VectorError(
            "SN39 inclusion policy differs from its durable reservation"
        )
    if (
        preflight.block is None
        or identity.get("next_epoch_start_block") != preflight.next_epoch_start_block
    ):
        raise wire.VectorError(
            "SN39 exact next epoch differs from its durable reservation"
        )
    if lane == "thin":
        # Applies to relay, thin-continuous, and launch alike: all three relay a
        # signed vector, so the payload is the authority for what may be
        # written and re-proving it is strictly more evidence. The authority
        # lane originates weights from its own FULL recomputation and has no
        # signed vector to bind, so it keeps proving its allocation through the
        # evidence identity and the recurring-write authorization below.
        _reverify_reserved_signed_vector(
            args,
            identity=identity,
            preflight=preflight,
            uid_weights=uid_weights,
        )

    launch = bool(getattr(args, "require_full_provenance_for_broadcast", False))
    if not launch and not _continuous_transition_required(args):
        # Relay: this runtime owes SN39 no launch of its own, so there is no
        # recurring-write authorization to re-prove. Everything that makes the
        # write safe still ran above (pinned trust profile, verified signature,
        # exact durable reservation, UID/epoch/inclusion safety). Refuse a call
        # that nonetheless claims launch or recurring authority it cannot
        # present, so the relay path can never be used to smuggle one.
        if (
            lane != "thin"
            or identity.get("continuous_authorization") is not None
            or getattr(args, "_continuous_submission_authorization", None) is not None
            or state.get("submission_pending_launch_attempt") is True
            or state.get("submission_pending_budget_scope")
            not in (None, "thin_bounded")
        ):
            raise wire.VectorError(
                "SN39 relay chain call claims launch or recurring-write "
                "authority it cannot present"
            )
        return
    if not launch:
        authorization = getattr(args, "_continuous_submission_authorization", None)
        if (
            not isinstance(authorization, ContinuousAuthorization)
            or identity.get("continuous_authorization")
            != _continuous_authorization_identity(authorization)
            or state.get("submission_continuous_launch_attempt_id")
            != authorization.launch_attempt_id
            or state.get("submission_continuous_release_sha256")
            != authorization.release_sha256
            or state.get("submission_continuous_reproducer_revision")
            != authorization.reproducer_revision
            or preflight.validator_hotkey != authorization.validator_hotkey
            or preflight.genesis_hash != authorization.genesis_hash
            or lane not in authorization.lanes
            or state.get("submission_pending_budget_scope")
            != authorization.authorization_sha256.removeprefix("sha256:")
            or state.get("submission_pending_budget_limit")
            != authorization.max_attempts
        ):
            raise wire.VectorError(
                "SN39 continuous chain call lacks its pre-reservation "
                "root-signed recurring-write authorization and durable budget"
            )
        try:
            from scaffold import sn39_continuous_authorization as recurring

            reverified = recurring.verify_authorization(
                expected={
                    "submission_journal": str(_submission_state_path(args)),
                    "genesis_hash": preflight.genesis_hash,
                    "validator_hotkey": preflight.validator_hotkey,
                    "launch_attempt_id": authorization.launch_attempt_id,
                    "release_sha256": authorization.release_sha256,
                    "reproducer_revision": authorization.reproducer_revision,
                    "not_before_time": state.get("submission_continuous_enabled_at"),
                },
                lane=lane,
                finalized_block=preflight.block,
            )
            reverified_authorization = ContinuousAuthorization(
                authorization_sha256=reverified.authorization_sha256,
                submission_journal=reverified.submission_journal,
                launch_attempt_id=reverified.launch_attempt_id,
                release_sha256=reverified.release_sha256,
                reproducer_revision=reverified.reproducer_revision,
                validator_hotkey=reverified.validator_hotkey,
                genesis_hash=reverified.genesis_hash,
                lanes=reverified.lanes,
                issued_at=reverified.issued_at,
                valid_from_time=reverified.valid_from_time,
                valid_until_time=reverified.valid_until_time,
                valid_from_block=reverified.valid_from_block,
                valid_until_block=reverified.valid_until_block,
                valid_from_nonce=reverified.valid_from_nonce,
                valid_until_nonce_exclusive=reverified.valid_until_nonce_exclusive,
                max_attempts=reverified.max_attempts,
            )
            if reverified_authorization != authorization:
                raise recurring.AuthorizationError(
                    "recurring-write artifact changed after reservation"
                )
            recurring.assert_still_ready(
                reverified,
                lane=lane,
                finalized_block=preflight.block,
            )
        except Exception as exc:
            raise wire.VectorError(
                "SN39 recurring-write authorization signature, bytes, expiry, "
                "or scope changed before the chain boundary"
            ) from exc
        return
    audit = getattr(args, "_launch_rewarded_set_audit", None)
    full = identity.get("full_provenance")
    rewarded = set(getattr(audit, "recomputed", {}) or {})
    receipts = set(getattr(audit, "receipt_hotkeys", ()) or ())
    replayed = set(getattr(audit, "raw_replayed_hotkeys", ()) or ())
    if (
        lane != "thin"
        or state.get("submission_pending_launch_attempt") is not True
        or state.get("submission_pending_launch_budget_limit") != 1
        or state.get("submission_launch_attempt_ids", []) != []
        or not isinstance(inclusion_policy, InclusionPolicy)
        or not isinstance(full, dict)
        or getattr(audit, "status", None) != PASS
        or getattr(audit, "agrees_with_vector", None) is not True
        or not rewarded
        or rewarded != receipts
        or rewarded != replayed
    ):
        raise wire.VectorError(
            "SN39 launch chain call lacks its one-shot rewarded-set raw-replay gate"
        )
    approval = _require_launch_approval(
        args,
        payload=identity.get("signed_vector") or {},
        audit=audit,
        preflight=preflight,
        uid_weights=uid_weights,
        hotkey_to_uid=preflight.hotkey_to_uid,
    )
    expected_approval_identity = {
        "approval_digest": approval["approval_digest"],
        "reviewed_finalized_block": approval["reviewed_finalized_block"],
        "reviewed_finalized_hash": approval["reviewed_finalized_hash"],
        "approval_valid_until_block": approval["approval_valid_until_block"],
    }
    if identity.get("launch_approval") != expected_approval_identity:
        raise wire.VectorError(
            "SN39 launch reservation differs from its root-controlled approval"
        )
    expected_freshness_boundary = _require_launch_evidence_after_rotations(
        payload=identity.get("signed_vector") or {},
        audit=audit,
        uid_safety=observed_uid_safety,
    )
    full_matches_audit = (
        full.get("source_epoch") == getattr(audit, "source_epoch", None),
        full.get("report_id") == getattr(audit, "report_id", None),
        full.get("manifest") == getattr(audit, "manifest_digest", None),
        full.get("policy_release") == getattr(audit, "policy_release", None),
        full.get("policy_digest") == getattr(audit, "policy_digest", None),
        full.get("mechanism") == getattr(audit, "mechanism", None),
        full.get("scope") == "rewarded_set_full",
        full.get("whole_epoch_assurance") == getattr(audit, "assurance", None),
        full.get("vector_agrees") is True,
        full.get("rewarded_hotkeys") == sorted(rewarded),
        full.get("raw_replayed_hotkeys") == sorted(replayed),
        full.get("verifier_digest") == SN39_VERIFIER_DIGEST,
        full.get("verifier_binary_digest")
        == getattr(audit, "verifier_binary_digest", None),
        isinstance(full.get("verifier_binary_digest"), str),
        full.get("report_signing_key_id")
        == getattr(audit, "report_signing_key_id", None),
        isinstance(full.get("report_signing_key_id"), str),
        full.get("signed_index") == getattr(audit, "signed_index", None),
        isinstance(full.get("signed_index"), dict),
        full.get("source_revision") == SN39_PRODUCER_REVISION,
        full.get("freshness_boundary") == expected_freshness_boundary,
    )
    if not all(full_matches_audit):
        raise wire.VectorError(
            "SN39 launch reservation does not match the synchronous rewarded-set "
            "raw replay"
        )


def _submit_exact_sn39_extrinsic(
    preflight: ChainPreflight,
    *,
    runtime_contract: Any,
    attempt_id: str,
    netuid: int,
    version_key: int,
    wire_uids: list[int],
    wire_weights: list[int],
    mortal_period_blocks: int,
) -> Any:
    """Sign one pinned-era SN39 call and journal its hash before broadcast.

    The generic SDK weight helper chooses its mortal-era reference block inside
    the signing call. That head can be later than the finalized block whose UID
    mappings and evidence window were authorized. SN39 therefore composes the
    same pallet call directly, pins the era to the proven finalized block, and
    fsyncs the exact signed hash and nonce before submitting it. A restart can
    identify this transaction cryptographically and never has to infer it from
    an otherwise identical call.
    """
    from bittensor.core.extrinsics.pallets import SubtensorModule
    from bittensor.core.types import ExtrinsicResponse

    if (
        preflight.block is None
        or preflight.block <= 0
        or netuid != 39
        or mortal_period_blocks != SN39_MORTAL_PERIOD_BLOCKS
        or len(wire_uids) != len(wire_weights)
        or not wire_uids
    ):
        raise wire.VectorError("SN39 exact signing contract is malformed")
    substrate = getattr(preflight.subtensor, "substrate", None)
    if substrate is None:
        raise wire.VectorError("SN39 exact signing has no substrate interface")
    latest_finalized_block, latest_finalized_hash = _finalized_chain_head(
        preflight.subtensor
    )
    if (
        latest_finalized_block != preflight.block
        or latest_finalized_hash != str(preflight.finalized_hash).lower()
    ):
        raise _RetryablePreSignHeadDrift(
            "SN39 finalized head advanced after preflight; refusing before signing"
        )
    nonce = substrate.get_account_next_index(preflight.wallet.hotkey.ss58_address)
    if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce < 0:
        raise wire.VectorError("SN39 validator nonce is malformed")
    # A relay has no recurring-write authorization and therefore no signed
    # nonce window to check. The launch runtime and any runtime that owes SN39
    # a launch still must present one before the account nonce is used.
    if not bool(
        getattr(runtime_contract, "require_full_provenance_for_broadcast", False)
    ) and _continuous_transition_required(runtime_contract):
        authorization = getattr(
            runtime_contract,
            "_continuous_submission_authorization",
            None,
        )
        if not isinstance(authorization, ContinuousAuthorization):
            raise wire.VectorError(
                "SN39 recurring submission has no signed nonce allowance"
            )
        try:
            from scaffold import sn39_continuous_authorization as recurring

            recurring.assert_nonce_ready(
                authorization,
                account_nonce=nonce,
            )
        except Exception as exc:
            raise wire.VectorError(
                "SN39 validator account nonce is outside the signed recurring-write "
                "allowance"
            ) from exc
    unlocked = ExtrinsicResponse.unlock_wallet(
        preflight.wallet,
        True,
        "hotkey",
    )
    if getattr(unlocked, "success", None) is not True:
        raise wire.VectorError("SN39 validator hotkey could not be unlocked")
    call = SubtensorModule(preflight.subtensor).set_mechanism_weights(
        netuid=netuid,
        mecid=0,
        dests=wire_uids,
        weights=wire_weights,
        version_key=version_key,
    )
    era = {
        "period": mortal_period_blocks,
        "current": preflight.block,
    }
    signed = substrate.create_signed_extrinsic(
        call=call,
        keypair=preflight.wallet.hotkey,
        nonce=nonce,
        era=era,
    )
    try:
        extrinsic_hash = f"0x{signed.extrinsic_hash.hex()}".lower()
    except (AttributeError, TypeError, ValueError) as exc:
        raise wire.VectorError("SN39 signed extrinsic has no canonical hash") from exc
    if _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None:
        raise wire.VectorError("SN39 signed extrinsic hash is malformed")
    _record_pending_broadcast_intent(
        runtime_contract,
        attempt_id=attempt_id,
        extrinsic_hash=extrinsic_hash,
        nonce=nonce,
        era_reference_block=preflight.block,
        mortal_period_blocks=mortal_period_blocks,
        version_key=version_key,
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    receipt = substrate.submit_extrinsic(
        signed,
        wait_for_inclusion=True,
        wait_for_finalization=True,
    )
    returned_hash = str(getattr(receipt, "extrinsic_hash", "")).lower()
    if returned_hash != extrinsic_hash:
        if _CHAIN_HASH_RE.fullmatch(returned_hash) is not None:
            raise _PostSignedSubmissionMismatch(
                "SN39 submission receipt differs from its pre-journaled signed hash"
            )
        raise _PendingReceiptNotProven(
            "SN39 submit returned no canonical receipt hash; the exact signed "
            "attempt remains fenced for restart-only proof"
        )
    receipt_success = getattr(receipt, "is_success", None)
    if receipt_success is False:
        raise _PostSignedSubmissionMismatch(
            "SN39 signed transaction finalized without successful execution"
        )
    if receipt_success is not True:
        raise _PendingReceiptNotProven(
            "SN39 submit returned no provable execution result; the exact signed "
            "attempt remains fenced for restart-only proof"
        )
    return receipt


def set_weights_on_chain(
    uid_weights: dict[int, float],
    *,
    network: str,
    netuid: int,
    wallet_name: str,
    wallet_hotkey: str,
    broadcast: bool,
    preflight: ChainPreflight | None = None,
    uid_hotkeys: dict[int, str] | None = None,
    inclusion_policy: InclusionPolicy | None = None,
    runtime_contract: Any | None = None,
    deadline_secs: float = CHAIN_OPERATION_DEADLINE_SECS,
) -> ChainSubmission:
    _validate_emission_vector(uid_weights)
    ordered = sorted(uid_weights.items())
    preview = ",".join(f"{u}={w:.4f}" for u, w in ordered[:12]) + (
        " ..." if len(ordered) > 12 else ""
    )
    uids = [u for u, _ in ordered]
    vals = [w for _, w in ordered]
    primary_call_started = False
    attempt_id: str | None = None
    try:
        wire_uids, wire_values = _wire_weights(uids, vals)
        if not broadcast and preflight is None:
            _lifecycle(
                "WEIGHTS dry-run",
                f"uids={len(ordered)} wire_uids={wire_uids} "
                f"wire_weights={wire_values} vector={preview}",
            )
            return ChainSubmission(success=True)
        if preflight is None:
            preflight = chain_preflight(
                network=network,
                netuid=netuid,
                wallet_name=wallet_name,
                wallet_hotkey=wallet_hotkey,
                deadline_secs=deadline_secs,
            )
        if broadcast and netuid == 39:
            _authorize_sn39_chain_submission(
                runtime_contract,
                uid_weights=uid_weights,
                uid_hotkeys=uid_hotkeys,
                network=network,
                netuid=netuid,
                wallet_name=wallet_name,
                wallet_hotkey=wallet_hotkey,
                preflight=preflight,
                inclusion_policy=inclusion_policy,
            )
        _validate_chain_constraints(uid_weights, preflight)
        mortal_period = (
            inclusion_policy.mortal_period_blocks
            if broadcast and netuid == 39 and inclusion_policy is not None
            else 128
        )
        if not broadcast:
            _lifecycle(
                "WEIGHTS dry-run",
                f"uids={len(ordered)} wire_uids={wire_uids} "
                f"wire_weights={wire_values} vector={preview}",
            )
            return ChainSubmission(success=True)
        with _chain_operation_deadline("weight submission", deadline_secs):
            if netuid == 39:
                state = (
                    _read_state(_submission_state_path(runtime_contract))
                    if runtime_contract is not None
                    else {}
                )
                attempt_id = state.get("submission_pending_id")
                if (
                    runtime_contract is None
                    or not isinstance(attempt_id, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
                ):
                    raise wire.VectorError(
                        "SN39 authorized call has no durable pending attempt"
                    )
                primary_call_started = True
                receipt = _submit_exact_sn39_extrinsic(
                    preflight,
                    runtime_contract=runtime_contract,
                    attempt_id=attempt_id,
                    netuid=netuid,
                    version_key=_weight_version_key(),
                    wire_uids=wire_uids,
                    wire_weights=wire_values,
                    mortal_period_blocks=mortal_period,
                )
                resp = receipt
                ok = True
            else:
                # Non-SN39 callers retain the standard SDK helper. The exact
                # signer/journal path above is the sole SN39 write primitive.
                from bittensor.core.extrinsics.weights import set_weights_extrinsic
                from bittensor.core.settings import version_as_int

                primary_call_started = True
                resp = set_weights_extrinsic(
                    subtensor=preflight.subtensor,
                    wallet=preflight.wallet,
                    netuid=netuid,
                    mechid=0,
                    uids=uids,
                    weights=vals,
                    version_key=version_as_int,
                    mev_protection=False,
                    period=mortal_period,
                    raise_error=True,
                    wait_for_inclusion=True,
                    wait_for_finalization=True,
                    wait_for_revealed_execution=False,
                )
                receipt = getattr(resp, "extrinsic_receipt", None)
                ok = bool(getattr(resp, "success", resp))
            # Some SDK receipt properties are lazy. Materialize every field
            # while the same wall-clock bound is still active.
            response_values: dict[str, Any] = {}
            for name in ("extrinsic_hash", "block_hash", "block_number"):
                value = getattr(receipt, name, None) or getattr(resp, name, None)
                if value is not None:
                    response_values[name] = value
            receipt_block_hash_value = response_values.get("block_hash")
            if receipt_block_hash_value is not None:
                response_values["block_hash"] = str(receipt_block_hash_value).lower()
            block_number = response_values.get("block_number")
            try:
                receipt_block_number = (
                    int(block_number) if block_number is not None else None
                )
            except (TypeError, ValueError):
                receipt_block_number = None
            receipt_block_hash = response_values.get("block_hash")
            if (
                ok
                and receipt_block_number is None
                and isinstance(receipt_block_hash, str)
            ):
                receipt_block_number = _canonical_receipt_block_number(
                    preflight.subtensor,
                    receipt_block_hash,
                )
                response_values["block_number"] = receipt_block_number
            receipt_extrinsic_hash = response_values.get("extrinsic_hash")
            finalized = False
            if ok:
                if (
                    receipt_block_number is None
                    or receipt_block_number <= 0
                    or not isinstance(receipt_block_hash, str)
                    or not isinstance(receipt_extrinsic_hash, str)
                ):
                    raise wire.VectorError(
                        "submission returned success without a canonical receipt "
                        "identity; the durable attempt remains fenced"
                    )
                candidate = ChainSubmission(
                    success=True,
                    extrinsic_hash=receipt_extrinsic_hash,
                    block_hash=receipt_block_hash,
                    block_number=receipt_block_number,
                    finalized=False,
                )
                if netuid == 39:
                    if (
                        runtime_contract is None
                        or not isinstance(attempt_id, str)
                        or re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
                    ):
                        raise wire.VectorError(
                            "SN39 successful call has no durable pending attempt"
                        )
                    _record_pending_submission_receipt(
                        runtime_contract,
                        attempt_id=attempt_id,
                        submission=candidate,
                        version_key=_weight_version_key(),
                        wire_uids=wire_uids,
                        wire_weights=wire_values,
                    )
                proof_status = _classify_finalized_receipt(
                    preflight.subtensor,
                    receipt=receipt,
                    extrinsic_hash=receipt_extrinsic_hash,
                    block_hash=receipt_block_hash,
                    block_number=receipt_block_number,
                    validator_hotkey=preflight.validator_hotkey,
                    netuid=netuid,
                    version_key=_weight_version_key(),
                    wire_uids=wire_uids,
                    wire_weights=wire_values,
                    uid_hotkeys=uid_hotkeys,
                    expected_subnet_owner_hotkey=(
                        SN39_BURN_HOTKEY if netuid == 39 else None
                    ),
                    inclusion_policy=inclusion_policy,
                )
                if netuid == 39 and isinstance(attempt_id, str):
                    _record_pending_proof_status(
                        runtime_contract,
                        attempt_id=attempt_id,
                        status=proof_status,
                    )
                finalized = proof_status == PASS
                if proof_status == NOT_PROVEN:
                    raise _PendingReceiptNotProven(
                        "submission receipt was recorded, but archive/RPC proof is "
                        "temporarily unavailable; restart may only re-prove this "
                        "exact receipt and must not submit again"
                    )
                if proof_status == FAIL:
                    raise _PostSignedSubmissionMismatch(
                        "submission receipt positively mismatches the reserved "
                        "inclusion contract; the attempt remains fenced for "
                        "operator investigation"
                    )
    except Exception as exc:
        unsigned_aborted = False
        if (
            netuid == 39
            and runtime_contract is not None
            and isinstance(attempt_id, str)
        ):
            try:
                unsigned_aborted = _abort_unsigned_common_submission(
                    runtime_contract,
                    attempt_id=attempt_id,
                )
            except (OSError, ValueError):
                # Never claim a safe retry unless the fsynced common journal
                # itself confirmed that no signed intent or receipt existed.
                unsigned_aborted = False
        event = (
            "CHAIN failed"
            if unsigned_aborted or not primary_call_started
            else "CHAIN ambiguous"
        )
        _lifecycle(event, f"uids={len(ordered)} reason={type(exc).__name__}")
        if (
            event == "CHAIN ambiguous"
            and netuid == 39
            and runtime_contract is not None
            and not isinstance(
                exc,
                (_PendingReceiptNotProven, _PostSignedSubmissionMismatch),
            )
        ):
            raise _PendingReceiptNotProven(
                "the exact signed transaction may have finalized, but its submit "
                "response or durable receipt could not be proven; restart may only "
                "reconcile this signed hash and must not submit again"
            ) from exc
        raise
    # newer bittensor returns an ExtrinsicResponse object (truthy even on
    # failure) — judge success by the field, not truthiness.
    block_number = response_values.get("block_number")
    try:
        parsed_block_number = int(block_number) if block_number is not None else None
    except (TypeError, ValueError):
        parsed_block_number = None
    submission = ChainSubmission(
        success=ok,
        extrinsic_hash=(
            str(response_values["extrinsic_hash"])
            if response_values.get("extrinsic_hash")
            else None
        ),
        block_hash=(
            str(response_values["block_hash"])
            if response_values.get("block_hash")
            else None
        ),
        block_number=parsed_block_number,
        finalized=finalized,
    )
    if ok:
        try:
            submission = _require_release_grade_submission(submission)
        except wire.VectorError as exc:
            _lifecycle(
                "CHAIN ambiguous",
                f"uids={len(ordered)} success=True receipt_identity=incomplete",
            )
            if netuid == 39:
                raise _PendingReceiptNotProven(
                    "the successful SN39 response has no complete canonical receipt "
                    "identity; the exact signed attempt remains fenced"
                ) from exc
            raise
    response_details = (
        [
            f"extrinsic_hash={submission.extrinsic_hash}",
            f"block_hash={submission.block_hash}",
            f"block_number={submission.block_number}",
            "finalized=true",
        ]
        if ok
        else []
    )
    _lifecycle(
        "CHAIN submitted" if ok else "CHAIN failed",
        " ".join([f"uids={len(ordered)}", f"success={ok}", *response_details]),
    )
    return submission


_CHAIN_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _require_release_grade_submission(submission: Any) -> ChainSubmission:
    """Require an exact finalized transaction identity after a successful call.

    If the SDK says success but omits identity, the call is operationally
    ambiguous: keep the pre-submit pending fence and require reconciliation
    rather than recording an unverifiable launch transaction.
    """
    if not bool(submission):
        raise wire.VectorError("chain submission did not succeed")
    extrinsic_hash = getattr(submission, "extrinsic_hash", None)
    block_hash = getattr(submission, "block_hash", None)
    block_number = getattr(submission, "block_number", None)
    finalized = getattr(submission, "finalized", None)
    if (
        not isinstance(extrinsic_hash, str)
        or _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None
        or not isinstance(block_hash, str)
        or _CHAIN_HASH_RE.fullmatch(block_hash) is None
        or isinstance(block_number, bool)
        or not isinstance(block_number, int)
        or block_number <= 0
        or finalized is not True
    ):
        raise wire.VectorError(
            "chain reported success without a release-grade extrinsic hash, "
            "block hash, positive block number, and canonical finalized-head "
            "proof; submission is ambiguous "
            "and must be reconciled before another write"
        )
    return ChainSubmission(
        success=True,
        extrinsic_hash=extrinsic_hash.lower(),
        block_hash=block_hash.lower(),
        block_number=block_number,
        finalized=True,
    )


def _finalized_block(raw) -> int | None:
    """Strictly coerce a metagraph-reported block number.

    Only a positive integral number is a usable finalized block: booleans,
    fractional floats, junk strings, and non-positive values are all None.
    Thin mode tolerates None (it never anchors a validity window); authority
    REFUSES on None instead of silently skipping report block-validity
    checks."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float) and not raw.is_integer():
        return None
    try:
        block = int(raw)
    except (TypeError, ValueError):
        return None
    return block if block > 0 else None


def _bt_subtensor(bt):
    """bittensor renamed `subtensor` -> `Subtensor` across major versions."""
    return getattr(bt, "subtensor", None) or bt.Subtensor


def _bt_wallet(bt):
    return getattr(bt, "wallet", None) or bt.Wallet


def _block_hash_lookup(network: str):
    """A callable resolving a historical block number to its hash via the
    validator's own subtensor connection (independent of Cathedral)."""

    def lookup(block: int):
        try:
            with _isolated_argv():
                import bittensor as bt

                return _bt_subtensor(bt)(
                    network=connection_target(network)
                ).get_block_hash(block)
        except Exception:  # noqa: BLE001 - unavailable lookup is None, not a pass
            return None

    return lookup


def _validated_historical_hotkeys(raw_hotkeys, *, metagraph_block, requested_block):
    """Validate the RAW historical hotkey sequence BEFORE any set
    construction: sequence type, per-hotkey validity, exact count with
    uniqueness (a set would silently swallow duplicates), and the returned
    metagraph's block equal to the REQUESTED block. Any violation returns
    None — malformed or misaligned history is unavailable history."""
    if isinstance(metagraph_block, bool):
        return None
    try:
        if int(metagraph_block) != int(requested_block):
            return None
    except (TypeError, ValueError):
        return None
    if not isinstance(raw_hotkeys, (list, tuple)) or not raw_hotkeys:
        return None
    for hotkey in raw_hotkeys:
        if not isinstance(hotkey, str) or not 1 <= len(hotkey.encode("utf-8")) <= 512:
            return None
    if len(set(raw_hotkeys)) != len(raw_hotkeys):
        return None
    return frozenset(raw_hotkeys)


def _historical_metagraph_lookup(network: str, netuid: int):
    """A callable resolving the SN39 metagraph AT a historical block to its
    exact hotkey set via the validator's own subtensor connection
    (Subtensor.metagraph(netuid, block=block)). Returns None when the
    history is unavailable, malformed, or not actually at the requested
    block — the audit treats that as NOT_PROVEN, never a pass."""

    def lookup(block: int):
        try:
            with _isolated_argv():
                import bittensor as bt

                mg = _bt_subtensor(bt)(network=connection_target(network)).metagraph(
                    netuid, block=int(block)
                )
            return _validated_historical_hotkeys(
                list(getattr(mg, "hotkeys", None) or ()),
                metagraph_block=getattr(mg, "block", None),
                requested_block=block,
            )
        except Exception:  # noqa: BLE001 - unavailable history is None, not a pass
            return None

    return lookup


def _metagraph_snapshot(
    *, network: str, netuid: int
) -> tuple[dict[str, int], int | None]:
    """Read the UID map at the node's exact canonical finalized head."""
    with _isolated_argv():
        import bittensor as bt

        subtensor = _bt_subtensor(bt)(network=connection_target(network))
        finalized_block, _finalized_hash = _finalized_chain_head(subtensor)
        mg = subtensor.metagraph(netuid, block=finalized_block)
        commit_reveal_enabled = _strict_commit_reveal_state(
            subtensor.commit_reveal_enabled(netuid=netuid, block=finalized_block)
        )
    if commit_reveal_enabled:
        raise wire.VectorError(
            "SN39 release health requires commit-reveal disabled at the "
            "finalized snapshot"
        )
    if _finalized_block(getattr(mg, "block", None)) != finalized_block:
        raise wire.VectorError("metagraph snapshot did not resolve at finalized head")
    mapping = {hk: int(uid) for uid, hk in zip(mg.uids.tolist(), mg.hotkeys)}
    return mapping, finalized_block


def metagraph_hotkey_to_uid(*, network: str, netuid: int) -> dict[str, int]:
    with _isolated_argv():
        import bittensor as bt  # import under blanked argv — bittensor parses

        mg = _bt_subtensor(bt)(network=connection_target(network)).metagraph(netuid)
    return {hk: int(uid) for uid, hk in zip(mg.uids.tolist(), mg.hotkeys)}


# -- main loop --------------------------------------------------------------------


def tick(args) -> bool:
    provenance_mode_early = getattr(args, "provenance", "shadow") or "shadow"
    _lifecycle("FEED fetch", f"source={_feed_label(args.publisher_url)}")
    if provenance_mode_early == "authority":
        # Authority's basis is the evidence chain + pins + chain snapshot.
        # Cathedral's vector is best-effort comparison input only; a down,
        # stale, or malformed endpoint must not stop independent audit.
        try:
            payload = fetch_vector(args.publisher_url)
        except Exception as exc:  # noqa: BLE001
            _lifecycle("FEED unavailable", f"reason={type(exc).__name__}")
            payload = None
        _prepare_tick_preflight(args)
        return _authority_tick(args, payload)
    _prepare_tick_preflight(args)
    with _thin_tick_lock(args):
        args._continuous_submission_authorization = None
        if (
            bool(getattr(args, "broadcast", False))
            and _continuous_transition_required(args)
            and not bool(getattr(args, "require_full_provenance_for_broadcast", False))
        ):
            args._continuous_submission_authorization = (
                _require_continuous_launch_transition(args)
            )
            # The public seal can require archive/network work. Refresh every
            # mutable chain fact after it, while still holding the shared lock.
            _prepare_tick_preflight(args)
        return _thin_tick_locked(args)


def _thin_tick_locked(args) -> bool:
    """Default thin/shadow tick under one cross-process submission lock."""
    payload = fetch_vector(args.publisher_url)
    _lifecycle(
        "FEED fetched",
        f"id={str(payload.get('vector_id', ''))[:8]} "
        f"policy_version={payload.get('policy_version')}",
    )
    state_path = Path(args.state_file)
    prior_state = _read_state(state_path)
    recovered_version = prior_state.get("thin_recovered_policy_version")
    if (
        isinstance(recovered_version, int)
        and not isinstance(recovered_version, bool)
        and payload.get("policy_version") == recovered_version
    ):
        observed_digest = _sha256_document(payload)
        if payload.get("vector_id") == prior_state.get(
            "thin_recovered_vector_id"
        ) and observed_digest == prior_state.get("thin_recovered_signed_vector_sha256"):
            wire.verify_signature(
                payload,
                public_key_hex=args.public_key_hex,
                expected_key_id=args.key_id,
            )
            _lifecycle(
                "VECTOR idle",
                f"id={str(payload.get('vector_id', ''))[:8]} "
                f"policy_version={recovered_version} already_finalized=true",
            )
            _get_events(args).event(
                "RECOVERED_VECTOR_IDLE",
                stage="result",
                status=INFO,
                artifact=str(payload.get("vector_id", ""))[:36] or None,
                detail=(
                    "exact recovered policy/vector is already finalized; "
                    "no second chain write was attempted"
                ),
            )
            return True
    fence = load_fence(Path(args.state_file))
    try:
        accept_vector(
            payload,
            public_key_hex=args.public_key_hex,
            key_id=args.key_id,
            network=args.network,
            netuid=args.netuid,
            fence_version=fence,
        )
    except Exception as e:
        _lifecycle("VERIFY failed", f"reason={type(e).__name__}")
        _lifecycle("VECTOR rejected", f"stage=accept reason={type(e).__name__}")
        raise
    _lifecycle("SIGNATURE valid", f"key_id={payload.get('key_id')}")
    _lifecycle(
        "FRESHNESS valid",
        f"network={payload.get('network')} netuid={payload.get('netuid')} "
        f"generated_at={payload.get('generated_at')} expires_at={payload.get('expires_at')}",
    )
    _lifecycle(
        "ROLLBACK valid",
        f"policy_version={payload.get('policy_version')} prior_fence={fence}",
    )
    _lifecycle(
        "VECTOR accepted",
        f"id={str(payload.get('vector_id', ''))[:8]} "
        f"policy_version={payload['policy_version']} "
        f"miners={len(payload['weights'])} "
        f"burn={payload['burn_snapshot']['forced_burn_percentage']}%",
    )
    _get_events(args).event(
        "VECTOR_ACCEPTED",
        stage="verify",
        status=PASS,
        artifact=str(payload.get("vector_id", ""))[:36] or None,
        detail=(
            f"policy_version={payload['policy_version']} "
            f"miners={len(payload['weights'])} "
            f"burn={payload['burn_snapshot']['forced_burn_percentage']}% "
            f"signature+freshness+rollback ok"
        ),
    )
    # offline is authoritative: no chain read AND no broadcast, even if
    # --broadcast was also passed (the two are contradictory; offline wins).
    preflight = None
    tick_block = None
    if args.offline:
        hk2uid = {w["miner_hotkey"]: i for i, w in enumerate(payload["weights"])}
        burn_hotkey = (payload.get("burn_snapshot") or {}).get("burn_hotkey")
        if burn_hotkey is not None and burn_hotkey not in hk2uid:
            hk2uid[burn_hotkey] = len(hk2uid)
        _lifecycle("MAP offline", "synthetic uid map, no chain access")
        broadcast = False
    else:
        broadcast = args.broadcast
        preflight = getattr(args, "_tick_preflight", None)
        if preflight is None:
            preflight = chain_preflight(
                network=args.network,
                netuid=args.netuid,
                wallet_name=args.wallet_name,
                wallet_hotkey=args.wallet_hotkey,
            )
            _bind_submission_identity(args, preflight)
        hk2uid = preflight.hotkey_to_uid
        tick_block = preflight.block
    try:
        uid_weights = vector_to_uid_weights(
            payload, hk2uid, require_policy=getattr(args, "require_policy", None)
        )
    except Exception as e:
        _lifecycle("VECTOR rejected", f"stage=map reason={type(e).__name__}")
        _get_events(args).event(
            "VECTOR_REJECTED",
            stage="map",
            status=FAIL,
            detail=f"reason={type(e).__name__}",
            remediation="The signed vector failed UID mapping; nothing was submitted.",
        )
        raise

    # Concurrent full-provenance stage (shadow audits; authority replaces the
    # submitted vector with the independent recomputation).
    provenance_mode = getattr(args, "provenance", "shadow") or "shadow"
    submission_authority = "thin"
    launch_rewarded_set_gate = bool(
        getattr(args, "require_full_provenance_for_broadcast", False)
    )
    if launch_rewarded_set_gate:
        if args.offline or not broadcast or tick_block is None:
            raise wire.VectorError(
                "launch rewarded-set gate requires an online broadcast with a "
                "finalized block"
            )
        launch_audit = _run_launch_rewarded_set_gate(
            args,
            payload=payload,
            uid_weights=uid_weights,
            hotkey_to_uid=hk2uid,
            current_block=tick_block,
            state_file=Path(args.state_file),
            persist=False,
        )
        preflight, hk2uid, uid_weights = _revalidate_launch_after_rewarded_set_replay(
            args,
            payload=payload,
            audit=launch_audit,
            fence_version=fence,
        )
        _require_launch_approval(
            args,
            payload=payload,
            audit=launch_audit,
            preflight=preflight,
            uid_weights=uid_weights,
            hotkey_to_uid=hk2uid,
        )
        args._tick_preflight = preflight
        tick_block = preflight.block
    elif provenance_mode == "shadow":
        # ONE metagraph snapshot supplies the UID map and the current
        # block; candidate membership is proven against the HISTORICAL
        # metagraph at the manifest's anchored block, via the validator's
        # own chain connection.
        _run_provenance_stage(
            args,
            payload,
            Path(args.state_file),
            current_block=None if args.offline else tick_block,
            historical_hotkeys_lookup=(
                None
                if args.offline
                else _historical_metagraph_lookup(args.network, args.netuid)
            ),
            block_hash_lookup=(
                None if args.offline else _block_hash_lookup(args.network)
            ),
        )

    ordered = sorted(uid_weights.items())
    preview = ",".join(f"{uid}:{weight:.6f}" for uid, weight in ordered[:12])
    if len(ordered) > 12:
        preview += ",..."
    burn_hotkey = (payload.get("burn_snapshot") or {}).get("burn_hotkey")
    burn_uid = (
        hk2uid.get(burn_hotkey)
        if burn_hotkey is not None
        else (payload.get("burn_snapshot") or {}).get("burn_uid")
    )
    if preflight is not None:
        _require_no_validator_compute_reward(
            uid_weights,
            preflight=preflight,
            burn_uid=int(burn_uid) if burn_uid is not None else None,
        )
    burn_share = uid_weights.get(int(burn_uid), 0.0) if burn_uid is not None else 0.0
    _lifecycle(
        "MAP complete",
        f"uids={len(uid_weights)} burn_uid={burn_uid} burn_share={burn_share:.6f} "
        f"vector={preview}",
    )
    signed_vector_sha256 = _sha256_document(payload)
    if args.offline:
        wire_uids = wire_values = None
    else:
        wire_uids, wire_values = _wire_weights(
            [uid for uid, _weight in ordered],
            [weight for _uid, weight in ordered],
        )
    state_file = Path(args.state_file)
    thin_attempt_id: str | None = None
    inclusion_policy: InclusionPolicy | None = None
    if broadcast:
        inclusion_policy = getattr(args, "_launch_inclusion_policy", None)
        if inclusion_policy is None:
            inclusion_policy = _vector_inclusion_policy(payload, preflight)
        uid_safety = _require_uid_mapping_stability(
            preflight,
            {uid: hotkey for hotkey, uid in hk2uid.items() if uid in uid_weights},
            mortal_period_blocks=inclusion_policy.mortal_period_blocks,
        )
        # Persist an ambiguity fence BEFORE the irreversible call. Mapping block
        # is retained in the exact submission record but excluded from the
        # dedup identity: advancing a block with the same signed vector and
        # resolved allocation is not new work.
        identity = {
            "network": args.network,
            "netuid": args.netuid,
            "mapping_block": tick_block,
            "validator_hotkey": preflight.validator_hotkey,
            "validator_uid": preflight.validator_uid,
            "vector_id": payload["vector_id"],
            "policy_version": int(payload["policy_version"]),
            "signed_vector_sha256": signed_vector_sha256,
            # The payload itself, not just its digest. The chain-write boundary
            # re-derives the whole policy from these bytes, so a reservation can
            # never be the sole authority for what gets written.
            "signed_vector": payload,
            "burn_hotkey": burn_hotkey,
            "uid_weights": [[uid, weight] for uid, weight in ordered],
            "uid_hotkeys": [
                [uid, hotkey]
                for hotkey, uid in sorted(hk2uid.items(), key=lambda item: item[1])
                if uid in uid_weights
            ],
            "next_epoch_start_block": (preflight.next_epoch_start_block),
            "inclusion_policy": _inclusion_policy_identity(inclusion_policy),
            "uid_safety": uid_safety,
        }
        continuous_authorization = getattr(
            args, "_continuous_submission_authorization", None
        )
        if continuous_authorization is not None:
            if not isinstance(continuous_authorization, ContinuousAuthorization):
                raise wire.VectorError(
                    "continuous authorization has an invalid runtime type"
                )
            identity["continuous_authorization"] = _continuous_authorization_identity(
                continuous_authorization
            )
        launch_audit = getattr(args, "_launch_rewarded_set_audit", None)
        if launch_audit is not None:
            freshness_boundary = _require_launch_evidence_after_rotations(
                payload=payload,
                audit=launch_audit,
                uid_safety=uid_safety,
            )
            identity["full_provenance"] = {
                "source_epoch": launch_audit.source_epoch,
                "report_id": launch_audit.report_id,
                "manifest": launch_audit.manifest_digest,
                "policy_release": launch_audit.policy_release,
                "policy_digest": launch_audit.policy_digest,
                "mechanism": launch_audit.mechanism,
                "scope": "rewarded_set_full",
                "whole_epoch_assurance": launch_audit.assurance,
                "vector_agrees": launch_audit.agrees_with_vector,
                "rewarded_hotkeys": sorted(launch_audit.recomputed),
                "raw_replayed_hotkeys": sorted(launch_audit.raw_replayed_hotkeys),
                "verifier_digest": getattr(args, "provenance_verifier_digest", None),
                "verifier_binary_digest": launch_audit.verifier_binary_digest,
                "report_signing_key_id": launch_audit.report_signing_key_id,
                "signed_index": launch_audit.signed_index,
                "source_revision": getattr(args, "provenance_source_revision", None),
                "freshness_boundary": freshness_boundary,
            }
            launch_approval = getattr(args, "_launch_approval", None)
            if not isinstance(launch_approval, dict):
                raise wire.VectorError(
                    "launch reservation has no consumed operator approval"
                )
            identity["launch_approval"] = {
                "approval_digest": launch_approval.get("approval_digest"),
                "reviewed_finalized_block": launch_approval.get(
                    "reviewed_finalized_block"
                ),
                "reviewed_finalized_hash": launch_approval.get(
                    "reviewed_finalized_hash"
                ),
                "approval_valid_until_block": launch_approval.get(
                    "approval_valid_until_block"
                ),
            }
        dedup_identity = {
            key: value
            for key, value in identity.items()
            if key not in {"mapping_block", "uid_safety"}
        }
        thin_attempt_id = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    dedup_identity,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        try:
            _reserve_common_submission(
                args,
                lane="thin",
                attempt_id=thin_attempt_id,
                identity=identity,
            )
        except (ValueError, OSError) as exc:
            raise wire.VectorError(
                "thin submission attempt fence refused before chain write: "
                f"{stable_error(exc)}"
            ) from exc
    submission = set_weights_on_chain(
        uid_weights,
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        broadcast=broadcast,
        preflight=preflight,
        uid_hotkeys=(
            {uid: hotkey for hotkey, uid in hk2uid.items() if uid in uid_weights}
            if not args.offline
            else None
        ),
        inclusion_policy=inclusion_policy,
        runtime_contract=args,
    )
    if broadcast:
        submission = _require_release_grade_submission(submission)
    ok = bool(submission)
    # Finalize the attempt and rollback fence in ONE atomic fsync before any
    # fallible telemetry. The common journal is the release source of truth, so
    # finalize it first. A crash before the lane-local telemetry write leaves a
    # recoverable finalized launch instead of an unrecoverable common pending
    # fence.
    if ok and broadcast:
        _finalize_common_submission(
            args,
            attempt_id=thin_attempt_id,
            submission=submission,
        )
        _write_state_fenced(
            state_file,
            {
                "highest_attempted_policy_version": int(payload["policy_version"]),
                "thin_submission_attempt_id": thin_attempt_id,
                "thin_submission_attempt_status": "finalized",
                "thin_submission_finalized_id": thin_attempt_id,
                "thin_submission_finalized_at": _ms_iso_now(),
                "thin_submission_extrinsic_hash": getattr(
                    submission, "extrinsic_hash", None
                ),
                "thin_submission_block_hash": getattr(submission, "block_hash", None),
                "thin_submission_block_number": getattr(
                    submission, "block_number", None
                ),
                "thin_submission_identity": identity,
                "thin_submission_dedup_identity": dedup_identity,
                "last_accepted_policy_version": int(payload["policy_version"]),
                "last_vector_id": payload["vector_id"],
                "accepted_at": _ms_iso_now(),
                "thin_recovered_policy_version": None,
                "thin_recovered_vector_id": None,
                "thin_recovered_signed_vector_sha256": None,
            },
        )
    _get_events(args).event(
        "WEIGHTS_SUBMITTED" if (ok and broadcast) else "WEIGHTS_DRY_RUN",
        stage="submit",
        status=PASS if ok else FAIL,
        detail=(
            f"authority={submission_authority} uids={len(ordered)} "
            f"burn_uid={burn_uid} burn_share={burn_share:.6f} vector={preview}"
        ),
        artifact=str(payload.get("vector_id", ""))[:36] or None,
        authority=submission_authority,
        uid_count=len(ordered),
        burn_uid=burn_uid,
        burn_share=burn_share,
        uid_weights={str(uid): weight for uid, weight in ordered},
        wire_uids=wire_uids,
        wire_weights=wire_values,
        version_key=_weight_version_key() if not args.offline else None,
        vector_id=payload.get("vector_id"),
        policy_version=payload.get("policy_version"),
        signed_vector_sha256=signed_vector_sha256,
        mapping_block=tick_block,
        validator_uid=preflight.validator_uid if preflight is not None else None,
        validator_hotkey=(
            preflight.validator_hotkey if preflight is not None else None
        ),
        extrinsic_hash=getattr(submission, "extrinsic_hash", None),
        block_hash=getattr(submission, "block_hash", None),
        block_number=getattr(submission, "block_number", None),
    )
    # Dry-run/offline passes never consume a version (with the pv<=fence rule
    # that would otherwise block the subsequent live broadcast).
    return ok


_VALIDATOR_RUNTIME_ROOT = Path("/var/lib/cathedral-validator")


def _bind_submission_identity(args: Any, preflight: ChainPreflight) -> None:
    """Bind runtime fencing to the canonical signer and chain genesis."""
    validator_hotkey = str(preflight.validator_hotkey)
    genesis_hash = str(preflight.genesis_hash).lower()
    if not validator_hotkey or _CHAIN_HASH_RE.fullmatch(genesis_hash) is None:
        raise wire.VectorError(
            "chain preflight did not establish a canonical signer/genesis identity"
        )
    existing_hotkey = getattr(args, "_submission_validator_hotkey", None)
    existing_genesis = getattr(args, "_submission_genesis_hash", None)
    if existing_hotkey is not None and existing_hotkey != validator_hotkey:
        raise wire.VectorError("validator signer changed within one tick")
    if existing_genesis is not None and existing_genesis != genesis_hash:
        raise wire.VectorError("chain genesis changed within one tick")
    args._submission_validator_hotkey = validator_hotkey
    args._submission_genesis_hash = genesis_hash


def _validate_resolved_chain_contract(
    args: Any,
    preflight: ChainPreflight,
    *,
    require_sn39_identity: bool = False,
) -> None:
    """Enforce the SN39 contract against the connected chain, not its label."""
    if (
        (not bool(getattr(args, "broadcast", False)) and not require_sn39_identity)
        or bool(getattr(args, "offline", False))
        or int(getattr(args, "netuid", -1)) != 39
    ):
        return
    genesis_hash = str(preflight.genesis_hash).lower()
    if genesis_hash != FINNEY_GENESIS_HASH:
        raise wire.VectorError(
            "SN39 broadcast is supported only on the pinned Finney genesis"
        )
    if str(getattr(args, "network", "")).strip().lower() != "finney":
        raise wire.VectorError(
            "Finney SN39 broadcast requires the `finney` signed-vector audience "
            "even when a self-hosted RPC endpoint is used"
        )
    if getattr(args, "require_policy", None) != "validated_supply_v1":
        raise wire.VectorError(
            "Finney SN39 broadcast requires the validated_supply_v1 policy"
        )
    if preflight.min_allowed_weights != 1 or not math.isclose(
        preflight.max_weight_limit,
        1.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise wire.VectorError(
            "Finney SN39 broadcast requires min_allowed_weights=1 and "
            "max_weight_limit=1.0 so revocation can fail safe to burn"
        )
    if preflight.commit_reveal_enabled:
        raise wire.VectorError("Finney SN39 broadcast requires commit-reveal disabled")
    expected_burn_hotkey = getattr(args, "provenance_burn_hotkey", None)
    if (
        not isinstance(expected_burn_hotkey, str)
        or not expected_burn_hotkey
        or preflight.subnet_owner_hotkey != expected_burn_hotkey
        or preflight.hotkey_to_uid.get(expected_burn_hotkey) is None
    ):
        raise wire.VectorError(
            "Finney SN39 broadcast requires the pinned burn hotkey to remain "
            "the live subnet owner"
        )
    if _submission_runtime_root(args) != _VALIDATOR_RUNTIME_ROOT:
        raise wire.VectorError(
            "Finney SN39 broadcast requires the canonical owner-only "
            f"runtime root {_VALIDATOR_RUNTIME_ROOT}"
        )


def _prepare_tick_preflight(args: Any) -> None:
    """Resolve canonical submission identity before taking its shared lock."""
    if bool(getattr(args, "offline", False)):
        args._tick_preflight = None
        return
    preflight = chain_preflight(
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
    )
    _validate_resolved_chain_contract(args, preflight)
    _bind_submission_identity(args, preflight)
    args._tick_preflight = preflight


def _submission_identity_digest(args: Any) -> str:
    """Hash the canonical chain/signer identity shared by every mode."""
    try:
        netuid = int(args.netuid)
    except (AttributeError, TypeError, ValueError) as exc:
        raise wire.VectorError("submission runtime identity is invalid") from exc
    validator_hotkey = getattr(args, "_submission_validator_hotkey", None)
    genesis_hash = getattr(args, "_submission_genesis_hash", None)
    if validator_hotkey is None or genesis_hash is None:
        if not bool(getattr(args, "offline", False)):
            raise wire.VectorError(
                "canonical signer/genesis must be resolved before submission locking"
            )
        # Offline reproduction never reaches a chain call. A separate namespace
        # keeps its test lock from colliding with any live signer identity.
        identity = {
            "offline": True,
            "network": str(args.network).strip().lower(),
            "netuid": netuid,
            "wallet_name": str(getattr(args, "wallet_name", "")).strip(),
            "wallet_hotkey": str(getattr(args, "wallet_hotkey", "")).strip(),
        }
    else:
        identity = {
            "genesis_hash": str(genesis_hash).lower(),
            "netuid": netuid,
            "validator_hotkey": str(validator_hotkey),
        }
    if netuid < 0 or any(value in ("", None) for value in identity.values()):
        raise wire.VectorError("submission runtime identity is incomplete")
    if not identity.get("offline") and (
        _CHAIN_HASH_RE.fullmatch(identity["genesis_hash"]) is None
    ):
        raise wire.VectorError("submission chain genesis is malformed")
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _submission_runtime_root(args: Any) -> Path:
    configured = getattr(args, "runtime_root", None)
    root = Path(configured) if configured else _VALIDATOR_RUNTIME_ROOT
    if not root.is_absolute():
        raise wire.VectorError("submission runtime root must be an absolute path")
    return root


def _submission_lock_path(args: Any) -> Path:
    """One HOME-independent lock for this chain/wallet identity."""
    return _submission_runtime_root(args) / (
        f"submission-{_submission_identity_digest(args)}.lock"
    )


def _submission_state_path(args: Any) -> Path:
    """One cross-mode ambiguity journal, independent of lane state files."""
    return _submission_runtime_root(args) / (
        f"journal-{_submission_identity_digest(args)}.json"
    )


def _reserve_common_submission(
    args: Any,
    *,
    lane: str,
    attempt_id: str,
    identity: dict[str, Any],
) -> None:
    lane_fence: dict[str, int]
    if lane == "thin":
        policy_version = identity.get("policy_version")
        if isinstance(policy_version, bool) or not isinstance(policy_version, int):
            raise ValueError("thin submission identity has no policy version")
        lane_fence = {"submission_highest_policy_version": policy_version}
    elif lane == "authority":
        source_epoch = identity.get("source_epoch")
        if isinstance(source_epoch, bool) or not isinstance(source_epoch, int):
            raise ValueError("authority submission identity has no source epoch")
        lane_fence = {"submission_highest_source_epoch": source_epoch}
    else:
        raise ValueError("submission lane must be thin or authority")
    max_submissions = int(getattr(args, "max_submissions", 0) or 0)
    if max_submissions < 0:
        raise ValueError("max submissions must be nonnegative")
    launch_attempt = bool(getattr(args, "require_full_provenance_for_broadcast", False))
    authorization = getattr(args, "_continuous_submission_authorization", None)
    # `_continuous_transition_required` is the single source of truth. Keeping
    # a separate config conjunct here let an authority-lane operator who set
    # the flag false reserve without the signed authorization the tick had
    # already demanded, so the reservation and the tick disagreed.
    recurring_required = bool(
        bool(getattr(args, "broadcast", False))
        and not bool(getattr(args, "offline", False))
        and int(getattr(args, "netuid", -1)) == 39
        and not launch_attempt
        and _continuous_transition_required(args)
    )
    if recurring_required and not isinstance(authorization, ContinuousAuthorization):
        raise ValueError(
            "SN39 recurring reservation lacks a separate signed authorization"
        )
    if isinstance(authorization, ContinuousAuthorization):
        authorization_identity = _continuous_authorization_identity(authorization)
        if (
            launch_attempt
            or identity.get("continuous_authorization") != authorization_identity
            or lane not in authorization.lanes
            or re.fullmatch(r"sha256:[0-9a-f]{64}", authorization.authorization_sha256)
            is None
            or authorization.max_attempts <= 0
            or authorization.max_attempts > 96
            or authorization.valid_from_nonce < 0
            or authorization.valid_until_nonce_exclusive
            != authorization.valid_from_nonce + authorization.max_attempts
            or (max_submissions > 0 and authorization.max_attempts > max_submissions)
        ):
            raise ValueError(
                "recurring authorization differs from the exact reservation "
                "or configured attempt ceiling"
            )
        # The signed artifact's digest gives each explicit renewal an
        # independent durable budget. The budget is consumed only when the
        # exact signed transaction is fsynced, never by a dry or failed tick.
        budget_updates = {
            "_submission_budget_scope": authorization.authorization_sha256.removeprefix(
                "sha256:"
            ),
            "_submission_budget_limit": authorization.max_attempts,
        }
    else:
        budget_updates = (
            {
                "_submission_budget_scope": (
                    "launch_full_gate" if launch_attempt else f"{lane}_bounded"
                ),
                "_submission_budget_limit": max_submissions,
            }
            if max_submissions
            else {}
        )
    _write_state_fenced(
        _submission_state_path(args),
        {
            "submission_genesis_hash": getattr(
                args, "_submission_genesis_hash", "offline"
            ),
            "provenance_netuid": int(args.netuid),
            "submission_validator_hotkey": getattr(
                args, "_submission_validator_hotkey", "offline"
            ),
            "_provisional_submission": True,
            "_allow_authority_lane_transition": bool(
                lane == "authority"
                and isinstance(authorization, ContinuousAuthorization)
                and "authority" in authorization.lanes
            ),
            "_launch_attempt": launch_attempt,
            **({"_launch_budget_limit": max_submissions} if launch_attempt else {}),
            **budget_updates,
            "submission_pending_id": attempt_id,
            "submission_pending_lane": lane,
            "submission_pending_identity": identity,
            "submission_pending_at": _ms_iso_now(),
            **lane_fence,
        },
    )


def _record_pending_submission_receipt(
    args: Any,
    *,
    attempt_id: str,
    submission: ChainSubmission,
    version_key: int,
    wire_uids: list[int],
    wire_weights: list[int],
) -> None:
    """Fsync the canonical receipt identity before any archive proof.

    The receipt is sufficient for a restart to re-prove the exact historical
    call. It never authorizes another submission.
    """
    if (
        not submission.success
        or not submission.extrinsic_hash
        or _CHAIN_HASH_RE.fullmatch(submission.extrinsic_hash) is None
        or not submission.block_hash
        or _CHAIN_HASH_RE.fullmatch(submission.block_hash) is None
        or not isinstance(submission.block_number, int)
        or submission.block_number <= 0
        or isinstance(version_key, bool)
        or not isinstance(version_key, int)
        or version_key < 0
        or len(wire_uids) != len(wire_weights)
    ):
        raise wire.VectorError(
            "successful chain call has no canonical restart-safe receipt identity"
        )
    _write_state_fenced(
        _submission_state_path(args),
        {
            "_record_receipt_for_submission_id": attempt_id,
            "submission_pending_receipt_candidate": {
                "extrinsic_hash": submission.extrinsic_hash,
                "block_hash": submission.block_hash,
                "block_number": submission.block_number,
                "version_key": version_key,
                "wire_uids": wire_uids,
                "wire_weights": wire_weights,
            },
            "submission_pending_proof_status": "pending",
            "submission_pending_receipt_recorded_at": _ms_iso_now(),
        },
    )


def _commit_pending_signed_attempt(
    args: Any,
    *,
    attempt_id: str,
    intent: dict[str, Any],
) -> None:
    """Atomically turn an unsigned reservation into the one irreversible attempt."""
    import fcntl

    state_file = _submission_state_path(args)
    state_directory = _open_private_state_dir(state_file.parent)
    os.close(state_directory)
    lock_descriptor = _open_private_lock(state_file.with_suffix(".lock"))
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        current = _read_state(state_file)
        lane = current.get("submission_pending_lane")
        identity = current.get("submission_pending_identity")
        if (
            current.get("submission_pending_id") != attempt_id
            or current.get("submission_pending_phase") != "unsigned_reserved"
            or lane not in {"thin", "authority"}
            or not isinstance(identity, dict)
            or current.get("submission_pending_broadcast_intent") is not None
            or current.get("submission_pending_receipt_candidate") is not None
        ):
            raise ValueError(
                "signed intent does not match one pristine unsigned reservation"
            )

        history = current.get("submission_attempt_ids", [])
        if not isinstance(history, list) or any(
            not isinstance(item, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
            for item in history
        ):
            raise ValueError("common submission attempt journal is corrupt")
        if attempt_id in history:
            raise ValueError("signed attempt already exists in the common journal")
        active_lane = current.get("submission_active_lane")
        transition_from = current.get("submission_pending_lane_transition_from")
        if active_lane is not None and active_lane != lane:
            identity_transition = bool(
                active_lane == "thin"
                and lane == "authority"
                and transition_from == "thin"
                and _authority_lane_transition_authorized(current, identity)
            )
            if not identity_transition:
                raise ValueError(
                    "submission authority lane changed before signed-intent commit"
                )
        elif transition_from is not None:
            raise ValueError("pending submission lane transition is inconsistent")

        budget_scope = current.get("submission_pending_budget_scope")
        budget_limit = current.get("submission_pending_budget_limit")
        budgets = current.get("submission_attempt_budgets", {})
        if not isinstance(budgets, dict):
            raise ValueError("submission attempt budgets are corrupt")
        updated_budgets = dict(budgets)
        if budget_scope is not None:
            if (
                not isinstance(budget_scope, str)
                or re.fullmatch(r"[a-z0-9_]{1,64}", budget_scope) is None
                or isinstance(budget_limit, bool)
                or not isinstance(budget_limit, int)
                or budget_limit <= 0
            ):
                raise ValueError("pending submission budget is malformed")
            budget = budgets.get(budget_scope, {"limit": budget_limit, "ids": []})
            if (
                not isinstance(budget, dict)
                or set(budget) != {"limit", "ids"}
                or budget.get("limit") != budget_limit
                or not isinstance(budget.get("ids"), list)
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                    for item in budget.get("ids", [])
                )
            ):
                raise ValueError("submission attempt budget changed or is corrupt")
            if len(budget["ids"]) >= budget_limit:
                raise ValueError(
                    f"submission attempt budget {budget_limit} is exhausted"
                )
            updated_budgets[budget_scope] = {
                "limit": budget_limit,
                "ids": [*budget["ids"], attempt_id],
            }
        elif budget_limit is not None:
            raise ValueError("pending submission budget limit has no scope")

        launch_attempt = current.get("submission_pending_launch_attempt")
        launch_limit = current.get("submission_pending_launch_budget_limit")
        launch_updates: dict[str, Any] = {}
        if not isinstance(launch_attempt, bool):
            raise ValueError("pending launch marker is malformed")
        if launch_attempt:
            launch_history = current.get("submission_launch_attempt_ids", [])
            if (
                launch_limit != 1
                or not isinstance(launch_history, list)
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", item) is None
                    for item in launch_history
                )
                or launch_history
            ):
                raise ValueError("launch submission budget 1 is exhausted or corrupt")
            launch_updates = {
                "submission_launch_attempt_ids": [attempt_id],
                "submission_launch_budget_limit": 1,
                "submission_launch_status": "pending",
            }
        elif launch_limit is not None:
            raise ValueError("non-launch signed attempt carries a launch budget")

        lane_updates: dict[str, int]
        if lane == "thin":
            policy_version = current.get("submission_pending_policy_version")
            prior_policy = current.get("submission_highest_policy_version")
            if (
                isinstance(policy_version, bool)
                or not isinstance(policy_version, int)
                or policy_version < 0
                or (isinstance(prior_policy, int) and policy_version <= prior_policy)
            ):
                raise ValueError("common thin policy fence changed before signing")
            lane_updates = {"submission_highest_policy_version": policy_version}
        else:
            source_epoch = current.get("submission_pending_source_epoch")
            prior_source = current.get("submission_highest_source_epoch")
            if (
                isinstance(source_epoch, bool)
                or not isinstance(source_epoch, int)
                or source_epoch < 0
                or (isinstance(prior_source, int) and source_epoch <= prior_source)
            ):
                raise ValueError("common authority epoch fence changed before signing")
            lane_updates = {"submission_highest_source_epoch": source_epoch}

        document = dict(current)
        document.update(
            {
                "submission_pending_phase": "signed_intent",
                "submission_pending_broadcast_intent": intent,
                "submission_pending_broadcast_started_at": _ms_iso_now(),
                "submission_attempt_ids": [*history, attempt_id],
                "submission_attempt_budgets": updated_budgets,
                "submission_active_lane": lane,
                "submission_attempt_count": len(history) + 1,
                **lane_updates,
                **launch_updates,
            }
        )
        _replace_private_state(state_file, document)
    finally:
        os.close(lock_descriptor)


def _abort_unsigned_common_submission(args: Any, *, attempt_id: str) -> bool:
    """Release only a reservation that provably never reached signed intent."""
    import fcntl

    state_file = _submission_state_path(args)
    state_directory = _open_private_state_dir(state_file.parent)
    os.close(state_directory)
    lock_descriptor = _open_private_lock(state_file.with_suffix(".lock"))
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        current = _read_state(state_file)
        if (
            current.get("submission_pending_id") != attempt_id
            or current.get("submission_pending_phase") != "unsigned_reserved"
            or current.get("submission_pending_broadcast_intent") is not None
            or current.get("submission_pending_receipt_candidate") is not None
        ):
            return False
        document = dict(current)
        for key in tuple(document):
            if key.startswith("submission_pending_"):
                document.pop(key, None)
        document["submission_pending_id"] = None
        _replace_private_state(state_file, document)
        return True
    finally:
        os.close(lock_descriptor)


def _record_pending_broadcast_intent(
    args: Any,
    *,
    attempt_id: str,
    extrinsic_hash: str,
    nonce: int,
    era_reference_block: int,
    mortal_period_blocks: int,
    version_key: int,
    wire_uids: list[int],
    wire_weights: list[int],
) -> None:
    """Fsync the exact signed transaction before its irreversible submission.

    The intent does not prove that a broadcast occurred and never authorizes a
    retry. Its signed hash gives restart recovery one cryptographic transaction
    identity to search for when the process dies after broadcast but before a
    canonical receipt is returned.
    """
    if (
        re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
        or _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None
        or isinstance(nonce, bool)
        or not isinstance(nonce, int)
        or nonce < 0
        or isinstance(era_reference_block, bool)
        or not isinstance(era_reference_block, int)
        or era_reference_block <= 0
        or mortal_period_blocks != SN39_MORTAL_PERIOD_BLOCKS
        or isinstance(version_key, bool)
        or not isinstance(version_key, int)
        or version_key < 0
        or not wire_uids
        or len(wire_uids) != len(wire_weights)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in [*wire_uids, *wire_weights]
        )
    ):
        raise wire.VectorError("pending broadcast intent is malformed")
    _commit_pending_signed_attempt(
        args,
        attempt_id=attempt_id,
        intent={
            "extrinsic_hash": extrinsic_hash.lower(),
            "nonce": nonce,
            "era_reference_block": era_reference_block,
            "mortal_period_blocks": mortal_period_blocks,
            "version_key": version_key,
            "wire_uids": wire_uids,
            "wire_weights": wire_weights,
        },
    )


def _record_pending_proof_status(
    args: Any,
    *,
    attempt_id: str,
    status: str,
) -> None:
    if status not in {PASS, FAIL, NOT_PROVEN}:
        raise ValueError("pending proof status is invalid")
    _write_state_fenced(
        _submission_state_path(args),
        {
            "_record_receipt_for_submission_id": attempt_id,
            "submission_pending_proof_status": status,
            "submission_pending_proof_checked_at": _ms_iso_now(),
        },
    )


def _finalize_common_submission(
    args: Any,
    *,
    attempt_id: str,
    submission: ChainSubmission,
    version_key: int | None = None,
) -> None:
    finalized_version_key = (
        _weight_version_key() if version_key is None else version_key
    )
    pending = _read_state(_submission_state_path(args))
    if (
        pending.get("submission_pending_id") != attempt_id
        or pending.get("submission_pending_phase") != "signed_intent"
        or not isinstance(pending.get("submission_pending_broadcast_intent"), dict)
    ):
        raise ValueError("submission finalization has no matching exact signed intent")
    pending_lane = pending.get("submission_pending_lane")
    pending_identity = pending.get("submission_pending_identity")
    pending_intent = pending.get("submission_pending_broadcast_intent")
    try:
        intent_hash = str(pending_intent["extrinsic_hash"]).lower()
        intent_version_key = pending_intent["version_key"]
        intent_wire_uids = list(pending_intent["wire_uids"])
        intent_wire_weights = list(pending_intent["wire_weights"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "submission finalization has a malformed signed intent"
        ) from exc
    try:
        pending_uid_weights = {
            int(uid): float(weight) for uid, weight in pending_identity["uid_weights"]
        }
        pending_ordered = sorted(pending_uid_weights.items())
        expected_wire_uids, expected_wire_weights = _wire_weights(
            [uid for uid, _weight in pending_ordered],
            [weight for _uid, weight in pending_ordered],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "submission finalization has a malformed reserved vector"
        ) from exc
    if (
        pending_lane not in {"thin", "authority"}
        or not isinstance(pending_identity, dict)
        or not submission.success
        or submission.finalized is not True
        or not isinstance(submission.extrinsic_hash, str)
        or _CHAIN_HASH_RE.fullmatch(submission.extrinsic_hash.lower()) is None
        or submission.extrinsic_hash.lower() != intent_hash
        or not isinstance(submission.block_hash, str)
        or _CHAIN_HASH_RE.fullmatch(submission.block_hash.lower()) is None
        or isinstance(submission.block_number, bool)
        or not isinstance(submission.block_number, int)
        or submission.block_number <= 0
        or isinstance(intent_version_key, bool)
        or not isinstance(intent_version_key, int)
        or intent_version_key < 0
        or intent_version_key != finalized_version_key
        or not intent_wire_uids
        or len(intent_wire_uids) != len(intent_wire_weights)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in [*intent_wire_uids, *intent_wire_weights]
        )
        or intent_wire_uids != expected_wire_uids
        or intent_wire_weights != expected_wire_weights
    ):
        raise ValueError("submission finalization differs from its exact signed intent")
    finalized_receipt = {
        "extrinsic_hash": submission.extrinsic_hash.lower(),
        "block_hash": submission.block_hash.lower(),
        "block_number": submission.block_number,
        "version_key": finalized_version_key,
        "wire_uids": intent_wire_uids,
        "wire_weights": intent_wire_weights,
    }
    launch_updates: dict[str, Any] = {}
    launch_attempt = pending.get("submission_pending_launch_attempt")
    if not isinstance(launch_attempt, bool):
        raise ValueError("submission finalization has no durable launch marker")
    if launch_attempt:
        launch_identity = pending.get("submission_pending_identity")
        launch_intent = pending.get("submission_pending_broadcast_intent")
        if not isinstance(launch_identity, dict):
            raise ValueError("launch finalization has no exact pending identity")
        if (
            pending.get("submission_pending_phase") != "signed_intent"
            or not isinstance(launch_intent, dict)
            or not isinstance(launch_identity.get("uid_safety"), dict)
        ):
            raise ValueError(
                "launch finalization has no signed intent or UID safety proof"
            )
        launch_updates = {
            "submission_launch_status": "finalized",
            "submission_launch_attempt_id": attempt_id,
            "submission_launch_identity": launch_identity,
            "submission_launch_extrinsic_hash": submission.extrinsic_hash,
            "submission_launch_block_hash": submission.block_hash,
            "submission_launch_block_number": submission.block_number,
            "submission_launch_version_key": finalized_version_key,
            "submission_launch_broadcast_intent": launch_intent,
            "submission_launch_uid_safety": launch_identity["uid_safety"],
            "submission_continuous_enabled": False,
        }
    _write_state_fenced(
        _submission_state_path(args),
        {
            "_finalize_submission_id": attempt_id,
            "submission_finalized_id": attempt_id,
            "submission_finalized_at": _ms_iso_now(),
            "submission_extrinsic_hash": submission.extrinsic_hash,
            "submission_block_hash": submission.block_hash,
            "submission_block_number": submission.block_number,
            "submission_version_key": finalized_version_key,
            "submission_finalized_lane": pending_lane,
            "submission_finalized_identity": pending_identity,
            "submission_finalized_broadcast_intent": pending_intent,
            "submission_finalized_receipt": finalized_receipt,
            "submission_pending_proof_status": PASS,
            **launch_updates,
        },
    )


def _recover_common_finalized_submission(
    args: Any,
    state: dict[str, Any],
) -> RecoveredSubmission | RecoveredAuthoritySubmission | None:
    """Mirror a proven common finalization after a lane-telemetry crash."""
    attempt_id = state.get("submission_finalized_id")
    lane = state.get("submission_finalized_lane")
    identity = state.get("submission_finalized_identity")
    intent = state.get("submission_finalized_broadcast_intent")
    receipt = state.get("submission_finalized_receipt")
    if attempt_id is None:
        return None
    if lane is None and identity is None and intent is None and receipt is None:
        # Compatibility with finalized journals created before the explicit
        # cross-file recovery record existed.
        return None
    history = state.get("submission_attempt_ids")
    if (
        not isinstance(attempt_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
        or lane not in {"thin", "authority"}
        or not isinstance(identity, dict)
        or not isinstance(intent, dict)
        or not isinstance(receipt, dict)
        or not isinstance(history, list)
        or attempt_id not in history
        or state.get("submission_pending_id") is not None
        or state.get("submission_active_lane") != lane
        or state.get("submission_pending_lane") != lane
        or state.get("submission_pending_phase") != "signed_intent"
        or state.get("submission_pending_identity") != identity
        or state.get("submission_pending_broadcast_intent") != intent
        or state.get("submission_pending_proof_status") != PASS
        or set(intent)
        != {
            "extrinsic_hash",
            "nonce",
            "era_reference_block",
            "mortal_period_blocks",
            "version_key",
            "wire_uids",
            "wire_weights",
        }
        or set(receipt)
        != {
            "extrinsic_hash",
            "block_hash",
            "block_number",
            "version_key",
            "wire_uids",
            "wire_weights",
        }
    ):
        raise _PostSignedSubmissionMismatch(
            "finalized common submission recovery record is contradictory"
        )
    try:
        extrinsic_hash = str(receipt["extrinsic_hash"]).lower()
        block_hash = str(receipt["block_hash"]).lower()
        block_number = receipt["block_number"]
        version_key = receipt["version_key"]
        wire_uids = list(receipt["wire_uids"])
        wire_weights = list(receipt["wire_weights"])
        intent_extrinsic_hash = str(intent["extrinsic_hash"]).lower()
        intent_version_key = intent["version_key"]
        intent_wire_uids = list(intent["wire_uids"])
        intent_wire_weights = list(intent["wire_weights"])
        intent_nonce = intent["nonce"]
        intent_era_reference_block = intent["era_reference_block"]
        intent_mortal_period_blocks = intent["mortal_period_blocks"]
        uid_weights = {
            int(uid): float(weight) for uid, weight in identity["uid_weights"]
        }
        uid_hotkeys = {int(uid): str(hotkey) for uid, hotkey in identity["uid_hotkeys"]}
        ordered_weights = sorted(uid_weights.items())
        expected_wire_uids, expected_wire_weights = _wire_weights(
            [uid for uid, _weight in ordered_weights],
            [weight for _uid, weight in ordered_weights],
        )
        burn_hotkey = str(identity["burn_hotkey"])
        burn_uid = next(
            uid for uid, hotkey in uid_hotkeys.items() if hotkey == burn_hotkey
        )
        burn_share = uid_weights[burn_uid]
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise _PostSignedSubmissionMismatch(
            "finalized common submission identity or receipt is malformed"
        ) from exc
    if (
        identity.get("network") != "finney"
        or identity.get("netuid") != 39
        or state.get("submission_genesis_hash") != FINNEY_GENESIS_HASH
        or identity.get("validator_hotkey") != state.get("submission_validator_hotkey")
        or intent_era_reference_block != identity.get("mapping_block")
        or set(uid_hotkeys) != set(uid_weights)
        or list(uid_hotkeys.values()).count(burn_hotkey) != 1
        or not math.isfinite(burn_share)
        or burn_share < 0.0
        or _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None
        or _CHAIN_HASH_RE.fullmatch(block_hash) is None
        or isinstance(block_number, bool)
        or not isinstance(block_number, int)
        or block_number <= 0
        or isinstance(version_key, bool)
        or not isinstance(version_key, int)
        or version_key < 0
        or isinstance(intent_nonce, bool)
        or not isinstance(intent_nonce, int)
        or intent_nonce < 0
        or isinstance(intent_era_reference_block, bool)
        or not isinstance(intent_era_reference_block, int)
        or intent_era_reference_block <= 0
        or intent_mortal_period_blocks != SN39_MORTAL_PERIOD_BLOCKS
        or isinstance(intent_version_key, bool)
        or not isinstance(intent_version_key, int)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in [
                *wire_uids,
                *wire_weights,
                *intent_wire_uids,
                *intent_wire_weights,
            ]
        )
        or extrinsic_hash != intent_extrinsic_hash
        or version_key != intent_version_key
        or wire_uids != intent_wire_uids
        or wire_weights != intent_wire_weights
        or wire_uids != expected_wire_uids
        or wire_weights != expected_wire_weights
        or state.get("submission_extrinsic_hash") != extrinsic_hash
        or state.get("submission_block_hash") != block_hash
        or state.get("submission_block_number") != block_number
        or state.get("submission_version_key") != version_key
    ):
        raise _PostSignedSubmissionMismatch(
            "finalized common submission does not match its durable identity"
        )

    lane_state_path = Path(args.state_file)
    try:
        lane_state = _read_state(lane_state_path)
    except ValueError as exc:
        raise _PostSignedSubmissionMismatch(
            "finalized common submission contradicts its lane state"
        ) from exc
    except OSError as exc:
        raise _PendingReceiptNotProven(
            "finalized common submission is proven, but its lane state is "
            "temporarily unavailable"
        ) from exc
    finalized_at = _ms_iso_now()
    dedup_identity = {
        key: value
        for key, value in identity.items()
        if key not in {"mapping_block", "uid_safety"}
    }
    if lane == "thin":
        try:
            policy_version = int(identity["policy_version"])
            vector_id = str(identity["vector_id"])
            signed_vector_sha256 = str(identity["signed_vector_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _PostSignedSubmissionMismatch(
                "finalized common thin identity is malformed"
            ) from exc
        if (
            policy_version < 0
            or not vector_id
            or re.fullmatch(r"sha256:[0-9a-f]{64}", signed_vector_sha256) is None
        ):
            raise _PostSignedSubmissionMismatch(
                "finalized common thin identity is malformed"
            )
        mirrored = bool(
            lane_state.get("thin_submission_attempt_status") == "finalized"
            and lane_state.get("thin_submission_finalized_id") == attempt_id
            and lane_state.get("thin_submission_identity") == identity
            and lane_state.get("thin_submission_extrinsic_hash") == extrinsic_hash
            and lane_state.get("thin_submission_block_hash") == block_hash
            and lane_state.get("thin_submission_block_number") == block_number
            and lane_state.get("last_accepted_policy_version") == policy_version
            and lane_state.get("last_vector_id") == vector_id
        )
        if mirrored:
            return None
        if (
            lane_state.get("thin_submission_attempt_id") == attempt_id
            or lane_state.get("thin_submission_finalized_id") == attempt_id
        ):
            raise _PostSignedSubmissionMismatch(
                "finalized common thin submission contradicts its lane mirror"
            )
        try:
            _write_state_fenced(
                lane_state_path,
                {
                    "highest_attempted_policy_version": policy_version,
                    "thin_submission_attempt_id": attempt_id,
                    "thin_submission_attempt_status": "finalized",
                    "thin_submission_finalized_id": attempt_id,
                    "thin_submission_finalized_at": finalized_at,
                    "thin_submission_extrinsic_hash": extrinsic_hash,
                    "thin_submission_block_hash": block_hash,
                    "thin_submission_block_number": block_number,
                    "thin_submission_identity": identity,
                    "thin_submission_dedup_identity": dedup_identity,
                    "last_accepted_policy_version": policy_version,
                    "last_vector_id": vector_id,
                    "accepted_at": finalized_at,
                    "thin_recovered_policy_version": policy_version,
                    "thin_recovered_vector_id": vector_id,
                    "thin_recovered_signed_vector_sha256": signed_vector_sha256,
                },
            )
        except ValueError as exc:
            raise _PostSignedSubmissionMismatch(
                "finalized common thin submission contradicts its lane state"
            ) from exc
        except OSError as exc:
            raise _PendingReceiptNotProven(
                "finalized common thin submission is proven, but its lane mirror "
                "could not be persisted"
            ) from exc
        return RecoveredSubmission(
            attempt_id=attempt_id,
            policy_version=policy_version,
            vector_id=vector_id,
            signed_vector_sha256=signed_vector_sha256,
            uid_weights=tuple(sorted(uid_weights.items())),
            burn_uid=burn_uid,
            burn_share=burn_share,
            extrinsic_hash=extrinsic_hash,
            block_hash=block_hash,
            block_number=block_number,
        )

    try:
        source_epoch = int(identity["source_epoch"])
        report_id = str(identity["report_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _PostSignedSubmissionMismatch(
            "finalized common FULL identity is malformed"
        ) from exc
    if source_epoch < 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", report_id) is None:
        raise _PostSignedSubmissionMismatch(
            "finalized common FULL identity is malformed"
        )
    mirrored = bool(
        lane_state.get("authority_submission_attempt_status") == "finalized"
        and lane_state.get("authority_submission_finalized_id") == attempt_id
        and lane_state.get("authority_submission_identity") == identity
        and lane_state.get("authority_submission_extrinsic_hash") == extrinsic_hash
        and lane_state.get("authority_submission_block_hash") == block_hash
        and lane_state.get("authority_submission_block_number") == block_number
    )
    if mirrored:
        return None
    if (
        lane_state.get("authority_submission_attempt_id") == attempt_id
        or lane_state.get("authority_submission_finalized_id") == attempt_id
    ):
        raise _PostSignedSubmissionMismatch(
            "finalized common FULL submission contradicts its lane mirror"
        )
    try:
        _write_state_fenced(
            lane_state_path,
            {
                "authority_submission_attempt_id": attempt_id,
                "authority_submission_attempt_status": "finalized",
                "authority_submission_attempted_at": finalized_at,
                "authority_submission_identity": identity,
                "authority_submission_dedup_identity": dedup_identity,
                "authority_submission_finalized_id": attempt_id,
                "authority_submission_finalized_at": finalized_at,
                "authority_submission_extrinsic_hash": extrinsic_hash,
                "authority_submission_block_hash": block_hash,
                "authority_submission_block_number": block_number,
            },
        )
    except ValueError as exc:
        raise _PostSignedSubmissionMismatch(
            "finalized common FULL submission contradicts its lane state"
        ) from exc
    except OSError as exc:
        raise _PendingReceiptNotProven(
            "finalized common FULL submission is proven, but its lane mirror "
            "could not be persisted"
        ) from exc
    return RecoveredAuthoritySubmission(
        attempt_id=attempt_id,
        source_epoch=source_epoch,
        report_id=report_id,
        uid_weights=tuple(sorted(uid_weights.items())),
        burn_uid=burn_uid,
        burn_share=burn_share,
        extrinsic_hash=extrinsic_hash,
        block_hash=block_hash,
        block_number=block_number,
    )


def _policy_from_submission_identity(identity: dict[str, Any]) -> InclusionPolicy:
    raw = identity.get("inclusion_policy")
    if not isinstance(raw, dict):
        raise wire.VectorError("pending submission has no inclusion policy")
    try:
        policy = InclusionPolicy(
            valid_from_block=int(raw["valid_from_block"]),
            valid_until_block=int(raw["valid_until_block"]),
            valid_from_time=wire._parse_canonical_utc(
                raw["valid_from_time"],
                field="pending inclusion valid_from_time",
            ),
            valid_until_time=wire._parse_canonical_utc(
                raw["valid_until_time"],
                field="pending inclusion valid_until_time",
            ),
            require_commit_reveal_disabled=raw["require_commit_reveal_disabled"],
            mortal_period_blocks=int(raw["mortal_period_blocks"]),
            expected_next_epoch_start_block=int(raw["expected_next_epoch_start_block"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError("pending inclusion policy is malformed") from exc
    if (
        not isinstance(policy.require_commit_reveal_disabled, bool)
        or identity.get("next_epoch_start_block")
        != policy.expected_next_epoch_start_block
    ):
        raise wire.VectorError("pending inclusion policy identity is inconsistent")
    return policy


def _locate_pending_broadcast_receipt(
    subtensor: Any,
    *,
    extrinsic_hash: str,
    era_reference_block: int,
    mortal_period_blocks: int,
    validator_hotkey: str,
    netuid: int,
    version_key: int,
    wire_uids: list[int],
    wire_weights: list[int],
    inclusion_policy: InclusionPolicy,
) -> tuple[str, ChainSubmission | None]:
    """Find the pre-journaled signed transaction after a crash hid its receipt.

    The search is limited to the four blocks authorized by the durable mortal
    inclusion policy and matches the signed hash, signer, and decoded call. It
    does not sign or submit anything.
    """
    substrate = getattr(subtensor, "substrate", None)
    if (
        substrate is None
        or _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None
        or isinstance(era_reference_block, bool)
        or not isinstance(era_reference_block, int)
        or era_reference_block <= 0
        or mortal_period_blocks != SN39_MORTAL_PERIOD_BLOCKS
    ):
        return NOT_PROVEN, None
    matches: list[ChainSubmission] = []
    try:
        finalized_hash = str(substrate.get_chain_finalised_head())
        finalized_number = int(substrate.get_block_number(finalized_hash))
        canonical_finalized_hash = str(substrate.get_block_hash(finalized_number))
        if (
            _CHAIN_HASH_RE.fullmatch(finalized_hash) is None
            or canonical_finalized_hash.lower() != finalized_hash.lower()
        ):
            return NOT_PROVEN, None
        search_from = max(
            inclusion_policy.valid_from_block,
            era_reference_block,
        )
        search_until = min(
            inclusion_policy.valid_until_block,
            era_reference_block + mortal_period_blocks,
        )
        if search_from >= search_until:
            return FAIL, None
        for block_number in range(search_from, search_until):
            if block_number > finalized_number:
                continue
            block_hash = str(substrate.get_block_hash(block_number))
            if _CHAIN_HASH_RE.fullmatch(block_hash) is None:
                return NOT_PROVEN, None
            block = substrate.get_block(block_hash=block_hash)
            if not isinstance(block, dict) or not isinstance(
                block.get("extrinsics"), (list, tuple)
            ):
                return NOT_PROVEN, None
            for item in block["extrinsics"]:
                observed = getattr(item, "value", None)
                if not isinstance(observed, dict):
                    continue
                observed_hash = str(observed.get("extrinsic_hash", "")).lower()
                if observed_hash != extrinsic_hash.lower():
                    continue
                call = observed.get("call") or {}
                exact_call = (
                    observed.get("address") == validator_hotkey
                    and call.get("call_module") == "SubtensorModule"
                    and call.get("call_function") == "set_mechanism_weights"
                    and _chain_call_arg(call, "netuid") == netuid
                    and _chain_call_arg(call, "mecid") == 0
                    and _chain_call_arg(call, "version_key") == version_key
                    and _chain_call_arg(call, "dests") == wire_uids
                    and _chain_call_arg(call, "weights") == wire_weights
                )
                if not exact_call:
                    return FAIL, None
                matches.append(
                    ChainSubmission(
                        success=True,
                        extrinsic_hash=extrinsic_hash.lower(),
                        block_hash=block_hash,
                        block_number=block_number,
                        finalized=True,
                    )
                )
    except Exception:  # noqa: BLE001 - archive/RPC unavailability is inconclusive
        return NOT_PROVEN, None
    if len(matches) > 1:
        return FAIL, None
    if not matches:
        if finalized_number >= search_until - 1:
            # Every block in the complete authorized mortal era was fetched
            # successfully from one reverse-checked finalized chain. Absence is
            # now terminal rather than an RPC gap: the signed transaction can no
            # longer be included, so restart recovery may retire only this exact
            # attempt without authorizing it again.
            return EXPIRED_WITHOUT_INCLUSION, None
        return NOT_PROVEN, None
    return PASS, matches[0]


def _expire_pending_common_submission(args: Any, *, attempt_id: str) -> None:
    """Retire one exhaustively absent signed attempt without erasing its budget.

    This transition is valid only after the complete mortal era has been
    finalized and read without an exact inclusion. It clears the ambiguity
    fence, but deliberately retains the signed-attempt, authorization-budget,
    policy/source high-water, and launch histories. The same attempt therefore
    remains permanently ineligible while a distinct later attempt still needs
    its own fresh vector/evidence and signed authorization.
    """
    import fcntl

    state_file = _submission_state_path(args)
    state_directory = _open_private_state_dir(state_file.parent)
    os.close(state_directory)
    lock_descriptor = _open_private_lock(state_file.with_suffix(".lock"))
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        current = _read_state(state_file)
        if (
            current.get("submission_pending_id") != attempt_id
            or current.get("submission_pending_phase") != "signed_intent"
            or not isinstance(current.get("submission_pending_broadcast_intent"), dict)
            or current.get("submission_pending_receipt_candidate") is not None
        ):
            raise ValueError(
                "expired submission does not match one receipt-free signed intent"
            )
        history = current.get("submission_attempt_ids")
        budgets = current.get("submission_attempt_budgets")
        if (
            not isinstance(history, list)
            or attempt_id not in history
            or not isinstance(budgets, dict)
        ):
            raise ValueError(
                "expired submission has no retained attempt or budget history"
            )
        terminal_attempts = current.get("submission_expired_attempts", [])
        if not isinstance(terminal_attempts, list) or any(
            not isinstance(item, dict) for item in terminal_attempts
        ):
            raise ValueError("expired submission history is malformed")
        if any(item.get("attempt_id") == attempt_id for item in terminal_attempts):
            raise ValueError("expired submission was already retired")

        lane = current.get("submission_pending_lane")
        identity = current.get("submission_pending_identity")
        intent = current.get("submission_pending_broadcast_intent")
        launch_attempt = current.get("submission_pending_launch_attempt")
        if (
            lane not in {"thin", "authority"}
            or not isinstance(identity, dict)
            or not isinstance(launch_attempt, bool)
        ):
            raise ValueError("expired submission identity is malformed")
        expired_at = _ms_iso_now()
        terminal = {
            "attempt_id": attempt_id,
            "status": EXPIRED_WITHOUT_INCLUSION,
            "expired_at": expired_at,
            "lane": lane,
            "identity": identity,
            "broadcast_intent": intent,
        }
        document = dict(current)
        for key in tuple(document):
            if key.startswith("submission_pending_"):
                document.pop(key, None)
        document.update(
            {
                "submission_pending_id": None,
                "submission_expired_status": EXPIRED_WITHOUT_INCLUSION,
                "submission_expired_id": attempt_id,
                "submission_expired_at": expired_at,
                "submission_expired_lane": lane,
                "submission_expired_identity": identity,
                "submission_expired_broadcast_intent": intent,
                "submission_expired_attempts": [*terminal_attempts, terminal],
            }
        )
        if launch_attempt:
            document.update(
                {
                    "submission_launch_status": EXPIRED_WITHOUT_INCLUSION,
                    "submission_launch_attempt_id": attempt_id,
                    "submission_launch_identity": identity,
                    "submission_launch_broadcast_intent": intent,
                    "submission_continuous_enabled": False,
                }
            )
        _replace_private_state(state_file, document)
    finally:
        os.close(lock_descriptor)


def _recover_pending_launch_receipt(
    args: Any,
) -> RecoveredSubmission | RecoveredAuthoritySubmission | None:
    """Re-prove and finalize one journaled thin or FULL receipt without writing.

    This runs before every profile tick. ``None`` means there is no pending
    submission. A positive historical mismatch or unavailable archive remains
    fenced and exits nonzero; recovery never signs or submits.
    """
    if not bool(getattr(args, "broadcast", False)) or bool(
        getattr(args, "offline", False)
    ):
        return None
    try:
        _prepare_tick_preflight(args)
    except (_PostSignedSubmissionMismatch, _PendingReceiptNotProven):
        raise
    except Exception as exc:
        raise _PendingReceiptNotProven(
            "pending receipt recovery chain preflight is temporarily unavailable"
        ) from exc
    recovery_lock = _pending_recovery_tick_lock(args)
    recovery_lock.__enter__()
    try:
        state_path = _submission_state_path(args)
        state = _read_state(state_path)
        attempt_id = state.get("submission_pending_id")
        if attempt_id is None:
            return _recover_common_finalized_submission(args, state)
        pending_lane = state.get("submission_pending_lane")
        if (
            not isinstance(attempt_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
            or pending_lane not in {"thin", "authority"}
        ):
            raise wire.VectorError("pending submission journal is malformed")
        pending_phase = state.get("submission_pending_phase")
        if pending_phase == "unsigned_reserved":
            if not _abort_unsigned_common_submission(args, attempt_id=attempt_id):
                raise wire.VectorError(
                    "unsigned reservation changed while restart recovery held the "
                    "submission lock"
                )
            _lifecycle(
                "CHAIN reservation released",
                f"attempt_id={attempt_id} signed=false broadcast=false",
            )
            return None
        if pending_phase != "signed_intent":
            raise wire.VectorError(
                "pending submission journal has no recognized unsigned or signed phase"
            )
        if state.get("submission_pending_proof_status") == FAIL:
            raise _PostSignedSubmissionMismatch(
                "pending submission has a positive historical proof mismatch; "
                "automatic recovery is forbidden"
            )
        identity = state.get("submission_pending_identity")
        candidate = state.get("submission_pending_receipt_candidate")
        if not isinstance(identity, dict):
            raise wire.VectorError(
                "pending submission has no complete submission identity"
            )
        preflight = getattr(args, "_tick_preflight", None)
        if not isinstance(preflight, ChainPreflight):
            raise _PendingReceiptNotProven(
                "pending submission recovery has no available chain preflight"
            )
        try:
            uid_weights = {
                int(uid): float(weight) for uid, weight in identity["uid_weights"]
            }
            uid_hotkeys = {
                int(uid): str(hotkey) for uid, hotkey in identity["uid_hotkeys"]
            }
            ordered = sorted(uid_weights.items())
            expected_wire_uids, expected_wire_weights = _wire_weights(
                [uid for uid, _weight in ordered],
                [weight for _uid, weight in ordered],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError("pending submission identity is malformed") from exc
        inclusion_policy = _policy_from_submission_identity(identity)
        intent = state.get("submission_pending_broadcast_intent")
        if not isinstance(intent, dict):
            raise wire.VectorError(
                "pending submission predates its exact signed broadcast intent; "
                "refusing any retry"
            )
        try:
            intent_extrinsic_hash = str(intent["extrinsic_hash"]).lower()
            intent_nonce = intent["nonce"]
            intent_era_reference_block = intent["era_reference_block"]
            intent_mortal_period = intent["mortal_period_blocks"]
            version_key = intent["version_key"]
            wire_uids = list(intent["wire_uids"])
            wire_weights = list(intent["wire_weights"])
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError(
                "pending signed broadcast intent is malformed"
            ) from exc
        if (
            _CHAIN_HASH_RE.fullmatch(intent_extrinsic_hash) is None
            or isinstance(intent_nonce, bool)
            or not isinstance(intent_nonce, int)
            or intent_nonce < 0
            or isinstance(intent_era_reference_block, bool)
            or not isinstance(intent_era_reference_block, int)
            or intent_era_reference_block != identity.get("mapping_block")
            or isinstance(intent_mortal_period, bool)
            or not isinstance(intent_mortal_period, int)
            or intent_mortal_period != inclusion_policy.mortal_period_blocks
            or intent_mortal_period != SN39_MORTAL_PERIOD_BLOCKS
            or isinstance(version_key, bool)
            or not isinstance(version_key, int)
            or version_key < 0
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in [*wire_uids, *wire_weights]
            )
            or wire_uids != expected_wire_uids
            or wire_weights != expected_wire_weights
        ):
            raise wire.VectorError(
                "pending signed broadcast intent differs from its reserved vector "
                "or inclusion policy"
            )
        if not isinstance(candidate, dict):
            locate_status, located = _locate_pending_broadcast_receipt(
                preflight.subtensor,
                extrinsic_hash=intent_extrinsic_hash,
                era_reference_block=intent_era_reference_block,
                mortal_period_blocks=intent_mortal_period,
                validator_hotkey=preflight.validator_hotkey,
                netuid=39,
                version_key=version_key,
                wire_uids=wire_uids,
                wire_weights=wire_weights,
                inclusion_policy=inclusion_policy,
            )
            if locate_status == EXPIRED_WITHOUT_INCLUSION and located is None:
                _expire_pending_common_submission(args, attempt_id=attempt_id)
                _lifecycle(
                    "CHAIN expired",
                    f"attempt_id={attempt_id} included=false resubmitted=false",
                )
                return None
            _record_pending_proof_status(
                args,
                attempt_id=attempt_id,
                status=locate_status,
            )
            if locate_status == FAIL:
                raise _PostSignedSubmissionMismatch(
                    "pending broadcast recovery found conflicting historical "
                    "calls; operator investigation is required"
                )
            if locate_status == NOT_PROVEN or located is None:
                raise _PendingReceiptNotProven(
                    "pending broadcast has no unique finalized exact transaction "
                    "in its authorized block window; no second chain write was "
                    "attempted"
                )
            _record_pending_submission_receipt(
                args,
                attempt_id=attempt_id,
                submission=located,
                version_key=version_key,
                wire_uids=wire_uids,
                wire_weights=wire_weights,
            )
            candidate = {
                "extrinsic_hash": located.extrinsic_hash,
                "block_hash": located.block_hash,
                "block_number": located.block_number,
                "version_key": version_key,
                "wire_uids": wire_uids,
                "wire_weights": wire_weights,
            }
        try:
            extrinsic_hash = str(candidate["extrinsic_hash"])
            block_hash = str(candidate["block_hash"])
            block_number = int(candidate["block_number"])
            version_key = int(candidate["version_key"])
            wire_uids = [int(value) for value in candidate["wire_uids"]]
            wire_weights = [int(value) for value in candidate["wire_weights"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError("pending receipt candidate is malformed") from exc
        if (
            identity.get("network") != "finney"
            or identity.get("netuid") != 39
            or identity.get("validator_hotkey") != preflight.validator_hotkey
            or state.get("submission_genesis_hash") != preflight.genesis_hash
            or _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None
            or extrinsic_hash.lower() != intent_extrinsic_hash
            or _CHAIN_HASH_RE.fullmatch(block_hash) is None
            or block_number <= 0
            or wire_uids != expected_wire_uids
            or wire_weights != expected_wire_weights
            or set(uid_hotkeys) != set(uid_weights)
        ):
            raise wire.VectorError(
                "pending receipt differs from its reserved vector or chain identity"
            )
        try:
            with _chain_operation_deadline(
                "pending launch receipt recovery",
                CHAIN_OPERATION_DEADLINE_SECS,
            ):
                proof_status = _classify_finalized_receipt(
                    preflight.subtensor,
                    receipt=None,
                    extrinsic_hash=extrinsic_hash,
                    block_hash=block_hash,
                    block_number=block_number,
                    validator_hotkey=preflight.validator_hotkey,
                    netuid=39,
                    version_key=version_key,
                    wire_uids=wire_uids,
                    wire_weights=wire_weights,
                    uid_hotkeys=uid_hotkeys,
                    expected_subnet_owner_hotkey=str(identity.get("burn_hotkey") or ""),
                    inclusion_policy=inclusion_policy,
                    require_receipt=False,
                )
        except (_PostSignedSubmissionMismatch, _PendingReceiptNotProven):
            raise
        except Exception as exc:
            raise _PendingReceiptNotProven(
                "pending receipt archive proof is temporarily unavailable"
            ) from exc
        _record_pending_proof_status(
            args,
            attempt_id=attempt_id,
            status=proof_status,
        )
        if proof_status == NOT_PROVEN:
            raise _PendingReceiptNotProven(
                "pending receipt is still not provable from the archive; "
                "no second chain write was attempted"
            )
        if proof_status == FAIL:
            raise _PostSignedSubmissionMismatch(
                "pending receipt positively mismatches its historical "
                "inclusion contract; operator investigation is required"
            )
        submission = ChainSubmission(
            success=True,
            extrinsic_hash=extrinsic_hash,
            block_hash=block_hash,
            block_number=block_number,
            finalized=True,
        )
        _finalize_common_submission(
            args,
            attempt_id=attempt_id,
            submission=submission,
            version_key=version_key,
        )
        try:
            burn_hotkey = str(identity["burn_hotkey"])
            burn_uid = next(
                uid for uid, hotkey in uid_hotkeys.items() if hotkey == burn_hotkey
            )
            burn_share = float(uid_weights[burn_uid])
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise wire.VectorError(
                "recovered submission has no exact burn identity"
            ) from exc
        if not math.isfinite(burn_share) or burn_share < 0.0:
            raise wire.VectorError("recovered submission identity is malformed")
        lane_state_path = Path(args.state_file)
        lane_state = _read_state(lane_state_path)
        if pending_lane == "thin":
            try:
                policy_version = int(identity["policy_version"])
                vector_id = str(identity["vector_id"])
                signed_vector_sha256 = str(identity["signed_vector_sha256"])
            except (KeyError, TypeError, ValueError) as exc:
                raise wire.VectorError(
                    "recovered thin submission has no exact policy/vector identity"
                ) from exc
            if (
                policy_version < 0
                or not vector_id
                or re.fullmatch(r"sha256:[0-9a-f]{64}", signed_vector_sha256) is None
            ):
                raise wire.VectorError("recovered thin identity is malformed")
            lane_updates = {
                "thin_submission_attempt_status": "finalized",
                "thin_submission_finalized_id": attempt_id,
                "thin_submission_finalized_at": _ms_iso_now(),
                "thin_submission_extrinsic_hash": extrinsic_hash,
                "thin_submission_block_hash": block_hash,
                "thin_submission_block_number": block_number,
                "last_accepted_policy_version": policy_version,
                "last_vector_id": vector_id,
                "accepted_at": _ms_iso_now(),
                "thin_recovered_policy_version": policy_version,
                "thin_recovered_vector_id": vector_id,
                "thin_recovered_signed_vector_sha256": signed_vector_sha256,
            }
            if lane_state.get("thin_submission_attempt_id") == attempt_id:
                _write_state(lane_state_path, lane_updates)
            else:
                _write_state_fenced(
                    lane_state_path,
                    {
                        "highest_attempted_policy_version": policy_version,
                        "thin_submission_attempt_id": attempt_id,
                        "thin_submission_attempted_at": _ms_iso_now(),
                        "thin_submission_identity": identity,
                        **lane_updates,
                    },
                )
            recovered: RecoveredSubmission | RecoveredAuthoritySubmission = (
                RecoveredSubmission(
                    attempt_id=attempt_id,
                    policy_version=policy_version,
                    vector_id=vector_id,
                    signed_vector_sha256=signed_vector_sha256,
                    uid_weights=tuple(sorted(uid_weights.items())),
                    burn_uid=burn_uid,
                    burn_share=burn_share,
                    extrinsic_hash=extrinsic_hash,
                    block_hash=block_hash,
                    block_number=block_number,
                )
            )
        else:
            try:
                source_epoch = int(identity["source_epoch"])
                report_id = str(identity["report_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise wire.VectorError(
                    "recovered FULL submission has no exact evidence identity"
                ) from exc
            if (
                source_epoch < 0
                or re.fullmatch(r"sha256:[0-9a-f]{64}", report_id) is None
            ):
                raise wire.VectorError("recovered FULL identity is malformed")
            dedup_identity = {
                key: value
                for key, value in identity.items()
                if key not in {"mapping_block", "uid_safety"}
            }
            authority_updates = {
                "authority_submission_attempt_id": attempt_id,
                "authority_submission_attempt_status": "finalized",
                "authority_submission_attempted_at": _ms_iso_now(),
                "authority_submission_identity": identity,
                "authority_submission_dedup_identity": dedup_identity,
                "authority_submission_finalized_id": attempt_id,
                "authority_submission_finalized_at": _ms_iso_now(),
                "authority_submission_extrinsic_hash": extrinsic_hash,
                "authority_submission_block_hash": block_hash,
                "authority_submission_block_number": block_number,
            }
            if lane_state.get("authority_submission_attempt_id") == attempt_id:
                _write_state(lane_state_path, authority_updates)
            else:
                _write_state_fenced(lane_state_path, authority_updates)
            recovered = RecoveredAuthoritySubmission(
                attempt_id=attempt_id,
                source_epoch=source_epoch,
                report_id=report_id,
                uid_weights=tuple(sorted(uid_weights.items())),
                burn_uid=burn_uid,
                burn_share=burn_share,
                extrinsic_hash=extrinsic_hash,
                block_hash=block_hash,
                block_number=block_number,
            )
        _lifecycle(
            "CHAIN recovered",
            f"extrinsic_hash={extrinsic_hash} block_number={block_number} "
            "resubmitted=false",
        )
        return recovered
    except (_PostSignedSubmissionMismatch, _PendingReceiptNotProven):
        raise
    except OSError as exc:
        raise _PendingReceiptNotProven(
            "pending receipt recovery state is temporarily unavailable"
        ) from exc
    except Exception as exc:
        raise _PostSignedSubmissionMismatch(
            "pending receipt recovery found contradictory durable state"
        ) from exc
    finally:
        recovery_lock.__exit__(*sys.exc_info())


def _match_signed_public_release_to_launch(
    *,
    public_result: dict[str, Any],
    state: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Bind the mutable service journal to the root-signed public launch seal.

    The validator account owns its local journal, so that journal can establish
    crash safety but cannot authorize a permanent transition by itself.  The
    public reproducer verifies a separately signed release, the historical
    chain execution, the inclusion-time evidence window, and the frozen
    evidence checkpoint.  This matcher then requires that independently
    verified release to name the exact journaled rewarded-set-gated attempt.
    """
    try:
        release = public_result["release"]
        launch = release["launch_submission"]
        mapping = launch["mapping"]
        broadcast_intent = launch["broadcast_intent"]
        snapshot = mapping["metagraph_snapshot"]
        extrinsic = launch["extrinsic"]
        checkpoint = launch["evidence_checkpoint"]
        full = identity["full_provenance"]
        release_uid_weights = {
            int(uid): float(weight) for uid, weight in mapping["uid_weights"].items()
        }
        identity_uid_weights = {
            int(uid): float(weight) for uid, weight in identity["uid_weights"]
        }
        identity_uid_hotkeys = {
            int(uid): str(hotkey) for uid, hotkey in identity["uid_hotkeys"]
        }
        snapshot_uids = [int(value) for value in snapshot["uids"]]
        snapshot_hotkeys = [str(value) for value in snapshot["hotkeys"]]
        snapshot_uid_hotkeys = dict(zip(snapshot_uids, snapshot_hotkeys))
        if len(snapshot_uids) != len(snapshot_hotkeys) or len(
            snapshot_uid_hotkeys
        ) != len(snapshot_uids):
            raise ValueError("public launch metagraph snapshot is inconsistent")
        burn_uid = int(mapping["burn_uid"])
        burn_hotkey = snapshot_uid_hotkeys[burn_uid]
        release_uid_hotkeys = {
            uid: snapshot_uid_hotkeys[uid] for uid in sorted(release_uid_weights)
        }
        expected_uids, expected_weights = _wire_weights(
            sorted(identity_uid_weights),
            [identity_uid_weights[uid] for uid in sorted(identity_uid_weights)],
        )
        if not isinstance(broadcast_intent, dict):
            raise ValueError("public launch broadcast intent is malformed")
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "root-signed public launch evidence is malformed"
        ) from exc

    exact_matches = (
        public_result.get("release_attestation") == PASS,
        public_result.get("historical_launch") == PASS,
        public_result.get("evidence_checkpoint") == PASS,
        release.get("network") == identity.get("network") == "finney",
        release.get("netuid") == identity.get("netuid") == 39,
        launch.get("vector_id") == identity.get("vector_id"),
        launch.get("policy_version") == identity.get("policy_version"),
        launch.get("signed_vector_sha256") == identity.get("signed_vector_sha256"),
        launch.get("signed_vector") == identity.get("signed_vector"),
        mapping.get("block") == identity.get("mapping_block"),
        mapping.get("validator_hotkey") == identity.get("validator_hotkey"),
        mapping.get("validator_uid") == identity.get("validator_uid"),
        snapshot.get("block") == identity.get("mapping_block"),
        mapping.get("next_epoch_start_block") == identity.get("next_epoch_start_block"),
        mapping.get("uid_safety") == identity.get("uid_safety"),
        mapping.get("uid_safety") == state.get("submission_launch_uid_safety"),
        extrinsic.get("block", 0) < mapping.get("next_epoch_start_block", 0),
        burn_uid in release_uid_weights,
        burn_hotkey == identity.get("burn_hotkey"),
        release_uid_weights == identity_uid_weights,
        release_uid_hotkeys == identity_uid_hotkeys,
        extrinsic.get("hash") == state.get("submission_launch_extrinsic_hash"),
        extrinsic.get("block_hash") == state.get("submission_launch_block_hash"),
        extrinsic.get("block") == state.get("submission_launch_block_number"),
        extrinsic.get("validator_uid") == identity.get("validator_uid"),
        extrinsic.get("uids") == expected_uids,
        extrinsic.get("weights_u16") == expected_weights,
        extrinsic.get("version_key") == state.get("submission_launch_version_key"),
        broadcast_intent == state.get("submission_launch_broadcast_intent"),
        broadcast_intent.get("extrinsic_hash") == extrinsic.get("hash"),
        broadcast_intent.get("era_reference_block") == mapping.get("block"),
        broadcast_intent.get("mortal_period_blocks") == SN39_MORTAL_PERIOD_BLOCKS,
        broadcast_intent.get("version_key") == extrinsic.get("version_key"),
        broadcast_intent.get("wire_uids") == extrinsic.get("uids"),
        broadcast_intent.get("wire_weights") == extrinsic.get("weights_u16"),
        broadcast_intent.get("era_reference_block", 0)
        <= extrinsic.get("block", -1)
        < broadcast_intent.get("era_reference_block", 0)
        + broadcast_intent.get("mortal_period_blocks", 0),
        full.get("scope") == "rewarded_set_full",
        full.get("whole_epoch_assurance") in {"receipts_only", "full"},
        full.get("vector_agrees") is True,
        full.get("rewarded_hotkeys") == full.get("raw_replayed_hotkeys"),
        bool(full.get("rewarded_hotkeys")),
        full.get("source_epoch") == checkpoint.get("source_epoch"),
        full.get("report_id") == checkpoint.get("report_id"),
        full.get("manifest") == checkpoint.get("manifest"),
        full.get("policy_release") == checkpoint.get("policy_release"),
        full.get("policy_digest") == checkpoint.get("policy_digest"),
        full.get("mechanism") == checkpoint.get("reward_mechanism", {}).get("id"),
        full.get("verifier_digest") == checkpoint.get("verifier_digest"),
        full.get("verifier_binary_digest") == checkpoint.get("verifier_binary_digest"),
        full.get("report_signing_key_id") == checkpoint.get("report_signing_key_id"),
        full.get("signed_index") == checkpoint.get("signed_index"),
        full.get("source_revision")
        == release.get("source_revisions", {}).get("producer"),
    )
    if not all(exact_matches):
        raise wire.VectorError(
            "root-signed public launch evidence does not match the exact "
            "rewarded-set-gated journal and chain submission"
        )
    return {
        "release_attestation": PASS,
        "historical_launch": PASS,
        "evidence_checkpoint": PASS,
        "reproducer_revision": public_result.get("reproducer_revision"),
        "release_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(
                release,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
    }


def reconcile_launch_transition(args: Any) -> dict[str, Any]:
    """Verify the signed public launch seal and enable continuous operation."""
    preflight = chain_preflight(
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
    )
    _validate_resolved_chain_contract(
        args,
        preflight,
        require_sn39_identity=True,
    )
    _bind_submission_identity(args, preflight)
    with _submission_tick_lock(args, lane="thin"):
        state_path = _submission_state_path(args)
        state = _read_state(state_path)
        if state.get("submission_pending_id") is not None:
            raise wire.VectorError(
                "launch submission remains ambiguous; reconcile it manually before "
                "enabling continuous operation"
            )
        launch_attempt_id = state.get("submission_launch_attempt_id")
        if (
            state.get("submission_launch_status") != "finalized"
            or not isinstance(launch_attempt_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", launch_attempt_id) is None
            or launch_attempt_id not in state.get("submission_launch_attempt_ids", [])
        ):
            raise wire.VectorError("no finalized rewarded-set-gated launch is recorded")
        identity = state.get("submission_launch_identity")
        if not isinstance(identity, dict):
            raise wire.VectorError("finalized launch identity is missing")
        try:
            uid_weights = {
                int(uid): float(weight) for uid, weight in identity["uid_weights"]
            }
            uid_hotkeys = {
                int(uid): str(hotkey) for uid, hotkey in identity["uid_hotkeys"]
            }
            wire_uids, wire_weights = _wire_weights(
                list(dict(sorted(uid_weights.items()))),
                [uid_weights[uid] for uid in sorted(uid_weights)],
            )
            extrinsic_hash = str(state["submission_launch_extrinsic_hash"])
            block_hash = str(state["submission_launch_block_hash"])
            block_number = int(state["submission_launch_block_number"])
            version_key = int(state["submission_launch_version_key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError("finalized launch journal is malformed") from exc
        if (
            identity.get("validator_hotkey") != preflight.validator_hotkey
            or state.get("submission_genesis_hash") != preflight.genesis_hash
            or _CHAIN_HASH_RE.fullmatch(extrinsic_hash) is None
            or _CHAIN_HASH_RE.fullmatch(block_hash) is None
            or block_number <= 0
        ):
            raise wire.VectorError("finalized launch identity differs from the chain")
        try:
            from scaffold import sn39_public_reproduction

            public_result = sn39_public_reproduction.verify_public_release()
            public_seal = _match_signed_public_release_to_launch(
                public_result=public_result,
                state=state,
                identity=identity,
            )
        except wire.VectorError:
            raise
        except Exception as exc:
            raise wire.VectorError(
                "root-signed public launch evidence did not reproduce"
            ) from exc
        with _chain_operation_deadline(
            "launch transition reconciliation", CHAIN_OPERATION_DEADLINE_SECS
        ):
            proven = _prove_finalized_receipt(
                preflight.subtensor,
                receipt=None,
                extrinsic_hash=extrinsic_hash,
                block_hash=block_hash,
                block_number=block_number,
                validator_hotkey=preflight.validator_hotkey,
                netuid=int(args.netuid),
                version_key=version_key,
                wire_uids=wire_uids,
                wire_weights=wire_weights,
                uid_hotkeys=uid_hotkeys,
                require_receipt=False,
            )
        if not proven:
            raise wire.VectorError(
                "recorded launch extrinsic, finality, or inclusion-block UID bindings "
                "did not reproduce"
            )
        _write_state_fenced(
            state_path,
            {
                "submission_continuous_enabled": True,
                "submission_continuous_enabled_at": _ms_iso_now(),
                "submission_continuous_launch_attempt_id": state[
                    "submission_launch_attempt_id"
                ],
                "submission_continuous_release_sha256": public_seal["release_sha256"],
                "submission_continuous_reproducer_revision": public_seal[
                    "reproducer_revision"
                ],
            },
        )
        return {
            "status": PASS,
            "launch_attempt_id": state["submission_launch_attempt_id"],
            "extrinsic_hash": extrinsic_hash,
            "block_hash": block_hash,
            "block_number": block_number,
            **public_seal,
        }


def _require_continuous_launch_transition(args: Any) -> ContinuousAuthorization:
    """Re-prove launch history and a separate recurring authorization.

    The service account owns its crash-safety journal and therefore cannot
    authorize itself merely by editing journal fields.  The root-signed public
    release is re-verified under the shared submission lock before every
    continuous reservation and must bind the exact historical launch attempt,
    chain receipt, and rewarded-set evidence checkpoint. A second, short-lived,
    root-controlled signed artifact must explicitly authorize recurring writes
    for this release, signer, chain, lane, and bounded attempt count. The
    returned immutable authorization is stored in the reservation; the lowest
    write boundary rechecks its time/block scope without network work after
    pending state has been fsynced.
    """
    state = _read_state(_submission_state_path(args))
    if state.get("submission_pending_id") is not None:
        raise wire.VectorError(
            "continuous submission journal has an unresolved pending attempt"
        )
    if (
        state.get("submission_continuous_enabled") is not True
        or state.get("submission_launch_status") != "finalized"
        or state.get("submission_continuous_launch_attempt_id")
        != state.get("submission_launch_attempt_id")
    ):
        raise wire.VectorError(
            "continuous broadcast is locked until `cathedral-validator "
            "reconcile-launch` independently verifies the finalized "
            "rewarded-set-gated "
            "launch"
        )
    identity = state.get("submission_launch_identity")
    if not isinstance(identity, dict):
        raise wire.VectorError("continuous launch identity is missing")
    try:
        from scaffold import sn39_public_reproduction

        public_result = sn39_public_reproduction.verify_public_release()
        public_seal = _match_signed_public_release_to_launch(
            public_result=public_result,
            state=state,
            identity=identity,
        )
    except wire.VectorError:
        raise
    except Exception as exc:
        raise wire.VectorError(
            "continuous broadcast could not reproduce the root-signed public "
            "launch evidence"
        ) from exc
    if (
        state.get("submission_continuous_release_sha256")
        != public_seal["release_sha256"]
        or state.get("submission_continuous_reproducer_revision")
        != public_seal["reproducer_revision"]
    ):
        raise wire.VectorError(
            "continuous journal does not match the reproduced root-signed "
            "launch authorization"
        )
    validator_hotkey = getattr(args, "_submission_validator_hotkey", None)
    genesis_hash = getattr(args, "_submission_genesis_hash", None)
    if (
        not isinstance(validator_hotkey, str)
        or not validator_hotkey
        or not isinstance(genesis_hash, str)
        or _CHAIN_HASH_RE.fullmatch(genesis_hash) is None
        or state.get("submission_validator_hotkey") != validator_hotkey
        or state.get("submission_genesis_hash") != genesis_hash
    ):
        raise wire.VectorError(
            "continuous launch authorization differs from the prepared signer "
            "or chain genesis"
        )
    attempt_id = state.get("submission_launch_attempt_id")
    if (
        not isinstance(attempt_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
    ):
        raise wire.VectorError("continuous launch attempt identity is malformed")
    preflight = getattr(args, "_tick_preflight", None)
    if (
        not isinstance(preflight, ChainPreflight)
        or preflight.block is None
        or preflight.validator_hotkey != validator_hotkey
        or preflight.genesis_hash != genesis_hash
    ):
        raise wire.VectorError(
            "recurring authorization requires the current finalized signer snapshot"
        )
    lane = (
        "authority"
        if (getattr(args, "provenance", "shadow") or "shadow") == "authority"
        else "thin"
    )
    try:
        from scaffold import sn39_continuous_authorization as recurring

        verified = recurring.verify_authorization(
            expected={
                "submission_journal": str(_submission_state_path(args)),
                "genesis_hash": genesis_hash,
                "validator_hotkey": validator_hotkey,
                "launch_attempt_id": attempt_id,
                "release_sha256": public_seal["release_sha256"],
                "reproducer_revision": str(public_seal["reproducer_revision"]),
                "not_before_time": state.get("submission_continuous_enabled_at"),
            },
            lane=lane,
            finalized_block=preflight.block,
        )
    except Exception as exc:
        raise wire.VectorError(
            "separate root-signed recurring-write authorization is absent, "
            "invalid, expired, exhausted in scope, or does not match this "
            "release/runtime"
        ) from exc
    return ContinuousAuthorization(
        authorization_sha256=verified.authorization_sha256,
        submission_journal=verified.submission_journal,
        launch_attempt_id=attempt_id,
        release_sha256=public_seal["release_sha256"],
        reproducer_revision=str(public_seal["reproducer_revision"]),
        validator_hotkey=validator_hotkey,
        genesis_hash=genesis_hash,
        lanes=verified.lanes,
        issued_at=verified.issued_at,
        valid_from_time=verified.valid_from_time,
        valid_until_time=verified.valid_until_time,
        valid_from_block=verified.valid_from_block,
        valid_until_block=verified.valid_until_block,
        valid_from_nonce=verified.valid_from_nonce,
        valid_until_nonce_exclusive=verified.valid_until_nonce_exclusive,
        max_attempts=verified.max_attempts,
    )


def _sn39_launch_lineage(args: Any) -> bool | None:
    """Report whether this runtime's own journal records an SN39 launch.

    Returns None when the canonical signer/genesis identity is not resolved
    yet. The journal is addressed BY that identity, so before it is bound the
    question is unanswerable rather than answered "no". Every boundary that can
    actually reach an irreversible chain call resolves the identity first
    (`_prepare_tick_preflight`/`_bind_submission_identity`), so a runtime that
    has launched can never slip past the gate on an unresolved probe.
    """
    if not bool(getattr(args, "offline", False)) and (
        getattr(args, "_submission_validator_hotkey", None) is None
        or getattr(args, "_submission_genesis_hash", None) is None
    ):
        return None
    try:
        journal = _submission_state_path(args)
    except Exception:  # noqa: BLE001 - identity or root not usable here
        return None
    try:
        os.stat(journal.parent)
    except (FileNotFoundError, NotADirectoryError):
        # No runtime root yet means no journal and therefore no launch history.
        # The root itself is pinned to its canonical location separately, so
        # this cannot be used to point the probe somewhere convenient.
        return False
    except OSError:
        return True
    try:
        state = _read_state_without_mutation(journal)
    except Exception:  # noqa: BLE001
        # An unreadable, foreign-owned, or malformed journal must never read as
        # "never launched"; that is exactly how a launcher would hide its own
        # obligation. Fail closed.
        return True
    # Every marker below is written only under a launch reservation. The
    # per-reservation `submission_pending_launch_attempt` is a bool on ordinary
    # thin writes too, so it is compared against True rather than None: a relay
    # that has already reserved once must not read as launch lineage and brick
    # itself on its second tick.
    return bool(
        state.get("submission_launch_status") is not None
        or state.get("submission_launch_attempt_id") is not None
        or state.get("submission_launch_attempt_ids")
        or state.get("submission_continuous_enabled") is True
        or state.get("submission_continuous_launch_attempt_id") is not None
        or state.get("submission_pending_launch_attempt") is True
    )


def _sn39_launch_obligation(args: Any) -> bool:
    """Does THIS runtime owe SN39 its own completed one-shot launch?

    The mainnet launch is a subnet-level event, not a per-validator one. A
    third-party validator that only relays Cathedral's signed vector can never
    satisfy a per-validator launch gate, so an unconditional gate locks every
    operator except Cathedral out of SN39 entirely. The obligation therefore
    tracks the things an operator cannot simply restate in a config file:

      1. the authority code path, which submits its own recomputation instead
         of relaying the signed vector, and so originates trust rather than
         carrying it;
      2. the launch runtime itself, which performs the one-shot transaction;
      3. possession of, or journalled lineage from, the controlled launch
         material at the release-pinned absolute paths.

    Every branch reads code constants or this runtime's own durable journal, so
    no config value, CLI flag, endpoint label, or environment variable can
    clear an obligation the runtime actually has. Declaring `provenance =
    "shadow"` to dodge branch 1 also gives up the ability to originate weights,
    which is the capability the gate exists to protect.
    """
    if (getattr(args, "provenance", "shadow") or "shadow") == "authority":
        return True
    if bool(getattr(args, "require_full_provenance_for_broadcast", False)):
        return True
    for path in (
        SN39_LAUNCH_CONTROLLED_DIR,
        SN39_LAUNCH_VERIFIER_BINARY,
        SN39_LAUNCH_APPROVAL_FILE,
    ):
        try:
            os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            # A host that cannot answer the question is treated as holding the
            # launch material; refusing the broadcast is the safe direction.
            return True
        return True
    return _sn39_launch_lineage(args) is True


def _continuous_transition_required(args: Any) -> bool:
    if (
        bool(getattr(args, "broadcast", False))
        and not bool(getattr(args, "offline", False))
        and int(getattr(args, "netuid", -1)) == 39
        and _sn39_launch_obligation(args)
    ):
        # No operator-controlled label, endpoint, config, or direct CLI
        # invocation may weaken the SN39 transition requirement for a runtime
        # that originates weights or holds/has held launch material.
        return True
    explicit = getattr(args, "require_completed_launch_for_broadcast", None)
    if explicit is not None:
        return bool(explicit)
    return getattr(args, "require_policy", None) == "validated_supply_v1"


@contextlib.contextmanager
def _submission_tick_lock(args: Any, *, lane: str):
    """One non-blocking cross-process submission section for every mode.

    Thin and FULL-authority processes contend on the same file, so only one can
    reach an irreversible chain call. Shadow audit work remains concurrent
    because it never enters a submission tick on its own.
    """
    import fcntl

    lock_path = _submission_lock_path(args)
    lock_directory = _open_private_state_dir(lock_path.parent)
    os.close(lock_directory)
    boundary = "audit or submission" if lane == "authority" else "fetch or submission"
    try:
        descriptor = _open_private_lock(lock_path)
    except (OSError, ValueError) as exc:
        raise wire.VectorError(
            f"{lane} submission lock unavailable ({stable_error(exc)}); "
            f"refusing before {boundary}"
        ) from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise wire.VectorError(
                f"{lane} submission lock is unavailable or already held for "
                f"this validator/chain identity; refusing before {boundary} "
                "(cross-mode linearized single-flight)"
            ) from exc
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _thin_tick_lock(args: Any):
    with _submission_tick_lock(args, lane="thin"):
        yield


@contextlib.contextmanager
def _pending_recovery_tick_lock(args: Any):
    """Classify lock acquisition/cleanup failure as unavailable proof.

    Exceptions raised by the recovery body pass through unchanged so durable
    contradictions can still be classified as positive failures below.
    """
    entered = False
    body_completed = False
    try:
        with _thin_tick_lock(args):
            entered = True
            yield
            body_completed = True
    except (_PostSignedSubmissionMismatch, _PendingReceiptNotProven):
        raise
    except Exception as exc:
        if entered and not body_completed:
            raise
        raise _PendingReceiptNotProven(
            "pending receipt recovery lock is temporarily unavailable"
        ) from exc


@contextlib.contextmanager
def _authority_tick_lock(args: Any):
    with _submission_tick_lock(args, lane="authority"):
        yield


def _authority_tick(args, payload: dict[str, Any] | None) -> bool:
    """Full-authority tick, linearized: the entire audit→reserve→submit
    sequence runs inside ONE cross-process critical section per state file,
    so no interleaving of concurrent authority ticks can put stale weights
    on-chain after newer ones."""
    if (
        not bool(getattr(args, "offline", False))
        and getattr(args, "_tick_preflight", None) is None
    ):
        _prepare_tick_preflight(args)
    with _authority_tick_lock(args):
        args._continuous_submission_authorization = None
        if bool(getattr(args, "broadcast", False)) and _continuous_transition_required(
            args
        ):
            args._continuous_submission_authorization = (
                _require_continuous_launch_transition(args)
            )
            _prepare_tick_preflight(args)
        return _authority_tick_locked(args, payload)


def _revalidate_authority_after_audit(
    args: Any,
    *,
    audit: Any,
    recomputed: dict[str, float],
) -> tuple[ChainPreflight, dict[str, int], dict[int, float], InclusionPolicy]:
    """Refresh every mutable chain input after the potentially slow FULL audit."""
    fresh = chain_preflight(
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
    )
    _validate_resolved_chain_contract(args, fresh)
    _bind_submission_identity(args, fresh)
    if fresh.block is None:
        raise wire.VectorError(
            "fresh authority preflight has no finalized block after audit"
        )
    uid_weights = _provenance_uid_weights(
        recomputed,
        mechanism=getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
        or MECHANISM_DEFAULT,
        burn_hotkey=getattr(args, "provenance_burn_hotkey", None),
        hotkey_to_uid=fresh.hotkey_to_uid,
    )
    burn_uid = fresh.hotkey_to_uid.get(getattr(args, "provenance_burn_hotkey", None))
    _require_no_validator_compute_reward(
        uid_weights,
        preflight=fresh,
        burn_uid=burn_uid,
    )
    _validate_chain_constraints(uid_weights, fresh)
    inclusion_policy = _authority_inclusion_policy(audit, fresh)
    _require_uid_mapping_stability(
        fresh,
        {
            uid: hotkey
            for hotkey, uid in fresh.hotkey_to_uid.items()
            if uid in uid_weights
        },
        mortal_period_blocks=inclusion_policy.mortal_period_blocks,
    )
    args._tick_preflight = fresh
    return fresh, fresh.hotkey_to_uid, uid_weights, inclusion_policy


def _authority_tick_locked(args, payload: dict[str, Any] | None) -> bool:
    """Full-authority tick body: audit the published evidence, recompute, and
    submit the independently derived UID vector (fixed mechanism burn to the
    configured destination). The signed Cathedral vector, when reachable and
    valid, is used for comparison inside the audit only — it is never
    UID-mapped and never a precondition. The chain snapshot is taken FIRST
    so its finalized block anchors the report validity window. Callers MUST
    hold the authority tick lock (see _authority_tick)."""
    comparison = None
    if payload is not None:
        try:
            wire.verify_signature(
                payload,
                public_key_hex=args.public_key_hex,
                expected_key_id=args.key_id,
            )
            comparison = payload
        except Exception as exc:  # noqa: BLE001 - comparison-only input
            _lifecycle("FEED invalid", f"reason={type(exc).__name__}")

    current_block: int | None = None
    preflight = None
    if args.offline:
        broadcast = False
        hk2uid: dict[str, int] = {}
    else:
        broadcast = args.broadcast
        preflight = getattr(args, "_tick_preflight", None)
        if preflight is None:
            preflight = chain_preflight(
                network=args.network,
                netuid=args.netuid,
                wallet_name=args.wallet_name,
                wallet_hotkey=args.wallet_hotkey,
            )
            _bind_submission_identity(args, preflight)
        hk2uid = preflight.hotkey_to_uid
        current_block = preflight.block

    # A missing or malformed metagraph block must never degrade to
    # current_block=None (which silently skips the report block-validity
    # check inside the audit): refuse BEFORE audit and BEFORE submission.
    if not args.offline and current_block is None:
        raise wire.VectorError(
            "authority requires a finalized integer metagraph block to anchor "
            "the report validity window; the chain snapshot did not provide "
            "one (refusing before audit or submission)"
        )

    _, recomputed = _run_provenance_stage(
        args,
        comparison if comparison is not None else {},
        Path(args.state_file),
        current_block=current_block,
        historical_hotkeys_lookup=(
            None
            if args.offline
            else _historical_metagraph_lookup(args.network, args.netuid)
        ),
        block_hash_lookup=(None if args.offline else _block_hash_lookup(args.network)),
    )
    if args.offline:
        hk2uid = {hotkey: index for index, hotkey in enumerate(sorted(recomputed))}
        hk2uid.setdefault(
            getattr(args, "provenance_burn_hotkey", None) or "", len(hk2uid)
        )
    uid_weights = _provenance_uid_weights(
        recomputed,
        mechanism=getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
        or MECHANISM_DEFAULT,
        burn_hotkey=getattr(args, "provenance_burn_hotkey", None),
        hotkey_to_uid=hk2uid,
    )
    inclusion_policy: InclusionPolicy | None = None
    if broadcast:
        authority_audit = getattr(args, "_authority_full_audit", None)
        if (
            getattr(authority_audit, "status", None) != PASS
            or getattr(authority_audit, "assurance", None) != "full"
        ):
            raise wire.VectorError(
                "authority submission has no current FULL provenance audit"
            )
        preflight, hk2uid, uid_weights, inclusion_policy = (
            _revalidate_authority_after_audit(
                args,
                audit=authority_audit,
                recomputed=recomputed,
            )
        )
        current_block = preflight.block
    ordered = sorted(uid_weights.items())
    preview = ",".join(f"{uid}:{weight:.6f}" for uid, weight in ordered[:12])
    _lifecycle(
        "AUTHORITY provenance",
        f"independently derived vector ({len(recomputed)} verified miners) "
        f"block={current_block} vector={preview}",
    )
    state_file = Path(args.state_file)
    attempt_id: str | None = None
    if broadcast:
        authority_audit = getattr(args, "_authority_full_audit", None)
        if inclusion_policy is None:
            raise wire.VectorError(
                "authority submission has no fresh post-audit inclusion policy"
            )
        # Reserve an exact attempt BEFORE the irreversible chain call. A crash,
        # RPC ambiguity, telemetry failure, or merely advancing to a later
        # metagraph block can therefore never cause an automatic duplicate
        # submission. The durable attempt ID is derived from the evidence,
        # independently recomputed hotkey allocation, resolved UID allocation,
        # mechanism, burn destination, and validator identity. The mapping
        # block remains in the stored exact submission identity for public
        # proof, but is deliberately excluded from the deduplication identity:
        # a new block with the same mapping is not new work. A genuinely new
        # report or allocation gets a different attempt ID and remains eligible.
        reserved = _read_state(state_file)
        mechanism = (
            getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
            or MECHANISM_DEFAULT
        )
        burn_hotkey = getattr(args, "provenance_burn_hotkey", None)
        identity = {
            "network": args.network,
            "netuid": args.netuid,
            "mapping_block": current_block,
            "validator_hotkey": preflight.validator_hotkey,
            "validator_uid": preflight.validator_uid,
            "source_epoch": reserved.get("provenance_last_source_epoch"),
            "report_id": reserved.get("provenance_last_report_id"),
            "index_epoch": reserved.get("provenance_index_epoch"),
            "index_manifest": reserved.get("provenance_index_manifest"),
            "policy_release": reserved.get("provenance_policy_release"),
            "policy_digest": reserved.get("provenance_policy_digest"),
            "mechanism": mechanism,
            "burn_hotkey": burn_hotkey,
            "hotkey_weights": [
                [hotkey, recomputed[hotkey]] for hotkey in sorted(recomputed)
            ],
            "uid_weights": [[uid, weight] for uid, weight in ordered],
            "uid_hotkeys": [
                [uid, hotkey]
                for hotkey, uid in sorted(hk2uid.items(), key=lambda item: item[1])
                if uid in uid_weights
            ],
            "next_epoch_start_block": (preflight.next_epoch_start_block),
            "inclusion_policy": _inclusion_policy_identity(inclusion_policy),
            "uid_safety": _require_uid_mapping_stability(
                preflight,
                {
                    uid: hotkey
                    for hotkey, uid in preflight.hotkey_to_uid.items()
                    if uid in uid_weights
                },
                mortal_period_blocks=inclusion_policy.mortal_period_blocks,
            ),
        }
        continuous_authorization = getattr(
            args, "_continuous_submission_authorization", None
        )
        if _continuous_transition_required(args):
            if not isinstance(continuous_authorization, ContinuousAuthorization):
                raise wire.VectorError(
                    "authority submission lacks pre-reservation continuous "
                    "authorization"
                )
            identity["continuous_authorization"] = _continuous_authorization_identity(
                continuous_authorization
            )
        required_identity = (
            "source_epoch",
            "report_id",
            "index_epoch",
            "index_manifest",
            "policy_release",
            "policy_digest",
        )
        if any(identity.get(key) is None for key in required_identity):
            raise wire.VectorError(
                "authority reservation lacks a complete evidence identity; "
                "refusing before submission"
            )
        dedup_identity = {
            key: value
            for key, value in identity.items()
            if key not in {"mapping_block", "uid_safety"}
        }
        attempt_bytes = json.dumps(
            dedup_identity,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        attempt_id = "sha256:" + hashlib.sha256(attempt_bytes).hexdigest()
        try:
            _reserve_common_submission(
                args,
                lane="authority",
                attempt_id=attempt_id,
                identity=identity,
            )
        except (ValueError, OSError) as exc:
            raise wire.VectorError(
                "authority submission attempt fence refused before chain write: "
                f"{stable_error(exc)}"
            ) from exc
    submission = set_weights_on_chain(
        uid_weights,
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        broadcast=broadcast,
        preflight=preflight,
        uid_hotkeys=(
            {uid: hotkey for hotkey, uid in hk2uid.items() if uid in uid_weights}
            if not args.offline
            else None
        ),
        inclusion_policy=inclusion_policy,
        runtime_contract=args,
    )
    if broadcast:
        submission = _require_release_grade_submission(submission)
    ok = bool(submission)
    if ok and broadcast:
        # The common journal is canonical for crash recovery. Finalize it before
        # the lane-local telemetry file so a crash cannot strand a proven
        # finalized extrinsic behind an unrecoverable common pending fence.
        _finalize_common_submission(
            args,
            attempt_id=attempt_id,
            submission=submission,
        )
        _write_state_fenced(
            state_file,
            {
                "authority_submission_attempt_id": attempt_id,
                "authority_submission_attempt_status": "finalized",
                "authority_submission_attempted_at": _ms_iso_now(),
                "authority_submission_identity": identity,
                "authority_submission_dedup_identity": dedup_identity,
                "authority_submission_finalized_id": attempt_id,
                "authority_submission_finalized_at": _ms_iso_now(),
                "authority_submission_extrinsic_hash": getattr(
                    submission, "extrinsic_hash", None
                ),
                "authority_submission_block_hash": getattr(
                    submission, "block_hash", None
                ),
                "authority_submission_block_number": getattr(
                    submission, "block_number", None
                ),
            },
        )
    _get_events(args).event(
        "WEIGHTS_SUBMITTED" if (ok and broadcast) else "WEIGHTS_DRY_RUN",
        stage="submit",
        status=PASS if ok else FAIL,
        detail=(
            f"authority=full_provenance uids={len(ordered)} "
            f"block={current_block} vector={preview}"
        ),
        authority="full_provenance",
        uid_count=len(ordered),
        wire_uids=(
            _wire_weights(
                [uid for uid, _weight in ordered],
                [weight for _uid, weight in ordered],
            )[0]
            if not args.offline
            else None
        ),
        wire_weights=(
            _wire_weights(
                [uid for uid, _weight in ordered],
                [weight for _uid, weight in ordered],
            )[1]
            if not args.offline
            else None
        ),
        version_key=_weight_version_key() if not args.offline else None,
    )
    return ok


def _drain_shadow_audit_once(args) -> bool:
    """--once only: the recurring loop reports a finished shadow audit on
    the NEXT tick, which a single run never has. Wait out the in-flight
    audit within its own documented total bound (the audit deadline),
    report every completed result exactly once, and return False — a
    truthful nonzero exit — when the outcome could not be captured.
    Recurring thin ticks never call this; their non-blocking single-flight
    drain is unchanged."""
    auditor = getattr(args, "_shadow_auditor", None)
    if auditor is None:
        return True
    bound = _provenance_settings(args).audit_deadline_secs
    resolved = auditor.wait(bound)
    completed = auditor.drain()
    healthy = bool(completed)
    for finished_audit, finished_state_file in completed:
        persisted = _log_audit_events(args, finished_audit, finished_state_file)
        healthy = healthy and (
            persisted
            and finished_audit.status == "PASS"
            and getattr(finished_audit, "assurance", "receipts_only") == "full"
            and finished_audit.agrees_with_vector is True
        )
    if not resolved:
        _get_events(args).event(
            "PROVENANCE_AUDIT_UNRESOLVED",
            stage="provenance",
            status=NOT_PROVEN,
            detail=(
                f"single-run shadow audit still in flight after its "
                f"{bound:.0f}s bound; its outcome was not captured"
            ),
            remediation=(
                "re-run, extend the audit deadline, or check the evidence "
                "endpoint; the thin submission itself was unaffected"
            ),
        )
        return False
    if not healthy:
        _get_events(args).event(
            "PROVENANCE_HEALTH_GATE_FAILED",
            stage="provenance",
            status=FAIL,
            detail=(
                "single-run shadow audit did not establish FULL assurance "
                "and exact agreement with the signed vector"
            ),
            remediation=(
                "inspect the preceding provenance verdict; do not treat this "
                "one-shot current-health run as launch-ready"
            ),
        )
        return False
    return True


def _validate_runtime_contract(args: Any) -> None:
    max_submissions = int(getattr(args, "max_submissions", 0) or 0)
    if max_submissions < 0:
        raise wire.VectorError("max_submissions must be nonnegative")
    launch_gate = bool(getattr(args, "require_full_provenance_for_broadcast", False))
    launch_preflight = bool(getattr(args, "launch_preflight", False))
    sn39_broadcast = (
        bool(getattr(args, "broadcast", False))
        and not bool(getattr(args, "offline", False))
        and int(getattr(args, "netuid", -1)) == 39
    )
    sn39_launch_profile = (
        int(getattr(args, "netuid", -1)) == 39
        and not bool(getattr(args, "offline", False))
        and (sn39_broadcast or launch_preflight)
    )
    if sn39_launch_profile:
        pinned = {
            "network": "finney",
            "publisher_url": SN39_PUBLISHER_URL,
            "public_key_hex": DEFAULT_PUBLIC_KEY_HEX,
            "key_id": SN39_WEIGHT_POLICY_KEY_ID,
            "require_policy": "validated_supply_v1",
            "evidence_url": SN39_EVIDENCE_URL,
            "provenance_registry_keys_digest": SN39_REGISTRY_KEYS_DIGEST,
            "provenance_report_keys_digest": SN39_REPORT_KEYS_DIGEST,
            "provenance_index_keys_digest": SN39_INDEX_KEYS_DIGEST,
            "provenance_verifier_digest": SN39_VERIFIER_DIGEST,
            "provenance_source_revision": SN39_PRODUCER_REVISION,
            "provenance_mechanism": MECHANISM_DEFAULT,
            "provenance_burn_hotkey": SN39_BURN_HOTKEY,
        }
        mismatches = [
            name
            for name, expected in pinned.items()
            if (
                str(getattr(args, name, "")).strip().lower()
                if name == "network"
                else getattr(args, name, None)
            )
            != expected
        ]
        if Path(str(getattr(args, "state_file", ""))) != SN39_STATE_FILE:
            mismatches.append("state_file")
        provenance_mode = getattr(args, "provenance", "shadow") or "shadow"
        if provenance_mode not in {"shadow", "authority"}:
            mismatches.append("provenance")
        if mismatches:
            raise wire.VectorError(
                "SN39 mainnet broadcast differs from the immutable trust "
                f"profile: {', '.join(sorted(set(mismatches)))}"
            )
        runtime_root = _submission_runtime_root(args)
        if runtime_root != _VALIDATOR_RUNTIME_ROOT:
            raise wire.VectorError(
                "SN39 mainnet broadcast requires the canonical owner-only "
                f"runtime root {_VALIDATOR_RUNTIME_ROOT}"
            )
        # A pure relay carries Cathedral's signature to the pinned netuid and
        # burn; it has no launch of its own to complete, so demanding the gate
        # from it would only lock third parties off the subnet. A runtime that
        # originates weights or holds launch material still must set it.
        if (
            not launch_gate
            and _sn39_launch_obligation(args)
            and not bool(getattr(args, "require_completed_launch_for_broadcast", False))
        ):
            raise wire.VectorError(
                "SN39 broadcast from a weight-originating or launch-capable "
                "runtime requires the completed-launch gate"
            )
    if not launch_gate:
        return
    missing = [
        name
        for name in (
            "provenance_controlled_dir",
            "provenance_verifier_binary",
            "provenance_burn_hotkey",
        )
        if not getattr(args, name, None)
    ]
    launch_paths_match = (
        Path(str(getattr(args, "provenance_controlled_dir", "")))
        == SN39_LAUNCH_CONTROLLED_DIR
        and Path(str(getattr(args, "provenance_verifier_binary", "")))
        == SN39_LAUNCH_VERIFIER_BINARY
    )
    approval_path_match = (
        Path(str(getattr(args, "launch_approval_file", "")))
        == SN39_LAUNCH_APPROVAL_FILE
    )
    launch_action_matches = (
        launch_preflight
        and not bool(getattr(args, "broadcast", False))
        or not launch_preflight
        and bool(getattr(args, "broadcast", False))
    )
    if (
        bool(getattr(args, "offline", False))
        or not bool(getattr(args, "once", False))
        or max_submissions != 1
        or (getattr(args, "provenance", "shadow") or "shadow") != "shadow"
        or not launch_paths_match
        or not approval_path_match
        or not launch_action_matches
        or missing
    ):
        suffix = f"; missing {', '.join(missing)}" if missing else ""
        raise wire.VectorError(
            "launch full-provenance mode requires exactly one online action "
            "(read-only preflight or approved broadcast), --once, "
            "provenance=shadow, max_submissions=1, controlled evidence, verifier "
            f"binary, immutable launch/approval paths, and burn hotkey{suffix}"
        )
    _launch_release_config_identity(args)


def run(args) -> int:
    """The validator loop, shared by `python -m scaffold.validator_thin` and the
    `cathedral-validator serve` console command. `args` is any object carrying
    the tick attributes (an argparse Namespace or a SimpleNamespace from the
    CLI's config loader)."""
    _validate_runtime_contract(args)
    if bool(getattr(args, "require_full_provenance_for_broadcast", False)):
        if bool(getattr(args, "launch_preflight", False)):
            raise wire.VectorError(
                "read-only launch preflight must use run_launch_preflight"
            )
        args._launch_approval = _load_launch_approval(args)
    require_policy = getattr(args, "require_policy", None)
    if require_policy:
        _lifecycle("PIN active", f"policy={require_policy}")
    submission_authority, provenance_mode = _runtime_modes(args)
    _lifecycle(
        "MODE active",
        f"submission_authority={submission_authority} provenance={provenance_mode} "
        + (
            "(thin submits; provenance audits concurrently; FULL requires "
            "controlled raw evidence)"
            if provenance_mode == "shadow"
            else "(independent recomputation submits)"
            if provenance_mode == "authority"
            else "(thin only; no provenance audit)"
        ),
    )
    _get_events(args).event(
        "STARTUP",
        stage="startup",
        status=INFO,
        detail=(
            f"submission_authority={submission_authority} "
            f"provenance={provenance_mode} policy_pin={require_policy or 'none'} "
            f"network={args.network} netuid={args.netuid}"
        ),
        authority=submission_authority,
        provenance_mode=provenance_mode,
        network=args.network,
        netuid=int(args.netuid),
        publisher_url=_safe_endpoint_label(args.publisher_url),
        weight_policy_public_key=getattr(args, "public_key_hex", None),
        weight_policy_key_id=getattr(args, "key_id", None),
        policy_pin=require_policy,
        provenance_evidence_url=_safe_endpoint_label(
            getattr(args, "evidence_url", None)
        ),
        provenance_registry_keys_digest=getattr(
            args, "provenance_registry_keys_digest", None
        ),
        provenance_report_keys_digest=getattr(
            args, "provenance_report_keys_digest", None
        ),
        provenance_index_keys_digest=getattr(
            args, "provenance_index_keys_digest", None
        ),
        provenance_verifier_digest=getattr(args, "provenance_verifier_digest", None),
        provenance_source_revision=getattr(args, "provenance_source_revision", None),
        provenance_mechanism=getattr(args, "provenance_mechanism", None),
        max_submissions=int(getattr(args, "max_submissions", 0) or 0),
        launch_full_gate=bool(
            getattr(args, "require_full_provenance_for_broadcast", False)
        ),
    )
    try:
        recovered = _recover_pending_launch_receipt(args)
    except _PostSignedSubmissionMismatch as exc:
        print(f"pending receipt contradiction: {stable_error(exc)}")
        _get_events(args).event(
            "PENDING_RECEIPT_CONTRADICTION",
            stage="result",
            status=FAIL,
            detail=str(exc)[:512],
            remediation=(
                "The exact signed attempt has a positive durable or historical "
                "contradiction. Keep every writer stopped and inspect the journal "
                "and named transaction; never submit a replacement."
            ),
        )
        return 1
    except _PendingReceiptNotProven as exc:
        print(f"pending receipt not proven: {stable_error(exc)}")
        _get_events(args).event(
            "PENDING_RECEIPT_NOT_PROVEN",
            stage="result",
            status=NOT_PROVEN,
            detail=str(exc)[:512],
            remediation=(
                "The exact signed attempt remains fenced. Wait for archive/RPC "
                "proof and restart to re-prove it; never submit a replacement."
            ),
        )
        return 1
    if recovered:
        event_fields: dict[str, Any] = {
            "stage": "result",
            "status": PASS,
            "detail": recovered.boundary_detail,
            "authority": (
                "full_provenance"
                if isinstance(recovered, RecoveredAuthoritySubmission)
                else "thin"
            ),
            "uid_count": len(recovered.uid_weights),
            "burn_uid": recovered.burn_uid,
            "burn_share": recovered.burn_share,
            "uid_weights": {str(uid): weight for uid, weight in recovered.uid_weights},
            "extrinsic_hash": recovered.extrinsic_hash,
            "block_hash": recovered.block_hash,
            "block_number": recovered.block_number,
        }
        if isinstance(recovered, RecoveredAuthoritySubmission):
            event_fields.update(
                {
                    "source_epoch": recovered.source_epoch,
                    "report_id": recovered.report_id,
                }
            )
        else:
            event_fields.update(
                {
                    "vector_id": recovered.vector_id,
                    "policy_version": recovered.policy_version,
                    "signed_vector_sha256": recovered.signed_vector_sha256,
                }
            )
        _get_events(args).event("PENDING_RECEIPT_RECOVERED", **event_fields)
        if bool(getattr(args, "once", False)):
            return 0
    while True:
        tick_ok = False
        pre_sign_head_drift_retries = 0
        while True:
            try:
                tick_ok = tick(args)
                break
            except _RetryablePreSignHeadDrift as e:
                if pre_sign_head_drift_retries >= SN39_PRE_SIGN_HEAD_DRIFT_RETRIES:
                    print(f"pre-sign head drift retry exhausted: {stable_error(e)}")
                    _get_events(args).event(
                        "PRE_SIGN_HEAD_DRIFT_RETRY_EXHAUSTED",
                        stage="submit",
                        status=FAIL,
                        detail=str(e)[:512],
                        remediation=(
                            "No transaction was signed. Wait for the next normal "
                            "tick, which will rebuild every chain, authorization, "
                            "mapping, and evidence proof from a new finalized head."
                        ),
                    )
                    break
                pre_sign_head_drift_retries += 1
                print(f"pre-sign head drift retry: {stable_error(e)}")
                _get_events(args).event(
                    "PRE_SIGN_HEAD_DRIFT_RETRY",
                    stage="submit",
                    status=NOT_PROVEN,
                    detail=str(e)[:512],
                    retry=pre_sign_head_drift_retries,
                    retry_limit=SN39_PRE_SIGN_HEAD_DRIFT_RETRIES,
                    remediation=(
                        "The unsigned reservation was safely released. Rebuilding "
                        "the complete tick from a fresh finalized head now."
                    ),
                )
                # This exception can escape set_weights_on_chain only after its
                # fsynced common journal proved that no signed intent, receipt,
                # or broadcast exists. Calling tick again deliberately repeats
                # preflight, recurring authorization, UID mapping, provenance,
                # and inclusion-policy construction instead of reusing any
                # mutable proof from the aborted attempt.
                continue
            except _PostSignedSubmissionMismatch as e:
                print(f"pending receipt contradiction: {stable_error(e)}")
                _get_events(args).event(
                    "PENDING_RECEIPT_CONTRADICTION",
                    stage="result",
                    status=FAIL,
                    detail=str(e)[:512],
                    remediation=(
                        "The exact signed attempt has a positive durable or "
                        "historical contradiction. Keep every writer stopped and "
                        "inspect the journal and named transaction; never submit "
                        "a replacement."
                    ),
                )
                return 1
            except _PendingReceiptNotProven as e:
                print(f"pending receipt not proven: {stable_error(e)}")
                _get_events(args).event(
                    "PENDING_RECEIPT_NOT_PROVEN",
                    stage="result",
                    status=NOT_PROVEN,
                    detail=str(e)[:512],
                    remediation=(
                        "The exact signed attempt remains fenced. Wait for archive/RPC "
                        "proof and restart to re-prove it; never submit a replacement."
                    ),
                )
                # Restart enters the dedicated receipt-recovery path before any new
                # tick can reserve or sign another submission.
                return 1
            except Exception as e:  # noqa: BLE001 - loop resilience; sanitized below
                print(f"tick failed: {stable_error(e)}")
                _get_events(args).event(
                    "TICK_FAILED",
                    stage="result",
                    status=FAIL,
                    detail=str(e)[:512],
                    remediation=(
                        "The tick failed closed. If failure occurred after the "
                        "chain call, a write may have finalized; inspect the "
                        "durable attempt state and named extrinsic before operator "
                        "recovery. Automatic same-attempt retry remains blocked."
                    ),
                )
                break
        if args.once:
            # A single run exits only after the background shadow audit's
            # outcome is captured and reported (bounded); a tick that ran
            # but did not succeed — or an audit outcome that could not be
            # captured — is a FAILED single run.
            shadow_ok = _drain_shadow_audit_once(args)
            return 0 if (tick_ok and shadow_ok) else 1
        time.sleep(args.interval_secs)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cathedral thin validator (v4)")
    p.add_argument(
        "--publisher-url",
        default=os.environ.get(
            "CATHEDRAL_PUBLISHER_URL", "https://api.cathedral.computer"
        ),
    )
    p.add_argument(
        "--public-key-hex",
        default=os.environ.get(
            "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY", DEFAULT_PUBLIC_KEY_HEX
        ),
        help="pinned Ed25519 public key (hex); defaults to Cathedral's published key",
    )
    p.add_argument(
        "--key-id",
        default=os.environ.get(
            "CATHEDRAL_WEIGHT_POLICY_KEY_ID", "cathedral-weight-policy"
        ),
    )
    p.add_argument("--network", default="finney")
    p.add_argument(
        "--chain-endpoint",
        default=os.environ.get(CHAIN_ENDPOINT_ENV, ""),
        help="connect to your own subtensor RPC node (ws/wss URL) instead of the "
        "public entrypoint; the network label is kept for signing. "
        f"Defaults to ${CHAIN_ENDPOINT_ENV}.",
    )
    p.add_argument("--netuid", type=int, default=39)
    p.add_argument(
        "--wallet-name", default=os.environ.get("BT_WALLET_NAME", "validator")
    )
    p.add_argument(
        "--wallet-hotkey", default=os.environ.get("BT_WALLET_HOTKEY", "default")
    )
    p.add_argument(
        "--state-file",
        default=os.environ.get(
            "CATHEDRAL_VALIDATOR_STATE",
            str(Path.home() / ".cathedral" / "thin_validator.json"),
        ),
    )
    p.add_argument(
        "--runtime-root",
        default=os.environ.get(
            "CATHEDRAL_VALIDATOR_RUNTIME_ROOT",
            str(_VALIDATOR_RUNTIME_ROOT),
        ),
        help="absolute owner-only cross-mode lock and ambiguity-journal directory",
    )
    p.add_argument("--interval-secs", type=float, default=1500.0)
    p.add_argument(
        "--max-submissions",
        type=int,
        default=int(os.environ.get("CATHEDRAL_VALIDATOR_MAX_SUBMISSIONS", "0")),
        help="optional local durable-attempt ceiling; 0 disables this extra "
        "ceiling, but SN39 recurring writes still require a separately signed "
        "bounded authorization; launch canary requires 1",
    )
    p.add_argument("--once", action="store_true", help="single tick, then exit")
    p.add_argument(
        "--offline",
        action="store_true",
        help="no chain access: verify + print only (CI / smoke)",
    )
    p.add_argument(
        "--broadcast",
        action="store_true",
        help="actually submit weights (default: dry-run)",
    )
    p.add_argument(
        "--require-full-provenance-for-broadcast",
        action="store_true",
        default=os.environ.get(
            "CATHEDRAL_VALIDATOR_REQUIRE_FULL_PROVENANCE_FOR_BROADCAST", ""
        )
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
        help="launch-only: require synchronous FULL raw-evidence replay and exact "
        "vector agreement before the one permitted chain write",
    )
    p.add_argument(
        "--require-policy",
        dest="require_policy",
        default=os.environ.get("CATHEDRAL_VALIDATOR_REQUIRE_POLICY", "").strip()
        or REQUIRE_POLICY_VALIDATED_SUPPLY_V1,
        help="pin the validator to a signed policy contract. "
        "validated_supply_v1 locks the launch 90%% Intel TDX / "
        "10%% unadmitted GPU-to-burn allocation. "
        "Default: validated_supply_v1.",
    )
    p.add_argument(
        "--provenance",
        choices=("off", "shadow", "authority"),
        default=os.environ.get("CATHEDRAL_VALIDATOR_PROVENANCE", "shadow"),
        help="full-provenance mode: 'shadow' (default) audits the "
        "published evidence concurrently while thin mode submits; "
        "'authority' submits the independent recomputation; "
        "'off' disables the audit.",
    )
    p.add_argument(
        "--evidence-url",
        default=os.environ.get("CATHEDRAL_EVIDENCE_URL", "") or None,
        help="public evidence base URL (default: <publisher-url>/v1/evidence)",
    )
    p.add_argument(
        "--evidence-dir",
        default=None,
        help="local evidence store directory (testing/reproduction)",
    )
    p.add_argument(
        "--provenance-registry-keys",
        default=os.environ.get("CATHEDRAL_PROVENANCE_REGISTRY_KEYS") or None,
        help="trusted policy-registry key file (JSON key_id -> base64)",
    )
    p.add_argument(
        "--provenance-registry-keys-digest",
        default=os.environ.get("CATHEDRAL_PROVENANCE_REGISTRY_KEYS_DIGEST") or None,
    )
    p.add_argument(
        "--provenance-report-keys",
        default=os.environ.get("CATHEDRAL_PROVENANCE_REPORT_KEYS") or None,
        help="trusted score-report key file (JSON key_id -> base64)",
    )
    p.add_argument(
        "--provenance-report-keys-digest",
        default=os.environ.get("CATHEDRAL_PROVENANCE_REPORT_KEYS_DIGEST") or None,
    )
    p.add_argument(
        "--provenance-index-keys",
        default=os.environ.get("CATHEDRAL_PROVENANCE_INDEX_KEYS") or None,
        help="trusted evidence-index key file (JSON key_id -> base64)",
    )
    p.add_argument(
        "--provenance-index-keys-digest",
        default=os.environ.get("CATHEDRAL_PROVENANCE_INDEX_KEYS_DIGEST") or None,
    )
    p.add_argument(
        "--provenance-verifier-digest",
        default=os.environ.get("CATHEDRAL_PROVENANCE_VERIFIER_DIGEST") or None,
        help="pinned Intel TDX verifier implementation digest (sha256:<hex>)",
    )
    p.add_argument(
        "--provenance-mechanism",
        default=os.environ.get("CATHEDRAL_PROVENANCE_MECHANISM", MECHANISM_DEFAULT),
        help="pinned versioned reward mechanism (default validated_supply_v1)",
    )
    p.add_argument(
        "--provenance-controlled-dir",
        default=os.environ.get("CATHEDRAL_PROVENANCE_CONTROLLED_DIR") or None,
        help="controlled-disclosure envelope directory (enables FULL assurance)",
    )
    p.add_argument(
        "--provenance-verifier-binary",
        default=os.environ.get("CATHEDRAL_PROVENANCE_VERIFIER_BINARY") or None,
        help="local pinned verifier binary for raw-evidence replay",
    )
    p.add_argument(
        "--provenance-source-revision",
        default=os.environ.get("CATHEDRAL_PROVENANCE_SOURCE_REVISION") or None,
        help="independent pin of the expected manifest source revision",
    )
    p.add_argument(
        "--provenance-burn-hotkey",
        default=os.environ.get("CATHEDRAL_PROVENANCE_BURN_HOTKEY") or None,
        help="authority mode's configured burn destination hotkey (the fixed "
        "10%% mechanism burn goes here; never taken from Cathedral's "
        "signed vector)",
    )
    p.add_argument("--provenance-index-max-age-secs", type=float, default=3600.0)
    p.add_argument(
        "--provenance-allow-private-hosts",
        action="store_true",
        help="testing only: permit evidence hosts on private ranges",
    )
    p.add_argument(
        "--jsonl",
        default=os.environ.get("CATHEDRAL_VALIDATOR_JSONL") or None,
        help="append the stable JSONL event stream to this file",
    )
    return p


def main() -> int:
    p = build_parser()
    args = p.parse_args()
    if not args.public_key_hex:
        p.error(
            "--public-key-hex (or CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY) is required — "
            "validators must pin the orchestrator's signing key"
        )
    if args.require_policy and args.require_policy not in REQUIRE_POLICY_CHOICES:
        p.error(
            f"--require-policy (or CATHEDRAL_VALIDATOR_REQUIRE_POLICY) must be one of "
            f"{', '.join(REQUIRE_POLICY_CHOICES)}; got {args.require_policy!r}"
        )
    # --chain-endpoint populates the env the resolver reads, so both the
    # validator_thin path and the ChainClient path honor it from one source.
    if args.chain_endpoint:
        os.environ[CHAIN_ENDPOINT_ENV] = args.chain_endpoint
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
