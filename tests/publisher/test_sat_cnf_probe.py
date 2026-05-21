from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
from typer.testing import CliRunner

from cathedral.cli.ops import app as ops_app
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    ChallengeRecord,
    SqliteChallengeSource,
    SqliteFetchTokenStore,
    init_sqlite_challenge_source,
)
from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID
from cathedral.publisher import sat_cnf_probe
from cathedral.publisher.cli import app as publisher_app
from cathedral.publisher.sat_cnf_probe import (
    probe_active_sat_cnf_url_from_db,
    probe_sat_cnf_url,
)

CNF_BODY = b"p cnf 2 1\n1 -2 0\n"
CNF_SHA256 = hashlib.sha256(CNF_BODY).hexdigest()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_probe_sat_cnf_url_hashes_body_without_exposing_url_or_body() -> None:
    secret_url = "https://api.cathedral.test/v1/challenges/probe-001/cnf?t=secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == secret_url
        return httpx.Response(200, content=CNF_BODY, headers={"content-type": "text/plain"})

    async def run() -> dict[str, object]:
        async with _client(handler) as client:
            return await probe_sat_cnf_url(
                secret_url,
                expected_sha256=CNF_SHA256,
                client=client,
            )

    result = asyncio.run(run())
    payload = json.dumps(result, sort_keys=True)

    assert result["ok"] is True
    assert result["bytes"] == len(CNF_BODY)
    assert result["cnf_hash_matches_expected"] is True
    assert "secret-token" not in payload
    assert "probe-001" not in payload
    assert CNF_BODY.decode("utf-8") not in payload


def test_probe_sat_cnf_url_rejects_hash_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"p cnf 1 1\n-1 0\n")

    async def run() -> dict[str, object]:
        async with _client(handler) as client:
            return await probe_sat_cnf_url(
                "https://api.cathedral.test/v1/challenges/probe-002/cnf?t=secret",
                expected_sha256=CNF_SHA256,
                client=client,
            )

    result = asyncio.run(run())

    assert result["ok"] is False
    assert result["cnf_hash_matches_expected"] is False
    assert "CNF URL SHA-256 does not match expected metadata" in result["errors"]


def test_probe_active_sat_cnf_url_from_db_mints_token_and_omits_secret(tmp_path) -> None:
    db_path = tmp_path / "publisher.db"
    asyncio.run(_seed_active_text_challenge(db_path, "probe-active-001"))
    captured_token = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_token
        captured_token = request.url.params["t"]
        assert request.url.path == "/v1/challenges/probe-active-001/cnf"
        return httpx.Response(200, content=CNF_BODY, headers={"content-type": "text/plain"})

    async def run() -> dict[str, object]:
        async with _client(handler) as client:
            return await probe_active_sat_cnf_url_from_db(
                str(db_path),
                public_base_url="https://api.cathedral.test",
                client=client,
            )

    result = asyncio.run(run())
    payload = json.dumps(result, sort_keys=True)

    assert result["ok"] is True
    assert result["challenge_id"] == "probe-active-001"
    assert result["fetch_token_status"] == "minted"
    assert result["cnf_hash_matches_expected"] is True
    assert captured_token
    assert captured_token not in payload
    assert "https://api.cathedral.test" not in payload
    asyncio.run(_assert_fetch_token_exists(db_path, "probe-active-001"))


def test_probe_active_sat_cnf_url_from_db_rejects_proxy_error(tmp_path) -> None:
    db_path = tmp_path / "publisher.db"
    asyncio.run(_seed_active_text_challenge(db_path, "probe-active-404"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "challenge_not_found"})

    async def run() -> dict[str, object]:
        async with _client(handler) as client:
            return await probe_active_sat_cnf_url_from_db(
                str(db_path),
                public_base_url="https://api.cathedral.test",
                client=client,
            )

    result = asyncio.run(run())

    assert result["ok"] is False
    assert result["status_code"] == 404
    assert "CNF URL returned HTTP 404" in result["errors"]


def test_publisher_cli_active_cnf_probe_prints_json(monkeypatch) -> None:
    async def fake_probe(*args, **kwargs) -> dict[str, object]:
        assert args == ("operator.db",)
        assert kwargs["public_base_url"] == "https://api.cathedral.test"
        return {"ok": True, "bytes": 18, "fetch_token_status": "minted"}

    monkeypatch.setattr(sat_cnf_probe, "probe_active_sat_cnf_url_from_db", fake_probe)

    result = CliRunner().invoke(
        publisher_app,
        [
            "sat-active-cnf-probe",
            "--db",
            "operator.db",
            "--public-base-url",
            "https://api.cathedral.test",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["fetch_token_status"] == "minted"


def test_ops_cli_active_cnf_probe_fails_on_probe_error(monkeypatch) -> None:
    async def fake_probe(*args, **kwargs) -> dict[str, object]:
        return {"ok": False, "errors": ["CNF URL returned HTTP 404"]}

    monkeypatch.setattr(sat_cnf_probe, "probe_active_sat_cnf_url_from_db", fake_probe)

    result = CliRunner().invoke(
        ops_app,
        [
            "sat-active-cnf-probe",
            "--db",
            "operator.db",
            "--public-base-url",
            "https://api.cathedral.test",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False


async def _seed_active_text_challenge(db_path, challenge_id: str) -> None:
    conn = await init_sqlite_challenge_source(str(db_path))
    try:
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id=challenge_id,
                family_id=FAMILY_ID,
                tier=1,
                cnf_text=CNF_BODY.decode("utf-8"),
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={
                    "storage": "sqlite_text",
                    "cnf_sha256": CNF_SHA256,
                    "num_vars": 2,
                    "num_clauses": 1,
                },
            ),
            overwrite_status=True,
        )
    finally:
        await conn.close()


async def _assert_fetch_token_exists(db_path, challenge_id: str) -> None:
    conn = await init_sqlite_challenge_source(str(db_path))
    try:
        token = await SqliteFetchTokenStore(conn).get(challenge_id)
        assert token is not None
        assert token.fetch_token
    finally:
        await conn.close()
