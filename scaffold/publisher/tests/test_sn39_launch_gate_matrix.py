"""SN39 launch-gate config matrix.

The SN39 mainnet launch is a subnet-level event that happens once. Requiring
every SN39 broadcaster to complete its own one-shot launch made third-party
validators unrunnable, because a relay can never satisfy a per-validator launch
gate. The gate is now scoped to runtimes that actually owe SN39 a launch:
the authority lane (which originates weights instead of relaying them), the
launch runtime itself, and any host holding or descended from the controlled
launch material.

This file pins the WHOLE truth table rather than the interesting rows, because
an unscoped gate is exactly the class of bug where the combination nobody wrote
a test for is the one that bites. Every test here fails if its guard is removed.
"""

from __future__ import annotations

import base64
import copy
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_validator_thin_validated_supply import payload as validated_supply_payload

from scaffold import validator_thin, wire_vector

VALIDATOR_HOTKEY = "5CanonicalValidator"
LAUNCH_ATTEMPT_ID = "sha256:" + "1" * 64
TDX_MINER_HOTKEY = "tdx-miner"


@pytest.fixture(autouse=True)
def _isolated_sn39_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every absolute path the gate consults.

    The launch-material probe deliberately reads code constants, not args, so a
    developer machine that happens to carry a real /etc/cathedral install would
    otherwise change the answer. Point the constants at paths that do not exist
    and let individual tests create them.
    """
    monkeypatch.setattr(
        validator_thin, "_VALIDATOR_RUNTIME_ROOT", tmp_path / "submission-runtime"
    )
    absent = tmp_path / "launch-material"
    monkeypatch.setattr(
        validator_thin, "SN39_LAUNCH_CONTROLLED_DIR", absent / "sn39-launch"
    )
    monkeypatch.setattr(
        validator_thin, "SN39_LAUNCH_VERIFIER_BINARY", absent / "cathedral-tdx-verifier"
    )
    monkeypatch.setattr(
        validator_thin,
        "SN39_LAUNCH_APPROVAL_FILE",
        absent / "sn39-launch-approval.json",
    )


def _pinned_sn39_args() -> SimpleNamespace:
    """A namespace matching the immutable SN39 trust profile exactly."""
    return SimpleNamespace(
        network="finney",
        netuid=39,
        broadcast=True,
        offline=False,
        once=False,
        max_submissions=0,
        wallet_name="validator",
        wallet_hotkey="default",
        publisher_url=validator_thin.SN39_PUBLISHER_URL,
        public_key_hex=validator_thin.DEFAULT_PUBLIC_KEY_HEX,
        key_id=validator_thin.SN39_WEIGHT_POLICY_KEY_ID,
        require_policy="validated_supply_v1",
        state_file=str(validator_thin.SN39_STATE_FILE),
        runtime_root=str(validator_thin._VALIDATOR_RUNTIME_ROOT),
        provenance="shadow",
        evidence_url=validator_thin.SN39_EVIDENCE_URL,
        provenance_registry_keys="config/provenance/registry-keys.json",
        provenance_registry_keys_digest=validator_thin.SN39_REGISTRY_KEYS_DIGEST,
        provenance_report_keys="config/provenance/report-keys.json",
        provenance_report_keys_digest=validator_thin.SN39_REPORT_KEYS_DIGEST,
        provenance_index_keys="config/provenance/index-keys.json",
        provenance_index_keys_digest=validator_thin.SN39_INDEX_KEYS_DIGEST,
        provenance_verifier_digest=validator_thin.SN39_VERIFIER_DIGEST,
        provenance_source_revision=validator_thin.SN39_PRODUCER_REVISION,
        provenance_mechanism=validator_thin.MECHANISM_DEFAULT,
        provenance_burn_hotkey=validator_thin.SN39_BURN_HOTKEY,
        provenance_controlled_dir=None,
        provenance_verifier_binary=None,
        launch_approval_file=str(validator_thin.SN39_LAUNCH_APPROVAL_FILE),
        launch_release_sha="a" * 40,
        launch_config_sha256="sha256:" + "b" * 64,
        launch_preflight=False,
        require_full_provenance_for_broadcast=False,
        require_completed_launch_for_broadcast=True,
        jsonl=None,
        _submission_validator_hotkey=VALIDATOR_HOTKEY,
        _submission_genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        _continuous_submission_authorization=None,
    )


def _relay_args() -> SimpleNamespace:
    """Third-party thin validator: relays Cathedral's signed vector only."""
    args = _pinned_sn39_args()
    args.require_completed_launch_for_broadcast = False
    return args


