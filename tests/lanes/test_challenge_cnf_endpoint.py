"""Endpoint tests for the public CNF route.

These tests exercise ``cathedral.publisher.challenge_cnf`` against a
minimal FastAPI app that mounts only the CNF router. The point is to
keep the assertions sharp on the cardinal-sin guarantees -- one route,
one threat model -- without dragging in the full publisher lifespan
(Hippius, signer, eval loop).

Coverage:

* Token absent  -> 404 with the canonical detail string
* Token present, no ?t=  -> 404
* Token present, wrong ?t=  -> 404
* Token present, correct ?t=, status=active  -> 200 with CNF body
* Token present, correct ?t=, status=locked within grace  -> 200
* Token present, correct ?t=, status=locked past grace  -> 404
* Token present, correct ?t=, status=pending  -> 404
* Unknown challenge_id  -> 404
* All 404 responses share the same body shape
"""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cathedral.eval.orchestrator import EvalOrchestrator
from cathedral.eval.polaris_runner import StubPolarisRunner
from cathedral.eval.scoring_pipeline import EvalSigner
from cathedral.lanes.challenge_lock import (
    SQLITE_SCHEMA as CHALLENGE_LOCK_SCHEMA,
)
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    CHALLENGE_STATUS_PENDING,
    ChallengeRecord,
    SqliteChallengeSource,
    SqliteFetchTokenStore,
)
from cathedral.lanes.challenge_source import (
    SQLITE_SCHEMA as CHALLENGE_SOURCE_SCHEMA,
)
from cathedral.lanes.publisher import score_and_sign_task_family_stdout
from cathedral.lanes.synthetic_boolean_v1 import SyntheticBooleanV1, problem_from_challenge_record
from cathedral.publisher import challenge_cnf as challenge_cnf_module
from cathedral.publisher.challenge_cnf import router as challenge_cnf_router
from cathedral.storage.hippius import StubHippiusClient
from cathedral.validator.db import connect

CNF_BODY = "p cnf 3 2\n1 -2 0\n2 3 0\n"
EXPECTED_SHA = hashlib.sha256(CNF_BODY.encode("utf-8")).hexdigest()
CHALLENGE_ID = "sat-endpoint-test-001"
FAKE_TOKEN = "test-token-xyz123"
NOT_FOUND_BODY = {"detail": "challenge_not_found"}


def _ms_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


@pytest.mark.parametrize("challenge_id", ["sat/bad", "sat?bad", "sat#bad", "sat bad"])
def test_problem_from_challenge_record_rejects_unsafe_cnf_url_challenge_ids(
    challenge_id: str,
) -> None:
    record = ChallengeRecord(
        challenge_id=challenge_id,
        family_id="synthetic_boolean_v1",
        tier=0,
        cnf_text=CNF_BODY,
        status=CHALLENGE_STATUS_ACTIVE,
        audit_metadata={"source": "unit"},
    )

    with pytest.raises(ValueError, match="challenge_id must be"):
        problem_from_challenge_record(
            record,
            public_base_url="https://api.cathedral.test/",
            fetch_token="token",
        )


def test_problem_from_challenge_record_encodes_cnf_url_fetch_token() -> None:
    record = ChallengeRecord(
        challenge_id="sat-safe_001~x",
        family_id="synthetic_boolean_v1",
        tier=0,
        cnf_text=CNF_BODY,
        status=CHALLENGE_STATUS_ACTIVE,
        audit_metadata={"source": "unit"},
    )

    problem, _hidden = problem_from_challenge_record(
        record,
        public_base_url="https://api.cathedral.test/",
        fetch_token="tok?x#y",
    )

    assert problem.public_input["cnf_url"] == (
        "https://api.cathedral.test/v1/challenges/sat-safe_001~x/cnf?t=tok%3Fx%23y"
    )


