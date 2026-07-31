"""Fail closed unless the immutable SN39 launch proof reproduces."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

RELEASE_URL = "https://api.cathedral.computer/v1/evidence/release.json"
SIGNATURE_URL = RELEASE_URL + ".sig"
RELEASE_KEY_ID = "cathedral-release-attestation-sn39-20260724"
MAX_RELEASE_BYTES = 128 * 1024
MAX_BLOB_BYTES = 4 * 1024 * 1024
PUBLIC_EVIDENCE_BASE = "https://api.cathedral.computer/v1/evidence"
PUBLIC_REPRODUCTION_DEADLINE_SECS = 120.0
EXPECTED_POLICY_KEY_HEX = "10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26"  # pragma: allowlist secret
EXPECTED_POLICY_KEY_ID = "cathedral-weight-policy"
EXPECTED_PRODUCER_REVISION = (
    "655c264421a1f5f2e625a372a40f595aa1e114ab"  # pragma: allowlist secret
)
EXPECTED_REPORT_KEY_ID = "cathedral-score-sn39-20260724"
FINNEY_GENESIS_HASH = (
    "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
)
EXPECTED_RELEASE_PINS = {
    "registry_keys": (
        "sha256:5fb8f00cd2541606927373f596c2ba77d4ce485df0539f4afd5091858af48512"
    ),
    "report_keys": (
        "sha256:30e438fff5b0508402b233eb5eec590a834882801a552edbbf7e62e45cf98c70"
    ),
    "index_keys": (
        "sha256:1e35b9ce36b3da3362a88feb93dfa90f1fe03ab7c42e902b13ac3789324f7611"
    ),
    "release_attestation_keys": (
        "sha256:1a60a22de160853d460b22853a426d0534fab4df0fe9f89e5859d60bb4ed3d12"
    ),
    "reproduction_dependencies": (
        "sha256:765f90428c0fbdb8fc58f03e82edd6dd9ea1cc50bd685dc2bdbbecef30aa1624"
    ),
    "reproduction_build_dependencies": (
        "sha256:b212eed198712c8f54ad6250dc64575485bef5c3c311d71ee3c24a2c80396912"
    ),
    "verifier_binary": (
        "sha256:35bb55f89f411d5dcf5f72be90488e999ee68c41dfc0429a0dcb8cc2b448b6bb"
    ),
    "verifier_implementation": (
        "sha256:8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
    ),
}
WIRE_VALIDATED_SUPPLY_U16 = 65535
WIRE_BURN_U16 = 7282
WIRE_TOTAL = WIRE_VALIDATED_SUPPLY_U16 + WIRE_BURN_U16
WIRE_VALIDATED_SUPPLY_SHARE = WIRE_VALIDATED_SUPPLY_U16 / WIRE_TOTAL
WIRE_BURN_SHARE = WIRE_BURN_U16 / WIRE_TOTAL
EXPECTED_VERSION_KEY = 10005000
RELEASE_SCHEMA = "cathedral_sn39_provenance_release_v2"
UID_SAFETY_SCHEMA = "cathedral_sn39_uid_safety_v2"
UID_SAFETY_STABILITY_BASIS = "operator_controlled_coldkeys"
POST_ROTATION_EVIDENCE_SCHEMA = "cathedral_sn39_post_rotation_evidence_v2"
SWAP_LOCK_STATES = frozenset({"never_rotated", "expired", "active"})
MORTAL_PERIOD_BLOCKS = 4


class ReproductionError(ValueError):
    """The public reproduction did not prove the documented result."""


class ReproductionNotProven(ReproductionError):
    """Required public or archive material was unavailable for reproduction."""


def _repo_revision(root: Path) -> str:
    try:
        revision = subprocess.check_output(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        checkout_changes = subprocess.check_output(
            [
                "/usr/bin/git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReproductionNotProven(
            "cannot resolve the reproducer Git revision"
        ) from exc
    if checkout_changes:
        raise ReproductionError(
            "reproducer checkout is not pristine (modified, untracked, or ignored "
            "files are forbidden)"
        )
    return revision


def _is_hash(value: Any, *, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value) == len(prefix) + 64
        and all(character in "0123456789abcdef" for character in value[len(prefix) :])
    )


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _canonical_document(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _strict_json_bytes(
    payload: bytes,
    *,
    label: str,
    canonical: bool = True,
    allow_trailing_newline: bool = False,
) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReproductionError(f"{label} has duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise ReproductionError(f"{label} has a non-finite JSON number")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ReproductionError(f"{label} has a non-finite JSON number")
        return parsed

    try:
        document = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except ReproductionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReproductionError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ReproductionError(f"{label} is not a JSON object")
    if canonical:
        encoded = _canonical_document(document)
        accepted = {encoded}
        if allow_trailing_newline:
            accepted.add(encoded + b"\n")
        if payload not in accepted:
            raise ReproductionError(f"{label} bytes are not canonical JSON")
    return document


def _parse_utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReproductionError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ReproductionError(f"{label} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ReproductionError(f"{label} is not UTC")
    return parsed


def _validate_post_rotation_boundary(
    launch: dict[str, Any],
    *,
    uid_safety: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    targets = uid_safety["rotation"]["targets"]
    receipts: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            raise ReproductionError("signed target rotation row is malformed")
        swap_lock = target.get("swap_lock")
        receipt = target.get("rotation_receipt")
        if swap_lock not in SWAP_LOCK_STATES:
            raise ReproductionError("signed target rotation lock state is malformed")
        if swap_lock != "active":
            # Only a claimed live lock carries a proof; anything else must not.
            if receipt is not None:
                raise ReproductionError("signed target rotation receipt is malformed")
            continue
        if (
            not isinstance(receipt, dict)
            or set(receipt)
            != {
                "call",
                "extrinsic_hash",
                "block_hash",
                "block_number",
                "block_timestamp",
                "extrinsic_index",
                "coldkey",
                "old_hotkey",
                "new_hotkey",
                "netuid",
                "keep_stake",
                "event",
            }
            or receipt.get("call") != "swap_hotkey_v2"
            or not _is_hash(receipt.get("extrinsic_hash"), prefix="0x")
            or not _is_hash(receipt.get("block_hash"), prefix="0x")
            or receipt.get("block_number") != target.get("last_hotkey_swap_block")
            or receipt.get("coldkey") != target.get("coldkey")
            or receipt.get("new_hotkey") != target.get("hotkey")
            or receipt.get("netuid") != 39
            or not isinstance(receipt.get("keep_stake"), bool)
            or receipt.get("event") != "HotkeySwappedOnSubnet"
            or isinstance(receipt.get("extrinsic_index"), bool)
            or not isinstance(receipt.get("extrinsic_index"), int)
            or receipt.get("extrinsic_index") < 0
        ):
            raise ReproductionError("signed target rotation receipt is malformed")
        receipts.append(receipt)
    freshness = checkpoint.get("freshness_boundary")
    if not isinstance(freshness, dict):
        raise ReproductionError("signed evidence has no post-rotation boundary")
    rotation_floor_block: int | None = None
    rotation_floor_time: datetime | None = None
    if receipts:
        rotation_floor_block = max(int(row["block_number"]) for row in receipts)
        rotation_floor_time = max(
            _parse_utc(row["block_timestamp"], label="rotation block timestamp")
            for row in receipts
        )
    vector = launch["signed_vector"]
    index = checkpoint["signed_index"]
    expected_keys = {
        "schema",
        "rotation_floor_block",
        "rotation_floor_timestamp",
        "candidate_block",
        "candidate_block_hash",
        "manifest_generated_at",
        "report_generated_at",
        "report_valid_from_block",
        "vector_generated_at",
        "index_generated_at",
    }
    if rotation_floor_time is None:
        floor_timestamp_matches = freshness.get("rotation_floor_timestamp") is None
    else:
        floor_timestamp_matches = (
            _parse_utc(
                freshness.get("rotation_floor_timestamp"),
                label="rotation floor timestamp",
            )
            == rotation_floor_time
        )
    if (
        set(freshness) != expected_keys
        or freshness.get("schema") != POST_ROTATION_EVIDENCE_SCHEMA
        or freshness.get("rotation_floor_block") != rotation_floor_block
        or not floor_timestamp_matches
        or isinstance(freshness.get("candidate_block"), bool)
        or not isinstance(freshness.get("candidate_block"), int)
        or not _is_hash(freshness.get("candidate_block_hash"), prefix="0x")
        or isinstance(freshness.get("report_valid_from_block"), bool)
        or not isinstance(freshness.get("report_valid_from_block"), int)
        or freshness.get("vector_generated_at") != vector.get("generated_at")
        or freshness.get("index_generated_at") != index.get("generated_at")
    ):
        raise ReproductionError("signed post-rotation evidence boundary is malformed")
    generated_times = {
        name: _parse_utc(freshness.get(name), label=name)
        for name in (
            "manifest_generated_at",
            "report_generated_at",
            "vector_generated_at",
            "index_generated_at",
        )
    }
    if rotation_floor_block is None or rotation_floor_time is None:
        # No target carried a live rotation lock, so there is no floor for the
        # evidence to postdate.
        return freshness
    if (
        freshness["candidate_block"] <= rotation_floor_block
        or freshness["report_valid_from_block"] <= rotation_floor_block
        or any(
            generated <= rotation_floor_time for generated in generated_times.values()
        )
    ):
        raise ReproductionError(
            "signed evidence generation does not follow the proven rotations"
        )
    return freshness


def _validate_launch_submission(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReproductionError("signed release has no exact launch submission")
    vector = raw.get("signed_vector")
    if not isinstance(vector, dict):
        raise ReproductionError("signed release has no exact signed vector")
    vector_digest = "sha256:" + hashlib.sha256(_canonical_document(vector)).hexdigest()
    if (
        raw.get("vector_id") != vector.get("vector_id")
        or raw.get("policy_version") != vector.get("policy_version")
        or raw.get("signed_vector_sha256") != vector_digest
    ):
        raise ReproductionError("signed launch vector identity or digest differs")

    mapping = raw.get("mapping")
    snapshot = (mapping or {}).get("metagraph_snapshot")
    if (
        not isinstance(mapping, dict)
        or isinstance(mapping.get("block"), bool)
        or not isinstance(mapping.get("block"), int)
        or mapping.get("block") < 1
        or not isinstance(snapshot, dict)
        or snapshot.get("network") != "finney"
        or snapshot.get("netuid") != 39
        or snapshot.get("block") != mapping.get("block")
        or not _is_hash(snapshot.get("block_hash"), prefix="0x")
        or not isinstance(snapshot.get("uids"), list)
        or not isinstance(snapshot.get("hotkeys"), list)
        or not isinstance(snapshot.get("validator_permit"), list)
        or len(snapshot["uids"]) != len(snapshot["hotkeys"])
        or len(snapshot["validator_permit"]) != len(snapshot["hotkeys"])
        or not snapshot["uids"]
        or not snapshot["hotkeys"]
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in snapshot["uids"]
        )
        or len(set(snapshot["uids"])) != len(snapshot["uids"])
        or len(set(snapshot["hotkeys"])) != len(snapshot["hotkeys"])
        or any(not isinstance(value, str) or not value for value in snapshot["hotkeys"])
        or any(not isinstance(value, bool) for value in snapshot["validator_permit"])
        or mapping.get("commit_reveal_enabled") is not False
    ):
        raise ReproductionError("signed historical metagraph snapshot is malformed")
    validator_uid = mapping.get("validator_uid")
    rewarded_uid = mapping.get("rewarded_uid")
    burn_uid = mapping.get("burn_uid")
    uid_weights = mapping.get("uid_weights")
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (validator_uid, rewarded_uid, burn_uid)
        )
        or len({validator_uid, rewarded_uid, burn_uid}) != 3
        or not isinstance(mapping.get("validator_hotkey"), str)
        or not mapping["validator_hotkey"]
        or not isinstance(mapping.get("rewarded_hotkey"), str)
        or not mapping["rewarded_hotkey"]
        or not isinstance(mapping.get("burn_hotkey"), str)
        or not mapping["burn_hotkey"]
        or len(
            {
                mapping["validator_hotkey"],
                mapping["rewarded_hotkey"],
                mapping["burn_hotkey"],
            }
        )
        != 3
        or not isinstance(uid_weights, dict)
        or set(uid_weights) != {str(rewarded_uid), str(burn_uid)}
        or not _is_finite_number(uid_weights.get(str(rewarded_uid)))
        or not _is_finite_number(uid_weights.get(str(burn_uid)))
        or not math.isclose(
            float(uid_weights[str(rewarded_uid)]),
            0.9,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            float(uid_weights[str(burn_uid)]),
            0.1,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or isinstance(mapping.get("next_epoch_start_block"), bool)
        or not isinstance(mapping.get("next_epoch_start_block"), int)
    ):
        raise ReproductionError("signed launch UID mapping differs from 90/10")

    extrinsic = raw.get("extrinsic")
    intent = raw.get("broadcast_intent")
    ordered_uids = sorted((rewarded_uid, burn_uid))
    expected_wire_weights = [
        WIRE_VALIDATED_SUPPLY_U16 if uid == rewarded_uid else WIRE_BURN_U16
        for uid in ordered_uids
    ]
    if (
        not isinstance(extrinsic, dict)
        or not _is_hash(extrinsic.get("hash"), prefix="0x")
        or not _is_hash(extrinsic.get("block_hash"), prefix="0x")
        or not isinstance(extrinsic.get("block"), int)
        or extrinsic.get("block") <= mapping.get("block")
        or extrinsic.get("block") >= mapping.get("next_epoch_start_block")
        or extrinsic.get("validator_uid") != validator_uid
        or extrinsic.get("uids") != ordered_uids
        or extrinsic.get("weights_u16") != expected_wire_weights
        or extrinsic.get("version_key") != EXPECTED_VERSION_KEY
    ):
        raise ReproductionError("signed launch extrinsic is malformed")
    if (
        not isinstance(intent, dict)
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
        or intent.get("extrinsic_hash") != extrinsic["hash"]
        or isinstance(intent.get("nonce"), bool)
        or not isinstance(intent.get("nonce"), int)
        or intent.get("nonce") < 0
        or intent.get("era_reference_block") != mapping["block"]
        or intent.get("mortal_period_blocks") != MORTAL_PERIOD_BLOCKS
        or intent.get("version_key") != extrinsic["version_key"]
        or intent.get("wire_uids") != extrinsic["uids"]
        or intent.get("wire_weights") != extrinsic["weights_u16"]
        or not (
            intent["era_reference_block"]
            <= extrinsic["block"]
            < intent["era_reference_block"] + intent["mortal_period_blocks"]
        )
    ):
        raise ReproductionError("signed launch broadcast intent is malformed")
    uid_safety = mapping.get("uid_safety")
    if (
        not isinstance(uid_safety, dict)
        or uid_safety.get("schema") != UID_SAFETY_SCHEMA
        or uid_safety.get("stability_basis") != UID_SAFETY_STABILITY_BASIS
        or not isinstance(uid_safety.get("registration"), dict)
        or not isinstance(uid_safety.get("rotation"), dict)
        or uid_safety["rotation"].get("status") != "PASS"
        or uid_safety["rotation"].get("mapping_block") != mapping["block"]
        or uid_safety["rotation"].get("mortal_period_blocks") != MORTAL_PERIOD_BLOCKS
        or uid_safety["rotation"].get("era_last_block")
        != mapping["block"] + MORTAL_PERIOD_BLOCKS - 1
        or not isinstance(uid_safety["rotation"].get("targets"), list)
        or {
            row.get("uid")
            for row in uid_safety["rotation"]["targets"]
            if isinstance(row, dict)
        }
        != {rewarded_uid, burn_uid}
    ):
        raise ReproductionError("signed launch UID/hotkey safety proof is malformed")

    checkpoint = raw.get("evidence_checkpoint")
    frozen_index = (checkpoint or {}).get("signed_index")
    if (
        not isinstance(checkpoint, dict)
        or not isinstance(checkpoint.get("source_epoch"), int)
        or not _is_hash(checkpoint.get("manifest"), prefix="sha256:")
        or not _is_hash(checkpoint.get("report_id"), prefix="sha256:")
        or not isinstance(checkpoint.get("policy_release"), int)
        or checkpoint.get("policy_release") < 1
        or not _is_hash(checkpoint.get("policy_digest"), prefix="sha256:")
        or checkpoint.get("report_signing_key_id") != EXPECTED_REPORT_KEY_ID
        or checkpoint.get("reward_mechanism")
        != {"id": "validated_supply_v1", "revision": 1}
        or checkpoint.get("verifier_digest")
        != EXPECTED_RELEASE_PINS["verifier_implementation"]
        or checkpoint.get("verifier_binary_digest")
        != EXPECTED_RELEASE_PINS["verifier_binary"]
        or not _is_hash(checkpoint.get("replay_result"), prefix="sha256:")
        or checkpoint.get("public_assurance") != "receipts_only"
        or not isinstance(frozen_index, dict)
        or (frozen_index.get("latest") or {}).get("manifest")
        != checkpoint.get("manifest")
        or (frozen_index.get("latest") or {}).get("source_epoch")
        != checkpoint.get("source_epoch")
    ):
        raise ReproductionError("signed evidence checkpoint is malformed")
    _validate_post_rotation_boundary(
        raw,
        uid_safety=uid_safety,
        checkpoint=checkpoint,
    )
    return raw


def verify_release_bytes(
    release_bytes: bytes,
    signature_bytes: bytes,
    *,
    public_keys: dict[str, str],
    repo_revision: str,
) -> dict[str, Any]:
    """Verify operator approval and bind it to the exact checked-out commit."""
    release = _strict_json_bytes(release_bytes, label="release attestation")
    signature = _strict_json_bytes(
        signature_bytes,
        label="release signature",
        allow_trailing_newline=True,
    )
    if (
        set(signature)
        != {
            "algorithm",
            "key_id",
            "payload",
            "payload_sha256",
            "signature",
        }
        or signature.get("payload") != "release.json exact bytes"
    ):
        raise ReproductionError("release signature envelope differs")
    if (
        signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != RELEASE_KEY_ID
        or release.get("release_attestation", {}).get("key_id") != RELEASE_KEY_ID
    ):
        raise ReproductionError("release attestation key or algorithm differs")
    expected_digest = "sha256:" + hashlib.sha256(release_bytes).hexdigest()
    if signature.get("payload_sha256") != expected_digest:
        raise ReproductionError("release attestation payload digest differs")
    try:
        public = base64.b64decode(public_keys[RELEASE_KEY_ID], validate=True)
        detached = base64.b64decode(signature["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(detached, release_bytes)
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise ReproductionError("release attestation signature is invalid") from exc
    reproducer_revision = release.get("reproducer_revision")
    if (
        not isinstance(reproducer_revision, str)
        or len(reproducer_revision) != 40
        or any(character not in "0123456789abcdef" for character in reproducer_revision)
        or repo_revision != reproducer_revision
    ):
        raise ReproductionError(
            "checked-out code is not the signed reproducer revision"
        )
    if (
        release.get("schema") != RELEASE_SCHEMA
        or release.get("network") != "finney"
        or release.get("netuid") != 39
        or release.get("validated_capability") != "intel_tdx_cpu"
        or release.get("submission_authority_default") != "thin"
        or release.get("full_provenance_mode") != "concurrent_shadow"
        or release.get("claim") != "SN39 mainnet: validated Intel TDX CPU compute."
    ):
        raise ReproductionError("signed release contract differs from the launch")
    if release.get("reward_mechanism") != {
        "id": "validated_supply_v1",
        "revision": 1,
        "validated_supply_share": 0.9,
        "burn_share": 0.1,
        "wire_quantization": {
            "weights_u16": [WIRE_VALIDATED_SUPPLY_U16, WIRE_BURN_U16],
            "effective_validated_supply_share": WIRE_VALIDATED_SUPPLY_SHARE,
            "effective_burn_share": WIRE_BURN_SHARE,
        },
    }:
        raise ReproductionError("signed reward mechanism differs from the launch")
    if release.get("source_revisions") != {
        "producer": EXPECTED_PRODUCER_REVISION,
        "validator": reproducer_revision,
    }:
        raise ReproductionError("signed source revisions differ from the launch")
    if release.get("pins") != EXPECTED_RELEASE_PINS:
        raise ReproductionError("signed release pins differ from the launch")
    launch = _validate_launch_submission(release.get("launch_submission"))
    return {
        "release_attestation": "PASS",
        "reproducer_revision": reproducer_revision,
        "release": release,
        "launch_vector_id": launch["vector_id"],
    }


def _call_arg(call: dict[str, Any], name: str) -> Any:
    for item in call.get("call_args") or ():
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("value")
    raise ReproductionError(f"launch extrinsic lacks {name}")


def _block_timestamp_ms(substrate: Any, block_hash: str) -> int:
    value = substrate.query(
        module="Timestamp",
        storage_function="Now",
        block_hash=block_hash,
    )
    raw = getattr(value, "value", value)
    if raw is None:
        raise ReproductionNotProven("launch inclusion block timestamp is unavailable")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ReproductionError("launch inclusion block timestamp is malformed")
    return raw


def _finney_subtensor() -> Any:
    try:
        import bittensor as bt

        return bt.Subtensor(network="archive")
    except Exception as exc:
        raise ReproductionNotProven("cannot connect to the Finney archive") from exc


def _require_finney_archive(
    subtensor: Any,
    *,
    deadline: float | None = None,
) -> str:
    """Fail closed unless the supplied archive is the pinned Finney chain."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        raise ReproductionNotProven("Finney archive substrate is unavailable")
    genesis_hash = _bounded_archive_call(
        deadline,
        "Finney genesis lookup",
        lambda: substrate.get_block_hash(0),
    )
    if genesis_hash is None:
        raise ReproductionNotProven("Finney genesis lookup returned no block hash")
    observed = str(genesis_hash).lower()
    if observed != FINNEY_GENESIS_HASH:
        raise ReproductionError("archive differs from the pinned Finney genesis")
    return observed


