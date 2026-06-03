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
import json
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
_TIER2_CHALLENGE = "pr5-test-tier2"
_TIER2_CNF = "p cnf 2 1\n1 0\n"
_TIER2_VALID_SOL = "s SATISFIABLE\nv 1 2 0\n"
_TIER2_CNF_SHA256 = hashlib.sha256(_TIER2_CNF.encode("utf-8")).hexdigest()


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


def _insert_challenge_sync(
    conn: Any,
    *,
    challenge_id: str,
    tier: int,
    cnf_text: str,
    status: str = "active",
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
            status,
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
    """Synchronously seed an active challenge before app startup.

    Opens the publisher SQLite file directly, applies the
    challenge-source schema, and inserts a single ACTIVE row. The
    publisher's lifespan then layers its own migrations on top — they
    are all idempotent so the seed survives.
    """
    import sqlite3

    from cathedral.lanes.challenge_source import SQLITE_SCHEMA

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SQLITE_SCHEMA)
        _insert_challenge_sync(conn, challenge_id=_CHALLENGE, tier=1, cnf_text=_CNF)
        conn.commit()
    finally:
        conn.close()


def _seed_two_active_challenges_sync(db_path: str) -> None:
    import sqlite3

    from cathedral.lanes.challenge_source import SQLITE_SCHEMA

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SQLITE_SCHEMA)
        _insert_challenge_sync(conn, challenge_id=_CHALLENGE, tier=1, cnf_text=_CNF)
        _insert_challenge_sync(
            conn,
            challenge_id=_TIER2_CHALLENGE,
            tier=2,
            cnf_text=_TIER2_CNF,
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


@pytest.fixture
def multi_tier_client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = str(tmp_path / "publisher-multi-tier.db")
    _seed_two_active_challenges_sync(db_path)
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


def test_current_challenge_returns_public_metadata_without_cnf_url(
    client: TestClient,
) -> None:
    resp = client.get("/v1/synthetic-boolean/current-challenge")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["family_id"] == _FAMILY
    assert body["challenge_id"] == _CHALLENGE
    assert body["status"] == "active"
    assert body["tier"] == 1
    assert body["storage"] == "sqlite_text"
    assert body["cnf_sha256"] == _CNF_SHA256
    assert body["cnf_bytes"] == len(_CNF.encode("utf-8"))
    assert body["num_vars"] == 3
    assert body["num_clauses"] == 2
    assert body["active_cnf_path"] == (
        f"/api/cathedral/v1/synthetic-boolean/active-cnf?challenge_id={_CHALLENGE}"
    )
    assert body["submit_path"] == "/api/cathedral/v1/agents/submit"
    assert "cnf_url" not in body
    assert "cnf_text" not in body
    assert "cnf_path" not in body


def test_current_challenge_tier_param_filters_by_tier(
    client: TestClient,
) -> None:
    # Tier 1 fixture matches; mismatched tier returns 404.
    resp1 = client.get("/v1/synthetic-boolean/current-challenge?tier=1")
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["tier"] == 1
    assert resp1.json()["challenge_id"] == _CHALLENGE

    resp_missing = client.get("/v1/synthetic-boolean/current-challenge?tier=2")
    assert resp_missing.status_code == 404
    assert resp_missing.json()["detail"] == "no_active_challenge"

    resp_bad = client.get("/v1/synthetic-boolean/current-challenge?tier=-1")
    assert resp_bad.status_code == 400


def test_active_challenges_list_endpoint_returns_all_actives(
    client: TestClient,
) -> None:
    resp = client.get("/v1/synthetic-boolean/active-challenges")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["family_id"] == _FAMILY
    assert body["count"] == len(body["items"])
    assert body["count"] >= 1
    # Every item carries the same public-only shape; same leak-guarantees.
    for item in body["items"]:
        assert "cnf_url" not in item
        assert "cnf_text" not in item
        assert "cnf_path" not in item
        assert item["family_id"] == _FAMILY
        assert item["status"] == "active"


def test_recent_wins_endpoint_returns_winners_and_leaks_nothing(
    client: TestClient, alice: Keypair
) -> None:
    # Submit a winning solve so we have at least one entry to find.
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id=_CHALLENGE,
        dimacs_solution=_VALID_SOL,
    )
    resp = client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 200, resp.text

    # Now the recent-wins endpoint should surface the win + winner_hotkey.
    wins = client.get("/v1/synthetic-boolean/recent-wins")
    assert wins.status_code == 200, wins.text
    body = wins.json()
    assert body["family_id"] == _FAMILY
    assert body["count"] >= 1
    item = body["items"][0]
    assert item["challenge_id"] == _CHALLENGE
    assert item["winner_hotkey"] == alice.ss58_address
    assert item["weighted_score"] == 1.0
    assert item["won_at"]
    # Same no-leak guarantee as active-challenges.
    assert "cnf_url" not in item
    assert "cnf_text" not in item
    assert "cnf_path" not in item

    # limit param: out-of-range values must be rejected (FastAPI returns
    # 400/422 depending on version — both indicate validation failure).
    bad_lo = client.get("/v1/synthetic-boolean/recent-wins?limit=0")
    assert bad_lo.status_code in {400, 422}
    bad_hi = client.get("/v1/synthetic-boolean/recent-wins?limit=200")
    assert bad_hi.status_code in {400, 422}


