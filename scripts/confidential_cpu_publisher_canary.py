#!/usr/bin/env python3
"""Offline acceptance canary for the confidential CPU reward path.

The canary consumes two exact, fresh producer reports:

1. a complete report with positive confidential-compute scores; and
2. a later complete empty report that revokes every prior score.

It sends both reports through the real source-scoped HTTP intake, composes and
signs the real confidential-primary weight vectors, verifies them with the
thin validator's pinned policy, and proves the later vector routes all mass to
the signed burn UID.  All state, signing keys, bearer tokens, and HMAC secrets
are ephemeral.  The canary never connects to a chain or broadcasts weights.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import gc
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

SOURCE = "cathedral_confidential_tdx"
TOKEN_ENV = "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX"
HMAC_ENV = "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX"
WEIGHTS_MODE_ENV = "CATHEDRAL_WEIGHTS_MODE"
WEIGHTS_NETWORK_ENV = "CATHEDRAL_WEIGHT_POLICY_NETWORK"
WEIGHTS_NETUID_ENV = "CATHEDRAL_WEIGHT_POLICY_NETUID"
WEIGHTS_BURN_UID_ENV = "CATHEDRAL_WEIGHT_POLICY_BURN_UID"
WEIGHTS_BURN_PERCENTAGE_ENV = "CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2"
WEIGHTS_PAYABLE_HOTKEYS_ENV = "CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS"
WEIGHTS_PAYABLE_MAX_AGE_ENV = "CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS_MAX_AGE_SECS"
WEIGHTS_PERMINER_BONUS_ENV = "CATHEDRAL_PERMINER_BONUS_MULT"
WEIGHTS_SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"
WEIGHTS_KEY_ID_ENV = "CATHEDRAL_WEIGHT_POLICY_KEY_ID"
_CHILD_ARG = "--_isolated-child"


class CanaryError(RuntimeError):
    """Raised when the input or an acceptance boundary fails."""


def _parse_report(body: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanaryError(f"{label} report is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CanaryError(f"{label} report must be a JSON object")
    if payload.get("source") != SOURCE:
        raise CanaryError(f"{label} report source must be {SOURCE!r}")
    if payload.get("complete") is not True:
        raise CanaryError(f"{label} report must declare complete=true")
    if not isinstance(payload.get("scores"), list):
        raise CanaryError(f"{label} report scores must be a list")
    return payload


def _positive_hotkeys(scores: list[Any]) -> set[str]:
    positive: set[str] = set()
    for index, row in enumerate(scores):
        if not isinstance(row, dict):
            raise CanaryError(f"positive report score {index} must be an object")
        hotkey = str(row.get("miner_hotkey") or row.get("hotkey") or "").strip()
        if not hotkey:
            raise CanaryError(f"positive report score {index} is missing a hotkey")
        raw_score = row.get("score")
        if isinstance(raw_score, bool):
            raise CanaryError(f"positive report score {index} must be numeric")
        try:
            score = float(raw_score)
        except (TypeError, ValueError) as exc:
            raise CanaryError(f"positive report score {index} must be numeric") from exc
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise CanaryError(f"positive report score {index} must be finite and in [0, 1]")
        if score > 0.0:
            positive.add(hotkey)
    return positive


def _validate_inputs(
    positive: dict[str, Any],
    revoke: dict[str, Any],
    hotkey_to_uid: Mapping[str, int],
    *,
    burn_uid: int,
    network: str,
    netuid: int,
) -> None:
    for label, report in (("positive", positive), ("revoke", revoke)):
        if report.get("mechanism") != SOURCE:
            raise CanaryError(f"{label} report mechanism must be {SOURCE!r}")
        if report.get("network") != network:
            raise CanaryError(
                f"{label} report network must match configured network {network!r}"
            )
        report_netuid = report.get("netuid")
        if type(report_netuid) is not int or report_netuid != netuid:
            raise CanaryError(
                f"{label} report netuid must match configured netuid {netuid}"
            )
    positive_hotkeys = _positive_hotkeys(positive["scores"])
    if not positive_hotkeys:
        raise CanaryError("positive report must contain at least one positive score")
    if not positive_hotkeys.intersection(hotkey_to_uid):
        raise CanaryError("at least one positive report hotkey must have a UID mapping")
    if revoke["scores"]:
        raise CanaryError("revoke report must use an empty scores list")
    try:
        positive_epoch = positive["epoch"]
        revoke_epoch = revoke["epoch"]
    except KeyError as exc:
        raise CanaryError("both reports must carry integer epochs") from exc
    if type(positive_epoch) is not int or type(revoke_epoch) is not int:
        raise CanaryError("both reports must carry integer epochs")
    if positive_epoch < 0 or revoke_epoch < 0:
        raise CanaryError("report epochs must be non-negative")
    if revoke_epoch <= positive_epoch:
        raise CanaryError("revoke report epoch must be newer than positive report epoch")
    if not isinstance(burn_uid, int) or isinstance(burn_uid, bool) or burn_uid < 0:
        raise CanaryError("burn UID must be a non-negative integer")
    uids: list[int] = []
    for hotkey, uid in hotkey_to_uid.items():
        if not str(hotkey).strip():
            raise CanaryError("UID mapping hotkeys must be nonempty")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            raise CanaryError("mapped UIDs must be non-negative integers")
        uids.append(uid)
    if len(uids) != len(set(uids)):
        raise CanaryError("each hotkey mapping must use a distinct UID")
    if burn_uid in uids:
        raise CanaryError("burn UID must not collide with a mapped miner UID")


@contextmanager
def _isolated_env(values: Mapping[str, str]) -> Iterator[None]:
    """Replace the process environment with an explicit canary allowlist."""
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _ms_iso(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _virtualenv_package_paths() -> list[str]:
    """Site-packages of the venv running this child, derived without ``site``.

    The child runs under ``-I -S``, and ``-S`` is deliberate: the canary's own
    evidence reports ``site_startup_disabled`` and ``sitecustomize_loaded``, so
    no ``.pth`` file or ``sitecustomize`` may execute before the publisher is
    imported. But relocating ``sys.prefix`` onto a virtualenv is itself part of
    what ``site`` does with ``pyvenv.cfg``, so suppressing it leaves
    ``sys.prefix`` on the BASE installation::

        venv/bin/python       -c 'import sys; print(sys.prefix)'   ->  the venv
        venv/bin/python -I -S -c 'import sys; print(sys.prefix)'   ->  /usr

    ``sysconfig.get_path("purelib")`` then answers for the base interpreter.
    That directory normally exists, so the ``is_dir()`` guard below passed and
    the wrong path was appended with nothing to show for it, while the venv's
    site-packages -- the only place the publisher's dependencies are installed
    -- never reached ``sys.path``. The child died with ``No module named
    'fastapi'``, so the canary could not run at all inside a virtualenv, and the
    failure surfaced as a ``CanaryError`` that read like a canary result rather
    than a broken launcher.

    ``sys.executable`` is still the venv's interpreter, so the venv root is
    recoverable from it. Reading ``pyvenv.cfg`` and computing a path through
    ``sysconfig`` executes no user code and imports no ``site``, so the
    isolation those flags exist for is untouched.
    """
    # NOT ``.resolve()``: a venv's ``bin/python`` is normally a symlink to the
    # base interpreter, so resolving it lands on ``/usr/bin/python3.12`` and
    # walks up to ``/usr`` -- discarding the venv this is trying to find. The
    # unresolved path is the one that identifies the environment. The resolved
    # form is still tried afterwards, for the case where the interpreter really
    # was invoked by its real path.
    executable = Path(sys.executable)
    roots = (
        executable.parent.parent,
        executable.parent,
        executable.resolve().parent.parent,
    )
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        if not (root / "pyvenv.cfg").is_file():
            continue
        # The "venv" scheme, not the default one. On Debian and Ubuntu the
        # default is the patched ``posix_local``, which answers
        # ``<base>/local/lib/pythonX.Y/dist-packages`` -- correct for the system
        # interpreter, and not where a venv puts anything. ``site`` would have
        # selected the venv scheme itself; ``-S`` is why it did not.
        scheme = "venv" if "venv" in sysconfig.get_scheme_names() else "posix_prefix"
        try:
            return [
                sysconfig.get_path(
                    name,
                    scheme=scheme,
                    vars={"base": str(root), "platbase": str(root)},
                )
                for name in ("purelib", "platlib")
            ]
        except (KeyError, TypeError):
            return []
    return []


def _add_dependency_paths() -> None:
    """Expose installed dependencies without executing ``site`` or ``.pth`` files."""
    # The venv's own packages go FIRST. When both are present, the venv is the
    # environment the canary was launched from and the base installation is at
    # best a fallback.
    candidates: list[str] = _virtualenv_package_paths()
    for scheme in sysconfig.get_scheme_names():
        if scheme.endswith("_user"):
            try:
                candidates.append(sysconfig.get_path("purelib", scheme=scheme))
            except (KeyError, TypeError):
                pass
    candidates.extend(
        path
        for path in (
            sysconfig.get_path("purelib"),
            sysconfig.get_path("platlib"),
        )
        if path
    )
    for raw_path in candidates:
        path = str(Path(raw_path).resolve())
        if Path(path).is_dir() and path not in sys.path:
            # Deliberately avoid site.addsitedir(): it executes .pth files.
            sys.path.append(path)


def _install_egress_audit_hook(attempts: list[str]) -> None:
    """Deny Python socket creation and process-launch escape paths in the child.

    Audit hooks cannot be removed, which is why this is installed only inside
    the disposable child process.  On macOS the parent additionally launches
    that child under ``sandbox-exec`` with every OS network operation denied.
    """

    def _deny(event: str, args: tuple[Any, ...]) -> None:
        network_socket = False
        if event == "socket.__new__":
            family = args[1] if len(args) > 1 else socket.AF_INET
            # TestClient/AnyIO may create AF_UNIX socketpairs for local thread
            # coordination. They cannot reach a network and remain allowed.
            network_socket = family != socket.AF_UNIX
        socket_egress = event in {"socket.connect", "socket.sendto", "socket.sendmsg"}
        blocked = (
            network_socket
            or socket_egress
            or event == "subprocess.Popen"
            or event == "os.system"
            or event.startswith("os.exec")
            or event.startswith("os.spawn")
            or event.startswith("os.posix_spawn")
            or event == "pty.spawn"
        )
        if not blocked:
            return
        attempts.append(event)
        raise CanaryError(f"child egress attempt blocked: {event}")

    sys.addaudithook(_deny)


def _headers(token: str, secret: str, body: bytes) -> dict[str, str]:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Cathedral-External-Signature": f"sha256={digest}",
    }


def _seed_metagraph(
    app: Any,
    hotkey_to_uid: Mapping[str, int],
    *,
    network: str,
    netuid: int,
    age_secs: float = 0.0,
) -> None:
    from datetime import timedelta

    updated_at = _ms_iso(datetime.now(timezone.utc) - timedelta(seconds=age_secs))

    def _write(conn: Any) -> None:
        for hotkey, uid in hotkey_to_uid.items():
            conn.execute(
                "INSERT OR REPLACE INTO metagraph_hotkeys("
                "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (network, netuid, hotkey, int(uid), "", 0, updated_at),
            )

    app.state.store.write(_write)


def _post_report(client: Any, body: bytes, token: str, secret: str) -> dict[str, Any]:
    response = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_headers(token, secret, body),
    )
    if response.status_code != 202:
        raise CanaryError(
            f"publisher rejected report: HTTP {response.status_code} {response.text}"
        )
    payload = response.json()
    if payload.get("idempotent") is not False:
        raise CanaryError("canary requires a newly accepted report, not an idempotent retry")
    return payload


def _assert_raw_body_auth(client: Any, body: bytes, token: str, secret: str) -> None:
    """Prove the route authenticates the exact bytes, not parsed JSON."""
    tampered = body + b" "
    response = client.post(
        "/v1/external-scores/violet",
        content=tampered,
        headers=_headers(token, secret, body),
    )
    if response.status_code != 401 or response.json().get("detail") != (
        "invalid_external_scores_signature"
    ):
        raise CanaryError(
            "publisher did not reject valid JSON whose raw bytes changed after HMAC"
        )


def _assert_report_binding(
    vector: dict[str, Any],
    accepted: dict[str, Any],
    *,
    revoked_all: bool,
) -> None:
    """Bind the signed policy metadata to the exact accepted report."""
    metadata = vector.get("policy_metadata")
    if not isinstance(metadata, dict):
        raise CanaryError("signed vector is missing policy metadata")
    status = metadata.get("external_scores")
    if not isinstance(status, dict):
        raise CanaryError("signed vector is missing external score status")
    expected = {
        "latest_epoch": int(accepted["epoch"]),
        "latest_report_sha256": accepted["report_sha256"],
        "latest_complete": True,
        "latest_fresh": True,
    }
    observed = {name: status.get(name) for name in expected}
    if observed != expected:
        raise CanaryError(
            f"signed vector is not bound to the accepted report: {observed!r}"
        )

    cp = metadata.get("confidential_primary")
    if not isinstance(cp, dict):
        raise CanaryError("signed vector is missing confidential-primary metadata")
    if cp.get("complete") is not True or cp.get("fresh") is not True:
        raise CanaryError("signed confidential-primary report is not complete and fresh")
    if revoked_all:
        if cp.get("confidential_mass") != 0.0:
            raise CanaryError("revoke vector retained confidential miner mass")
        if cp.get("degradation_reason") != "confidential_snapshot_revoked_all":
            raise CanaryError(
                "burn vector came from unrelated degradation, not the accepted revocation"
            )
    else:
        if cp.get("confidential_mass") != 1.0:
            reason = cp.get("degradation_reason")
            raise CanaryError(
                f"positive report did not produce confidential miner mass: {reason}"
            )
        if cp.get("degradation_reason") is not None:
            raise CanaryError("positive vector unexpectedly carries a degradation reason")
        payable = metadata.get("payable_hotkeys")
        if not isinstance(payable, dict) or payable.get("enforced") is not True:
            raise CanaryError("signed payable-hotkey filter was not enforced")
        if payable.get("snapshot_fresh") is not True:
            raise CanaryError("signed payable-hotkey snapshot was not fresh")


def _assert_old_report_rejected(
    client: Any,
    store: Any,
    body: bytes,
    token: str,
    secret: str,
    *,
    expected_epoch: int,
    expected_digest: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_headers(token, secret, body),
    )
    try:
        detail = response.json().get("detail")
    except (AttributeError, ValueError):
        detail = None
    if response.status_code != 409 or detail != "epoch_too_old":
        raise CanaryError(
            "older report failed for the wrong reason: "
            f"HTTP {response.status_code} {detail!r}"
        )
    latest = store.query(
        "SELECT epoch, report_sha256 FROM external_score_reports "
        "WHERE source=? ORDER BY epoch DESC LIMIT 1",
        (SOURCE,),
    )
    observed = (
        (int(latest[0]["epoch"]), str(latest[0]["report_sha256"]))
        if latest
        else None
    )
    expected = (expected_epoch, expected_digest)
    if observed != expected:
        raise CanaryError(
            "older report rejection changed the latest stored report: "
            f"expected {expected!r}, observed {observed!r}"
        )
    return {
        "http_status": response.status_code,
        "reason": detail,
        "latest_report_unchanged": True,
    }


def _assert_old_vector_rejected(
    vector: dict[str, Any],
    *,
    public_key_hex: str,
    key_id: str,
    network: str,
    netuid: int,
    fence_version: int,
) -> None:
    from scaffold import validator_thin

    try:
        validator_thin.accept_vector(
            vector,
            public_key_hex=public_key_hex,
            key_id=key_id,
            network=network,
            netuid=netuid,
            fence_version=fence_version,
        )
    except validator_thin.wire.VectorError as exc:
        if "rollback/replay" not in str(exc):
            raise CanaryError(f"old vector failed for the wrong reason: {exc}") from exc
        return
    raise CanaryError("thin validator accepted the older positive vector after revocation")


def _verify_vector(
    payload: dict[str, Any],
    *,
    public_key_hex: str,
    key_id: str,
    network: str,
    netuid: int,
    fence_version: int,
    hotkey_to_uid: Mapping[str, int],
) -> dict[int, float]:
    from scaffold import validator_thin

    validator_thin.accept_vector(
        payload,
        public_key_hex=public_key_hex,
        key_id=key_id,
        network=network,
        netuid=netuid,
        fence_version=fence_version,
    )
    return validator_thin.vector_to_uid_weights(
        payload,
        dict(hotkey_to_uid),
        require_policy=validator_thin.REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1,
    )


def _run_canary_in_child(
    positive_body: bytes,
    revoke_body: bytes,
    hotkey_to_uid: Mapping[str, int],
    *,
    network: str = "finney",
    netuid: int = 39,
    burn_uid: int = 0,
    metagraph_age_secs: float = 0.0,
    egress_attempts: list[str],
    os_network_sandbox: str,
) -> dict[str, Any]:
    """Execute inside the already-sanitized, egress-guarded child process."""
    if not isinstance(network, str) or not network.strip():
        raise CanaryError("network must be a nonempty string")
    if not isinstance(netuid, int) or isinstance(netuid, bool) or netuid < 0:
        raise CanaryError("netuid must be a non-negative integer")
    positive = _parse_report(positive_body, label="positive")
    revoke = _parse_report(revoke_body, label="revoke")
    _validate_inputs(
        positive,
        revoke,
        hotkey_to_uid,
        burn_uid=burn_uid,
        network=network,
        netuid=netuid,
    )
    report_hotkeys = _positive_hotkeys(positive["scores"])
    mapped_positive_hotkeys = report_hotkeys.intersection(hotkey_to_uid)
    expected_positive_uids = {hotkey_to_uid[hotkey] for hotkey in mapped_positive_hotkeys}
    filtered_hotkeys = sorted(report_hotkeys - set(hotkey_to_uid))

    signing_key = secrets.token_hex(32)
    key_id = "cathedral-confidential-cpu-canary"
    token = secrets.token_urlsafe(32)
    hmac_secret = secrets.token_urlsafe(32)
    summary: dict[str, Any] | None = None
    temp_root: Path | None = None
    store_closed = False
    cnf_store_registration_removed = False

    with tempfile.TemporaryDirectory(prefix="cathedral-cpu-canary-") as tmpdir:
        temp_root = Path(tmpdir)
        database_path = str(Path(tmpdir) / "publisher.sqlite")
        env: dict[str, str] = {
            # This is the complete process environment during app construction.
            "TMPDIR": tmpdir,
            "CATHEDRAL_SERVICE_ROLE": "submit",
            "CATHEDRAL_V2_BLOB_DIR": str(Path(tmpdir) / "blobs"),
            "CATHEDRAL_CNF_TOKEN_SECRET": secrets.token_urlsafe(32),
            "CATHEDRAL_V2_SUBMIT_TOKEN_SECRET": secrets.token_urlsafe(32),
            # Exact source-scoped intake credentials, generated for this process.
            "CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED": "1",
            "CATHEDRAL_EXTERNAL_SCORES_ENABLED": "1",
            "CATHEDRAL_EXTERNAL_SCORES_SOURCE": SOURCE,
            "CATHEDRAL_EXTERNAL_SCORES_MODE": "confidential_primary",
            "CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM": "true",
            "CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED": "1",
            "CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS": "3600",
            "CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_AGE_SECS": "3600",
            TOKEN_ENV: token,
            HMAC_ENV: hmac_secret,
            # Publisher composition and thin-validator contract.
            WEIGHTS_MODE_ENV: "flat_recent",
            WEIGHTS_NETWORK_ENV: network,
            WEIGHTS_NETUID_ENV: str(netuid),
            WEIGHTS_BURN_UID_ENV: str(burn_uid),
            WEIGHTS_BURN_PERCENTAGE_ENV: "0",
            WEIGHTS_PAYABLE_HOTKEYS_ENV: "filter",
            WEIGHTS_PAYABLE_MAX_AGE_ENV: "600",
            WEIGHTS_PERMINER_BONUS_ENV: "0",
            WEIGHTS_SIGNING_KEY_ENV: signing_key,
            WEIGHTS_KEY_ID_ENV: key_id,
        }
        app: Any | None = None
        client: Any | None = None
        store: Any | None = None
        store_registry_key: int | None = None
        with _isolated_env(env):
            # Import publisher code only after inherited configuration has been
            # replaced. The child process guarantees these are first imports.
            repository_root = str(Path(__file__).resolve().parents[1])
            if repository_root not in sys.path:
                sys.path.insert(0, repository_root)
            _add_dependency_paths()
            from fastapi.testclient import TestClient
            from scaffold.publisher import app as app_mod
            from scaffold.publisher import rows, weights

            try:
                app = app_mod.build_app(
                    database_path=database_path,
                    signing_key_hex=signing_key,
                )
                store = app.state.store
                store_registry_key = id(store)
                if store.backend != "sqlite" or Path(store.path) != Path(database_path):
                    raise CanaryError("publisher did not use the isolated SQLite database")
                _seed_metagraph(
                    app,
                    hotkey_to_uid,
                    network=network,
                    netuid=netuid,
                    age_secs=metagraph_age_secs,
                )
                client = TestClient(app)
                _assert_raw_body_auth(client, positive_body, token, hmac_secret)
                positive_accept = _post_report(client, positive_body, token, hmac_secret)
                positive_vector = weights.build_signed_vector(
                    store,
                    signing_key_hex=signing_key,
                )
                public_key = rows.public_key_hex(signing_key)
                positive_uid_weights = _verify_vector(
                    positive_vector,
                    public_key_hex=public_key,
                    key_id=key_id,
                    network=network,
                    netuid=netuid,
                    fence_version=-1,
                    hotkey_to_uid=hotkey_to_uid,
                )
                _assert_report_binding(
                    positive_vector,
                    positive_accept,
                    revoked_all=False,
                )
                if set(positive_uid_weights) != expected_positive_uids:
                    raise CanaryError(
                        "positive vector did not map exactly the registered positive "
                        f"UIDs: {positive_uid_weights}"
                    )

                revoke_accept = _post_report(client, revoke_body, token, hmac_secret)
                revoke_vector = weights.build_signed_vector(
                    store,
                    signing_key_hex=signing_key,
                )
                revoke_uid_weights = _verify_vector(
                    revoke_vector,
                    public_key_hex=public_key,
                    key_id=key_id,
                    network=network,
                    netuid=netuid,
                    fence_version=int(positive_vector["policy_version"]),
                    hotkey_to_uid=hotkey_to_uid,
                )
                _assert_report_binding(
                    revoke_vector,
                    revoke_accept,
                    revoked_all=True,
                )
                if revoke_uid_weights != {burn_uid: 1.0}:
                    raise CanaryError(
                        "revoke vector did not route all mass to burn UID: "
                        f"{revoke_uid_weights}"
                    )
                old_report_replay = _assert_old_report_rejected(
                    client,
                    store,
                    positive_body,
                    token,
                    hmac_secret,
                    expected_epoch=int(revoke_accept["epoch"]),
                    expected_digest=str(revoke_accept["report_sha256"]),
                )
                _assert_old_vector_rejected(
                    positive_vector,
                    public_key_hex=public_key,
                    key_id=key_id,
                    network=network,
                    netuid=netuid,
                    fence_version=int(revoke_vector["policy_version"]),
                )

                summary = {
                    "status": "passed",
                    "network": network,
                    "netuid": netuid,
                    "burn_uid": burn_uid,
                    "positive": {
                        "input_sha256": hashlib.sha256(positive_body).hexdigest(),
                        "epoch": int(positive["epoch"]),
                        "publisher_report_sha256": positive_accept["report_sha256"],
                        "vector_id": positive_vector["vector_id"],
                        "policy_version": int(positive_vector["policy_version"]),
                        "uid_weights": positive_uid_weights,
                        "filtered_unregistered_hotkeys": filtered_hotkeys,
                    },
                    "revoke": {
                        "input_sha256": hashlib.sha256(revoke_body).hexdigest(),
                        "epoch": int(revoke["epoch"]),
                        "publisher_report_sha256": revoke_accept["report_sha256"],
                        "vector_id": revoke_vector["vector_id"],
                        "policy_version": int(revoke_vector["policy_version"]),
                        "uid_weights": revoke_uid_weights,
                        "old_report_replay": old_report_replay,
                        "old_vector_rollback_rejected": True,
                    },
                    "isolation": {
                        "environment": "explicit_allowlist",
                        "imports_after_environment_isolation": True,
                        "database_backend": "sqlite",
                        "child_process": True,
                        "python_isolated_mode": bool(sys.flags.isolated),
                        "site_startup_disabled": bool(sys.flags.no_site),
                        "sitecustomize_loaded": "sitecustomize" in sys.modules,
                        "python_egress_guard": "audit_hook",
                        "os_network_sandbox": os_network_sandbox,
                        "egress_attempts": len(egress_attempts),
                        "raw_body_tamper_rejected": True,
                    },
                }
            finally:
                if client is not None:
                    client.close()
                if store_registry_key is not None:
                    app_mod._CNF_STORES.pop(store_registry_key, None)
                    cnf_store_registration_removed = (
                        store_registry_key not in app_mod._CNF_STORES
                    )
                if store is not None:
                    store.close()
                    store_closed = True
                if app is not None:
                    app.state.signing_key_hex = None
                    app.state.store = None
                    app.state.v2_store = None
                client = None
                store = None
                app = None
                env.clear()
                gc.collect()

    if summary is None or temp_root is None:
        raise CanaryError("canary did not produce an evidence summary")
    summary["isolation"].update({
        "cnf_store_registration_removed": cnf_store_registration_removed,
        "store_closed": store_closed,
        "temporary_root_removed": not temp_root.exists(),
    })
    if (
        not store_closed
        or not cnf_store_registration_removed
        or temp_root.exists()
        or egress_attempts
        or summary["isolation"]["sitecustomize_loaded"]
    ):
        raise CanaryError("canary isolation cleanup did not complete")
    serialized = json.dumps(summary, sort_keys=True)
    if any(secret and secret in serialized for secret in (signing_key, token, hmac_secret)):
        raise CanaryError("private canary material leaked into the evidence summary")
    summary["isolation"]["private_values_in_evidence"] = False
    signing_key = token = hmac_secret = ""
    return summary


def _child_command() -> tuple[list[str], str]:
    python_command = [
        sys.executable,
        "-I",
        "-S",
        str(Path(__file__).resolve()),
        _CHILD_ARG,
    ]
    sandbox_exec = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
    if sandbox_exec:
        profile = "(version 1) (allow default) (deny network*)"
        return [sandbox_exec, "-p", profile, *python_command], "sandbox-exec-deny-network"
    return python_command, "unavailable-python-guard-only"


def run_canary(
    positive_body: bytes,
    revoke_body: bytes,
    hotkey_to_uid: Mapping[str, int],
    *,
    network: str = "finney",
    netuid: int = 39,
    burn_uid: int = 0,
    _metagraph_age_secs: float = 0.0,
) -> dict[str, Any]:
    """Run the canary in a clean child and return its secret-free evidence."""
    request = {
        "positive_body_b64": base64.b64encode(positive_body).decode("ascii"),
        "revoke_body_b64": base64.b64encode(revoke_body).decode("ascii"),
        "hotkey_to_uid": dict(hotkey_to_uid),
        "network": network,
        "netuid": netuid,
        "burn_uid": burn_uid,
        "metagraph_age_secs": _metagraph_age_secs,
    }
    command, sandbox_label = _child_command()
    child_env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "CATHEDRAL_CANARY_OS_NETWORK_SANDBOX": sandbox_label,
    }
    try:
        result = subprocess.run(
            command,
            input=json.dumps(request, separators=(",", ":")),
            text=True,
            capture_output=True,
            cwd=Path(__file__).resolve().parents[1],
            env=child_env,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CanaryError(f"isolated canary child failed to start: {exc}") from exc
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        stderr = result.stderr.strip()[-1000:]
        raise CanaryError(
            "isolated canary child returned invalid evidence"
            + (f": {stderr}" if stderr else "")
        ) from exc
    if result.returncode != 0 or envelope.get("ok") is not True:
        reason = str(envelope.get("error") or result.stderr.strip() or "unknown child failure")
        raise CanaryError(reason)
    summary = envelope.get("summary")
    if not isinstance(summary, dict):
        raise CanaryError("isolated canary child omitted its evidence summary")
    for phase in ("positive", "revoke"):
        section = summary.get(phase)
        if isinstance(section, dict) and isinstance(section.get("uid_weights"), dict):
            section["uid_weights"] = {
                int(uid): float(weight)
                for uid, weight in section["uid_weights"].items()
            }
    return summary


def _isolated_child_main() -> int:
    """Internal entrypoint. Read one request before locking down egress."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise CanaryError("child request must be an object")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"invalid child request: {exc}"}))
        return 2

    probe = request.get("egress_probe")
    if probe is not None:
        attempts: list[str] = []
        _install_egress_audit_hook(attempts)
        try:
            if probe == "tcp":
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            elif probe == "udp":
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            elif probe == "process":
                subprocess.run([sys.executable, "-c", "pass"], check=False)
            elif probe == "unix":
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(
                    "/tmp/cathedral-canary-must-not-connect.sock"
                )
            else:
                raise CanaryError(f"unknown egress probe {probe!r}")
        except CanaryError:
            print(json.dumps({"ok": True, "blocked_event": attempts[-1]}))
            return 0
        print(json.dumps({"ok": False, "error": f"egress probe {probe!r} escaped"}))
        return 1

    try:
        positive_body = base64.b64decode(request["positive_body_b64"], validate=True)
        revoke_body = base64.b64decode(request["revoke_body_b64"], validate=True)
        mapping = request["hotkey_to_uid"]
        if not isinstance(mapping, dict):
            raise CanaryError("hotkey_to_uid must be an object")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"invalid child request: {exc}"}))
        return 2

    attempts: list[str] = []
    _install_egress_audit_hook(attempts)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            summary = _run_canary_in_child(
                positive_body,
                revoke_body,
                mapping,
                network=request.get("network", "finney"),
                netuid=request.get("netuid", 39),
                burn_uid=request.get("burn_uid", 0),
                metagraph_age_secs=float(request.get("metagraph_age_secs", 0.0)),
                egress_attempts=attempts,
                os_network_sandbox=os.environ.get(
                    "CATHEDRAL_CANARY_OS_NETWORK_SANDBOX",
                    "unavailable-python-guard-only",
                ),
            )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "summary": summary}, sort_keys=True))
    return 0