def _bounded_archive_call(
    deadline: float | None,
    label: str,
    operation: Callable[[], Any],
) -> Any:
    """Run one read-only archive operation inside the command-wide deadline."""
    if deadline is None:
        try:
            return operation()
        except ReproductionError:
            raise
        except Exception as exc:
            raise ReproductionNotProven(f"{label} is unavailable") from exc
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReproductionNotProven(
            f"public reproduction deadline expired before {label}"
        )
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            outcome.put((True, operation()))
        except BaseException as exc:  # noqa: BLE001 - transfer to caller
            outcome.put((False, exc))

    worker = threading.Thread(
        target=invoke,
        name="sn39-public-archive-read",
        daemon=True,
    )
    worker.start()
    worker.join(remaining)
    if worker.is_alive():
        raise ReproductionNotProven(
            f"public reproduction deadline exceeded during {label}"
        )
    succeeded, value = outcome.get_nowait()
    if not succeeded:
        if isinstance(value, ReproductionError):
            raise value
        raise ReproductionNotProven(f"{label} is unavailable") from value
    return value


def _materialize_execution_receipt(receipt: Any) -> dict[str, Any]:
    if receipt is None:
        raise ReproductionNotProven("launch extrinsic execution receipt is unavailable")
    return {
        "extrinsic_idx": int(getattr(receipt, "extrinsic_idx", -1)),
        "is_success": getattr(receipt, "is_success", None),
        "error_message": getattr(receipt, "error_message", None),
    }


