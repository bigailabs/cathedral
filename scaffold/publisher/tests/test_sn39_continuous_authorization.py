from __future__ import annotations

import base64
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scaffold import sn39_continuous_authorization as recurring, validator_thin
from scripts import build_sn39_release_manifest


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
HOTKEY = "5" + "A" * 47
LAUNCH_ATTEMPT = "sha256:" + "1" * 64
RELEASE_DIGEST = "sha256:" + "2" * 64
REVISION = "3" * 40
JOURNAL = str(
    Path(recurring.RUNTIME_ROOT)
    / (
        "journal-"
        + hashlib.sha256(
            recurring.canonical_json(
                {
                    "genesis_hash": recurring.FINNEY_GENESIS_HASH,
                    "netuid": 39,
                    "validator_hotkey": HOTKEY,
                }
            )
        ).hexdigest()
        + ".json"
    )
)


def _expected() -> dict[str, object]:
    return {
        "submission_journal": JOURNAL,
        "genesis_hash": recurring.FINNEY_GENESIS_HASH,
        "validator_hotkey": HOTKEY,
        "launch_attempt_id": LAUNCH_ATTEMPT,
        "release_sha256": RELEASE_DIGEST,
        "reproducer_revision": REVISION,
        "not_before_time": recurring.canonical_time(NOW - timedelta(minutes=1)),
    }


def _document() -> dict[str, object]:
    return {
        "schema": recurring.SCHEMA,
        "purpose": recurring.PURPOSE,
        "network": "finney",
        "genesis_hash": recurring.FINNEY_GENESIS_HASH,
        "netuid": 39,
        "validator_hotkey": HOTKEY,
        "runtime_root": recurring.RUNTIME_ROOT,
        "state_file": recurring.STATE_FILE,
        "submission_journal": JOURNAL,
        "mechanism": recurring.MECHANISM,
        "call_module": "SubtensorModule",
        "call_function": "set_mechanism_weights",
        "mecid": 0,
        "lanes": ["thin"],
        "launch_attempt_id": LAUNCH_ATTEMPT,
        "release_sha256": RELEASE_DIGEST,
        "reproducer_revision": REVISION,
        "issued_at": recurring.canonical_time(NOW - timedelta(seconds=30)),
        "valid_from_time": recurring.canonical_time(NOW - timedelta(seconds=30)),
        "valid_until_time": recurring.canonical_time(NOW + timedelta(hours=24)),
        "valid_from_block": 10_000,
        "valid_until_block": 17_200,
        "valid_from_nonce": 50,
        "valid_until_nonce_exclusive": 98,
        "max_attempts": 48,
    }


