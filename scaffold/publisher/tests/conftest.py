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


@pytest.fixture(autouse=True)
def _isolate_process_wide_rate_limiters():
    """Give every test its own rate-limit budget.

    ``ratelimit._state`` and ``ratelimit._abuse_state`` are process-wide and
    keyed on client IP, which under TestClient is the same ``testclient`` for
    every request in the session. One key, one 60-second window, ~1300 tests:
    the suite spent the 120-request budget within the first few modules and
    then handed 429s to whatever ran next.

    The cost was 34 of 62 failures in a one-process run, concentrated in
    ``test_submit_admission.py`` (16) and ``test_pm_submit_async.py`` (16) --
    both of which pass cleanly on their own. Because the budget is spent
    against a wall clock, the failures also moved with machine speed, so the
    "inherited" baseline the advisory CI job compares against was never stable
    enough to tell a new failure from an old one.

    Reset before AND after: before so a test never inherits a spent budget,
    after so the last test in a session does not leave one behind for whatever
    a future runner (xdist worker reuse, an interactive session) starts next.
    """
    from scaffold.publisher import ratelimit

    ratelimit.reset_state_for_tests()
    yield
    ratelimit.reset_state_for_tests()
