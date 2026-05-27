"""Tests for the SAT autopilot loop.

Drives ``run_one_tick`` and the planner against:
  - a real in-process SqliteChallengeSource (so DB writes from
    import_challenge_from_generator land in the same place the
    counter reads from), and
  - an httpx.MockTransport-backed SatGeneratorClient (so we can
    script the generator's responses).

The full ``run_autopilot_loop`` is covered with one cancellation
test; the per-tick behavior is the load-bearing surface and gets
the bulk of the coverage.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import httpx
import pytest

from cathedral.lanes.challenge_source import (
    SqliteChallengeSource,
    ensure_sqlite_challenge_source_schema,
)
from cathedral.publisher.sat_autopilot import (
    AutopilotConfig,
    _count_cathedral_pending_per_combo,
    _plan_imports,
    autopilot_enabled,
    config_from_env,
    run_autopilot_loop,
    run_one_tick,
)
from cathedral.publisher.sat_generator_client import SatGeneratorClient

_BASE = "https://gen.test"
_TOKEN = "test-token"
_CNF = b"p cnf 5 3\n1 2 0\n-2 3 0\n4 -5 0\n"
_CNF_SHA = hashlib.sha256(_CNF).hexdigest()
_FAMILY = "synthetic_boolean_v1"


# ---------------------------------------------------------------------------
# Fixtures + mock handler factory
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_source(tmp_path: Path):
    db_path = tmp_path / "publisher.db"
    conn = await aiosqlite.connect(str(db_path))
    await ensure_sqlite_challenge_source_schema(conn)
    await conn.commit()
    yield SqliteChallengeSource(conn), conn
    await conn.close()


def _lease_body(*, tier: int = 1, kind: str = "sha256_preimage") -> dict[str, Any]:
    return {
        "lease_id": f"lease_t{tier}_{kind}",
        "expires_at": "2026-05-27T16:00:00Z",
        "generator_run_id": f"gen_t{tier}_{kind}",
        "cnf_url": f"{_BASE}/v1/artifacts/gen_t{tier}_{kind}/cnf",
        "cnf_sha256": _CNF_SHA,
        "byte_size": len(_CNF),
        "num_vars": 5,
        "num_clauses": 3,
        "tier": tier,
        "kind": kind,
        "family": _FAMILY,
        "cnf_class": "structured_crypto",
    }


def _pool_health_body(combos: list[tuple[int, str, int]]) -> dict[str, Any]:
    """combos = [(tier, kind, ready_depth), ...]"""
    return {
        "families": [
            {
                "family": _FAMILY,
                "tier": tier,
                "kind": kind,
                "ready_depth": ready,
                "leased_depth": 0,
            }
            for tier, kind, ready in combos
        ],
        "producer": {"running": True},
    }


def _build_handler(
    *,
    pool_body: dict[str, Any],
    lease_fail: bool = False,
):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/pool/health":
            return httpx.Response(200, json=pool_body)
        if path == "/v1/challenges/lease":
            if lease_fail:
                return httpx.Response(409, json={"detail": "pool empty"})
            # Pull tier/kind from the request body so concurrent
            # different-combo leases each get the right shape back.
            import json as _json

            body = _json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                201, json=_lease_body(tier=body["tier"], kind=body["kind"])
            )
        if "/v1/artifacts/" in path and path.endswith("/cnf"):
            return httpx.Response(200, content=_CNF)
        if "/confirm" in path:
            return httpx.Response(200, json={"status": "confirmed"})
        if "/release" in path:
            return httpx.Response(200, json={"status": "released"})
        return httpx.Response(404)

    return handler, calls


# ---------------------------------------------------------------------------
# Config + env
# ---------------------------------------------------------------------------


def test_autopilot_enabled_reads_env() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CATHEDRAL_SAT_AUTOPILOT_ENABLED", None)
        assert autopilot_enabled() is False
    with patch.dict(os.environ, {"CATHEDRAL_SAT_AUTOPILOT_ENABLED": "true"}):
        assert autopilot_enabled() is True
    with patch.dict(os.environ, {"CATHEDRAL_SAT_AUTOPILOT_ENABLED": "no"}):
        assert autopilot_enabled() is False


def test_config_from_env_uses_defaults_when_unset() -> None:
    with patch.dict(os.environ, {}, clear=False):
        for k in list(os.environ):
            if k.startswith("CATHEDRAL_SAT_AUTOPILOT_"):
                os.environ.pop(k, None)
        cfg = config_from_env()
    assert cfg.interval_seconds == 300
    assert cfg.default_target_pending == 3
    assert cfg.max_imports_per_tick == 2
    assert cfg.storage_root == Path("/data/sat-challenges")
    assert cfg.target_overrides == {}


def test_config_from_env_parses_targets_json() -> None:
    with patch.dict(
        os.environ,
        {
            "CATHEDRAL_SAT_AUTOPILOT_TARGETS": (
                '{"1:sha256_preimage": 5, "3:sha256_preimage": 1}'
            )
        },
    ):
        cfg = config_from_env()
    assert cfg.target_for(1, "sha256_preimage") == 5
    assert cfg.target_for(3, "sha256_preimage") == 1
    # Fallback to default when no override:
    assert cfg.target_for(2, "random_3sat") == 3


def test_config_from_env_rejects_bad_interval() -> None:
    with patch.dict(
        os.environ, {"CATHEDRAL_SAT_AUTOPILOT_INTERVAL_SECONDS": "5"}
    ):
        with pytest.raises(ValueError):
            config_from_env()


def test_config_from_env_rejects_malformed_target_key() -> None:
    with patch.dict(
        os.environ,
        {"CATHEDRAL_SAT_AUTOPILOT_TARGETS": '{"tier1_kind": 3}'},
    ):
        with pytest.raises(ValueError, match="<tier>:<kind>"):
            config_from_env()


def test_config_from_env_rejects_negative_target_value() -> None:
    with patch.dict(
        os.environ,
        {"CATHEDRAL_SAT_AUTOPILOT_TARGETS": '{"1:sha256_preimage": -1}'},
    ):
        with pytest.raises(ValueError, match=">= 0"):
            config_from_env()


# ---------------------------------------------------------------------------
# Planner unit tests
# ---------------------------------------------------------------------------


def test_plan_imports_skips_combos_with_empty_pool() -> None:
    plan = _plan_imports(
        pool_families=[
            {"family": _FAMILY, "tier": 1, "kind": "sha256_preimage", "ready_depth": 0},
        ],
        cathedral_counts={(1, "sha256_preimage"): (0, 0)},
        config=AutopilotConfig(target_overrides={}),
    )
    assert plan == []


def test_plan_imports_caps_per_combo_by_ready_depth() -> None:
    """Target 5 pending, generator only has 2 ready -> plan 2, not 5."""
    plan = _plan_imports(
        pool_families=[
            {"family": _FAMILY, "tier": 1, "kind": "sha256_preimage", "ready_depth": 2},
        ],
        cathedral_counts={(1, "sha256_preimage"): (0, 0)},
        config=AutopilotConfig(
            default_target_pending=5,
            max_imports_per_tick=10,
            target_overrides={},
        ),
    )
    assert len(plan) == 1
    assert plan[0].target - plan[0].pending == 2


def test_plan_imports_respects_global_max_per_tick() -> None:
    plan = _plan_imports(
        pool_families=[
            {"family": _FAMILY, "tier": 1, "kind": "sha256_preimage", "ready_depth": 10},
            {"family": _FAMILY, "tier": 2, "kind": "sha256_preimage", "ready_depth": 10},
        ],
        cathedral_counts={},
        config=AutopilotConfig(
            default_target_pending=5,
            max_imports_per_tick=3,
            target_overrides={},
        ),
    )
    total = sum(n.target - n.pending for n in plan)
    assert total == 3


def test_plan_imports_no_deficit_returns_empty() -> None:
    plan = _plan_imports(
        pool_families=[
            {"family": _FAMILY, "tier": 1, "kind": "sha256_preimage", "ready_depth": 5},
        ],
        cathedral_counts={(1, "sha256_preimage"): (3, 1)},  # at target
        config=AutopilotConfig(default_target_pending=3, target_overrides={}),
    )
    assert plan == []


# ---------------------------------------------------------------------------
# Counter against a real DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_per_combo_groups_by_tier_and_kind(tmp_path, db_source) -> None:
    source, _ = db_source
    handler, _ = _build_handler(
        pool_body=_pool_health_body([(1, "sha256_preimage", 5)])
    )
    # Seed the DB by importing two pending of (1, sha256_preimage).
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            default_target_pending=2,
            max_imports_per_tick=2,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        await run_one_tick(client=client, source=source, config=cfg)
    counts = await _count_cathedral_pending_per_combo(source, _FAMILY)
    assert counts[(1, "sha256_preimage")] == (2, 0)


# ---------------------------------------------------------------------------
# End-to-end tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_imports_when_below_target(tmp_path, db_source) -> None:
    source, _ = db_source
    handler, calls = _build_handler(
        pool_body=_pool_health_body([(1, "sha256_preimage", 5)])
    )
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            default_target_pending=2,
            max_imports_per_tick=5,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        summary = await run_one_tick(client=client, source=source, config=cfg)
    assert summary["imports_succeeded"] == 2
    assert summary["errors"] == 0
    # Exactly 2 lease calls landed; not 5 (target is 2).
    leases = [p for m, p in calls if p == "/v1/challenges/lease"]
    assert len(leases) == 2


@pytest.mark.asyncio
async def test_tick_noop_when_at_target(tmp_path, db_source) -> None:
    source, _ = db_source
    handler, calls = _build_handler(
        pool_body=_pool_health_body([(1, "sha256_preimage", 5)])
    )
    # First tick: import up to target=2.
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            default_target_pending=2,
            max_imports_per_tick=5,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        await run_one_tick(client=client, source=source, config=cfg)
        # Second tick: pool is at target, expect zero leases.
        calls.clear()
        summary = await run_one_tick(client=client, source=source, config=cfg)
    assert summary["imports_succeeded"] == 0
    leases = [p for m, p in calls if p == "/v1/challenges/lease"]
    assert leases == []


@pytest.mark.asyncio
async def test_tick_handles_lease_failure_without_crashing(
    tmp_path, db_source
) -> None:
    source, _ = db_source
    handler, _ = _build_handler(
        pool_body=_pool_health_body([(1, "sha256_preimage", 5)]),
        lease_fail=True,
    )
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            default_target_pending=2,
            max_imports_per_tick=2,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        summary = await run_one_tick(client=client, source=source, config=cfg)
    assert summary["imports_succeeded"] == 0
    assert summary["errors"] >= 1


@pytest.mark.asyncio
async def test_tick_skips_combos_with_zero_ready_depth(
    tmp_path, db_source
) -> None:
    source, _ = db_source
    handler, calls = _build_handler(
        pool_body=_pool_health_body([(1, "sha256_preimage", 0)])
    )
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            default_target_pending=3,
            max_imports_per_tick=5,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        summary = await run_one_tick(client=client, source=source, config=cfg)
    assert summary["imports_succeeded"] == 0
    leases = [p for m, p in calls if p == "/v1/challenges/lease"]
    assert leases == []


@pytest.mark.asyncio
async def test_tick_only_imports_known_combos(tmp_path, db_source) -> None:
    """Generator advertises (1, random_3sat). Operator pre-seeded a
    (1, sha256_preimage) row already pending. Autopilot must NOT confuse
    the kinds when counting deficit."""
    source, _ = db_source
    # Tick 1: import 2x random_3sat
    handler, _ = _build_handler(
        pool_body=_pool_health_body([(1, "random_3sat", 5)])
    )
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            default_target_pending=2,
            max_imports_per_tick=2,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        s1 = await run_one_tick(client=client, source=source, config=cfg)
    assert s1["imports_succeeded"] == 2
    counts = await _count_cathedral_pending_per_combo(source, _FAMILY)
    assert counts.get((1, "random_3sat"), (0, 0)) == (2, 0)
    assert (1, "sha256_preimage") not in counts


# ---------------------------------------------------------------------------
# Loop: cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_exits_promptly_when_stop_set_mid_plan(
    tmp_path, db_source
) -> None:
    """If stop fires while the tick is iterating its plan, the tick must
    bail before starting another lease — not drain the full plan."""
    source, _ = db_source
    handler, calls = _build_handler(
        pool_body=_pool_health_body([(1, "sha256_preimage", 10)])
    )
    stop = asyncio.Event()
    stop.set()  # pre-set: tick should do zero imports
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            default_target_pending=5,
            max_imports_per_tick=5,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        summary = await run_one_tick(
            client=client, source=source, config=cfg, stop=stop
        )
    assert summary["stopped_early"] is True
    assert summary["imports_succeeded"] == 0
    leases = [p for m, p in calls if p == "/v1/challenges/lease"]
    assert leases == []


@pytest.mark.asyncio
async def test_counter_skips_rows_with_non_dict_audit(tmp_path, db_source) -> None:
    """A row with audit_metadata stored as a JSON list (not a dict) must
    not crash the counter — just get skipped with a warning."""
    source, conn = db_source
    # Insert a row by hand whose audit_metadata is a JSON list.
    await conn.execute(
        "INSERT INTO lane_challenges "
        "(challenge_id, family_id, tier, cnf_text, cnf_path, status, "
        "audit_metadata, created_at_iso, updated_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "bad-row",
            _FAMILY,
            1,
            "",
            "/tmp/whatever.cnf",
            "pending",
            '["this", "is", "a", "list"]',
            "2026-05-27T00:00:00Z",
            "2026-05-27T00:00:00Z",
        ),
    )
    await conn.commit()
    counts = await _count_cathedral_pending_per_combo(source, _FAMILY)
    # Counter returned without crashing; bad row absent from counts.
    assert (1, "") not in counts


@pytest.mark.asyncio
async def test_counter_treats_non_string_kind_as_empty(tmp_path, db_source) -> None:
    """A row whose audit_metadata.kind is a number (operator mistake)
    must not match any generator-advertised combo."""
    source, conn = db_source
    await conn.execute(
        "INSERT INTO lane_challenges "
        "(challenge_id, family_id, tier, cnf_text, cnf_path, status, "
        "audit_metadata, created_at_iso, updated_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "weird-kind",
            _FAMILY,
            1,
            "",
            "/tmp/whatever.cnf",
            "pending",
            '{"kind": 42}',
            "2026-05-27T00:00:00Z",
            "2026-05-27T00:00:00Z",
        ),
    )
    await conn.commit()
    counts = await _count_cathedral_pending_per_combo(source, _FAMILY)
    # Bucketed as "", not as "42".
    assert counts.get((1, ""), (0, 0)) == (1, 0)
    assert (1, "42") not in counts


@pytest.mark.asyncio
async def test_loop_exits_when_stop_set(tmp_path, db_source) -> None:
    """Stop set externally interrupts the wait_for sleep and exits."""
    source, _ = db_source
    handler, _ = _build_handler(
        pool_body=_pool_health_body([(1, "sha256_preimage", 0)])  # no work
    )
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        cfg = AutopilotConfig(
            interval_seconds=30,  # would otherwise sleep 30s
            default_target_pending=0,
            max_imports_per_tick=1,
            storage_root=tmp_path / "cnfs",
            target_overrides={},
        )
        stop = asyncio.Event()
        loop_task = asyncio.create_task(
            run_autopilot_loop(
                client=client, source=source, config=cfg, stop=stop
            )
        )
        # Give the tick a moment to start, then ask it to stop.
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)
    assert loop_task.done() and not loop_task.cancelled()
