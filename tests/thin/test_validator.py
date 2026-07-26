from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from cathedral_thin import validator as validator_module
from cathedral_thin.core import StateStore, ThinSubnetError, mark_pending
from cathedral_thin.e2e import Axon, solve_wire, wire_response
from cathedral_thin.score_classes import (
    AssignmentPolicy,
    DecisionStore,
    EvidenceRef,
    ExternalClassPolicy,
    OwnerRegistrationPolicy,
    RegistrationCheckpoint,
    ScoreEntry,
    ScorePolicy,
    SourceCheckpoint,
    VerifiedOwnerRegistration,
    VerifiedReport,
)
from cathedral_thin.validator import (
    BittensorRuntime,
    Peer,
    ValidatorConfig,
    ValidatorRunner,
    close_dendrite,
    evaluate_peers,
    run_validator_loop,
    snapshot_peers,
)


@pytest.mark.parametrize(
    "network",
    [
        "finney",
        "wss://entrypoint-finney.opentensor.ai:443",
        "wss://self-hosted-finney.example",
        "test",
    ],
)
def test_legacy_sat_validator_refuses_sn39_broadcast_for_every_label(
    network: str,
) -> None:
    args = validator_module.build_parser().parse_args(
        ["--network", network, "--netuid", "39", "--broadcast"]
    )
    with pytest.raises(SystemExit, match="cannot broadcast on SN39"):
        validator_module.validate_args(args)


def test_legacy_runtime_refuses_direct_sn39_submission() -> None:
    calls: list[dict] = []
    runtime = BittensorRuntime(
        wallet=object(),
        subtensor=SimpleNamespace(set_weights=lambda **kwargs: calls.append(kwargs)),
        dendrite=object(),
        netuid=39,
        mev_protection=False,
        commit_reveal_version=4,
    )
    pending = SimpleNamespace(uids=[1], weights=[1.0])
    with pytest.raises(ThinSubnetError, match="disabled on SN39"):
        asyncio.run(runtime.submit_weights(pending))
    assert calls == []


def test_legacy_sat_validator_defaults_to_non_mainnet_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BT_NETWORK", raising=False)
    monkeypatch.delenv("BT_NETUID", raising=False)
    args = validator_module.build_parser().parse_args([])
    assert (args.network, args.netuid, args.broadcast) == ("local", 1, False)


def config() -> ValidatorConfig:
    return ValidatorConfig("local", 1, "validator", 16, 60, 5.0, 100.0, 0.8, 1.0, 4, 10)


def test_metagraph_requires_coldkeys_for_sybil_collapse():
    metagraph = SimpleNamespace(uids=[0], hotkeys=["h"], axons=[Axon("h")])
    with pytest.raises(Exception, match="coldkeys"):
        snapshot_peers(metagraph)


def test_transport_hotkey_is_required(tmp_path):
    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    peer = Peer(0, "miner", "cold", Axon("miner"), True)

    async def missing_wire_identity(_peer, synapse, _timeout):
        response = wire_response(synapse, hotkey="", assignment_b64=solve_wire(synapse))
        return response

    results = asyncio.run(
        evaluate_peers(
            [peer], state=state, config=cfg, round_id=1, query=missing_wire_identity
        )
    )
    assert results["miner"].reason == "axon_identity_mismatch"


def test_concurrency_queue_time_does_not_reduce_miner_score(tmp_path):
    cfg = ValidatorConfig("local", 1, "validator", 16, 60, 5.0, 100.0, 0.8, 1.0, 1, 10)
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    peers = [
        Peer(0, "slow-first", "cold-1", Axon("slow-first"), True),
        Peer(1, "fast-second", "cold-2", Axon("fast-second"), True),
    ]

    async def query(peer, synapse, _timeout):
        if peer.hotkey == "slow-first":
            await asyncio.sleep(0.06)
        return wire_response(
            synapse, hotkey=peer.hotkey, assignment_b64=solve_wire(synapse)
        )

    results = asyncio.run(
        evaluate_peers(peers, state=state, config=cfg, round_id=1, query=query)
    )
    assert results["fast-second"].reason == "verified"
    assert results["fast-second"].observed_ms < 30.0


