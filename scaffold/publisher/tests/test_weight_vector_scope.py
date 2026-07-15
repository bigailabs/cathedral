"""Persisted weight vectors must be isolated by their signed subnet envelope."""

import json

from scaffold.publisher import weights
from scaffold.publisher.store import Store


def _vector(network: str, netuid: int, vector_id: str) -> dict:
    return {
        "network": network,
        "netuid": netuid,
        "vector_id": vector_id,
        "generated_at": "2026-07-13T00:00:00.000Z",
        "policy_version": 1,
    }


def test_persisted_vectors_are_scoped_by_network_and_netuid(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "publisher.sqlite"))

    monkeypatch.setenv(weights.NETWORK_ENV, "test")
    monkeypatch.setenv(weights.NETUID_ENV, "292")
    test_vector = _vector("test", 292, "test-vector")
    weights._persist_vector(store, test_vector)

    monkeypatch.setenv(weights.NETWORK_ENV, "finney")
    monkeypatch.setenv(weights.NETUID_ENV, "39")
    assert weights._load_persisted_vector(store) is None
    main_vector = _vector("finney", 39, "main-vector")
    weights._persist_vector(store, main_vector)
    assert weights._load_persisted_vector(store) == main_vector

    monkeypatch.setenv(weights.NETWORK_ENV, "test")
    monkeypatch.setenv(weights.NETUID_ENV, "292")
    assert weights._load_persisted_vector(store) == test_vector

    rows = store.query("SELECT id, vector_json FROM signed_weight_vectors ORDER BY id")
    assert {row["id"] for row in rows} == {"latest:finney:39", "latest:test:292"}
    assert {json.loads(row["vector_json"])["vector_id"] for row in rows} == {
        "main-vector",
        "test-vector",
    }


def test_refresh_lock_is_scoped_by_network_and_netuid(monkeypatch):
    monkeypatch.setenv(weights.NETWORK_ENV, "test")
    monkeypatch.setenv(weights.NETUID_ENV, "292")
    test_lock = weights._refresh_lock_name()

    monkeypatch.setenv(weights.NETWORK_ENV, "finney")
    monkeypatch.setenv(weights.NETUID_ENV, "39")
    main_lock = weights._refresh_lock_name()

    assert test_lock == "cathedral:weights:refresh:test:292"
    assert main_lock == "cathedral:weights:refresh:finney:39"
    assert test_lock != main_lock


def test_legacy_singleton_is_only_adopted_for_matching_subnet(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "publisher.sqlite"))
    legacy = _vector("test", 292, "legacy-test")

    def write(conn):
        conn.execute(
            "INSERT INTO signed_weight_vectors"
            "(id, generated_at_iso, policy_version, vector_json, updated_at_iso) "
            "VALUES (?, ?, ?, ?, ?)",
            ("latest", legacy["generated_at"], 1, json.dumps(legacy), legacy["generated_at"]),
        )

    store.write(write)

    monkeypatch.setenv(weights.NETWORK_ENV, "finney")
    monkeypatch.setenv(weights.NETUID_ENV, "39")
    assert weights._load_persisted_vector(store) is None

    monkeypatch.setenv(weights.NETWORK_ENV, "test")
    monkeypatch.setenv(weights.NETUID_ENV, "292")
    assert weights._load_persisted_vector(store) == legacy