@pytest_asyncio.fixture
async def wired_app(tmp_path: Any) -> Any:
    """Build a minimal FastAPI app with the CNF router mounted and the
    challenge + token stores wired onto app.state. Each test gets a
    fresh sqlite file via tmp_path."""
    conn = await connect(str(tmp_path / "publisher.db"))
    await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
    await conn.executescript(CHALLENGE_LOCK_SCHEMA)
    await conn.commit()
    source = SqliteChallengeSource(conn)
    tokens = SqliteFetchTokenStore(conn)
    app = FastAPI()
    app.include_router(challenge_cnf_router)
    app.state.task_family_challenge_source = source
    app.state.task_family_fetch_token_store = tokens
    try:
        yield {
            "app": app,
            "conn": conn,
            "source": source,
            "tokens": tokens,
        }
    finally:
        await conn.close()


async def _seed_active_with_token(
    wired: dict[str, Any],
    *,
    challenge_id: str = CHALLENGE_ID,
    cnf_text: str = CNF_BODY,
    status: str = CHALLENGE_STATUS_ACTIVE,
    minted_at: datetime | None = None,
    time_limit_secs: int = 60,
    fetch_token: str = FAKE_TOKEN,
    now_iso: str | None = None,
) -> None:
    now = minted_at or datetime.now(UTC)
    await wired["source"].upsert(
        ChallengeRecord(
            challenge_id=challenge_id,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text=cnf_text,
            status=status,
            audit_metadata={"source": "endpoint-test"},
        ),
        now_iso=now_iso or _ms_iso(now),
    )
    await wired["tokens"].mint_if_absent(
        challenge_id,
        fetch_token=fetch_token,
        minted_at_iso=_ms_iso(now),
        announced_time_limit_secs=time_limit_secs,
    )


@pytest.mark.asyncio
async def test_missing_token_row_returns_404(wired_app: dict[str, Any]) -> None:
    # Challenge row exists (status=active) but no token has been minted.
    # This is the early-active case: announce hasn't happened.
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text=CNF_BODY,
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={},
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY


@pytest.mark.asyncio
async def test_unknown_challenge_returns_404(wired_app: dict[str, Any]) -> None:
    client = TestClient(wired_app["app"])
    r = client.get("/v1/challenges/nonexistent-id/cnf", params={"t": "anything"})
    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY


@pytest.mark.asyncio
async def test_missing_query_param_returns_404(wired_app: dict[str, Any]) -> None:
    await _seed_active_with_token(wired_app)
    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf")
    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY


@pytest.mark.asyncio
async def test_wrong_token_returns_404(wired_app: dict[str, Any]) -> None:
    await _seed_active_with_token(wired_app)
    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": "not-the-token"})
    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY


@pytest.mark.asyncio
async def test_active_with_correct_token_returns_cnf(wired_app: dict[str, Any]) -> None:
    await _seed_active_with_token(wired_app)
    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    assert r.status_code == 200
    assert r.text == CNF_BODY
    assert hashlib.sha256(r.text.encode("utf-8")).hexdigest() == EXPECTED_SHA
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_cnf_fetch_ip_rate_limit(
    wired_app: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_CNF_FETCH_IP_LIMIT_PER_MIN", "1")
    await _seed_active_with_token(wired_app)
    client = TestClient(wired_app["app"])

    first = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    second = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert second.json()["detail"].startswith("rate limited:")


@pytest.mark.asyncio
async def test_active_file_backed_with_correct_token_returns_cnf(
    wired_app: dict[str, Any],
    tmp_path: Any,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(CNF_BODY, encoding="utf-8")
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    await wired_app["tokens"].mint_if_absent(
        CHALLENGE_ID,
        fetch_token=FAKE_TOKEN,
        minted_at_iso=_ms_iso(datetime.now(UTC)),
        announced_time_limit_secs=60,
    )

    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})

    assert r.status_code == 200
    assert r.text == CNF_BODY
    assert hashlib.sha256(r.content).hexdigest() == EXPECTED_SHA