class FakeRuntime:
    def __init__(self):
        self.responses = [SimpleNamespace(success=False), SimpleNamespace(success=True)]
        self.submissions = []
        self.query_calls = 0
        self._block = 20
        self.peer = Peer(0, "miner", "cold", Axon("miner"), True)

    def metagraph(self):
        return SimpleNamespace(
            uids=[self.peer.uid],
            hotkeys=[self.peer.hotkey],
            coldkeys=[self.peer.coldkey],
            axons=[self.peer.axon],
        )

    def block(self):
        return self._block

    async def query(self, peer, synapse, timeout):
        self.query_calls += 1
        return wire_response(
            synapse, hotkey=peer.hotkey, assignment_b64=solve_wire(synapse)
        )

    async def submit_weights(self, pending):
        self.submissions.append(
            (pending.digest, list(pending.uids), list(pending.weights))
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_runner_persists_and_retries_identical_vector(tmp_path):
    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    runtime = FakeRuntime()
    runner = ValidatorRunner(
        config=cfg, runtime=runtime, store=store, state=state, broadcast=True
    )
    assert not asyncio.run(runner.tick())
    assert state.pending_vector is not None
    state.pending_vector.next_retry_at_ms = 0
    store.save(state)
    assert asyncio.run(runner.tick())
    assert state.pending_vector is None
    assert runtime.submissions[0] == runtime.submissions[1]


def test_continuous_dry_run_queries_each_round_only_once(tmp_path):
    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    runtime = FakeRuntime()
    runner = ValidatorRunner(
        config=cfg, runtime=runtime, store=store, state=state, broadcast=False
    )
    assert asyncio.run(runner.tick())
    assert asyncio.run(runner.tick())
    assert runtime.query_calls == 1
    runtime._block += cfg.round_blocks
    assert asyncio.run(runner.tick())
    assert runtime.query_calls == 2


def test_all_zero_local_round_retains_prior_vector_and_completes(tmp_path, capsys):
    class ZeroRuntime(FakeRuntime):
        async def query(self, peer, synapse, timeout):
            self.query_calls += 1
            return wire_response(
                synapse,
                hotkey=peer.hotkey,
                assignment_b64="",
            )

    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    state.confirmed_vector_digest = "sha256:" + "a" * 64
    state.confirmed_decision_digest = "sha256:" + "b" * 64
    store.save(state)
    runtime = ZeroRuntime()
    runner = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=state,
        broadcast=True,
        decision_store=DecisionStore(tmp_path / "decisions"),
    )

    assert asyncio.run(runner.tick())
    assert runtime.query_calls == 1
    assert runtime.submissions == []
    assert state.pending_vector is None
    assert state.last_completed_round == 2
    assert state.ema_scores == {"miner": 0.0}
    assert state.confirmed_vector_digest == "sha256:" + "a" * 64
    assert state.confirmed_decision_digest == "sha256:" + "b" * 64
    decision = json.loads(next((tmp_path / "decisions").glob("*.json")).read_text())
    assert decision["onchain_vector"] == []
    assert decision["classes"][0]["normalized_weights"] == {}
    assert (
        "no positive scores; retained prior on-chain vector" in capsys.readouterr().out
    )

    reloaded = store.load_or_create()
    assert reloaded.last_completed_round == 2
    assert reloaded.confirmed_vector_digest == "sha256:" + "a" * 64
    assert reloaded.confirmed_decision_digest == "sha256:" + "b" * 64


