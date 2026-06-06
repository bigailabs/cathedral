"""Integration tests for ``import_challenge_from_generator``.

Uses ``httpx.MockTransport`` for the generator side + a real
in-process SQLite ``SqliteChallengeSource`` so the DB upsert /
activate paths exercise their real code.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest

from cathedral.lanes.challenge_source import (
    SqliteChallengeSource,
    ensure_sqlite_challenge_source_schema,
)
from cathedral.publisher.sat_generator_client import (
    SatGeneratorClient,
)
from cathedral.publisher.sat_generator_import import (
    import_challenge_from_generator,
)

_BASE = "https://gen.test"
_TOKEN = "test-token"
_CNF = b"p cnf 5 3\n1 2 0\n-2 3 0\n4 -5 0\n"
_CNF_SHA = hashlib.sha256(_CNF).hexdigest()


def _lease_body_for(cnf: bytes = _CNF) -> dict[str, Any]:
    sha = hashlib.sha256(cnf).hexdigest()
    header = next(line for line in cnf.decode().splitlines() if line.startswith("p cnf "))
    _, _, num_vars, num_clauses = header.split()
    return {
        "lease_id": "lease_xyz",
        "expires_at": "2026-05-27T16:00:00Z",
        "generator_run_id": "gen_xyz",
        "cnf_url": f"{_BASE}/v1/artifacts/gen_xyz/cnf",
        "cnf_sha256": sha,
        "byte_size": len(cnf),
        "num_vars": int(num_vars),
        "num_clauses": int(num_clauses),
        "tier": 1,
        "kind": "sha256_preimage",
        "family": "synthetic_boolean_v1",
        "cnf_class": "structured_crypto",
    }


def _record_handler(cnf: bytes = _CNF, *, fail: str | None = None):
    """Build a mock handler that records every call and can be made to fail.

    ``fail`` triggers:
      - "confirm": confirm returns 409
      - "fetch_hash": fetch returns wrong-byte body
      - "fetch_404": fetch returns 404
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/challenges/lease":
            return httpx.Response(201, json=_lease_body_for(cnf))
        if "/v1/artifacts/" in path and path.endswith("/cnf"):
            if fail == "fetch_hash":
                return httpx.Response(200, content=b"corrupted bytes")
            if fail == "fetch_404":
                return httpx.Response(404, json={"detail": "missing"})
            return httpx.Response(200, content=cnf)
        if "/confirm" in path:
            if fail == "confirm":
                return httpx.Response(409, json={"detail": "hash mismatch"})
            return httpx.Response(200, json={"status": "confirmed"})
        if "/release" in path:
            return httpx.Response(200, json={"status": "released"})
        return httpx.Response(404)

    return handler, calls


@pytest.fixture
async def db_source(tmp_path: Path):
    db_path = tmp_path / "publisher.db"
    conn = await aiosqlite.connect(str(db_path))
    await ensure_sqlite_challenge_source_schema(conn)
    await conn.commit()
    yield SqliteChallengeSource(conn), conn
    await conn.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_import_and_activate(tmp_path: Path, db_source) -> None:
    source, _conn = db_source
    handler, calls = _record_handler()
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        result = await import_challenge_from_generator(
            client=client,
            source=source,
            storage_root=tmp_path / "cnfs",
            tier=1,
            kind="sha256_preimage",
        )

    # Returned shape
    assert result.tier == 1
    assert result.kind == "sha256_preimage"
    assert result.cnf_sha256 == _CNF_SHA
    assert result.activated is True
    assert result.cathedral_challenge_id.startswith("sat-t1-sha256-preimage-")

    # CNF on disk
    cnf_path = tmp_path / "cnfs" / f"{result.cathedral_challenge_id}.cnf"
    assert cnf_path.exists()
    assert cnf_path.read_bytes() == _CNF

    # DB row is active in the right tier
    active = await source.get_active_for_tier("synthetic_boolean_v1", 1)
    assert active is not None
    assert active.challenge_id == result.cathedral_challenge_id
    assert active.tier == 1
    audit = active.audit_metadata
    assert audit["source"] == "generator_lease"
    assert audit["generator_run_id"] == "gen_xyz"
    assert audit["cnf_sha256"] == _CNF_SHA

    # File-backed storage: the row points at the on-disk CNF and carries NO
    # inline cnf_text, so the db_write_lock insert stays tiny (throughput).
    assert audit["storage"] == "file"
    assert active.cnf_path == str(cnf_path)
    assert active.cnf_text == ""
    # 64-bit suffix to avoid same-day id collisions at high import volume.
    suffix = result.cathedral_challenge_id.rsplit("-", 1)[-1]
    assert len(suffix) == 16

    # Lease was confirmed, NOT released.
    paths = [p for _, p in calls]
    assert any("/confirm" in p for p in paths)
    assert not any("/release" in p for p in paths)