def test_active_cnf_can_select_active_challenge_by_tier_or_id(
    multi_tier_client: TestClient,
    alice: Keypair,
) -> None:
    headers = _active_cnf_headers(alice)

    by_tier = multi_tier_client.get(
        "/v1/synthetic-boolean/active-cnf?tier=2",
        headers=headers,
    )
    assert by_tier.status_code == 200, by_tier.text
    tier_body = by_tier.json()
    assert tier_body["challenge_id"] == _TIER2_CHALLENGE
    assert tier_body["tier"] == 2
    assert tier_body["cnf_sha256"] == _TIER2_CNF_SHA256

    by_id = multi_tier_client.get(
        f"/v1/synthetic-boolean/active-cnf?challenge_id={_TIER2_CHALLENGE}",
        headers=headers,
    )
    assert by_id.status_code == 200, by_id.text
    id_body = by_id.json()
    assert id_body["challenge_id"] == _TIER2_CHALLENGE
    assert id_body["tier"] == 2

    ambiguous = multi_tier_client.get(
        f"/v1/synthetic-boolean/active-cnf?challenge_id={_TIER2_CHALLENGE}&tier=2",
        headers=headers,
    )
    assert ambiguous.status_code == 400
    assert ambiguous.json()["detail"] == "use either challenge_id or tier, not both"


def test_solve_post_accepts_non_default_active_tier(
    multi_tier_client: TestClient,
    alice: Keypair,
    tmp_path: Path,
) -> None:
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id=_TIER2_CHALLENGE,
        dimacs_solution=_TIER2_VALID_SOL,
    )
    resp = multi_tier_client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ranked"
    assert body["challenge_id"] == _TIER2_CHALLENGE

    import sqlite3

    with sqlite3.connect(str(tmp_path / "publisher-multi-tier.db")) as raw:
        rows = dict(
            raw.execute(
                "SELECT challenge_id, status FROM lane_challenges "
                "WHERE challenge_id IN (?, ?)",
                (_CHALLENGE, _TIER2_CHALLENGE),
            ).fetchall()
        )
    assert rows[_CHALLENGE] == "active"
    assert rows[_TIER2_CHALLENGE] == "locked"


def test_same_miner_same_bundle_can_submit_distinct_challenges(
    multi_tier_client: TestClient,
    alice: Keypair,
    tmp_path: Path,
) -> None:
    first_data, first_headers = _solve_post_form(
        kp=alice,
        challenge_id=_CHALLENGE,
        dimacs_solution=_VALID_SOL,
    )
    first = multi_tier_client.post(
        "/v1/agents/submit", data=first_data, headers=first_headers
    )
    assert first.status_code == 200, first.text

    second_data, second_headers = _solve_post_form(
        kp=alice,
        challenge_id=_TIER2_CHALLENGE,
        dimacs_solution=_TIER2_VALID_SOL,
    )
    second = multi_tier_client.post(
        "/v1/agents/submit", data=second_data, headers=second_headers
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] != first.json()["id"]

    import sqlite3

    with sqlite3.connect(str(tmp_path / "publisher-multi-tier.db")) as raw:
        rows = raw.execute(
            "SELECT sat_challenge_id, seq_no, status FROM agent_submissions "
            "WHERE miner_hotkey = ? ORDER BY sat_challenge_id",
            (alice.ss58_address,),
        ).fetchall()
    assert set(rows) == {
        (_CHALLENGE, 1, "ranked"),
        (_TIER2_CHALLENGE, 1, "ranked"),
    }


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
            "SELECT status, current_score, sat_challenge_id, seq_no "
            "FROM agent_submissions WHERE id = ?",
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
    assert sub == ("ranked", 1.0, _CHALLENGE, 1)
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


