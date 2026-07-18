"""End-to-end acceptance tests for the confidential CPU reward path."""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone

import pytest

_CANARY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "scripts",
    "confidential_cpu_publisher_canary.py",
)
_spec = importlib.util.spec_from_file_location(
    "confidential_cpu_publisher_canary", _CANARY_PATH
)
canary = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(canary)
CanaryError = canary.CanaryError
run_canary = canary.run_canary


WORKER_HOTKEY = "5CpuWorker111111111111111111111111111111111111111111"
UNREGISTERED_HOTKEY = "5Unregistered11111111111111111111111111111111111111"


def _body(
    epoch,
    scores: list[dict],
    *,
    network: str = "finney",
    netuid=39,
) -> bytes:
    report = {
        "source": "cathedral_confidential_tdx",
        "mechanism": "cathedral_confidential_tdx",
        "network": network,
        "netuid": netuid,
        "epoch": epoch,
        "complete": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scores": scores,
        "metadata": {"canary": "confidential-cpu-publisher"},
    }
    # Deliberate whitespace and trailing newline exercise raw-body HMAC over the
    # exact producer bytes rather than a re-serialized approximation.
    return (json.dumps(report, indent=2) + "\n").encode("utf-8")


def test_exact_report_reaches_pinned_validator_then_zero_revokes() -> None:
    summary = run_canary(
        _body(
            41,
            [
                {"miner_hotkey": WORKER_HOTKEY, "score": 1.0},
                {"miner_hotkey": UNREGISTERED_HOTKEY, "score": 0.75},
            ],
        ),
        _body(42, []),
        {WORKER_HOTKEY: 7},
        burn_uid=0,
    )

    assert summary["status"] == "passed"
    assert summary["positive"]["uid_weights"] == {7: 1.0}
    assert summary["positive"]["filtered_unregistered_hotkeys"] == [
        UNREGISTERED_HOTKEY
    ]
    assert summary["revoke"]["uid_weights"] == {0: 1.0}
    assert summary["revoke"]["old_report_replay"] == {
        "http_status": 409,
        "latest_report_unchanged": True,
        "reason": "epoch_too_old",
    }
    assert summary["revoke"]["old_vector_rollback_rejected"] is True
    assert (
        summary["revoke"]["policy_version"]
        > summary["positive"]["policy_version"]
    )
    isolation = dict(summary["isolation"])
    assert isolation.pop("os_network_sandbox") in {
        "sandbox-exec-deny-network",
        "unavailable-python-guard-only",
    }
    assert isolation == {
        "database_backend": "sqlite",
        "child_process": True,
        "cnf_store_registration_removed": True,
        "egress_attempts": 0,
        "environment": "explicit_allowlist",
        "imports_after_environment_isolation": True,
        "private_values_in_evidence": False,
        "python_isolated_mode": True,
        "python_egress_guard": "audit_hook",
        "raw_body_tamper_rejected": True,
        "site_startup_disabled": True,
        "sitecustomize_loaded": False,
        "store_closed": True,
        "temporary_root_removed": True,
    }


def test_canary_rejects_nonempty_revoke_report() -> None:
    with pytest.raises(CanaryError, match="empty scores list"):
        run_canary(
            _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": 1.0}]),
            _body(42, [{"miner_hotkey": WORKER_HOTKEY, "score": 0.0}]),
            {WORKER_HOTKEY: 7},
        )


def test_canary_requires_at_least_one_registered_positive_hotkey() -> None:
    with pytest.raises(CanaryError, match="at least one positive"):
        run_canary(
            _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": 1.0}]),
            _body(42, []),
            {},
        )


