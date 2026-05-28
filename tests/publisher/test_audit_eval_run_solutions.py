"""Tests for the eval_run_solutions sidecar + audit endpoint (issue #242).

Covers the six acceptance scenarios from the issue:

1. Round-trip: POST a valid SAT solve, GET via the audit route, assert
   byte-identical DIMACS body.
2. Auth required: GET without bearer → 401; GET with wrong bearer → 401.
3. Audit-not-configured: CATHEDRAL_AUDIT_TOKEN unset → 503 even with a
   bearer.
4. 404: unknown eval_run_id → 404.
5. CASCADE: deleting the eval_runs row deletes the sidecar.
6. Public surfaces don't leak: ``/v1/leaderboard/recent`` and the
   (currently 410) ``/v1/agents/{agent_id}`` response do NOT include the
   DIMACS body — neither as a key in the response dict nor as a value
   substring.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from bittensor_wallet import Keypair
from fastapi.testclient import TestClient

from cathedral.auth.hotkey_signature import canonical_claim_bytes
from cathedral.publisher.app import build_app
from cathedral.publisher.audit import AUDIT_TOKEN_ENV

_FAMILY = "synthetic_boolean_v1"
_CHALLENGE = "audit242-uf3-1"
_CNF = "p cnf 3 2\n1 2 0\n-2 3 0\n"
_VALID_SOL = "s SATISFIABLE\nv 1 2 3 0\n"
_AUDIT_TOKEN = "test-audit-token-issue-242"


@pytest.fixture(autouse=True)
def _enable_pr5_flag() -> Iterator[None]:
    prev = os.environ.get("CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED")
    prev_feed = os.environ.get("CATHEDRAL_TASK_FAMILY_FEED_ENABLED")
    os.environ["CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED"] = "true"
    # The leak-test queries /v1/leaderboard/recent and needs schema-5
    # SAT rows surfaced; the publisher gates that emission on
    # CATHEDRAL_TASK_FAMILY_FEED_ENABLED. Enable here so the leak scan
    # actually has a row to inspect (a leak we'd otherwise never see).
    os.environ["CATHEDRAL_TASK_FAMILY_FEED_ENABLED"] = "true"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED", None)
        else:
            os.environ["CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED"] = prev
        if prev_feed is None:
            os.environ.pop("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", None)
        else:
            os.environ["CATHEDRAL_TASK_FAMILY_FEED_ENABLED"] = prev_feed


@pytest.fixture
def _audit_token_set() -> Iterator[str]:
    """Set CATHEDRAL_AUDIT_TOKEN for the duration of the test."""
    prev = os.environ.get(AUDIT_TOKEN_ENV)
    os.environ[AUDIT_TOKEN_ENV] = _AUDIT_TOKEN
    try:
        yield _AUDIT_TOKEN
    finally:
        if prev is None:
            os.environ.pop(AUDIT_TOKEN_ENV, None)
        else:
            os.environ[AUDIT_TOKEN_ENV] = prev


@pytest.fixture
def _audit_token_unset() -> Iterator[None]:
    """Force CATHEDRAL_AUDIT_TOKEN unset for the duration of the test."""
    prev = os.environ.get(AUDIT_TOKEN_ENV)
    os.environ.pop(AUDIT_TOKEN_ENV, None)
    try:
        yield
    finally:
        if prev is not None:
            os.environ[AUDIT_TOKEN_ENV] = prev


@pytest.fixture
def alice() -> Keypair:
    return Keypair.create_from_uri("//Alice")


def _insert_challenge_sync(
    conn: Any,
    *,
    challenge_id: str,
    tier: int,
    cnf_text: str,
) -> None:
    cnf_sha256 = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    header = next(line for line in cnf_text.splitlines() if line.startswith("p cnf "))
    _, _, num_vars, num_clauses = header.split()
    conn.execute(
        "INSERT OR REPLACE INTO lane_challenges ("
        "challenge_id, family_id, tier, cnf_text, cnf_path, status, "
        "audit_metadata, losers_published_at_iso, "
        "created_at_iso, updated_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            challenge_id,
            _FAMILY,
            tier,
            cnf_text,
            None,
            "active",
            json.dumps(
                {
                    "cnf_sha256": cnf_sha256,
                    "num_vars": int(num_vars),
                    "num_clauses": int(num_clauses),
                },
                sort_keys=True,
            ),
            None,
            "2026-05-26T00:00:00.000Z",
            "2026-05-26T00:00:00.000Z",
        ),
    )


def _seed_active_challenge_sync(db_path: str) -> None:
    from cathedral.lanes.challenge_source import SQLITE_SCHEMA

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SQLITE_SCHEMA)
        _insert_challenge_sync(conn, challenge_id=_CHALLENGE, tier=1, cnf_text=_CNF)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = str(tmp_path / "publisher.db")
    _seed_active_challenge_sync(db_path)
    app = build_app(database_path=db_path)
    with TestClient(app) as c:
        # Stash db_path on the client so tests can poke the raw sqlite
        # file without having to thread it through every signature.
        c._cathedral_db_path = db_path  # type: ignore[attr-defined]
        yield c


def _now_iso_ms() -> str:
    from datetime import UTC, datetime

    dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sign_solve(
    kp: Keypair,
    *,
    bundle_hash: str,
    submitted_at: str,
    challenge_id: str,
    dimacs_solution: str,
) -> str:
    sol_sha = hashlib.sha256(dimacs_solution.encode("utf-8")).hexdigest()
    payload = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id=_FAMILY,
        miner_hotkey=kp.ss58_address,
        submitted_at=submitted_at,
        challenge_id=challenge_id,
        dimacs_solution_sha256=sol_sha,
    )
    return base64.b64encode(kp.sign(payload)).decode("ascii")


def _solve_post_form(
    *,
    kp: Keypair,
    challenge_id: str,
    dimacs_solution: str,
    submitted_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    import blake3

    submitted_at = submitted_at or _now_iso_ms()
    bundle_hash = blake3.blake3(b"").hexdigest()
    sig = _sign_solve(
        kp,
        bundle_hash=bundle_hash,
        submitted_at=submitted_at,
        challenge_id=challenge_id,
        dimacs_solution=dimacs_solution,
    )
    data = {
        "card_id": _FAMILY,
        "display_name": kp.ss58_address[:10],
        "attestation_mode": "ssh-probe",
        "ssh_host": "miner.example.com",
        "ssh_user": "cathedral",
        "ssh_port": "22",
        "submitted_at": submitted_at,
        "challenge_id": challenge_id,
        "dimacs_solution": dimacs_solution,
    }
    headers = {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
    }
    return data, headers


def _submit_winning_solve(client: TestClient, kp: Keypair) -> str:
    """POST a valid solve, assert 200, return the resulting eval_run_id."""
    data, headers = _solve_post_form(
        kp=kp,
        challenge_id=_CHALLENGE,
        dimacs_solution=_VALID_SOL,
    )
    resp = client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["eval_run_id"]


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_audit_returns_byte_identical_body(
    client: TestClient,
    alice: Keypair,
    _audit_token_set: str,
) -> None:
    eval_run_id = _submit_winning_solve(client, alice)

    resp = client.get(
        f"/v1/audit/eval-runs/{eval_run_id}/solution",
        headers={"Authorization": f"Bearer {_audit_token_set}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["eval_run_id"] == eval_run_id
    # Byte-for-byte equality on the DIMACS body — no canonicalisation,
    # no trimming, no normalisation. This is the whole point of issue
    # #242: the audit needs the exact bytes the miner sent.
    assert body["dimacs_solution"] == _VALID_SOL
    # body_sha256 mirrors miner_solution_sha256 so an auditor can
    # independently confirm the body matches the signed hash.
    expected_sha = hashlib.sha256(_VALID_SOL.encode("utf-8")).hexdigest()
    assert body["body_sha256"] == expected_sha
    assert body["stored_at"]


# ---------------------------------------------------------------------------
# 2. Auth required
# ---------------------------------------------------------------------------


def test_audit_without_bearer_returns_401(
    client: TestClient,
    alice: Keypair,
    _audit_token_set: str,
) -> None:
    eval_run_id = _submit_winning_solve(client, alice)
    resp = client.get(f"/v1/audit/eval-runs/{eval_run_id}/solution")
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "unauthorized"


def test_audit_with_wrong_bearer_returns_401(
    client: TestClient,
    alice: Keypair,
    _audit_token_set: str,
) -> None:
    eval_run_id = _submit_winning_solve(client, alice)
    resp = client.get(
        f"/v1/audit/eval-runs/{eval_run_id}/solution",
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "unauthorized"


def test_audit_with_non_bearer_scheme_returns_401(
    client: TestClient,
    alice: Keypair,
    _audit_token_set: str,
) -> None:
    """An Authorization header in the wrong scheme is rejected like missing."""
    eval_run_id = _submit_winning_solve(client, alice)
    resp = client.get(
        f"/v1/audit/eval-runs/{eval_run_id}/solution",
        headers={"Authorization": f"Basic {_audit_token_set}"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 3. Audit not configured
# ---------------------------------------------------------------------------


def test_audit_not_configured_returns_503_even_with_bearer(
    client: TestClient,
    alice: Keypair,
    _audit_token_unset: None,
) -> None:
    # Submit the solve while the env var IS set so the write goes through
    # (the write path doesn't depend on AUDIT_TOKEN_ENV — it always
    # writes the sidecar). But the read should still 503 because the
    # operator has not opted-in to the audit surface.
    eval_run_id = _submit_winning_solve(client, alice)

    # Even WITH a plausible bearer, an unconfigured publisher must refuse.
    resp = client.get(
        f"/v1/audit/eval-runs/{eval_run_id}/solution",
        headers={"Authorization": "Bearer anything-goes-here"},
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "audit not configured"


# ---------------------------------------------------------------------------
# 4. Unknown eval_run_id → 404
# ---------------------------------------------------------------------------


def test_audit_unknown_eval_run_id_returns_404(
    client: TestClient,
    _audit_token_set: str,
) -> None:
    resp = client.get(
        "/v1/audit/eval-runs/no-such-id-exists/solution",
        headers={"Authorization": f"Bearer {_audit_token_set}"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "not found"


# ---------------------------------------------------------------------------
# 5. CASCADE
# ---------------------------------------------------------------------------


def test_eval_run_solutions_cascade_on_eval_runs_delete(
    client: TestClient,
    alice: Keypair,
    _audit_token_set: str,
) -> None:
    """Deleting the parent eval_runs row must cascade-drop the sidecar."""
    eval_run_id = _submit_winning_solve(client, alice)

    db_path = client._cathedral_db_path  # type: ignore[attr-defined]
    # Verify both rows exist.
    with sqlite3.connect(db_path) as raw:
        raw.execute("PRAGMA foreign_keys = ON")
        parent = raw.execute(
            "SELECT id FROM eval_runs WHERE id = ?", (eval_run_id,)
        ).fetchone()
        child = raw.execute(
            "SELECT eval_run_id FROM eval_run_solutions WHERE eval_run_id = ?",
            (eval_run_id,),
        ).fetchone()
        assert parent is not None
        assert child is not None

        # Now delete the parent — sidecar should disappear.
        raw.execute("DELETE FROM eval_runs WHERE id = ?", (eval_run_id,))
        raw.commit()

        child_after = raw.execute(
            "SELECT eval_run_id FROM eval_run_solutions WHERE eval_run_id = ?",
            (eval_run_id,),
        ).fetchone()
        assert child_after is None


# ---------------------------------------------------------------------------
# 6. Public surfaces don't leak the body
# ---------------------------------------------------------------------------


def _flatten_for_substring_scan(obj: Any) -> list[str]:
    """Walk a JSON tree and yield every string value it contains.

    Used by the no-leak assertion so we catch a body that snuck into the
    response under any key (or nested inside a list/dict), not only the
    obvious ``dimacs_solution`` top-level key.
    """
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten_for_substring_scan(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_flatten_for_substring_scan(item))
    return out


def _all_keys(obj: Any) -> set[str]:
    """Return every dict key appearing anywhere in the JSON tree."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item)
    return keys