def _probe_egress_guard(kind: str) -> str:
    """Exercise one real egress primitive in a disposable guarded child."""
    command, sandbox_label = _child_command()
    result = subprocess.run(
        command,
        input=json.dumps({"egress_probe": kind}),
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "CATHEDRAL_CANARY_OS_NETWORK_SANDBOX": sandbox_label,
        },
        timeout=30,
        check=False,
    )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CanaryError("egress probe child returned invalid evidence") from exc
    if result.returncode != 0 or envelope.get("ok") is not True:
        raise CanaryError(str(envelope.get("error") or "egress probe failed"))
    return str(envelope["blocked_event"])


def _uid_mapping(value: str) -> tuple[str, int]:
    hotkey, separator, raw_uid = value.rpartition("=")
    if not separator or not hotkey.strip():
        raise argparse.ArgumentTypeError("expected HOTKEY=UID")
    try:
        uid = int(raw_uid)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("UID must be an integer") from exc
    if uid < 0:
        raise argparse.ArgumentTypeError("UID must be non-negative")
    return hotkey.strip(), uid


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the confidential CPU report -> publisher -> signed vector -> "
            "policy-pinned thin-validator path without chain access"
        )
    )
    parser.add_argument("--positive-report", type=Path, required=True)
    parser.add_argument("--revoke-report", type=Path, required=True)
    parser.add_argument(
        "--uid-map",
        action="append",
        type=_uid_mapping,
        required=True,
        metavar="HOTKEY=UID",
        help=(
            "repeat for each currently registered positive hotkey; omitted positive "
            "hotkeys must be filtered as unregistered"
        ),
    )
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=_nonnegative_int, default=39)
    parser.add_argument("--burn-uid", type=_nonnegative_int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mapping = dict(args.uid_map)
    if len(mapping) != len(args.uid_map):
        raise CanaryError("each --uid-map hotkey must be unique")
    summary = run_canary(
        args.positive_report.read_bytes(),
        args.revoke_report.read_bytes(),
        mapping,
        network=args.network,
        netuid=args.netuid,
        burn_uid=args.burn_uid,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == [_CHILD_ARG]:
        raise SystemExit(_isolated_child_main())
    raise SystemExit(main())
