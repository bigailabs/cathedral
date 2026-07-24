"""The hot per-miner read paths must be exempt from the coarse global RPM
limiter, for BOTH v1 and v2. They have their own pm_read_hard_cap concurrency
gate; the per-IP RPM limiter (keyed on a shared upstream IP on the un-proxied
v2 origin) otherwise throttled the whole fast-path fleet into one bucket
(reported live: /v2/.../challenges -> 429 on ~4 of 5 requests, global not
per-IP). See issue #337.
"""
from __future__ import annotations

from scaffold.publisher import ratelimit


def test_v2_per_miner_reads_are_exempt_from_global_limiter():
    assert ratelimit._is_exempt("/v2/synthetic-boolean/per-miner/challenges")
    assert ratelimit._is_exempt("/v2/synthetic-boolean/per-miner/cnf")
    assert ratelimit._is_exempt("/v2/synthetic-boolean/per-miner/cnf-access")


def test_v1_per_miner_reads_still_exempt():
    assert ratelimit._is_exempt("/v1/synthetic-boolean/per-miner/challenges")
    assert ratelimit._is_exempt("/v1/synthetic-boolean/per-miner/cnf")


def test_legacy_prefixed_v2_path_also_exempt():
    # middleware runs before the /api/cathedral prefix strip, so both forms count
    assert ratelimit._is_exempt(
        "/api/cathedral/v2/synthetic-boolean/per-miner/challenges")


def test_v2_writes_are_NOT_exempt():
    # submit/blob write paths must still hit the limiter
    assert not ratelimit._is_exempt("/v2/agents/submit-bitset")
    assert not ratelimit._is_exempt("/v2/agents/submit-manifest")
    assert not ratelimit._is_exempt("/v2/blobs/solutions")


def test_v2_per_miner_reads_are_abuse_targets():
    # when the abuse limiter is enabled, v2 hot reads get graduated backoff too
    assert ("GET", "/v2/synthetic-boolean/per-miner/challenges") in ratelimit._ABUSE_TARGETS
    assert ("GET", "/v2/synthetic-boolean/per-miner/cnf") in ratelimit._ABUSE_TARGETS
    assert ("GET", "/v2/synthetic-boolean/per-miner/cnf-access") in ratelimit._ABUSE_TARGETS