@pytest.mark.asyncio
async def test_active_file_backed_snapshot_cache_runs_off_event_loop_once(
    wired_app: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(CNF_BODY, encoding="utf-8")
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    await wired_app["tokens"].mint_if_absent(
        CHALLENGE_ID,
        fetch_token=FAKE_TOKEN,
        minted_at_iso=_ms_iso(datetime.now(UTC)),
        announced_time_limit_secs=60,
    )

    original_to_thread = challenge_cnf_module.asyncio.to_thread
    offloaded: list[Any] = []

    async def spy_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(func)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(challenge_cnf_module.asyncio, "to_thread", spy_to_thread)

    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    r_again = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})

    assert r.status_code == 200
    assert r.text == CNF_BODY
    assert r_again.status_code == 200
    assert r_again.text == CNF_BODY
    assert offloaded == [challenge_cnf_module._materialize_verified_cnf_snapshot]


@pytest.mark.asyncio
async def test_active_file_backed_snapshot_cache_uses_configured_root(
    wired_app: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    cache_root = tmp_path / "operator-cache"
    cnf_path.write_text(CNF_BODY, encoding="utf-8")
    monkeypatch.setenv(challenge_cnf_module.CNF_SNAPSHOT_DIR_ENV, str(cache_root))
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    await wired_app["tokens"].mint_if_absent(
        CHALLENGE_ID,
        fetch_token=FAKE_TOKEN,
        minted_at_iso=_ms_iso(datetime.now(UTC)),
        announced_time_limit_secs=60,
    )

    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})

    assert r.status_code == 200
    assert r.text == CNF_BODY
    cache = wired_app["app"].state.cnf_snapshot_cache
    assert cache._root == cache_root
    snapshots = list(cache_root.glob("*.cnf"))
    assert snapshots
    assert stat.S_IMODE(cache_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshots[0].stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_file_backed_snapshot_cache_prunes_locked_past_grace(
    wired_app: dict[str, Any],
    tmp_path: Any,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(CNF_BODY, encoding="utf-8")
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    await wired_app["tokens"].mint_if_absent(
        CHALLENGE_ID,
        fetch_token=FAKE_TOKEN,
        minted_at_iso=_ms_iso(datetime.now(UTC)),
        announced_time_limit_secs=1,
    )

    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    assert r.status_code == 200

    cache = wired_app["app"].state.cnf_snapshot_cache
    snapshot_paths = [entry.path for entry in cache._by_digest.values()]
    assert snapshot_paths
    assert all(path.exists() for path in snapshot_paths)

    locked_at = datetime.now(UTC) - timedelta(seconds=120)
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_LOCKED,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(locked_at),
        overwrite_status=True,
    )

    r_past_grace = client.get(
        f"/v1/challenges/{CHALLENGE_ID}/cnf",
        params={"t": FAKE_TOKEN},
    )

    assert r_past_grace.status_code == 404
    assert r_past_grace.json() == NOT_FOUND_BODY
    assert cache._by_digest == {}
    assert all(not path.exists() for path in snapshot_paths)


@pytest.mark.asyncio
async def test_active_file_backed_streams_verified_open_file_after_path_replace(
    wired_app: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    replacement_path = tmp_path / "replacement.cnf"
    mutated_body = "p cnf 1 1\n-1 0\n"
    cnf_path.write_text(CNF_BODY, encoding="utf-8")
    replacement_path.write_text(mutated_body, encoding="utf-8")
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    await wired_app["tokens"].mint_if_absent(
        CHALLENGE_ID,
        fetch_token=FAKE_TOKEN,
        minted_at_iso=_ms_iso(datetime.now(UTC)),
        announced_time_limit_secs=60,
    )

    original_iter = challenge_cnf_module._iter_open_file

    def replace_path_before_stream(handle: Any):
        cnf_path.unlink()
        replacement_path.rename(cnf_path)
        yield from original_iter(handle)

    monkeypatch.setattr(
        challenge_cnf_module,
        "_iter_open_file",
        replace_path_before_stream,
    )

    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})

    assert r.status_code == 200
    assert r.text == CNF_BODY
    assert cnf_path.read_text(encoding="utf-8") == mutated_body