def test_expired_validated_supply_revokes_to_burn_and_restart_cannot_restore(tmp_path):
    class SupplyRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.peers = [
                Peer(
                    0,
                    "burn-hotkey",
                    "burn-cold",
                    SimpleNamespace(hotkey="burn-hotkey", port=0, is_serving=False),
                    False,
                ),
                Peer(7, "tdx-miner", "tdx-cold", Axon("tdx-miner"), True),
            ]
            self.responses = [
                SimpleNamespace(success=True),
                SimpleNamespace(success=True),
                SimpleNamespace(success=True),
            ]

        def metagraph(self):
            return SimpleNamespace(
                uids=[peer.uid for peer in self.peers],
                hotkeys=[peer.hotkey for peer in self.peers],
                coldkeys=[peer.coldkey for peer in self.peers],
                axons=[peer.axon for peer in self.peers],
            )

    def external(class_id: str, allocation: str) -> ExternalClassPolicy:
        return ExternalClassPolicy(
            class_id=class_id,
            allocation=Decimal(allocation),
            source_id="cathedralconfidential",
            locations=("unused",),
            trusted_keys={"key": b"0" * 32},
            max_age_seconds=600,
            max_future_seconds=30,
            max_block_span=100,
            require_evidence=True,
            assignment=AssignmentPolicy(
                "metric", "verified_work_units", "linear", None
            ),
        )

    tdx = external("intel_tdx", "0.9")
    gpu = external("verified_gpu", "0.1")
    policy = ScorePolicy(
        network="local",
        netuid=1,
        classes=(tdx, gpu),
        digest="sha256:" + "99" * 32,
        burn_hotkey="burn-hotkey",
    )
    report = VerifiedReport(
        class_id=tdx.class_id,
        source_id=tdx.source_id,
        source_epoch=4,
        report_id="sha256:" + "11" * 32,
        previous_report_id=None,
        generated_at=datetime.now(UTC),
        valid_until=datetime.now(UTC),
        valid_from_block=0,
        valid_until_block=100,
        policy_digest="sha256:" + "22" * 32,
        verifier_digest="sha256:" + "33" * 32,
        signing_key_id="key",
        entries=(
            ScoreEntry(
                "tdx-miner",
                {"verified_work_units": Decimal(1)},
                None,
                ("receipt_verified",),
                (
                    EvidenceRef(
                        "receipt",
                        "sha256:" + "44" * 32,
                        "sha256:" + "55" * 32,
                        None,
                    ),
                ),
            ),
        ),
        document={},
    )
    runtime = SupplyRuntime()

    def loader(class_policy, **_kwargs):
        if class_policy.class_id == tdx.class_id and runtime._block == 20:
            return report, SourceCheckpoint(report.source_epoch, report.report_id)
        raise ThinSubnetError("evidence expired")

    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    runner = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=state,
        broadcast=True,
        score_policy=policy,
        report_loader=loader,
    )

    assert asyncio.run(runner.tick())
    assert runtime.submissions[-1][1:] == ([0, 7], [0.1, 0.9])

    runtime._block = 30
    assert asyncio.run(runner.tick())
    assert runtime.submissions[-1][1:] == ([0], [1.0])
    assert state.class_checkpoints[tdx.class_id] == {
        "source_epoch": 4,
        "report_id": report.report_id,
    }

    runtime._block = 40
    restarted = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=store.load_or_create(),
        broadcast=True,
        score_policy=policy,
        report_loader=loader,
    )
    assert asyncio.run(restarted.tick())
    assert runtime.submissions[-1][1:] == ([0], [1.0])


