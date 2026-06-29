"""Regression tests for serving signed weight vectors across read roles.

The validator feed must never rebuild weights on the request path, but multiple
read origins also must not serve different in-memory vectors after one process
has persisted a newer signed vector.
"""

from __future__ import annotations

from scaffold.publisher import weights
from scaffold.publisher.store import Store


def _vector(signature: str, policy_version: int = 1) -> dict:
    return {
        "signature": signature,
        "generated_at": "2026-06-29T00:00:00.000Z",
        "policy_version": policy_version,
        "weights": [],
    }


def test_cached_vector_prefers_shared_persisted_vector(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(weights, "_ensure_bg_started", lambda *a, **kw: None)
    store = Store(str(tmp_path / "weights.db"))
    weights._reset_vector_cache()

    weights._cache_write(_vector("old-process-cache", 1))
    weights._persist_vector(store, _vector("new-shared-persisted", 2))

    served = weights.cached_vector(store, signing_key_hex="00" * 32)

    assert served is not None
    assert served["signature"] == "new-shared-persisted"
    # The local cache is updated too, so the next DB outage keeps serving the
    # converged signed payload rather than reverting to the stale process copy.
    monkeypatch.setattr(
        weights,
        "_load_persisted_vector",
        lambda _store: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    fallback = weights.cached_vector(store, signing_key_hex="00" * 32)
    assert fallback is not None
    assert fallback["signature"] == "new-shared-persisted"


def test_cached_vector_falls_back_to_process_cache_on_db_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(weights, "_ensure_bg_started", lambda *a, **kw: None)
    store = Store(str(tmp_path / "weights.db"))
    weights._reset_vector_cache()
    weights._cache_write(_vector("process-cache", 1))
    monkeypatch.setattr(
        weights,
        "_load_persisted_vector",
        lambda _store: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    served = weights.cached_vector(store, signing_key_hex="00" * 32)

    assert served is not None
    assert served["signature"] == "process-cache"
