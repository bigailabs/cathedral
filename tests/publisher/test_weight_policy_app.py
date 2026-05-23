from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

import cathedral.publisher.app as publisher_app
from cathedral.publisher.app import build_app


def _clear_weight_policy_env(monkeypatch) -> None:
    for name in (
        "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY",
        "CATHEDRAL_WEIGHT_POLICY_NETWORK",
        "CATHEDRAL_WEIGHT_POLICY_NETUID",
        "CATHEDRAL_WEIGHT_POLICY_KEY_ID",
        "CATHEDRAL_WEIGHT_POLICY_BURN_UID",
        "CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE",
        "CATHEDRAL_WEIGHT_POLICY_INTERVAL_SECS",
        "CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS",
        "CATHEDRAL_WEIGHT_POLICY_LIMIT",
        "CATHEDRAL_WEIGHT_POLICY_TASK_FAMILY_WEIGHTS_JSON",
        "CATHEDRAL_WEIGHT_POLICY_SYNTHETIC_BOOLEAN_V1_WEIGHT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_weight_policy_route_builds_without_signing_key(tmp_path, monkeypatch) -> None:
    """Regression: mounting the route must not reference an undefined router."""

    _clear_weight_policy_env(monkeypatch)

    app = build_app(str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        for path in (
            "/v1/validator/weights/next",
            "/api/cathedral/v1/validator/weights/next",
        ):
            response = client.get(path)

            assert response.status_code == 503
            assert response.json() == {"detail": "no weight vector available yet"}


def test_weight_policy_route_serves_produced_vector_when_configured(
    tmp_path,
    monkeypatch,
) -> None:
    """Configured publisher apps must wire the store and producer together."""

    _clear_weight_policy_env(monkeypatch)
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY", "11" * 32)
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_INTERVAL_SECS", "3600")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS", "3600")

    app = build_app(str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        response = client.get("/v1/validator/weights/next")
        for _ in range(50):
            if response.status_code == 200:
                break
            time.sleep(0.02)
            response = client.get("/v1/validator/weights/next")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["signature"]
        assert payload["key_id"] == "cathedral-weight-policy"
        assert payload["network"] == "finney"
        assert payload["netuid"] == 39
        assert payload["policy_metadata"]["score_source"] == (
            "agent_submissions.current_score+configured_task_family_rows"
        )


def test_weight_policy_producer_starts_after_sat_seed(tmp_path, monkeypatch) -> None:
    """Regression: startup DB writers must finish before producer task starts."""

    _clear_weight_policy_env(monkeypatch)
    events: list[str] = []

    async def fake_seed(_source) -> None:
        events.append("seed_start")
        await asyncio.sleep(0)
        events.append("seed_end")

    async def fake_producer(*_args, **kwargs) -> None:
        events.append("producer_start")
        await kwargs["stop"].wait()

    monkeypatch.setattr(
        publisher_app,
        "_seed_synthetic_boolean_challenge_from_env",
        fake_seed,
    )
    monkeypatch.setattr(
        publisher_app,
        "load_producer_from_env",
        lambda: (object(), object()),
    )
    monkeypatch.setattr(
        publisher_app,
        "run_weight_policy_producer",
        fake_producer,
    )
    monkeypatch.setenv(
        "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH",
        str(tmp_path / "active.cnf"),
    )

    app = build_app(str(tmp_path / "publisher.db"))
    with TestClient(app):
        for _ in range(50):
            if "producer_start" in events:
                break
            time.sleep(0.02)

        assert events == ["seed_start", "seed_end", "producer_start"]