def test_external_only_class_sets_weights_without_validator_scoring_infra(tmp_path):
    class MultiPeerRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.peers = [
                Peer(0, "miner-a", "cold-a", Axon("miner-a"), True),
                Peer(1, "miner-b", "cold-b", Axon("miner-b"), True),
            ]
            self.responses = [SimpleNamespace(success=True)]

        def metagraph(self):
            return SimpleNamespace(
                uids=[peer.uid for peer in self.peers],
                hotkeys=[peer.hotkey for peer in self.peers],
                coldkeys=[peer.coldkey for peer in self.peers],
                axons=[peer.axon for peer in self.peers],
            )

    external = ExternalClassPolicy(
        class_id="confidential_compute",
        allocation=Decimal(1),
        source_id="cathedralconfidential",
        locations=("unused",),
        trusted_keys={"key": b"0" * 32},
        max_age_seconds=600,
        max_future_seconds=30,
        max_block_span=100,
        require_evidence=True,
        assignment=AssignmentPolicy("metric", "verified_work_units", "linear", None),
    )
    policy = ScorePolicy(
        network="local",
        netuid=1,
        classes=(external,),
        digest="sha256:" + "99" * 32,
    )
    report = VerifiedReport(
        class_id=external.class_id,
        source_id=external.source_id,
        source_epoch=4,
        report_id="sha256:" + "11" * 32,
        previous_report_id=None,
        generated_at=datetime.now(UTC),
        valid_until=datetime.now(UTC),
        valid_from_block=0,
        valid_until_block=100,
        policy_digest="sha256:" + "22" * 32,
        verifier_digest="sha256:" + "33" * 32,
        signing_key_id="key",
        entries=(
            ScoreEntry(
                "miner-a",
                {"verified_work_units": Decimal(3)},
                None,
                ("receipt_verified",),
                (
                    EvidenceRef(
                        "receipt", "sha256:" + "44" * 32, "sha256:" + "55" * 32, None
                    ),
                ),
            ),
            ScoreEntry(
                "miner-b",
                {"verified_work_units": Decimal(1)},
                None,
                ("receipt_verified",),
                (
                    EvidenceRef(
                        "receipt", "sha256:" + "66" * 32, "sha256:" + "77" * 32, None
                    ),
                ),
            ),
        ),
        document={},
    )

    def loader(_policy, **_kwargs):
        return report, SourceCheckpoint(report.source_epoch, report.report_id)

    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    runtime = MultiPeerRuntime()
    runner = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=state,
        broadcast=True,
        score_policy=policy,
        decision_store=DecisionStore(tmp_path / "decisions"),
        report_loader=loader,
    )
    assert asyncio.run(runner.tick())
    assert runtime.query_calls == 0
    assert runtime.submissions[0][1:] == ([0, 1], [0.75, 0.25])
    assert state.class_checkpoints[external.class_id] == {
        "source_epoch": 4,
        "report_id": report.report_id,
    }
    assert state.confirmed_decision_digest is not None
    assert len(list((tmp_path / "decisions").glob("*.json"))) == 1