def _write_pair(
    root: Path,
    *,
    document: dict[str, object] | None = None,
) -> tuple[Path, Path, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    authorization = recurring.canonical_json(document or _document()) + b"\n"
    signature = {
        "schema": recurring.SIGNATURE_SCHEMA,
        "algorithm": "Ed25519",
        "key_id": recurring.RELEASE_KEY_ID,
        "payload": "sn39-recurring-write-authorization.json exact bytes",
        "payload_sha256": "sha256:" + hashlib.sha256(authorization).hexdigest(),
        "signature": base64.b64encode(private.sign(authorization)).decode("ascii"),
    }
    root.mkdir(mode=0o755)
    authorization_path = root / "authorization.json"
    signature_path = root / "authorization.json.sig"
    authorization_path.write_bytes(authorization)
    signature_path.write_bytes(recurring.canonical_json(signature) + b"\n")
    authorization_path.chmod(0o644)
    signature_path.chmod(0o644)
    return (
        authorization_path,
        signature_path,
        base64.b64encode(public).decode("ascii"),
    )


def _verify(
    authorization_path: Path,
    signature_path: Path,
    public_key: str,
    *,
    expected: dict[str, object] | None = None,
    lane: str = "thin",
    block: int = 10_100,
    now: datetime = NOW,
) -> recurring.VerifiedAuthorization:
    return recurring.verify_authorization(
        expected=expected or _expected(),
        lane=lane,
        finalized_block=block,
        now=now,
        authorization_path=authorization_path,
        signature_path=signature_path,
        expected_uid=os.getuid(),
        public_key_base64=public_key,
    )


def test_valid_recurring_authorization_is_separate_bounded_and_machine_bound(
    tmp_path: Path,
) -> None:
    authorization_path, signature_path, public_key = _write_pair(tmp_path / "etc")
    verified = _verify(authorization_path, signature_path, public_key)
    assert verified.authorization_sha256.startswith("sha256:")
    assert verified.launch_attempt_id == LAUNCH_ATTEMPT
    assert verified.release_sha256 == RELEASE_DIGEST
    assert verified.reproducer_revision == REVISION
    assert verified.validator_hotkey == HOTKEY
    assert verified.genesis_hash == recurring.FINNEY_GENESIS_HASH
    assert verified.lanes == ("thin",)
    assert verified.max_attempts == 48


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "submission_journal",
            recurring.RUNTIME_ROOT + "/journal-" + "b" * 64 + ".json",
            "submission_journal differs",
        ),
        ("validator_hotkey", "5" + "B" * 47, "validator_hotkey differs"),
        ("genesis_hash", "0x" + "4" * 64, "genesis_hash differs"),
        ("launch_attempt_id", "sha256:" + "5" * 64, "launch_attempt_id differs"),
        ("release_sha256", "sha256:" + "6" * 64, "release_sha256 differs"),
        ("reproducer_revision", "7" * 40, "reproducer_revision differs"),
        ("call_function", "set_weights", "call_function differs"),
        ("mecid", 1, "mecid differs"),
        ("max_attempts", 0, "attempt allowance"),
        ("max_attempts", recurring.MAX_ATTEMPTS + 1, "attempt allowance"),
        ("valid_from_nonce", -1, "valid_from_nonce"),
        ("valid_until_nonce_exclusive", 99, "nonce window"),
    ],
)
def test_wrong_identity_scope_or_attempt_bound_is_rejected(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    document = _document()
    document[field] = value
    paths = _write_pair(tmp_path / field, document=document)
    with pytest.raises(recurring.AuthorizationError, match=message):
        _verify(*paths)


def test_one_lane_never_silently_authorizes_the_other(tmp_path: Path) -> None:
    paths = _write_pair(tmp_path / "lane")
    with pytest.raises(recurring.AuthorizationError, match="lane is not approved"):
        _verify(*paths, lane="authority")


@pytest.mark.parametrize(
    "mutation",
    ["payload", "signature", "duplicate", "noncanonical"],
)
def test_tamper_and_ambiguous_json_fail_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    authorization_path, signature_path, public_key = _write_pair(tmp_path / mutation)
    if mutation == "payload":
        payload = authorization_path.read_bytes()
        authorization_path.write_bytes(
            payload.replace(b'"max_attempts":48', b'"max_attempts":47')
        )
    elif mutation == "signature":
        payload = signature_path.read_bytes()
        signature_path.write_bytes(payload.replace(b'"Ed25519"', b'"Ed25518"'))
    elif mutation == "duplicate":
        payload = authorization_path.read_bytes()
        authorization_path.write_bytes(
            payload.replace(b'{"call_function"', b'{"max_attempts":48,"call_function"')
        )
    else:
        authorization_path.write_bytes(recurring.canonical_json(_document()) + b" \n")
    with pytest.raises(recurring.AuthorizationError):
        _verify(authorization_path, signature_path, public_key)


def test_mutable_or_linked_artifact_fails_closed(tmp_path: Path) -> None:
    authorization_path, signature_path, public_key = _write_pair(tmp_path / "mode")
    authorization_path.chmod(0o666)
    with pytest.raises(recurring.AuthorizationError, match="root-controlled"):
        _verify(authorization_path, signature_path, public_key)

    authorization_path.unlink()
    target = tmp_path / "target"
    target.write_bytes(recurring.canonical_json(_document()) + b"\n")
    authorization_path.symlink_to(target)
    with pytest.raises(recurring.AuthorizationError):
        _verify(authorization_path, signature_path, public_key)


def test_release_signing_seed_must_be_private_not_merely_read_only(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    seed = directory / "seed"
    seed.write_text(base64.b64encode(b"x" * 32).decode("ascii"))
    seed.chmod(0o644)
    with pytest.raises(recurring.AuthorizationError, match="root-controlled"):
        recurring.read_root_controlled(
            seed,
            label="release signing seed",
            expected_uid=os.getuid(),
            require_private_mode=True,
            expected_mode=0o600,
        )
    seed.chmod(0o400)
    with pytest.raises(recurring.AuthorizationError, match="root-controlled"):
        recurring.read_root_controlled(
            seed,
            label="release signing seed",
            expected_uid=os.getuid(),
            require_private_mode=True,
            expected_mode=0o600,
        )
    seed.chmod(0o600)
    assert (
        recurring.read_root_controlled(
            seed,
            label="release signing seed",
            expected_uid=os.getuid(),
            require_private_mode=True,
            expected_mode=0o600,
        )
        == seed.read_bytes()
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"valid_until_time": recurring.canonical_time(NOW + timedelta(minutes=1))},
            "time window",
        ),
        (
            {
                "valid_until_time": recurring.canonical_time(
                    NOW + timedelta(seconds=recurring.MAX_VALIDITY_SECONDS + 1)
                )
            },
            "time window",
        ),
        ({"valid_until_block": 10_102}, "block window"),
        (
            {"valid_until_block": 10_000 + recurring.MAX_VALIDITY_BLOCKS + 1},
            "block window",
        ),
    ],
)
def test_expired_or_overlong_windows_are_rejected(
    tmp_path: Path,
    change: dict[str, object],
    message: str,
) -> None:
    document = _document()
    document.update(change)
    paths = _write_pair(
        tmp_path / hashlib.sha256(str(change).encode()).hexdigest(), document=document
    )
    with pytest.raises(recurring.AuthorizationError, match=message):
        _verify(*paths)