def _operator_continuous_args() -> SimpleNamespace:
    """Cathedral's own post-launch continuous lane."""
    args = _pinned_sn39_args()
    args.provenance_controlled_dir = str(validator_thin.SN39_LAUNCH_CONTROLLED_DIR)
    args.provenance_verifier_binary = str(validator_thin.SN39_LAUNCH_VERIFIER_BINARY)
    return args


def _operator_launch_args() -> SimpleNamespace:
    """Cathedral's one-shot launch canary."""
    args = _pinned_sn39_args()
    args.once = True
    args.max_submissions = 1
    args.require_full_provenance_for_broadcast = True
    args.require_completed_launch_for_broadcast = False
    args.provenance_controlled_dir = str(validator_thin.SN39_LAUNCH_CONTROLLED_DIR)
    args.provenance_verifier_binary = str(validator_thin.SN39_LAUNCH_VERIFIER_BINARY)
    return args


def _install_launch_material() -> None:
    """Create the controlled launch package at its release-pinned paths."""
    validator_thin.SN39_LAUNCH_CONTROLLED_DIR.mkdir(parents=True, exist_ok=True)
    validator_thin.SN39_LAUNCH_VERIFIER_BINARY.parent.mkdir(parents=True, exist_ok=True)
    validator_thin.SN39_LAUNCH_VERIFIER_BINARY.write_bytes(b"verifier")


def _journal_launch_lineage(
    args: SimpleNamespace, *, status: str = "finalized"
) -> None:
    """Record a completed one-shot launch in this runtime's own journal."""
    validator_thin._write_state_fenced(
        validator_thin._submission_state_path(args),
        {
            "submission_genesis_hash": validator_thin.FINNEY_GENESIS_HASH,
            "provenance_netuid": 39,
            "submission_validator_hotkey": VALIDATOR_HOTKEY,
            "submission_launch_status": status,
            "submission_launch_attempt_id": LAUNCH_ATTEMPT_ID,
            "submission_launch_attempt_ids": [LAUNCH_ATTEMPT_ID],
            "submission_continuous_enabled": True,
            "submission_continuous_launch_attempt_id": LAUNCH_ATTEMPT_ID,
        },
    )


# ---------------------------------------------------------------------------
# The predicate itself: one row per supported configuration
# ---------------------------------------------------------------------------


def test_operator_launch_lane_is_gated_and_fully_pinned() -> None:
    args = _operator_launch_args()
    _install_launch_material()
    # The launch runtime performs the one-shot transaction, so the launch gate
    # applies to it by construction and every launch pin still binds.
    validator_thin._validate_runtime_contract(args)
    assert validator_thin._sn39_launch_obligation(args) is True
    for field, value in (
        ("once", False),
        ("max_submissions", 2),
        ("provenance_controlled_dir", None),
        ("provenance", "authority"),
    ):
        broken = SimpleNamespace(**vars(args))
        setattr(broken, field, value)
        with pytest.raises(validator_thin.wire.VectorError, match="launch"):
            validator_thin._validate_runtime_contract(broken)


def test_operator_continuous_lane_still_requires_signed_authorization() -> None:
    args = _operator_continuous_args()
    _install_launch_material()
    _journal_launch_lineage(args)
    validator_thin._validate_runtime_contract(args)
    assert validator_thin._continuous_transition_required(args) is True
    # The gate is still the same gate: a finalized launch in the journal does
    # not self-authorize recurring writes.
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="continuous launch identity is missing",
    ):
        validator_thin._require_continuous_launch_transition(args)
    with pytest.raises(
        ValueError, match="recurring reservation lacks a separate signed authorization"
    ):
        validator_thin._reserve_common_submission(
            args,
            lane="thin",
            attempt_id="sha256:" + "2" * 64,
            identity={"policy_version": 7},
        )


def test_third_party_relay_needs_no_launch_and_no_authorization() -> None:
    args = _relay_args()
    validator_thin._validate_runtime_contract(args)
    assert validator_thin._sn39_launch_obligation(args) is False
    assert validator_thin._continuous_transition_required(args) is False
    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id="sha256:" + "3" * 64,
        identity={"policy_version": 7},
    )
    journal = validator_thin._read_state(validator_thin._submission_state_path(args))
    assert journal["submission_pending_lane"] == "thin"
    assert journal["submission_pending_launch_attempt"] is False


