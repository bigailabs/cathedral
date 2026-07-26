"""Distributed validator: challenge miners directly, verify, score, set weights."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bittensor as bt

from .bt_compat import (
    current_block,
    listify,
    make_dendrite,
    make_subtensor,
    make_wallet,
)
from .core import (
    PendingVector,
    StateStore,
    ThinSubnetError,
    ValidatorState,
    build_challenge,
    config_fingerprint,
    gate_scores_by_current_verification,
    grade_response,
    mark_pending,
    note_submission_failure,
    note_submission_success,
    response_succeeded,
    submission_failure_is_ambiguous,
    update_ema,
)
from .protocol import SatChallenge
from .score_classes import (
    ClassDecision,
    DecisionStore,
    RegistrationCheckpoint,
    ScorePolicy,
    SourceCheckpoint,
    compose_class_decisions,
    decision_document,
    default_score_policy,
    external_class_decision,
    load_best_owner_registration,
    load_best_report,
    load_score_policy,
    local_class_decision,
    materialize_registered_policy,
)

WEIGHTS_VERSION_KEY = 1_000_001


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Peer:
    uid: int
    hotkey: str
    coldkey: str
    axon: Any
    serviceable: bool


@dataclass(frozen=True)
class RoundResult:
    hotkey: str
    score: float
    reason: str
    observed_ms: float
    miner_reported_ms: float


@dataclass(frozen=True)
class ValidatorConfig:
    network: str
    netuid: int
    validator_hotkey: str
    n_vars: int
    n_clauses: int
    timeout_secs: float
    reference_ms: float
    correctness_share: float
    ema_alpha: float
    concurrency: int
    round_blocks: int
    score_policy_digest: str | None = None

    def fingerprint(self) -> str:
        return config_fingerprint(
            {
                "protocol": 1,
                "network": self.network,
                "netuid": self.netuid,
                "validator_hotkey": self.validator_hotkey,
                "n_vars": self.n_vars,
                "n_clauses": self.n_clauses,
                "timeout_secs": self.timeout_secs,
                "reference_ms": self.reference_ms,
                "correctness_share": self.correctness_share,
                "ema_alpha": self.ema_alpha,
                "round_blocks": self.round_blocks,
                "score_policy_digest": self.score_policy_digest,
            }
        )


def snapshot_peers(metagraph: Any) -> list[Peer]:
    uids = [int(value) for value in listify(metagraph.uids)]
    hotkeys = [str(value) for value in listify(metagraph.hotkeys)]
    if not hasattr(metagraph, "coldkeys"):
        raise ThinSubnetError("metagraph lacks coldkeys; Sybil collapse cannot run")
    coldkeys = [str(value) for value in listify(metagraph.coldkeys)]
    axons = list(metagraph.axons)
    if not (len(uids) == len(hotkeys) == len(coldkeys) == len(axons)):
        raise ThinSubnetError("metagraph arrays have inconsistent lengths")
    if len(set(uids)) != len(uids) or len(set(hotkeys)) != len(hotkeys):
        raise ThinSubnetError("metagraph contains duplicate UID or hotkey")
    peers = []
    for uid, hotkey, coldkey, axon in zip(uids, hotkeys, coldkeys, axons):
        port = int(getattr(axon, "port", 0) or 0)
        serving = bool(getattr(axon, "is_serving", port > 0)) and port > 0
        peers.append(
            Peer(
                uid=uid, hotkey=hotkey, coldkey=coldkey, axon=axon, serviceable=serving
            )
        )
    return peers


def remote_hotkey(response: Any) -> str:
    return str(getattr(getattr(response, "axon", None), "hotkey", "") or "")


def response_status_ok(response: Any) -> bool:
    dendrite = getattr(response, "dendrite", None)
    if dendrite is None:
        return False
    status = getattr(dendrite, "status_code", None)
    try:
        return int(status) == 200
    except (TypeError, ValueError):
        return False


async def evaluate_peers(
    peers: list[Peer],
    *,
    state: ValidatorState,
    config: ValidatorConfig,
    round_id: int,
    query: Callable[[Peer, SatChallenge, float], Awaitable[Any]],
) -> dict[str, RoundResult]:
    semaphore = asyncio.Semaphore(config.concurrency)

    async def one(peer: Peer) -> RoundResult:
        if not peer.serviceable:
            return RoundResult(peer.hotkey, 0.0, "axon_unavailable", 0.0, 0.0)
        async with semaphore:
            issued = now_ms()
            try:
                challenge, cnf = await asyncio.to_thread(
                    build_challenge,
                    state.master_secret,
                    netuid=config.netuid,
                    validator_hotkey=config.validator_hotkey,
                    miner_hotkey=peer.hotkey,
                    round_id=round_id,
                    issued_at_ms=issued,
                    ttl_ms=int(config.timeout_secs * 1000),
                    n_vars=config.n_vars,
                    n_clauses=config.n_clauses,
                )
            except (ThinSubnetError, OSError) as exc:
                return RoundResult(
                    peer.hotkey, 0.0, f"challenge_generation:{exc}", 0.0, 0.0
                )
            synapse = SatChallenge.from_challenge(challenge)
            # Start after acquiring the slot and generating the CNF: queueing and
            # validator-local work must never make an honest miner look slower.
            started = time.monotonic()
            try:
                response = await query(peer, synapse, config.timeout_secs)
            except Exception as exc:  # transport failures score zero; the loop survives
                observed = (time.monotonic() - started) * 1000.0
                return RoundResult(
                    peer.hotkey, 0.0, f"transport:{type(exc).__name__}", observed, 0.0
                )
            observed = (time.monotonic() - started) * 1000.0
        if isinstance(response, list):
            response = response[0] if response else None
        if response is None:
            return RoundResult(peer.hotkey, 0.0, "empty_response", observed, 0.0)
        if not response_status_ok(response):
            return RoundResult(peer.hotkey, 0.0, "transport_status", observed, 0.0)
        wire_hotkey = remote_hotkey(response)
        if wire_hotkey != peer.hotkey:
            return RoundResult(
                peer.hotkey, 0.0, "axon_identity_mismatch", observed, 0.0
            )
        miner_reported = float(getattr(response, "miner_elapsed_ms", 0.0) or 0.0)
        if getattr(response, "error", ""):
            return RoundResult(
                peer.hotkey, 0.0, "miner_error", observed, miner_reported
            )
        score, reason = grade_response(
            challenge,
            cnf,
            response_challenge_id=str(getattr(response, "challenge_id", "")),
            response_miner_hotkey=str(getattr(response, "miner_hotkey", "")),
            assignment_b64=str(getattr(response, "assignment_b64", "")),
            observed_ms=observed,
            received_at_ms=now_ms(),
            reference_ms=config.reference_ms,
            correctness_share=config.correctness_share,
        )
        return RoundResult(peer.hotkey, score, reason, observed, miner_reported)

    results = await asyncio.gather(*(one(peer) for peer in peers))
    return {result.hotkey: result for result in results}


def uid_vector(
    weights_by_hotkey: dict[str, float], peers: list[Peer]
) -> tuple[list[int], list[float]]:
    uid_of = {peer.hotkey: peer.uid for peer in peers}
    pairs = []
    for hotkey, weight in weights_by_hotkey.items():
        if hotkey not in uid_of:
            continue
        value = float(weight)
        if not math.isfinite(value) or value < 0:
            raise ThinSubnetError("non-finite or negative final weight")
        if value > 0:
            pairs.append((uid_of[hotkey], value))
    pairs.sort()
    if not pairs:
        return [], []
    total = sum(weight for _, weight in pairs)
    if not math.isfinite(total) or total <= 0:
        raise ThinSubnetError("final weight total is not positive")
    return [uid for uid, _ in pairs], [weight / total for _, weight in pairs]


class BittensorRuntime:
    def __init__(
        self,
        *,
        wallet: Any,
        subtensor: Any,
        dendrite: Any,
        netuid: int,
        mev_protection: bool,
        commit_reveal_version: int,
    ):
        self.wallet = wallet
        self.subtensor = subtensor
        self.dendrite = dendrite
        self.netuid = netuid
        self.mev_protection = mev_protection
        self.commit_reveal_version = commit_reveal_version

    async def query(self, peer: Peer, synapse: SatChallenge, timeout: float) -> Any:
        return await self.dendrite.forward(
            peer.axon,
            synapse,
            timeout=timeout,
            deserialize=False,
        )

    def metagraph(self) -> Any:
        return self.subtensor.metagraph(self.netuid, lite=True)

    def block(self) -> int:
        return current_block(self.subtensor)

    def subnet_owner_coldkey(self, netuid: int, *, block: int) -> str:
        getter = getattr(self.subtensor, "subnet", None)
        if not callable(getter):
            raise ThinSubnetError("installed Bittensor SDK lacks subnet owner lookup")
        info = getter(netuid, block=block)
        owner = str(getattr(info, "owner_coldkey", "") or "")
        if not owner:
            raise ThinSubnetError(f"source subnet {netuid} has no current owner")
        return owner

    async def submit_weights(self, pending: PendingVector) -> Any:
        if self.netuid == 39:
            raise ThinSubnetError(
                "legacy SAT validator broadcasts are disabled on SN39 "
                "regardless of network label or RPC endpoint"
            )
        kwargs = {
            "wallet": self.wallet,
            "netuid": self.netuid,
            "uids": pending.uids,
            "weights": pending.weights,
            "version_key": WEIGHTS_VERSION_KEY,
            "wait_for_inclusion": True,
            "wait_for_finalization": True,
        }
        signature = inspect.signature(self.subtensor.set_weights)
        if "commit_reveal_version" in signature.parameters:
            kwargs["commit_reveal_version"] = self.commit_reveal_version
        if "mev_protection" in signature.parameters:
            kwargs["mev_protection"] = self.mev_protection
            kwargs["wait_for_revealed_execution"] = False
        return await asyncio.to_thread(self.subtensor.set_weights, **kwargs)

    def prepare_weights(
        self, uids: list[int], weights: list[float], metagraph: Any
    ) -> tuple[list[int], list[float]]:
        """Apply current chain constraints without rewarding failed miners."""
        import numpy as np
        from bittensor.utils.weight_utils import process_weights_for_netuid

        positive_count = sum(float(weight) > 0 for weight in weights)
        min_allowed = int(self.subtensor.min_allowed_weights(netuid=self.netuid))
        max_limit = float(self.subtensor.max_weight_limit(netuid=self.netuid))
        if positive_count < min_allowed:
            raise ThinSubnetError(
                f"verified weight count {positive_count} is below chain minimum {min_allowed}"
            )
        if max_limit <= 0 or max_limit > 1:
            raise ThinSubnetError(f"invalid chain max_weight_limit {max_limit}")
        if positive_count * max_limit < 1.0 - 1e-9:
            raise ThinSubnetError(
                f"{positive_count} positive weights cannot satisfy max_weight_limit {max_limit}"
            )
        out_uids, out_weights = process_weights_for_netuid(
            np.asarray(uids, dtype=np.int64),
            np.asarray(weights, dtype=np.float32),
            self.netuid,
            self.subtensor,
            metagraph=metagraph,
        )
        return [int(value) for value in out_uids.tolist()], [
            float(value) for value in out_weights.tolist()
        ]


class ValidatorRunner:
    def __init__(
        self,
        *,
        config: ValidatorConfig,
        runtime: BittensorRuntime,
        store: StateStore,
        state: ValidatorState,
        broadcast: bool,
        retry_ambiguous: bool = False,
        score_policy: ScorePolicy | None = None,
        decision_store: DecisionStore | None = None,
        report_loader: Callable[..., Any] = load_best_report,
        registration_loader: Callable[..., Any] = load_best_owner_registration,
    ):
        self.config = config
        self.runtime = runtime
        self.store = store
        self.state = state
        self.broadcast = broadcast
        self.retry_ambiguous = retry_ambiguous
        self.score_policy = score_policy or default_score_policy(
            network=config.network, netuid=config.netuid
        )
        if (
            config.score_policy_digest is not None
            and config.score_policy_digest != self.score_policy.digest
        ):
            raise ThinSubnetError("validator config and score policy digests differ")
        self.decision_store = decision_store
        self.report_loader = report_loader
        self.registration_loader = registration_loader
        self._last_dry_run_round: int | None = None

    async def _load_owner_registrations(
        self,
        *,
        peers: list[Peer],
        block: int,
        require_same_authority: bool = False,
    ) -> dict[str, tuple[Any, RegistrationCheckpoint]]:
        policies = [
            item
            for item in self.score_policy.external_classes
            if item.owner_registration is not None
        ]
        if not policies:
            return {}
        getter = getattr(self.runtime, "subnet_owner_coldkey", None)
        if not callable(getter):
            raise ThinSubnetError("runtime lacks source subnet owner verification")
        source_netuids = sorted(
            {item.owner_registration.source_netuid for item in policies}
        )

        async def read_owner(source_netuid: int) -> tuple[int, str]:
            owner = await asyncio.to_thread(getter, source_netuid, block=block)
            owner_value = str(owner)
            if not owner_value:
                raise ThinSubnetError(
                    f"source subnet {source_netuid} has no current owner"
                )
            return source_netuid, owner_value

        owner_by_netuid = dict(
            await asyncio.gather(*(read_owner(netuid) for netuid in source_netuids))
        )
        registered_hotkeys = {peer.hotkey: peer.coldkey for peer in peers}
        semaphore = asyncio.Semaphore(8)

        async def load_one(class_policy: Any) -> tuple[str, Any, Any]:
            raw = self.state.registration_checkpoints.get(class_policy.class_id)
            checkpoint = (
                None
                if raw is None
                else RegistrationCheckpoint(
                    owner_coldkey=str(raw["owner_coldkey"]),
                    delegate_hotkey=str(raw["delegate_hotkey"]),
                    sequence=int(raw["sequence"]),
                    registration_id=str(raw["registration_id"]),
                )
            )
            if checkpoint is not None and require_same_authority:
                current_owner = owner_by_netuid[
                    class_policy.owner_registration.source_netuid
                ]
                if current_owner != checkpoint.owner_coldkey:
                    raise ThinSubnetError(
                        f"owner authorization changed for class {class_policy.class_id}"
                    )
                if (
                    registered_hotkeys.get(checkpoint.delegate_hotkey)
                    != checkpoint.owner_coldkey
                ):
                    raise ThinSubnetError(
                        f"owner delegate deregistered for class {class_policy.class_id}"
                    )
            async with semaphore:
                registration, accepted = await asyncio.to_thread(
                    self.registration_loader,
                    class_policy,
                    network=self.config.network,
                    netuid=self.config.netuid,
                    current_block=block,
                    current_owner_coldkey=owner_by_netuid[
                        class_policy.owner_registration.source_netuid
                    ],
                    registered_hotkeys=registered_hotkeys,
                    checkpoint=checkpoint,
                )
            return class_policy.class_id, registration, accepted

        loaded = await asyncio.gather(*(load_one(item) for item in policies))
        return {
            class_id: (registration, checkpoint)
            for class_id, registration, checkpoint in loaded
        }

    async def _submit_pending(
        self,
        *,
        metagraph: Any | None = None,
        current_registration_ids: dict[str, str] | None = None,
    ) -> bool:
        pending = self.state.pending_vector
        if pending is None:
            return True
        if pending.ambiguous and not self.retry_ambiguous:
            print(
                f"pending vector outcome ambiguous digest={pending.digest[:12]}; "
                "inspect chain state, then use --retry-ambiguous explicitly"
            )
            return False
        current = now_ms()
        if pending.next_retry_at_ms > current:
            print(
                f"pending vector retry deferred digest={pending.digest[:12]} attempts={pending.attempts}"
            )
            return False
        if not self.broadcast:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "digest": pending.digest,
                        "uids": pending.uids,
                        "weights": pending.weights,
                        "provenance_digest": pending.provenance_digest,
                    },
                    sort_keys=True,
                )
            )
            return False
        try:
            if metagraph is None:
                metagraph = await asyncio.to_thread(self.runtime.metagraph)
            current_peers = snapshot_peers(metagraph)
            current_hotkey_by_uid = {peer.uid: peer.hotkey for peer in current_peers}
        except Exception as exc:
            note_submission_failure(
                self.state, now_ms=current, ambiguous=pending.ambiguous
            )
            self.store.save(self.state)
            print(f"pending identity refresh exception={type(exc).__name__}")
            return False
        stale = [
            [uid, expected_hotkey, current_hotkey_by_uid.get(uid)]
            for uid, expected_hotkey in zip(pending.uids, pending.hotkeys)
            if current_hotkey_by_uid.get(uid) != expected_hotkey
        ]
        if stale:
            # Never let a deregistration/re-registration race transfer a stale
            # reward to a different hotkey that inherited the same UID.
            self.state.pending_vector = None
            self.store.save(self.state)
            print(
                json.dumps(
                    {"pending_vector_cancelled": "uid_hotkey_changed", "stale": stale}
                )
            )
            return False
        if pending.registration_ids:
            if current_registration_ids is None:
                try:
                    block = await asyncio.to_thread(self.runtime.block)
                    current_registrations = await self._load_owner_registrations(
                        peers=current_peers,
                        block=block,
                        require_same_authority=True,
                    )
                    current_registration_ids = {
                        class_id: registration.registration_id
                        for class_id, (registration, _) in current_registrations.items()
                    }
                except ThinSubnetError as exc:
                    message = str(exc)
                    if message.startswith(
                        ("owner authorization changed", "owner delegate deregistered")
                    ):
                        self.state.pending_vector = None
                        self.store.save(self.state)
                        print(
                            json.dumps(
                                {
                                    "pending_vector_cancelled": (
                                        "owner_authorization_changed"
                                    ),
                                    "error": message,
                                },
                                sort_keys=True,
                            )
                        )
                        return False
                    note_submission_failure(
                        self.state, now_ms=current, ambiguous=pending.ambiguous
                    )
                    self.store.save(self.state)
                    print(
                        "pending owner authorization refresh held "
                        f"digest={pending.digest[:12]} error={message[:200]}"
                    )
                    return False
            if current_registration_ids != pending.registration_ids:
                self.state.pending_vector = None
                self.store.save(self.state)
                print(
                    json.dumps(
                        {
                            "pending_vector_cancelled": "owner_registration_changed",
                            "expected": pending.registration_ids,
                            "current": current_registration_ids,
                        },
                        sort_keys=True,
                    )
                )
                return False
        try:
            response = await self.runtime.submit_weights(pending)
        except Exception as exc:
            note_submission_failure(self.state, now_ms=current, ambiguous=True)
            self.store.save(self.state)
            print(f"set_weights exception={type(exc).__name__}")
            return False
        if response_succeeded(response):
            note_submission_success(self.state)
            self.store.save(self.state)
            print(f"set_weights confirmed digest={pending.digest[:12]}")
            return True
        ambiguous = submission_failure_is_ambiguous(response)
        note_submission_failure(self.state, now_ms=current, ambiguous=ambiguous)
        self.store.save(self.state)
        message = str(getattr(response, "message", "") or "")[:200]
        print(
            f"set_weights failed digest={pending.digest[:12]} attempts={pending.attempts} "
            f"ambiguous={ambiguous} {message}"
        )
        return False

    async def tick(self) -> bool:
        if self.state.pending_vector is not None:
            return await self._submit_pending()
        metagraph = await asyncio.to_thread(self.runtime.metagraph)
        peers = snapshot_peers(metagraph)
        block = await asyncio.to_thread(self.runtime.block)
        round_id = block // self.config.round_blocks
        if not self.broadcast and round_id == self._last_dry_run_round:
            print(f"dry-run round {round_id} already previewed by this process")
            return True
        if round_id <= self.state.last_completed_round:
            print(f"round {round_id} already completed")
            return True
        coldkey_of = {peer.hotkey: peer.coldkey for peer in peers}
        decisions = []
        local_policy = self.score_policy.local_class
        if local_policy is not None:
            results = await evaluate_peers(
                peers,
                state=self.state,
                config=self.config,
                round_id=round_id,
                query=self.runtime.query,
            )
            observations = {hotkey: result.score for hotkey, result in results.items()}
            new_ema = update_ema(
                self.state.ema_scores,
                observations,
                current_hotkeys=[peer.hotkey for peer in peers],
                alpha=self.config.ema_alpha,
            )
            eligible_scores = gate_scores_by_current_verification(new_ema, observations)
            decisions.append(
                local_class_decision(
                    local_policy,
                    eligible_scores,
                    coldkey_of=coldkey_of,
                    reasons={
                        hotkey: result.reason for hotkey, result in results.items()
                    },
                )
            )
        else:
            results = {}
            new_ema = dict(self.state.ema_scores)

        next_checkpoints = dict(self.state.class_checkpoints)
        next_registration_checkpoints = dict(self.state.registration_checkpoints)
        source_semaphore = asyncio.Semaphore(8)
        try:
            owner_registrations = await self._load_owner_registrations(
                peers=peers, block=block
            )
        except ThinSubnetError:
            if self.score_policy.burn_hotkey is None:
                raise
            # A policy with an explicit burn destination treats unavailable or
            # invalid owner authorization as an empty class. Per-class loading
            # below converts that absence into the signed revocation vector.
            owner_registrations = {}

        async def load_external(
            class_policy: Any,
        ) -> tuple[Any, Any, Any, Any, Any]:
            raw_checkpoint = self.state.class_checkpoints.get(class_policy.class_id)
            checkpoint = (
                None
                if raw_checkpoint is None
                else SourceCheckpoint(
                    source_epoch=int(raw_checkpoint["source_epoch"]),
                    report_id=str(raw_checkpoint["report_id"]),
                )
            )
            registration = None
            registration_checkpoint = None
            effective_policy = class_policy
            if class_policy.owner_registration is not None:
                registration, registration_checkpoint = owner_registrations[
                    class_policy.class_id
                ]
                effective_policy = materialize_registered_policy(
                    class_policy, registration
                )
            async with source_semaphore:
                report, accepted_checkpoint = await asyncio.to_thread(
                    self.report_loader,
                    effective_policy,
                    network=self.config.network,
                    netuid=self.config.netuid,
                    current_block=block,
                    checkpoint=checkpoint,
                )
            return (
                effective_policy,
                report,
                accepted_checkpoint,
                registration,
                registration_checkpoint,
            )

        external_policies = self.score_policy.external_classes
        loaded_external = await asyncio.gather(
            *(load_external(item) for item in external_policies),
            return_exceptions=True,
        )
        for configured_policy, loaded in zip(external_policies, loaded_external):
            if isinstance(loaded, Exception):
                if self.score_policy.burn_hotkey is None:
                    raise loaded
                reason = f"{type(loaded).__name__}:{str(loaded)[:200]}"
                decisions.append(
                    ClassDecision(
                        class_id=configured_policy.class_id,
                        kind=configured_policy.kind,
                        allocation=str(configured_policy.allocation),
                        source_id=configured_policy.source_id,
                        source_epoch=None,
                        report_id=None,
                        assignment={"mode": "revoked_to_burn"},
                        raw_scores={},
                        normalized_weights={},
                        provenance={"class_failure": reason},
                    )
                )
                continue
            (
                class_policy,
                report,
                accepted_checkpoint,
                registration,
                registration_checkpoint,
            ) = loaded
            decisions.append(
                external_class_decision(
                    class_policy,
                    report,
                    coldkey_of=coldkey_of,
                    owner_registration=registration,
                )
            )
            next_checkpoints[class_policy.class_id] = {
                "source_epoch": accepted_checkpoint.source_epoch,
                "report_id": accepted_checkpoint.report_id,
            }
            if registration_checkpoint is not None:
                next_registration_checkpoints[class_policy.class_id] = {
                    "owner_coldkey": registration_checkpoint.owner_coldkey,
                    "delegate_hotkey": registration_checkpoint.delegate_hotkey,
                    "sequence": registration_checkpoint.sequence,
                    "registration_id": registration_checkpoint.registration_id,
                }
        decision_by_id = {item.class_id: item for item in decisions}
        decisions = [
            decision_by_id[item.class_id] for item in self.score_policy.classes
        ]
        hotkey_weights = compose_class_decisions(self.score_policy, decisions)
        uids, weights = uid_vector(hotkey_weights, peers)
        burn_only = self.score_policy.burn_hotkey is not None and hotkey_weights == {
            self.score_policy.burn_hotkey: 1.0
        }
        if uids and hasattr(self.runtime, "prepare_weights") and not burn_only:
            original = dict(zip(uids, weights))
            uids, weights = await asyncio.to_thread(
                self.runtime.prepare_weights, uids, weights, metagraph
            )
            if self.score_policy.external_classes:
                prepared = dict(zip(uids, weights))
                if set(prepared) != set(original) or any(
                    abs(prepared[uid] - original[uid]) > 1e-6 for uid in original
                ):
                    raise ThinSubnetError(
                        "chain constraints would alter configured class allocations; held"
                    )
        summary = {
            "round": round_id,
            "block": block,
            "miners": len(peers),
            "verified": sum(result.reason == "verified" for result in results.values()),
            "positive_weights": len(weights),
            "score_policy_digest": self.score_policy.digest,
            "classes": [item.class_id for item in decisions],
            "reasons": {
                reason: sum(r.reason == reason for r in results.values())
                for reason in sorted({r.reason for r in results.values()})
            },
        }
        print(json.dumps(summary, sort_keys=True))
        decision, decision_digest = decision_document(
            validator_hotkey=self.config.validator_hotkey,
            network=self.config.network,
            netuid=self.config.netuid,
            round_id=round_id,
            block=block,
            policy_digest=self.score_policy.digest,
            decisions=decisions,
            peers=[
                {
                    "uid": peer.uid,
                    "hotkey": peer.hotkey,
                    "coldkey": peer.coldkey,
                    "serviceable": peer.serviceable,
                }
                for peer in peers
            ],
            uids=uids,
            weights=weights,
        )
        if self.decision_store is not None:
            decision_path = await asyncio.to_thread(self.decision_store.write, decision)
            print(f"decision record={decision_path} digest={decision_digest}")
        if not self.broadcast:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "uids": uids,
                        "weights": weights,
                        "results": {
                            hotkey: result.reason
                            for hotkey, result in sorted(results.items())
                        },
                        "decision_digest": decision_digest,
                    },
                    sort_keys=True,
                )
            )
            self._last_dry_run_round = round_id
            return True
        self.state.ema_scores = new_ema
        self.state.class_checkpoints = next_checkpoints
        self.state.registration_checkpoints = next_registration_checkpoints
        self.state.last_completed_round = round_id
        if not uids:
            self.store.save(self.state)
            print("no positive scores; retained prior on-chain vector")
            return True
        hotkey_by_uid = {peer.uid: peer.hotkey for peer in peers}
        try:
            pending_hotkeys = [hotkey_by_uid[uid] for uid in uids]
        except KeyError as exc:
            raise ThinSubnetError(
                f"weight processor returned unknown UID {exc.args[0]}"
            ) from exc
        mark_pending(
            self.state,
            uids=uids,
            weights=weights,
            hotkeys=pending_hotkeys,
            provenance_digest=decision_digest,
            registration_ids={
                class_id: registration.registration_id
                for class_id, (registration, _) in owner_registrations.items()
            },
        )
        self.store.save(self.state)
        return await self._submit_pending(
            metagraph=metagraph,
            current_registration_ids={
                class_id: registration.registration_id
                for class_id, (registration, _) in owner_registrations.items()
            },
        )


async def run_validator_loop(
    runner: ValidatorRunner,
    *,
    once: bool,
    interval_secs: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run validator ticks while keeping continuous mode alive across RPC faults."""
    while True:
        try:
            await runner.tick()
        except ThinSubnetError as exc:
            print(f"validator fail-closed: {exc}")
            if once:
                return 1
        except Exception as exc:
            print(
                "validator tick exception; retaining persisted state and retrying "
                f"exception={type(exc).__name__}"
            )
            if once:
                return 1
        if once:
            return 0
        await sleep(max(5.0, interval_secs))


