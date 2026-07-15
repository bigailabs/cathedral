"""Focused contract tests for V2 immutable CNF artifact delivery."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from scaffold.publisher import epoch_publisher
from scaffold.publisher import per_miner as pm
from scaffold.publisher import v2_bitset_submit
from scaffold.publisher import v2_pipeline
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes
from scaffold.publisher.store import Store


SIGNING_KEY_HEX = "42" * 32
_FAMILY = "synthetic_boolean_v1"
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
_BASE_URL = "https://artifacts.example.invalid"


class MemoryObjects:
    def __init__(self, *, fail_on_put: int | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict[str, object]] = []
        self.fail_on_put = fail_on_put

    def put(self, key: str, data: bytes, content_type: str, cache_control: str) -> None:
        call_number = len(self.puts) + 1
        if self.fail_on_put == call_number:
            raise RuntimeError("injected_object_write_failure")
        self.puts.append(
            {
                "key": key,
                "data": bytes(data),
                "content_type": content_type,
                "cache_control": cache_control,
            }
        )
        self.objects[key] = bytes(data)

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)


def _clear_pm_caches() -> None:
    pm.item_meta.cache_clear()
    pm._gen_cached.cache_clear()
    pm._instance_index.cache_clear()


def _configure_pm(monkeypatch, *, seed: str) -> None:
    for prefix in ("CATHEDRAL_PERMINER", "CATHEDRAL_V2_PERMINER"):
        monkeypatch.setenv(f"{prefix}_ENABLED", "1")
        monkeypatch.setenv(f"{prefix}_SEED_SECRET", seed)
        monkeypatch.setenv(f"{prefix}_ALLOTMENT_T1", "1")
        monkeypatch.setenv(f"{prefix}_ALLOTMENT_T2", "1")
        monkeypatch.setenv(f"{prefix}_NVARS_T1", "12")
        monkeypatch.setenv(f"{prefix}_NVARS_T2", "12")
        monkeypatch.setenv(f"{prefix}_NCLAUSES_T1", "30")
        monkeypatch.setenv(f"{prefix}_NCLAUSES_T2", "30")
    monkeypatch.setenv("CATHEDRAL_V2_REAL_FRACTION", "0")
    _clear_pm_caches()


def _publish(
    store: Store,
    sink: MemoryObjects,
    *,
    identity: str,
    epoch: int,
    published_at: str = "2026-07-10T12:00:00.000Z",
) -> dict:
    with v2_pipeline.v2_pm_env():
        return epoch_publisher.prepublish_epoch_pair(
            store,
            epoch=epoch,
            assignment_identities=[identity],
            allotment_by_tier={1: 1, 2: 1},
            cnf_base_url=_BASE_URL,
            put_object=sink.put,
            get_object=sink.get,
            published_at=published_at,
        )


def _now_iso() -> str:
    value = datetime.now(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _keypair(uri: str):
    from bittensor_wallet import Keypair

    return Keypair.create_from_uri(uri)


def _read_headers(keypair) -> dict[str, str]:
    submitted_at = _now_iso()
    claim = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE,
        card_id=_FAMILY,
        miner_hotkey=keypair.ss58_address,
        submitted_at=submitted_at,
        challenge_id="",
        dimacs_solution_sha256="",
    )
    return {
        "X-Cathedral-Hotkey": keypair.ss58_address,
        "X-Cathedral-Signature": base64.b64encode(keypair.sign(claim)).decode("ascii"),
        "X-Cathedral-Submitted-At": submitted_at,
    }


def _build_app(
    tmp_path,
    monkeypatch,
    *,
    epoch: int,
    owner,
    allowlist: str = "",
):
    _configure_pm(monkeypatch, seed=f"artifact-app-seed-{epoch}")
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BITSET_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_CNF_ARTIFACTS_ENABLED", "true")
    monkeypatch.setenv(
        "CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "artifact-submit-token-secret"
    )
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS", "300")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_ALLOWLIST", allowlist)
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    monkeypatch.setattr(pm, "current_epoch", lambda: epoch)
    app = build_app(
        database_path=str(tmp_path / "publisher.sqlite"),
        signing_key_hex=SIGNING_KEY_HEX,
    )
    sink = MemoryObjects()
    _publish(app.state.v2_store, sink, identity=owner.ss58_address, epoch=epoch)
    return app, sink


def _challenge_id(identity: str, epoch: int, tier: int = 1, seq: int = 0) -> str:
    with v2_pipeline.v2_pm_env():
        return pm.instance_id(identity, epoch, tier, seq)


def _access(
    client: TestClient, keypair, challenge_id: str, *, tier: int = 1, seq: int = 0
):
    return client.get(
        "/v2/synthetic-boolean/per-miner/cnf-access",
        params={"challenge_id": challenge_id, "tier": tier, "seq": seq},
        headers=_read_headers(keypair),
    )


def test_content_addressed_identity_and_verified_idempotent_publication(
    tmp_path, monkeypatch
):
    _configure_pm(monkeypatch, seed="artifact-identity-seed")
    store = Store(str(tmp_path / "catalog.sqlite"), prefer_env_database_url=False)
    sink = MemoryObjects()

    same_a = epoch_publisher.content_addressed_identity(b"p cnf 1 1\n1 0\n")
    same_b = epoch_publisher.content_addressed_identity(b"p cnf 1 1\n1 0\n")
    different = epoch_publisher.content_addressed_identity(b"p cnf 1 1\n-1 0\n")
    assert same_a == same_b
    assert same_a.key == f"v2/cnf/v1/sha256/{same_a.sha256}.cnf"
    assert same_a.key != different.key

    summary = _publish(store, sink, identity="assignment-identity-a", epoch=101)
    assert summary["ready"] is True
    assert summary["published_current"] == summary["expected_current"] == 2
    assert summary["published_next"] == summary["expected_next"] == 2
    assert epoch_publisher.epoch_is_ready(store, 101)
    first_put_count = len(sink.puts)
    assert first_put_count == summary["unique_artifacts"]

    for put in sink.puts:
        body = put["data"]
        identity = epoch_publisher.content_addressed_identity(body)
        assert put["key"] == identity.key
        assert sink.objects[identity.key] == body
        assert put["content_type"] == epoch_publisher.ARTIFACT_CONTENT_TYPE
        assert put["cache_control"] == epoch_publisher.ARTIFACT_CACHE_CONTROL
        assert body.startswith(b"p cnf ")
        assert b"submit_token" not in body
        assert b"artifact-submit-token-secret" not in body
        assert b"assignment-identity-a" not in body

    # Identical rerun only reads/verifies existing immutable objects.
    rerun = _publish(store, sink, identity="assignment-identity-a", epoch=101)
    assert rerun["ready"] is True
    assert len(sink.puts) == first_put_count


def test_existing_object_mismatch_is_rejected_and_clears_readiness(
    tmp_path, monkeypatch
):
    _configure_pm(monkeypatch, seed="artifact-mismatch-seed")
    store = Store(str(tmp_path / "catalog.sqlite"), prefer_env_database_url=False)
    sink = MemoryObjects()
    _publish(store, sink, identity="assignment-identity-b", epoch=201)
    corrupt_key = sorted(sink.objects)[0]
    sink.objects[corrupt_key] = b"p cnf 1 1\n1 0\ncorrupt"

    with pytest.raises(epoch_publisher.ArtifactMismatchError):
        _publish(store, sink, identity="assignment-identity-b", epoch=201)
    assert epoch_publisher.epoch_is_ready(store, 201) is False


def test_partial_current_next_publication_never_claims_readiness(tmp_path, monkeypatch):
    _configure_pm(monkeypatch, seed="artifact-partial-seed")
    store = Store(str(tmp_path / "catalog.sqlite"), prefer_env_database_url=False)
    # Two current-epoch writes succeed; the first next-epoch write fails.
    sink = MemoryObjects(fail_on_put=3)
    with pytest.raises(RuntimeError, match="injected_object_write_failure"):
        _publish(store, sink, identity="assignment-identity-c", epoch=301)
    readiness = epoch_publisher.epoch_readiness(store, 301)
    assert readiness is not None
    assert int(readiness["ready"]) == 0
    assert (
        int(readiness["published_current"]) == int(readiness["expected_current"]) == 2
    )
    assert int(readiness["published_next"]) < int(readiness["expected_next"])
    assert epoch_publisher.epoch_is_ready(store, 301) is False

    # Resume verifies the already-written objects and completes the pair.
    sink.fail_on_put = None
    assert (
        _publish(store, sink, identity="assignment-identity-c", epoch=301)["ready"]
        is True
    )


def test_authenticated_metadata_ownership_allowlist_and_token_binding(
    tmp_path, monkeypatch
):
    owner = _keypair("//ArtifactOwner")
    other = _keypair("//ArtifactOther")
    app, sink = _build_app(
        tmp_path,
        monkeypatch,
        epoch=401,
        owner=owner,
        allowlist=owner.ss58_address,
    )
    client = TestClient(app)
    challenge_id = _challenge_id(owner.ss58_address, 401)

    missing_auth = client.get(
        "/v2/synthetic-boolean/per-miner/cnf-access",
        params={"challenge_id": challenge_id, "tier": 1, "seq": 0},
    )
    assert missing_auth.status_code == 422
    assert missing_auth.headers["cache-control"] == "no-store"

    response = _access(client, owner, challenge_id)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    payload = response.json()
    assert payload["schema"] == epoch_publisher.ACCESS_SCHEMA
    assert payload["artifact_key"] == epoch_publisher.artifact_key(
        payload["cnf_sha256"]
    )
    assert payload["artifact_url"] == f"{_BASE_URL}/{payload['artifact_key']}"
    assert payload["cnf_bytes"] == len(sink.objects[payload["artifact_key"]])
    assert (
        hashlib.sha256(sink.objects[payload["artifact_key"]]).hexdigest()
        == payload["cnf_sha256"]
    )
    assert owner.ss58_address not in payload["artifact_url"]

    token_payload = v2_bitset_submit.verify_submit_token(
        payload["submit_token"],
        secret="artifact-submit-token-secret",
        miner_hotkey=owner.ss58_address,
        challenge_id=challenge_id,
    )
    assert {
        key: token_payload[key]
        for key in (
            "miner_hotkey",
            "challenge_id",
            "epoch",
            "tier",
            "seq",
            "nvars",
            "cnf_sha256",
        )
    } == {
        "miner_hotkey": owner.ss58_address,
        "challenge_id": challenge_id,
        "epoch": 401,
        "tier": 1,
        "seq": 0,
        "nvars": payload["n_vars"],
        "cnf_sha256": payload["cnf_sha256"],
    }
    with pytest.raises(
        v2_bitset_submit.BitsetSubmitError, match="submit_token_hotkey_mismatch"
    ):
        v2_bitset_submit.verify_submit_token(
            payload["submit_token"],
            secret="artifact-submit-token-secret",
            miner_hotkey=other.ss58_address,
            challenge_id=challenge_id,
        )

    # A valid signature from a different miner cannot claim this assignment.
    rejected = _access(client, other, challenge_id)
    assert rejected.status_code == 404
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.json()["detail"] == "challenge_id_not_in_miner_set"


def test_metadata_enforces_submit_token_allowlist(tmp_path, monkeypatch):
    owner = _keypair("//ArtifactAllowlistOwner")
    app, _sink = _build_app(
        tmp_path,
        monkeypatch,
        epoch=451,
        owner=owner,
        allowlist="5DifferentAllowlistedHotkey",
    )
    response = _access(
        TestClient(app),
        owner,
        _challenge_id(owner.ss58_address, 451),
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == "v2_submit_token_hotkey_not_allowlisted"


def test_legacy_body_token_endpoint_reuses_published_bytes_then_falls_back(
    tmp_path, monkeypatch
):
    owner = _keypair("//ArtifactLegacyOwner")
    app, _sink = _build_app(tmp_path, monkeypatch, epoch=501, owner=owner)
    client = TestClient(app)
    challenge_id = _challenge_id(owner.ss58_address, 501)
    path = "/v2/synthetic-boolean/per-miner/cnf"
    params = {"challenge_id": challenge_id, "tier": 1, "seq": 0}

    original_generate = pm.generate_instance

    def _generation_must_not_run(*_args, **_kwargs):
        raise AssertionError("published-byte reuse unexpectedly regenerated the CNF")

    monkeypatch.setattr(pm, "generate_instance", _generation_must_not_run)
    reused = client.get(path, params=params, headers=_read_headers(owner))
    assert reused.status_code == 200, reused.text
    assert reused.headers["cache-control"] == "no-store"
    assert reused.headers["x-cathedral-cnf-artifact-reused"] == "true"
    assert reused.headers["x-cathedral-submit-token"]
    assert (
        hashlib.sha256(reused.content).hexdigest()
        == reused.headers["x-cathedral-cnf-sha256"]
    )

    # Simulate publication/catalog unavailability.  The old endpoint keeps its
    # exact deterministic generation path and still mints a body-bound token.
    def _delete(conn):
        conn.execute(
            "DELETE FROM v2_cnf_artifacts WHERE challenge_id=?", (challenge_id,)
        )
        conn.execute("DELETE FROM v2_cnf_store WHERE challenge_id=?", (challenge_id,))

    app.state.v2_store.write(_delete)
    monkeypatch.setattr(pm, "generate_instance", original_generate)
    fallback = client.get(path, params=params, headers=_read_headers(owner))
    assert fallback.status_code == 200, fallback.text
    assert fallback.headers.get("x-cathedral-cnf-artifact-reused") is None
    assert fallback.text == reused.text
    assert fallback.headers["x-cathedral-submit-token"]


def test_metadata_epoch_grace_and_token_expiry(tmp_path, monkeypatch):
    owner = _keypair("//ArtifactGraceOwner")
    app, sink = _build_app(tmp_path, monkeypatch, epoch=601, owner=owner)
    # The previous epoch is independently ready only after its own current+next
    # pair was published.  This preserves the existing one-epoch grace window.
    _publish(app.state.v2_store, sink, identity=owner.ss58_address, epoch=600)
    client = TestClient(app)

    current = _access(client, owner, _challenge_id(owner.ss58_address, 601))
    previous = _access(client, owner, _challenge_id(owner.ss58_address, 600))
    assert current.status_code == 200, current.text
    assert previous.status_code == 200, previous.text

    stale = _access(client, owner, _challenge_id(owner.ss58_address, 599))
    future = _access(client, owner, _challenge_id(owner.ss58_address, 602))
    assert stale.status_code == 410
    assert future.status_code == 410
    assert stale.json()["detail"] == "per_miner_challenge_expired"
    assert future.json()["detail"] == "per_miner_challenge_expired"

    import scaffold.publisher.app as app_module

    monkeypatch.setattr(
        app_module, "_now_iso_ms_plus", lambda _secs: "2000-01-01T00:00:00.000Z"
    )
    expired_response = _access(client, owner, _challenge_id(owner.ss58_address, 601))
    assert expired_response.status_code == 200
    with pytest.raises(
        v2_bitset_submit.BitsetSubmitError, match="submit_token_expired"
    ):
        v2_bitset_submit.verify_submit_token(
            expired_response.json()["submit_token"],
            secret="artifact-submit-token-secret",
            miner_hotkey=owner.ss58_address,
            challenge_id=_challenge_id(owner.ss58_address, 601),
        )