def test_relay_stays_ungated_after_its_own_first_reservation() -> None:
    """The per-reservation launch marker is a bool, not a launch record.

    Reading it as lineage would gate a relay from its second tick onward and
    silently reintroduce the very lockout this change removes.
    """
    args = _relay_args()
    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id="sha256:" + "4" * 64,
        identity={"policy_version": 7},
    )
    assert validator_thin._sn39_launch_lineage(args) is False
    assert validator_thin._continuous_transition_required(args) is False


def test_authority_lane_is_gated_no_matter_what_the_config_says() -> None:
    args = _relay_args()
    args.provenance = "authority"
    assert validator_thin._sn39_launch_obligation(args) is True
    assert validator_thin._continuous_transition_required(args) is True
    # Opting out is refused outright for a runtime that originates weights.
    with pytest.raises(validator_thin.wire.VectorError, match="completed-launch gate"):
        validator_thin._validate_runtime_contract(args)


def test_launch_material_on_the_host_gates_regardless_of_config() -> None:
    args = _relay_args()
    assert validator_thin._continuous_transition_required(args) is False
    _install_launch_material()
    # Possession is read from code constants, so no config value clears it.
    assert validator_thin._sn39_launch_obligation(args) is True
    assert validator_thin._continuous_transition_required(args) is True
    with pytest.raises(validator_thin.wire.VectorError, match="completed-launch gate"):
        validator_thin._validate_runtime_contract(args)


def test_journalled_launch_lineage_gates_regardless_of_config() -> None:
    """Ratchet: once this runtime has launched, it can never opt back out."""
    args = _relay_args()
    assert validator_thin._continuous_transition_required(args) is False
    _journal_launch_lineage(args)
    assert validator_thin._sn39_launch_lineage(args) is True
    assert validator_thin._continuous_transition_required(args) is True
    with pytest.raises(validator_thin.wire.VectorError, match="completed-launch gate"):
        validator_thin._validate_runtime_contract(args)


def test_launch_lineage_fails_closed_on_an_unreadable_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _relay_args()
    validator_thin._VALIDATOR_RUNTIME_ROOT.mkdir(parents=True, mode=0o700)

    def unreadable(_path: Path) -> dict:
        raise OSError("journal unavailable")

    monkeypatch.setattr(validator_thin, "_read_state_without_mutation", unreadable)
    assert validator_thin._sn39_launch_lineage(args) is True
    assert validator_thin._continuous_transition_required(args) is True


def test_absent_runtime_root_is_not_launch_lineage() -> None:
    """A first start has no journal at all; that is "never launched", not
    "unreadable". Reading it as unreadable would refuse every fresh relay."""
    args = _relay_args()
    assert not validator_thin._VALIDATOR_RUNTIME_ROOT.exists()
    assert validator_thin._sn39_launch_lineage(args) is False