async def close_dendrite(dendrite: Any) -> None:
    """Close the SDK HTTP session before the event loop shuts down."""
    close = getattr(dendrite, "aclose_session", None)
    if callable(close):
        await close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the legacy Cathedral SAT validator on test/local subnets. "
            "SN39 mainnet uses cathedral-validator."
        )
    )
    parser.add_argument("--network", default=os.environ.get("BT_NETWORK", "local"))
    parser.add_argument(
        "--netuid", type=int, default=int(os.environ.get("BT_NETUID", "1"))
    )
    parser.add_argument(
        "--wallet-name", default=os.environ.get("BT_WALLET_NAME", "validator")
    )
    parser.add_argument(
        "--wallet-hotkey", default=os.environ.get("BT_WALLET_HOTKEY", "default")
    )
    parser.add_argument("--wallet-path", default=os.environ.get("BT_WALLET_PATH", ""))
    parser.add_argument(
        "--state-file",
        default=os.environ.get(
            "CATHEDRAL_THIN_STATE", "~/.cathedral-thin/validator.json"
        ),
    )
    parser.add_argument("--accept-config-change", action="store_true")
    parser.add_argument(
        "--score-policy",
        default=os.environ.get("CATHEDRAL_THIN_SCORE_POLICY", ""),
        help="canonical JSON class-allocation policy; default is 100%% local SAT",
    )
    parser.add_argument(
        "--decision-dir",
        default=os.environ.get("CATHEDRAL_THIN_DECISION_DIR", ""),
        help="immutable local provenance records (default: beside validator state)",
    )
    parser.add_argument(
        "--vars", type=int, default=int(os.environ.get("CATHEDRAL_THIN_VARS", "384"))
    )
    parser.add_argument(
        "--clauses",
        type=int,
        default=int(os.environ.get("CATHEDRAL_THIN_CLAUSES", "1635")),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("CATHEDRAL_THIN_TIMEOUT", "45")),
    )
    parser.add_argument(
        "--reference-ms",
        type=float,
        default=float(os.environ.get("CATHEDRAL_THIN_REFERENCE_MS", "15000")),
    )
    parser.add_argument("--correctness-share", type=float, default=0.8)
    parser.add_argument("--ema-alpha", type=float, default=0.35)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument(
        "--round-blocks",
        type=int,
        default=int(os.environ.get("CATHEDRAL_THIN_ROUND_BLOCKS", "100")),
    )
    parser.add_argument("--interval-secs", type=float, default=60.0)
    parser.add_argument("--commit-reveal-version", type=int, default=4)
    parser.add_argument("--mev-protection", action="store_true")
    parser.add_argument(
        "--retry-ambiguous",
        action="store_true",
        help="explicitly retry a persisted submission whose chain outcome is unknown",
    )
    parser.add_argument(
        "--broadcast", action="store_true", help="submit weights; default is dry-run"
    )
    parser.add_argument("--once", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.broadcast and args.netuid == 39:
        raise SystemExit(
            "cathedral-thin-validator cannot broadcast on SN39; "
            "use the immutable cathedral-validator SN39 release path"
        )
    if args.netuid < 0 or not 3 <= args.vars <= 4096 or not 1 <= args.clauses <= 20_000:
        raise SystemExit("invalid netuid or challenge dimensions")
    if (
        args.timeout <= 0
        or args.reference_ms <= 0
        or args.concurrency <= 0
        or args.round_blocks <= 0
    ):
        raise SystemExit("timeouts, concurrency, and round-blocks must be positive")
    if not 0 <= args.correctness_share <= 1 or not 0 < args.ema_alpha <= 1:
        raise SystemExit("invalid scoring fractions")
    ratio = args.clauses / args.vars
    if not 4.1 <= ratio <= 4.4:
        raise SystemExit(
            "3-SAT clause/variable ratio must stay near the 4.26 phase transition"
        )


async def async_main(args: argparse.Namespace) -> int:
    if args.broadcast and args.netuid == 39:
        raise ThinSubnetError(
            "legacy SAT validator broadcasts are disabled on SN39 regardless "
            "of network label or RPC endpoint"
        )
    score_policy = (
        load_score_policy(args.score_policy, network=args.network, netuid=args.netuid)
        if args.score_policy
        else default_score_policy(network=args.network, netuid=args.netuid)
    )
    wallet = make_wallet(
        bt,
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=args.wallet_path or None,
    )
    subtensor = make_subtensor(bt, network=args.network)
    dendrite = make_dendrite(bt, wallet=wallet)
    try:
        hotkey = str(wallet.hotkey.ss58_address)
        config = ValidatorConfig(
            network=args.network,
            netuid=args.netuid,
            validator_hotkey=hotkey,
            n_vars=args.vars,
            n_clauses=args.clauses,
            timeout_secs=args.timeout,
            reference_ms=args.reference_ms,
            correctness_share=args.correctness_share,
            ema_alpha=args.ema_alpha,
            concurrency=args.concurrency,
            round_blocks=args.round_blocks,
            score_policy_digest=score_policy.digest,
        )
        store = StateStore(args.state_file, fingerprint=config.fingerprint())
        with store.lease():
            state = store.load_or_create(allow_config_change=args.accept_config_change)
            runtime = BittensorRuntime(
                wallet=wallet,
                subtensor=subtensor,
                dendrite=dendrite,
                netuid=args.netuid,
                mev_protection=args.mev_protection,
                commit_reveal_version=args.commit_reveal_version,
            )
            runner = ValidatorRunner(
                config=config,
                runtime=runtime,
                store=store,
                state=state,
                broadcast=args.broadcast,
                retry_ambiguous=args.retry_ambiguous,
                score_policy=score_policy,
                decision_store=DecisionStore(
                    args.decision_dir
                    or Path(args.state_file).expanduser().parent / "decisions"
                ),
            )
            print(
                f"cathedral-thin validator hotkey={hotkey} "
                f"netuid={args.netuid} broadcast={args.broadcast}"
            )
            return await run_validator_loop(
                runner,
                once=args.once,
                interval_secs=args.interval_secs,
            )
    finally:
        await close_dendrite(dendrite)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    try:
        return asyncio.run(async_main(args))
    except ThinSubnetError as exc:
        print(f"validator fail-closed: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