def test_authorization_issued_before_reconciliation_is_rejected(
    tmp_path: Path,
) -> None:
    paths = _write_pair(tmp_path / "early")
    expected = _expected()
    expected["not_before_time"] = recurring.canonical_time(NOW)
    with pytest.raises(recurring.AuthorizationError, match="time window"):
        _verify(*paths, expected=expected)


def test_lower_boundary_rechecks_expiry_and_lane() -> None:
    authorization = recurring.VerifiedAuthorization(
        authorization_sha256="sha256:" + "8" * 64,
        submission_journal=JOURNAL,
        launch_attempt_id=LAUNCH_ATTEMPT,
        release_sha256=RELEASE_DIGEST,
        reproducer_revision=REVISION,
        validator_hotkey=HOTKEY,
        genesis_hash=recurring.FINNEY_GENESIS_HASH,
        lanes=("thin",),
        issued_at=recurring.canonical_time(NOW - timedelta(minutes=1)),
        valid_from_time=recurring.canonical_time(NOW - timedelta(minutes=1)),
        valid_until_time=recurring.canonical_time(NOW + timedelta(hours=1)),
        valid_from_block=10_000,
        valid_until_block=10_500,
        valid_from_nonce=50,
        valid_until_nonce_exclusive=53,
        max_attempts=3,
    )
    recurring.assert_still_ready(
        authorization, lane="thin", finalized_block=10_100, now=NOW
    )
    with pytest.raises(recurring.AuthorizationError, match="expired"):
        recurring.assert_still_ready(
            authorization,
            lane="thin",
            finalized_block=10_100,
            now=NOW + timedelta(hours=1),
        )
    with pytest.raises(recurring.AuthorizationError, match="expired"):
        recurring.assert_still_ready(
            authorization, lane="authority", finalized_block=10_100, now=NOW
        )
    recurring.assert_nonce_ready(authorization, account_nonce=50)
    recurring.assert_nonce_ready(authorization, account_nonce=52)
    with pytest.raises(recurring.AuthorizationError, match="nonce"):
        recurring.assert_nonce_ready(authorization, account_nonce=53)