@pytest.mark.asyncio
async def test_import_without_activate_leaves_pending(tmp_path: Path, db_source) -> None:
    source, _ = db_source
    handler, _ = _record_handler()
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        result = await import_challenge_from_generator(
            client=client,
            source=source,
            storage_root=tmp_path / "cnfs",
            tier=1,
            kind="sha256_preimage",
            activate=False,
        )
    assert result.activated is False
    active = await source.get_active_for_tier("synthetic_boolean_v1", 1)
    assert active is None
    pending = await source.list_for_family("synthetic_boolean_v1", status="pending")
    assert any(p.challenge_id == result.cathedral_challenge_id for p in pending)


# ---------------------------------------------------------------------------
# Failure paths — release must fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hash_mismatch_releases_and_does_not_activate(
    tmp_path: Path, db_source
) -> None:
    source, _ = db_source
    handler, calls = _record_handler(fail="fetch_hash")
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(Exception):
            await import_challenge_from_generator(
                client=client,
                source=source,
                storage_root=tmp_path / "cnfs",
                tier=1,
                kind="sha256_preimage",
            )

    # No active row landed
    active = await source.get_active_for_tier("synthetic_boolean_v1", 1)
    assert active is None
    # Lease was released
    paths = [p for _, p in calls]
    assert any("/release" in p for p in paths)
    assert not any("/confirm" in p for p in paths)
    # No orphan CNF on disk
    cnfs_dir = tmp_path / "cnfs"
    if cnfs_dir.exists():
        assert not list(cnfs_dir.glob("*.cnf"))


@pytest.mark.asyncio
async def test_artifact_404_releases_lease(tmp_path: Path, db_source) -> None:
    source, _ = db_source
    handler, calls = _record_handler(fail="fetch_404")
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(Exception):
            await import_challenge_from_generator(
                client=client,
                source=source,
                storage_root=tmp_path / "cnfs",
                tier=1,
                kind="sha256_preimage",
            )
    paths = [p for _, p in calls]
    assert any("/release" in p for p in paths)


@pytest.mark.asyncio
async def test_confirm_failure_keeps_committed_row_and_file(
    tmp_path: Path, db_source
) -> None:
    """Confirm coming back 409 (or any error) AFTER the DB row was
    upserted must NOT delete the on-disk CNF — the public active row
    references it. The lease side may be inconsistent (auto-expires
    generator-side); Cathedral side stays consistent.
    """
    source, _ = db_source
    handler, calls = _record_handler(fail="confirm")
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(Exception):
            await import_challenge_from_generator(
                client=client,
                source=source,
                storage_root=tmp_path / "cnfs",
                tier=1,
                kind="sha256_preimage",
            )
    # confirm was attempted; release was the recovery on the generator side
    paths = [p for _, p in calls]
    assert any("/confirm" in p for p in paths)
    assert any("/release" in p for p in paths)
    # CRITICAL: the DB row was upserted + activated, so the CNF file
    # MUST still be present (the row points at it).
    cnfs_dir = tmp_path / "cnfs"
    files = list(cnfs_dir.glob("*.cnf"))
    assert len(files) == 1, "CNF was deleted after confirm-fail; row would be orphaned"
    # And the active row still exists, referencing this file.
    active = await source.get_active_for_tier("synthetic_boolean_v1", 1)
    assert active is not None
    assert active.cnf_path == str(files[0])


# ---------------------------------------------------------------------------
# Boundary — audit metadata stays out of public projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_metadata_carries_generator_fields_privately(
    tmp_path: Path, db_source
) -> None:
    """generator_run_id and cnf_class should live in audit_metadata but
    NOT in any miner-facing projection. The public projection is built
    in publisher/submit.py:_public_challenge_view; that function only
    reads audit['kind'] + a handful of size fields. Generator-specific
    fields stay in audit and never reach the wire."""
    source, conn = db_source
    handler, _ = _record_handler()
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        await import_challenge_from_generator(
            client=client,
            source=source,
            storage_root=tmp_path / "cnfs",
            tier=1,
            kind="sha256_preimage",
        )
    # Verify by directly reading the row: audit has the private fields,
    # but the public projection — which submit.py builds — does not.
    cur = await conn.execute(
        "SELECT audit_metadata FROM lane_challenges WHERE status='active'"
    )
    row = await cur.fetchone()
    audit = json.loads(row[0])
    assert "generator_run_id" in audit
    assert audit["generator_run_id"] == "gen_xyz"

    # Now build the public projection the same way submit.py does and
    # verify none of the private fields leak.
    from cathedral.publisher.submit import _public_challenge_view

    active = await source.get_active_for_tier("synthetic_boolean_v1", 1)
    public = _public_challenge_view(active)
    assert "generator_run_id" not in public
    assert "cnf_url" not in public
    assert "cnf_path" not in public