def test_registered_subnet_owner_contributes_class_without_weight_key(tmp_path):
    class RegisteredRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.peers = [
                Peer(0, "miner-a", "cold-a", Axon("miner-a"), True),
                Peer(1, "miner-b", "cold-b", Axon("miner-b"), True),
                Peer(2, "owner-delegate", "source-owner", Axon("owner-delegate"), True),
            ]
            self.responses = [SimpleNamespace(success=True)]
            self.owner_lookups = []

        def metagraph(self):
            return SimpleNamespace(
                uids=[peer.uid for peer in self.peers],
                hotkeys=[peer.hotkey for peer in self.peers],
                coldkeys=[peer.coldkey for peer in self.peers],
                axons=[peer.axon for peer in self.peers],
            )

        def subnet_owner_coldkey(self, netuid, *, block):
            self.owner_lookups.append((netuid, block))
            return "source-owner"

    registration_policy = OwnerRegistrationPolicy(
        source_netuid=7,
        locations=("registration",),
        max_age_seconds=86_400,
        max_future_seconds=30,
        max_block_span=10_000,
        require_target_registration=True,
    )
    external = ExternalClassPolicy(
        class_id="confidential_compute",
        allocation=Decimal(1),
        source_id="testnet_owner_source",
        locations=("https://reports.example/latest.json",),
        trusted_keys={},
        max_age_seconds=600,
        max_future_seconds=30,
        max_block_span=100,
        require_evidence=True,
        assignment=AssignmentPolicy("metric", "verified_work_units", "linear", None),
        owner_registration=registration_policy,
    )
    policy = ScorePolicy(
        network="local",
        netuid=1,
        classes=(external,),
        digest="sha256:" + "99" * 32,
    )
    registration = VerifiedOwnerRegistration(
        source_netuid=7,
        target_netuid=1,
        owner_coldkey="source-owner",
        delegate_hotkey="owner-delegate",
        source_id=external.source_id,
        class_ids=(external.class_id,),
        report_locations=("https://reports.example/latest.json",),
        report_keys={"delegated-key": b"0" * 32},
        sequence=5,
        previous_registration_id=None,
        registration_id="sha256:" + "aa" * 32,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
        valid_from_block=0,
        valid_until_block=100,
        document={},
    )
    report = VerifiedReport(
        class_id=external.class_id,
        source_id=external.source_id,
        source_epoch=4,
        report_id="sha256:" + "11" * 32,
        previous_report_id=None,
        generated_at=datetime.now(UTC),
        valid_until=datetime.now(UTC),
        valid_from_block=0,
        valid_until_block=100,
        policy_digest="sha256:" + "22" * 32,
        verifier_digest="sha256:" + "33" * 32,
        signing_key_id="delegated-key",
        entries=(
            ScoreEntry(
                "miner-a",
                {"verified_work_units": Decimal(3)},
                None,
                ("receipt_verified",),
                (
                    EvidenceRef(
                        "receipt", "sha256:" + "44" * 32, "sha256:" + "55" * 32, None
                    ),
                ),
            ),
            ScoreEntry(
                "miner-b",
                {"verified_work_units": Decimal(1)},
                None,
                ("receipt_verified",),
                (
                    EvidenceRef(
                        "receipt", "sha256:" + "66" * 32, "sha256:" + "77" * 32, None
                    ),
                ),
            ),
        ),
        document={},
    )

    def registration_loader(_policy, **kwargs):
        assert kwargs["current_owner_coldkey"] == "source-owner"
        assert kwargs["registered_hotkeys"]["owner-delegate"] == "source-owner"
        return registration, RegistrationCheckpoint(
            "source-owner", "owner-delegate", 5, registration.registration_id
        )

    def report_loader(materialized, **_kwargs):
        assert materialized.locations == registration.report_locations
        assert materialized.trusted_keys == registration.report_keys
        return report, SourceCheckpoint(report.source_epoch, report.report_id)

    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    runtime = RegisteredRuntime()
    runner = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=state,
        broadcast=True,
        score_policy=policy,
        decision_store=DecisionStore(tmp_path / "decisions"),
        report_loader=report_loader,
        registration_loader=registration_loader,
    )

    assert asyncio.run(runner.tick())
    assert runtime.owner_lookups == [(7, 20)]
    assert runtime.query_calls == 0
    assert runtime.submissions[0][1:] == ([0, 1], [0.75, 0.25])
    assert state.registration_checkpoints[external.class_id] == {
        "owner_coldkey": "source-owner",
        "delegate_hotkey": "owner-delegate",
        "sequence": 5,
        "registration_id": registration.registration_id,
    }
    decision = json.loads(next((tmp_path / "decisions").glob("*.json")).read_text())
    provenance = decision["classes"][0]["assignment"]["owner_registration"]
    assert provenance["owner_coldkey"] == "source-owner"
    assert provenance["delegate_hotkey"] == "owner-delegate"


def test_pending_vector_is_cancelled_if_uid_changes_hotkey(tmp_path):
    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    runtime = FakeRuntime()
    runner = ValidatorRunner(
        config=cfg, runtime=runtime, store=store, state=state, broadcast=True
    )
    assert not asyncio.run(runner.tick())
    assert state.pending_vector is not None
    runtime.peer = Peer(0, "replacement", "new-cold", Axon("replacement"), True)
    state.pending_vector.next_retry_at_ms = 0
    store.save(state)
    assert not asyncio.run(runner.tick())
    assert state.pending_vector is None
    assert len(runtime.submissions) == 1