def test_public_surfaces_do_not_leak_dimacs_solution_body(
    client: TestClient,
    alice: Keypair,
    _audit_token_set: str,
) -> None:
    """``/v1/leaderboard/recent`` and ``/v1/agents/{id}`` MUST NOT leak.

    The acceptance criteria explicitly require that public read paths
    expose only the hash, not the body. The body is private + bearer-
    gated via the audit route. Use a unique body so a substring scan
    has no false positives from default fixture text.
    """
    # Use a custom DIMACS body containing a unique marker so the
    # substring scan below cannot collide with any incidental text in
    # the leaderboard response (timestamps, hashes, etc.). The marker
    # rides on a DIMACS comment line ('c ...') which the verifier
    # ignores, so the body is still a valid satisfying assignment for
    # the seeded CNF (1 2 3) but carries a substring no public surface
    # would ever otherwise emit.
    unique_marker = "unique-audit-marker-242-leak-canary"
    leak_canary = f"c {unique_marker}\ns SATISFIABLE\nv 1 2 3 0\n"
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id=_CHALLENGE,
        dimacs_solution=leak_canary,
    )
    post = client.post("/v1/agents/submit", data=data, headers=headers)
    assert post.status_code == 200, post.text
    eval_run_id = post.json()["eval_run_id"]

    # Confirm the audit route DOES return it (sanity: write happened).
    audit = client.get(
        f"/v1/audit/eval-runs/{eval_run_id}/solution",
        headers={"Authorization": f"Bearer {_audit_token_set}"},
    )
    assert audit.status_code == 200, audit.text
    assert audit.json()["dimacs_solution"] == leak_canary

    # Public leaderboard. since=epoch so the cursor matches our just-
    # written row.
    lb = client.get(
        "/v1/leaderboard/recent?since=2020-01-01T00:00:00.000Z&limit=50"
    )
    assert lb.status_code == 200, lb.text
    lb_body = lb.json()
    assert "items" in lb_body
    # Spot-check: our row is in there (so the leak-check is meaningful).
    eval_run_ids_in_feed = {
        item.get("id") or item.get("eval_run_id") for item in lb_body["items"]
    }
    assert eval_run_id in eval_run_ids_in_feed, lb_body

    # No "dimacs_solution" key anywhere in the tree.
    leaked_keys = _all_keys(lb_body)
    assert "dimacs_solution" not in leaked_keys, (
        f"public leaderboard exposed dimacs_solution key: {leaked_keys!r}"
    )
    # No occurrence of the body bytes as a substring of any value.
    for value in _flatten_for_substring_scan(lb_body):
        assert unique_marker not in value, (
            "public leaderboard leaked DIMACS body bytes via a string value"
        )

    # /v1/agents/{agent_id} is the dead-route 410 stub. Confirm the
    # body still doesn't appear there. (If the route ever revives, this
    # test pins the no-leak guarantee for whatever replaces it.)
    submission_id = post.json()["id"]
    agent = client.get(f"/v1/agents/{submission_id}")
    # 410 (dead-route) or whatever future shape: as long as the body
    # is not in the response, we're good.
    agent_payload: dict[str, Any]
    try:
        agent_payload = agent.json()
    except ValueError:
        agent_payload = {}
    assert "dimacs_solution" not in _all_keys(agent_payload)
    for value in _flatten_for_substring_scan(agent_payload):
        assert unique_marker not in value, (
            "/v1/agents/{id} leaked DIMACS body bytes via a string value"
        )