def _materialize_finalized_head(substrate: Any) -> tuple[str, int, str]:
    block_hash = str(substrate.get_chain_finalised_head())
    block_number = int(substrate.get_block_number(block_hash))
    return block_hash, block_number, str(substrate.get_block_hash(block_number))


def _archive_storage_value(
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
    except Exception as exc:
        raise ReproductionNotProven(
            f"historical {name} storage is unavailable"
        ) from exc
    return getattr(observed, "value", observed)


def _archive_constant_value(
    substrate: Any,
    *,
    name: str,
    block_hash: str,
) -> Any:
    try:
        observed = substrate.get_constant(
            module_name="SubtensorModule",
            constant_name=name,
            block_hash=block_hash,
        )
    except Exception as exc:
        raise ReproductionNotProven(
            f"historical {name} constant is unavailable"
        ) from exc
    return getattr(observed, "value", observed)


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
    except Exception as exc:
        raise ReproductionNotProven(
            "historical target rotation block is unavailable"
        ) from exc
    if (
        not _is_hash(block_hash, prefix="0x")
        or canonical_number != block_number
        or not isinstance(block, dict)
        or not isinstance(block.get("extrinsics"), (list, tuple))
    ):
        raise ReproductionError("historical target rotation block is non-canonical")
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
        raise ReproductionError(
            "historical target has no unique exact swap_hotkey_v2 call"
        )
    extrinsic_index, observed = matching[0]
    call = observed["call"]
    extrinsic_hash = str(observed.get("extrinsic_hash", "")).lower()
    old_hotkey = _call_arg(call, "hotkey")
    keep_stake = _call_arg(call, "keep_stake")
    if (
        not _is_hash(extrinsic_hash, prefix="0x")
        or not isinstance(old_hotkey, str)
        or not old_hotkey
        or old_hotkey == target_hotkey
        or not isinstance(keep_stake, bool)
    ):
        raise ReproductionError("historical target rotation call is malformed")
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
    except Exception as exc:
        raise ReproductionNotProven(
            "historical target rotation execution is unavailable"
        ) from exc
    if (
        receipt_index != extrinsic_index
        or receipt_success is not True
        or receipt_error is not None
        or not isinstance(events, (list, tuple))
        or isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or timestamp_ms <= 0
    ):
        raise ReproductionError(
            "historical target rotation receipt is incomplete or failed"
        )
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
        raise ReproductionError(
            "historical target rotation event is absent or ambiguous"
        )
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


def _recompute_uid_safety(
    subtensor: Any,
    *,
    metagraph: Any,
    mapping: dict[str, Any],
    block_hash: str,
) -> dict[str, Any]:
    """Rebuild the signed UID/hotkey-safety document from archive state."""
    block = int(mapping["block"])
    try:
        canonical_block_hash = subtensor.substrate.get_block_hash(block)
    except Exception as exc:
        raise ReproductionNotProven(
            "historical mapping block hash is unavailable from the archive"
        ) from exc
    if canonical_block_hash is None:
        raise ReproductionNotProven(
            "historical mapping block hash is unavailable from the archive"
        )
    if str(canonical_block_hash).lower() != block_hash.lower():
        raise ReproductionError(
            "historical mapping block hash contradicts the signed release"
        )
    raw_uids = getattr(metagraph, "uids", ())
    if hasattr(raw_uids, "tolist"):
        raw_uids = raw_uids.tolist()
    uids = [int(value) for value in raw_uids]
    hotkeys = [str(value) for value in getattr(metagraph, "hotkeys", ())]
    try:
        max_uids = int(getattr(metagraph, "max_uids"))
        hparams = getattr(metagraph, "hparams")
        max_regs_per_block = int(getattr(hparams, "max_regs_per_block"))
        immunity_period = int(getattr(hparams, "immunity_period"))
        registration_blocks = [
            int(value) for value in getattr(metagraph, "block_at_registration")
        ]
        min_nonimmune_uids = int(
            _archive_storage_value(
                subtensor,
                name="MinNonImmuneUids",
                params=[39],
                block=block,
            )
        )
        subnet_owner_coldkey = str(
            _archive_storage_value(
                subtensor,
                name="SubnetOwner",
                params=[39],
                block=block,
            )
            or ""
        )
        owned_hotkeys = [
            str(value)
            for value in (
                _archive_storage_value(
                    subtensor,
                    name="OwnedHotkeys",
                    params=[subnet_owner_coldkey],
                    block=block,
                )
                or ()
            )
        ]
        immune_owner_uids_limit = int(
            _archive_storage_value(
                subtensor,
                name="ImmuneOwnerUidsLimit",
                params=[39],
                block=block,
            )
        )
    except ReproductionError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReproductionError(
            "historical UID replacement-safety inputs are malformed"
        ) from exc
    if (
        len(uids) != len(hotkeys)
        or len(registration_blocks) != len(hotkeys)
        or max_uids < len(uids)
        or max_regs_per_block < 0
        or immunity_period < 0
        or min_nonimmune_uids < 0
        or not subnet_owner_coldkey
        or immune_owner_uids_limit < 0
        or len(set(owned_hotkeys)) != len(owned_hotkeys)
    ):
        raise ReproductionError(
            "historical UID replacement-safety inputs are malformed"
        )
    registration_by_hotkey = {
        hotkey: (uid, registered_at)
        for uid, hotkey, registered_at in zip(
            uids,
            hotkeys,
            registration_blocks,
        )
    }
    owner_rows = sorted(
        (registered_at, uid, hotkey)
        for hotkey in owned_hotkeys
        if (row := registration_by_hotkey.get(hotkey)) is not None
        for uid, registered_at in [row]
    )
    owner_immortal_rows = owner_rows[:immune_owner_uids_limit]
    owner_hotkey = str(mapping["burn_hotkey"])
    owner_current = registration_by_hotkey.get(owner_hotkey)
    if owner_current is not None and all(
        row[2] != owner_hotkey for row in owner_immortal_rows
    ):
        owner_uid, owner_registered_at = owner_current
        owner_immortal_rows.insert(
            0,
            (owner_registered_at, owner_uid, owner_hotkey),
        )
        owner_immortal_rows = owner_immortal_rows[:immune_owner_uids_limit]
    owner_immortal_hotkeys = {row[2] for row in owner_immortal_rows}
    free_uid_slots = max_uids - len(uids)
    maximum_era_registrations = max_regs_per_block * MORTAL_PERIOD_BLOCKS
    capacity_protects_all = free_uid_slots >= maximum_era_registrations
    temporally_immune_hotkeys = {
        hotkey
        for hotkey, registered_at in zip(hotkeys, registration_blocks)
        if registered_at + immunity_period >= block + MORTAL_PERIOD_BLOCKS
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
    target_rows = [
        (int(mapping["rewarded_uid"]), str(mapping["rewarded_hotkey"])),
        (int(mapping["burn_uid"]), str(mapping["burn_hotkey"])),
    ]
    if any(hotkey not in replacement_safe_hotkeys for _uid, hotkey in target_rows):
        raise ReproductionError(
            "historical target UID was not registration-replacement-safe"
        )
    try:
        hotkey_swap_interval = int(
            _archive_constant_value(
                subtensor.substrate,
                name="HotkeySwapOnSubnetInterval",
                block_hash=block_hash,
            )
        )
        coldkey_swap_delay = int(
            _archive_storage_value(
                subtensor,
                name="ColdkeySwapAnnouncementDelay",
                params=[],
                block=block,
            )
        )
    except ReproductionError:
        raise
    except (TypeError, ValueError) as exc:
        raise ReproductionError("historical swap constants are malformed") from exc
    if (
        hotkey_swap_interval < MORTAL_PERIOD_BLOCKS
        or coldkey_swap_delay < MORTAL_PERIOD_BLOCKS
    ):
        raise ReproductionError("historical swap constants do not cover the mortal era")
    era_last_block = block + MORTAL_PERIOD_BLOCKS - 1
    targets: list[dict[str, Any]] = []
    for uid, hotkey in sorted(target_rows):
        coldkey = str(
            _archive_storage_value(
                subtensor,
                name="Owner",
                params=[hotkey],
                block=block,
            )
            or ""
        )
        if not coldkey:
            raise ReproductionError("historical target hotkey owner is malformed")
        try:
            last_swap_block = int(
                _archive_storage_value(
                    subtensor,
                    name="LastHotkeySwapOnNetuid",
                    params=[39, coldkey],
                    block=block,
                )
            )
        except ReproductionError:
            raise
        except (TypeError, ValueError) as exc:
            raise ReproductionError(
                "historical target hotkey swap block is malformed"
            ) from exc
        pending = _archive_storage_value(
            subtensor,
            name="ColdkeySwapAnnouncements",
            params=[coldkey],
            block=block,
        )
        if pending is not None:
            raise ReproductionError(
                "historical target coldkey had a pending swap announcement"
            )
        # Mirrors the validator and the finalizer: publish the lock state, prove
        # a live lock, and require none. These three recomputations are compared
        # for equality, so they must stay byte-identical.
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
                subtensor.substrate,
                block_number=last_swap_block,
                coldkey=coldkey,
                target_hotkey=hotkey,
            )
            successor = _archive_storage_value(
                subtensor,
                name="HotkeySuccessor",
                params=[39, hotkey],
                block=block,
            )
            root = _archive_storage_value(
                subtensor,
                name="HotkeyRoot",
                params=[39, hotkey],
                block=block,
            )
            old_successor = _archive_storage_value(
                subtensor,
                name="HotkeySuccessor",
                params=[39, rotation_receipt["old_hotkey"]],
                block=block,
            )
            old_root = _archive_storage_value(
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
                raise ReproductionError(
                    "historical target hotkey lineage differs from its rotation"
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
                "rotation_receipt": rotation_receipt,
                "registration_replacement_safe": True,
            }
        )
    return {
        "schema": UID_SAFETY_SCHEMA,
        "stability_basis": UID_SAFETY_STABILITY_BASIS,
        "registration": {
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
        },
        "rotation": {
            "status": "PASS",
            "mapping_block": block,
            "mapping_block_hash": block_hash,
            "mortal_period_blocks": MORTAL_PERIOD_BLOCKS,
            "era_last_block": era_last_block,
            "hotkey_swap_on_subnet_interval": hotkey_swap_interval,
            "coldkey_swap_announcement_delay": coldkey_swap_delay,
            "targets": targets,
        },
    }


def verify_historical_launch(
    release: dict[str, Any],
    *,
    subtensor: Any | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Verify the exact signed launch record against a Finney archive node."""
    from scaffold import wire_vector
    from scaffold.validator_thin import vector_to_uid_weights

    launch = _validate_launch_submission(release.get("launch_submission"))
    vector = launch["signed_vector"]
    try:
        wire_vector.verify_signature(
            vector,
            public_key_hex=EXPECTED_POLICY_KEY_HEX,
            expected_key_id=EXPECTED_POLICY_KEY_ID,
        )
    except Exception as exc:
        raise ReproductionError("launch vector signature is invalid") from exc
    if (
        vector.get("network") != "finney"
        or vector.get("netuid") != 39
        or vector.get("policy_metadata", {})
        .get("validated_supply", {})
        .get("contract_version")
        != "v2"
    ):
        raise ReproductionError("launch vector policy contract differs")

    if subtensor is None:
        subtensor = _bounded_archive_call(
            deadline,
            "Finney archive connection",
            _finney_subtensor,
        )
    _require_finney_archive(subtensor, deadline=deadline)

    mapping = launch["mapping"]
    snapshot = mapping["metagraph_snapshot"]
    mapping_block = int(mapping["block"])
    extrinsic = launch["extrinsic"]
    if mapping_block >= int(extrinsic["block"]):
        raise ReproductionError("historical mapping must precede launch extrinsic")
    (
        actual_mapping_hash,
        metagraph,
        historical_commit_reveal,
        historical_owner_hotkey,
        historical_next_epoch,
    ) = _bounded_archive_call(
        deadline,
        "historical launch metagraph lookup",
        lambda: (
            subtensor.get_block_hash(mapping_block),
            subtensor.metagraph(39, block=mapping_block),
            subtensor.commit_reveal_enabled(netuid=39, block=mapping_block),
            subtensor.get_subnet_owner_hotkey(39, block=mapping_block),
            subtensor.get_next_epoch_start_block(39, block=mapping_block),
        ),
    )
    if (
        actual_mapping_hash is None
        or metagraph is None
        or historical_commit_reveal is None
        or historical_owner_hotkey is None
        or historical_next_epoch is None
        or any(
            not hasattr(metagraph, field)
            for field in ("block", "uids", "hotkeys", "validator_permit")
        )
        or any(
            getattr(metagraph, field) is None
            for field in ("block", "uids", "hotkeys", "validator_permit")
        )
    ):
        raise ReproductionNotProven(
            "historical launch metagraph lookup returned incomplete material"
        )
    raw_actual_uids = getattr(metagraph, "uids", ())
    if hasattr(raw_actual_uids, "tolist"):
        raw_actual_uids = raw_actual_uids.tolist()
    actual_uids = [int(value) for value in raw_actual_uids]
    actual_hotkeys = [str(value) for value in getattr(metagraph, "hotkeys", ())]
    actual_validator_permit = [
        bool(value) for value in getattr(metagraph, "validator_permit", ())
    ]
    if (
        int(getattr(metagraph, "block", -1)) != mapping_block
        or str(actual_mapping_hash).lower() != snapshot["block_hash"].lower()
        or actual_uids != snapshot["uids"]
        or actual_hotkeys != snapshot["hotkeys"]
        or actual_validator_permit != snapshot["validator_permit"]
        or historical_commit_reveal is not False
        or str(historical_owner_hotkey) != mapping["burn_hotkey"]
        or historical_next_epoch != mapping["next_epoch_start_block"]
    ):
        raise ReproductionError(
            "historical metagraph differs from the signed snapshot, or its "
            "owner, epoch schedule, or commit-reveal state changed"
        )
    hotkey_to_uid = dict(zip(actual_hotkeys, actual_uids))
    validator_index = (
        actual_hotkeys.index(mapping["validator_hotkey"])
        if mapping.get("validator_hotkey") in actual_hotkeys
        else None
    )
    if (
        hotkey_to_uid.get(mapping.get("validator_hotkey")) != mapping["validator_uid"]
        or validator_index is None
        or actual_validator_permit[validator_index] is not True
        or hotkey_to_uid.get(vector["weights"][0]["miner_hotkey"])
        != mapping["rewarded_uid"]
        or hotkey_to_uid.get(vector["burn_snapshot"]["burn_hotkey"])
        != mapping["burn_uid"]
        or mapping.get("rewarded_hotkey") != vector["weights"][0]["miner_hotkey"]
        or mapping.get("burn_hotkey") != vector["burn_snapshot"]["burn_hotkey"]
    ):
        raise ReproductionError("launch hotkeys do not map to the signed UIDs")
    mapped = vector_to_uid_weights(
        vector,
        hotkey_to_uid,
        require_policy="validated_supply_v1",
    )
    expected_mapping = {
        int(mapping["rewarded_uid"]): 0.9,
        int(mapping["burn_uid"]): 0.1,
    }
    if mapped != expected_mapping:
        raise ReproductionError("launch vector does not independently map to 90/10")
    actual_uid_safety = _bounded_archive_call(
        deadline,
        "historical UID and hotkey safety lookup",
        lambda: _recompute_uid_safety(
            subtensor,
            metagraph=metagraph,
            mapping=mapping,
            block_hash=str(actual_mapping_hash).lower(),
        ),
    )
    if actual_uid_safety != mapping.get("uid_safety"):
        raise ReproductionError(
            "historical UID/hotkey safety differs from the signed release"
        )

    (
        actual_block_hash,
        block,
        inclusion_metagraph,
        chain_rows,
        inclusion_commit_reveal,
        inclusion_owner_hotkey,
        inclusion_next_epoch,
        inclusion_timestamp_ms,
    ) = _bounded_archive_call(
        deadline,
        "launch extrinsic archive lookup",
        lambda: (
            subtensor.get_block_hash(int(extrinsic["block"])),
            subtensor.substrate.get_block(block_hash=extrinsic["block_hash"]),
            subtensor.metagraph(39, block=int(extrinsic["block"])),
            subtensor.weights(39, block=int(extrinsic["block"])),
            subtensor.commit_reveal_enabled(
                netuid=39,
                block=int(extrinsic["block"]),
            ),
            subtensor.get_subnet_owner_hotkey(
                39,
                block=int(extrinsic["block"]),
            ),
            subtensor.get_next_epoch_start_block(
                39,
                block=int(extrinsic["block"]),
            ),
            _block_timestamp_ms(subtensor.substrate, extrinsic["block_hash"]),
        ),
    )
    if (
        actual_block_hash is None
        or not isinstance(block, dict)
        or inclusion_metagraph is None
        or chain_rows is None
        or inclusion_commit_reveal is None
        or inclusion_owner_hotkey is None
        or inclusion_next_epoch is None
        or not isinstance(block.get("header"), dict)
        or "number" not in block["header"]
        or "hash" not in block["header"]
        or block["header"]["number"] is None
        or block["header"]["hash"] is None
        or not isinstance(block.get("extrinsics"), (list, tuple))
        or any(
            not hasattr(inclusion_metagraph, field)
            for field in ("block", "uids", "hotkeys", "validator_permit")
        )
        or any(
            getattr(inclusion_metagraph, field) is None
            for field in ("block", "uids", "hotkeys", "validator_permit")
        )
    ):
        raise ReproductionNotProven(
            "launch extrinsic archive lookup returned incomplete material"
        )
    if (
        str(actual_block_hash).lower() != extrinsic["block_hash"].lower()
        or int(block.get("header", {}).get("number", -1)) != extrinsic["block"]
        or str(block.get("header", {}).get("hash", "")).lower()
        != extrinsic["block_hash"].lower()
    ):
        raise ReproductionError("launch inclusion block differs")
    inclusion_hotkeys = [
        str(value) for value in getattr(inclusion_metagraph, "hotkeys", ())
    ]
    raw_inclusion_uids = getattr(inclusion_metagraph, "uids", ())
    if hasattr(raw_inclusion_uids, "tolist"):
        raw_inclusion_uids = raw_inclusion_uids.tolist()
    inclusion_uids = [int(value) for value in raw_inclusion_uids]
    inclusion_map = dict(zip(inclusion_uids, inclusion_hotkeys))
    inclusion_permits = [
        bool(value) for value in getattr(inclusion_metagraph, "validator_permit", ())
    ]
    inclusion_validator_index = (
        inclusion_hotkeys.index(mapping["validator_hotkey"])
        if mapping["validator_hotkey"] in inclusion_hotkeys
        else None
    )
    if (
        int(getattr(inclusion_metagraph, "block", -1)) != extrinsic["block"]
        or len(inclusion_uids) != len(inclusion_hotkeys)
        or len(inclusion_permits) != len(inclusion_hotkeys)
        or len(inclusion_map) != len(inclusion_uids)
        or inclusion_validator_index is None
        or inclusion_permits[inclusion_validator_index] is not True
        or inclusion_map.get(mapping["validator_uid"]) != mapping["validator_hotkey"]
        or inclusion_map.get(mapping["rewarded_uid"])
        != vector["weights"][0]["miner_hotkey"]
        or inclusion_map.get(mapping["burn_uid"])
        != vector["burn_snapshot"]["burn_hotkey"]
        or str(inclusion_owner_hotkey) != mapping["burn_hotkey"]
        or inclusion_next_epoch != mapping["next_epoch_start_block"]
    ):
        raise ReproductionError("launch inclusion UID mapping differs")
    try:
        inclusion_time = datetime.fromtimestamp(inclusion_timestamp_ms / 1000, UTC)
        vector_generated = wire_vector._parse_canonical_utc(
            vector.get("generated_at"),
            field="generated_at",
        )
        vector_expiry = wire_vector._parse_canonical_utc(
            vector.get("expires_at"),
            field="expires_at",
        )
    except Exception as exc:
        raise ReproductionError("launch vector time binding is malformed") from exc
    if (
        inclusion_commit_reveal is not False
        or not vector_generated <= inclusion_time < vector_expiry
    ):
        raise ReproductionError("launch policy was not valid at the inclusion block")
    matching = [
        (index, item.value)
        for index, item in enumerate(block.get("extrinsics", ()))
        if isinstance(getattr(item, "value", None), dict)
        and item.value.get("extrinsic_hash") == extrinsic["hash"]
    ]
    if len(matching) != 1:
        raise ReproductionError("exact launch extrinsic is absent or duplicated")
    extrinsic_index, observed = matching[0]
    call = observed.get("call") or {}
    if (
        observed.get("address") != mapping["validator_hotkey"]
        or call.get("call_module") != "SubtensorModule"
        or call.get("call_function") != "set_mechanism_weights"
        or _call_arg(call, "netuid") != 39
        or _call_arg(call, "mecid") != 0
        or _call_arg(call, "version_key") != extrinsic["version_key"]
        or _call_arg(call, "dests") != extrinsic["uids"]
        or _call_arg(call, "weights") != extrinsic["weights_u16"]
    ):
        raise ReproductionError("launch extrinsic call differs from the signed record")
    intent = launch["broadcast_intent"]
    if "nonce" not in observed or "era" not in observed:
        raise ReproductionNotProven(
            "decoded launch extrinsic nonce or mortal era is unavailable"
        )
    observed_nonce = observed.get("nonce")
    raw_era = getattr(observed.get("era"), "value", observed.get("era"))
    if not isinstance(raw_era, dict) or not {"period", "phase"} <= set(raw_era):
        raise ReproductionNotProven(
            "decoded launch extrinsic mortal-era fields are unavailable"
        )
    try:
        observed_period = int(raw_era["period"])
        observed_phase = int(raw_era["phase"])
    except (TypeError, ValueError) as exc:
        raise ReproductionError(
            "decoded launch extrinsic mortal era is malformed"
        ) from exc
    if observed_period <= 0:
        raise ReproductionError("decoded launch extrinsic mortal era is malformed")
    era_reference = int(intent["era_reference_block"])
    expected_phase = era_reference % MORTAL_PERIOD_BLOCKS
    derived_birth = int(extrinsic["block"]) - (
        (int(extrinsic["block"]) - observed_phase) % observed_period
    )
    if (
        isinstance(observed_nonce, bool)
        or not isinstance(observed_nonce, int)
        or observed_nonce != intent["nonce"]
        or observed_period != MORTAL_PERIOD_BLOCKS
        or observed_phase != expected_phase
        or derived_birth != era_reference
    ):
        raise ReproductionError(
            "decoded launch extrinsic nonce or mortal era contradicts the signed intent"
        )
    execution = _bounded_archive_call(
        deadline,
        "launch extrinsic execution lookup",
        lambda: _materialize_execution_receipt(
            subtensor.substrate.retrieve_extrinsic_by_hash(
                extrinsic["block_hash"],
                extrinsic["hash"],
            )
        ),
    )
    if not (
        execution["extrinsic_idx"] == extrinsic_index
        and execution["is_success"] is True
        and execution["error_message"] is None
    ):
        raise ReproductionError("launch extrinsic did not execute successfully")
    rows = [row for row in chain_rows if int(row[0]) == mapping["validator_uid"]]
    actual_weights = (
        [[int(uid), int(weight)] for uid, weight in rows[0][1]]
        if len(rows) == 1
        else []
    )
    if actual_weights != [
        [uid, weight]
        for uid, weight in zip(extrinsic["uids"], extrinsic["weights_u16"])
    ]:
        raise ReproductionError("historical on-chain weights differ from the launch")
    finalized_hash, finalized_number, canonical_finalized_hash = _bounded_archive_call(
        deadline,
        "Finney finalized-head proof",
        lambda: _materialize_finalized_head(subtensor.substrate),
    )
    if (
        not _is_hash(finalized_hash.lower(), prefix="0x")
        or canonical_finalized_hash.lower() != finalized_hash.lower()
        or finalized_number < extrinsic["block"]
        or finalized_number < mapping_block
    ):
        raise ReproductionError(
            "launch mapping or extrinsic is not below the canonical finalized head"
        )
    return {
        "historical_launch": "PASS",
        "launch_extrinsic": extrinsic["hash"],
        "launch_block": extrinsic["block"],
        "finalized_head_block": finalized_number,
    }


def _load_pinned_key_document(path: Path, pin_name: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ReproductionNotProven(
            f"cannot read public key bundle {path.name}"
        ) from exc
    expected = EXPECTED_RELEASE_PINS.get(pin_name)
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if expected is None or actual != expected:
        raise ReproductionError(
            f"public key bundle {path.name} differs from its compiled byte pin"
        )
    return _strict_json_bytes(payload, label=f"public key bundle {path.name}")


def _load_public_keys(path: Path, pin_name: str | None = None) -> dict[str, bytes]:
    try:
        if pin_name is None:
            pin_name = {
                "registry-keys.json": "registry_keys",
                "report-keys.json": "report_keys",
                "index-keys.json": "index_keys",
                "release-attestation-keys.json": "release_attestation_keys",
            }[path.name]
        document = _load_pinned_key_document(path, pin_name)
        return {
            str(key_id): base64.b64decode(value, validate=True)
            for key_id, value in document.items()
        }
    except ReproductionError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ReproductionError(f"invalid public key bundle {path.name}") from exc


def verify_historical_candidates(
    manifest: dict[str, Any],
    *,
    subtensor: Any,
    deadline: float | None = None,
) -> None:
    """Require the evidence candidate set to equal its historical metagraph."""
    _require_finney_archive(subtensor, deadline=deadline)
    snapshot = manifest.get("candidate_set")
    candidates = (snapshot or {}).get("candidates")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("network") != "finney"
        or snapshot.get("netuid") != 39
        or not isinstance(snapshot.get("block"), int)
        or snapshot["block"] <= 0
        or not _is_hash(snapshot.get("block_hash"), prefix="0x")
        or not isinstance(candidates, list)
        or not candidates
    ):
        raise ReproductionError("evidence candidate snapshot is malformed")
    declared: list[str] = []
    for row in candidates:
        hotkey = row.get("hotkey") if isinstance(row, dict) else None
        if not isinstance(hotkey, str) or not hotkey:
            raise ReproductionError("evidence candidate snapshot is malformed")
        declared.append(hotkey)
    if len(set(declared)) != len(declared):
        raise ReproductionError("evidence candidate snapshot has duplicate hotkeys")
    block = int(snapshot["block"])
    actual_hash, metagraph = _bounded_archive_call(
        deadline,
        "evidence historical metagraph lookup",
        lambda: (
            subtensor.get_block_hash(block),
            subtensor.metagraph(39, block=block),
        ),
    )
    if (
        actual_hash is None
        or metagraph is None
        or not hasattr(metagraph, "block")
        or not hasattr(metagraph, "hotkeys")
        or getattr(metagraph, "block") is None
        or getattr(metagraph, "hotkeys") is None
    ):
        raise ReproductionNotProven(
            "evidence historical metagraph lookup returned incomplete material"
        )
    actual = [str(value) for value in getattr(metagraph, "hotkeys", ())]
    if (
        int(getattr(metagraph, "block", -1)) != block
        or str(actual_hash).lower() != snapshot["block_hash"].lower()
        or not actual
        or len(set(actual)) != len(actual)
        or set(actual) != set(declared)
    ):
        raise ReproductionError(
            "evidence candidate set differs from the historical metagraph"
        )


def _validate_frozen_manifest(
    manifest: dict[str, Any],
    checkpoint: dict[str, Any],
) -> None:
    freshness = checkpoint["freshness_boundary"]
    if (
        manifest.get("network") != "finney"
        or manifest.get("netuid") != 39
        or manifest.get("source_epoch") != checkpoint["source_epoch"]
        or manifest.get("source_revision") != EXPECTED_PRODUCER_REVISION
        or manifest.get("reward_mechanism") != checkpoint["reward_mechanism"]
        or manifest.get("policy_registry", {}).get("release")
        != checkpoint["policy_release"]
        or manifest.get("policy_registry", {}).get("digest")
        != checkpoint["policy_digest"]
        or manifest.get("policy_registry", {}).get("blob")
        != checkpoint["policy_digest"]
        or manifest.get("score_report", {}).get("report_id") != checkpoint["report_id"]
        or manifest.get("score_report", {}).get("signing_key_id")
        != checkpoint["report_signing_key_id"]
        or manifest.get("verifier", {}).get("digest") != checkpoint["verifier_digest"]
        or manifest.get("verifier", {}).get("binary_blob")
        != checkpoint["verifier_binary_digest"]
        or manifest.get("generated_at") != freshness["manifest_generated_at"]
        or manifest.get("candidate_set", {}).get("block")
        != freshness["candidate_block"]
        or str(manifest.get("candidate_set", {}).get("block_hash", "")).lower()
        != freshness["candidate_block_hash"]
    ):
        raise ReproductionError(
            "frozen evidence manifest differs from the signed checkpoint"
        )


def _validate_frozen_report_freshness(
    report: dict[str, Any],
    checkpoint: dict[str, Any],
) -> None:
    freshness = checkpoint["freshness_boundary"]
    if (
        report.get("generated_at") != freshness["report_generated_at"]
        or report.get("valid_from_block") != freshness["report_valid_from_block"]
        or report.get("source_epoch") != checkpoint["source_epoch"]
        or report.get("report_id") != checkpoint["report_id"]
    ):
        raise ReproductionError(
            "frozen score report differs from the post-rotation boundary"
        )


def _validate_frozen_result(result: Any, checkpoint: dict[str, Any]) -> None:
    if (
        int(result.source_epoch) != checkpoint["source_epoch"]
        or result.report_id != checkpoint["report_id"]
        or result.signing_key_id != checkpoint["report_signing_key_id"]
        or result.policy_release != checkpoint["policy_release"]
        or result.policy_digest != checkpoint["policy_digest"]
        or result.verifier_digest != checkpoint["verifier_digest"]
        or result.mechanism_id != checkpoint["reward_mechanism"]["id"]
        or result.mechanism_revision != checkpoint["reward_mechanism"]["revision"]
        or result.assurance_level != "receipts_only"
    ):
        raise ReproductionError(
            "verified evidence result differs from the signed checkpoint"
        )


def _validate_controlled_replay_result(
    document: dict[str, Any],
    checkpoint: dict[str, Any],
    launch: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    expected = _controlled_replay_result_document(checkpoint, launch, manifest)
    if document != expected:
        raise ReproductionError(
            "content-addressed controlled TDX replay result differs from "
            "the signed checkpoint and exact replay input digests"
        )


def _controlled_replay_result_document(
    checkpoint: dict[str, Any],
    launch: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Describe only the exact public and controlled bytes replayed by root.

    The controlled envelope itself remains private. Its content digest and
    every public receipt/work/result input are bound into the signed release,
    so a mutable journal assertion cannot be promoted into a replay PASS.
    """
    positive_hotkeys = sorted(
        str(row["miner_hotkey"])
        for row in launch["signed_vector"]["weights"]
        if float(row["weight"]) > 0.0
    )
    if not positive_hotkeys or len(set(positive_hotkeys)) != len(positive_hotkeys):
        raise ReproductionError("controlled TDX replay has no unique positive set")
    bindings: dict[str, dict[str, Any]] = {}
    for row in manifest.get("attestations") or ():
        if not isinstance(row, dict):
            raise ReproductionError("controlled TDX replay binding is malformed")
        hotkey = row.get("hotkey")
        if not isinstance(hotkey, str) or not hotkey or hotkey in bindings:
            raise ReproductionError("controlled TDX replay binding is ambiguous")
        bindings[hotkey] = row
    receipts: dict[str, dict[str, Any]] = {}
    for row in manifest.get("receipts") or ():
        if not isinstance(row, dict):
            raise ReproductionError("controlled TDX replay receipt is malformed")
        hotkey = row.get("hotkey")
        if not isinstance(hotkey, str) or not hotkey or hotkey in receipts:
            raise ReproductionError("controlled TDX replay receipt is ambiguous")
        receipts[hotkey] = row
    replay_inputs: list[dict[str, Any]] = []
    for hotkey in positive_hotkeys:
        binding = bindings.get(hotkey)
        receipt = receipts.get(hotkey)
        if binding is None or receipt is None:
            raise ReproductionError(
                "controlled TDX replay lacks an exact positive input binding"
            )
        replay_input = {
            "hotkey": hotkey,
            "receipt_id": receipt.get("receipt_id"),
            "receipt_blob": receipt.get("blob"),
            "work_item_blob": receipt.get("work_item_blob"),
            "result_blob": receipt.get("result_blob"),
            "envelope_digest": binding.get("envelope_digest"),
            "evidence_digest": binding.get("evidence_digest"),
            "challenge_digest": binding.get("challenge_digest"),
        }
        if (
            not isinstance(replay_input["receipt_id"], str)
            or not replay_input["receipt_id"]
            or any(
                not _is_hash(replay_input[field], prefix="sha256:")
                for field in (
                    "receipt_blob",
                    "work_item_blob",
                    "result_blob",
                    "envelope_digest",
                    "evidence_digest",
                    "challenge_digest",
                )
            )
        ):
            raise ReproductionError("controlled TDX replay input digest is malformed")
        replay_inputs.append(replay_input)
    return {
        "schema": "cathedral_sn39_tdx_replay_result_v2",
        "status": "PASS",
        "assurance": "root_finalizer_positive_raw_replay",
        "source_epoch": checkpoint["source_epoch"],
        "manifest": checkpoint["manifest"],
        "report_id": checkpoint["report_id"],
        "policy_release": checkpoint["policy_release"],
        "policy_digest": checkpoint["policy_digest"],
        "reward_mechanism": checkpoint["reward_mechanism"],
        "verifier_digest": checkpoint["verifier_digest"],
        "verifier_binary_digest": checkpoint["verifier_binary_digest"],
        "replayed_hotkeys": positive_hotkeys,
        "replay_inputs": replay_inputs,
    }


def verify_frozen_evidence(
    release: dict[str, Any],
    *,
    subtensor: Any | None = None,
    load_public_blob: Callable[[str], bytes] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Recompute the exact signed public checkpoint, never mutable `latest`."""
    from cathedral import provenance
    from cathedral.evidence import parse_manifest, verify_index
    from cathedral.score_class import parse_score_report_json

    root = Path(__file__).resolve().parents[1]
    launch = _validate_launch_submission(release.get("launch_submission"))
    checkpoint = launch["evidence_checkpoint"]
    index = checkpoint["signed_index"]
    try:
        issued_at = datetime.fromisoformat(str(index["generated_at"]))
        verified_index = verify_index(
            _canonical_document(index),
            _load_public_keys(root / "config/provenance/index-keys.json"),
            expected_network="finney",
            expected_netuid=39,
            max_age_seconds=None,
            now=issued_at,
        )
    except Exception as exc:
        raise ReproductionError("frozen evidence index is invalid") from exc
    if (
        verified_index["latest"]["manifest"] != checkpoint["manifest"]
        or int(verified_index["latest"]["source_epoch"]) != checkpoint["source_epoch"]
    ):
        raise ReproductionError("frozen evidence index differs from the checkpoint")

    cache: dict[str, bytes] = {}

    def load_blob(digest: str) -> bytes:
        if digest not in cache:
            if not _is_hash(digest, prefix="sha256:"):
                raise ReproductionError("evidence blob digest is malformed")
            if load_public_blob is None:
                raise ReproductionNotProven(
                    "hardened public evidence transport is missing"
                )
            try:
                data = load_public_blob(digest)
            except ReproductionError:
                raise
            except Exception as exc:
                raise ReproductionNotProven(
                    f"public evidence blob {digest} is unavailable"
                ) from exc
            if not isinstance(data, bytes):
                raise ReproductionNotProven(
                    f"public evidence blob {digest} returned incomplete material"
                )
            if len(data) > MAX_BLOB_BYTES:
                raise ReproductionError("public evidence blob exceeds its size cap")
            if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise ReproductionError("evidence blob content differs from its digest")
            cache[digest] = data
        return cache[digest]

    try:
        manifest = parse_manifest(load_blob(checkpoint["manifest"]))
        _validate_frozen_manifest(manifest, checkpoint)
        report = parse_score_report_json(load_blob(manifest["score_report"]["blob"]))
        _validate_frozen_report_freshness(report, checkpoint)
        controlled_replay = _strict_json_bytes(
            load_blob(checkpoint["replay_result"]),
            label="controlled TDX replay result",
        )
        _validate_controlled_replay_result(
            controlled_replay,
            checkpoint,
            launch,
            manifest,
        )
        if subtensor is None:
            subtensor = _bounded_archive_call(
                deadline,
                "Finney archive connection",
                _finney_subtensor,
            )
        verify_historical_candidates(
            manifest,
            subtensor=subtensor,
            deadline=deadline,
        )
        registry_bytes = load_blob(manifest["policy_registry"]["blob"])
        report_bytes = load_blob(manifest["score_report"]["blob"])
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
        _strict_json_bytes(
            report_bytes,
            label="frozen score report",
        )
        inclusion_timestamp_ms = _bounded_archive_call(
            deadline,
            "launch inclusion timestamp lookup",
            lambda: _block_timestamp_ms(
                subtensor.substrate,
                launch["extrinsic"]["block_hash"],
            ),
        )
        inclusion_moment = datetime.fromtimestamp(inclusion_timestamp_ms / 1000, UTC)
        result = provenance.verify_and_recompute(
            report_bytes=report_bytes,
            receipts_by_id=receipts,
            registry_bytes=registry_bytes,
            trusted_registry_keys=_load_public_keys(
                root / "config/provenance/registry-keys.json"
            ),
            report_signing_keys=_load_public_keys(
                root / "config/provenance/report-keys.json"
            ),
            expected_network="finney",
            expected_netuid=39,
            expected_verifier_digest=(
                "sha256:"
                "8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
            ),
            mechanism_id="validated_supply_v1",
            now=inclusion_moment,
            candidate_set=manifest["candidate_set"],
            work_artifacts_by_receipt=work_artifacts,
            current_block=launch["extrinsic"]["block"],
        )
        agrees, discrepancies = provenance.compare_with_vector(
            result,
            launch["signed_vector"],
            wire_report_sha256=manifest.get("wire_report_sha256"),
        )
    except ReproductionError:
        raise
    except Exception as exc:
        raise ReproductionError("frozen public evidence recomputation failed") from exc
    _validate_frozen_result(result, checkpoint)
    if not agrees or discrepancies:
        raise ReproductionError("frozen evidence does not reproduce the launch vector")
    return {
        "evidence_checkpoint": "PASS",
        "evidence_source_epoch": checkpoint["source_epoch"],
        "evidence_candidate_set": "PASS",
        "public_assurance": "receipts_only",
        "root_finalizer_tdx_replay": "PASS",
        "independent_raw_tdx_replay": "NOT_PROVEN",
    }


def verify_public_release() -> dict[str, Any]:
    from scaffold.provenance_audit import (
        ProvenanceAuditError,
        ProvenanceSettings,
        _fetcher,
    )

    root = Path(__file__).resolve().parents[1]
    keys = _load_pinned_key_document(
        root / "config/provenance/release-attestation-keys.json",
        "release_attestation_keys",
    )
    deadline = time.monotonic() + PUBLIC_REPRODUCTION_DEADLINE_SECS
    settings = ProvenanceSettings(
        mode="shadow",
        evidence_url=PUBLIC_EVIDENCE_BASE,
        allow_private_hosts=False,
        audit_deadline_secs=PUBLIC_REPRODUCTION_DEADLINE_SECS,
    )
    try:
        _load_index, load_blob, fetch_named = _fetcher(
            settings,
            deadline=deadline,
            include_raw_fetch=True,
        )
        release_bytes = fetch_named("/release.json")
        signature_bytes = fetch_named("/release.json.sig")
        if not isinstance(release_bytes, bytes) or not isinstance(
            signature_bytes, bytes
        ):
            raise ReproductionNotProven(
                "public release fetch returned incomplete material"
            )
        if (
            len(release_bytes) > MAX_RELEASE_BYTES
            or len(signature_bytes) > MAX_RELEASE_BYTES
        ):
            raise ReproductionError("public release artifact exceeds its size cap")
        result = verify_release_bytes(
            release_bytes,
            signature_bytes,
            public_keys=keys,
            repo_revision=_repo_revision(root),
        )
    except ProvenanceAuditError as exc:
        raise ReproductionNotProven(
            "hardened public evidence fetch is unavailable"
        ) from exc
    release = result["release"]
    subtensor = _bounded_archive_call(
        deadline,
        "Finney archive connection",
        _finney_subtensor,
    )
    result.update(
        verify_historical_launch(
            release,
            subtensor=subtensor,
            deadline=deadline,
        )
    )
    result.update(
        verify_frozen_evidence(
            release,
            subtensor=subtensor,
            load_public_blob=load_blob,
            deadline=deadline,
        )
    )
    return result


EXPECTED_STARTUP = {
    "authority": "thin",
    "provenance_mode": "shadow",
    "network": "finney",
    "netuid": 39,
    "publisher_url": "https://api.cathedral.computer",
    "weight_policy_public_key": (
        "10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26"  # pragma: allowlist secret
    ),
    "weight_policy_key_id": "cathedral-weight-policy",
    "policy_pin": "validated_supply_v1",
    # STARTUP records only the credential-free origin. The exact evidence path
    # is bound by the signed release and fetched by the hardened reproducer.
    "provenance_evidence_url": "https://api.cathedral.computer",
    "provenance_registry_keys_digest": (
        "sha256:5fb8f00cd2541606927373f596c2ba77d4ce485df0539f4afd5091858af48512"
    ),
    "provenance_report_keys_digest": (
        "sha256:30e438fff5b0508402b233eb5eec590a834882801a552edbbf7e62e45cf98c70"
    ),
    "provenance_index_keys_digest": (
        "sha256:1e35b9ce36b3da3362a88feb93dfa90f1fe03ab7c42e902b13ac3789324f7611"
    ),
    "provenance_verifier_digest": (
        "sha256:8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
    ),
    "provenance_source_revision": (
        "655c264421a1f5f2e625a372a40f595aa1e114ab"  # pragma: allowlist secret
    ),
    "provenance_mechanism": "validated_supply_v1",
}


def _load_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReproductionError(f"cannot read JSONL event stream: {exc}") from exc
    events: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            document = _strict_json_bytes(
                line.encode("utf-8"),
                label=f"event line {number}",
                canonical=False,
            )
        except ReproductionError as exc:
            raise ReproductionError(f"invalid JSON on line {number}") from exc
        events.append(document)
    if not events:
        raise ReproductionError("event stream is empty")
    return events


def _latest(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next(
        (event for event in reversed(events) if event.get("event") == name),
        None,
    )


def assert_current_dry_run(
    path: Path,
) -> dict[str, Any]:
    """Validate one time-dependent, no-write dry run against the current feed."""
    events = _load_events(path)
    startup = _latest(events, "STARTUP")
    if startup is None or startup.get("status") != "INFO":
        raise ReproductionError("missing STARTUP event")
    startup_detail = str(startup.get("detail", ""))
    if (
        "submission_authority=thin" not in startup_detail
        or "provenance=shadow" not in startup_detail
    ):
        raise ReproductionError("validator did not run in thin/shadow mode")
    mismatched_startup = [
        name
        for name, expected in EXPECTED_STARTUP.items()
        if startup.get(name) != expected
    ]
    if mismatched_startup:
        raise ReproductionError(
            "resolved launch pins differ: " + ", ".join(mismatched_startup)
        )

    failures = {
        "TICK_FAILED",
        "PROVENANCE_AUDIT_FAIL",
        "PROVENANCE_VECTOR_MISMATCH",
        "PROVENANCE_AUDIT_UNRESOLVED",
    }
    startup_index = events.index(startup)
    observed_failures = [
        str(event.get("event"))
        for event in events[startup_index:]
        if event.get("event") in failures
        or event.get("status") in {"FAIL", "NOT_PROVEN"}
    ]
    if observed_failures:
        raise ReproductionError(
            "fail-closed event(s) observed: " + ", ".join(observed_failures)
        )

    submission = _latest(events[startup_index:], "WEIGHTS_DRY_RUN")
    if submission is None or submission.get("status") != "PASS":
        raise ReproductionError("missing successful no-write thin result")
    burn_share = submission.get("burn_share")
    uid_weights = submission.get("uid_weights")
    mapping_block = submission.get("mapping_block")
    burn_uid = submission.get("burn_uid")
    exact_uid_weights = (
        isinstance(uid_weights, dict)
        and isinstance(burn_uid, int)
        and not isinstance(burn_uid, bool)
        and str(burn_uid) in uid_weights
        and len(uid_weights) == 2
        and all(
            not isinstance(uid_weights[uid], bool)
            and isinstance(uid_weights[uid], (int, float))
            for uid in uid_weights
        )
        and math.isclose(
            float(uid_weights[str(burn_uid)]),
            0.1,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        and len(
            [
                value
                for uid, value in uid_weights.items()
                if uid != str(burn_uid)
                and math.isclose(
                    float(value),
                    0.9,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ]
        )
        == 1
    )
    expected_wire_uids = (
        sorted(int(uid) for uid in uid_weights) if exact_uid_weights else []
    )
    expected_wire_weights = (
        [
            WIRE_BURN_U16 if uid == burn_uid else WIRE_VALIDATED_SUPPLY_U16
            for uid in expected_wire_uids
        ]
        if exact_uid_weights
        else []
    )
    if (
        submission.get("authority") != "thin"
        or submission.get("uid_count") != 2
        or isinstance(burn_share, bool)
        or not isinstance(burn_share, (int, float))
        or not math.isclose(float(burn_share), 0.1, rel_tol=0.0, abs_tol=1e-12)
        or not exact_uid_weights
        or submission.get("wire_uids") != expected_wire_uids
        or submission.get("wire_weights") != expected_wire_weights
        or submission.get("version_key") != EXPECTED_VERSION_KEY
        or isinstance(mapping_block, bool)
        or not isinstance(mapping_block, int)
        or mapping_block <= 0
        or isinstance(submission.get("validator_uid"), bool)
        or not isinstance(submission.get("validator_uid"), int)
        or submission.get("validator_uid") in expected_wire_uids
        or not isinstance(submission.get("validator_hotkey"), str)
        or not submission.get("validator_hotkey")
    ):
        raise ReproductionError(
            "thin result is not the dynamically resolved rewarded/burn 90/10 boundary"
        )

    provenance = _latest(events[startup_index:], "PROVENANCE_AUDIT_PASS")
    if provenance is None or provenance.get("status") != "PASS":
        raise ReproductionError("missing successful FULL provenance result")
    if provenance.get("vector_agrees") is not True:
        raise ReproductionError(
            "FULL provenance recomputation did not agree with the signed vector"
        )

    return {
        "authority": "thin",
        "burn_share": "0.10",
        "chain_write": False,
        "provenance": "shadow",
        "current_dry_run": "PASS",
        "current_controlled_full": "PASS",
        # The controlled replay proves the evidence checkpoint exercised by
        # this run. It is not evidence that every miner/event in a whole epoch
        # was independently disclosed and replayed.
        "whole_epoch_full": "NOT_PROVEN",
    }


def assert_public_reproduction(
    *,
    release_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reproduce the immutable launch without consulting the mutable live feed."""
    release_result = (
        verify_public_release() if release_result is None else release_result
    )
    required = {
        "release_attestation": "signed release attestation",
        "historical_launch": "exact historical launch",
        "evidence_checkpoint": "frozen evidence checkpoint",
        "evidence_candidate_set": "historical evidence candidate set",
    }
    for field, label in required.items():
        if release_result.get(field) != "PASS":
            raise ReproductionError(f"{label} did not reproduce")
    return {
        "chain_write": False,
        "public_recomputation": "PASS",
        "root_finalizer_tdx_replay": "PASS",
        "independent_raw_tdx_replay": "NOT_PROVEN",
        "whole_epoch_full": "NOT_PROVEN",
        **{field: "PASS" for field in required},
        "reproducer_revision": release_result.get("reproducer_revision"),
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print(
            "usage: assert_sn39_public_reproduction.py",
            file=sys.stderr,
        )
        return 2
    try:
        summary = assert_public_reproduction()
    except ReproductionNotProven as exc:
        print(f"SN39 public reproduction: NOT_PROVEN: {exc}", file=sys.stderr)
        return 3
    except ReproductionError as exc:
        print(f"SN39 public reproduction: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "SN39 public reproduction: PASS "
        + json.dumps(summary, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