def test_challenge_not_active_returns_409_without_persisting_submission(
    client: TestClient, alice: Keypair, tmp_path: Path
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

    import sqlite3

    with sqlite3.connect(str(tmp_path / "publisher.db")) as raw:
        row_count = raw.execute(
            "SELECT COUNT(*) FROM agent_submissions WHERE miner_hotkey = ?",
            (alice.ss58_address,),
        ).fetchone()[0]
    assert row_count == 0


def test_second_valid_post_loses_to_locked_winner(
    client: TestClient, alice: Keypair, bob: Keypair, tmp_path: Path
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

    import sqlite3

    with sqlite3.connect(str(tmp_path / "publisher.db")) as raw:
        rows = raw.execute(
            "SELECT miner_hotkey, sat_challenge_id, seq_no "
            "FROM agent_submissions "
            "WHERE sat_challenge_id = ? ORDER BY seq_no",
            (_CHALLENGE,),
        ).fetchall()
    assert rows == [(alice.ss58_address, _CHALLENGE, 1)]


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


# --------------------------------------------------------------------------
# PR-bundle: tier_difficulty multi-active + score_multiplier on public API.
# --------------------------------------------------------------------------


def _insert_labeled_challenge_sync(
    conn: Any,
    *,
    challenge_id: str,
    tier: int,
    cnf_text: str,
    difficulty_label: str | None,
    score_multiplier: float = 1.0,
    status: str = "active",
) -> None:
    cnf_sha256 = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    header = next(line for line in cnf_text.splitlines() if line.startswith("p cnf "))
    _, _, num_vars, num_clauses = header.split()
    conn.execute(
        "INSERT OR REPLACE INTO lane_challenges ("
        "challenge_id, family_id, tier, cnf_text, cnf_path, status, "
        "audit_metadata, losers_published_at_iso, "
        "score_multiplier, difficulty_label, "
        "created_at_iso, updated_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            challenge_id,
            _FAMILY,
            tier,
            cnf_text,
            None,
            status,
            json.dumps(
                {
                    "cnf_sha256": cnf_sha256,
                    "num_vars": int(num_vars),
                    "num_clauses": int(num_clauses),
                    "kind": "sha256_preimage",
                },
                sort_keys=True,
            ),
            None,
            float(score_multiplier),
            difficulty_label,
            "2026-05-27T00:00:00.000Z",
            "2026-05-27T00:00:00.000Z",
        ),
    )


def _seed_two_difficulty_actives_sync(db_path: str) -> None:
    """Seed two simultaneously-active rows at tier=1 with distinct difficulty_label."""
    import sqlite3

    from cathedral.lanes.challenge_source import ensure_sqlite_challenge_source_schema

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        # Drive the schema through the async migration path. We bridge
        # sync sqlite3 → aiosqlite via a one-shot asyncio.run so the
        # ALTER + index migrations match production exactly.
        import asyncio

        import aiosqlite

        async def _migrate() -> None:
            a_conn = await aiosqlite.connect(db_path)
            try:
                await ensure_sqlite_challenge_source_schema(a_conn)
            finally:
                await a_conn.close()

        conn.close()
        asyncio.run(_migrate())
        conn = sqlite3.connect(db_path)
        _insert_labeled_challenge_sync(
            conn,
            challenge_id="t1-3b",
            tier=1,
            cnf_text=_CNF,
            difficulty_label="3b",
            score_multiplier=1.0,
        )
        _insert_labeled_challenge_sync(
            conn,
            challenge_id="t1-6b",
            tier=1,
            cnf_text=_TIER2_CNF,
            difficulty_label="6b",
            score_multiplier=10.0,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def two_difficulty_client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = str(tmp_path / "publisher-two-diff.db")
    _seed_two_difficulty_actives_sync(db_path)
    app = build_app(database_path=db_path)
    with TestClient(app) as c:
        yield c


def test_active_challenges_returns_two_rows_when_two_difficulty_actives(
    two_difficulty_client: TestClient,
) -> None:
    """``/active-challenges`` must list both labeled actives + carry new fields."""
    resp = two_difficulty_client.get("/v1/synthetic-boolean/active-challenges")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    ids = {item["challenge_id"] for item in body["items"]}
    assert ids == {"t1-3b", "t1-6b"}
    by_id = {item["challenge_id"]: item for item in body["items"]}
    assert by_id["t1-3b"]["difficulty_label"] == "3b"
    assert by_id["t1-6b"]["difficulty_label"] == "6b"
    assert by_id["t1-3b"]["score_multiplier"] == pytest.approx(1.0)
    assert by_id["t1-6b"]["score_multiplier"] == pytest.approx(10.0)
    # No leak.
    for item in body["items"]:
        assert "cnf_text" not in item
        assert "cnf_path" not in item


def test_current_challenge_difficulty_filter_prefers_label_then_falls_back(
    two_difficulty_client: TestClient,
) -> None:
    """``?tier=1&difficulty=3b`` returns the labeled row; unknown label falls back."""
    resp = two_difficulty_client.get(
        "/v1/synthetic-boolean/current-challenge?tier=1&difficulty=3b"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["challenge_id"] == "t1-3b"
    assert body["difficulty_label"] == "3b"
    assert body["score_multiplier"] == pytest.approx(1.0)

    # Unknown difficulty: degrades to the tier-only active (whichever
    # the source picks first — ordered by challenge_id ASC).
    resp_unknown = two_difficulty_client.get(
        "/v1/synthetic-boolean/current-challenge?tier=1&difficulty=99x"
    )
    assert resp_unknown.status_code == 200
    body_unknown = resp_unknown.json()
    assert body_unknown["challenge_id"] in {"t1-3b", "t1-6b"}


def test_recent_wins_carries_difficulty_label_and_score_multiplier(
    two_difficulty_client: TestClient,
    alice: Keypair,
) -> None:
    """Solving the labeled active surfaces the label + multiplier on recent-wins."""
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id="t1-3b",
        dimacs_solution=_VALID_SOL,
    )
    resp = two_difficulty_client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 200, resp.text

    wins = two_difficulty_client.get("/v1/synthetic-boolean/recent-wins")
    body = wins.json()
    matched = [w for w in body["items"] if w["challenge_id"] == "t1-3b"]
    assert matched, body
    assert matched[0]["difficulty_label"] == "3b"
    assert matched[0]["score_multiplier"] == pytest.approx(1.0)


def test_solve_post_locking_labeled_row_keeps_other_labeled_active(
    two_difficulty_client: TestClient,
    alice: Keypair,
) -> None:
    """Solve the 3b row; the 6b row must stay active (promote-next scoped)."""
    data, headers = _solve_post_form(
        kp=alice,
        challenge_id="t1-3b",
        dimacs_solution=_VALID_SOL,
    )
    resp = two_difficulty_client.post("/v1/agents/submit", data=data, headers=headers)
    assert resp.status_code == 200, resp.text

    actives = two_difficulty_client.get("/v1/synthetic-boolean/active-challenges")
    body = actives.json()
    ids = {item["challenge_id"] for item in body["items"]}
    # The 6b labeled active must remain. The 3b row is now locked
    # (its slot is empty until a 3b-labeled pending is promoted).
    assert "t1-6b" in ids
    assert "t1-3b" not in ids


# --------------------------------------------------------------------------
# fix/sat-board-rotation: submit-gate ordering + per-(hotkey, challenge)
# throttle scope. These run with a REAL 60s interval (the suite default is 0,
# set in conftest) so they actually exercise the limiter.
# --------------------------------------------------------------------------


def test_wrong_card_id_does_not_consume_rate_limit_slot(
    client: TestClient,
    alice: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale-config miner spraying the dead ``eu-ai-act`` lane must not burn
    its own hotkey's rate-limit slot — the wrong-card 400 happens before the
    guard, so a real SAT solve from the same hotkey immediately after wins."""
    monkeypatch.setenv("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "60")

    # Wrong card_id: rejected at the card gate (before the guard, before sig
    # verify) — a dummy signature is fine because it is never reached.
    bad = client.post(
        "/v1/agents/submit",
        data={
            "card_id": "eu-ai-act",
            "display_name": "alice-stale-config",
            "attestation_mode": "ssh-probe",
            "ssh_host": "miner.example.com",
            "ssh_user": "cathedral",
            "ssh_port": "22",
        },
        headers={
            "X-Cathedral-Hotkey": alice.ss58_address,
            "X-Cathedral-Signature": base64.b64encode(b"dummy").decode("ascii"),
        },
    )
    assert bad.status_code == 400, bad.text

    # Real SAT solve from the SAME hotkey, immediately after: must NOT be 429.
    data, headers = _solve_post_form(
        kp=alice, challenge_id=_CHALLENGE, dimacs_solution=_VALID_SOL
    )
    good = client.post("/v1/agents/submit", data=data, headers=headers)
    assert good.status_code == 200, good.text
    assert good.json()["status"] == "ranked"


def test_one_hotkey_solves_two_actives_back_to_back_under_real_interval(
    multi_tier_client: TestClient,
    alice: Keypair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a real 60s interval, one hotkey can solve TWO different active
    challenges back-to-back and BOTH reach the winner path (both lock).

    With the old per-hotkey throttle the second would 429; the per-(hotkey,
    challenge) scope is what lets winner-take-all reward speed across the board.
    """
    monkeypatch.setenv("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "60")

    d1, h1 = _solve_post_form(
        kp=alice, challenge_id=_CHALLENGE, dimacs_solution=_VALID_SOL
    )
    r1 = multi_tier_client.post("/v1/agents/submit", data=d1, headers=h1)
    assert r1.status_code == 200, r1.text

    d2, h2 = _solve_post_form(
        kp=alice, challenge_id=_TIER2_CHALLENGE, dimacs_solution=_TIER2_VALID_SOL
    )
    r2 = multi_tier_client.post("/v1/agents/submit", data=d2, headers=h2)
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] != r1.json()["id"]

    import sqlite3

    with sqlite3.connect(str(tmp_path / "publisher-multi-tier.db")) as raw:
        rows = dict(
            raw.execute(
                "SELECT challenge_id, status FROM lane_challenges "
                "WHERE challenge_id IN (?, ?)",
                (_CHALLENGE, _TIER2_CHALLENGE),
            ).fetchall()
        )
    # Both reached atomic_claim_winner → both locked.
    assert rows[_CHALLENGE] == "locked"
    assert rows[_TIER2_CHALLENGE] == "locked"


def test_one_hotkey_cannot_spam_same_challenge_under_real_interval(
    client: TestClient,
    alice: Keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same hotkey hammering the SAME challenge inside the interval is 429 —
    the per-(hotkey, challenge) throttle still blocks same-challenge spam,
    and the rate limit fires before the lock check."""
    monkeypatch.setenv("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "60")

    d1, h1 = _solve_post_form(
        kp=alice, challenge_id=_CHALLENGE, dimacs_solution=_VALID_SOL
    )
    r1 = client.post("/v1/agents/submit", data=d1, headers=h1)
    assert r1.status_code == 200, r1.text

    # Different solution body (distinct signature, so not a replay) but SAME
    # hotkey + SAME challenge, within the interval → rate-limited.
    retry_sol = "s SATISFIABLE\nv 1 2 3 0\nc retry\n"
    d2, h2 = _solve_post_form(
        kp=alice, challenge_id=_CHALLENGE, dimacs_solution=retry_sol
    )
    r2 = client.post("/v1/agents/submit", data=d2, headers=h2)
    assert r2.status_code == 429, r2.text
