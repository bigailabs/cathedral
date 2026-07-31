"""One test must not spend another test's rate-limit budget.

``ratelimit._state`` is a process-wide singleton keyed on client IP, and under
TestClient every request in the session reports the same client. Without the
autouse reset in ``conftest.py`` the whole suite shares one 120-request,
60-second budget, so tests that ran late in a session got 429s earned by tests
that ran early. Measured cost: 34 of 62 failures in a one-process run,
concentrated in two files that both pass alone.

Those two tests below are an ordered pair, and the order is the point: the
first spends the entire budget, the second asserts it starts clean anyway.
Delete the fixture and the second one fails -- which is the property being
guarded, since the bug was never visible in any single test.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from scaffold.publisher import ratelimit

# Not exempt (see ratelimit._EXEMPT_SUFFIXES) so it actually reaches the limiter.
PROBE_PATH = "/v1/agents/submit"


def _client() -> TestClient:
    app = Starlette(
        routes=[Route(PROBE_PATH, lambda request: PlainTextResponse("ok"))],
    )
    app.add_middleware(ratelimit.RateLimitMiddleware)
    return TestClient(app)


def _limit() -> int:
    return ratelimit._ratelimit_rpm()


def test_the_limiter_still_throttles_within_one_test():
    """The reset must not turn the limiter off -- only scope it to a test.

    Guards the obvious wrong fix (setting CATHEDRAL_RATELIMIT_RPM=0 in the
    suite), which would make the failures go away and the middleware untested.
    """
    limit = _limit()
    if limit <= 0:
        pytest.skip("global limiter disabled via CATHEDRAL_RATELIMIT_RPM")

    with _client() as client:
        for _ in range(limit):
            assert client.get(PROBE_PATH).status_code == 200
        throttled = client.get(PROBE_PATH)

    assert throttled.status_code == 429
    assert throttled.text == "rate_limited"


def test_a_following_test_starts_with_a_fresh_budget():
    """Runs immediately after the test that exhausted the window.

    Same process, same client IP, same 60-second window -- so on the shared
    singleton this request is the one that got the 429. It passes only because
    the state is reset between tests.
    """
    if _limit() <= 0:
        pytest.skip("global limiter disabled via CATHEDRAL_RATELIMIT_RPM")

    assert not ratelimit._state._entries, (
        "the global limiter carried per-key state into this test; the autouse "
        "reset in conftest.py is not running or no longer clears _state"
    )

    with _client() as client:
        assert client.get(PROBE_PATH).status_code == 200


def test_reset_clears_the_abuse_limiter_too():
    """The abuse limiter is default-off, so it leaks only where a test enables it.

    That is narrower than the global limiter but the same failure shape, and it
    is the one a reader is most likely to forget when adding a third limiter.
    """
    ratelimit._abuse_state.check("ip:1.2.3.4", 1, retry_base=1, retry_max=1)
    assert ratelimit._abuse_state._entries

    ratelimit.reset_state_for_tests()

    assert not ratelimit._abuse_state._entries
    assert not ratelimit._state._entries
    assert ratelimit.unresolved_ip_count() == 0