@pytest.mark.parametrize("bad_score", [{"not": "numeric"}, [1], "not-a-number", True])
def test_canary_normalizes_malformed_score_errors(bad_score) -> None:
    with pytest.raises(CanaryError, match="must be numeric"):
        run_canary(
            _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": bad_score}]),
            _body(42, []),
            {WORKER_HOTKEY: 7},
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"netuid": -1}, "netuid"),
        ({"burn_uid": -1}, "burn UID"),
    ],
)
def test_canary_rejects_negative_chain_ids(kwargs, message) -> None:
    with pytest.raises(CanaryError, match=message):
        run_canary(
            _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": 1.0}]),
            _body(42, []),
            {WORKER_HOTKEY: 7},
            **kwargs,
        )


def test_stale_registration_snapshot_fails_before_positive_acceptance() -> None:
    with pytest.raises(CanaryError, match="registration_snapshot_unavailable"):
        run_canary(
            _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": 1.0}]),
            _body(42, []),
            {WORKER_HOTKEY: 7},
            _metagraph_age_secs=7200,
        )


def test_inherited_publisher_environment_cannot_escape_isolation(
    tmp_path, monkeypatch
) -> None:
    poison_blob_dir = tmp_path / "deployed-blob-dir"
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-never-be-used.invalid/live")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_DIR", str(poison_blob_dir))
    monkeypatch.setenv("CATHEDRAL_BLOB_DIR", str(poison_blob_dir))
    monkeypatch.setenv("CATHEDRAL_HIPPIUS_TOKEN", "inherited-production-token")
    monkeypatch.setenv("CATHEDRAL_HIPPIUS_BUCKET", "inherited-production-bucket")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "inherited-seed")
    monkeypatch.delenv("CATHEDRAL_PERMINER_SEED_SECRET", raising=False)

    summary = run_canary(
        _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": 1.0}]),
        _body(42, []),
        {WORKER_HOTKEY: 7},
    )

    assert summary["status"] == "passed"
    assert not poison_blob_dir.exists()
    assert "CATHEDRAL_PERMINER_SEED_SECRET" not in os.environ
    assert os.environ["CATHEDRAL_V2_BLOB_DIR"] == str(poison_blob_dir)
    assert os.environ["CATHEDRAL_HIPPIUS_TOKEN"] == "inherited-production-token"


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("positive", "network", "test", "positive report network"),
        ("revoke", "network", "test", "revoke report network"),
        ("positive", "netuid", 40, "positive report netuid"),
        ("revoke", "netuid", None, "revoke report netuid"),
    ],
)
def test_reports_are_bound_to_exact_target_subnet(target, field, value, message) -> None:
    reports = {
        "positive": json.loads(
            _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": 1.0}])
        ),
        "revoke": json.loads(_body(42, [])),
    }
    if value is None:
        reports[target].pop(field)
    else:
        reports[target][field] = value
    with pytest.raises(CanaryError, match=message):
        run_canary(
            json.dumps(reports["positive"]).encode(),
            json.dumps(reports["revoke"]).encode(),
            {WORKER_HOTKEY: 7},
        )


@pytest.mark.parametrize(
    ("value", "missing", "message"),
    [
        (41.5, False, "integer epochs"),
        ("41", False, "integer epochs"),
        (True, False, "integer epochs"),
        (None, True, "integer epochs"),
        (-1, False, "non-negative"),
    ],
)
def test_report_epoch_contract_is_exact(value, missing, message) -> None:
    positive = json.loads(
        _body(41, [{"miner_hotkey": WORKER_HOTKEY, "score": 1.0}])
    )
    if missing:
        positive.pop("epoch")
    else:
        positive["epoch"] = value
    with pytest.raises(CanaryError, match=message):
        run_canary(
            json.dumps(positive).encode(),
            _body(42, []),
            {WORKER_HOTKEY: 7},
        )


@pytest.mark.parametrize(
    ("kind", "event"),
    [
        ("tcp", "socket.__new__"),
        ("udp", "socket.__new__"),
        ("unix", "socket.connect"),
        ("process", "subprocess.Popen"),
    ],
)
def test_disposable_child_blocks_real_egress_primitives(kind, event) -> None:
    assert canary._probe_egress_guard(kind) == event
