#!/usr/bin/env python3
"""Build, archive-verify, sign, and publish the exact SN39 launch release."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The root finalizer imports from the already-verified immutable checkout.
# Never create ignored bytecode there after the pristine-tree gate passes.
sys.dont_write_bytecode = True

PUBLIC_ROOT = Path("/var/lib/cathedral-public-evidence")
PRIVATE_SEED = Path("/etc/cathedral/release-attestation-signing-sn39-20260724.key")
RUNTIME_ROOT = Path("/var/lib/cathedral-validator")
MANIFEST = Path("/etc/cathedral-validator/sn39-release-manifest.json")
CONTROLLED_ROOT = Path("/var/lib/cathedral-validator-controlled-sn39/current")
VERIFIER_BINARY = Path("/opt/cathedral-sn39/bin/cathedral-tdx-verifier")
RELEASE_KEY_ID = "cathedral-release-attestation-sn39-20260724"
FINALIZER_CONTEXT_ENV = "CATHEDRAL_SN39_FINALIZER_CONTEXT"
JOURNAL_NAME = re.compile(r"journal-[0-9a-f]{64}\.json")
SHA = re.compile(r"[0-9a-f]{40}")
HASH = re.compile(r"0x[0-9a-f]{64}")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_PUBLIC_BLOB_BYTES = 4 * 1024 * 1024
MAX_RELEASE_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CONTROLLED_ENVELOPE_BYTES = 4 * 1024 * 1024
MAX_VERIFIER_BINARY_BYTES = 32 * 1024 * 1024
ROOT_UID = 0
FINNEY_GENESIS_HASH = (
    "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
)


class ReleaseError(RuntimeError):
    """The irreversible launch cannot be sealed safely."""


def canonical_json(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def strict_json(payload: bytes, *, label: str) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseError(f"{label} has duplicate JSON keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ReleaseError(f"{label} has a non-finite number")
            ),
        )
    except ReleaseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReleaseError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ReleaseError(f"{label} is not a JSON object")
    return document


def read_launch_journal(path: Path) -> dict[str, Any]:
    if path.parent != RUNTIME_ROOT or JOURNAL_NAME.fullmatch(path.name) is None:
        raise ReleaseError("launch journal is outside the canonical runtime root")
    try:
        runtime_info = path.parent.lstat()
    except OSError as exc:
        raise ReleaseError("launch runtime root is unavailable") from exc
    if (
        stat.S_ISLNK(runtime_info.st_mode)
        or not stat.S_ISDIR(runtime_info.st_mode)
        or stat.S_IMODE(runtime_info.st_mode) != 0o700
        or runtime_info.st_uid == 0
    ):
        raise ReleaseError("launch runtime root is not service-owned mode 0700")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError("cannot open the launch journal") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != runtime_info.st_uid
            or info.st_gid != runtime_info.st_gid
            or info.st_size <= 0
            or info.st_size > MAX_JOURNAL_BYTES
        ):
            raise ReleaseError(
                "launch journal is not a service-owned private bounded regular file"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_JOURNAL_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return strict_json(payload, label="launch journal")


def git(release: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["/usr/bin/git", *arguments],
            cwd=release,
            text=True,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            timeout=30,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("cannot verify the release checkout") from exc


def verify_release_checkout(release: Path, release_sha: str) -> None:
    if SHA.fullmatch(release_sha) is None:
        raise ReleaseError("release SHA is malformed")
    try:
        info = release.lstat()
    except OSError as exc:
        raise ReleaseError("release checkout is unavailable") from exc
    if release.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ReleaseError("release checkout is not a real directory")
    if git(release, "rev-parse", "HEAD") != release_sha or git(
        release,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    ):
        raise ReleaseError("release checkout is not the exact pristine requested SHA")


def _read_root_manifest_digest() -> str:
    try:
        path_info = MANIFEST.lstat()
    except OSError as exc:
        raise ReleaseError("root-owned release manifest is unavailable") from exc
    if (
        MANIFEST.is_symlink()
        or not stat.S_ISREG(path_info.st_mode)
        or path_info.st_nlink != 1
        or path_info.st_uid != ROOT_UID
        or stat.S_IMODE(path_info.st_mode) & 0o022
        or path_info.st_size <= 0
        or path_info.st_size > MAX_MANIFEST_BYTES
    ):
        raise ReleaseError("release manifest is not an immutable root-owned file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(MANIFEST, flags)
    except OSError as exc:
        raise ReleaseError("root-owned release manifest is unavailable") from exc
    try:
        opened_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_info.st_mode)
            or opened_info.st_nlink != 1
            or opened_info.st_uid != ROOT_UID
            or stat.S_IMODE(opened_info.st_mode) & 0o022
            or opened_info.st_size != path_info.st_size
            or opened_info.st_dev != path_info.st_dev
            or opened_info.st_ino != path_info.st_ino
        ):
            raise ReleaseError("release manifest changed while it was opened")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_MANIFEST_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload or len(payload) > MAX_MANIFEST_BYTES:
        raise ReleaseError("root-owned release manifest is unavailable")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _launcher_context_digest(
    *,
    release_sha: str,
    journal: Path,
    manifest_digest: str,
) -> str:
    payload = (
        "cathedral-sn39-finalizer-context-v1\n"
        f"{release_sha}\n"
        f"{manifest_digest}\n"
        f"{journal}\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_launcher_context(*, release_sha: str, journal: Path) -> None:
    if os.geteuid() != ROOT_UID:
        raise ReleaseError("release finalizer must run as root")
    manifest_digest = _read_root_manifest_digest()
    expected = _launcher_context_digest(
        release_sha=release_sha,
        journal=journal,
        manifest_digest=manifest_digest,
    )
    supplied = os.environ.pop(FINALIZER_CONTEXT_ENV, "")
    if not hmac.compare_digest(supplied, expected):
        raise ReleaseError(
            "release finalizer was not entered through the immutable-install launcher"
        )


def _require_finney_archive(subtensor: Any) -> str:
    """Fail closed unless the supplied archive is the pinned Finney chain."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        raise ReleaseError("Finney archive substrate is unavailable")
    try:
        genesis_hash = substrate.get_block_hash(0)
    except Exception as exc:  # noqa: BLE001 - archive identity must fail closed
        raise ReleaseError("Finney archive genesis is unavailable") from exc
    if genesis_hash is None:
        raise ReleaseError("Finney archive genesis is unavailable")
    observed = str(genesis_hash).lower()
    if observed != FINNEY_GENESIS_HASH:
        raise ReleaseError("archive differs from the pinned Finney genesis")
    return observed


