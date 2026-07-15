"""Startup env-pinning for the V2 per-miner surface (pin_v2_pm_env).

The V2 per-miner handlers and the verify worker used to take v2_pipeline's
process-global _PM_ENV_LOCK on every call (v2_pm_env bridges the
CATHEDRAL_V2_PERMINER_* names onto the legacy names per_miner reads directly),
serializing all V2 per-miner traffic to one request at a time per process.
pin_v2_pm_env() copies the mapping into os.environ once at startup instead,
making v2_pm_env() a lock-free no-op. Guards covered here:
  - implied by CATHEDRAL_LAUNCH_PROFILE=v2-converged, or explicit opt-in via
    CATHEDRAL_V2_PERMINER_ENV_PIN (default off outside the profile: zero change);
  - refuses when unprefixed CATHEDRAL_PERMINER_ENABLED is truthy (the V1
    per-miner surface is active in-process);
  - refuses when a mapped legacy name is set to a conflicting value;
  - never pins CATHEDRAL_PERMINER_ENABLED itself (that would enable the V1
    routes and could flip pm_primary legacy scoring) -- the V2 handlers gate
    on v2_perminer_enabled() instead;
  - missing seed secret still fails closed (503) on the V2 endpoints in both
    pinned and bridged modes.
"""
from __future__ import annotations

import os
import threading

import pytest

from scaffold.publisher import per_miner as pm
from scaffold.publisher import v2_pipeline
from scaffold.publisher.app import build_app


@pytest.fixture(autouse=True)
def _clean_pin_env(monkeypatch):
    for legacy, v2_name in v2_pipeline._V2_PM_ENV_MAP.items():
        monkeypatch.delenv(legacy, raising=False)
        monkeypatch.delenv(v2_name, raising=False)
    monkeypatch.delenv("CATHEDRAL_V2_PERMINER_ENV_PIN", raising=False)
    monkeypatch.delenv("CATHEDRAL_LAUNCH_PROFILE", raising=False)
    monkeypatch.setattr(v2_pipeline, "_PM_ENV_PINNED", False)


def test_pin_default_off(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "pin-test-seed")
    assert v2_pipeline.pin_v2_pm_env() is False
    assert v2_pipeline._PM_ENV_PINNED is False
    assert "CATHEDRAL_PERMINER_SEED_SECRET" not in os.environ


