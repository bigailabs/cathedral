from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from scaffold.publisher import external_scores
from scaffold.publisher.app import build_app
from scaffold.publisher.store import Store


TOKEN = "confidential-token"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", TOKEN)
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", raising=False)
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", raising=False)
    monkeypatch.delenv("CATHEDRAL_CONFIDENTIAL_SCORE_MAX_AGE_MS", raising=False)
    app = build_app(database_path=str(tmp_path / "confidential.db"), signing_key_hex="11" * 32)
    return TestClient(app)


def _report(generated_at: datetime, *, scores: list[dict] | None = None) -> dict:
    return {
        "report_id": "confidential-report-1",
        "source": "ignored_by_route",
        "mechanism": "ignored_by_route",
        "epoch": 7,
        "generated_at": _iso(generated_at),
        "scores": scores if scores is not None else [
            {
                "miner_hotkey": "5ConfidentialMiner",
                "uid": 42,
                "score": 0.75,
                "quality": 0.8,
                "tasks_scored": 3,
            },
        ],
        "metadata": {"batch": "unit"},
    }


def _post(client: TestClient, payload: dict, *, token: str = TOKEN):
    return client.post(
        "/v1/external-scores/cathedral-confidential",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def _post_violet(client: TestClient, payload: dict, *, token: str = TOKEN):
    return client.post(
        "/v1/external-scores/violet",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


class _FrozenDateTime(datetime):
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        current = cls.current
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)


def test_cathedral_confidential_records_and_reads_back(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    resp = _post(client, _report(now))

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["source"] == "cathedral_confidential"
    assert body["score_count"] == 1
    scores = external_scores.recent_scores(
        client.app.state.store,
        source="cathedral_confidential",
        since_iso=_iso(now - timedelta(minutes=1)),
    )
    assert scores == {"5ConfidentialMiner": 0.75}


def test_cathedral_confidential_rejects_bad_auth(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = _post(client, _report(datetime.now(timezone.utc)), token="wrong")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_token"


def test_cathedral_confidential_rejects_future_timestamp(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    future = datetime.now(timezone.utc) + timedelta(seconds=61)

    resp = _post(client, _report(future))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "confidential_score_timestamp_in_future"


def test_cathedral_confidential_rejects_stale_timestamp(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    stale = datetime.now(timezone.utc) - timedelta(minutes=16)

    resp = _post(client, _report(stale))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "confidential_score_timestamp_too_old"


def test_cathedral_confidential_rejects_negative_score(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = _post(client, _report(
        datetime.now(timezone.utc),
        scores=[{"miner_hotkey": "5ConfidentialMiner", "score": -0.1}],
    ))

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_score_0"


def test_violet_route_cannot_admit_confidential_source(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    stale = datetime.now(timezone.utc) - timedelta(days=7)
    payload = {
        **_report(stale),
        "report_id": "violet-confidential-bypass",
        "source": external_scores.CONFIDENTIAL_SOURCE,
        "mechanism": external_scores.CONFIDENTIAL_SOURCE,
        "scores": [{"miner_hotkey": "5VictimOrAttacker", "score": 1.0}],
    }

    resp = _post_violet(client, payload)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_source_for_violet_endpoint"
    scores = external_scores.recent_scores(
        client.app.state.store,
        source=external_scores.CONFIDENTIAL_SOURCE,
        since_iso=_iso(datetime.now(timezone.utc) - timedelta(days=8)),
    )
    assert scores == {}


def test_store_report_applies_confidential_freshness_by_source(tmp_path, monkeypatch):
    monkeypatch.delenv("CATHEDRAL_CONFIDENTIAL_SCORE_MAX_AGE_MS", raising=False)
    store = Store(str(tmp_path / "confidential-store.db"), prefer_env_database_url=False)
    stale = datetime.now(timezone.utc) - timedelta(minutes=16)
    report = external_scores.normalize_report(
        {
            **_report(stale),
            "report_id": "direct-confidential-stale",
            "source": external_scores.CONFIDENTIAL_SOURCE,
            "mechanism": external_scores.CONFIDENTIAL_SOURCE,
        },
        default_source=external_scores.CONFIDENTIAL_SOURCE,
    )

    with pytest.raises(external_scores.ExternalScoreError) as exc:
        external_scores.store_report(store, report)

    assert exc.value.reason == "confidential_score_timestamp_too_old"
    assert external_scores.recent_scores(
        store,
        source=external_scores.CONFIDENTIAL_SOURCE,
        since_iso=_iso(datetime.now(timezone.utc) - timedelta(days=1)),
    ) == {}


def test_recent_scores_expires_confidential_by_generated_at(tmp_path, monkeypatch):
    monkeypatch.setattr(external_scores, "datetime", _FrozenDateTime)
    client = _client(tmp_path, monkeypatch)
    accepted_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _FrozenDateTime.current = accepted_at
    generated_at = accepted_at - timedelta(minutes=14, seconds=59)
    payload = {
        **_report(generated_at),
        "report_id": "near-expired-confidential",
        "scores": [{"miner_hotkey": "5StillPaid", "score": 1.0}],
    }

    resp = _post(client, payload)
    assert resp.status_code == 202

    _FrozenDateTime.current = accepted_at + timedelta(minutes=59, seconds=59)
    scores = external_scores.recent_scores(
        client.app.state.store,
        source=external_scores.CONFIDENTIAL_SOURCE,
        since_iso=_iso(_FrozenDateTime.current - timedelta(seconds=3600)),
    )
    assert scores == {}


def test_recent_scores_keeps_fresh_confidential_report(tmp_path, monkeypatch):
    monkeypatch.setattr(external_scores, "datetime", _FrozenDateTime)
    client = _client(tmp_path, monkeypatch)
    accepted_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _FrozenDateTime.current = accepted_at
    generated_at = accepted_at - timedelta(minutes=1)
    payload = {
        **_report(generated_at),
        "report_id": "fresh-confidential",
        "scores": [{"miner_hotkey": "5FreshMiner", "score": 0.9}],
    }

    resp = _post(client, payload)
    assert resp.status_code == 202

    _FrozenDateTime.current = accepted_at + timedelta(minutes=2)
    scores = external_scores.recent_scores(
        client.app.state.store,
        source=external_scores.CONFIDENTIAL_SOURCE,
        since_iso=_iso(_FrozenDateTime.current - timedelta(seconds=3600)),
    )
    assert scores == {"5FreshMiner": 0.9}
