import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_v2_pm_env_pin():
    """pin_v2_pm_env() is once-per-process by design (build_app startup); each
    test that builds an app simulates a fresh process. Restore the pin flag and
    the mapped legacy env names around every test so a suite run with
    CATHEDRAL_V2_PERMINER_ENV_PIN=1 does not leak one test's pinned values
    into the next.
    """
    from scaffold.publisher import v2_pipeline

    saved_flag = v2_pipeline._PM_ENV_PINNED
    saved_env = {name: os.environ.get(name) for name in v2_pipeline._V2_PM_ENV_MAP}
    yield
    v2_pipeline._PM_ENV_PINNED = saved_flag
    for name, value in saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _stable_cnf_token_secret(monkeypatch):
    """Submit-role apps fail closed without a stable CNF token secret (production
    contract enforced by ``_cnf_token_secret`` / ``publisher_verify.py``: split
    submit replicas must share one secret so a CNF token minted on one validates
    on another). Provide a fixed test secret so any test that builds a
    submit-role app can construct it. A test that specifically exercises the
    unset/fail-closed path can ``monkeypatch.delenv`` it.
    """
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-cnf-token-secret")
