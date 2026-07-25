#!/usr/bin/env python3
"""Inspect or execute one explicitly approved SN39 ``swap_hotkey_v2`` call.

The default mode performs public chain reads and composes the exact call, but
never unlocks a coldkey, signs, or submits.  A broadcast requires the digest
from a separate inspection plus two owner-only, previously absent output
paths.  Before submission, the exact signed hash is fsynced to the state file;
an unavailable response therefore remains an operator-reconciled ambiguity
and can never be treated as permission to retry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

NETWORK = "finney"
NETUID = 39
ERA_PERIOD_BLOCKS = 64
# A review remains usable for approximately one mortal era, while the last
# possible inclusion of the signed 64-block transaction also stays inside the
# reviewed approval boundary.
APPROVAL_LIFETIME_BLOCKS = ERA_PERIOD_BLOCKS * 2
ROTATION_CONTEXT_ENV = "CATHEDRAL_SN39_ROTATION_CONTEXT"
CONTEXT_SCHEMA = "cathedral_sn39_rotation_execution_v1"
AUTHORITY_STATE_ROOT = Path("/var/lib/cathedral-sn39-rotation")
ROOT_UID = 0
MAX_PRIVATE_ARTIFACT_BYTES = 256 * 1024
FINNEY_GENESIS_HASH = (
    "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
)
REVIEW_SCHEMA = "cathedral_sn39_hotkey_rotation_review_v1"
STATE_SCHEMA = "cathedral_sn39_hotkey_rotation_attempt_v1"
TARGET_SCHEMA = "cathedral_sn39_hotkey_rotation_target_v1"
HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SS58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{40,64}$")
SIGNATURE = re.compile(r"^0x[0-9a-fA-F]{128}$")
ROLES = ("rewarded", "owner-burn")
COST_AUTHORIZATION_MODEL = "pre_sign_estimate_ceiling_not_an_on_chain_spend_cap"
# Bittensor 10.5 documents this SS58 account as the Owner storage default for
# a hotkey that has never been associated with a coldkey.
UNOWNED_HOTKEY_OWNER = "5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM"


class RotationError(RuntimeError):
    """The requested rotation is malformed or positively failed."""


class RotationNotProven(RotationError):
    """A signed attempt may have landed, but its exact receipt is unavailable."""


@dataclass(frozen=True)
class Options:
    wallet_name: str
    new_wallet_name: str
    new_wallet_hotkey: str
    expected_coldkey: str
    old_hotkey: str
    new_hotkey: str
    expected_uid: int
    role: str
    keep_stake: bool
    authority_host: str
    authority_uid: int
    max_transaction_fee_rao: int
    execution_context: dict[str, Any] | None = None
    netuid: int = NETUID
    broadcast: bool = False
    reconcile: bool = False
    confirmation_digest: str | None = None
    state_file: Path | None = None
    receipt_out: Path | None = None
    reviewed_finalized_block: int | None = None
    reviewed_finalized_hash: str | None = None
    reviewed_coldkey_nonce: int | None = None
    approval_valid_until_block: int | None = None


@dataclass(frozen=True)
class Runtime:
    wallet: Any
    new_wallet: Any
    subtensor: Any
    substrate: Any


@dataclass(frozen=True)
class Inspection:
    call: Any
    approval: dict[str, Any]
    observation: dict[str, Any]

    @property
    def confirmation_digest(self) -> str:
        return _digest(self.approval)


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(document)).hexdigest()


def _target_id(options: Options) -> str:
    """One durable attempt namespace for an old hotkey, regardless of new choice."""
    return hashlib.sha256(
        _canonical(
            {
                "schema": TARGET_SCHEMA,
                "network": NETWORK,
                "netuid": NETUID,
                "signer_coldkey": options.expected_coldkey,
                "old_hotkey": options.old_hotkey,
            }
        )
    ).hexdigest()


def _artifact_names(options: Options) -> tuple[str, str]:
    target = _target_id(options)
    return (
        f"rotation-{target}.attempt.json",
        f"rotation-{target}.receipt.json",
    )


def _canonical_attempt_dir(options: Options) -> Path:
    """Return the manifest-bound durable attempt directory for this authority."""
    context = options.execution_context
    if not isinstance(context, dict):
        raise RotationError("immutable rotation execution context is unavailable")
    try:
        return Path(str(context["authority_state_dir"])).resolve(strict=True)
    except (KeyError, OSError, RuntimeError) as exc:
        raise RotationError(
            "canonical durable rotation directory is unavailable"
        ) from exc


def _load_execution_context() -> dict[str, Any]:
    raw = os.environ.pop(ROTATION_CONTEXT_ENV, "")
    if not raw or len(raw.encode("utf-8")) > 4096:
        raise RotationError(
            "immutable rotation launcher context is absent or oversized"
        )

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RotationError("rotation launcher context has duplicate keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            raw,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RotationError("rotation launcher context has a non-finite number")
            ),
        )
    except RotationError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RotationError("rotation launcher context is not strict JSON") from exc
    if not isinstance(document, dict) or _canonical(document).decode("utf-8") != raw:
        raise RotationError("rotation launcher context is not canonical JSON")
    return document


def _validate_execution_context(options: Options) -> dict[str, Any]:
    context = options.execution_context
    expected_fields = {
        "schema",
        "source_sha",
        "manifest_sha256",
        "bundle_tree_sha256",
        "venv_tree_sha256",
        "launcher_sha256",
        "authority_host",
        "authority_uid",
        "authority_home",
        "authority_state_dir",
    }
    if not isinstance(context, dict) or set(context) != expected_fields:
        raise RotationError("immutable rotation execution context fields differ")
    for field in (
        "manifest_sha256",
        "bundle_tree_sha256",
        "venv_tree_sha256",
        "launcher_sha256",
    ):
        if (
            not isinstance(context.get(field), str)
            or DIGEST.fullmatch(context[field]) is None
        ):
            raise RotationError("immutable rotation execution digest is malformed")
    source_sha = context.get("source_sha")
    authority_home = context.get("authority_home")
    authority_state_dir = context.get("authority_state_dir")
    expected_state_dir = AUTHORITY_STATE_ROOT / f"uid-{options.authority_uid}"
    if (
        context.get("schema") != CONTEXT_SCHEMA
        or not isinstance(source_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or context.get("authority_host") != options.authority_host
        or context.get("authority_uid") != options.authority_uid
        or not isinstance(authority_home, str)
        or not authority_home.startswith("/")
        or not isinstance(authority_state_dir, str)
        or authority_state_dir != str(expected_state_dir)
    ):
        raise RotationError("immutable rotation execution identity differs")
    try:
        account = pwd.getpwuid(options.authority_uid)
        state_root_info = AUTHORITY_STATE_ROOT.lstat()
        state_path = Path(authority_state_dir)
        state_info = state_path.lstat()
    except (KeyError, OSError) as exc:
        raise RotationError(
            "rotation authority account or durable state directory is unavailable"
        ) from exc
    if (
        account.pw_dir != authority_home
        or AUTHORITY_STATE_ROOT.is_symlink()
        or not stat.S_ISDIR(state_root_info.st_mode)
        or state_root_info.st_uid != ROOT_UID
        or stat.S_IMODE(state_root_info.st_mode) & 0o022
        or state_path.is_symlink()
        or not stat.S_ISDIR(state_info.st_mode)
        or state_info.st_uid != options.authority_uid
        or stat.S_IMODE(state_info.st_mode) != 0o700
    ):
        raise RotationError(
            "rotation authority account or durable state directory changed"
        )
    return context


def _hash(value: Any, *, label: str) -> str:
    if isinstance(value, bytes):
        rendered = "0x" + value.hex()
    elif hasattr(value, "hex") and not isinstance(value, str):
        rendered = str(value.hex())
        if not rendered.startswith("0x"):
            rendered = "0x" + rendered
    else:
        rendered = str(value)
    rendered = rendered.lower()
    if HASH.fullmatch(rendered) is None:
        raise RotationError(f"{label} is not a canonical 32-byte hash")
    return rendered


def _call_hex(call: Any) -> str:
    data = getattr(call, "data", None)
    if hasattr(data, "to_hex"):
        rendered = str(data.to_hex())
    elif isinstance(data, bytes):
        rendered = "0x" + data.hex()
    else:
        rendered = ""
    rendered = rendered.lower()
    if (
        not rendered.startswith("0x")
        or len(rendered) <= 2
        or len(rendered[2:]) % 2
        or re.fullmatch(r"0x[0-9a-f]+", rendered) is None
    ):
        raise RotationError("composed swap_hotkey_v2 call has no canonical bytes")
    return rendered


def _sequence(value: Any) -> list[Any]:
    raw = value.tolist() if hasattr(value, "tolist") else value
    if not isinstance(raw, (list, tuple)):
        raise RotationError("finalized metagraph returned a malformed sequence")
    return list(raw)


def _storage_value(
    subtensor: Any,
    *,
    name: str,
    params: list[Any],
    block: int,
) -> Any:
    observed = subtensor.query_subtensor(name=name, params=params, block=block)
    return getattr(observed, "value", observed)


def _key_address(key: Any) -> str:
    address = getattr(key, "ss58_address", None)
    if not isinstance(address, str) or SS58.fullmatch(address) is None:
        raise RotationError("wallet coldkey address is unavailable or malformed")
    return address


def _rao(value: Any, *, label: str) -> int:
    raw = getattr(value, "rao", value)
    if isinstance(raw, bool):
        raise RotationError(f"{label} is malformed")
    try:
        amount = int(raw)
    except (TypeError, ValueError) as exc:
        raise RotationError(f"{label} is malformed") from exc
    if amount < 0:
        raise RotationError(f"{label} is malformed")
    return amount


def _connect(
    wallet_name: str,
    new_wallet_name: str,
    new_wallet_hotkey: str,
) -> Runtime:
    try:
        import bittensor as bt

        wallet_type = getattr(bt, "Wallet", None) or bt.wallet
        subtensor_type = getattr(bt, "Subtensor", None) or bt.subtensor
        wallet = wallet_type(name=wallet_name)
        new_wallet = wallet_type(
            name=new_wallet_name,
            hotkey=new_wallet_hotkey,
        )
        subtensor = subtensor_type(network=NETWORK)
    except Exception as exc:
        raise RotationError(
            "cannot open the named wallet or connect to Finney"
        ) from exc
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        raise RotationError("Finney connection has no substrate interface")
    return Runtime(
        wallet=wallet,
        new_wallet=new_wallet,
        subtensor=subtensor,
        substrate=substrate,
    )


def _require_designated_authority(options: Options) -> None:
    """Fence every inspection and broadcast to one reviewed local authority."""
    if (
        not isinstance(options.authority_host, str)
        or re.fullmatch(r"[^\s\x00-\x1f\x7f]{1,255}", options.authority_host) is None
        or isinstance(options.authority_uid, bool)
        or not isinstance(options.authority_uid, int)
        or options.authority_uid < 0
    ):
        raise RotationError("designated authority host or OS uid is malformed")
    if (
        os.uname().nodename != options.authority_host
        or os.geteuid() != options.authority_uid
    ):
        raise RotationError(
            "rotation must run on the designated authority host and exact OS uid"
        )
    _validate_execution_context(options)


def _validate_options(options: Options) -> None:
    _require_designated_authority(options)
    if (
        not isinstance(options.wallet_name, str)
        or not options.wallet_name.strip()
        or not isinstance(options.new_wallet_name, str)
        or not options.new_wallet_name.strip()
        or not isinstance(options.new_wallet_hotkey, str)
        or not options.new_wallet_hotkey.strip()
        or isinstance(options.expected_uid, bool)
        or not isinstance(options.expected_uid, int)
        or options.expected_uid < 0
        or options.netuid != NETUID
        or options.role not in ROLES
        or not isinstance(options.keep_stake, bool)
        or isinstance(options.max_transaction_fee_rao, bool)
        or not isinstance(options.max_transaction_fee_rao, int)
        or options.max_transaction_fee_rao <= 0
    ):
        raise RotationError("rotation arguments are incomplete or outside SN39")
    for label, value in (
        ("expected coldkey", options.expected_coldkey),
        ("old hotkey", options.old_hotkey),
        ("new hotkey", options.new_hotkey),
    ):
        if not isinstance(value, str) or SS58.fullmatch(value) is None:
            raise RotationError(f"{label} is not a plausible SS58 address")
    if (
        len(
            {
                options.expected_coldkey,
                options.old_hotkey,
                options.new_hotkey,
            }
        )
        != 3
    ):
        raise RotationError("coldkey, old hotkey, and new hotkey must be distinct")
    if options.broadcast and options.reconcile:
        raise RotationError("broadcast and reconciliation are mutually exclusive")
    if options.broadcast:
        if (
            not isinstance(options.confirmation_digest, str)
            or DIGEST.fullmatch(options.confirmation_digest) is None
            or options.state_file is None
            or options.receipt_out is None
            or isinstance(options.reviewed_finalized_block, bool)
            or not isinstance(options.reviewed_finalized_block, int)
            or options.reviewed_finalized_block <= 0
            or not isinstance(options.reviewed_finalized_hash, str)
            or HASH.fullmatch(options.reviewed_finalized_hash) is None
            or isinstance(options.reviewed_coldkey_nonce, bool)
            or not isinstance(options.reviewed_coldkey_nonce, int)
            or options.reviewed_coldkey_nonce < 0
            or isinstance(options.approval_valid_until_block, bool)
            or not isinstance(options.approval_valid_until_block, int)
            or options.approval_valid_until_block
            != options.reviewed_finalized_block + APPROVAL_LIFETIME_BLOCKS
        ):
            raise RotationError(
                "broadcast requires the exact reviewed finalized block/hash, "
                "coldkey nonce, fixed approval expiry, prior confirmation digest, "
                "state file, and receipt output"
            )
        _require_canonical_artifact_paths(options)
        assert options.state_file is not None
        assert options.receipt_out is not None
        _require_new_output_path(options.state_file, label="state")
        _require_new_output_path(options.receipt_out, label="receipt")
    elif options.reconcile:
        if (
            options.state_file is None
            or options.receipt_out is None
            or any(
                value is not None
                for value in (
                    options.confirmation_digest,
                    options.reviewed_finalized_block,
                    options.reviewed_finalized_hash,
                    options.reviewed_coldkey_nonce,
                    options.approval_valid_until_block,
                )
            )
        ):
            raise RotationError(
                "reconciliation requires only the canonical state and receipt paths"
            )
        _require_canonical_artifact_paths(options)
        _safe_parent(options.state_file, label="state")
        _safe_parent(options.receipt_out, label="receipt")
    elif any(
        value is not None
        for value in (
            options.confirmation_digest,
            options.state_file,
            options.receipt_out,
            options.reviewed_finalized_block,
            options.reviewed_finalized_hash,
            options.reviewed_coldkey_nonce,
            options.approval_valid_until_block,
        )
    ):
        raise RotationError(
            "confirmation and output paths require --broadcast or --reconcile"
        )


def _require_canonical_artifact_paths(options: Options) -> None:
    if options.state_file is None or options.receipt_out is None:
        raise RotationError("state and receipt paths are required")
    if options.state_file == options.receipt_out:
        raise RotationError("state and receipt paths must differ")
    if not options.state_file.is_absolute() or not options.receipt_out.is_absolute():
        raise RotationError("state and receipt paths must be absolute")
    expected_state, expected_receipt = _artifact_names(options)
    try:
        state_parent = options.state_file.parent.resolve(strict=True)
        receipt_parent = options.receipt_out.parent.resolve(strict=True)
    except OSError as exc:
        raise RotationError(
            "canonical durable rotation directory is unavailable"
        ) from exc
    if (
        state_parent != _canonical_attempt_dir(options)
        or receipt_parent != state_parent
        or options.state_file.name != expected_state
        or options.receipt_out.name != expected_receipt
    ):
        raise RotationError(
            "state and receipt must use the canonical deterministic "
            "old-hotkey attempt scope from the inspection"
        )


def _safe_parent(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise RotationError(f"{label} path must be absolute")
    try:
        info = path.parent.lstat()
    except OSError as exc:
        raise RotationError(f"{label} parent directory is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise RotationError(f"{label} parent directory is not owner-controlled")


def _require_new_output_path(path: Path, *, label: str) -> None:
    _safe_parent(path, label=label)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RotationError(f"{label} path cannot be inspected safely") from exc
    raise RotationError(
        f"{label} path already exists; this rotation attempt cannot be retried"
    )


def _read_private_document(path: Path, *, label: str) -> dict[str, Any]:
    _safe_parent(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RotationError(f"{label} is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size <= 0
            or info.st_size > MAX_PRIVATE_ARTIFACT_BYTES
        ):
            raise RotationError(
                f"{label} is not a private owner-controlled bounded file"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_PRIVATE_ARTIFACT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > MAX_PRIVATE_ARTIFACT_BYTES:
        raise RotationError(f"{label} exceeds its size cap")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RotationError(f"{label} has duplicate JSON keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RotationError(f"{label} has a non-finite number")
            ),
        )
    except RotationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RotationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict) or payload != _canonical(document) + b"\n":
        raise RotationError(f"{label} is not canonical JSON")
    return document


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_create(path: Path, document: dict[str, Any]) -> None:
    _require_new_output_path(path, label="output")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(_canonical(document) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_replace_private(path: Path, document: dict[str, Any]) -> None:
    _safe_parent(path, label="state")
    try:
        info = path.lstat()
    except OSError as exc:
        raise RotationNotProven(
            "owner-only attempt state disappeared after signing"
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise RotationNotProven("owner-only attempt state changed after signing")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(_canonical(document) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def inspect_rotation(options: Options, runtime: Runtime) -> Inspection:
    """Compose and inspect one call without accessing the secret coldkey."""
    wallet_coldkey = _key_address(runtime.wallet.coldkeypub)
    if wallet_coldkey != options.expected_coldkey:
        raise RotationError("named wallet coldkey differs from --expected-coldkey")
    wallet_new_hotkey = _key_address(runtime.new_wallet.hotkeypub)
    if wallet_new_hotkey != options.new_hotkey:
        raise RotationError("named new-hotkey wallet differs from --new-hotkey")
    try:
        finalized_hash = _hash(
            runtime.substrate.get_chain_finalised_head(),
            label="finalized head",
        )
        finalized_block = int(runtime.substrate.get_block_number(finalized_hash))
        canonical_hash = _hash(
            runtime.substrate.get_block_hash(finalized_block),
            label="canonical finalized block",
        )
        genesis_hash = _hash(
            runtime.substrate.get_block_hash(0),
            label="Finney genesis block",
        )
        runtime_version = runtime.substrate.get_block_runtime_version(finalized_hash)
        nonce = runtime.substrate.get_account_next_index(options.expected_coldkey)
        metagraph = runtime.subtensor.metagraph(NETUID, block=finalized_block)
        owner_hotkey = str(
            runtime.subtensor.get_subnet_owner_hotkey(
                NETUID,
                block=finalized_block,
            )
            or ""
        )
        old_owner = _storage_value(
            runtime.subtensor,
            name="Owner",
            params=[options.old_hotkey],
            block=finalized_block,
        )
        new_owner = _storage_value(
            runtime.subtensor,
            name="Owner",
            params=[options.new_hotkey],
            block=finalized_block,
        )
        pending_coldkey_swap = _storage_value(
            runtime.subtensor,
            name="ColdkeySwapAnnouncements",
            params=[options.expected_coldkey],
            block=finalized_block,
        )
        key_swap_cost_rao = _rao(
            _storage_value(
                runtime.subtensor,
                name="KeySwapOnSubnetCost",
                params=[],
                block=finalized_block,
            ),
            label="KeySwapOnSubnetCost",
        )
        account_value = runtime.substrate.query(
            module="System",
            storage_function="Account",
            params=[options.expected_coldkey],
            block_hash=finalized_hash,
        )
        account = getattr(account_value, "value", account_value)
        coldkey_free_balance_rao = _rao(
            account["data"]["free"],
            label="coldkey free balance",
        )
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError(
            "cannot inspect the finalized SN39 rotation boundary"
        ) from exc
    if (
        finalized_block <= 0
        or canonical_hash != finalized_hash
        or genesis_hash != FINNEY_GENESIS_HASH
        or not isinstance(runtime_version, dict)
        or isinstance(nonce, bool)
        or not isinstance(nonce, int)
        or nonce < 0
    ):
        raise RotationError(
            "finalized head, pinned Finney genesis, or coldkey nonce is malformed"
        )
    if options.broadcast:
        assert options.reviewed_finalized_block is not None
        assert options.reviewed_finalized_hash is not None
        assert options.reviewed_coldkey_nonce is not None
        assert options.approval_valid_until_block is not None
        try:
            reviewed_canonical_hash = _hash(
                runtime.substrate.get_block_hash(options.reviewed_finalized_block),
                label="reviewed finalized block",
            )
        except Exception as exc:
            raise RotationError(
                "cannot prove the reviewed finalized snapshot is still canonical"
            ) from exc
        if (
            reviewed_canonical_hash != options.reviewed_finalized_hash.lower()
            or finalized_block < options.reviewed_finalized_block
            or finalized_block + ERA_PERIOD_BLOCKS - 1
            > options.approval_valid_until_block
            or nonce != options.reviewed_coldkey_nonce
        ):
            raise RotationError(
                "reviewed finalized snapshot, approval lifetime, or coldkey nonce "
                "changed before signing"
            )
        reviewed_finalized_block = options.reviewed_finalized_block
        reviewed_finalized_hash = options.reviewed_finalized_hash.lower()
        reviewed_coldkey_nonce = options.reviewed_coldkey_nonce
        approval_valid_until_block = options.approval_valid_until_block
    else:
        reviewed_finalized_block = finalized_block
        reviewed_finalized_hash = finalized_hash
        reviewed_coldkey_nonce = nonce
        approval_valid_until_block = finalized_block + APPROVAL_LIFETIME_BLOCKS
    spec_version = runtime_version.get("specVersion")
    transaction_version = runtime_version.get("transactionVersion")
    if (
        isinstance(spec_version, bool)
        or not isinstance(spec_version, int)
        or spec_version < 0
        or isinstance(transaction_version, bool)
        or not isinstance(transaction_version, int)
        or transaction_version < 0
    ):
        raise RotationError("finalized runtime identity is malformed")
    uids = [int(value) for value in _sequence(getattr(metagraph, "uids", None))]
    hotkeys = [str(value) for value in _sequence(getattr(metagraph, "hotkeys", None))]
    if (
        int(getattr(metagraph, "block", -1)) != finalized_block
        or len(uids) != len(hotkeys)
        or len(set(uids)) != len(uids)
        or len(set(hotkeys)) != len(hotkeys)
        or hotkeys.count(options.old_hotkey) != 1
        or options.new_hotkey in hotkeys
    ):
        raise RotationError(
            "old hotkey is not uniquely registered or new hotkey is not fresh"
        )
    old_uid = uids[hotkeys.index(options.old_hotkey)]
    if old_uid != options.expected_uid:
        raise RotationError("old hotkey UID differs from --expected-uid")
    normalized_old_owner = (
        None if old_owner in (None, "", UNOWNED_HOTKEY_OWNER) else str(old_owner)
    )
    normalized_new_owner = (
        None if new_owner in (None, "", UNOWNED_HOTKEY_OWNER) else str(new_owner)
    )
    if normalized_old_owner != options.expected_coldkey:
        raise RotationError("expected coldkey does not own the old hotkey")
    if normalized_new_owner is not None:
        raise RotationError("new hotkey already has an on-chain owner")
    if pending_coldkey_swap is not None:
        raise RotationError("owner coldkey has a pending swap announcement")
    reviewed_maximum_estimated_spend_rao = (
        key_swap_cost_rao + options.max_transaction_fee_rao
    )
    if coldkey_free_balance_rao < reviewed_maximum_estimated_spend_rao:
        raise RotationError(
            "coldkey balance is below the reviewed swap cost and fee-estimate ceiling"
        )
    if options.role == "owner-burn":
        if owner_hotkey != options.old_hotkey:
            raise RotationError("owner-burn role does not name the subnet owner hotkey")
    elif owner_hotkey == options.old_hotkey:
        raise RotationError("rewarded role cannot rotate the subnet owner hotkey")
    try:
        call = runtime.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="swap_hotkey_v2",
            call_params={
                "hotkey": options.old_hotkey,
                "new_hotkey": options.new_hotkey,
                "netuid": NETUID,
                "keep_stake": options.keep_stake,
            },
            block_hash=finalized_hash,
        )
        call_bytes = _call_hex(call)
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError(
            "runtime metadata cannot compose the exact swap_hotkey_v2 call"
        ) from exc
    approval = {
        "schema": REVIEW_SCHEMA,
        "execution_bundle": _validate_execution_context(options),
        "network": NETWORK,
        "genesis_hash": genesis_hash,
        "runtime_spec_version": spec_version,
        "runtime_transaction_version": transaction_version,
        "authority_host": options.authority_host,
        "authority_uid": options.authority_uid,
        "netuid": NETUID,
        "role": options.role,
        "signer_coldkey": options.expected_coldkey,
        "old_hotkey": options.old_hotkey,
        "new_hotkey": options.new_hotkey,
        "expected_uid": options.expected_uid,
        "keep_stake": options.keep_stake,
        "era_period_blocks": ERA_PERIOD_BLOCKS,
        "reviewed_finalized_block": reviewed_finalized_block,
        "reviewed_finalized_hash": reviewed_finalized_hash,
        "reviewed_coldkey_nonce": reviewed_coldkey_nonce,
        "approval_valid_until_block": approval_valid_until_block,
        "key_swap_cost_rao": key_swap_cost_rao,
        "coldkey_free_balance_rao": coldkey_free_balance_rao,
        "reviewed_transaction_fee_estimate_ceiling_rao": (
            options.max_transaction_fee_rao
        ),
        "reviewed_maximum_estimated_spend_rao": (reviewed_maximum_estimated_spend_rao),
        "on_chain_spend_cap_enforced": False,
        "cost_authorization_model": COST_AUTHORIZATION_MODEL,
        "call": "SubtensorModule.swap_hotkey_v2",
        "call_hex": call_bytes,
    }
    observation = {
        "finalized_block": finalized_block,
        "finalized_block_hash": finalized_hash,
        "old_uid": old_uid,
        "subnet_owner_hotkey": owner_hotkey,
        "coldkey_nonce": nonce,
        "old_hotkey_owner": normalized_old_owner,
        "new_hotkey_owner": normalized_new_owner,
        "pending_coldkey_swap": None,
        "old_hotkey_registered": True,
        "new_hotkey_fresh": True,
    }
    return Inspection(call=call, approval=approval, observation=observation)


def _signed_extrinsic_hash(signed: Any) -> str:
    try:
        return _hash(signed.extrinsic_hash, label="signed extrinsic")
    except (AttributeError, TypeError, ValueError) as exc:
        raise RotationError("signed rotation has no canonical hash") from exc


def _prove_new_hotkey_possession(
    options: Options,
    runtime: Runtime,
    inspection: Inspection,
) -> dict[str, str]:
    """Unlock only the new hotkey and prove it controls the reviewed identity."""
    challenge = b"cathedral-sn39-new-hotkey-possession-v1:" + _canonical(
        inspection.approval
    )
    try:
        key = runtime.new_wallet.hotkey
        if _key_address(key) != options.new_hotkey:
            raise RotationError(
                "unlocked new hotkey differs from the approved identity"
            )
        raw_signature = key.sign(challenge)
        if isinstance(raw_signature, bytes):
            signature = "0x" + raw_signature.hex()
        else:
            signature = str(raw_signature)
        if SIGNATURE.fullmatch(signature) is None:
            raise RotationError("new hotkey returned a malformed proof signature")
        if key.verify(challenge, signature) is not True:
            raise RotationError("new hotkey proof-of-possession did not verify")
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError(
            "cannot prove control of the approved new hotkey; nothing was signed "
            "by the coldkey"
        ) from exc
    return {
        "challenge_sha256": ("sha256:" + hashlib.sha256(challenge).hexdigest()),
        "approval_digest": inspection.confirmation_digest,
        "signature": signature.lower(),
    }


def _require_best_runtime_identity(
    runtime: Runtime,
    inspection: Inspection,
) -> None:
    """Fail if any observable signing boundary differs from the approval."""
    try:
        best_hash = _hash(
            runtime.substrate.get_chain_head(),
            label="best chain head",
        )
        best_block = int(runtime.substrate.get_block_number(best_hash))
        canonical_best_hash = _hash(
            runtime.substrate.get_block_hash(best_block),
            label="canonical best chain head",
        )
        genesis_hash = _hash(
            runtime.substrate.get_block_hash(0),
            label="current Finney genesis",
        )
        current = runtime.substrate.get_block_runtime_version(best_hash)
        current_nonce = runtime.substrate.get_account_next_index(
            inspection.approval["signer_coldkey"]
        )
        current_call = runtime.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="swap_hotkey_v2",
            call_params={
                "hotkey": inspection.approval["old_hotkey"],
                "new_hotkey": inspection.approval["new_hotkey"],
                "netuid": NETUID,
                "keep_stake": inspection.approval["keep_stake"],
            },
            block_hash=best_hash,
        )
        current_call_hex = _call_hex(current_call)
        current_key_swap_cost_rao = _rao(
            _storage_value(
                runtime.subtensor,
                name="KeySwapOnSubnetCost",
                params=[],
                block=best_block,
            ),
            label="current KeySwapOnSubnetCost",
        )
        current_old_owner = _storage_value(
            runtime.subtensor,
            name="Owner",
            params=[inspection.approval["old_hotkey"]],
            block=best_block,
        )
        current_new_owner = _storage_value(
            runtime.subtensor,
            name="Owner",
            params=[inspection.approval["new_hotkey"]],
            block=best_block,
        )
        current_pending_coldkey_swap = _storage_value(
            runtime.subtensor,
            name="ColdkeySwapAnnouncements",
            params=[inspection.approval["signer_coldkey"]],
            block=best_block,
        )
        current_metagraph = runtime.subtensor.metagraph(NETUID, block=best_block)
        current_owner_hotkey = str(
            runtime.subtensor.get_subnet_owner_hotkey(
                NETUID,
                block=best_block,
            )
            or ""
        )
        account_value = runtime.substrate.query(
            module="System",
            storage_function="Account",
            params=[inspection.approval["signer_coldkey"]],
            block_hash=best_hash,
        )
        account = getattr(account_value, "value", account_value)
        current_free_balance_rao = _rao(
            account["data"]["free"],
            label="current coldkey free balance",
        )
        current_uids = [
            int(value) for value in _sequence(getattr(current_metagraph, "uids", None))
        ]
        current_hotkeys = [
            str(value)
            for value in _sequence(getattr(current_metagraph, "hotkeys", None))
        ]
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError("cannot prove the current signing boundary") from exc
    normalized_old_owner = (
        None
        if current_old_owner in (None, "", UNOWNED_HOTKEY_OWNER)
        else str(current_old_owner)
    )
    normalized_new_owner = (
        None
        if current_new_owner in (None, "", UNOWNED_HOTKEY_OWNER)
        else str(current_new_owner)
    )
    old_hotkey = inspection.approval["old_hotkey"]
    expected_uid = inspection.approval["expected_uid"]
    role = inspection.approval["role"]
    if (
        not isinstance(current, dict)
        or isinstance(best_block, bool)
        or canonical_best_hash != best_hash
        or genesis_hash != FINNEY_GENESIS_HASH
        or best_block < inspection.approval["reviewed_finalized_block"]
        or best_block + ERA_PERIOD_BLOCKS - 1
        > inspection.approval["approval_valid_until_block"]
        or isinstance(current_nonce, bool)
        or current_nonce != inspection.approval["reviewed_coldkey_nonce"]
        or current.get("specVersion") != inspection.approval["runtime_spec_version"]
        or current.get("transactionVersion")
        != inspection.approval["runtime_transaction_version"]
        or current_call_hex != inspection.approval["call_hex"]
        or current_key_swap_cost_rao != inspection.approval["key_swap_cost_rao"]
        or current_free_balance_rao
        < inspection.approval["reviewed_maximum_estimated_spend_rao"]
        or normalized_old_owner != inspection.approval["signer_coldkey"]
        or normalized_new_owner is not None
        or current_pending_coldkey_swap is not None
        or int(getattr(current_metagraph, "block", -1)) != best_block
        or len(current_uids) != len(current_hotkeys)
        or len(set(current_uids)) != len(current_uids)
        or len(set(current_hotkeys)) != len(current_hotkeys)
        or current_hotkeys.count(old_hotkey) != 1
        or inspection.approval["new_hotkey"] in current_hotkeys
        or current_uids[current_hotkeys.index(old_hotkey)] != expected_uid
        or (role == "owner-burn" and current_owner_hotkey != old_hotkey)
        or (role == "rewarded" and current_owner_hotkey == old_hotkey)
    ):
        raise RotationError(
            "current signing head, nonce, lifetime, runtime, call, ownership, "
            "or economic state differs from the approved snapshot"
        )


def _estimate_transaction_fee_rao(
    options: Options,
    runtime: Runtime,
    inspection: Inspection,
    coldkey: Any,
    *,
    nonce: int,
    era_reference_block: int,
) -> int:
    era = {
        "period": ERA_PERIOD_BLOCKS,
        "current": era_reference_block,
    }
    try:
        payment = runtime.substrate.get_payment_info(
            call=inspection.call,
            keypair=coldkey,
            nonce=nonce,
            era=era,
            tip=0,
        )
        fee = _rao(payment["partial_fee"], label="transaction fee estimate")
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError(
            "cannot prove the exact rotation fee before signing the broadcast intent"
        ) from exc
    if fee > options.max_transaction_fee_rao:
        raise RotationError(
            "estimated rotation fee exceeds the separately approved ceiling"
        )
    if (
        inspection.approval["key_swap_cost_rao"] + fee
        > inspection.approval["coldkey_free_balance_rao"]
    ):
        raise RotationError(
            "coldkey balance cannot cover the reviewed swap cost and estimated fee"
        )
    return fee


def _finalized_fee_evidence(
    options: Options,
    receipt: Any,
    historical_execution: Any,
    *,
    receipt_fee_source: str,
) -> dict[str, Any]:
    """Verify actual finalized fee when either SDK receipt exposes it."""
    unavailable = object()
    observed: list[tuple[str, int]] = []
    for source, candidate in (
        (receipt_fee_source, receipt),
        ("canonical_historical_receipt", historical_execution),
    ):
        try:
            raw = getattr(candidate, "total_fee_amount", unavailable)
        except Exception as exc:
            raise RotationNotProven(
                "finalized rotation exposes an unreadable actual transaction fee"
            ) from exc
        if raw is unavailable or raw is None:
            continue
        try:
            fee = _rao(raw, label="actual finalized transaction fee")
        except RotationError as exc:
            raise RotationNotProven(
                "finalized rotation exposes a malformed actual transaction fee"
            ) from exc
        observed.append((source, fee))
    amounts = {amount for _, amount in observed}
    if len(amounts) > 1:
        raise RotationNotProven(
            "finalized rotation receipts disagree on the actual transaction fee"
        )
    if not observed:
        return {
            "actual_transaction_fee_rao": None,
            "actual_transaction_fee_source": "NOT_EXPOSED_BY_SDK",
            "actual_fee_within_reviewed_estimate_ceiling": None,
        }
    fee = observed[0][1]
    return {
        "actual_transaction_fee_rao": fee,
        "actual_transaction_fee_source": "+".join(source for source, _ in observed),
        "actual_fee_within_reviewed_estimate_ceiling": (
            fee <= options.max_transaction_fee_rao
        ),
    }


def _event_value(raw: Any) -> dict[str, Any] | None:
    document = raw if isinstance(raw, dict) else getattr(raw, "value", None)
    if not isinstance(document, dict):
        return None
    event = document.get("event")
    if not isinstance(event, dict):
        event = getattr(event, "value", None)
    return event if isinstance(event, dict) else None


def _chain_call_arg(call: dict[str, Any], name: str) -> Any:
    for item in call.get("call_args") or ():
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("value")
    return None


def _receipt_artifact(
    options: Options,
    runtime: Runtime,
    receipt: Any,
    *,
    signed_hash: str,
    receipt_fee_source: str = "submitted_receipt",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the exact receipt object consumed by the launch verifiers."""
    if receipt_fee_source not in {
        "submitted_receipt",
        "recovered_historical_receipt",
    }:
        raise RotationError("finalized fee receipt source is invalid")
    try:
        receipt_hash = _hash(
            getattr(receipt, "extrinsic_hash", None),
            label="receipt extrinsic",
        )
        block_hash = _hash(
            getattr(receipt, "block_hash", None),
            label="receipt block",
        )
        block_number_raw = getattr(receipt, "block_number", None)
        block_number = (
            int(block_number_raw)
            if block_number_raw is not None
            else int(runtime.substrate.get_block_number(block_hash))
        )
        canonical_hash = _hash(
            runtime.substrate.get_block_hash(block_number),
            label="canonical receipt block",
        )
        extrinsic_index = int(getattr(receipt, "extrinsic_idx", -1))
        finalized = getattr(receipt, "finalized", None)
        success = getattr(receipt, "is_success", None)
        error_message = getattr(receipt, "error_message", None)
        events = getattr(receipt, "triggered_events", None)
        block = runtime.substrate.get_block(block_hash=block_hash)
        historical_execution = runtime.substrate.retrieve_extrinsic_by_hash(
            block_hash,
            signed_hash,
        )
        timestamp_value = runtime.substrate.query(
            module="Timestamp",
            storage_function="Now",
            block_hash=block_hash,
        )
        timestamp_ms = getattr(timestamp_value, "value", timestamp_value)
    except Exception as exc:
        raise RotationNotProven(
            f"signed rotation {signed_hash} has no complete finalized receipt"
        ) from exc
    if (
        receipt_hash != signed_hash
        or canonical_hash != block_hash
        or block_number <= 0
        or extrinsic_index < 0
        or finalized is not True
    ):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has an ambiguous finalized receipt"
        )
    if not isinstance(block, dict):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has no canonical finalized block"
        )
    extrinsics = block.get("extrinsics")
    if not isinstance(extrinsics, (list, tuple)):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has no canonical finalized extrinsics"
        )
    if extrinsic_index >= len(extrinsics):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has an unavailable canonical index"
        )
    matching_indexes = [
        index
        for index, item in enumerate(extrinsics)
        if isinstance(getattr(item, "value", None), dict)
        and str(item.value.get("extrinsic_hash", "")).lower() == signed_hash
    ]
    observed = getattr(extrinsics[extrinsic_index], "value", None)
    if (
        matching_indexes != [extrinsic_index]
        or not isinstance(observed, dict)
        or str(observed.get("extrinsic_hash", "")).lower() != signed_hash
    ):
        raise RotationError(
            f"rotation {signed_hash} contradicts its canonical block index"
        )
    call = observed.get("call")
    exact_call = bool(
        observed.get("address") == options.expected_coldkey
        and isinstance(call, dict)
        and call.get("call_module") == "SubtensorModule"
        and call.get("call_function") == "swap_hotkey_v2"
        and _chain_call_arg(call, "hotkey") == options.old_hotkey
        and _chain_call_arg(call, "new_hotkey") == options.new_hotkey
        and _chain_call_arg(call, "netuid") == NETUID
        and _chain_call_arg(call, "keep_stake") is options.keep_stake
    )
    if not exact_call:
        raise RotationError(
            f"rotation {signed_hash} canonical call differs from the approved swap"
        )
    historical_success = getattr(historical_execution, "is_success", None)
    historical_index = getattr(historical_execution, "extrinsic_idx", None)
    if (
        historical_execution is None
        or not isinstance(historical_success, bool)
        or isinstance(historical_index, bool)
    ):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has no historical execution proof"
        )
    try:
        historical_execution_index = int(historical_index)
    except (TypeError, ValueError):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has no historical execution index"
        ) from None
    if (
        historical_success is not True
        or getattr(historical_execution, "error_message", None) is not None
        or historical_execution_index != extrinsic_index
    ):
        raise RotationError(
            f"rotation {signed_hash} failed or contradicts its historical execution"
        )
    if success is False:
        raise RotationError(
            f"rotation {signed_hash} finalized without successful execution"
        )
    if (
        success is not True
        or error_message is not None
        or not isinstance(events, (list, tuple))
        or isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms <= 0
    ):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has an ambiguous finalized receipt"
        )
    try:
        block_timestamp = (
            datetime.fromtimestamp(timestamp_ms / 1000, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OSError, OverflowError, ValueError):
        raise RotationNotProven(
            f"signed rotation {signed_hash} has invalid finalized timestamp"
        ) from None
    matching_events: list[dict[str, Any]] = []
    for raw in events:
        event = _event_value(raw)
        if (
            isinstance(event, dict)
            and event.get("module_id") == "SubtensorModule"
            and event.get("event_id") == "HotkeySwappedOnSubnet"
            and isinstance(event.get("attributes"), dict)
            and event["attributes"].get("coldkey") == options.expected_coldkey
            and event["attributes"].get("old_hotkey") == options.old_hotkey
            and event["attributes"].get("new_hotkey") == options.new_hotkey
            and event["attributes"].get("netuid") == NETUID
        ):
            matching_events.append(event)
    if len(matching_events) != 1:
        raise RotationNotProven(
            f"signed rotation {signed_hash} has no unique matching finalized event"
        )
    return (
        {
            "call": "swap_hotkey_v2",
            "extrinsic_hash": signed_hash,
            "block_hash": block_hash,
            "block_number": block_number,
            "block_timestamp": block_timestamp,
            "extrinsic_index": extrinsic_index,
            "coldkey": options.expected_coldkey,
            "old_hotkey": options.old_hotkey,
            "new_hotkey": options.new_hotkey,
            "netuid": NETUID,
            "keep_stake": options.keep_stake,
            "event": "HotkeySwappedOnSubnet",
        },
        _finalized_fee_evidence(
            options,
            receipt,
            historical_execution,
            receipt_fee_source=receipt_fee_source,
        ),
    )


def _validate_pending_attempt(
    options: Options,
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_state_fields = {
        "schema",
        "phase",
        "confirmation_digest",
        "approval",
        "new_hotkey_possession",
        "economic_boundary",
        "signed_intent",
    }
    approval = state.get("approval")
    possession = state.get("new_hotkey_possession")
    economic = state.get("economic_boundary")
    signed = state.get("signed_intent")
    expected_approval_fields = {
        "schema",
        "execution_bundle",
        "network",
        "genesis_hash",
        "runtime_spec_version",
        "runtime_transaction_version",
        "authority_host",
        "authority_uid",
        "netuid",
        "role",
        "signer_coldkey",
        "old_hotkey",
        "new_hotkey",
        "expected_uid",
        "keep_stake",
        "era_period_blocks",
        "reviewed_finalized_block",
        "reviewed_finalized_hash",
        "reviewed_coldkey_nonce",
        "approval_valid_until_block",
        "key_swap_cost_rao",
        "coldkey_free_balance_rao",
        "reviewed_transaction_fee_estimate_ceiling_rao",
        "reviewed_maximum_estimated_spend_rao",
        "on_chain_spend_cap_enforced",
        "cost_authorization_model",
        "call",
        "call_hex",
    }
    if (
        set(state) != expected_state_fields
        or state.get("schema") != STATE_SCHEMA
        or state.get("phase") != "broadcast_pending"
        or not isinstance(approval, dict)
        or set(approval) != expected_approval_fields
        or not isinstance(possession, dict)
        or set(possession) != {"challenge_sha256", "approval_digest", "signature"}
        or not isinstance(economic, dict)
        or set(economic)
        != {
            "key_swap_cost_rao",
            "estimated_transaction_fee_rao",
            "reviewed_transaction_fee_estimate_ceiling_rao",
            "reviewed_maximum_estimated_spend_rao",
            "on_chain_spend_cap_enforced",
            "cost_authorization_model",
        }
        or not isinstance(signed, dict)
        or set(signed)
        != {
            "extrinsic_hash",
            "nonce",
            "era_reference_block",
            "era_reference_hash",
            "era_period_blocks",
        }
    ):
        raise RotationError("pending rotation attempt schema or fields differ")
    confirmation = _digest(approval)
    call_hex = str(approval.get("call_hex", ""))
    if (
        state.get("confirmation_digest") != confirmation
        or possession.get("approval_digest") != confirmation
        or DIGEST.fullmatch(str(possession.get("challenge_sha256"))) is None
        or SIGNATURE.fullmatch(str(possession.get("signature"))) is None
        or approval.get("schema") != REVIEW_SCHEMA
        or approval.get("execution_bundle") != _validate_execution_context(options)
        or approval.get("network") != NETWORK
        or approval.get("genesis_hash") != FINNEY_GENESIS_HASH
        or approval.get("authority_host") != options.authority_host
        or approval.get("authority_uid") != options.authority_uid
        or approval.get("netuid") != NETUID
        or approval.get("role") != options.role
        or approval.get("signer_coldkey") != options.expected_coldkey
        or approval.get("old_hotkey") != options.old_hotkey
        or approval.get("new_hotkey") != options.new_hotkey
        or approval.get("expected_uid") != options.expected_uid
        or approval.get("keep_stake") is not options.keep_stake
        or approval.get("era_period_blocks") != ERA_PERIOD_BLOCKS
        or approval.get("reviewed_transaction_fee_estimate_ceiling_rao")
        != options.max_transaction_fee_rao
        or approval.get("on_chain_spend_cap_enforced") is not False
        or approval.get("cost_authorization_model") != COST_AUTHORIZATION_MODEL
        or approval.get("call") != "SubtensorModule.swap_hotkey_v2"
        or re.fullmatch(r"0x[0-9a-f]+", call_hex) is None
        or len(call_hex) <= 2
        or len(call_hex[2:]) % 2
    ):
        raise RotationError(
            "pending rotation attempt differs from the approved target or bundle"
        )
    integer_fields = (
        "runtime_spec_version",
        "runtime_transaction_version",
        "reviewed_finalized_block",
        "reviewed_coldkey_nonce",
        "approval_valid_until_block",
        "key_swap_cost_rao",
        "coldkey_free_balance_rao",
        "reviewed_transaction_fee_estimate_ceiling_rao",
        "reviewed_maximum_estimated_spend_rao",
    )
    if any(
        isinstance(approval.get(field), bool)
        or not isinstance(approval.get(field), int)
        or approval[field] < 0
        for field in integer_fields
    ):
        raise RotationError("pending rotation approval has malformed integer fields")
    reviewed_block = approval["reviewed_finalized_block"]
    valid_until = approval["approval_valid_until_block"]
    nonce = approval["reviewed_coldkey_nonce"]
    era_reference = signed.get("era_reference_block")
    if (
        reviewed_block <= 0
        or valid_until != reviewed_block + APPROVAL_LIFETIME_BLOCKS
        or HASH.fullmatch(str(approval.get("reviewed_finalized_hash"))) is None
        or approval["reviewed_maximum_estimated_spend_rao"]
        != approval["key_swap_cost_rao"]
        + approval["reviewed_transaction_fee_estimate_ceiling_rao"]
        or approval["coldkey_free_balance_rao"]
        < approval["reviewed_maximum_estimated_spend_rao"]
        or economic.get("key_swap_cost_rao") != approval["key_swap_cost_rao"]
        or economic.get("reviewed_transaction_fee_estimate_ceiling_rao")
        != approval["reviewed_transaction_fee_estimate_ceiling_rao"]
        or economic.get("reviewed_maximum_estimated_spend_rao")
        != approval["reviewed_maximum_estimated_spend_rao"]
        or economic.get("on_chain_spend_cap_enforced") is not False
        or economic.get("cost_authorization_model") != COST_AUTHORIZATION_MODEL
        or isinstance(economic.get("estimated_transaction_fee_rao"), bool)
        or not isinstance(economic.get("estimated_transaction_fee_rao"), int)
        or not 0
        <= economic["estimated_transaction_fee_rao"]
        <= approval["reviewed_transaction_fee_estimate_ceiling_rao"]
        or HASH.fullmatch(str(signed.get("extrinsic_hash"))) is None
        or signed.get("nonce") != nonce
        or isinstance(era_reference, bool)
        or not isinstance(era_reference, int)
        or not reviewed_block <= era_reference
        or era_reference + ERA_PERIOD_BLOCKS - 1 > valid_until
        or HASH.fullmatch(str(signed.get("era_reference_hash"))) is None
        or (
            era_reference == reviewed_block
            and signed.get("era_reference_hash") != approval["reviewed_finalized_hash"]
        )
        or signed.get("era_period_blocks") != ERA_PERIOD_BLOCKS
    ):
        raise RotationError("pending rotation economic or signed intent differs")
    return approval, signed


def _verify_recorded_possession(
    options: Options,
    runtime: Runtime,
    *,
    approval: dict[str, Any],
    possession: dict[str, Any],
) -> None:
    challenge = b"cathedral-sn39-new-hotkey-possession-v1:" + _canonical(approval)
    expected_challenge = "sha256:" + hashlib.sha256(challenge).hexdigest()
    signature = str(possession.get("signature", ""))
    try:
        public = runtime.new_wallet.hotkeypub
        if (
            _key_address(public) != options.new_hotkey
            or possession.get("challenge_sha256") != expected_challenge
            or SIGNATURE.fullmatch(signature) is None
            or public.verify(challenge, signature) is not True
        ):
            raise RotationError(
                "pending rotation new-hotkey possession proof is invalid"
            )
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError(
            "pending rotation new-hotkey possession proof is unavailable"
        ) from exc


def _historical_rotation_receipt(
    runtime: Runtime,
    *,
    signed_hash: str,
    reviewed_finalized_block: int,
    reviewed_finalized_hash: str,
    era_reference_block: int,
    era_reference_hash: str,
    era_period_blocks: int,
) -> Any:
    """Locate one exact signed hash inside its already-recorded mortal window."""
    try:
        finalized_hash = _hash(
            runtime.substrate.get_chain_finalised_head(),
            label="reconciliation finalized head",
        )
        finalized_block = int(runtime.substrate.get_block_number(finalized_hash))
        canonical_finalized_hash = _hash(
            runtime.substrate.get_block_hash(finalized_block),
            label="canonical reconciliation finalized head",
        )
        genesis_hash = _hash(
            runtime.substrate.get_block_hash(0),
            label="reconciliation Finney genesis",
        )
        canonical_reviewed_hash = _hash(
            runtime.substrate.get_block_hash(reviewed_finalized_block),
            label="canonical reviewed finalized block",
        )
        canonical_reference_hash = _hash(
            runtime.substrate.get_block_hash(era_reference_block),
            label="canonical reconciliation era reference",
        )
    except Exception as exc:
        raise RotationNotProven(
            "cannot read the finalized head for rotation reconciliation"
        ) from exc
    if (
        finalized_block <= 0
        or canonical_finalized_hash != finalized_hash
        or genesis_hash != FINNEY_GENESIS_HASH
        or canonical_reviewed_hash != reviewed_finalized_hash
        or canonical_reference_hash != era_reference_hash
    ):
        raise RotationError(
            "rotation reconciliation is not on the exact approved canonical "
            "pinned Finney history"
        )
    last_possible_block = era_reference_block + era_period_blocks - 1
    matches: list[tuple[int, str, int]] = []
    for block_number in range(
        era_reference_block,
        min(finalized_block, last_possible_block) + 1,
    ):
        try:
            block_hash = _hash(
                runtime.substrate.get_block_hash(block_number),
                label="reconciliation block",
            )
            block = runtime.substrate.get_block(block_hash=block_hash)
        except Exception as exc:
            raise RotationNotProven(
                "canonical rotation reconciliation window is unavailable"
            ) from exc
        extrinsics = block.get("extrinsics") if isinstance(block, dict) else None
        if not isinstance(extrinsics, (list, tuple)):
            raise RotationNotProven(
                "canonical rotation reconciliation block is malformed"
            )
        for index, item in enumerate(extrinsics):
            value = getattr(item, "value", None)
            if (
                isinstance(value, dict)
                and str(value.get("extrinsic_hash", "")).lower() == signed_hash
            ):
                matches.append((block_number, block_hash, index))
    if len(matches) > 1:
        raise RotationError(
            "signed rotation hash appears more than once in its mortal window"
        )
    if not matches:
        if finalized_block < last_possible_block:
            raise RotationNotProven(
                "signed rotation mortal window is not fully finalized and has no match"
            )
        raise RotationError(
            "signed rotation was not included anywhere in its finalized mortal window"
        )
    block_number, block_hash, extrinsic_index = matches[0]
    try:
        historical = runtime.substrate.retrieve_extrinsic_by_hash(
            block_hash,
            signed_hash,
        )
        events = getattr(historical, "triggered_events", None)
        total_fee_amount = getattr(historical, "total_fee_amount", None)
    except Exception as exc:
        raise RotationNotProven(
            "signed rotation historical execution is unavailable"
        ) from exc
    return SimpleNamespace(
        extrinsic_hash=signed_hash,
        block_hash=block_hash,
        block_number=block_number,
        extrinsic_idx=extrinsic_index,
        finalized=True,
        is_success=getattr(historical, "is_success", None),
        error_message=getattr(historical, "error_message", None),
        triggered_events=events,
        total_fee_amount=total_fee_amount,
    )


def _post_rotation_proof(
    options: Options,
    runtime: Runtime,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """Prove the finalized UID, owner, role, and lineage before proceeding."""
    block = artifact["block_number"]
    block_hash = artifact["block_hash"]
    try:
        canonical_hash = _hash(
            runtime.substrate.get_block_hash(block),
            label="canonical post-rotation block",
        )
        metagraph = runtime.subtensor.metagraph(NETUID, block=block)
        subnet_owner_hotkey = str(
            runtime.subtensor.get_subnet_owner_hotkey(NETUID, block=block) or ""
        )
        new_owner = _storage_value(
            runtime.subtensor,
            name="Owner",
            params=[options.new_hotkey],
            block=block,
        )
        last_swap = _storage_value(
            runtime.subtensor,
            name="LastHotkeySwapOnNetuid",
            params=[NETUID, options.expected_coldkey],
            block=block,
        )
        successor = _storage_value(
            runtime.subtensor,
            name="HotkeySuccessor",
            params=[NETUID, options.new_hotkey],
            block=block,
        )
        root = _storage_value(
            runtime.subtensor,
            name="HotkeyRoot",
            params=[NETUID, options.new_hotkey],
            block=block,
        )
        old_successor = _storage_value(
            runtime.subtensor,
            name="HotkeySuccessor",
            params=[NETUID, options.old_hotkey],
            block=block,
        )
        old_root = _storage_value(
            runtime.subtensor,
            name="HotkeyRoot",
            params=[NETUID, options.old_hotkey],
            block=block,
        )
        pending_coldkey_swap = _storage_value(
            runtime.subtensor,
            name="ColdkeySwapAnnouncements",
            params=[options.expected_coldkey],
            block=block,
        )
        uids = [int(value) for value in _sequence(getattr(metagraph, "uids", None))]
        hotkeys = [
            str(value) for value in _sequence(getattr(metagraph, "hotkeys", None))
        ]
        observed_block = int(getattr(metagraph, "block", -1))
        last_swap_block = int(last_swap)
    except Exception as exc:
        raise RotationNotProven(
            "finalized rotation post-state and lineage are unavailable"
        ) from exc
    if (
        canonical_hash != block_hash
        or observed_block != block
        or len(uids) != len(hotkeys)
        or len(set(uids)) != len(uids)
        or len(set(hotkeys)) != len(hotkeys)
    ):
        raise RotationNotProven("finalized rotation post-state is ambiguous")
    expected_root = str(old_root) if old_root not in (None, "") else options.old_hotkey
    role_matches = (
        subnet_owner_hotkey == options.new_hotkey
        if options.role == "owner-burn"
        else subnet_owner_hotkey != options.new_hotkey
    )
    if (
        hotkeys.count(options.new_hotkey) != 1
        or options.old_hotkey in hotkeys
        or uids[hotkeys.index(options.new_hotkey)] != options.expected_uid
        or str(new_owner) != options.expected_coldkey
        or last_swap_block != block
        or not role_matches
        or successor not in (None, "")
        or root in (None, "")
        or str(old_successor) != options.new_hotkey
        or str(root) != expected_root
        or pending_coldkey_swap is not None
    ):
        raise RotationError(
            "finalized rotation does not preserve the approved UID, owner, "
            "role, lineage, and coldkey boundary"
        )
    return {
        "block_number": block,
        "block_hash": block_hash,
        "uid": options.expected_uid,
        "hotkey": options.new_hotkey,
        "coldkey": options.expected_coldkey,
        "role": options.role,
        "subnet_owner_hotkey": subnet_owner_hotkey,
        "last_hotkey_swap_block": last_swap_block,
        "hotkey_root": str(root),
        "old_hotkey_successor": str(old_successor),
        "pending_coldkey_swap": None,
    }


def reconcile_attempt(options: Options, runtime: Runtime) -> dict[str, Any]:
    """Complete a pending signed attempt from canonical chain history only."""
    assert options.state_file is not None
    assert options.receipt_out is not None
    state = _read_private_document(
        options.state_file,
        label="pending rotation state",
    )
    approval, signed_intent = _validate_pending_attempt(options, state)
    possession = state["new_hotkey_possession"]
    assert isinstance(possession, dict)
    _verify_recorded_possession(
        options,
        runtime,
        approval=approval,
        possession=possession,
    )
    signed_hash = str(signed_intent["extrinsic_hash"]).lower()
    receipt = _historical_rotation_receipt(
        runtime,
        signed_hash=signed_hash,
        reviewed_finalized_block=int(approval["reviewed_finalized_block"]),
        reviewed_finalized_hash=str(approval["reviewed_finalized_hash"]),
        era_reference_block=int(signed_intent["era_reference_block"]),
        era_reference_hash=str(signed_intent["era_reference_hash"]),
        era_period_blocks=int(signed_intent["era_period_blocks"]),
    )
    artifact, fee_evidence = _receipt_artifact(
        options,
        runtime,
        receipt,
        signed_hash=signed_hash,
        receipt_fee_source="recovered_historical_receipt",
    )
    try:
        options.receipt_out.lstat()
    except FileNotFoundError:
        try:
            _atomic_create(options.receipt_out, artifact)
        except Exception as exc:
            raise RotationNotProven(
                f"rotation {signed_hash} was recovered but its receipt could "
                "not be durably recorded"
            ) from exc
    except OSError as exc:
        raise RotationNotProven(
            "canonical rotation receipt path is unavailable"
        ) from exc
    else:
        existing = _read_private_document(
            options.receipt_out,
            label="recovered rotation receipt",
        )
        if existing != artifact:
            raise RotationError(
                "existing rotation receipt contradicts canonical chain history"
            )
    post_state = _post_rotation_proof(options, runtime, artifact)
    finalized_state = {
        **state,
        "phase": "finalized",
        "economic_boundary": {
            **state["economic_boundary"],
            **fee_evidence,
        },
        "receipt_sha256": _digest(artifact),
        "receipt": artifact,
        "post_rotation_proof": post_state,
    }
    try:
        _atomic_replace_private(options.state_file, finalized_state)
    except RotationNotProven:
        raise
    except Exception as exc:
        raise RotationNotProven(
            f"rotation {signed_hash} was recovered but its final state could "
            "not be durably recorded"
        ) from exc
    return {
        "schema": STATE_SCHEMA,
        "status": "PASS",
        "chain_write": False,
        "recovered_chain_write": True,
        "confirmation_digest": state["confirmation_digest"],
        "receipt_sha256": _digest(artifact),
        "receipt": artifact,
        "economic_boundary": finalized_state["economic_boundary"],
        "post_rotation_proof": post_state,
    }


def execute(options: Options, *, runtime: Runtime | None = None) -> dict[str, Any]:
    """Inspect by default; sign and submit only after exact digest confirmation."""
    _validate_options(options)
    active = (
        runtime
        if runtime is not None
        else _connect(
            options.wallet_name,
            options.new_wallet_name,
            options.new_wallet_hotkey,
        )
    )
    if options.reconcile:
        return reconcile_attempt(options, active)
    inspection = inspect_rotation(options, active)
    if not options.broadcast:
        state_name, receipt_name = _artifact_names(options)
        return {
            "schema": REVIEW_SCHEMA,
            "status": "INSPECT_ONLY",
            "chain_write": False,
            "signing": False,
            "confirmation_digest": inspection.confirmation_digest,
            "attempt_scope": {
                "id": _target_id(options),
                "state_filename": state_name,
                "receipt_filename": receipt_name,
            },
            "approval": inspection.approval,
            "observation": inspection.observation,
        }
    if options.confirmation_digest != inspection.confirmation_digest:
        raise RotationError(
            "confirmation digest differs from the current exact call review"
        )
    possession_proof = _prove_new_hotkey_possession(
        options,
        active,
        inspection,
    )
    try:
        coldkey = active.wallet.coldkey
    except Exception:
        raise RotationError(
            "cannot unlock the approved coldkey; nothing was signed by the "
            "coldkey, recorded, or submitted"
        ) from None
    signer = _key_address(coldkey)
    if signer != options.expected_coldkey:
        raise RotationError("unlocked coldkey differs from the approved signer")
    refreshed = inspect_rotation(options, active)
    if refreshed.confirmation_digest != inspection.confirmation_digest:
        raise RotationError(
            "rotation boundary changed while keys were unlocked; nothing was "
            "signed by the coldkey"
        )
    inspection = refreshed
    _require_best_runtime_identity(active, inspection)
    nonce = inspection.observation["coldkey_nonce"]
    era_reference = inspection.observation["finalized_block"]
    era_reference_hash = inspection.observation["finalized_block_hash"]
    estimated_transaction_fee_rao = _estimate_transaction_fee_rao(
        options,
        active,
        inspection,
        coldkey,
        nonce=nonce,
        era_reference_block=era_reference,
    )
    _require_best_runtime_identity(active, inspection)
    signed_era = {
        "period": ERA_PERIOD_BLOCKS,
        "current": era_reference,
    }
    try:
        signed = active.substrate.create_signed_extrinsic(
            call=inspection.call,
            keypair=coldkey,
            nonce=nonce,
            era=signed_era,
            tip=0,
        )
        signed_hash = _signed_extrinsic_hash(signed)
        _require_best_runtime_identity(active, inspection)
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError("cannot sign the exact approved rotation call") from exc
    attempt = {
        "schema": STATE_SCHEMA,
        "phase": "broadcast_pending",
        "confirmation_digest": inspection.confirmation_digest,
        "approval": inspection.approval,
        "new_hotkey_possession": possession_proof,
        "economic_boundary": {
            "key_swap_cost_rao": inspection.approval["key_swap_cost_rao"],
            "estimated_transaction_fee_rao": estimated_transaction_fee_rao,
            "reviewed_transaction_fee_estimate_ceiling_rao": (
                options.max_transaction_fee_rao
            ),
            "reviewed_maximum_estimated_spend_rao": inspection.approval[
                "reviewed_maximum_estimated_spend_rao"
            ],
            "on_chain_spend_cap_enforced": False,
            "cost_authorization_model": COST_AUTHORIZATION_MODEL,
        },
        "signed_intent": {
            "extrinsic_hash": signed_hash,
            "nonce": nonce,
            "era_reference_block": era_reference,
            "era_reference_hash": era_reference_hash,
            "era_period_blocks": ERA_PERIOD_BLOCKS,
        },
    }
    assert options.state_file is not None
    assert options.receipt_out is not None
    try:
        _atomic_create(options.state_file, attempt)
    except (OSError, RotationError) as exc:
        raise RotationError(
            "cannot durably record the signed rotation intent; nothing was submitted"
        ) from exc
    try:
        receipt = active.substrate.submit_extrinsic(
            signed,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
    except Exception as exc:
        raise RotationNotProven(
            f"signed rotation {signed_hash} may have broadcast; inspect the "
            "owner-only attempt state and chain before any new approval"
        ) from exc
    artifact, fee_evidence = _receipt_artifact(
        options,
        active,
        receipt,
        signed_hash=signed_hash,
    )
    try:
        _atomic_create(options.receipt_out, artifact)
    except Exception as exc:
        raise RotationNotProven(
            f"rotation {signed_hash} finalized but its canonical receipt could "
            "not be durably recorded"
        ) from exc
    post_state = _post_rotation_proof(options, active, artifact)
    try:
        finalized_state = {
            **attempt,
            "phase": "finalized",
            "economic_boundary": {
                **attempt["economic_boundary"],
                **fee_evidence,
            },
            "receipt_sha256": _digest(artifact),
            "receipt": artifact,
            "post_rotation_proof": post_state,
        }
        _atomic_replace_private(options.state_file, finalized_state)
    except RotationNotProven:
        raise
    except Exception as exc:
        raise RotationNotProven(
            f"rotation {signed_hash} finalized but its local artifact could "
            "not be durably recorded"
        ) from exc
    return {
        "schema": STATE_SCHEMA,
        "status": "PASS",
        "chain_write": True,
        "confirmation_digest": inspection.confirmation_digest,
        "receipt_sha256": _digest(artifact),
        "receipt": artifact,
        "economic_boundary": finalized_state["economic_boundary"],
        "post_rotation_proof": post_state,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect one exact SN39 swap_hotkey_v2 call. Default mode never "
            "signs or submits."
        )
    )
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--new-wallet-name", required=True)
    parser.add_argument("--new-wallet-hotkey", required=True)
    parser.add_argument("--authority-host", required=True)
    parser.add_argument("--authority-uid", type=int, required=True)
    parser.add_argument(
        "--max-transaction-fee-rao",
        type=int,
        required=True,
        help=(
            "fail-closed ceiling for the exact pre-sign fee estimate; "
            "swap_hotkey_v2 has no on-chain spend-cap argument"
        ),
    )
    parser.add_argument("--expected-coldkey", required=True)
    parser.add_argument("--old-hotkey", required=True)
    parser.add_argument("--new-hotkey", required=True)
    parser.add_argument("--expected-uid", type=int, required=True)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--netuid", type=int, required=True)
    stake = parser.add_mutually_exclusive_group(required=True)
    stake.add_argument("--keep-stake", dest="keep_stake", action="store_true")
    stake.add_argument(
        "--do-not-keep-stake",
        dest="keep_stake",
        action="store_false",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--broadcast",
        action="store_true",
        help="sign and submit only after --confirmation-digest matches",
    )
    action.add_argument(
        "--reconcile",
        action="store_true",
        help="recover one existing signed attempt from canonical chain history",
    )
    parser.add_argument("--confirmation-digest")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--receipt-out", type=Path)
    parser.add_argument("--reviewed-finalized-block", type=int)
    parser.add_argument("--reviewed-finalized-hash")
    parser.add_argument("--reviewed-coldkey-nonce", type=int)
    parser.add_argument("--approval-valid-until-block", type=int)
    return parser


def _options(namespace: argparse.Namespace) -> Options:
    return Options(
        wallet_name=namespace.wallet_name,
        new_wallet_name=namespace.new_wallet_name,
        new_wallet_hotkey=namespace.new_wallet_hotkey,
        authority_host=namespace.authority_host,
        authority_uid=namespace.authority_uid,
        max_transaction_fee_rao=namespace.max_transaction_fee_rao,
        execution_context=_load_execution_context(),
        expected_coldkey=namespace.expected_coldkey,
        old_hotkey=namespace.old_hotkey,
        new_hotkey=namespace.new_hotkey,
        expected_uid=namespace.expected_uid,
        role=namespace.role,
        keep_stake=namespace.keep_stake,
        netuid=namespace.netuid,
        broadcast=namespace.broadcast,
        reconcile=namespace.reconcile,
        confirmation_digest=namespace.confirmation_digest,
        state_file=namespace.state_file,
        receipt_out=namespace.receipt_out,
        reviewed_finalized_block=namespace.reviewed_finalized_block,
        reviewed_finalized_hash=namespace.reviewed_finalized_hash,
        reviewed_coldkey_nonce=namespace.reviewed_coldkey_nonce,
        approval_valid_until_block=namespace.approval_valid_until_block,
    )


def main(argv: list[str] | None = None) -> int:
    if sys.flags.isolated != 1:
        print(
            "SN39 rotation: FAIL: Python isolated mode (-I) is required",
            file=sys.stderr,
        )
        return 1
    namespace = build_parser().parse_args(argv)
    try:
        result = execute(_options(namespace))
    except RotationNotProven as exc:
        print(f"SN39 rotation: NOT_PROVEN: {exc}", file=sys.stderr)
        return 3
    except RotationError as exc:
        print(f"SN39 rotation: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