def test_operator_builder_requires_reconciled_launch_and_explicit_bounds() -> None:
    state = {
        "submission_continuous_enabled": True,
        "submission_launch_status": "finalized",
        "submission_launch_attempt_id": LAUNCH_ATTEMPT,
        "submission_continuous_launch_attempt_id": LAUNCH_ATTEMPT,
        "submission_continuous_release_sha256": RELEASE_DIGEST,
        "submission_continuous_reproducer_revision": REVISION,
        "submission_validator_hotkey": HOTKEY,
        "submission_genesis_hash": recurring.FINNEY_GENESIS_HASH,
    }
    document = recurring.build_from_journal(
        state,
        journal_path=Path(JOURNAL),
        expected_validator_hotkey=HOTKEY,
        reviewed_finalized_block=10_000,
        reviewed_validator_nonce=50,
        max_attempts=12,
        valid_for_blocks=7_200,
        valid_for_seconds=86_400,
        allow_authority_lane=False,
        now=NOW,
    )
    assert document["lanes"] == ["thin"]
    assert document["max_attempts"] == 12
    assert document["valid_until_block"] == 17_200
    assert document["valid_from_nonce"] == 50
    assert document["valid_until_nonce_exclusive"] == 62

    with pytest.raises(recurring.AuthorizationError, match="filename differs"):
        recurring.build_from_journal(
            state,
            journal_path=Path(recurring.RUNTIME_ROOT)
            / ("journal-" + "f" * 64 + ".json"),
            expected_validator_hotkey=HOTKEY,
            reviewed_finalized_block=10_000,
            reviewed_validator_nonce=50,
            max_attempts=12,
            valid_for_blocks=7_200,
            valid_for_seconds=86_400,
            allow_authority_lane=False,
            now=NOW,
        )

    state["submission_continuous_enabled"] = False
    with pytest.raises(recurring.AuthorizationError, match="reconciled"):
        recurring.build_from_journal(
            state,
            journal_path=Path(JOURNAL),
            expected_validator_hotkey=HOTKEY,
            reviewed_finalized_block=10_000,
            reviewed_validator_nonce=50,
            max_attempts=12,
            valid_for_blocks=7_200,
            valid_for_seconds=86_400,
            allow_authority_lane=False,
            now=NOW,
        )


def test_root_operator_reproduces_public_launch_before_reading_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "submission_launch_identity": {"network": "finney"},
        "submission_continuous_release_sha256": RELEASE_DIGEST,
        "submission_continuous_reproducer_revision": REVISION,
    }
    public_result = {"release_attestation": "PASS"}
    observed: list[object] = []
    monkeypatch.setattr(
        "scaffold.sn39_public_reproduction.verify_public_release",
        lambda: observed.append("reproduced") or public_result,
    )
    monkeypatch.setattr(
        validator_thin,
        "_match_signed_public_release_to_launch",
        lambda **kwargs: (
            observed.append(kwargs)
            or {
                "release_sha256": RELEASE_DIGEST,
                "reproducer_revision": REVISION,
            }
        ),
    )
    assert recurring.verify_journal_public_release(state)["release_sha256"] == (
        RELEASE_DIGEST
    )
    assert observed[0] == "reproduced"
    assert observed[1]["public_result"] is public_result

    state["submission_continuous_release_sha256"] = "sha256:" + "f" * 64
    with pytest.raises(recurring.AuthorizationError, match="differs"):
        recurring.verify_journal_public_release(state)


def test_authorizer_requires_exact_immutable_launcher_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = tmp_path / "install"
    install.mkdir(mode=0o755)
    manifest = install / "manifest.json"
    manifest.write_bytes(b'{"schema":"fixture"}\n')
    manifest.chmod(0o644)
    arguments = ["--journal", JOURNAL, "--max-attempts", "1"]
    release_sha = "a" * 40
    manifest_digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    context = recurring.authorizer_context_digest(
        release_sha=release_sha,
        manifest_digest=manifest_digest,
        arguments=arguments,
    )
    monkeypatch.setenv(recurring.RELEASE_SHA_ENV, release_sha)
    monkeypatch.setenv(recurring.AUTHORIZER_CONTEXT_ENV, context)
    recurring.require_launcher_context(
        arguments,
        manifest_path=manifest,
        expected_uid=os.getuid(),
    )
    assert recurring.RELEASE_SHA_ENV not in os.environ
    assert recurring.AUTHORIZER_CONTEXT_ENV not in os.environ

    monkeypatch.setenv(recurring.RELEASE_SHA_ENV, release_sha)
    monkeypatch.setenv(recurring.AUTHORIZER_CONTEXT_ENV, "sha256:" + "0" * 64)
    with pytest.raises(recurring.AuthorizationError, match="immutable launcher"):
        recurring.require_launcher_context(
            arguments,
            manifest_path=manifest,
            expected_uid=os.getuid(),
        )