@pytest.mark.parametrize(
    "failure", ["owner_transfer", "delegate_deregistered", "registration_rotation"]
)
def test_pending_registered_vector_is_cancelled_if_authority_changes(tmp_path, failure):
    class AuthorityRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.owner = (
                "new-source-owner" if failure == "owner_transfer" else "source-owner"
            )

        def metagraph(self):
            hotkeys = [self.peer.hotkey]
            coldkeys = [self.peer.coldkey]
            axons = [self.peer.axon]
            uids = [self.peer.uid]
            if failure != "delegate_deregistered":
                hotkeys.append("owner-delegate")
                coldkeys.append("source-owner")
                axons.append(Axon("owner-delegate"))
                uids.append(1)
            return SimpleNamespace(
                uids=uids, hotkeys=hotkeys, coldkeys=coldkeys, axons=axons
            )

        def subnet_owner_coldkey(self, _netuid, *, block):
            assert block == 20
            return self.owner

    external = ExternalClassPolicy(
        class_id="confidential_compute",
        allocation=Decimal(1),
        source_id="testnet_owner_source",
        locations=("https://reports.example/latest.json",),
        trusted_keys={},
        max_age_seconds=600,
        max_future_seconds=30,
        max_block_span=100,
        require_evidence=True,
        assignment=AssignmentPolicy("metric", "verified_work_units", "linear", None),
        owner_registration=OwnerRegistrationPolicy(
            source_netuid=7,
            locations=("registration",),
            max_age_seconds=86_400,
            max_future_seconds=30,
            max_block_span=10_000,
            require_target_registration=True,
        ),
    )
    policy = ScorePolicy(
        network="local",
        netuid=1,
        classes=(external,),
        digest="sha256:" + "99" * 32,
    )
    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    registration_id = "sha256:" + "aa" * 32
    state.registration_checkpoints = {
        external.class_id: {
            "owner_coldkey": "source-owner",
            "delegate_hotkey": "owner-delegate",
            "sequence": 5,
            "registration_id": registration_id,
        }
    }
    mark_pending(
        state,
        uids=[0],
        weights=[1.0],
        hotkeys=["miner"],
        registration_ids={external.class_id: registration_id},
    )
    store.save(state)
    runtime = AuthorityRuntime()
    replacement = VerifiedOwnerRegistration(
        source_netuid=7,
        target_netuid=1,
        owner_coldkey="source-owner",
        delegate_hotkey="owner-delegate",
        source_id=external.source_id,
        class_ids=(external.class_id,),
        report_locations=external.locations,
        report_keys={"new-key": b"1" * 32},
        sequence=6,
        previous_registration_id=registration_id,
        registration_id="sha256:" + "bb" * 32,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
        valid_from_block=0,
        valid_until_block=100,
        document={},
    )

    def registration_loader(_policy, **_kwargs):
        return replacement, RegistrationCheckpoint(
            "source-owner",
            "owner-delegate",
            replacement.sequence,
            replacement.registration_id,
        )

    runner = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=state,
        broadcast=True,
        score_policy=policy,
        registration_loader=registration_loader,
    )

    assert not asyncio.run(runner.tick())
    assert state.pending_vector is None
    assert runtime.submissions == []


def test_ambiguous_chain_outcome_requires_explicit_retry(tmp_path):
    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    runtime = FakeRuntime()
    runtime.responses = [
        ConnectionError("outcome unknown"),
        SimpleNamespace(success=True),
    ]
    runner = ValidatorRunner(
        config=cfg, runtime=runtime, store=store, state=state, broadcast=True
    )
    assert not asyncio.run(runner.tick())
    assert state.pending_vector is not None
    assert state.pending_vector.ambiguous
    state.pending_vector.next_retry_at_ms = 0
    store.save(state)
    reloaded = store.load_or_create()
    assert reloaded.pending_vector is not None
    assert reloaded.pending_vector.ambiguous
    held = ValidatorRunner(
        config=cfg, runtime=runtime, store=store, state=reloaded, broadcast=True
    )
    assert not asyncio.run(held.tick())
    assert len(runtime.submissions) == 1

    opted_in = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=reloaded,
        broadcast=True,
        retry_ambiguous=True,
    )
    assert asyncio.run(opted_in.tick())
    assert reloaded.pending_vector is None
    assert len(runtime.submissions) == 2


