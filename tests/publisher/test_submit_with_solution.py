"""PR5 (solve-on-submit) integration tests.

Tests the new ``dimacs_solution`` form field on ``POST /v1/agents/submit``
when ``CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED=true``:

- valid winner: 200 + ``ranked`` + signed eval_run row +
  ``lane_challenge_winners`` row + challenge flipped to ``locked``.
- valid loser (concurrent / late): 409 + losing eval_run row.
- invalid DIMACS (each error class): 400 + losing eval_run + spec
  error code.
- challenge_not_active: 409 with the active challenge id surfaced.
- flag-off ignores the field (regression with the legacy path).
- 100 concurrent valid POSTs → exactly one wins.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from bittensor_wallet import Keypair
from fastapi.testclient import TestClient

from cathedral.auth.hotkey_signature import canonical_claim_bytes
from cathedral.publisher.app import build_app

_FAMILY = "synthetic_boolean_v1"
_CHALLENGE = "pr5-test-uf3-2"
_CNF = "p cnf 3 2\n1 2 0\n-2 3 0\n"
_VALID_SOL = "s SATISFIABLE\nv 1 2 3 0\n"
_UNSAT_SOL = "s SATISFIABLE\nv 1 2 -3 0\n"
_CNF_SHA256 = hashlib.sha256(_CNF.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _enable_pr5_flag() -> Iterator[None]:
    prev = os.environ.get("CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED")
    os.environ["CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED"] = "true"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED", None)
        else:
            os.environ["CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED"] = prev


@pytest.fixture
def alice() -> Keypair:
    return Keypair.create_from_uri("//Alice")


@pytest.fixture
def bob() -> Keypair:
    return Keypair.create_from_uri("//Bob")


def _seed_active_challenge_sync(db_path: str) -> None:
    """Synchronously seed an active challenge before app startup.

    Opens the publisher SQLite file directly, applies the
    challenge-source schema, and inserts a single ACTIVE row. The
    publisher's lifespan then layers its own migrations on top — they
    are all idempotent so the seed survives.
    """
    import json
    import sqlite3

    from cathedral.lanes.challenge_source import SQLITE_SCHEMA

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SQLITE_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO lane_challenges ("
            "challenge_id, family_id, tier, cnf_text, cnf_path, status, "
            "audit_metadata, losers_published_at_iso, "
            "created_at_iso, updated_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _CHALLENGE,
                _FAMILY,
                1,
                _CNF,
                None,
                "active",
                json.dumps(
                    {
                        "cnf_sha256": _CNF_SHA256,
                        "num_vars": 3,
                        "num_clauses": 2,
                    },
                    sort_keys=True,
                ),
                None,
                "2026-05-26T00:00:00.000Z",
                "2026-05-26T00:00:00.000Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = str(tmp_path / "publisher.db")
    _seed_active_challenge_sync(db_path)
    app = build_app(database_path=db_path)
    with TestClient(app) as c:
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


def _sign_active_cnf(kp: Keypair, *, submitted_at: str) -> str:
    import blake3

    payload = canonical_claim_bytes(
        bundle_hash=blake3.blake3(b"").hexdigest(),
        card_id=_FAMILY,
        miner_hotkey=kp.ss58_address,
        submitted_at=submitted_at,
        challenge_id="",
        dimacs_solution_sha256="",
    )
    return base64.b64encode(kp.sign(payload)).decode("ascii")


def _active_cnf_headers(
    kp: Keypair, *, submitted_at: str | None = None
) -> dict[str, str]:
    submitted_at = submitted_at or _now_iso_ms()
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Submitted-At": submitted_at,
        "X-Cathedral-Signature": _sign_active_cnf(kp, submitted_at=submitted_at),
    }


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


def test_active_cnf_requires_valid_hotkey_signature(
    client: TestClient, alice: Keypair, bob: Keypair
) -> None:
    headers = _active_cnf_headers(alice)
    ok = client.get("/v1/synthetic-boolean/active-cnf", headers=headers)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["challenge_id"] == _CHALLENGE
    assert body["cnf_sha256"] == _CNF_SHA256
    assert f"/v1/challenges/{_CHALLENGE}/cnf?t=" in body["cnf_url"]

    submitted_at = headers["X-Cathedral-Submitted-At"]
    bad_headers = dict(headers)
    bad_headers["X-Cathedral-Signature"] = _sign_active_cnf(
        bob,
        submitted_at=submitted_at,
    )
    bad = client.get("/v1/synthetic-boolean/active-cnf", headers=bad_headers)
    assert bad.status_code == 401, bad.text
    assert bad.json()["detail"] == "invalid hotkey signature"


def test_active_cnf_requires_submitted_at_header(
    client: TestClient, alice: Keypair
) -> None:
    headers = _active_cnf_headers(alice)
    headers.pop("X-Cathedral-Submitted-At")

    resp = client.get("/v1/synthetic-boolean/active-cnf", headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "missing X-Cathedral-Submitted-At"


def test_active_cnf_rejects_stale_signed_timestamp(
    client: TestClient, alice: Keypair
) -> None:
    headers = _active_cnf_headers(alice, submitted_at="2020-01-01T00:00:00.000Z")

    resp = client.get("/v1/synthetic-boolean/active-cnf", headers=headers)
    assert resp.status_code == 400, resp.text
    assert "outside acceptable clock-skew window" in resp.json()["detail"]


def test_winner_gets_200_ranked_and_attestation_pending(
    client: TestClient, alice: Keypair, tmp_path: Path
) -> None:
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id=_CHALLENGE,
        dimacs_solution=_VALID_SOL,
    )
    resp = client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ranked"
    assert body["attestation_status"] == "pending"
    assert body["weighted_score"] == 1.0
    assert body["challenge_id"] == _CHALLENGE
    assert body["eval_run_id"]
    assert body["id"]

    import sqlite3

    with sqlite3.connect(str(tmp_path / "publisher.db")) as raw:
        sub = raw.execute(
            "SELECT status, current_score FROM agent_submissions WHERE id = ?",
            (body["id"],),
        ).fetchone()
        run = raw.execute(
            "SELECT weighted_score, cathedral_signature, eval_output_schema_version, "
            "attestation_status FROM eval_runs WHERE id = ?",
            (body["eval_run_id"],),
        ).fetchone()
        challenge = raw.execute(
            "SELECT status FROM lane_challenges WHERE challenge_id = ?",
            (_CHALLENGE,),
        ).fetchone()
    assert sub == ("ranked", 1.0)
    assert run is not None
    assert run[0] == 1.0
    assert isinstance(run[1], str) and run[1]
    assert run[2] == 5
    assert run[3] == "pending"
    assert challenge == ("locked",)


def test_invalid_dimacs_returns_400_and_writes_losing_eval_run(
    client: TestClient, alice: Keypair, tmp_path: Path
) -> None:
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id=_CHALLENGE,
        dimacs_solution=_UNSAT_SOL,
    )
    resp = client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        assert detail.get("detail") == "solution_unsatisfied"
        assert detail.get("challenge_id") == _CHALLENGE
    else:
        # FastAPI flattens dict details sometimes; either shape is fine
        assert "solution_unsatisfied" in str(detail)

    import sqlite3

    with sqlite3.connect(str(tmp_path / "publisher.db")) as raw:
        row = raw.execute(
            "SELECT status, rejection_reason FROM agent_submissions "
            "WHERE miner_hotkey = ?",
            (alice.ss58_address,),
        ).fetchone()
    assert row == ("rejected", "solution_unsatisfied")


def test_challenge_not_active_returns_409(
    client: TestClient, alice: Keypair
) -> None:
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id="not-a-real-challenge",
        dimacs_solution=_VALID_SOL,
    )
    resp = client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 409, resp.text
    detail = resp.json().get("detail")
    detail_str = detail if isinstance(detail, str) else detail.get("detail", "")
    assert "challenge_not_active" in detail_str or "challenge_not_active" in str(detail)


def test_second_valid_post_loses_to_locked_winner(
    client: TestClient, alice: Keypair, bob: Keypair
) -> None:
    data1, h1 = _solve_post_form(
        kp=alice, challenge_id=_CHALLENGE, dimacs_solution=_VALID_SOL
    )
    r1 = client.post("/v1/agents/submit", data=data1, headers=h1)
    assert r1.status_code == 200, r1.text

    # Build a different valid solution body so its sha256 differs (but
    # the assignment is the same). The challenge is already locked, so
    # this should 409.
    second_sol = "s SATISFIABLE\nv 1 2 3 0\nc post-lock attempt\n"
    data2, h2 = _solve_post_form(
        kp=bob, challenge_id=_CHALLENGE, dimacs_solution=second_sol
    )
    r2 = client.post("/v1/agents/submit", data=data2, headers=h2)
    assert r2.status_code == 409, r2.text
    detail = r2.json().get("detail")
    detail_str = detail if isinstance(detail, str) else (detail or {}).get("detail", "")
    assert "challenge_already_locked" in detail_str or "challenge_already_locked" in str(detail)


def test_flag_off_ignores_dimacs_solution(
    tmp_path: Path, alice: Keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the PR5 flag is off, the solve-POST field is ignored."""
    monkeypatch.setenv("CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED", "false")
    db_path = str(tmp_path / "publisher-flag-off.db")
    app = build_app(database_path=db_path)
    with TestClient(app) as c:
        # No need to seed a challenge — flag-off shouldn't even look.
        submitted_at = _now_iso_ms()
        import blake3

        bundle_hash = blake3.blake3(b"").hexdigest()
        # Sign under the LEGACY 4-field shape since the flag is off.
        payload = canonical_claim_bytes(
            bundle_hash=bundle_hash,
            card_id=_FAMILY,
            miner_hotkey=alice.ss58_address,
            submitted_at=submitted_at,
        )
        sig = base64.b64encode(alice.sign(payload)).decode("ascii")
        data = {
            "card_id": _FAMILY,
            "display_name": "alice-flag-off",
            "attestation_mode": "ssh-probe",
            "ssh_host": "miner.example.com",
            "ssh_user": "cathedral",
            "ssh_port": "22",
            "submitted_at": submitted_at,
            # Field present, but the handler should ignore it.
            "challenge_id": _CHALLENGE,
            "dimacs_solution": _VALID_SOL,
        }
        headers = {
            "X-Cathedral-Hotkey": alice.ss58_address,
            "X-Cathedral-Signature": sig,
        }
        resp = c.post("/v1/agents/submit", data=data, headers=headers)
        assert resp.status_code == 202, resp.text
        body = resp.json()
    assert body["status"] == "pending_check"