@pytest.mark.asyncio
async def test_active_file_backed_streams_verified_snapshot_after_in_place_write(
    wired_app: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    mutated_body = "p cnf 1 1\n-1 0\n"
    cnf_path.write_text(CNF_BODY, encoding="utf-8")
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    await wired_app["tokens"].mint_if_absent(
        CHALLENGE_ID,
        fetch_token=FAKE_TOKEN,
        minted_at_iso=_ms_iso(datetime.now(UTC)),
        announced_time_limit_secs=60,
    )

    original_iter = challenge_cnf_module._iter_open_file

    def mutate_source_before_stream(handle: Any):
        cnf_path.write_text(mutated_body, encoding="utf-8")
        yield from original_iter(handle)

    monkeypatch.setattr(
        challenge_cnf_module,
        "_iter_open_file",
        mutate_source_before_stream,
    )

    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})

    assert r.status_code == 200
    assert r.text == CNF_BODY
    assert cnf_path.read_text(encoding="utf-8") == mutated_body


@pytest.mark.asyncio
async def test_active_file_backed_rejects_changed_cnf_digest(
    wired_app: dict[str, Any],
    tmp_path: Any,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(CNF_BODY, encoding="utf-8")
    await wired_app["source"].upsert(
        ChallengeRecord(
            challenge_id=CHALLENGE_ID,
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="",
            cnf_path=str(cnf_path),
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={
                "source": "endpoint-test",
                "storage": "file",
                "cnf_sha256": EXPECTED_SHA,
            },
        ),
        now_iso=_ms_iso(datetime.now(UTC)),
    )
    await wired_app["tokens"].mint_if_absent(
        CHALLENGE_ID,
        fetch_token=FAKE_TOKEN,
        minted_at_iso=_ms_iso(datetime.now(UTC)),
        announced_time_limit_secs=60,
    )
    cnf_path.write_text("p cnf 1 1\n-1 0\n", encoding="utf-8")

    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})

    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY


def test_file_backed_snapshot_rejects_oversized_replacement_before_copy(
    tmp_path: Any,
) -> None:
    cnf_path = tmp_path / "active.cnf"
    cache_root = tmp_path / "snapshots"
    cnf_path.write_text(CNF_BODY + "c oversized replacement\n", encoding="utf-8")

    with pytest.raises(challenge_cnf_module._CnfFileOversizedError):
        challenge_cnf_module._materialize_verified_cnf_snapshot(
            cnf_path,
            expected_sha256=EXPECTED_SHA,
            cache_root=cache_root,
            max_bytes=len(CNF_BODY.encode("utf-8")),
        )

    assert not list(cache_root.iterdir())


@pytest.mark.asyncio
async def test_locked_within_grace_returns_cnf(wired_app: dict[str, Any]) -> None:
    # Lock happened 30 seconds ago, time limit is 60s, grace is +30s -> 90s
    # total. Still inside the window, so the URL stays live.
    locked_at = datetime.now(UTC) - timedelta(seconds=30)
    await _seed_active_with_token(
        wired_app,
        status=CHALLENGE_STATUS_LOCKED,
        time_limit_secs=60,
        now_iso=_ms_iso(locked_at),
        minted_at=locked_at - timedelta(seconds=10),
    )
    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    assert r.status_code == 200
    assert r.text == CNF_BODY


@pytest.mark.asyncio
async def test_locked_past_grace_returns_404(wired_app: dict[str, Any]) -> None:
    # Lock happened 200s ago, time_limit 60s + 30s grace = 90s window.
    # We're well past the grace window so the endpoint must close.
    locked_at = datetime.now(UTC) - timedelta(seconds=200)
    await _seed_active_with_token(
        wired_app,
        status=CHALLENGE_STATUS_LOCKED,
        time_limit_secs=60,
        now_iso=_ms_iso(locked_at),
        minted_at=locked_at - timedelta(seconds=10),
    )
    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY


@pytest.mark.asyncio
async def test_pending_status_returns_404(wired_app: dict[str, Any]) -> None:
    # Pending challenges should never serve, even with a (somehow minted)
    # valid token. Defense in depth: pending should only become fetchable
    # via the status flip to active, never by ad-hoc token mint.
    await _seed_active_with_token(wired_app, status=CHALLENGE_STATUS_PENDING)
    client = TestClient(wired_app["app"])
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY


