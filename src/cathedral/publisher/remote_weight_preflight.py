"""Publisher-side one-shot preflight for signed remote weight vectors."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite

from cathedral.policy.signing import VectorVerificationError, verify_vector
from cathedral.publisher.weight_policy import (
    WeightPolicyStore,
    load_producer_from_env,
    produce_weight_policy_once,
)

_SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"


@dataclass(frozen=True)
class PublisherRemoteWeightPreflightResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


async def run_publisher_remote_weight_preflight(
    database_path: str,
    *,
    env: Mapping[str, str] | None = None,
    issued_at: datetime | None = None,
) -> PublisherRemoteWeightPreflightResult:
    """Build and verify one signed vector from the publisher DB.

    The publisher DB is opened read-only. The generated vector is stored
    only in a process-local :class:`WeightPolicyStore` and is not printed
    or persisted by this helper.
    """
    values = os.environ if env is None else env
    errors: list[str] = []
    warnings: list[str] = []

    db_path = await asyncio.to_thread(_existing_database_path, database_path)
    if db_path is None:
        return PublisherRemoteWeightPreflightResult(
            errors=("publisher database does not exist",),
            details={"database_exists": False},
        )

    try:
        loaded = load_producer_from_env(values)
    except Exception as exc:
        return PublisherRemoteWeightPreflightResult(
            errors=(f"weight policy producer config is invalid: {exc}",),
            details={"database_exists": True},
        )
    if loaded is None:
        return PublisherRemoteWeightPreflightResult(
            errors=(f"{_SIGNING_KEY_ENV} is required",),
            details={"database_exists": True},
        )

    config, private_key = loaded
    details: dict[str, Any] = {
        "database_exists": True,
        "network": config.network,
        "netuid": config.netuid,
        "key_id": config.key_id,
        "burn_uid": config.burn_uid,
        "forced_burn_percentage": config.forced_burn_percentage,
        "valid_for_secs": config.valid_for_secs,
        "limit": config.limit,
        "task_family_since_days": config.task_family_since_days,
        "task_family_weights": dict(config.task_family_weights or {}),
    }

    store = WeightPolicyStore()
    conn = await _connect_readonly(db_path)
    state_conn = await aiosqlite.connect(":memory:")
    try:
        vector = await produce_weight_policy_once(
            conn,
            store,
            private_key,
            config=config,
            issued_at=issued_at,
            state_conn=state_conn,
        )
    except Exception as exc:
        errors.append(f"could not build signed remote weight vector: {exc}")
        return PublisherRemoteWeightPreflightResult(
            errors=tuple(errors),
            warnings=tuple(warnings),
            details=details,
        )
    finally:
        await state_conn.close()
        await conn.close()

    try:
        verify_vector(
            vector,
            public_key=private_key.public_key(),
            expected_key_id=config.key_id,
        )
    except VectorVerificationError as exc:
        errors.append(f"signed vector self-verification failed: {exc}")

    now_iso = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        vector.invariant_check(
            network=config.network,
            netuid=config.netuid,
            now_iso=now_iso,
        )
    except ValueError as exc:
        errors.append(f"signed vector invariant check failed: {exc}")

    details.update(
        {
            "vector_id": vector.vector_id,
            "policy_version": vector.policy_version,
            "generated_at": vector.generated_at,
            "expires_at": vector.expires_at,
            "weight_entries": len(vector.weights),
            "has_signature": bool(vector.signature),
            "policy_hash": vector.policy_hash,
            "policy_metadata": vector.policy_metadata,
        }
    )

    if not vector.weights:
        warnings.append(
            "signed vector has no miner weight entries; burn fallback must be intentional"
        )
    sat_weight = float((config.task_family_weights or {}).get("synthetic_boolean_v1", 0.0))
    if sat_weight > 0.0:
        warnings.append("signed vector has nonzero synthetic_boolean_v1 policy weight")

    return PublisherRemoteWeightPreflightResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
        details=details,
    )


def _existing_database_path(database_path: str) -> str | None:
    expanded = Path(database_path).expanduser()
    if not expanded.exists():
        return None
    return str(expanded.resolve())


async def _connect_readonly(path: str) -> aiosqlite.Connection:
    uri = "file:" + quote(path, safe="/") + "?mode=ro"
    return await aiosqlite.connect(uri, uri=True)


__all__ = [
    "PublisherRemoteWeightPreflightResult",
    "run_publisher_remote_weight_preflight",
]