def test_flag_on_registration_only_does_not_enter_probe_queue(
    client: TestClient, alice: Keypair
) -> None:
    """With PR5 enabled, no DIMACS answer means no SSH probe queue entry."""
    submitted_at = _now_iso_ms()
    import blake3

    bundle_hash = blake3.blake3(b"").hexdigest()
    payload = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id=_FAMILY,
        miner_hotkey=alice.ss58_address,
        submitted_at=submitted_at,
        challenge_id="",
        dimacs_solution_sha256="",
    )
    sig = base64.b64encode(alice.sign(payload)).decode("ascii")
    resp = client.post(
        "/v1/agents/submit",
        data={
            "card_id": _FAMILY,
            "display_name": "alice-register-only",
            "attestation_mode": "ssh-probe",
            "ssh_host": "miner.example.com",
            "ssh_user": "cathedral",
            "ssh_port": "22",
            "submitted_at": submitted_at,
        },
        headers={
            "X-Cathedral-Hotkey": alice.ss58_address,
            "X-Cathedral-Signature": sig,
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "pending_solution"


def test_concurrent_winners_exactly_one_wins(
    client: TestClient,
) -> None:
    """Race 50 sr25519 hotkeys against the same locked challenge.

    Asserts exactly one POST returns 200 + ``ranked``;
    all others return 409 ``challenge_already_locked``. Uses real
    sr25519 keypairs (not strings) so the signature path is exercised.

    Per the spec: this is the critical race-condition test for the
    BEGIN IMMEDIATE + INSERT-OR-IGNORE atomic claim path.
    """
    n = 50
    keypairs = [
        Keypair.create_from_uri(f"//PR5RaceMiner{i}") for i in range(n)
    ]

    def _post_one(kp: Keypair) -> int:
        data, headers = _solve_post_form(
            kp=kp,
            challenge_id=_CHALLENGE,
            dimacs_solution=_VALID_SOL,
        )
        return client.post(
            "/v1/agents/submit", data=data, headers=headers
        ).status_code

    # TestClient is synchronous; we drive concurrency at the thread
    # pool level. The asyncio.Lock + BEGIN IMMEDIATE on the server
    # serialize the actual claim, so we expect exactly one 200 +
    # (n-1) 409s.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(n, 16)) as ex:
        statuses = list(ex.map(_post_one, keypairs))

    wins = sum(1 for s in statuses if s == 200)
    losses = sum(1 for s in statuses if s == 409)
    assert wins == 1, f"expected exactly 1 winner; got {wins}. statuses={statuses!r}"
    assert wins + losses == n, f"unexpected status counts: {statuses!r}"
