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
SN39_PRODUCER_REVISION = "fa39af97e738fdbed5c454f976b61246590b5794"
SN39_BURN_HOTKEY = "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"
SN39_STATE_FILE = Path("/var/lib/cathedral-validator/thin-state.json")
SN39_LAUNCH_CONTROLLED_DIR = Path("/etc/cathedral/controlled/sn39-launch")
SN39_LAUNCH_VERIFIER_BINARY = Path("/opt/cathedral-sn39/bin/cathedral-tdx-verifier")
SN39_LAUNCH_VALIDATOR_UID = 30
SN39_LAUNCH_REWARDED_UID = 163
SN39_LAUNCH_BURN_UID = 204


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
            active_lane = current.get("submission_active_lane")
            if active_lane is not None and active_lane != lane:
                raise ValueError(
                    "submission authority lane changed without explicit operator "
                    f"reconciliation ({active_lane!r} -> {lane!r})"
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
                updates["submission_attempt_budgets"] = {
                    **budgets,
                    budget_scope: {
                        "limit": budget_limit,
                        "ids": [*budget["ids"], new_common_attempt],
                    },
                }
            if launch_attempt:
                configured_limit = updates.pop("_launch_budget_limit", None)
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
                updates["submission_launch_attempt_ids"] = [
                    *launch_history,
                    new_common_attempt,
                ]
                updates["submission_launch_budget_limit"] = configured_limit
                updates["submission_launch_status"] = "pending"
            policy_version = updates.get("submission_highest_policy_version")
            source_epoch = updates.get("submission_highest_source_epoch")
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
            updates["submission_attempt_ids"] = [
                *raw_common_history,
                new_common_attempt,
            ]
            updates["submission_active_lane"] = lane
            updates["submission_attempt_count"] = len(raw_common_history) + 1
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


def _get_events(args) -> EventLogger:
    """One logger per process; authority is stamped on every record."""
    existing = getattr(args, "_events", None)
    if existing is not None:
        return existing
    mode = getattr(args, "provenance", "shadow") or "shadow"
    authority = "full_provenance" if mode == "authority" else "thin"
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
MECHANISM_BURN_FRACTION = {MECHANISM_DEFAULT: 0.10}


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
        state=_read_state(state_file),
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
    _validate_resolved_chain_contract(args, fresh)
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
        fresh.validator_uid != SN39_LAUNCH_VALIDATOR_UID
        or fresh.hotkey_to_uid.get(fresh.validator_hotkey) != SN39_LAUNCH_VALIDATOR_UID
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
    if (
        rewarded_hotkey not in fresh.hotkey_to_uid
        or burn_hotkey != getattr(args, "provenance_burn_hotkey", None)
        or burn_hotkey not in fresh.hotkey_to_uid
        or fresh.hotkey_to_uid[rewarded_hotkey] != SN39_LAUNCH_REWARDED_UID
        or fresh.hotkey_to_uid[burn_hotkey] != SN39_LAUNCH_BURN_UID
        or set(uid_weights) != {SN39_LAUNCH_REWARDED_UID, SN39_LAUNCH_BURN_UID}
        or not math.isclose(
            uid_weights[SN39_LAUNCH_REWARDED_UID],
            0.90,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            uid_weights[SN39_LAUNCH_BURN_UID],
            0.10,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise wire.VectorError(
            "fresh launch allocation is not the immutable UID 163/204 90/10 "
            "release boundary"
        )
    args._launch_inclusion_policy = InclusionPolicy(
        valid_from_block=valid_from,
        valid_until_block=valid_until,
        valid_from_time=inclusion_start,
        valid_until_time=inclusion_expiry,
    )
    _require_inclusion_policy_ready(args._launch_inclusion_policy, fresh)
    return fresh, fresh.hotkey_to_uid, uid_weights


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


@dataclass(frozen=True)
class ChainSubmission:
    success: bool
    extrinsic_hash: str | None = None
    block_hash: str | None = None
    block_number: int | None = None
    finalized: bool = False

    def __bool__(self) -> bool:
        return self.success


CHAIN_OPERATION_DEADLINE_SECS = 180.0
# A write is refused unless its evidence remains valid beyond the entire
# synchronous SDK deadline plus an explicit clock/RPC margin. The mortal era
# is intentionally short and is also bounded by the evidence block window.
SN39_MIN_VALIDITY_MARGIN_SECS = 60.0
SN39_MORTAL_PERIOD_BLOCKS = 16


@dataclass(frozen=True)
class InclusionPolicy:
    """Policy facts that must still hold at the actual inclusion block."""

    valid_from_block: int
    valid_until_block: int
    valid_from_time: datetime
    valid_until_time: datetime
    require_commit_reveal_disabled: bool = True
    mortal_period_blocks: int = SN39_MORTAL_PERIOD_BLOCKS


@dataclass(frozen=True)
class ContinuousAuthorization:
    """Root-signed launch authorization proven before a durable reservation."""

    launch_attempt_id: str
    release_sha256: str
    reproducer_revision: str
    validator_hotkey: str
    genesis_hash: str


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
    }


def _continuous_authorization_identity(
    authorization: ContinuousAuthorization,
) -> dict[str, str]:
    return {
        "launch_attempt_id": authorization.launch_attempt_id,
        "release_sha256": authorization.release_sha256,
        "reproducer_revision": authorization.reproducer_revision,
        "validator_hotkey": authorization.validator_hotkey,
        "genesis_hash": authorization.genesis_hash,
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
    index = hotkeys.index(validator_hotkey)
    if not permits[index]:
        raise wire.VectorError("validator hotkey lacks validator permit")
    block = _finalized_block(getattr(metagraph, "block", None))
    if block != finalized_block:
        raise wire.VectorError("metagraph did not resolve at the finalized chain head")
    result = ChainPreflight(
        wallet=wallet,
        subtensor=subtensor,
        hotkey_to_uid=hotkey_to_uid,
        validator_hotkey=validator_hotkey,
        validator_uid=hotkey_to_uid[validator_hotkey],
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
    )
    _lifecycle(
        "PREFLIGHT complete",
        f"validator_hotkey={validator_hotkey} validator_uid={result.validator_uid} "
        f"block={block if block is not None else 'unknown'} "
        f"min_allowed={result.min_allowed_weights} max_limit={result.max_weight_limit} "
        f"commit_reveal={str(result.commit_reveal_enabled).lower()}",
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


def _prove_finalized_receipt(
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
    inclusion_policy: InclusionPolicy | None = None,
    require_receipt: bool = True,
) -> bool:
    """Prove finality and the exact included ``set_mechanism_weights`` call."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        return False
    if require_receipt and (
        receipt is None or getattr(receipt, "is_success", None) is not True
    ):
        return False
    try:
        finalized_hash = str(substrate.get_chain_finalised_head())
        finalized_number = int(substrate.get_block_number(finalized_hash))
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
                return False
            inclusion_time = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
        else:
            commit_reveal_at_inclusion = None
            inclusion_time = None
    except Exception:  # noqa: BLE001 - any archive/RPC fault is a failed proof
        return False
    matching = [
        item.value
        for item in block.get("extrinsics", ())
        if isinstance(getattr(item, "value", None), dict)
        and str(item.value.get("extrinsic_hash", "")).lower() == extrinsic_hash.lower()
    ]
    if len(matching) != 1:
        return False
    observed = matching[0]
    call = observed.get("call") or {}
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
            inclusion_bindings_ok = (
                len(inclusion_uids) == len(inclusion_hotkeys)
                and len(inclusion_map) == len(inclusion_uids)
                and _finalized_block(getattr(inclusion_metagraph, "block", None))
                == block_number
                and all(
                    inclusion_map.get(uid) == hotkey
                    for uid, hotkey in uid_hotkeys.items()
                )
            )
        except (AttributeError, TypeError, ValueError):
            inclusion_bindings_ok = False
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
    )
    return (
        finalized_number >= block_number
        and canonical_hash.lower() == block_hash.lower()
        and _CHAIN_HASH_RE.fullmatch(finalized_hash) is not None
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
    root-signed launch seal.
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
    if (
        not isinstance(attempt_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None
        or lane not in {"thin", "authority"}
        or state.get("submission_active_lane") != lane
        or not isinstance(identity, dict)
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
    if identity.get("inclusion_policy") != _inclusion_policy_identity(inclusion_policy):
        raise wire.VectorError(
            "SN39 inclusion policy differs from its durable reservation"
        )

    launch = bool(getattr(args, "require_full_provenance_for_broadcast", False))
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
        ):
            raise wire.VectorError(
                "SN39 continuous chain call lacks its pre-reservation "
                "root-signed launch authorization"
            )
        return
    audit = getattr(args, "_launch_rewarded_set_audit", None)
    full = identity.get("full_provenance")
    rewarded = set(getattr(audit, "recomputed", {}) or {})
    receipts = set(getattr(audit, "receipt_hotkeys", ()) or ())
    replayed = set(getattr(audit, "raw_replayed_hotkeys", ()) or ())
    if (
        lane != "thin"
        or state.get("submission_launch_status") != "pending"
        or state.get("submission_launch_budget_limit") != 1
        or state.get("submission_launch_attempt_ids") != [attempt_id]
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
    )
    if not all(full_matches_audit):
        raise wire.VectorError(
            "SN39 launch reservation does not match the synchronous rewarded-set "
            "raw replay"
        )


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
    try:
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
        wire_uids, wire_values = _wire_weights(uids, vals)
        if not broadcast:
            _lifecycle(
                "WEIGHTS dry-run",
                f"uids={len(ordered)} wire_uids={wire_uids} "
                f"wire_weights={wire_values} vector={preview}",
            )
            return ChainSubmission(success=True)
        with _chain_operation_deadline("weight submission", deadline_secs):
            # Keep the irreversible SDK primitive inside this authorized
            # function. There is no separately importable repository helper
            # that can bypass the SN39 runtime/state-machine checks above.
            from bittensor.core.extrinsics.weights import set_weights_extrinsic
            from bittensor.core.settings import version_as_int

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
            # Some SDK receipt properties are lazy. Materialize every field
            # while the same wall-clock bound is still active.
            ok = bool(getattr(resp, "success", resp))
            response_values: dict[str, Any] = {}
            receipt = getattr(resp, "extrinsic_receipt", None)
            for name in ("extrinsic_hash", "block_hash", "block_number"):
                value = getattr(receipt, name, None) or getattr(resp, name, None)
                if value is not None:
                    response_values[name] = value
            block_number = response_values.get("block_number")
            try:
                receipt_block_number = (
                    int(block_number) if block_number is not None else None
                )
            except (TypeError, ValueError):
                receipt_block_number = None
            receipt_block_hash = response_values.get("block_hash")
            receipt_extrinsic_hash = response_values.get("extrinsic_hash")
            finalized = bool(
                ok
                and receipt_block_number is not None
                and receipt_block_number > 0
                and isinstance(receipt_block_hash, str)
                and isinstance(receipt_extrinsic_hash, str)
                and _prove_finalized_receipt(
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
                    inclusion_policy=inclusion_policy,
                )
            )
    except Exception as exc:
        _lifecycle("CHAIN failed", f"uids={len(ordered)} reason={type(exc).__name__}")
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
        except wire.VectorError:
            _lifecycle(
                "CHAIN ambiguous",
                f"uids={len(ordered)} success=True receipt_identity=incomplete",
            )
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
        )
        preflight, hk2uid, uid_weights = _revalidate_launch_after_rewarded_set_replay(
            args,
            payload=payload,
            audit=launch_audit,
            fence_version=fence,
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
    burn_share = uid_weights.get(int(burn_uid), 0.0) if burn_uid is not None else 0.0
    _lifecycle(
        "MAP complete",
        f"uids={len(uid_weights)} burn_uid={burn_uid} burn_share={burn_share:.6f} "
        f"vector={preview}",
    )
    signed_vector_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    signed_vector_sha256 = "sha256:" + hashlib.sha256(signed_vector_bytes).hexdigest()
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
            "burn_hotkey": burn_hotkey,
            "uid_weights": [[uid, weight] for uid, weight in ordered],
            "uid_hotkeys": [
                [uid, hotkey]
                for hotkey, uid in sorted(hk2uid.items(), key=lambda item: item[1])
                if uid in uid_weights
            ],
            "inclusion_policy": _inclusion_policy_identity(inclusion_policy),
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
            identity["signed_vector"] = payload
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
            }
        dedup_identity = {
            key: value for key, value in identity.items() if key != "mapping_block"
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
            _write_state_fenced(
                state_file,
                {
                    "highest_attempted_policy_version": int(payload["policy_version"]),
                    "thin_submission_attempt_id": thin_attempt_id,
                    "thin_submission_attempt_status": "pending",
                    "thin_submission_attempted_at": _ms_iso_now(),
                    "thin_submission_identity": identity,
                    "thin_submission_dedup_identity": dedup_identity,
                },
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
    # fallible telemetry. If this write fails, the already-fsynced pending
    # attempt still blocks an unsafe automatic retry.
    if ok and broadcast:
        _write_state(
            state_file,
            {
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
                "last_accepted_policy_version": int(payload["policy_version"]),
                "last_vector_id": payload["vector_id"],
                "accepted_at": _ms_iso_now(),
            },
        )
        _finalize_common_submission(
            args,
            attempt_id=thin_attempt_id,
            submission=submission,
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


def _finalize_common_submission(
    args: Any,
    *,
    attempt_id: str,
    submission: ChainSubmission,
) -> None:
    launch_updates: dict[str, Any] = {}
    if bool(getattr(args, "require_full_provenance_for_broadcast", False)):
        pending = _read_state(_submission_state_path(args))
        launch_identity = pending.get("submission_pending_identity")
        if not isinstance(launch_identity, dict):
            raise ValueError("launch finalization has no exact pending identity")
        launch_updates = {
            "submission_launch_status": "finalized",
            "submission_launch_attempt_id": attempt_id,
            "submission_launch_identity": launch_identity,
            "submission_launch_extrinsic_hash": submission.extrinsic_hash,
            "submission_launch_block_hash": submission.block_hash,
            "submission_launch_block_number": submission.block_number,
            "submission_launch_version_key": _weight_version_key(),
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
            "submission_version_key": _weight_version_key(),
            **launch_updates,
        },
    )


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
        snapshot_hotkeys = [str(value) for value in snapshot["hotkeys"]]
        burn_uid = int(mapping["burn_uid"])
        burn_hotkey = snapshot_hotkeys[burn_uid]
        release_uid_hotkeys = {
            uid: snapshot_hotkeys[uid] for uid in sorted(release_uid_weights)
        }
        expected_uids, expected_weights = _wire_weights(
            sorted(identity_uid_weights),
            [identity_uid_weights[uid] for uid in sorted(identity_uid_weights)],
        )
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
    """Re-prove the external launch authorization before reservation.

    The service account owns its crash-safety journal and therefore cannot
    authorize itself merely by editing journal fields.  The root-signed public
    release is re-verified under the shared submission lock before every
    continuous reservation and must bind the exact local attempt, chain
    receipt, and rewarded-set evidence checkpoint. The returned immutable
    authorization is stored in the reservation; the lowest write boundary
    validates it locally and performs no fallible network work after pending
    state has been fsynced.
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
    return ContinuousAuthorization(
        launch_attempt_id=attempt_id,
        release_sha256=public_seal["release_sha256"],
        reproducer_revision=str(public_seal["reproducer_revision"]),
        validator_hotkey=validator_hotkey,
        genesis_hash=genesis_hash,
    )


def _continuous_transition_required(args: Any) -> bool:
    if (
        bool(getattr(args, "broadcast", False))
        and not bool(getattr(args, "offline", False))
        and int(getattr(args, "netuid", -1)) == 39
    ):
        # No operator-controlled label, endpoint, config, or direct CLI
        # invocation may weaken the SN39 transition requirement.
        return True
    explicit = getattr(args, "require_completed_launch_for_broadcast", None)
    if explicit is not None:
        return bool(explicit)
    return getattr(args, "require_policy", None) == "validated_supply_v1"


@contextlib.contextmanager
def _submission_tick_lock(args: Any, *, lane: str):
    """One non-blocking cross-process submission section for every mode.

    Thin and FULL-authority processes must contend on the same file. Separate
    per-lane locks permit both to reach the irreversible chain call and race
    which vector lands last. Shadow audit work remains concurrent because it
    never enters a submission tick on its own.
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
    _validate_chain_constraints(uid_weights, fresh)
    inclusion_policy = _authority_inclusion_policy(audit, fresh)
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
            "inclusion_policy": _inclusion_policy_identity(inclusion_policy),
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
            key: value for key, value in identity.items() if key != "mapping_block"
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
            _write_state_fenced(
                state_file,
                {
                    "authority_submission_attempt_id": attempt_id,
                    "authority_submission_attempt_status": "pending",
                    "authority_submission_attempted_at": _ms_iso_now(),
                    "authority_submission_identity": identity,
                    "authority_submission_dedup_identity": dedup_identity,
                },
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
        # The RPC waited for finalization. Persist the final identity before any
        # event write/flush. If this write fails, the already-fsynced pending
        # attempt above still blocks an unsafe automatic retry.
        _write_state(
            state_file,
            {
                "authority_submission_attempt_status": "finalized",
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
        _finalize_common_submission(
            args,
            attempt_id=attempt_id,
            submission=submission,
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
    sn39_broadcast = (
        bool(getattr(args, "broadcast", False))
        and not bool(getattr(args, "offline", False))
        and int(getattr(args, "netuid", -1)) == 39
    )
    if sn39_broadcast:
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
        if not launch_gate and not bool(
            getattr(args, "require_completed_launch_for_broadcast", False)
        ):
            raise wire.VectorError(
                "continuous SN39 broadcast requires the completed-launch gate"
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
    if (
        not bool(getattr(args, "broadcast", False))
        or bool(getattr(args, "offline", False))
        or not bool(getattr(args, "once", False))
        or max_submissions != 1
        or (getattr(args, "provenance", "shadow") or "shadow") != "shadow"
        or not launch_paths_match
        or missing
    ):
        suffix = f"; missing {', '.join(missing)}" if missing else ""
        raise wire.VectorError(
            "launch full-provenance broadcast requires online broadcast, --once, "
            "provenance=shadow, max_submissions=1, controlled evidence, verifier "
            f"binary, immutable launch paths, and burn hotkey{suffix}"
        )


def run(args) -> int:
    """The validator loop, shared by `python -m scaffold.validator_thin` and the
    `cathedral-validator serve` console command. `args` is any object carrying
    the tick attributes (an argparse Namespace or a SimpleNamespace from the
    CLI's config loader)."""
    _validate_runtime_contract(args)
    require_policy = getattr(args, "require_policy", None)
    if require_policy:
        _lifecycle("PIN active", f"policy={require_policy}")
    provenance_mode = getattr(args, "provenance", "shadow") or "shadow"
    submission_authority = (
        "full_provenance" if provenance_mode == "authority" else "thin"
    )
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
    while True:
        tick_ok = False
        try:
            tick_ok = tick(args)
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
        help="durable attempt ceiling; 0 means unlimited, launch canary requires 1",
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