def test_pin_copies_values_except_enabled(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "pin-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "7")
    assert v2_pipeline.pin_v2_pm_env() is True
    assert v2_pipeline._PM_ENV_PINNED is True
    assert os.environ["CATHEDRAL_PERMINER_SEED_SECRET"] == "pin-test-seed"
    assert os.environ["CATHEDRAL_PERMINER_ALLOTMENT_T1"] == "7"
    # ENABLED is never pinned: the V1 surface must stay off.
    assert "CATHEDRAL_PERMINER_ENABLED" not in os.environ
    # per_miner sees the pinned config without the bridge ...
    assert pm.allotment_for(1) == 7
    assert pm.seed_secret_configured() is True
    # ... but the V1 gate stays closed while the V2 gate is open.
    assert pm.perminer_enabled() is False
    assert v2_pipeline.v2_perminer_enabled() is True


def test_converged_profile_pins_canonical_env_without_extra_flag(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_LAUNCH_PROFILE", "v2-converged")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SEED_SECRET", "canonical-seed")
    monkeypatch.setenv("CATHEDRAL_PERMINER_ALLOTMENT_T1", "7")
    assert v2_pipeline.pin_v2_pm_env() is True
    assert v2_pipeline._PM_ENV_PINNED is True
    assert os.environ["CATHEDRAL_PERMINER_SEED_SECRET"] == "canonical-seed"
    assert os.environ["CATHEDRAL_PERMINER_ALLOTMENT_T1"] == "7"
    assert "CATHEDRAL_PERMINER_ENABLED" not in os.environ
    assert pm.allotment_for(1) == 7
    assert pm.seed_secret_configured() is True
    assert pm.perminer_enabled() is False
    assert v2_pipeline.v2_perminer_enabled() is True


def test_converged_profile_pinned_v2_pm_env_is_lock_free(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_LAUNCH_PROFILE", "v2-converged")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SEED_SECRET", "canonical-seed")
    assert v2_pipeline.pin_v2_pm_env() is True
    entered = threading.Event()

    def use_env():
        with v2_pipeline.v2_pm_env():
            entered.set()

    with v2_pipeline._PM_ENV_LOCK:
        t = threading.Thread(target=use_env)
        t.start()
        assert entered.wait(timeout=5.0), "v2_pm_env blocked on _PM_ENV_LOCK despite profile pin"
        t.join(timeout=5.0)


def test_converged_profile_pin_refuses_when_v1_perminer_enabled(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_LAUNCH_PROFILE", "v2-converged")
    monkeypatch.setenv("CATHEDRAL_PERMINER_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SEED_SECRET", "canonical-seed")
    assert v2_pipeline.pin_v2_pm_env() is False
    assert v2_pipeline._PM_ENV_PINNED is False


def test_converged_profile_pin_refuses_on_conflicting_v2_twin(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_LAUNCH_PROFILE", "v2-converged")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SEED_SECRET", "canonical-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "v2-twin-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "7")
    assert v2_pipeline.pin_v2_pm_env() is False
    assert v2_pipeline._PM_ENV_PINNED is False
    # Refusal happens before copying any V2 twin into the canonical env.
    assert "CATHEDRAL_PERMINER_ALLOTMENT_T1" not in os.environ
    assert os.environ["CATHEDRAL_PERMINER_SEED_SECRET"] == "canonical-seed"


def test_pin_ignores_falsy_legacy_enabled(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "pin-test-seed")
    # Set-but-falsy legacy ENABLED is not "V1 active", and although it differs
    # from the V2 value it is excluded from the conflict check (ENABLED is
    # never pinned, so there is nothing to conflict with).
    monkeypatch.setenv("CATHEDRAL_PERMINER_ENABLED", "0")
    assert v2_pipeline.pin_v2_pm_env() is True
    assert os.environ["CATHEDRAL_PERMINER_ENABLED"] == "0"
    assert v2_pipeline.v2_perminer_enabled() is True


def test_pin_refuses_when_v1_perminer_enabled(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", "1")
    monkeypatch.setenv("CATHEDRAL_PERMINER_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "pin-test-seed")
    assert v2_pipeline.pin_v2_pm_env() is False
    assert v2_pipeline._PM_ENV_PINNED is False
    assert "CATHEDRAL_PERMINER_SEED_SECRET" not in os.environ


def test_pin_refuses_on_conflicting_legacy_value(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "v2-seed")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SEED_SECRET", "operator-set-legacy-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "7")
    assert v2_pipeline.pin_v2_pm_env() is False
    assert v2_pipeline._PM_ENV_PINNED is False
    # Nothing was copied -- the refusal happens before any mutation.
    assert "CATHEDRAL_PERMINER_ALLOTMENT_T1" not in os.environ
    assert os.environ["CATHEDRAL_PERMINER_SEED_SECRET"] == "operator-set-legacy-seed"


def test_pin_allows_equal_or_unmapped_legacy_values(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", "1")
    # Equal value on both names is not a conflict.
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "same-seed")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SEED_SECRET", "same-seed")
    # Legacy set with no V2 counterpart: left alone (v2_pm_env would not have
    # touched it either).
    monkeypatch.setenv("CATHEDRAL_PERMINER_ALLOTMENT_T1", "9")
    assert v2_pipeline.pin_v2_pm_env() is True
    assert os.environ["CATHEDRAL_PERMINER_ALLOTMENT_T1"] == "9"
    assert os.environ["CATHEDRAL_PERMINER_SEED_SECRET"] == "same-seed"


def test_pinned_v2_pm_env_is_lock_free(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "pin-test-seed")
    assert v2_pipeline.pin_v2_pm_env() is True
    entered = threading.Event()

    def use_env():
        with v2_pipeline.v2_pm_env():
            entered.set()

    # Hold the process-global lock on this thread: a pinned v2_pm_env() on
    # another thread must complete anyway.
    with v2_pipeline._PM_ENV_LOCK:
        t = threading.Thread(target=use_env)
        t.start()
        assert entered.wait(timeout=5.0), "v2_pm_env blocked on _PM_ENV_LOCK despite pin"
        t.join(timeout=5.0)


def test_unpinned_v2_pm_env_still_takes_lock(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "pin-test-seed")
    entered = threading.Event()

    def use_env():
        with v2_pipeline.v2_pm_env():
            entered.set()

    with v2_pipeline._PM_ENV_LOCK:
        t = threading.Thread(target=use_env)
        t.start()
        # The bridge path serializes on the lock -- the documented pre-pin
        # behavior this PR leaves in place as the fallback.
        assert not entered.wait(timeout=0.3)
    t.join(timeout=5.0)
    assert entered.is_set()


def test_v2_perminer_enabled_matches_per_miner_truthiness(monkeypatch):
    for val in ("1", "true", "yes", "on", " True ", "YES"):
        monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", val)
        assert v2_pipeline.v2_perminer_enabled() is True, val
    for val in ("0", "false", "off", "no", "", "banana"):
        monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", val)
        assert v2_pipeline.v2_perminer_enabled() is False, val
    # Fallback to the unprefixed name must agree with per_miner exactly.
    monkeypatch.delenv("CATHEDRAL_V2_PERMINER_ENABLED", raising=False)
    for val in ("1", "true", "yes", "on", "0", "off", "no", "", "banana"):
        monkeypatch.setenv("CATHEDRAL_PERMINER_ENABLED", val)
        assert v2_pipeline.v2_perminer_enabled() == pm.perminer_enabled(), val
    monkeypatch.delenv("CATHEDRAL_PERMINER_ENABLED", raising=False)
    assert v2_pipeline.v2_perminer_enabled() is False
    # Presence beats fallback: a V2 name set-but-empty disables the surface
    # even when the legacy name is truthy (matches the v2_pm_env bridge, which
    # copies the empty value over the legacy name).
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "")
    monkeypatch.setenv("CATHEDRAL_PERMINER_ENABLED", "1")
    assert v2_pipeline.v2_perminer_enabled() is False


@pytest.mark.parametrize("pin", ["0", "1"])
def test_missing_seed_secret_fails_closed_503(tmp_path, monkeypatch, pin):
    """V2 enabled without a seed secret must 503, pinned or bridged.

    pm.require_seed_secret() is vacuous when the unprefixed ENABLED is unset
    (the pinned case), so the handlers use _require_v2_perminer_ready -- this
    guards that the fail-closed contract survives the pin.
    """
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENV_PIN", pin)
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    for name in ("CATHEDRAL_V2_PERMINER_SEED_SECRET", "CATHEDRAL_REFILL_SEED_SECRET",
                 "CATHEDRAL_PUBLISHER_SEED_SECRET"):
        monkeypatch.delenv(name, raising=False)

    from starlette.testclient import TestClient

    app = build_app(database_path=str(tmp_path / "pub.sqlite"),
                    signing_key_hex="11" * 32)
    assert v2_pipeline._PM_ENV_PINNED is (pin == "1")
    client = TestClient(app)
    resp = client.get(
        "/v2/synthetic-boolean/per-miner/challenges",
        headers={
            "X-Cathedral-Hotkey": "hk-placeholder",
            "X-Cathedral-Signature": "sig-placeholder",
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "per_miner_seed_secret_missing"