def test_recurring_authorization_verifier_is_explicitly_release_bound() -> None:
    assert (
        "scaffold/sn39_continuous_authorization.py"
        in build_sn39_release_manifest.RELEASE_FILES
    )
    root = Path(__file__).resolve().parents[3]
    unit = (root / "deploy/sn39/cathedral-validator-sn39.service").read_text("utf-8")
    assert (
        "ConditionPathExists=/etc/cathedral-validator/"
        "sn39-recurring-write-authorization.json\n"
    ) in unit
    assert (
        "ConditionPathExists=/etc/cathedral-validator/"
        "sn39-recurring-write-authorization.json.sig\n"
    ) in unit
    read_only_line = next(
        line for line in unit.splitlines() if line.startswith("ReadOnlyPaths=")
    )
    assert "sn39-recurring-write-authorization" not in read_only_line
    assert "ProtectSystem=strict" in unit
    assert recurring.AUTHORIZATION_PATH == Path(
        "/etc/cathedral-validator/sn39-recurring-write-authorization.json"
    )
    assert recurring.SIGNATURE_PATH == Path(
        "/etc/cathedral-validator/sn39-recurring-write-authorization.json.sig"
    )


def _runtime_authorization(
    *,
    digest_nibble: str = "8",
    max_attempts: int = 2,
) -> validator_thin.ContinuousAuthorization:
    return validator_thin.ContinuousAuthorization(
        authorization_sha256="sha256:" + digest_nibble * 64,
        submission_journal=JOURNAL,
        launch_attempt_id=LAUNCH_ATTEMPT,
        release_sha256=RELEASE_DIGEST,
        reproducer_revision=REVISION,
        validator_hotkey=HOTKEY,
        genesis_hash=recurring.FINNEY_GENESIS_HASH,
        lanes=("thin",),
        issued_at=recurring.canonical_time(NOW - timedelta(minutes=1)),
        valid_from_time=recurring.canonical_time(NOW - timedelta(minutes=1)),
        valid_until_time=recurring.canonical_time(NOW + timedelta(days=1)),
        valid_from_block=10_000,
        valid_until_block=17_200,
        valid_from_nonce=50,
        valid_until_nonce_exclusive=50 + max_attempts,
        max_attempts=max_attempts,
    )


def _runtime_args(
    tmp_path: Path,
    authorization: validator_thin.ContinuousAuthorization | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        broadcast=True,
        offline=False,
        network="finney",
        netuid=39,
        runtime_root=str(tmp_path / "runtime"),
        max_submissions=0,
        require_full_provenance_for_broadcast=False,
        require_completed_launch_for_broadcast=True,
        require_policy="validated_supply_v1",
        _submission_validator_hotkey=HOTKEY,
        _submission_genesis_hash=recurring.FINNEY_GENESIS_HASH,
        _continuous_submission_authorization=authorization,
    )


def _reserve_and_consume(
    args: SimpleNamespace,
    authorization: validator_thin.ContinuousAuthorization,
    *,
    policy_version: int,
    attempt_nibble: str,
) -> str:
    attempt = "sha256:" + attempt_nibble * 64
    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id=attempt,
        identity={
            "network": "finney",
            "netuid": 39,
            "validator_hotkey": HOTKEY,
            "policy_version": policy_version,
            "uid_weights": [[1, 1.0]],
            "uid_hotkeys": [[1, "tdx-miner"]],
            "continuous_authorization": (
                validator_thin._continuous_authorization_identity(authorization)
            ),
        },
    )
    validator_thin._record_pending_broadcast_intent(
        args,
        attempt_id=attempt,
        extrinsic_hash="0x" + attempt_nibble * 64,
        nonce=policy_version,
        era_reference_block=10_000 + policy_version,
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        version_key=validator_thin._weight_version_key(),
        wire_uids=[1],
        wire_weights=[65_535],
    )
    validator_thin._finalize_common_submission(
        args,
        attempt_id=attempt,
        submission=validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + attempt_nibble * 64,
            block_hash="0x" + chr(ord(attempt_nibble) + 1) * 64,
            block_number=10_000 + policy_version,
            finalized=True,
        ),
    )
    return attempt


def test_one_shot_launch_state_alone_cannot_reserve_recurring_write(
    tmp_path: Path,
) -> None:
    args = _runtime_args(tmp_path, None)
    with pytest.raises(ValueError, match="separate signed authorization"):
        validator_thin._reserve_common_submission(
            args,
            lane="thin",
            attempt_id="sha256:" + "4" * 64,
            identity={"policy_version": 1},
        )
    assert not validator_thin._submission_state_path(args).exists()


