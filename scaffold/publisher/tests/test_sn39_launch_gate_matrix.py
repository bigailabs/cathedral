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

import copy
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_validator_thin_validated_supply import payload as validated_supply_payload

from scaffold import validator_thin

VALIDATOR_HOTKEY = "5CanonicalValidator"
LAUNCH_ATTEMPT_ID = "sha256:" + "1" * 64


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


def _chain_fixtures(monkeypatch: pytest.MonkeyPatch):
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
            "tdx-miner": 1,
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
    return policy, uid_safety, preflight


def _reserve_thin(args: SimpleNamespace, *, policy, uid_safety) -> str:
    identity = {
        "network": "finney",
        "netuid": 39,
        "mapping_block": 199,
        "validator_hotkey": VALIDATOR_HOTKEY,
        "validator_uid": 30,
        "vector_id": "vector-1",
        "policy_version": 7,
        "signed_vector_sha256": "sha256:" + "c" * 64,
        "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
        "uid_weights": [[1, 0.9], [2, 0.1]],
        "uid_hotkeys": [[1, "tdx-miner"], [2, validator_thin.SN39_BURN_HOTKEY]],
        "next_epoch_start_block": 240,
        "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
        "uid_safety": uid_safety,
    }
    attempt_id = "sha256:" + "9" * 64
    validator_thin._reserve_common_submission(
        args, lane="thin", attempt_id=attempt_id, identity=identity
    )
    return attempt_id


def test_relay_reaches_the_chain_boundary_without_an_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, uid_safety, preflight = _chain_fixtures(monkeypatch)
    args = _relay_args()
    _reserve_thin(args, policy=policy, uid_safety=uid_safety)
    validator_thin._authorize_sn39_chain_submission(
        args,
        uid_weights={1: 0.9, 2: 0.1},
        uid_hotkeys={1: "tdx-miner", 2: validator_thin.SN39_BURN_HOTKEY},
        network="finney",
        netuid=39,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        preflight=preflight,
        inclusion_policy=policy,
    )


def test_obligated_runtime_is_still_refused_at_the_chain_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, uid_safety, preflight = _chain_fixtures(monkeypatch)
    args = _relay_args()
    _reserve_thin(args, policy=policy, uid_safety=uid_safety)
    # Same durable reservation, but this runtime now owes SN39 a launch.
    _install_launch_material()
    args.require_completed_launch_for_broadcast = True
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="root-signed recurring-write authorization",
    ):
        validator_thin._authorize_sn39_chain_submission(
            args,
            uid_weights={1: 0.9, 2: 0.1},
            uid_hotkeys={1: "tdx-miner", 2: validator_thin.SN39_BURN_HOTKEY},
            network="finney",
            netuid=39,
            wallet_name=args.wallet_name,
            wallet_hotkey=args.wallet_hotkey,
            preflight=preflight,
            inclusion_policy=policy,
        )


def test_relay_boundary_refuses_a_smuggled_authority_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay branch must not become a hole for claims it cannot prove."""
    policy, uid_safety, preflight = _chain_fixtures(monkeypatch)
    args = _relay_args()
    _reserve_thin(args, policy=policy, uid_safety=uid_safety)
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
        validator_thin._authorize_sn39_chain_submission(
            args,
            uid_weights={1: 0.9, 2: 0.1},
            uid_hotkeys={1: "tdx-miner", 2: validator_thin.SN39_BURN_HOTKEY},
            network="finney",
            netuid=39,
            wallet_name=args.wallet_name,
            wallet_hotkey=args.wallet_hotkey,
            preflight=preflight,
            inclusion_policy=policy,
        )


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
        "provenance",
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
