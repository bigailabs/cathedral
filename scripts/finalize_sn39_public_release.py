#!/usr/bin/env python3
"""Build, archive-verify, sign, and publish the exact SN39 launch release."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# The root finalizer imports from the already-verified immutable checkout.
# Never create ignored bytecode there after the pristine-tree gate passes.
sys.dont_write_bytecode = True

PUBLIC_ROOT = Path("/var/lib/cathedral-public-evidence")
PRIVATE_SEED = Path("/etc/cathedral/release-attestation-signing-sn39-20260724.key")
RUNTIME_ROOT = Path("/var/lib/cathedral-validator")
RELEASE_KEY_ID = "cathedral-release-attestation-sn39-20260724"
JOURNAL_NAME = re.compile(r"journal-[0-9a-f]{64}\.json")
SHA = re.compile(r"[0-9a-f]{40}")
HASH = re.compile(r"0x[0-9a-f]{64}")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_PUBLIC_BLOB_BYTES = 4 * 1024 * 1024
MAX_RELEASE_BYTES = 128 * 1024


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


def _require_launch_state(
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = state.get("submission_launch_identity")
    if (
        state.get("submission_pending_id") is not None
        or state.get("submission_launch_status") != "finalized"
        or state.get("submission_launch_budget_limit") != 1
        or state.get("submission_continuous_enabled") is not False
        or not isinstance(identity, dict)
    ):
        raise ReleaseError("journal has no final, unreconciled one-shot launch")
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
    return identity, full


def _archive_snapshot(
    subtensor: Any,
    *,
    block: int,
) -> tuple[str, list[str]]:
    block_hash = str(subtensor.get_block_hash(block)).lower()
    metagraph = subtensor.metagraph(39, block=block)
    commit_reveal = subtensor.commit_reveal_enabled(netuid=39, block=block)
    hotkeys = [str(value) for value in getattr(metagraph, "hotkeys", ())]
    if (
        HASH.fullmatch(block_hash) is None
        or int(getattr(metagraph, "block", -1)) != block
        or not hotkeys
        or len(set(hotkeys)) != len(hotkeys)
        or commit_reveal is not False
    ):
        raise ReleaseError(
            "archive mapping snapshot is malformed or commit-reveal is on"
        )
    return block_hash, hotkeys


def build_release(
    state: dict[str, Any],
    *,
    release_sha: str,
    subtensor: Any,
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

    identity, full = _require_launch_state(state)
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
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError("journal launch identity is malformed") from exc
    if (
        identity.get("network") != "finney"
        or identity.get("netuid") != 39
        or validator_uid != 30
        or uid_weights != {163: 0.9, 204: 0.1}
        or set(uid_hotkeys) != {163, 204}
        or HASH.fullmatch(extrinsic_hash) is None
        or HASH.fullmatch(block_hash) is None
        or block_number <= mapping_block
        or version_key != EXPECTED_VERSION_KEY
    ):
        raise ReleaseError("journal launch boundary differs from the SN39 release")

    mapping_hash, hotkeys = _archive_snapshot(subtensor, block=mapping_block)
    validator_hotkey = str(identity.get("validator_hotkey") or "")
    burn_hotkey = str(identity.get("burn_hotkey") or "")
    if (
        len(hotkeys) <= 204
        or hotkeys[validator_uid] != validator_hotkey
        or hotkeys[163] != uid_hotkeys[163]
        or not uid_hotkeys[204]
        or hotkeys[204] != uid_hotkeys[204]
        or hotkeys[204] != burn_hotkey
    ):
        raise ReleaseError("archive mapping does not match the journaled hotkeys")

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

    replay_result = {
        "schema": "cathedral_sn39_tdx_replay_result_v1",
        "status": "PASS",
        "assurance": "operator_attested_positive_raw_replay",
        "source_epoch": full["source_epoch"],
        "manifest": full["manifest"],
        "report_id": full["report_id"],
        "policy_release": full["policy_release"],
        "policy_digest": full["policy_digest"],
        "reward_mechanism": {"id": full["mechanism"], "revision": 1},
        "verifier_digest": full["verifier_digest"],
        "verifier_binary_digest": full["verifier_binary_digest"],
        "replayed_hotkeys": sorted(full["raw_replayed_hotkeys"]),
    }
    replay_bytes = canonical_json(replay_result)
    replay_digest = "sha256:" + hashlib.sha256(replay_bytes).hexdigest()
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
        "replay_result": replay_digest,
        "public_assurance": "receipts_only",
        "signed_index": signed_index,
    }
    release = {
        "schema": "cathedral_sn39_provenance_release_v1",
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
            "mapping": {
                "block": mapping_block,
                "validator_uid": validator_uid,
                "validator_hotkey": validator_hotkey,
                "burn_uid": 204,
                "commit_reveal_enabled": False,
                "uid_weights": {"163": 0.9, "204": 0.1},
                "metagraph_snapshot": {
                    "network": "finney",
                    "netuid": 39,
                    "block": mapping_block,
                    "block_hash": mapping_hash,
                    "hotkeys": hotkeys,
                },
            },
            "extrinsic": {
                "hash": extrinsic_hash,
                "block": block_number,
                "block_hash": block_hash,
                "validator_uid": validator_uid,
                "uids": [163, 204],
                "weights_u16": [
                    WIRE_VALIDATED_SUPPLY_U16,
                    WIRE_BURN_U16,
                ],
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


def _read_public_file(path: Path, *, size_cap: int, label: str) -> bytes | None:
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
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size <= 0
            or info.st_size > size_cap
        ):
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


def put_blob(root: Path, payload: bytes) -> str:
    if not payload or len(payload) > MAX_PUBLIC_BLOB_BYTES:
        raise ReleaseError("public replay-result blob exceeds its size cap")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    directory = root / "blobs" / "sha256"
    _safe_public_directory(root)
    _safe_public_directory(root / "blobs")
    _safe_public_directory(directory)
    path = directory / digest.split(":", 1)[1]
    existing = _read_public_file(
        path,
        size_cap=MAX_PUBLIC_BLOB_BYTES,
        label="public replay-result blob",
    )
    if existing is not None:
        if existing != payload:
            raise ReleaseError("public replay-result blob collides with other bytes")
        return digest
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        existing = _read_public_file(
            path,
            size_cap=MAX_PUBLIC_BLOB_BYTES,
            label="public replay-result blob",
        )
        if existing != payload:
            raise ReleaseError("public replay-result blob collides with other bytes")
        return digest
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_public_directory(directory)
    return digest


def verify_frozen_release_evidence(
    release: dict[str, Any],
    *,
    replay_bytes: bytes,
    public_root: Path,
    subtensor: Any,
) -> None:
    from scaffold.sn39_public_reproduction import verify_frozen_evidence

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
        or result.get("operator_attested_tdx_replay") != "PASS"
    ):
        raise ReleaseError("frozen public evidence did not reproduce")


def atomic_write(path: Path, payload: bytes) -> None:
    """Durably publish immutable bytes, accepting only an idempotent rerun."""
    if not payload or len(payload) > MAX_RELEASE_BYTES:
        raise ReleaseError("public release artifact exceeds its size cap")
    _safe_public_directory(path.parent)
    existing = _read_public_file(
        path,
        size_cap=MAX_RELEASE_BYTES,
        label=f"public release artifact {path.name}",
    )
    if existing is not None:
        if existing != payload:
            raise ReleaseError(
                f"public release artifact {path.name} is already sealed differently"
            )
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing = _read_public_file(
                path,
                size_cap=MAX_RELEASE_BYTES,
                label=f"public release artifact {path.name}",
            )
            if existing != payload:
                raise ReleaseError(
                    f"public release artifact {path.name} is already sealed differently"
                )
        _fsync_public_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _archive_subtensor() -> Any:
    try:
        import bittensor as bt

        return bt.Subtensor(network="archive")
    except Exception as exc:
        raise ReleaseError("cannot connect to the Finney archive") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    if not args.release.is_absolute():
        raise ReleaseError("release checkout path must be absolute")
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