def _require_launch_state(
    state: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    identity = state.get("submission_launch_identity")
    broadcast_intent = state.get("submission_launch_broadcast_intent")
    uid_safety = state.get("submission_launch_uid_safety")
    if (
        state.get("submission_pending_id") is not None
        or state.get("submission_launch_status") != "finalized"
        or state.get("submission_launch_budget_limit") != 1
        or state.get("submission_continuous_enabled") is not False
        or not isinstance(identity, dict)
        or not isinstance(broadcast_intent, dict)
        or not isinstance(uid_safety, dict)
        or identity.get("uid_safety") != uid_safety
    ):
        raise ReleaseError(
            "journal has no final, unreconciled one-shot launch with durable "
            "broadcast intent and UID-safety proof"
        )
    attempt = state.get("submission_launch_attempt_id")
    if (
        not isinstance(attempt, str)
        or SHA256.fullmatch(attempt) is None
        or state.get("submission_launch_attempt_ids") != [attempt]
    ):
        raise ReleaseError("journal launch-attempt budget is malformed")
    full = identity.get("full_provenance")
    vector = identity.get("signed_vector")
    if not isinstance(full, dict) or not isinstance(vector, dict):
        raise ReleaseError("journal lacks the launch vector or provenance checkpoint")
    return identity, full, broadcast_intent, uid_safety


def _archive_snapshot(
    subtensor: Any,
    *,
    block: int,
) -> tuple[str, list[int], list[str], list[bool]]:
    block_hash = str(subtensor.get_block_hash(block)).lower()
    metagraph = subtensor.metagraph(39, block=block)
    commit_reveal = subtensor.commit_reveal_enabled(netuid=39, block=block)
    raw_uids = getattr(metagraph, "uids", ())
    if hasattr(raw_uids, "tolist"):
        raw_uids = raw_uids.tolist()
    uids = [int(value) for value in raw_uids]
    hotkeys = [str(value) for value in getattr(metagraph, "hotkeys", ())]
    validator_permit = [
        bool(value) for value in getattr(metagraph, "validator_permit", ())
    ]
    if (
        HASH.fullmatch(block_hash) is None
        or int(getattr(metagraph, "block", -1)) != block
        or len(uids) != len(hotkeys)
        or len(validator_permit) != len(hotkeys)
        or len(set(uids)) != len(uids)
        or not hotkeys
        or len(set(hotkeys)) != len(hotkeys)
        or commit_reveal is not False
    ):
        raise ReleaseError(
            "archive mapping snapshot is malformed or commit-reveal is on"
        )
    return block_hash, uids, hotkeys, validator_permit


def _storage_value(
    subtensor: Any,
    *,
    name: str,
    params: list[Any],
    block: int,
) -> Any:
    try:
        observed = subtensor.query_subtensor(
            name=name,
            params=params,
            block=block,
        )
    except Exception as exc:  # noqa: BLE001 - archive proof fails closed
        raise ReleaseError(
            f"archive cannot query {name} at the launch mapping block"
        ) from exc
    return getattr(observed, "value", observed)


def _call_arg(call: dict[str, Any], name: str) -> Any:
    for item in call.get("call_args") or ():
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("value")
    return None


def _archive_rotation_receipt(
    substrate: Any,
    *,
    block_number: int,
    coldkey: str,
    target_hotkey: str,
) -> dict[str, Any]:
    try:
        block_hash = str(substrate.get_block_hash(block_number)).lower()
        canonical_number = int(substrate.get_block_number(block_hash))
        block = substrate.get_block(block_hash=block_hash)
    except Exception as exc:  # noqa: BLE001 - archive proof fails closed
        raise ReleaseError("archive cannot read the target rotation block") from exc
    if (
        HASH.fullmatch(block_hash) is None
        or canonical_number != block_number
        or not isinstance(block, dict)
        or not isinstance(block.get("extrinsics"), (list, tuple))
    ):
        raise ReleaseError("archive target rotation block is non-canonical")
    matching: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(block["extrinsics"]):
        observed = getattr(item, "value", None)
        if not isinstance(observed, dict):
            continue
        call = observed.get("call")
        if (
            isinstance(call, dict)
            and observed.get("address") == coldkey
            and call.get("call_module") == "SubtensorModule"
            and call.get("call_function") == "swap_hotkey_v2"
            and _call_arg(call, "new_hotkey") == target_hotkey
            and _call_arg(call, "netuid") == 39
        ):
            matching.append((index, observed))
    if len(matching) != 1:
        raise ReleaseError(
            "archive target rotation has no unique exact swap_hotkey_v2 call"
        )
    extrinsic_index, observed = matching[0]
    call = observed["call"]
    extrinsic_hash = str(observed.get("extrinsic_hash", "")).lower()
    old_hotkey = _call_arg(call, "hotkey")
    keep_stake = _call_arg(call, "keep_stake")
    if (
        HASH.fullmatch(extrinsic_hash) is None
        or not isinstance(old_hotkey, str)
        or not old_hotkey
        or old_hotkey == target_hotkey
        or not isinstance(keep_stake, bool)
    ):
        raise ReleaseError("archive target rotation call is malformed")
    try:
        receipt = substrate.retrieve_extrinsic_by_hash(block_hash, extrinsic_hash)
        receipt_index = int(getattr(receipt, "extrinsic_idx", -1))
        receipt_success = getattr(receipt, "is_success", None)
        receipt_error = getattr(receipt, "error_message", None)
        events = getattr(receipt, "triggered_events", None)
        timestamp_value = substrate.query(
            module="Timestamp",
            storage_function="Now",
            block_hash=block_hash,
        )
        timestamp_ms = getattr(timestamp_value, "value", timestamp_value)
    except Exception as exc:  # noqa: BLE001 - archive proof fails closed
        raise ReleaseError("archive cannot prove target rotation execution") from exc
    if (
        receipt_index != extrinsic_index
        or receipt_success is not True
        or receipt_error is not None
        or not isinstance(events, (list, tuple))
        or isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms <= 0
    ):
        raise ReleaseError("archive target rotation receipt is incomplete")
    matching_events = []
    for event in events:
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
        raise ReleaseError("archive target rotation event is absent or ambiguous")
    return {
        "call": "swap_hotkey_v2",
        "extrinsic_hash": extrinsic_hash,
        "block_hash": block_hash,
        "block_number": block_number,
        "block_timestamp": datetime.fromtimestamp(timestamp_ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "extrinsic_index": extrinsic_index,
        "coldkey": coldkey,
        "old_hotkey": old_hotkey,
        "new_hotkey": target_hotkey,
        "netuid": 39,
        "keep_stake": keep_stake,
        "event": "HotkeySwappedOnSubnet",
    }


def _archive_uid_safety(
    subtensor: Any,
    *,
    block: int,
    block_hash: str,
    target_uid_hotkeys: dict[int, str],
    subnet_owner_hotkey: str,
) -> dict[str, Any]:
    """Rebuild the complete UID-safety proof from the historical archive.

    This intentionally does not trust or reuse the validator's journaled
    calculations. The finalizer queries the same immutable mapping block and
    independently repeats both the registration-replacement and hotkey-swap
    safety calculations before publishing either result.
    """
    if (
        isinstance(block, bool)
        or not isinstance(block, int)
        or block <= 0
        or HASH.fullmatch(block_hash) is None
        or not target_uid_hotkeys
        or any(
            isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 0
            or not isinstance(hotkey, str)
            or not hotkey
            for uid, hotkey in target_uid_hotkeys.items()
        )
        or not subnet_owner_hotkey
    ):
        raise ReleaseError("launch UID-safety request is malformed")

    metagraph = subtensor.metagraph(39, block=block)
    raw_uids = getattr(metagraph, "uids", ())
    raw_registration_blocks = getattr(metagraph, "block_at_registration", ())
    if hasattr(raw_uids, "tolist"):
        raw_uids = raw_uids.tolist()
    if hasattr(raw_registration_blocks, "tolist"):
        raw_registration_blocks = raw_registration_blocks.tolist()
    try:
        uids = [int(value) for value in raw_uids]
        hotkeys = [str(value) for value in getattr(metagraph, "hotkeys", ())]
        registration_blocks = [int(value) for value in raw_registration_blocks]
        max_uids = int(getattr(metagraph, "max_uids"))
        hparams = getattr(metagraph, "hparams")
        max_regs_per_block = int(getattr(hparams, "max_regs_per_block"))
        immunity_period = int(getattr(hparams, "immunity_period"))
        min_nonimmune_uids = int(
            _storage_value(
                subtensor,
                name="MinNonImmuneUids",
                params=[39],
                block=block,
            )
        )
        subnet_owner_coldkey = str(
            _storage_value(
                subtensor,
                name="SubnetOwner",
                params=[39],
                block=block,
            )
            or ""
        )
        raw_owned_hotkeys = _storage_value(
            subtensor,
            name="OwnedHotkeys",
            params=[subnet_owner_coldkey],
            block=block,
        )
        owned_hotkeys = [str(value) for value in raw_owned_hotkeys]
        immune_owner_uids_limit = int(
            _storage_value(
                subtensor,
                name="ImmuneOwnerUidsLimit",
                params=[39],
                block=block,
            )
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseError(
            "archive registration and immunity policy is unavailable"
        ) from exc
    if (
        len(uids) != len(hotkeys)
        or len(uids) != len(registration_blocks)
        or len(set(uids)) != len(uids)
        or len(set(hotkeys)) != len(hotkeys)
        or not hotkeys
        or max_uids < len(uids)
        or max_regs_per_block < 0
        or immunity_period < 0
        or min_nonimmune_uids < 0
        or not subnet_owner_coldkey
        or immune_owner_uids_limit < 0
        or any(not hotkey for hotkey in owned_hotkeys)
        or len(set(owned_hotkeys)) != len(owned_hotkeys)
    ):
        raise ReleaseError("archive registration and immunity policy is malformed")

    uid_hotkeys = dict(zip(uids, hotkeys))
    if any(
        uid_hotkeys.get(uid) != hotkey for uid, hotkey in target_uid_hotkeys.items()
    ):
        raise ReleaseError("archive target UID mapping differs from the launch vector")
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

    mortal_period_blocks = 4
    free_uid_slots = max_uids - len(uids)
    maximum_era_registrations = max_regs_per_block * mortal_period_blocks
    capacity_protects_all = free_uid_slots >= maximum_era_registrations
    temporally_immune_hotkeys = {
        hotkey
        for hotkey, registered_at in zip(hotkeys, registration_blocks)
        if registered_at + immunity_period >= block + mortal_period_blocks
    }
    prunable_nonimmune_count = sum(
        registered_at + immunity_period <= block
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
    unsafe_targets = sorted(set(target_uid_hotkeys.values()) - replacement_safe_hotkeys)
    if unsafe_targets:
        raise ReleaseError(
            "archive cannot prove every launch UID mapping replacement-safe"
        )
    registration_safety = {
        "max_uids": max_uids,
        "max_regs_per_block": max_regs_per_block,
        "immunity_period": immunity_period,
        "min_nonimmune_uids": min_nonimmune_uids,
        "block_at_registration": [
            {
                "uid": uid,
                "hotkey": hotkey,
                "block_at_registration": registered_at,
            }
            for uid, hotkey, registered_at in zip(
                uids,
                hotkeys,
                registration_blocks,
            )
        ],
        "subnet_owner_coldkey": subnet_owner_coldkey,
        "owned_hotkeys": owned_hotkeys,
        "immune_owner_uids_limit": immune_owner_uids_limit,
        "free_uid_slots": free_uid_slots,
        "maximum_era_registrations": maximum_era_registrations,
        "owner_immortal_hotkeys": sorted(owner_immortal_hotkeys),
        "replacement_safe_hotkeys": sorted(replacement_safe_hotkeys),
    }

    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        raise ReleaseError("archive cannot prove swap safety without substrate")
    try:
        substrate_block_hash = str(substrate.get_block_hash(block)).lower()
        if substrate_block_hash != block_hash:
            raise ValueError("archive substrate block hash differs")

        def constant_value(name: str) -> Any:
            observed = substrate.get_constant(
                module_name="SubtensorModule",
                constant_name=name,
                block_hash=block_hash,
            )
            return getattr(observed, "value", observed)

        hotkey_swap_interval = int(constant_value("HotkeySwapOnSubnetInterval"))
        coldkey_swap_delay = int(
            _storage_value(
                subtensor,
                name="ColdkeySwapAnnouncementDelay",
                params=[],
                block=block,
            )
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseError("archive cannot prove the launch swap constants") from exc
    if hotkey_swap_interval < mortal_period_blocks:
        raise ReleaseError(
            "archive hotkey-swap cooldown is shorter than the mortal era"
        )
    if coldkey_swap_delay < mortal_period_blocks:
        raise ReleaseError(
            "archive coldkey-swap announcement delay is shorter than the mortal era"
        )

    era_last_block = block + mortal_period_blocks - 1
    target_rows: list[dict[str, Any]] = []
    for uid, hotkey in sorted(target_uid_hotkeys.items()):
        coldkey = str(
            _storage_value(
                subtensor,
                name="Owner",
                params=[hotkey],
                block=block,
            )
            or ""
        )
        if not coldkey:
            raise ReleaseError(f"archive cannot prove the owner for target UID {uid}")
        try:
            last_swap_block = int(
                _storage_value(
                    subtensor,
                    name="LastHotkeySwapOnNetuid",
                    params=[39, coldkey],
                    block=block,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ReleaseError(
                f"archive cannot prove the last hotkey swap for target UID {uid}"
            ) from exc
        pending_coldkey_swap = _storage_value(
            subtensor,
            name="ColdkeySwapAnnouncements",
            params=[coldkey],
            block=block,
        )
        if pending_coldkey_swap is not None:
            raise ReleaseError(
                f"archive target UID {uid} has a pending coldkey-swap announcement"
            )
        # Mirrors the validator: the lock state is published, a live lock is
        # proven, and no lock is required. Any drift here breaks the archive
        # equality check against the durable reservation.
        if last_swap_block > 0:
            safe_until_block = last_swap_block + hotkey_swap_interval
            swap_lock = "active" if era_last_block <= safe_until_block else "expired"
        else:
            safe_until_block = None
            swap_lock = "never_rotated"
        rotation_receipt: dict[str, Any] | None = None
        hotkey_root: str | None = None
        if swap_lock == "active":
            rotation_receipt = _archive_rotation_receipt(
                substrate,
                block_number=last_swap_block,
                coldkey=coldkey,
                target_hotkey=hotkey,
            )
            successor = _storage_value(
                subtensor,
                name="HotkeySuccessor",
                params=[39, hotkey],
                block=block,
            )
            root = _storage_value(
                subtensor,
                name="HotkeyRoot",
                params=[39, hotkey],
                block=block,
            )
            old_successor = _storage_value(
                subtensor,
                name="HotkeySuccessor",
                params=[39, rotation_receipt["old_hotkey"]],
                block=block,
            )
            old_root = _storage_value(
                subtensor,
                name="HotkeyRoot",
                params=[39, rotation_receipt["old_hotkey"]],
                block=block,
            )
            expected_root = (
                str(old_root)
                if old_root not in (None, "")
                else str(rotation_receipt["old_hotkey"])
            )
            if (
                successor not in (None, "")
                or root in (None, "")
                or str(old_successor) != hotkey
                or str(root) != expected_root
            ):
                raise ReleaseError(
                    f"archive target UID {uid} lineage differs from its rotation"
                )
            hotkey_root = str(root)
        target_rows.append(
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
                "rotation_receipt": rotation_receipt,
                "registration_replacement_safe": (hotkey in replacement_safe_hotkeys),
            }
        )
    return {
        "schema": "cathedral_sn39_uid_safety_v2",
        "stability_basis": "operator_controlled_coldkeys",
        "registration": registration_safety,
        "rotation": {
            "status": "PASS",
            "mapping_block": block,
            "mapping_block_hash": block_hash,
            "mortal_period_blocks": mortal_period_blocks,
            "era_last_block": era_last_block,
            "hotkey_swap_on_subnet_interval": hotkey_swap_interval,
            "coldkey_swap_announcement_delay": coldkey_swap_delay,
            "targets": target_rows,
        },
    }


def _validated_broadcast_intent(
    raw: dict[str, Any],
    *,
    extrinsic_hash: str,
    mapping_block: int,
    inclusion_block: int,
    version_key: int,
    wire_uids: list[int],
    wire_weights: list[int],
) -> dict[str, Any]:
    expected_keys = {
        "extrinsic_hash",
        "nonce",
        "era_reference_block",
        "mortal_period_blocks",
        "version_key",
        "wire_uids",
        "wire_weights",
    }
    if set(raw) != expected_keys:
        raise ReleaseError("journal broadcast intent has an ambiguous field set")
    try:
        intent_hash = str(raw["extrinsic_hash"]).lower()
        nonce = raw["nonce"]
        era_reference_block = raw["era_reference_block"]
        mortal_period_blocks = raw["mortal_period_blocks"]
        intent_version_key = raw["version_key"]
        intent_uids = raw["wire_uids"]
        intent_weights = raw["wire_weights"]
    except KeyError as exc:
        raise ReleaseError("journal broadcast intent is incomplete") from exc
    if (
        HASH.fullmatch(intent_hash) is None
        or intent_hash != extrinsic_hash
        or isinstance(nonce, bool)
        or not isinstance(nonce, int)
        or nonce < 0
        or isinstance(era_reference_block, bool)
        or not isinstance(era_reference_block, int)
        or era_reference_block != mapping_block
        or isinstance(mortal_period_blocks, bool)
        or not isinstance(mortal_period_blocks, int)
        or mortal_period_blocks != 4
        or isinstance(intent_version_key, bool)
        or not isinstance(intent_version_key, int)
        or intent_version_key != version_key
        or not isinstance(intent_uids, list)
        or not isinstance(intent_weights, list)
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in intent_uids
        )
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in intent_weights
        )
        or intent_uids != wire_uids
        or intent_weights != wire_weights
        or inclusion_block < era_reference_block
        or inclusion_block >= era_reference_block + mortal_period_blocks
    ):
        raise ReleaseError(
            "journal launch receipt is not the exact signed mortal broadcast intent"
        )
    return {
        "extrinsic_hash": intent_hash,
        "nonce": nonce,
        "era_reference_block": era_reference_block,
        "mortal_period_blocks": mortal_period_blocks,
        "version_key": intent_version_key,
        "wire_uids": intent_uids,
        "wire_weights": intent_weights,
    }


def _read_controlled_envelope(root: Path, digest: str) -> bytes:
    if SHA256.fullmatch(digest) is None:
        raise ReleaseError("controlled envelope digest is malformed")
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ReleaseError("controlled evidence directory is unavailable") from exc
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != ROOT_UID
        or stat.S_IMODE(root_info.st_mode) & 0o022
        or stat.S_IMODE(root_info.st_mode) & 0o007
    ):
        raise ReleaseError(
            "controlled evidence directory is not private and root-controlled"
        )
    path = root / f"{digest.split(':', 1)[1]}.json"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError("controlled replay envelope is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != ROOT_UID
            or stat.S_IMODE(info.st_mode) & 0o022
            or stat.S_IMODE(info.st_mode) & 0o007
            or info.st_size <= 0
            or info.st_size > MAX_CONTROLLED_ENVELOPE_BYTES
        ):
            raise ReleaseError(
                "controlled replay envelope is not a private single-link "
                "root-controlled file"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_CONTROLLED_ENVELOPE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not payload
        or len(payload) > MAX_CONTROLLED_ENVELOPE_BYTES
        or "sha256:" + hashlib.sha256(payload).hexdigest() != digest
    ):
        raise ReleaseError("controlled replay envelope differs from its digest")
    return payload


def _read_verifier_binary(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError("pinned TDX verifier binary is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != ROOT_UID
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size <= 0
            or info.st_size > MAX_VERIFIER_BINARY_BYTES
        ):
            raise ReleaseError(
                "pinned TDX verifier is not a single-link root-controlled file"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_VERIFIER_BINARY_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload or len(payload) > MAX_VERIFIER_BINARY_BYTES:
        raise ReleaseError("pinned TDX verifier exceeds its size cap")
    return payload


def _replay_frozen_controlled_positive(
    *,
    checkpoint: dict[str, Any],
    signed_vector: dict[str, Any],
    inclusion_block: int,
    inclusion_block_hash: str,
    public_root: Path,
    controlled_root: Path,
    verifier_binary_path: Path,
    subtensor: Any,
) -> dict[str, Any]:
    """Independently replay the exact controlled positive evidence as root."""
    from cathedral import provenance
    from cathedral.evidence import parse_manifest, verify_index
    from cathedral.score_class import parse_score_report_json

    from scaffold.sn39_public_reproduction import (
        _block_timestamp_ms,
        _controlled_replay_result_document,
        _load_public_keys,
        _validate_frozen_manifest,
        _validate_frozen_report_freshness,
        _validate_frozen_result,
        verify_historical_candidates,
    )

    release_root = Path(__file__).resolve().parents[1]
    _safe_public_directory(public_root)
    _safe_public_directory(public_root / "blobs")
    _safe_public_directory(public_root / "blobs" / "sha256")

    def load_blob(digest: str) -> bytes:
        if SHA256.fullmatch(digest) is None:
            raise ReleaseError("frozen public evidence digest is malformed")
        payload = _read_public_file(
            public_root / "blobs" / "sha256" / digest.split(":", 1)[1],
            size_cap=MAX_PUBLIC_BLOB_BYTES,
            label=f"frozen public blob {digest}",
        )
        if payload is None:
            raise ReleaseError(f"frozen public blob is unavailable: {digest}")
        if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
            raise ReleaseError(f"frozen public blob is corrupt: {digest}")
        return payload

    try:
        index = checkpoint["signed_index"]
        issued_at = datetime.fromisoformat(str(index["generated_at"]))
        verified_index = verify_index(
            canonical_json(index),
            _load_public_keys(release_root / "config/provenance/index-keys.json"),
            expected_network="finney",
            expected_netuid=39,
            max_age_seconds=None,
            now=issued_at,
        )
        if (
            verified_index["latest"]["manifest"] != checkpoint["manifest"]
            or int(verified_index["latest"]["source_epoch"])
            != checkpoint["source_epoch"]
        ):
            raise ReleaseError(
                "frozen evidence index differs from the launch checkpoint"
            )
        manifest = parse_manifest(load_blob(checkpoint["manifest"]))
        _validate_frozen_manifest(manifest, checkpoint)
        report_bytes = load_blob(manifest["score_report"]["blob"])
        report = parse_score_report_json(report_bytes)
        _validate_frozen_report_freshness(report, checkpoint)
        verify_historical_candidates(manifest, subtensor=subtensor)
        registry_bytes = load_blob(manifest["policy_registry"]["blob"])
        receipts = {
            row["receipt_id"]: load_blob(row["blob"]) for row in manifest["receipts"]
        }
        work_artifacts = {
            row["receipt_id"]: (
                load_blob(row["work_item_blob"]),
                load_blob(row["result_blob"]),
            )
            for row in manifest["receipts"]
        }
        inclusion_timestamp_ms = _block_timestamp_ms(
            subtensor.substrate,
            inclusion_block_hash,
        )
        inclusion_moment = datetime.fromtimestamp(inclusion_timestamp_ms / 1000, UTC)
        result = provenance.verify_and_recompute(
            report_bytes=report_bytes,
            receipts_by_id=receipts,
            registry_bytes=registry_bytes,
            trusted_registry_keys=_load_public_keys(
                release_root / "config/provenance/registry-keys.json"
            ),
            report_signing_keys=_load_public_keys(
                release_root / "config/provenance/report-keys.json"
            ),
            expected_network="finney",
            expected_netuid=39,
            expected_verifier_digest=checkpoint["verifier_digest"],
            mechanism_id=checkpoint["reward_mechanism"]["id"],
            now=inclusion_moment,
            candidate_set=manifest["candidate_set"],
            work_artifacts_by_receipt=work_artifacts,
            current_block=inclusion_block,
        )
        _validate_frozen_result(result, checkpoint)
        agrees, discrepancies = provenance.compare_with_vector(
            result,
            signed_vector,
            wire_report_sha256=manifest.get("wire_report_sha256"),
        )
        if not agrees or discrepancies:
            raise ReleaseError(
                "frozen evidence recomputation differs from the signed launch vector"
            )
        bindings = {str(row["hotkey"]): row for row in manifest["attestations"]}
        positive_hotkeys = sorted(
            miner.hotkey for miner in result.miners if miner.receipt_verified
        )
        signed_positive_hotkeys = sorted(
            str(row["miner_hotkey"])
            for row in signed_vector["weights"]
            if float(row["weight"]) > 0.0
        )
        if (
            not positive_hotkeys
            or positive_hotkeys != signed_positive_hotkeys
            or len(bindings) != len(manifest["attestations"])
        ):
            raise ReleaseError(
                "frozen positive receipt set differs from the rewarded launch set"
            )
        envelopes = {
            hotkey: _read_controlled_envelope(
                controlled_root,
                str(bindings[hotkey]["envelope_digest"]),
            )
            for hotkey in positive_hotkeys
        }
        verifier_bytes = _read_verifier_binary(verifier_binary_path)
        candidate_snapshot = manifest["candidate_set"]
        replayed = provenance.replay_positive_miners(
            result,
            candidate_outcomes={
                str(row["hotkey"]): str(row["outcome"])
                for row in candidate_snapshot["candidates"]
            },
            independent_candidates={
                str(row["hotkey"]) for row in candidate_snapshot["candidates"]
            },
            independent_block_hash=str(candidate_snapshot["block_hash"]),
            epoch_generated_at=manifest["generated_at"],
            challenge_anchor={
                "block": int(candidate_snapshot["block"]),
                "block_hash": str(candidate_snapshot["block_hash"]),
                "network": "finney",
                "netuid": 39,
            },
            registry=provenance.load_registry(
                registry_bytes,
                _load_public_keys(
                    release_root / "config/provenance/registry-keys.json"
                ),
                now=inclusion_moment,
            ),
            envelopes_by_hotkey=envelopes,
            attestation_bindings=bindings,
            verifier_binary=verifier_bytes,
            verifier_blob_digest=manifest["verifier"]["binary_blob"],
            verifier_command=tuple(manifest["verifier"]["command"]),
            verifier_artifacts=tuple(
                manifest["verifier"].get("artifacts") or manifest["verifier"]["command"]
            ),
        )
        raw_replayed = sorted(
            miner.hotkey
            for miner in replayed.miners
            if getattr(miner, "raw_verified", False)
        )
        if raw_replayed != positive_hotkeys:
            raise ReleaseError(
                "root finalizer did not replay every rewarded controlled envelope"
            )
        return _controlled_replay_result_document(
            checkpoint,
            {"signed_vector": signed_vector},
            manifest,
        )
    except ReleaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - final replay must fail closed
        raise ReleaseError(
            "root finalizer could not reproduce the controlled positive TDX evidence"
        ) from exc


def build_release(
    state: dict[str, Any],
    *,
    release_sha: str,
    subtensor: Any,
    public_root: Path = PUBLIC_ROOT,
    controlled_root: Path = CONTROLLED_ROOT,
    verifier_binary_path: Path = VERIFIER_BINARY,
) -> tuple[dict[str, Any], bytes]:
    from scaffold.sn39_public_reproduction import (
        EXPECTED_PRODUCER_REVISION,
        EXPECTED_RELEASE_PINS,
        EXPECTED_VERSION_KEY,
        WIRE_BURN_SHARE,
        WIRE_BURN_U16,
        WIRE_VALIDATED_SUPPLY_SHARE,
        WIRE_VALIDATED_SUPPLY_U16,
        _validate_launch_submission,
        verify_historical_launch,
    )
    from scaffold.validator_thin import _wire_weights

    _require_finney_archive(subtensor)
    identity, full, raw_broadcast_intent, journal_uid_safety = _require_launch_state(
        state
    )
    vector = identity["signed_vector"]
    vector_digest = "sha256:" + hashlib.sha256(canonical_json(vector)).hexdigest()
    if vector_digest != identity.get("signed_vector_sha256"):
        raise ReleaseError("journal vector bytes differ from their submission digest")
    try:
        mapping_block = int(identity["mapping_block"])
        validator_uid = int(identity["validator_uid"])
        uid_weights = {
            int(uid): float(weight) for uid, weight in identity["uid_weights"]
        }
        uid_hotkeys = {int(uid): str(hotkey) for uid, hotkey in identity["uid_hotkeys"]}
        extrinsic_hash = str(state["submission_launch_extrinsic_hash"]).lower()
        block_hash = str(state["submission_launch_block_hash"]).lower()
        block_number = int(state["submission_launch_block_number"])
        version_key = int(state["submission_launch_version_key"])
        next_epoch_start_block = int(identity["next_epoch_start_block"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError("journal launch identity is malformed") from exc
    signed_rows = vector.get("weights")
    burn_snapshot = vector.get("burn_snapshot")
    if (
        not isinstance(signed_rows, list)
        or len(signed_rows) != 1
        or not isinstance(signed_rows[0], dict)
        or not isinstance(burn_snapshot, dict)
    ):
        raise ReleaseError("journal signed vector is not the one-miner launch boundary")
    rewarded_hotkey = str(signed_rows[0].get("miner_hotkey") or "")
    burn_hotkey = str(burn_snapshot.get("burn_hotkey") or "")
    rewarded_uids = [
        uid for uid, hotkey in uid_hotkeys.items() if hotkey == rewarded_hotkey
    ]
    burn_uids = [uid for uid, hotkey in uid_hotkeys.items() if hotkey == burn_hotkey]
    if len(rewarded_uids) != 1 or len(burn_uids) != 1:
        raise ReleaseError("journal launch hotkeys do not resolve to unique UIDs")
    rewarded_uid = rewarded_uids[0]
    burn_uid = burn_uids[0]
    ordered_uids = sorted(uid_weights)
    ordered_weights = [uid_weights[uid] for uid in ordered_uids]
    wire_uids, wire_values = _wire_weights(ordered_uids, ordered_weights)
    broadcast_intent = _validated_broadcast_intent(
        raw_broadcast_intent,
        extrinsic_hash=extrinsic_hash,
        mapping_block=mapping_block,
        inclusion_block=block_number,
        version_key=version_key,
        wire_uids=wire_uids,
        wire_weights=wire_values,
    )
    if (
        identity.get("network") != "finney"
        or identity.get("netuid") != 39
        or not rewarded_hotkey
        or not burn_hotkey
        or rewarded_uid == burn_uid
        or set(uid_weights) != {rewarded_uid, burn_uid}
        or set(uid_hotkeys) != {rewarded_uid, burn_uid}
        or uid_weights.get(rewarded_uid) != 0.9
        or uid_weights.get(burn_uid) != 0.1
        or HASH.fullmatch(extrinsic_hash) is None
        or HASH.fullmatch(block_hash) is None
        or block_number < mapping_block
        or block_number >= mapping_block + 4
        or block_number >= next_epoch_start_block
        or version_key != EXPECTED_VERSION_KEY
    ):
        raise ReleaseError("journal launch boundary differs from the SN39 release")

    mapping_hash, mapping_uids, hotkeys, mapping_validator_permit = _archive_snapshot(
        subtensor,
        block=mapping_block,
    )
    archive_mapping = dict(zip(mapping_uids, hotkeys))
    validator_hotkey = str(identity.get("validator_hotkey") or "")
    mapping_owner_hotkey = str(
        subtensor.get_subnet_owner_hotkey(39, block=mapping_block) or ""
    )
    mapping_next_epoch = subtensor.get_next_epoch_start_block(
        39,
        block=mapping_block,
    )
    if (
        archive_mapping.get(validator_uid) != validator_hotkey
        or mapping_validator_permit[hotkeys.index(validator_hotkey)] is not True
        or archive_mapping.get(rewarded_uid) != rewarded_hotkey
        or archive_mapping.get(burn_uid) != burn_hotkey
        or uid_hotkeys.get(rewarded_uid) != rewarded_hotkey
        or uid_hotkeys.get(burn_uid) != burn_hotkey
        or mapping_owner_hotkey != burn_hotkey
        or isinstance(mapping_next_epoch, bool)
        or not isinstance(mapping_next_epoch, int)
        or mapping_next_epoch != next_epoch_start_block
    ):
        raise ReleaseError(
            "archive mapping, owner, or epoch schedule does not match the "
            "journaled launch identity"
        )

    archive_uid_safety = _archive_uid_safety(
        subtensor,
        block=mapping_block,
        block_hash=mapping_hash,
        target_uid_hotkeys={
            rewarded_uid: rewarded_hotkey,
            burn_uid: burn_hotkey,
        },
        subnet_owner_hotkey=mapping_owner_hotkey,
    )
    if archive_uid_safety != journal_uid_safety:
        raise ReleaseError(
            "archive UID-safety proof differs from the durable launch proof"
        )

    signed_index = full.get("signed_index")
    if (
        not isinstance(signed_index, dict)
        or (signed_index.get("latest") or {}).get("source_epoch")
        != full.get("source_epoch")
        or (signed_index.get("latest") or {}).get("manifest") != full.get("manifest")
    ):
        raise ReleaseError("journal has no exact signed evidence index checkpoint")
    if (
        full.get("scope") != "rewarded_set_full"
        or full.get("vector_agrees") is not True
        or not full.get("rewarded_hotkeys")
        or full.get("rewarded_hotkeys") != full.get("raw_replayed_hotkeys")
        or full.get("source_revision") != EXPECTED_PRODUCER_REVISION
        or not isinstance(full.get("report_signing_key_id"), str)
        or not isinstance(full.get("verifier_binary_digest"), str)
    ):
        raise ReleaseError("journal rewarded-set provenance gate is incomplete")

    checkpoint = {
        "source_epoch": full["source_epoch"],
        "manifest": full["manifest"],
        "report_id": full["report_id"],
        "policy_release": full["policy_release"],
        "policy_digest": full["policy_digest"],
        "report_signing_key_id": full["report_signing_key_id"],
        "reward_mechanism": {"id": full["mechanism"], "revision": 1},
        "verifier_digest": full["verifier_digest"],
        "verifier_binary_digest": full["verifier_binary_digest"],
        "public_assurance": "receipts_only",
        "signed_index": signed_index,
        "freshness_boundary": full.get("freshness_boundary"),
    }
    replay_result = _replay_frozen_controlled_positive(
        checkpoint=checkpoint,
        signed_vector=vector,
        inclusion_block=block_number,
        inclusion_block_hash=block_hash,
        public_root=public_root,
        controlled_root=controlled_root,
        verifier_binary_path=verifier_binary_path,
        subtensor=subtensor,
    )
    replay_bytes = canonical_json(replay_result)
    checkpoint["replay_result"] = "sha256:" + hashlib.sha256(replay_bytes).hexdigest()
    release = {
        "schema": "cathedral_sn39_provenance_release_v2",
        "network": "finney",
        "netuid": 39,
        "validated_capability": "intel_tdx_cpu",
        "submission_authority_default": "thin",
        "full_provenance_mode": "concurrent_shadow",
        "claim": "SN39 mainnet: validated Intel TDX CPU compute.",
        "reward_mechanism": {
            "id": "validated_supply_v1",
            "revision": 1,
            "validated_supply_share": 0.9,
            "burn_share": 0.1,
            "wire_quantization": {
                "weights_u16": [
                    WIRE_VALIDATED_SUPPLY_U16,
                    WIRE_BURN_U16,
                ],
                "effective_validated_supply_share": WIRE_VALIDATED_SUPPLY_SHARE,
                "effective_burn_share": WIRE_BURN_SHARE,
            },
        },
        "launch_submission": {
            "vector_id": identity["vector_id"],
            "policy_version": identity["policy_version"],
            "signed_vector_sha256": vector_digest,
            "signed_vector": vector,
            "broadcast_intent": broadcast_intent,
            "mapping": {
                "block": mapping_block,
                "validator_uid": validator_uid,
                "validator_hotkey": validator_hotkey,
                "rewarded_uid": rewarded_uid,
                "rewarded_hotkey": rewarded_hotkey,
                "burn_uid": burn_uid,
                "burn_hotkey": burn_hotkey,
                "commit_reveal_enabled": False,
                "next_epoch_start_block": next_epoch_start_block,
                "uid_weights": {
                    str(rewarded_uid): 0.9,
                    str(burn_uid): 0.1,
                },
                "uid_safety": archive_uid_safety,
                "metagraph_snapshot": {
                    "network": "finney",
                    "netuid": 39,
                    "block": mapping_block,
                    "block_hash": mapping_hash,
                    "uids": mapping_uids,
                    "hotkeys": hotkeys,
                    "validator_permit": mapping_validator_permit,
                },
            },
            "extrinsic": {
                "hash": extrinsic_hash,
                "block": block_number,
                "block_hash": block_hash,
                "validator_uid": validator_uid,
                "uids": wire_uids,
                "weights_u16": wire_values,
                "version_key": version_key,
            },
            "evidence_checkpoint": checkpoint,
        },
        "reproducer_revision": release_sha,
        "source_revisions": {
            "producer": EXPECTED_PRODUCER_REVISION,
            "validator": release_sha,
        },
        "pins": EXPECTED_RELEASE_PINS,
        "release_attestation": {"key_id": RELEASE_KEY_ID},
    }
    _validate_launch_submission(release["launch_submission"])
    verify_historical_launch(release, subtensor=subtensor)
    return release, replay_bytes


def _read_root_seed(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError("release signing seed is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 256
        ):
            raise ReleaseError("release signing seed is not root-only mode 0600")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(257).strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        seed = base64.b64decode(raw, validate=True)
    except (TypeError, ValueError) as exc:
        raise ReleaseError("release signing seed is not canonical base64") from exc
    if len(seed) != 32:
        raise ReleaseError("release signing seed is not 32 bytes")
    return seed


def build_signature(
    release_bytes: bytes,
    *,
    seed: bytes,
    release_sha: str,
    release_root: Path,
) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from scaffold.sn39_public_reproduction import verify_release_bytes

    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    signature = {
        "algorithm": "Ed25519",
        "key_id": RELEASE_KEY_ID,
        "payload": "release.json exact bytes",
        "payload_sha256": "sha256:" + hashlib.sha256(release_bytes).hexdigest(),
        "signature": base64.b64encode(private.sign(release_bytes)).decode("ascii"),
    }
    signature_bytes = canonical_json(signature) + b"\n"
    pinned = strict_json(
        (release_root / "config/provenance/release-attestation-keys.json").read_bytes(),
        label="release public key bundle",
    )
    expected = pinned.get(RELEASE_KEY_ID)
    if expected != base64.b64encode(public).decode("ascii"):
        raise ReleaseError("private release key differs from the committed public pin")
    verify_release_bytes(
        release_bytes,
        signature_bytes,
        public_keys={RELEASE_KEY_ID: str(expected)},
        repo_revision=release_sha,
    )
    return signature_bytes


def _safe_public_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"public evidence directory is unavailable: {path}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ReleaseError(f"public evidence directory is unsafe: {path}")


def _read_public_file(
    path: Path,
    *,
    size_cap: int,
    label: str,
    allowed_links: frozenset[int] = frozenset({1}),
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReleaseError(f"{label} is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink not in allowed_links
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size <= 0
            or info.st_size > size_cap
        ):
            if stat.S_ISREG(info.st_mode) and info.st_nlink not in allowed_links:
                raise ReleaseError(f"{label} has an untrusted hardlink alias")
            raise ReleaseError(f"{label} is not an owner-controlled bounded file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(size_cap + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload or len(payload) > size_cap:
        raise ReleaseError(f"{label} exceeds its size cap")
    return payload


def _fsync_public_directory(directory: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(directory, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ReleaseError("public evidence directory changed during publication")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_pending_publication(
    path: Path,
    *,
    size_cap: int,
    label: str,
    allowed_links: frozenset[int],
) -> tuple[bytes, os.stat_result] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReleaseError(f"{label} recovery file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink not in allowed_links
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size > size_cap
        ):
            if stat.S_ISREG(info.st_mode) and info.st_nlink not in allowed_links:
                raise ReleaseError(f"{label} recovery file has a hardlink alias")
            raise ReleaseError(f"{label} recovery file is unsafe")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(size_cap + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > size_cap:
        raise ReleaseError(f"{label} recovery file exceeds its size cap")
    return payload, info


@contextmanager
def _publication_lock(directory: Path):
    """Serialize recovery and publication; process death releases the lock."""
    _safe_public_directory(directory)
    lock_path = directory / ".sn39-publication.lock"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    created = False
    descriptor = -1
    try:
        descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _fsync_public_directory(directory)
    except FileExistsError:
        try:
            descriptor = os.open(lock_path, flags)
        except OSError as exc:
            raise ReleaseError("public publication lock is unavailable") from exc
    except ReleaseError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ReleaseError("public publication lock is unavailable") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise ReleaseError("public publication lock cannot be acquired") from exc
        try:
            info = os.fstat(descriptor)
            path_info = lock_path.lstat()
        except OSError as exc:
            raise ReleaseError("public publication lock changed") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != 0
            or path_info.st_dev != info.st_dev
            or path_info.st_ino != info.st_ino
            or path_info.st_nlink != 1
        ):
            raise ReleaseError(
                "public publication lock is not a private single-link "
                "owner-controlled file"
            )
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    if created:
        # The lock is deliberately persistent so no process can lock an
        # unlinked predecessor inode while a peer locks a replacement inode.
        _fsync_public_directory(directory)


def _publish_once_locked(
    path: Path,
    payload: bytes,
    *,
    size_cap: int,
    label: str,
    conflict: str,
) -> None:
    """Publish while the directory's persistent publication lock is held."""
    pending = path.parent / f".{path.name}.pending"
    try:
        final_info = path.lstat()
    except FileNotFoundError:
        final_info = None
    except OSError as exc:
        raise ReleaseError(f"{label} is unavailable") from exc

    if final_info is not None:
        if (
            stat.S_ISLNK(final_info.st_mode)
            or not stat.S_ISREG(final_info.st_mode)
            or final_info.st_uid != os.geteuid()
            or stat.S_IMODE(final_info.st_mode) & 0o022
        ):
            raise ReleaseError(f"{label} is not an owner-controlled file")
        if final_info.st_nlink == 2:
            recovery = _read_pending_publication(
                pending,
                size_cap=size_cap,
                label=label,
                allowed_links=frozenset({2}),
            )
            if recovery is None:
                raise ReleaseError(f"{label} has an untrusted hardlink alias")
            pending_bytes, pending_info = recovery
            if (
                pending_info.st_dev != final_info.st_dev
                or pending_info.st_ino != final_info.st_ino
                or pending_bytes != payload
            ):
                raise ReleaseError(f"{label} has an untrusted hardlink alias")
            os.unlink(pending)
            _fsync_public_directory(path.parent)
            existing = _read_public_file(path, size_cap=size_cap, label=label)
            if existing != payload:
                raise ReleaseError(conflict)
            return
        if final_info.st_nlink != 1:
            raise ReleaseError(f"{label} has an untrusted hardlink alias")
        existing = _read_public_file(path, size_cap=size_cap, label=label)
        if existing != payload:
            raise ReleaseError(conflict)
        stale = _read_pending_publication(
            pending,
            size_cap=size_cap,
            label=label,
            allowed_links=frozenset({1}),
        )
        if stale is not None:
            os.unlink(pending)
        # Re-fsync even on an idempotent recovery. A prior process may have
        # died after unlinking the staging name but before that directory
        # update reached stable storage.
        _fsync_public_directory(path.parent)
        return

    staged = _read_pending_publication(
        pending,
        size_cap=size_cap,
        label=label,
        allowed_links=frozenset({1}),
    )
    if staged is not None and staged[0] != payload:
        # A crash may leave a short staging file. The final path is absent,
        # the staging inode is single-link and owner-controlled, so replacing
        # only this internal recovery name cannot alter published evidence.
        os.unlink(pending)
        _fsync_public_directory(path.parent)
        staged = None
    if staged is None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(pending, flags, 0o644)
        except FileExistsError:
            raise ReleaseError(f"{label} staging file appeared while locked")
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_public_directory(path.parent)
    try:
        os.link(pending, path, follow_symlinks=False)
    except FileExistsError:
        raise ReleaseError(f"{label} appeared while publication was locked")
    _fsync_public_directory(path.parent)
    os.unlink(pending)
    _fsync_public_directory(path.parent)
    existing = _read_public_file(path, size_cap=size_cap, label=label)
    if existing != payload:
        raise ReleaseError(conflict)


def _publish_once(
    path: Path,
    payload: bytes,
    *,
    size_cap: int,
    label: str,
    conflict: str,
) -> None:
    """Crash-recoverable publish-once, serialized across finalizer processes."""
    if not payload or len(payload) > size_cap:
        raise ReleaseError(f"{label} exceeds its size cap")
    with _publication_lock(path.parent):
        _publish_once_locked(
            path,
            payload,
            size_cap=size_cap,
            label=label,
            conflict=conflict,
        )


def put_blob(root: Path, payload: bytes) -> str:
    if not payload or len(payload) > MAX_PUBLIC_BLOB_BYTES:
        raise ReleaseError("public replay-result blob exceeds its size cap")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    directory = root / "blobs" / "sha256"
    _safe_public_directory(root)
    _safe_public_directory(root / "blobs")
    _safe_public_directory(directory)
    path = directory / digest.split(":", 1)[1]
    _publish_once(
        path,
        payload,
        size_cap=MAX_PUBLIC_BLOB_BYTES,
        label="public replay-result blob",
        conflict="public replay-result blob collides with other bytes",
    )
    return digest


def verify_frozen_release_evidence(
    release: dict[str, Any],
    *,
    replay_bytes: bytes,
    public_root: Path,
    subtensor: Any,
) -> None:
    from scaffold.sn39_public_reproduction import verify_frozen_evidence

    _require_finney_archive(subtensor)
    if not replay_bytes or len(replay_bytes) > MAX_PUBLIC_BLOB_BYTES:
        raise ReleaseError("generated replay result exceeds its size cap")
    replay_digest = "sha256:" + hashlib.sha256(replay_bytes).hexdigest()

    def load_blob(digest: str) -> bytes:
        if digest == replay_digest:
            return replay_bytes
        if SHA256.fullmatch(digest) is None:
            raise ReleaseError("release requested a malformed evidence digest")
        path = public_root / "blobs" / "sha256" / digest.split(":", 1)[1]
        payload = _read_public_file(
            path,
            size_cap=MAX_PUBLIC_BLOB_BYTES,
            label=f"frozen public blob {digest}",
        )
        if payload is None:
            raise ReleaseError(f"frozen public blob is unavailable: {digest}")
        if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
            raise ReleaseError(f"frozen public blob is corrupt: {digest}")
        return payload

    result = verify_frozen_evidence(
        release,
        subtensor=subtensor,
        load_public_blob=load_blob,
    )
    if (
        result.get("evidence_checkpoint") != "PASS"
        or result.get("evidence_candidate_set") != "PASS"
        or result.get("root_finalizer_tdx_replay") != "PASS"
    ):
        raise ReleaseError("frozen public evidence did not reproduce")


def atomic_write(path: Path, payload: bytes) -> None:
    """Durably publish immutable bytes, accepting only an idempotent rerun."""
    _publish_once(
        path,
        payload,
        size_cap=MAX_RELEASE_BYTES,
        label=f"public release artifact {path.name}",
        conflict=(f"public release artifact {path.name} is already sealed differently"),
    )


def _archive_subtensor() -> Any:
    try:
        import bittensor as bt

        archive = bt.Subtensor(network="archive")
    except Exception as exc:
        raise ReleaseError("cannot connect to the Finney archive") from exc
    _require_finney_archive(archive)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    if not args.release.is_absolute():
        raise ReleaseError("release checkout path must be absolute")
    _require_launcher_context(
        release_sha=args.release_sha,
        journal=args.journal,
    )
    try:
        requested_release = args.release.lstat()
        release_root = args.release.resolve(strict=True)
        script_root = Path(__file__).resolve(strict=True).parents[1]
    except (OSError, RuntimeError) as exc:
        raise ReleaseError("release checkout path cannot be resolved safely") from exc
    if stat.S_ISLNK(requested_release.st_mode) or release_root != script_root:
        raise ReleaseError(
            "finalizer must run from the exact non-symlink release being sealed"
        )
    verify_release_checkout(release_root, args.release_sha)
    if str(release_root) not in sys.path:
        sys.path.insert(0, str(release_root))
    state = read_launch_journal(args.journal)
    archive = _archive_subtensor()
    release, replay_bytes = build_release(
        state,
        release_sha=args.release_sha,
        subtensor=archive,
    )
    verify_frozen_release_evidence(
        release,
        replay_bytes=replay_bytes,
        public_root=PUBLIC_ROOT,
        subtensor=archive,
    )
    release_bytes = canonical_json(release)
    signature_bytes = build_signature(
        release_bytes,
        seed=_read_root_seed(PRIVATE_SEED),
        release_sha=args.release_sha,
        release_root=release_root,
    )
    expected_replay = release["launch_submission"]["evidence_checkpoint"][
        "replay_result"
    ]
    actual_replay = put_blob(PUBLIC_ROOT, replay_bytes)
    if actual_replay != expected_replay:
        raise ReleaseError("published replay-result digest differs from the release")
    atomic_write(PUBLIC_ROOT / "release.json", release_bytes)
    atomic_write(PUBLIC_ROOT / "release.json.sig", signature_bytes)
    print(
        json.dumps(
            {
                "extrinsic_hash": release["launch_submission"]["extrinsic"]["hash"],
                "release_sha256": (
                    "sha256:" + hashlib.sha256(release_bytes).hexdigest()
                ),
                "replay_result": actual_replay,
                "status": "SN39_PUBLIC_RELEASE_PUBLISHED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