def test_ambiguous_retry_stays_ambiguous_if_identity_refresh_fails(tmp_path):
    class FailingIdentityRuntime(FakeRuntime):
        def metagraph(self):
            raise ConnectionError("temporary rpc failure")

    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    mark_pending(state, uids=[0], weights=[1.0], hotkeys=["miner"])
    assert state.pending_vector is not None
    state.pending_vector.ambiguous = True
    runner = ValidatorRunner(
        config=cfg,
        runtime=FailingIdentityRuntime(),
        store=store,
        state=state,
        broadcast=True,
        retry_ambiguous=True,
    )

    assert not asyncio.run(runner.tick())
    assert state.pending_vector is not None
    assert state.pending_vector.ambiguous


def test_ambiguous_retry_stays_ambiguous_if_registration_refresh_fails(tmp_path):
    cfg = config()
    store = StateStore(tmp_path / "state.json", fingerprint=cfg.fingerprint())
    state = store.load_or_create()
    mark_pending(
        state,
        uids=[0],
        weights=[1.0],
        hotkeys=["miner"],
        registration_ids={"external": "sha256:" + "aa" * 32},
    )
    assert state.pending_vector is not None
    state.pending_vector.ambiguous = True
    runtime = FakeRuntime()
    runner = ValidatorRunner(
        config=cfg,
        runtime=runtime,
        store=store,
        state=state,
        broadcast=True,
        retry_ambiguous=True,
    )

    async def fail_registration_refresh(**_kwargs):
        raise ThinSubnetError("registration artifact temporarily unavailable")

    runner._load_owner_registrations = fail_registration_refresh

    assert not asyncio.run(runner.tick())
    assert state.pending_vector is not None
    assert state.pending_vector.ambiguous
    assert runtime.submissions == []


def test_validator_loop_holds_persisted_state_across_raw_tick_exception(capsys):
    class FailingRunner:
        async def tick(self):
            raise ConnectionError("temporary rpc failure")

    class StopLoop(Exception):
        pass

    async def stop_after_backoff(delay):
        assert delay == 5.0
        raise StopLoop

    with pytest.raises(StopLoop):
        asyncio.run(
            run_validator_loop(
                FailingRunner(),
                once=False,
                interval_secs=1.0,
                sleep=stop_after_backoff,
            )
        )
    assert "retaining persisted state and retrying" in capsys.readouterr().out


def test_validator_loop_once_returns_failure_on_raw_tick_exception():
    class FailingRunner:
        async def tick(self):
            raise ConnectionError("temporary rpc failure")

    assert (
        asyncio.run(run_validator_loop(FailingRunner(), once=True, interval_secs=5.0))
        == 1
    )


def test_validator_closes_dendrite_session_before_event_loop_exit():
    class FakeDendrite:
        def __init__(self):
            self.closed = False

        async def aclose_session(self):
            self.closed = True

    dendrite = FakeDendrite()
    asyncio.run(close_dendrite(dendrite))
    assert dendrite.closed


def test_async_main_closes_dendrite_after_normal_return(tmp_path, monkeypatch):
    class FakeDendrite:
        def __init__(self):
            self.closed = False

        async def aclose_session(self):
            self.closed = True

    dendrite = FakeDendrite()
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator"))
    monkeypatch.setattr(
        validator_module, "make_wallet", lambda *_args, **_kwargs: wallet
    )
    monkeypatch.setattr(
        validator_module, "make_subtensor", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        validator_module, "make_dendrite", lambda *_args, **_kwargs: dendrite
    )

    async def finish_once(_runner, **_kwargs):
        return 0

    monkeypatch.setattr(validator_module, "run_validator_loop", finish_once)
    args = validator_module.build_parser().parse_args(
        [
            "--network",
            "local",
            "--netuid",
            "1",
            "--state-file",
            str(tmp_path / "state.json"),
            "--decision-dir",
            str(tmp_path / "decisions"),
            "--once",
        ]
    )

    assert asyncio.run(validator_module.async_main(args)) == 0
    assert dendrite.closed