@pytest.mark.asyncio
async def test_404_body_is_identical_across_miss_paths(wired_app: dict[str, Any]) -> None:
    """Every miss path should produce a byte-identical 404 body so the
    endpoint can't be turned into an existence oracle (probing which
    challenge IDs exist vs which are pending vs which are locked-past-
    grace must all look the same on the wire)."""
    # Seed one active + token row so we can hit a "wrong token" miss.
    await _seed_active_with_token(wired_app)
    client = TestClient(wired_app["app"])

    bodies = [
        client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": "wrong"}).text,
        client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf").text,
        client.get("/v1/challenges/unknown-id/cnf", params={"t": FAKE_TOKEN}).text,
        client.get("/v1/challenges/unknown-id/cnf").text,
    ]
    assert all(b == bodies[0] for b in bodies)


@pytest.mark.asyncio
async def test_route_works_under_canonical_prefix(wired_app: dict[str, Any]) -> None:
    """Mounting under /api/cathedral should produce the same behaviour."""
    # Re-mount the router under the canonical prefix on the same app so
    # we can hit it via both URLs in the same fixture.
    wired_app["app"].include_router(challenge_cnf_router, prefix="/api/cathedral")
    await _seed_active_with_token(wired_app)
    client = TestClient(wired_app["app"])
    r = client.get(
        f"/api/cathedral/v1/challenges/{CHALLENGE_ID}/cnf",
        params={"t": FAKE_TOKEN},
    )
    assert r.status_code == 200
    assert r.text == CNF_BODY