def test_launch_material_probe_fails_closed_on_a_stat_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the material probe is blinded here.

    The journal probe is left working and reports "no lineage", so the True
    below can only come from the material probe itself.
    """
    args = _relay_args()
    material = {
        validator_thin.SN39_LAUNCH_CONTROLLED_DIR,
        validator_thin.SN39_LAUNCH_VERIFIER_BINARY,
        validator_thin.SN39_LAUNCH_APPROVAL_FILE,
    }
    real_stat = validator_thin.os.stat

    def denied(path, *args_, **kwargs):
        if path in material:
            raise PermissionError("cannot stat controlled material")
        return real_stat(path, *args_, **kwargs)

    assert validator_thin._sn39_launch_lineage(args) is False
    monkeypatch.setattr(validator_thin.os, "stat", denied)
    assert validator_thin._sn39_launch_obligation(args) is True


def test_unresolved_signer_identity_does_not_answer_the_lineage_probe() -> None:
    """Startup contract validation runs before the signer is bound.

    The journal is addressed by that identity, so the probe must report
    "unknown" instead of "never launched"; the default-on config flag still
    gates the runtime in that window.
    """
    args = _relay_args()
    args._submission_validator_hotkey = None
    args._submission_genesis_hash = None
    assert validator_thin._sn39_launch_lineage(args) is None


# ---------------------------------------------------------------------------
# Out of scope: non-Finney, offline, and other subnets are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broadcast", False),
        ("offline", True),
        ("netuid", 1),
    ],
)
def test_non_sn39_broadcast_paths_keep_their_prior_predicate(
    field: str, value: object
) -> None:
    args = _relay_args()
    setattr(args, field, value)
    # Outside the SN39 broadcast window the predicate has always been the
    # explicit flag, then the policy pin. That is unchanged in both directions.
    assert validator_thin._continuous_transition_required(args) is False
    args.require_completed_launch_for_broadcast = True
    assert validator_thin._continuous_transition_required(args) is True
    args.require_completed_launch_for_broadcast = None
    assert validator_thin._continuous_transition_required(args) is True
    args.require_policy = "confidential_primary_v1"
    assert validator_thin._continuous_transition_required(args) is False


def test_offline_and_other_subnets_never_consult_launch_material() -> None:
    _install_launch_material()
    for field, value in (("offline", True), ("netuid", 1), ("broadcast", False)):
        args = _relay_args()
        setattr(args, field, value)
        assert validator_thin._continuous_transition_required(args) is False


def test_non_finney_label_is_still_refused_for_sn39() -> None:
    args = _relay_args()
    args.network = "test"
    with pytest.raises(
        validator_thin.wire.VectorError, match="immutable trust profile"
    ):
        validator_thin._validate_runtime_contract(args)


# ---------------------------------------------------------------------------
# The relay still gets the full pinned trust profile, field by field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("publisher_url", "https://attacker.invalid"),
        ("public_key_hex", "00" * 32),
        ("key_id", "attacker-policy"),
        ("require_policy", "confidential_primary_v1"),
        ("evidence_url", "https://attacker.invalid/evidence"),
        ("provenance_registry_keys_digest", "sha256:" + "1" * 64),
        ("provenance_report_keys_digest", "sha256:" + "2" * 64),
        ("provenance_index_keys_digest", "sha256:" + "3" * 64),
        ("provenance_verifier_digest", "sha256:" + "4" * 64),
        ("provenance_source_revision", "5" * 40),
        ("provenance_mechanism", "attacker_mechanism"),
        ("provenance_burn_hotkey", "5AttackerBurn"),
        ("state_file", "/tmp/attacker-state.json"),
        ("network", "test"),
        ("provenance", "off"),
    ],
)
def test_relay_with_a_wrong_trust_profile_field_is_refused(
    field: str, value: object
) -> None:
    args = _relay_args()
    setattr(args, field, value)
    with pytest.raises(
        validator_thin.wire.VectorError, match="immutable trust profile"
    ):
        validator_thin._validate_runtime_contract(args)


def test_relay_cannot_redirect_the_runtime_root(tmp_path: Path) -> None:
    args = _relay_args()
    args.runtime_root = str(tmp_path / "attacker-controlled-runtime")
    with pytest.raises(validator_thin.wire.VectorError, match="canonical owner-only"):
        validator_thin._validate_runtime_contract(args)


# ---------------------------------------------------------------------------
# The chain boundary: relays pass, obligated runtimes do not
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _sign(payload: dict, private_key: Ed25519PrivateKey) -> dict:
    payload["signature"] = base64.b64encode(
        private_key.sign(wire_vector.canonical_bytes(payload))
    ).decode()
    return payload


def _signed_relay_vector(
    private_key: Ed25519PrivateKey,
    *,
    policy_version: int = 7,
    burn_hotkey: str = validator_thin.SN39_BURN_HOTKEY,
    validated_supply: bool = True,
) -> dict:
    """One real signed relay vector: 90% to the TDX miner, 10% burned.

    Against the fixture metagraph this derives exactly {1: 0.9, 2: 0.1}, which
    is what the reservation and the chain call below both claim.
    """
    now = datetime.now(UTC)
    metadata: dict[str, dict] = {
        "confidential_primary": {
            "contract_version": "v1",
            "mode": "confidential_primary",
            "source": "cathedral_confidential_tdx",
            "base_mass": 0.0,
            "confidential_mass": 1.0,
            "complete": True,
            "fresh": True,
            "confirmed": True,
        }
    }
    if validated_supply:
        metadata["validated_supply"] = {
            "contract_version": "v2",
            "intel_tdx_allocation": 0.90,
            "fixed_burn_allocation": 0.10,
            "burn_hotkey": burn_hotkey,
        }
    payload = {
        "vector_id": "vector-1",
        "policy_version": policy_version,
        "network": "finney",
        "netuid": 39,
        "generated_at": _iso(now),
        "expires_at": _iso(now + timedelta(minutes=5)),
        "burn_snapshot": {
            "burn_uid": None,
            "burn_hotkey": burn_hotkey,
            "forced_burn_percentage": 10.0,
        },
        "policy_metadata": metadata,
        "policy_hash": "sha256:relay-fixture",
        "key_id": validator_thin.SN39_WEIGHT_POLICY_KEY_ID,
        "weights": [
            {
                "miner_hotkey": TDX_MINER_HOTKEY,
                "weight": 1.0,
                "base_component": 0.0,
                "external_component": 1.0,
            }
        ],
    }
    return _sign(payload, private_key)


def _chain_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The boundary now verifies Cathedral's signature over the reserved payload,
    # so the tests need a key they can actually sign with. Pin the release
    # constants to this test's key and lane state file rather than weakening
    # what the boundary checks.
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        validator_thin,
        "DEFAULT_PUBLIC_KEY_HEX",
        private_key.public_key().public_bytes_raw().hex(),
    )
    monkeypatch.setattr(
        validator_thin, "SN39_STATE_FILE", tmp_path / "lane-state" / "thin-state.json"
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=100,
        valid_until_block=300,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2099, 1, 1, 0, 0, tzinfo=UTC),
        expected_next_epoch_start_block=240,
    )
    uid_safety = {"schema": "fixture_uid_safety"}
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            TDX_MINER_HOTKEY: 1,
            validator_thin.SN39_BURN_HOTKEY: 2,
            VALIDATOR_HOTKEY: 30,
        },
        validator_hotkey=VALIDATOR_HOTKEY,
        validator_uid=30,
        block=199,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        next_epoch_start_block=240,
    )
    monkeypatch.setattr(
        validator_thin,
        "_validate_resolved_chain_contract",
        lambda _args, _preflight: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_require_inclusion_policy_ready",
        lambda _policy, _preflight: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_require_uid_mapping_stability",
        lambda *_a, **_kw: uid_safety,
    )
    monkeypatch.setattr(
        validator_thin, "_validate_chain_constraints", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        validator_thin, "_chain_operation_deadline", lambda *_a, **_kw: nullcontext()
    )
    return private_key, policy, uid_safety, preflight


def _reserve_thin(
    args: SimpleNamespace,
    *,
    policy,
    uid_safety,
    payload: dict | None,
    uid_weights: list[list] | None = None,
    signed_vector_sha256: str | None = None,
) -> str:
    """Reserve exactly what a thin tick reserves, from a real signed payload.

    The digest defaults to the payload's own, because that is what the tick
    computes; the overrides exist so a test can reserve a deliberately
    inconsistent record without hand-rolling the whole identity.
    """
    identity = {
        "network": "finney",
        "netuid": 39,
        "mapping_block": 199,
        "validator_hotkey": VALIDATOR_HOTKEY,
        "validator_uid": 30,
        "vector_id": "vector-1",
        "policy_version": 7,
        "signed_vector_sha256": (
            signed_vector_sha256
            if signed_vector_sha256 is not None
            else validator_thin._sha256_document(payload or {})
        ),
        "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
        "uid_weights": uid_weights if uid_weights is not None else [[1, 0.9], [2, 0.1]],
        "uid_hotkeys": [[1, TDX_MINER_HOTKEY], [2, validator_thin.SN39_BURN_HOTKEY]],
        "next_epoch_start_block": 240,
        "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
        "uid_safety": uid_safety,
    }
    if payload is not None:
        identity["signed_vector"] = payload
    attempt_id = "sha256:" + "9" * 64
    validator_thin._reserve_common_submission(
        args, lane="thin", attempt_id=attempt_id, identity=identity
    )
    return attempt_id


def _authorize(args: SimpleNamespace, *, policy, preflight, uid_weights=None) -> None:
    validator_thin._authorize_sn39_chain_submission(
        args,
        uid_weights=uid_weights if uid_weights is not None else {1: 0.9, 2: 0.1},
        uid_hotkeys={1: TDX_MINER_HOTKEY, 2: validator_thin.SN39_BURN_HOTKEY},
        network="finney",
        netuid=39,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        preflight=preflight,
        inclusion_policy=policy,
    )


def test_relay_reaches_the_chain_boundary_without_an_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args, policy=policy, uid_safety=uid_safety, payload=_signed_relay_vector(key)
    )
    _authorize(args, policy=policy, preflight=preflight)


def test_obligated_runtime_is_still_refused_at_the_chain_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args, policy=policy, uid_safety=uid_safety, payload=_signed_relay_vector(key)
    )
    # Same durable reservation, but this runtime now owes SN39 a launch.
    _install_launch_material()
    args.require_completed_launch_for_broadcast = True
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="root-signed recurring-write authorization",
    ):
        _authorize(args, policy=policy, preflight=preflight)


def test_relay_boundary_refuses_a_smuggled_authority_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relay branch must not become a hole for claims it cannot prove."""
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args, policy=policy, uid_safety=uid_safety, payload=_signed_relay_vector(key)
    )
    journal = validator_thin._submission_state_path(args)
    state = validator_thin._read_state(journal)
    state["submission_pending_identity"]["continuous_authorization"] = {
        "authorization_sha256": "sha256:" + "e" * 64
    }
    validator_thin._write_state(journal, state)
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="claims launch or recurring-write authority",
    ):
        _authorize(args, policy=policy, preflight=preflight)