def test_async_main_closes_dendrite_if_wallet_identity_fails(monkeypatch):
    class BadWallet:
        @property
        def hotkey(self):
            raise ValueError("locked hotkey")

    class FakeDendrite:
        def __init__(self):
            self.closed = False

        async def aclose_session(self):
            self.closed = True

    dendrite = FakeDendrite()
    monkeypatch.setattr(
        validator_module, "make_wallet", lambda *_args, **_kwargs: BadWallet()
    )
    monkeypatch.setattr(
        validator_module, "make_subtensor", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        validator_module, "make_dendrite", lambda *_args, **_kwargs: dendrite
    )
    args = validator_module.build_parser().parse_args(
        ["--network", "local", "--netuid", "1", "--once"]
    )

    with pytest.raises(ValueError, match="locked hotkey"):
        asyncio.run(validator_module.async_main(args))
    assert dendrite.closed


def test_bittensor_adapter_passes_commit_reveal_compatibility_flags():
    calls = []

    class FakeSubtensor:
        def set_weights(
            self,
            wallet,
            netuid,
            uids,
            weights,
            version_key,
            commit_reveal_version,
            mev_protection,
            wait_for_inclusion,
            wait_for_finalization,
            wait_for_revealed_execution,
        ):
            calls.append(locals())
            return SimpleNamespace(success=True)

    runtime = BittensorRuntime(
        wallet=object(),
        subtensor=FakeSubtensor(),
        dendrite=object(),
        netuid=9,
        mev_protection=True,
        commit_reveal_version=4,
    )
    pending = SimpleNamespace(uids=[1], weights=[1.0])
    response = asyncio.run(runtime.submit_weights(pending))
    assert response.success
    assert calls[0]["commit_reveal_version"] == 4
    assert calls[0]["wait_for_revealed_execution"] is False


def test_bittensor_adapter_reads_source_subnet_owner_at_scoring_block():
    calls = []

    class FakeSubtensor:
        def subnet(self, netuid, *, block):
            calls.append((netuid, block))
            return SimpleNamespace(owner_coldkey="source-owner")

    runtime = BittensorRuntime(
        wallet=object(),
        subtensor=FakeSubtensor(),
        dendrite=object(),
        netuid=39,
        mev_protection=False,
        commit_reveal_version=4,
    )
    assert runtime.subnet_owner_coldkey(7, block=1200) == "source-owner"
    assert calls == [(7, 1200)]


def test_chain_weight_constraints_fail_closed_instead_of_uniform_fallback():
    class Constraints:
        def min_allowed_weights(self, *, netuid):
            return 2

        def max_weight_limit(self, *, netuid):
            return 0.6

    runtime = BittensorRuntime(
        wallet=object(),
        subtensor=Constraints(),
        dendrite=object(),
        netuid=1,
        mev_protection=False,
        commit_reveal_version=4,
    )
    with pytest.raises(Exception, match="below chain minimum"):
        runtime.prepare_weights([1], [1.0], SimpleNamespace(n=3))


def test_chain_weight_processor_caps_a_valid_vector():
    class Constraints:
        def min_allowed_weights(self, *, netuid):
            return 2

        def max_weight_limit(self, *, netuid):
            return 0.6

    runtime = BittensorRuntime(
        wallet=object(),
        subtensor=Constraints(),
        dendrite=object(),
        netuid=1,
        mev_protection=False,
        commit_reveal_version=4,
    )
    uids, weights = runtime.prepare_weights([1, 2], [0.9, 0.1], SimpleNamespace(n=3))
    assert uids == [1, 2]
    assert max(weights) <= 0.600001
    assert sum(weights) == pytest.approx(1.0)
