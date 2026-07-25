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
    "fa39af97e738fdbed5c454f976b61246590b5794"  # pragma: allowlist secret
)
EXPECTED_REPORT_KEY_ID = "cathedral-score-sn39-20260724"
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
        "sha256:8a4d730778c37ef7cc47e2ffcba74e42dcdd19240283f688567dd06204181e5b"
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


class ReproductionError(ValueError):
    """The public reproduction did not prove the documented result."""


def _repo_revision(root: Path) -> str:
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        checkout_changes = subprocess.check_output(
            [
                "git",
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
        raise ReproductionError("cannot resolve the reproducer Git revision") from exc
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
        or not isinstance(snapshot, dict)
        or snapshot.get("network") != "finney"
        or snapshot.get("netuid") != 39
        or snapshot.get("block") != mapping.get("block")
        or not _is_hash(snapshot.get("block_hash"), prefix="0x")
        or not isinstance(snapshot.get("hotkeys"), list)
        or not snapshot["hotkeys"]
        or any(not isinstance(value, str) or not value for value in snapshot["hotkeys"])
        or mapping.get("commit_reveal_enabled") is not False
    ):
        raise ReproductionError("signed historical metagraph snapshot is malformed")
    if (
        mapping.get("validator_uid") != 30
        or mapping.get("burn_uid") != 204
        or mapping.get("uid_weights") != {"163": 0.9, "204": 0.1}
    ):
        raise ReproductionError("signed launch UID mapping differs from 90/10")

    extrinsic = raw.get("extrinsic")
    if (
        not isinstance(extrinsic, dict)
        or not _is_hash(extrinsic.get("hash"), prefix="0x")
        or not _is_hash(extrinsic.get("block_hash"), prefix="0x")
        or not isinstance(extrinsic.get("block"), int)
        or extrinsic.get("validator_uid") != 30
        or extrinsic.get("uids") != [163, 204]
        or extrinsic.get("weights_u16") != [WIRE_VALIDATED_SUPPLY_U16, WIRE_BURN_U16]
        or extrinsic.get("version_key") != EXPECTED_VERSION_KEY
    ):
        raise ReproductionError("signed launch extrinsic is malformed")

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
        release.get("schema") != "cathedral_sn39_provenance_release_v1"
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
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ReproductionError("launch inclusion block timestamp is malformed")
    return raw


def _finney_subtensor() -> Any:
    try:
        import bittensor as bt

        return bt.Subtensor(network="archive")
    except Exception as exc:
        raise ReproductionError("cannot connect to the Finney archive") from exc


def _bounded_archive_call(
    deadline: float | None,
    label: str,
    operation: Callable[[], Any],
) -> Any:
    """Run one read-only archive operation inside the command-wide deadline."""
    if deadline is None:
        return operation()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReproductionError(f"public reproduction deadline expired before {label}")
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
        raise ReproductionError(f"public reproduction deadline exceeded during {label}")
    succeeded, value = outcome.get_nowait()
    if not succeeded:
        raise ReproductionError(f"{label} failed") from value
    return value


def _materialize_execution_receipt(receipt: Any) -> dict[str, Any]:
    return {
        "extrinsic_idx": int(getattr(receipt, "extrinsic_idx", -1)),
        "is_success": getattr(receipt, "is_success", None),
        "error_message": getattr(receipt, "error_message", None),
    }


def _materialize_finalized_head(substrate: Any) -> tuple[str, int, str]:
    block_hash = str(substrate.get_chain_finalised_head())
    block_number = int(substrate.get_block_number(block_hash))
    return block_hash, block_number, str(substrate.get_block_hash(block_number))


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

    mapping = launch["mapping"]
    snapshot = mapping["metagraph_snapshot"]
    mapping_block = int(mapping["block"])
    extrinsic = launch["extrinsic"]
    if mapping_block >= int(extrinsic["block"]):
        raise ReproductionError("historical mapping must precede launch extrinsic")
    actual_mapping_hash, metagraph, historical_commit_reveal = _bounded_archive_call(
        deadline,
        "historical launch metagraph lookup",
        lambda: (
            subtensor.get_block_hash(mapping_block),
            subtensor.metagraph(39, block=mapping_block),
            subtensor.commit_reveal_enabled(netuid=39, block=mapping_block),
        ),
    )
    actual_hotkeys = [str(value) for value in getattr(metagraph, "hotkeys", ())]
    if (
        int(getattr(metagraph, "block", -1)) != mapping_block
        or str(actual_mapping_hash).lower() != snapshot["block_hash"].lower()
        or actual_hotkeys != snapshot["hotkeys"]
        or historical_commit_reveal is not False
    ):
        raise ReproductionError(
            "historical metagraph differs from the signed snapshot, or its "
            "commit-reveal state changed"
        )
    hotkey_to_uid = {hotkey: uid for uid, hotkey in enumerate(actual_hotkeys)}
    if (
        hotkey_to_uid.get(mapping.get("validator_hotkey")) != mapping["validator_uid"]
        or hotkey_to_uid.get(vector["weights"][0]["miner_hotkey"]) != 163
        or hotkey_to_uid.get(vector["burn_snapshot"]["burn_hotkey"]) != 204
    ):
        raise ReproductionError("launch hotkeys do not map to the signed UIDs")
    mapped = vector_to_uid_weights(
        vector,
        hotkey_to_uid,
        require_policy="validated_supply_v1",
    )
    if mapped != {163: 0.9, 204: 0.1}:
        raise ReproductionError("launch vector does not independently map to 90/10")

    (
        actual_block_hash,
        block,
        inclusion_metagraph,
        chain_rows,
        inclusion_commit_reveal,
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
            _block_timestamp_ms(subtensor.substrate, extrinsic["block_hash"]),
        ),
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
    if (
        int(getattr(inclusion_metagraph, "block", -1)) != extrinsic["block"]
        or len(inclusion_hotkeys) <= 204
        or inclusion_hotkeys[mapping["validator_uid"]] != mapping["validator_hotkey"]
        or inclusion_hotkeys[163] != vector["weights"][0]["miner_hotkey"]
        or inclusion_hotkeys[204] != vector["burn_snapshot"]["burn_hotkey"]
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
        [163, WIRE_VALIDATED_SUPPLY_U16],
        [204, WIRE_BURN_U16],
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
        raise ReproductionError(f"cannot read public key bundle {path.name}") from exc
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
    ):
        raise ReproductionError(
            "frozen evidence manifest differs from the signed checkpoint"
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
) -> None:
    positive_hotkeys = sorted(
        str(row["miner_hotkey"])
        for row in launch["signed_vector"]["weights"]
        if float(row["weight"]) > 0.0
    )
    expected = {
        "schema": "cathedral_sn39_tdx_replay_result_v1",
        "status": "PASS",
        "assurance": "operator_attested_positive_raw_replay",
        "source_epoch": checkpoint["source_epoch"],
        "manifest": checkpoint["manifest"],
        "report_id": checkpoint["report_id"],
        "policy_release": checkpoint["policy_release"],
        "policy_digest": checkpoint["policy_digest"],
        "reward_mechanism": checkpoint["reward_mechanism"],
        "verifier_digest": checkpoint["verifier_digest"],
        "verifier_binary_digest": checkpoint["verifier_binary_digest"],
        "replayed_hotkeys": positive_hotkeys,
    }
    if document != expected:
        raise ReproductionError(
            "content-addressed controlled TDX replay result differs from "
            "the signed checkpoint"
        )


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
                raise ReproductionError("hardened public evidence transport is missing")
            data = load_public_blob(digest)
            if len(data) > MAX_BLOB_BYTES:
                raise ReproductionError("public evidence blob exceeds its size cap")
            if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise ReproductionError("evidence blob content differs from its digest")
            cache[digest] = data
        return cache[digest]

    try:
        manifest = parse_manifest(load_blob(checkpoint["manifest"]))
        _validate_frozen_manifest(manifest, checkpoint)
        controlled_replay = _strict_json_bytes(
            load_blob(checkpoint["replay_result"]),
            label="controlled TDX replay result",
        )
        _validate_controlled_replay_result(controlled_replay, checkpoint, launch)
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
        "operator_attested_tdx_replay": "PASS",
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
        raise ReproductionError("hardened public evidence fetch failed closed") from exc
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
        "fa39af97e738fdbed5c454f976b61246590b5794"  # pragma: allowlist secret
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
    exact_uid_weights = (
        isinstance(uid_weights, dict)
        and set(uid_weights) == {"163", "204"}
        and all(
            not isinstance(uid_weights[uid], bool)
            and isinstance(uid_weights[uid], (int, float))
            for uid in ("163", "204")
        )
        and math.isclose(float(uid_weights["163"]), 0.9, rel_tol=0.0, abs_tol=1e-12)
        and math.isclose(float(uid_weights["204"]), 0.1, rel_tol=0.0, abs_tol=1e-12)
    )
    if (
        submission.get("authority") != "thin"
        or submission.get("uid_count") != 2
        or submission.get("burn_uid") != 204
        or isinstance(burn_share, bool)
        or not isinstance(burn_share, (int, float))
        or not math.isclose(float(burn_share), 0.1, rel_tol=0.0, abs_tol=1e-12)
        or not exact_uid_weights
        or submission.get("wire_uids") != [163, 204]
        or submission.get("wire_weights")
        != [
            WIRE_VALIDATED_SUPPLY_U16,
            WIRE_BURN_U16,
        ]
        or submission.get("version_key") != EXPECTED_VERSION_KEY
        or isinstance(mapping_block, bool)
        or not isinstance(mapping_block, int)
        or mapping_block <= 0
        or submission.get("validator_uid") != 30
        or not isinstance(submission.get("validator_hotkey"), str)
        or not submission.get("validator_hotkey")
    ):
        raise ReproductionError(
            "thin result is not the documented two-UID 163/204 90/10 boundary"
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
        "operator_attested_tdx_replay": "PASS",
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