# ---------------------------------------------------------------------------
# The reservation is evidence, not authority: the boundary re-derives from the
# payload the reservation carries and refuses anything it cannot re-prove
# ---------------------------------------------------------------------------


def test_boundary_refuses_a_reservation_with_no_bound_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fabricated-reservation case: a bare digest proves nothing."""
    _key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args,
        policy=policy,
        uid_safety=uid_safety,
        payload=None,
        signed_vector_sha256="sha256:" + "c" * 64,
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="carries no signed vector to re-verify",
    ):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_refuses_a_payload_that_contradicts_its_reserved_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args,
        policy=policy,
        uid_safety=uid_safety,
        payload=_signed_relay_vector(key),
        signed_vector_sha256="sha256:" + "c" * 64,
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="does not hash to its reserved digest",
    ):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_refuses_a_payload_whose_signature_does_not_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed by a key that is not the pinned one, so only the signature is
    wrong; the digest still matches because it covers the signature field."""
    _key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    payload = _signed_relay_vector(Ed25519PrivateKey.generate())
    _reserve_thin(args, policy=policy, uid_safety=uid_safety, payload=payload)
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="ed25519 signature verify failed",
    ):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_refuses_a_payload_with_no_signature_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    payload = _signed_relay_vector(key)
    payload.pop("signature")
    _reserve_thin(args, policy=policy, uid_safety=uid_safety, payload=payload)
    with pytest.raises(validator_thin.wire.VectorError, match="missing signature"):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_refuses_a_validly_signed_payload_that_moves_the_burn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-signed after tampering, so the signature is genuine and only the
    fixed 90/10 contract is wrong.

    Two boundary checks catch this: the explicit _validated_supply_meta
    re-assertion and the uid-weight re-derivation, which validates the same
    contract on its way through vector_to_uid_weights. Removing either alone
    leaves this test passing; removing both lets a 0.11 burn through.
    """
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    payload = _signed_relay_vector(key)
    payload["policy_metadata"]["validated_supply"]["fixed_burn_allocation"] = 0.11
    _sign(payload, key)
    _reserve_thin(args, policy=policy, uid_safety=uid_safety, payload=payload)
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="fixed burn allocation must equal 0.10",
    ):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_refuses_a_payload_with_no_fixed_supply_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args,
        policy=policy,
        uid_safety=uid_safety,
        payload=_signed_relay_vector(key, validated_supply=False),
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="carries no fixed 90/10 supply contract",
    ):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_refuses_a_reservation_that_redirects_the_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reservation and chain call agree with each other and disagree with the
    payload. Only re-deriving from the payload catches this."""
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args,
        policy=policy,
        uid_safety=uid_safety,
        payload=_signed_relay_vector(key),
        uid_weights=[[1, 0.5], [2, 0.5]],
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="differs from the allocation its reserved signed vector derives",
    ):
        _authorize(
            args, policy=policy, preflight=preflight, uid_weights={1: 0.5, 2: 0.5}
        )