def test_signed_attempt_budget_is_atomic_per_authorization_and_renewable(
    tmp_path: Path,
) -> None:
    authorization = _runtime_authorization(max_attempts=2)
    args = _runtime_args(tmp_path, authorization)
    first = _reserve_and_consume(
        args, authorization, policy_version=1, attempt_nibble="4"
    )
    second = _reserve_and_consume(
        args, authorization, policy_version=2, attempt_nibble="6"
    )
    state = validator_thin._read_state(validator_thin._submission_state_path(args))
    scope = authorization.authorization_sha256.removeprefix("sha256:")
    assert state["submission_attempt_budgets"][scope] == {
        "limit": 2,
        "ids": [first, second],
    }
    with pytest.raises(ValueError, match="budget 2 is exhausted"):
        validator_thin._reserve_common_submission(
            args,
            lane="thin",
            attempt_id="sha256:" + "8" * 64,
            identity={
                "policy_version": 3,
                "continuous_authorization": (
                    validator_thin._continuous_authorization_identity(authorization)
                ),
            },
        )

    renewal = _runtime_authorization(digest_nibble="9", max_attempts=1)
    args._continuous_submission_authorization = renewal
    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id="sha256:" + "a" * 64,
        identity={
            "policy_version": 3,
            "continuous_authorization": (
                validator_thin._continuous_authorization_identity(renewal)
            ),
        },
    )
    renewed = validator_thin._read_state(validator_thin._submission_state_path(args))
    assert renewed["submission_pending_budget_scope"] == "9" * 64
    assert renewed["submission_pending_budget_limit"] == 1


def test_reservation_rejects_tampered_scope_and_stricter_local_cap(
    tmp_path: Path,
) -> None:
    authorization = _runtime_authorization(max_attempts=2)
    args = _runtime_args(tmp_path, authorization)
    identity = validator_thin._continuous_authorization_identity(authorization)
    identity["max_attempts"] = 3
    with pytest.raises(ValueError, match="recurring authorization differs"):
        validator_thin._reserve_common_submission(
            args,
            lane="thin",
            attempt_id="sha256:" + "b" * 64,
            identity={
                "policy_version": 1,
                "continuous_authorization": identity,
            },
        )

    args.max_submissions = 1
    with pytest.raises(ValueError, match="configured attempt ceiling"):
        validator_thin._reserve_common_submission(
            args,
            lane="thin",
            attempt_id="sha256:" + "c" * 64,
            identity={
                "policy_version": 1,
                "continuous_authorization": (
                    validator_thin._continuous_authorization_identity(authorization)
                ),
            },
        )


def test_chain_nonce_ceiling_fails_before_wallet_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _runtime_authorization(max_attempts=2)
    args = _runtime_args(tmp_path, authorization)
    finalized_hash = "0x" + "f" * 64
    substrate = SimpleNamespace(
        get_account_next_index=lambda _address: (
            authorization.valid_until_nonce_exclusive
        )
    )
    preflight = validator_thin.ChainPreflight(
        wallet=SimpleNamespace(hotkey=SimpleNamespace(ss58_address=HOTKEY)),
        subtensor=SimpleNamespace(substrate=substrate),
        hotkey_to_uid={HOTKEY: 30},
        validator_hotkey=HOTKEY,
        validator_uid=30,
        block=10_100,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        finalized_hash=finalized_hash,
    )
    monkeypatch.setattr(
        validator_thin,
        "_finalized_chain_head",
        lambda _subtensor: (10_100, finalized_hash),
    )
    unlocks: list[object] = []
    monkeypatch.setattr(
        "bittensor.core.types.ExtrinsicResponse.unlock_wallet",
        lambda *call_args, **_kwargs: (
            unlocks.append(call_args) or SimpleNamespace(success=True)
        ),
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="outside the signed recurring-write allowance",
    ):
        validator_thin._submit_exact_sn39_extrinsic(
            preflight,
            runtime_contract=args,
            attempt_id="sha256:" + "d" * 64,
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[1],
            wire_weights=[65_535],
            mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        )
    assert unlocks == []