@pytest.mark.asyncio
async def test_announced_problem_fetches_scores_and_does_not_leak(
    wired_app: dict[str, Any],
) -> None:
    """Exercise the announce URL, endpoint fetch, hash check, and verifier."""
    cnf_body = "p cnf 2 2\n1 0\n2 0\n"
    record = ChallengeRecord(
        challenge_id="sat-local-e2e-001",
        family_id="synthetic_boolean_v1",
        tier=0,
        cnf_text=cnf_body,
        status=CHALLENGE_STATUS_ACTIVE,
        audit_metadata={"source": "local-e2e"},
    )
    await wired_app["source"].upsert(record)
    orchestrator = EvalOrchestrator(
        db=wired_app["conn"],
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        registry=object(),
        task_family_challenge_source=wired_app["source"],
        task_family_challenge_lock=None,
        task_family_fetch_token_store=wired_app["tokens"],
        public_base_url="http://testserver",
    )

    announced = await orchestrator._announce_synthetic_boolean_problem(
        record,
        log=structlog.get_logger("test"),
        family_id="synthetic_boolean_v1",
    )
    assert announced is not None
    problem, hidden = announced
    public_input = problem.public_input
    assert "cnf" not in public_input
    assert sorted(public_input) == ["cnf_sha256", "cnf_url", "format", "num_clauses", "num_vars"]

    client = TestClient(wired_app["app"], base_url="http://testserver")
    wrong_token = client.get(str(public_input["cnf_url"]).replace("t=", "t=wrong", 1))
    assert wrong_token.status_code == 404
    assert wrong_token.json() == NOT_FOUND_BODY

    fetched = client.get(str(public_input["cnf_url"]))
    assert fetched.status_code == 200
    assert fetched.text == cnf_body
    assert hashlib.sha256(fetched.text.encode("utf-8")).hexdigest() == public_input["cnf_sha256"]

    stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 2 0\\n"}\n```'
    signed = score_and_sign_task_family_stdout(
        lane=SyntheticBooleanV1(),
        problem=problem,
        hidden=hidden,
        submission_row={
            "id": "sub-local-e2e",
            "miner_hotkey": "5MinerLocal",
            "display_name": "Local Miner",
        },
        stdout=stdout,
        ran_at_iso="2026-05-19T18:00:00.000Z",
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        eval_run_id="run-local-e2e",
        epoch_salt="epoch_local:synthetic_boolean_v1",
    )
    assert signed.row["weighted_score"] == 1.0
    serialized = json.dumps(signed.row, sort_keys=True, default=str)
    for forbidden in (
        "p cnf",
        "cnf_url",
        "cnf_sha256",
        "fetch_token",
        "sat-local-e2e-001",
        "s SATISFIABLE",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_file_backed_problem_fetches_scores_and_does_not_leak(
    wired_app: dict[str, Any],
    tmp_path: Any,
) -> None:
    """Exercise the file-backed announce URL, endpoint fetch, and verifier."""
    cnf_body = "p cnf 2 2\n1 0\n2 0\n"
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(cnf_body, encoding="utf-8")
    record = ChallengeRecord(
        challenge_id="sat-file-e2e-001",
        family_id="synthetic_boolean_v1",
        tier=0,
        cnf_text="",
        cnf_path=str(cnf_path),
        status=CHALLENGE_STATUS_ACTIVE,
        audit_metadata={
            "source": "local-e2e",
            "storage": "file",
            "cnf_sha256": hashlib.sha256(cnf_body.encode("utf-8")).hexdigest(),
            "num_vars": 2,
            "num_clauses": 2,
        },
    )
    await wired_app["source"].upsert(record)
    orchestrator = EvalOrchestrator(
        db=wired_app["conn"],
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        registry=object(),
        task_family_challenge_source=wired_app["source"],
        task_family_challenge_lock=None,
        task_family_fetch_token_store=wired_app["tokens"],
        public_base_url="http://testserver",
    )

    announced = await orchestrator._announce_synthetic_boolean_problem(
        record,
        log=structlog.get_logger("test"),
        family_id="synthetic_boolean_v1",
    )
    assert announced is not None
    problem, hidden = announced
    public_input = problem.public_input
    assert "cnf" not in public_input
    assert public_input["cnf_sha256"] == record.audit_metadata["cnf_sha256"]
    assert "cnf" not in hidden.hidden_payload
    assert hidden.hidden_payload["cnf_path"] == str(cnf_path)

    client = TestClient(wired_app["app"], base_url="http://testserver")
    fetched = client.get(str(public_input["cnf_url"]))
    assert fetched.status_code == 200
    assert fetched.text == cnf_body

    stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 2 0\\n"}\n```'
    signed = score_and_sign_task_family_stdout(
        lane=SyntheticBooleanV1(),
        problem=problem,
        hidden=hidden,
        submission_row={
            "id": "sub-file-e2e",
            "miner_hotkey": "5MinerLocal",
            "display_name": "Local Miner",
        },
        stdout=stdout,
        ran_at_iso="2026-05-19T18:00:00.000Z",
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        eval_run_id="run-file-e2e",
        epoch_salt="epoch_local:synthetic_boolean_v1",
    )
    assert signed.row["weighted_score"] == 1.0
    serialized = json.dumps(signed.row, sort_keys=True, default=str)
    for forbidden in ("p cnf", "cnf_url", "fetch_token", str(cnf_path), "s SATISFIABLE"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_file_backed_verifier_rejects_changed_cnf_digest(
    wired_app: dict[str, Any],
    tmp_path: Any,
) -> None:
    original_cnf = "p cnf 1 1\n1 0\n"
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(original_cnf, encoding="utf-8")
    record = ChallengeRecord(
        challenge_id="sat-file-mutated-001",
        family_id="synthetic_boolean_v1",
        tier=0,
        cnf_text="",
        cnf_path=str(cnf_path),
        status=CHALLENGE_STATUS_ACTIVE,
        audit_metadata={
            "source": "local-e2e",
            "storage": "file",
            "cnf_sha256": hashlib.sha256(original_cnf.encode("utf-8")).hexdigest(),
            "num_vars": 1,
            "num_clauses": 1,
        },
    )
    await wired_app["source"].upsert(record)
    orchestrator = EvalOrchestrator(
        db=wired_app["conn"],
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        registry=object(),
        task_family_challenge_source=wired_app["source"],
        task_family_challenge_lock=None,
        task_family_fetch_token_store=wired_app["tokens"],
        public_base_url="http://testserver",
    )

    announced = await orchestrator._announce_synthetic_boolean_problem(
        record,
        log=structlog.get_logger("test"),
        family_id="synthetic_boolean_v1",
    )
    assert announced is not None
    problem, hidden = announced
    cnf_path.write_text("p cnf 1 1\n-1 0\n", encoding="utf-8")

    stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv -1 0\\n"}\n```'
    signed = score_and_sign_task_family_stdout(
        lane=SyntheticBooleanV1(),
        problem=problem,
        hidden=hidden,
        submission_row={
            "id": "sub-file-mutated",
            "miner_hotkey": "5MinerLocal",
            "display_name": "Local Miner",
        },
        stdout=stdout,
        ran_at_iso="2026-05-19T18:00:00.000Z",
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        eval_run_id="run-file-mutated",
        epoch_salt="epoch_local:synthetic_boolean_v1",
    )

    assert signed.row["weighted_score"] == 0.0
    assert signed.row["rejection_reason"] == "cnf_hash_mismatch"


@pytest.mark.asyncio
async def test_file_backed_verifier_rejects_replacement_above_seeded_size(
    wired_app: dict[str, Any],
    tmp_path: Any,
) -> None:
    original_cnf = "p cnf 1 1\n1 0\n"
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(original_cnf, encoding="utf-8")
    record = ChallengeRecord(
        challenge_id="sat-file-oversized-001",
        family_id="synthetic_boolean_v1",
        tier=0,
        cnf_text="",
        cnf_path=str(cnf_path),
        status=CHALLENGE_STATUS_ACTIVE,
        audit_metadata={
            "source": "local-e2e",
            "storage": "file",
            "cnf_sha256": hashlib.sha256(original_cnf.encode("utf-8")).hexdigest(),
            "cnf_bytes": len(original_cnf.encode("utf-8")),
            "num_vars": 1,
            "num_clauses": 1,
        },
    )
    await wired_app["source"].upsert(record)
    orchestrator = EvalOrchestrator(
        db=wired_app["conn"],
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        registry=object(),
        task_family_challenge_source=wired_app["source"],
        task_family_challenge_lock=None,
        task_family_fetch_token_store=wired_app["tokens"],
        public_base_url="http://testserver",
    )

    announced = await orchestrator._announce_synthetic_boolean_problem(
        record,
        log=structlog.get_logger("test"),
        family_id="synthetic_boolean_v1",
    )
    assert announced is not None
    problem, hidden = announced
    cnf_path.write_text("c replacement grew\np cnf 1 1\n-1 0\n", encoding="utf-8")

    stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv -1 0\\n"}\n```'
    signed = score_and_sign_task_family_stdout(
        lane=SyntheticBooleanV1(),
        problem=problem,
        hidden=hidden,
        submission_row={
            "id": "sub-file-oversized",
            "miner_hotkey": "5MinerLocal",
            "display_name": "Local Miner",
        },
        stdout=stdout,
        ran_at_iso="2026-05-19T18:00:00.000Z",
        signer=EvalSigner(Ed25519PrivateKey.generate()),
        eval_run_id="run-file-oversized",
        epoch_salt="epoch_local:synthetic_boolean_v1",
    )

    assert signed.row["weighted_score"] == 0.0
    assert signed.row["rejection_reason"] == "cnf_oversized"


@pytest.mark.asyncio
async def test_endpoint_disabled_when_stores_not_wired(tmp_path: Any) -> None:
    """If app.state lacks the stores (feed disabled / test app), every
    request must 404, never crash. Matches the cardinal-sin posture of
    treating absence the same as a real miss."""
    app = FastAPI()
    app.include_router(challenge_cnf_router)
    # Intentionally do NOT set app.state.task_family_challenge_source
    # or app.state.task_family_fetch_token_store.
    client = TestClient(app)
    r = client.get(f"/v1/challenges/{CHALLENGE_ID}/cnf", params={"t": FAKE_TOKEN})
    assert r.status_code == 404
    assert r.json() == NOT_FOUND_BODY