def test_boundary_refuses_a_payload_that_burns_to_the_wrong_hotkey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args,
        policy=policy,
        uid_safety=uid_safety,
        payload=_signed_relay_vector(key, burn_hotkey="5AttackerBurn"),
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="does not burn to the pinned hotkey",
    ):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_refuses_a_payload_the_rollback_fence_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fence is re-read here, not taken from the reservation, so a replayed
    policy version is refused even though the reservation looks current."""
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args,
        policy=policy,
        uid_safety=uid_safety,
        payload=_signed_relay_vector(key, policy_version=7),
    )
    validator_thin._write_state(
        Path(args.state_file), {"last_accepted_policy_version": 7}
    )
    with pytest.raises(validator_thin.wire.VectorError, match="rollback/replay"):
        _authorize(args, policy=policy, preflight=preflight)


def test_boundary_accepts_the_same_vector_while_the_fence_is_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counterpart to the rollback test: re-reading the fence must not refuse
    the write it is fencing. The fence only advances after the chain call
    succeeds, so a live lane state one version behind still authorizes."""
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    args = _relay_args()
    _reserve_thin(
        args,
        policy=policy,
        uid_safety=uid_safety,
        payload=_signed_relay_vector(key, policy_version=7),
    )
    validator_thin._write_state(
        Path(args.state_file), {"last_accepted_policy_version": 6}
    )
    _authorize(args, policy=policy, preflight=preflight)


