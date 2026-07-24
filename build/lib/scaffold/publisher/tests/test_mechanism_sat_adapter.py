"""Tests for scaffold/publisher/mechanism_sat_adapter.py.

Covers the SAT-as-mechanism-#1 adapter: verified V2 SAT scores
(v2_pipeline.score_totals) remapped from miner_hotkey to miner uid via the
metagraph_hotkeys snapshot table, per deploy/MECHANISM_ROUTER_CONTRACT.md.
"""
from __future__ import annotations

import hashlib

from scaffold.publisher import mechanism_sat_adapter, weights
from scaffold.publisher.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "publisher.sqlite"))


def _insert_manifest(
    store: Store,
    *,
    manifest_id: str,
    hotkey: str,
    challenge_id: str,
    weighted_score: float,
    status: str = "verified",
    verified_at_iso: str | None = "2026-07-01T00:00:00.000Z",
    epoch: int | None = None,
) -> None:
    def write(conn):
        conn.execute(
            "INSERT INTO solution_manifests("
            "id, idempotency_key, miner_hotkey, challenge_id, card_id, "
            "assignment_encoding, solution_cid, solution_sha256, solution_bytes, "
            "status, received_at_iso, submitted_at, signature, manifest_json, "
            "weighted_score, verified_at_iso, epoch"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest_id,
                f"idem-{manifest_id}",
                hotkey,
                challenge_id,
                "family",
                "dimacs/v1",
                "local://missing",
                hashlib.sha256(manifest_id.encode()).hexdigest(),
                1,
                status,
                "2026-07-01T00:00:00.000Z",
                "2026-07-01T00:00:00.000Z",
                "sig",
                "{}",
                weighted_score,
                verified_at_iso,
                epoch,
            ),
        )

    store.write(write)


def _insert_metagraph_hotkey(
    store: Store,
    *,
    hotkey: str,
    uid: int,
    network: str = "finney",
    netuid: int = 39,
    updated_at: str = "2026-07-01T00:00:00.000Z",
) -> None:
    def write(conn):
        conn.execute(
            "INSERT OR REPLACE INTO metagraph_hotkeys("
            "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (network, netuid, hotkey, uid, "", 123, updated_at),
        )

    store.write(write)


def _common_env(monkeypatch) -> None:
    monkeypatch.setenv(weights.NETWORK_ENV, "finney")
    monkeypatch.setenv(weights.NETUID_ENV, "39")


def test_maps_verified_scores_to_uids_and_drops_unmapped_hotkeys(tmp_path, monkeypatch):
    _common_env(monkeypatch)
    store = _store(tmp_path)

    _insert_manifest(
        store, manifest_id="m-mapped-1", hotkey="hk-mapped-1",
        challenge_id="chal-1", weighted_score=3.0,
    )
    _insert_manifest(
        store, manifest_id="m-mapped-2", hotkey="hk-mapped-2",
        challenge_id="chal-2", weighted_score=5.0,
    )
    _insert_manifest(
        store, manifest_id="m-unmapped", hotkey="hk-unmapped",
        challenge_id="chal-3", weighted_score=7.0,
    )
    _insert_metagraph_hotkey(store, hotkey="hk-mapped-1", uid=11)
    _insert_metagraph_hotkey(store, hotkey="hk-mapped-2", uid=22)
    # hk-unmapped intentionally has no metagraph_hotkeys row.

    vector, meta = mechanism_sat_adapter.sat_mechanism_scores(store)

    assert vector == {11: 3.0, 22: 5.0}
    assert meta.mechanism_id == mechanism_sat_adapter.MECHANISM_ID
    assert meta.source == "sat_adapter"
    assert meta.sig_ok is True
    assert isinstance(meta.signed_at_ms, int)
    assert meta.signed_at_ms > 0


def test_sums_multiple_verified_challenges_per_uid(tmp_path, monkeypatch):
    _common_env(monkeypatch)
    store = _store(tmp_path)

    _insert_manifest(
        store, manifest_id="m-1", hotkey="hk-a", challenge_id="chal-1",
        weighted_score=2.0,
    )
    _insert_manifest(
        store, manifest_id="m-2", hotkey="hk-a", challenge_id="chal-2",
        weighted_score=4.5,
    )
    _insert_metagraph_hotkey(store, hotkey="hk-a", uid=1)

    vector, _meta = mechanism_sat_adapter.sat_mechanism_scores(store)

    assert vector == {1: 6.5}


def test_unverified_rows_are_not_scored(tmp_path, monkeypatch):
    _common_env(monkeypatch)
    store = _store(tmp_path)

    _insert_manifest(
        store, manifest_id="m-pending", hotkey="hk-a", challenge_id="chal-1",
        weighted_score=9.0, status="received", verified_at_iso=None,
    )
    _insert_metagraph_hotkey(store, hotkey="hk-a", uid=1)

    vector, meta = mechanism_sat_adapter.sat_mechanism_scores(store)

    assert vector == {}
    assert meta.source == "sat_adapter"


def test_empty_store_returns_empty_vector_and_valid_meta(tmp_path, monkeypatch):
    _common_env(monkeypatch)
    store = _store(tmp_path)

    vector, meta = mechanism_sat_adapter.sat_mechanism_scores(store)

    assert vector == {}
    assert meta.mechanism_id == mechanism_sat_adapter.MECHANISM_ID
    assert meta.source == "sat_adapter"
    assert meta.sig_ok is True


def test_since_iso_and_epoch_filters_pass_through(tmp_path, monkeypatch):
    _common_env(monkeypatch)
    store = _store(tmp_path)

    _insert_manifest(
        store, manifest_id="m-old", hotkey="hk-a", challenge_id="chal-1",
        weighted_score=1.0, verified_at_iso="2026-01-01T00:00:00.000Z",
    )
    _insert_manifest(
        store, manifest_id="m-new", hotkey="hk-a", challenge_id="chal-2",
        weighted_score=2.0, verified_at_iso="2026-07-01T00:00:00.000Z",
    )
    _insert_metagraph_hotkey(store, hotkey="hk-a", uid=1)

    vector, _meta = mechanism_sat_adapter.sat_mechanism_scores(
        store, since_iso="2026-06-01T00:00:00.000Z",
    )

    assert vector == {1: 2.0}


def test_default_off_no_env_still_maps_using_default_network_netuid(tmp_path, monkeypatch):
    """No CATHEDRAL_WEIGHT_POLICY_* env set: defaults (finney/39) should still
    find a mapping if metagraph_hotkeys already carries those defaults, and
    must not raise."""
    monkeypatch.delenv(weights.NETWORK_ENV, raising=False)
    monkeypatch.delenv(weights.NETUID_ENV, raising=False)
    store = _store(tmp_path)

    _insert_manifest(
        store, manifest_id="m-1", hotkey="hk-a", challenge_id="chal-1",
        weighted_score=1.0,
    )
    _insert_metagraph_hotkey(store, hotkey="hk-a", uid=1, network="finney", netuid=39)

    vector, meta = mechanism_sat_adapter.sat_mechanism_scores(store)

    assert vector == {1: 1.0}
    assert meta.source == "sat_adapter"