class _ReachedTheChainCall(Exception):
    """Raised in place of the extrinsic once the boundary has authorized."""


def test_a_real_relay_tick_reserves_what_the_boundary_demands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Producer and consumer, together.

    Every other boundary test hand-builds the reservation, so all of them would
    still pass if the tick stopped binding the payload. This one runs the real
    thin tick, lets it build and fsync its own reservation, and then hands the
    real chain call to the real boundary.
    """
    key, policy, uid_safety, preflight = _chain_fixtures(tmp_path, monkeypatch)
    payload = _signed_relay_vector(key)
    args = _relay_args()
    args._events = SimpleNamespace(event=lambda _code, **_fields: None)
    monkeypatch.setattr(validator_thin, "fetch_vector", lambda _url: payload)
    monkeypatch.setattr(validator_thin, "chain_preflight", lambda **_kwargs: preflight)
    monkeypatch.setattr(
        validator_thin, "_vector_inclusion_policy", lambda *_a, **_kw: policy
    )
    # The shadow auditor is a background stage with its own coverage; it is not
    # part of what authorizes a write.
    monkeypatch.setattr(
        validator_thin, "_run_provenance_stage", lambda *_a, **_kw: ("shadow", None)
    )

    def _chain_call(uid_weights, **kwargs):
        validator_thin._authorize_sn39_chain_submission(
            kwargs["runtime_contract"],
            uid_weights=uid_weights,
            uid_hotkeys=kwargs["uid_hotkeys"],
            network=kwargs["network"],
            netuid=kwargs["netuid"],
            wallet_name=kwargs["wallet_name"],
            wallet_hotkey=kwargs["wallet_hotkey"],
            preflight=kwargs["preflight"],
            inclusion_policy=kwargs["inclusion_policy"],
        )
        raise _ReachedTheChainCall

    monkeypatch.setattr(validator_thin, "set_weights_on_chain", _chain_call)
    with pytest.raises(_ReachedTheChainCall):
        validator_thin._thin_tick_locked(args)

    reserved = validator_thin._read_state(validator_thin._submission_state_path(args))
    identity = reserved["submission_pending_identity"]
    assert identity["signed_vector"] == payload
    assert identity["signed_vector_sha256"] == validator_thin._sha256_document(payload)
    assert identity["uid_weights"] == [[1, 0.9], [2, 0.1]]


def test_signed_nonce_allowance_tracks_the_same_obligation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validator_thin, "_finalized_chain_head", lambda _sub: (199, "0x" + "f" * 64)
    )
    substrate = SimpleNamespace(get_account_next_index=lambda _ss58: 5)
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=VALIDATOR_HOTKEY))
    preflight = validator_thin.ChainPreflight(
        wallet=wallet,
        subtensor=SimpleNamespace(substrate=substrate),
        hotkey_to_uid={VALIDATOR_HOTKEY: 30},
        validator_hotkey=VALIDATOR_HOTKEY,
        validator_uid=30,
        block=199,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        finalized_hash="0x" + "f" * 64,
        next_epoch_start_block=240,
    )
    call = dict(
        attempt_id="sha256:" + "9" * 64,
        netuid=39,
        version_key=1,
        wire_uids=[1, 2],
        wire_weights=[58982, 6553],
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
    )

    obligated = _relay_args()
    obligated.provenance = "authority"
    with pytest.raises(validator_thin.wire.VectorError, match="signed nonce allowance"):
        validator_thin._submit_exact_sn39_extrinsic(
            preflight, runtime_contract=obligated, **call
        )

    # The relay has no signed nonce window to check, so it must get past this
    # guard. It fails later, on the real wallet/extrinsic work this fixture
    # deliberately does not provide.
    relay = _relay_args()
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - later failure is fine
        validator_thin._submit_exact_sn39_extrinsic(
            preflight, runtime_contract=relay, **call
        )
    assert "signed nonce allowance" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The fixed 10% burn is untouched by any of this
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("policy_metadata", "validated_supply", "fixed_burn_allocation"),
            0.11,
            "0.10",
        ),
        (("policy_metadata", "validated_supply", "intel_tdx_allocation"), 0.89, "0.90"),
        (("burn_snapshot", "forced_burn_percentage"), 5.0, "burn 10%"),
    ],
)
def test_tampered_burn_contract_is_still_refused(
    path: tuple[str, ...], value: object, message: str
) -> None:
    tampered = copy.deepcopy(validated_supply_payload())
    cursor = tampered
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(validator_thin.wire.VectorError, match=message):
        validator_thin._validated_supply_meta(tampered)


def test_untampered_burn_contract_still_passes() -> None:
    policy = validator_thin._validated_supply_meta(validated_supply_payload())
    assert policy is not None
    assert policy["fixed_burn_allocation"] == 0.10
    assert policy["intel_tdx_allocation"] == 0.90


# ---------------------------------------------------------------------------
# The shipped relay profile is a real, runnable configuration
# ---------------------------------------------------------------------------


def test_shipped_relay_profile_runs_without_a_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scaffold import cli

    root = Path(__file__).resolve().parents[3]
    args = cli._resolve_serve_config(
        SimpleNamespace(
            config=str(root / "config" / "validator-thin-sn39-relay.toml"),
            dry_run=False,
            once=False,
            offline=False,
        )
    )
    shipped_runtime_root = args.runtime_root
    args.broadcast = True
    args.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    args._submission_validator_hotkey = VALIDATOR_HOTKEY
    args._submission_genesis_hash = validator_thin.FINNEY_GENESIS_HASH
    assert args.provenance == "shadow"
    assert args.require_full_provenance_for_broadcast is False
    assert args.require_completed_launch_for_broadcast is False
    validator_thin._validate_runtime_contract(args)
    assert validator_thin._continuous_transition_required(args) is False

    # Every trust-bearing value is byte-identical to the operator profile.
    #
    # `provenance` is deliberately NOT in this list, and must not be added back.
    # It is the one field the two profiles are supposed to disagree on. A relay
    # must stay `shadow` -- asserted directly above, which is the stronger and
    # more honest claim -- while an operator running the authority lane sets
    # `authority`. That difference IS the distinction between originating
    # weights and relaying them, and scoping the launch gate to it is what this
    # whole file is about.
    #
    # Requiring byte-identity here asserted the opposite: that no operator may
    # ever run authority without breaking the relay's test. It passed only
    # because both shipped profiles happen to be `shadow` today, so the coupling
    # stayed invisible until a downstream operator profile set `authority` and
    # this test went red for doing exactly what the relay config documents.
    operator = cli._resolve_serve_config(
        SimpleNamespace(
            config=str(root / "config" / "validator-mainnet-sn39.toml"),
            dry_run=False,
            once=False,
            offline=False,
        )
    )
    for field in (
        "publisher_url",
        "public_key_hex",
        "key_id",
        "network",
        "netuid",
        "require_policy",
        "state_file",
        "evidence_url",
        "provenance_registry_keys_digest",
        "provenance_report_keys_digest",
        "provenance_index_keys_digest",
        "provenance_verifier_digest",
        "provenance_source_revision",
        "provenance_mechanism",
        "provenance_burn_hotkey",
        "max_submissions",
    ):
        assert getattr(args, field) == getattr(operator, field), field
    assert shipped_runtime_root == operator.runtime_root
