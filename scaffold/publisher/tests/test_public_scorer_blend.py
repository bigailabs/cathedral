"""Regression and property tests for the public scorer external-score blend.

These tests cover the frozen contract for external score blending into the
Cathedral signed vector.

Required tests:
1. fraction=.10, 1 base + 100 equal external => base .9, each ext .001
2. empty base + external scores => no external miner payout
3. base only => base receives all miner mass
4. overlapping hotkey => sum of its two contributions, mechanism masses .9/.1
5. newer zero revokes older 1.0; omission in newer complete snapshot also revokes
6. stale, future, repeated, out-of-order epochs fail as specified
7. external outage/expired snapshot degrades to base-only
8. random positive vectors never realize external mass above configured fraction
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from scaffold import validator_thin
from scaffold.publisher import external_scores, weights
from scaffold.publisher.store import Store


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{dt.microsecond // 1000:03d}Z"
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure every test starts with external scoring disabled (default)."""
    for k in (
        "CATHEDRAL_EXTERNAL_SCORES_ENABLED",
        "CATHEDRAL_EXTERNAL_SCORES_SOURCE",
        "CATHEDRAL_EXTERNAL_SCORES_MODE",
        "CATHEDRAL_EXTERNAL_SCORES_FRACTION",
        "CATHEDRAL_EXTERNAL_SCORES_WEIGHT",
        "CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT",
        "CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM",
        "CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION",
        "CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED",
        "CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS",
        "CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_AGE_SECS",
        "CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_FUTURE_SECS",
        "CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED",
        "CATHEDRAL_VALIDATED_SUPPLY_ENABLED",
        "CATHEDRAL_WEIGHT_POLICY_BURN_HOTKEY",
        "CATHEDRAL_WEIGHT_POLICY_BURN_UID",
        "CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def store(tmp_path):
    """Fresh SQLite-backed store per test."""
    return Store(str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeStore:
    """Minimal store mock for _apply_external_scores tests.

    Supports the latest_snapshot_scores query pattern (report + entries),
    registration gate (metagraph_hotkeys), and recent_scores legacy path.
    """

    def __init__(
        self,
        ext_scores: list[tuple[str, float]],
        meta_hotkeys: list[str],
        *,
        complete: bool = True,
        epoch: int = 1,
        generated_at: str | None = None,
        source: str = "violet_audio",
        report_exists: bool = True,
    ):
        now = _now()
        self._generated_at = generated_at or _iso(now)
        self._source = source
        self._epoch = epoch
        self._complete = complete
        self._report_id = "test-report-1"
        self._report_exists = report_exists and bool(ext_scores)
        report_obj = {
            "source": source,
            "epoch": epoch,
            "complete": complete,
            "generated_at": self._generated_at,
            "scores": [{"miner_hotkey": hk, "score": s} for hk, s in ext_scores],
        }
        self._report_json = json.dumps(report_obj)
        self._ext_scores = ext_scores
        self._meta_hotkeys = meta_hotkeys
        self._meta_updated = _iso(now)

    def query(self, sql, params):
        if "FROM external_score_reports" in sql:
            if not self._report_exists:
                return []
            return [
                {
                    "id": self._report_id,
                    "epoch": self._epoch,
                    "generated_at_iso": self._generated_at,
                    "received_at_iso": self._generated_at,
                    "report_json": self._report_json,
                    "report_sha256": "fake",
                    "score_count": len(self._ext_scores),
                }
            ]
        if "FROM external_score_entries" in sql:
            if "report_id" in sql:
                return [{"miner_hotkey": hk, "score": s} for hk, s in self._ext_scores]
            cutoff = params[1] if len(params) > 1 else ""
            return [
                {"miner_hotkey": hk, "score": s, "received_at_iso": self._generated_at}
                for hk, s in self._ext_scores
                if s > 0 and self._generated_at > str(cutoff)
            ]
        if "FROM metagraph_hotkeys" in sql:
            cutoff = params[2]
            return [
                {"hotkey": hk, "updated_at_iso": self._meta_updated}
                for hk in self._meta_hotkeys
                if self._meta_updated > str(cutoff)
            ]
        return []

    def write(self, fn):
        raise NotImplementedError


def _enable_blend(monkeypatch, fraction: float = 0.10):
    """Enable external scoring with an explicit fraction."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "violet_audio")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", str(fraction))
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED", "0")


def _make_report(
    scores: list[tuple[str, float]],
    *,
    source: str = "violet_audio",
    epoch: int = 1,
    complete: bool = True,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    return external_scores.normalize_report(
        {
            "source": source,
            "epoch": epoch,
            "complete": complete,
            "generated_at": _iso(now),
            "scores": [{"miner_hotkey": hk, "score": s} for hk, s in scores],
        },
        now=now,
    )


# ===========================================================================
# Test 1: fraction=.10, one base miner and 100 equal external miners
# ===========================================================================


def test_fraction_10pct_base_and_100_external(monkeypatch, capsys):
    """base contribution .9, each external .001, tolerance 1e-9."""
    _enable_blend(monkeypatch, 0.10)
    base_hk = "5BASE_MINER_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ext_hks = [f"5EXT_{i:04d}_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" for i in range(100)]
    all_hks = [base_hk] + ext_hks
    ext_scores_list = [(hk, 1.0) for hk in ext_hks]
    fake = FakeStore(ext_scores_list, all_hks)  # all registered
    base = {base_hk: 1.0}
    now = _now()
    result, meta = weights._apply_external_scores(fake, base, now=now)
    assert capsys.readouterr().out == "[weights] external_scores blend applied\n"
    assert meta["blended"] is True

    # L1-normalize: base has 1 miner with score 1.0 -> base_norm = {base: 1.0}
    # ext has 100 miners each 1.0 -> after identity collapse all equal ->
    # ext_norm each = 1/100 = 0.01
    # blend: base_hk = 0.9 * 1.0 + 0.1 * 0.0 = 0.9
    # each ext = 0.9 * 0.0 + 0.1 * 0.01 = 0.001
    base_contribution = result[base_hk]
    assert abs(base_contribution - 0.9) < 1e-9, (
        f"base contribution {base_contribution} != 0.9"
    )
    for hk in ext_hks:
        ext_contribution = result[hk]
        assert abs(ext_contribution - 0.001) < 1e-9, (
            f"ext contribution {ext_contribution} != 0.001"
        )
    # Total mass should be 1.0
    total = sum(result.values())
    assert abs(total - 1.0) < 1e-9, f"total mass {total} != 1.0"
    # Mechanism masses
    assert abs(meta["base_mass"] - 0.9) < 1e-9
    assert abs(meta["external_mass"] - 0.1) < 1e-9


# ===========================================================================
# Test 2: empty base + external scores => no external miner payout
# ===========================================================================


def test_empty_base_plus_external_fails_closed(monkeypatch):
    """External-only must never expand to 100%; fail closed to empty."""
    _enable_blend(monkeypatch, 0.10)
    ext_hks = [f"5EXT_{i}_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" for i in range(5)]
    ext_list = [(hk, 1.0) for hk in ext_hks]
    fake = FakeStore(ext_list, ext_hks)
    base: dict[str, float] = {}
    result, meta = weights._apply_external_scores(fake, base, now=_now())
    assert result == {}, "external-only must fail closed to empty"
    assert meta.get("degraded") == "external_only_fail_closed"


# ===========================================================================
# Test 3: base only => base receives all miner mass
# ===========================================================================


def test_base_only_gets_all_mass(monkeypatch):
    """When no external scores exist, base should pass through unchanged."""
    _enable_blend(monkeypatch, 0.10)
    base = {"5MINER_A": 0.7, "5MINER_B": 0.3}
    # No external scores
    fake = FakeStore([], [])
    result, meta = weights._apply_external_scores(fake, base, now=_now())
    assert result == base
    assert meta["base_mass"] == 1.0
    assert meta["external_mass"] == 0.0


# ===========================================================================
# Test 4: overlapping hotkey receives sum of its two contributions
# ===========================================================================


def test_overlapping_hotkey_sum_of_contributions(monkeypatch):
    """A hotkey in both base and external gets its portion from each mechanism.
    Mechanism masses must remain .9/.1."""
    _enable_blend(monkeypatch, 0.10)
    overlap_hk = "5OVERLAP_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    base_only_hk = "5BASEONLY_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ext_only_hk = "5EXTONLY_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    all_hks = [overlap_hk, base_only_hk, ext_only_hk]
    base = {overlap_hk: 1.0, base_only_hk: 1.0}
    ext_list = [(overlap_hk, 1.0), (ext_only_hk, 1.0)]
    fake = FakeStore(ext_list, all_hks)
    result, meta = weights._apply_external_scores(fake, base, now=_now())
    assert meta["blended"] is True
    assert abs(meta["base_mass"] - 0.9) < 1e-9
    assert abs(meta["external_mass"] - 0.1) < 1e-9
    # base_norm: overlap=0.5, base_only=0.5 (L1-normalized)
    # ext_norm: overlap=0.5, ext_only=0.5 (L1-normalized)
    # overlap = 0.9*0.5 + 0.1*0.5 = 0.45 + 0.05 = 0.50
    # base_only = 0.9*0.5 + 0.1*0.0 = 0.45
    # ext_only = 0.9*0.0 + 0.1*0.5 = 0.05
    assert abs(result[overlap_hk] - 0.50) < 1e-9, (
        f"overlap {result[overlap_hk]} != 0.50"
    )
    assert abs(result[base_only_hk] - 0.45) < 1e-9, (
        f"base_only {result[base_only_hk]} != 0.45"
    )
    assert abs(result[ext_only_hk] - 0.05) < 1e-9, (
        f"ext_only {result[ext_only_hk]} != 0.05"
    )
    total = sum(result.values())
    assert abs(total - 1.0) < 1e-9


# ===========================================================================
# Test 5: newer zero revokes; omission in newer complete snapshot also revokes
# ===========================================================================


def test_newer_zero_revokes_older_positive(store):
    """A newer epoch with score=0 for a hotkey revokes a prior positive score."""
    now = _now()
    source = "violet_audio"
    hk_keep = "5KEEP_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    hk_revoke = "5REVOKE_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    # Epoch 1: both positive
    r1 = _make_report(
        [(hk_keep, 1.0), (hk_revoke, 0.8)],
        source=source,
        epoch=1,
        complete=True,
        now=now,
    )
    external_scores.store_report(store, r1)

    # Epoch 2: revoke by explicit zero
    r2 = _make_report(
        [(hk_keep, 1.0), (hk_revoke, 0.0)],
        source=source,
        epoch=2,
        complete=True,
        now=now,
    )
    external_scores.store_report(store, r2)

    scores = external_scores.latest_snapshot_scores(
        store, source=source, max_age_secs=3600, now=now
    )
    assert scores is not None
    assert hk_keep in scores
    assert hk_revoke not in scores, "explicit zero must revoke"


def test_omission_in_newer_snapshot_revokes(store):
    """Omission in a newer complete snapshot also revokes."""
    now = _now()
    source = "violet_audio"
    hk_keep = "5KEEP_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    hk_gone = "5GONE_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    r1 = _make_report(
        [(hk_keep, 1.0), (hk_gone, 0.8)],
        source=source,
        epoch=1,
        complete=True,
        now=now,
    )
    external_scores.store_report(store, r1)

    # Epoch 2: hk_gone is simply omitted
    r2 = _make_report(
        [(hk_keep, 0.9)],
        source=source,
        epoch=2,
        complete=True,
        now=now,
    )
    external_scores.store_report(store, r2)

    scores = external_scores.latest_snapshot_scores(
        store, source=source, max_age_secs=3600, now=now
    )
    assert scores is not None
    assert hk_keep in scores
    assert hk_gone not in scores, "omitted hotkey must be revoked in complete snapshot"


# ===========================================================================
# Test 6: stale, future, repeated, out-of-order epochs fail as specified
# ===========================================================================


def test_stale_report_rejected():
    """generated_at older than max age must be rejected."""
    now = _now()
    old = now - timedelta(seconds=7200)  # 2 hours ago, max age is 3600s
    with pytest.raises(external_scores.ExternalScoreError, match="report_too_old"):
        external_scores.normalize_report(
            {
                "source": "violet_audio",
                "epoch": 1,
                "complete": True,
                "generated_at": _iso(old),
                "scores": [{"miner_hotkey": "5A", "score": 0.5}],
            },
            now=now,
        )


def test_future_report_rejected():
    """generated_at too far in the future must be rejected."""
    now = _now()
    future = now + timedelta(seconds=300)  # 5 min ahead, max future is 120s
    with pytest.raises(external_scores.ExternalScoreError, match="report_in_future"):
        external_scores.normalize_report(
            {
                "source": "violet_audio",
                "epoch": 1,
                "complete": True,
                "generated_at": _iso(future),
                "scores": [{"miner_hotkey": "5A", "score": 0.5}],
            },
            now=now,
        )


def test_repeated_epoch_same_digest_is_idempotent(store):
    """Byte-identical retry at the same epoch is idempotent."""
    now = _now()
    report = _make_report(
        [("5A", 1.0)], source="violet_audio", epoch=5, complete=True, now=now
    )
    r1 = external_scores.store_report(store, report)
    assert r1["status"] == "accepted"
    assert r1.get("idempotent") is not True

    # Same report again
    r2 = external_scores.store_report(store, report)
    assert r2["status"] == "accepted"
    assert r2["idempotent"] is True


def test_older_epoch_rejected(store):
    """A report with an older epoch than already stored must be rejected."""
    now = _now()
    r_new = _make_report(
        [("5A", 1.0)], source="violet_audio", epoch=10, complete=True, now=now
    )
    external_scores.store_report(store, r_new)

    r_old = _make_report(
        [("5A", 0.5)], source="violet_audio", epoch=5, complete=True, now=now
    )
    with pytest.raises(external_scores.ExternalScoreError, match="epoch_too_old"):
        external_scores.store_report(store, r_old)


def test_conflicting_epoch_rejected(store):
    """Same epoch but different content (different digest) must be rejected."""
    now = _now()
    r1 = _make_report(
        [("5A", 1.0)], source="violet_audio", epoch=5, complete=True, now=now
    )
    external_scores.store_report(store, r1)

    r2 = _make_report(
        [("5A", 0.5)], source="violet_audio", epoch=5, complete=True, now=now
    )
    with pytest.raises(external_scores.ExternalScoreError, match="epoch_conflict"):
        external_scores.store_report(store, r2)


def test_postgres_epoch_fence_locks_exact_audience_before_latest_read(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETWORK", "finney")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETUID", "39")
    now = _now()
    report = external_scores.normalize_report(
        {
            "complete": True,
            "epoch": 7,
            "generated_at": _iso(now),
            "network": "finney",
            "netuid": 39,
            "scores": [{"miner_hotkey": "5A", "score": 1.0}],
            "source": "cathedral_confidential_tdx",
        },
        now=now,
    )
    report = external_scores.bind_authenticated_body(report, b"postgres-fence-fixture")

    class Cursor:
        @staticmethod
        def fetchone():
            return None

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))
            return Cursor()

    class PostgresStore:
        backend = "postgres"

        def __init__(self):
            self.connection = Connection()

        def write(self, fn):
            return fn(self.connection)

    postgres = PostgresStore()
    accepted = external_scores.store_report(postgres, report)

    assert accepted["status"] == "accepted"
    assert postgres.connection.calls[0][0] == "SELECT pg_advisory_xact_lock(?)"
    lock_key = postgres.connection.calls[0][1][0]
    assert -(2**63) <= lock_key < 2**63
    assert (
        "WHERE source=? AND network=? AND netuid=?" in postgres.connection.calls[1][0]
    )


# ===========================================================================
# Test 7: external outage/expired snapshot degrades to base-only
# ===========================================================================


def test_expired_snapshot_degrades_to_base_only(monkeypatch):
    """When the external snapshot is stale, the blend must degrade to base-only."""
    _enable_blend(monkeypatch, 0.10)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS", "3600")
    now = _now()
    stale_time = now - timedelta(seconds=7200)  # 2 hours ago
    ext_list = [("5EXT_A", 1.0)]
    all_hks = ["5BASE_A", "5EXT_A"]
    fake = FakeStore(ext_list, all_hks, generated_at=_iso(stale_time))
    base = {"5BASE_A": 1.0}
    result, meta = weights._apply_external_scores(fake, base, now=now)
    # Stale external -> degrades to base-only
    assert result == base, "stale external must degrade to base-only"
    assert meta["base_mass"] == 1.0
    assert meta.get("external_mass", 0.0) == 0.0


def test_no_external_reports_degrades_to_base_only(monkeypatch):
    """No external reports at all -> base-only."""
    _enable_blend(monkeypatch, 0.10)
    fake = FakeStore([], [])
    base = {"5BASE_A": 1.0, "5BASE_B": 0.5}
    result, meta = weights._apply_external_scores(fake, base, now=_now())
    assert result == base


def test_incomplete_snapshot_degrades_to_base_only(monkeypatch):
    """A snapshot without complete=true must not be used for blending."""
    _enable_blend(monkeypatch, 0.10)
    now = _now()
    fake = FakeStore(
        [("5EXT", 1.0)],
        ["5EXT", "5BASE"],
        complete=False,
    )
    base = {"5BASE": 1.0}
    result, meta = weights._apply_external_scores(fake, base, now=now)
    assert result == base, "incomplete snapshot must degrade to base-only"


# ===========================================================================
# Test 8: random positive vectors never realize external mass above fraction
# ===========================================================================


def test_random_vectors_external_mass_bounded(monkeypatch):
    """Property test: for random positive vectors, the realized external mass
    never exceeds the configured fraction."""
    fraction = 0.10
    _enable_blend(monkeypatch, fraction)
    rng = random.Random(42)

    for trial in range(50):
        n_base = rng.randint(1, 20)
        n_ext = rng.randint(1, 50)
        base_hks = [f"5BASE_{trial}_{i}" for i in range(n_base)]
        ext_hks = [f"5EXT_{trial}_{i}" for i in range(n_ext)]
        all_hks = base_hks + ext_hks
        base = {hk: rng.random() * 10 for hk in base_hks}
        ext_list = [(hk, rng.random()) for hk in ext_hks]
        fake = FakeStore(ext_list, all_hks)
        result, meta = weights._apply_external_scores(fake, base, now=_now())
        if not result:
            continue
        total = sum(result.values())
        if total <= 0:
            continue
        # Compute realized external mass: sum of contributions from ext-only
        # hotkeys + external fraction of overlap hotkeys.
        # Since the blend is exact (1-f)*base_norm + f*ext_norm, the total
        # external mass in the output is exactly f (when both have mass).
        if meta.get("blended"):
            realized_ext = meta["external_mass"]
            assert realized_ext <= fraction + 1e-9, (
                f"trial {trial}: external mass {realized_ext} > fraction {fraction}"
            )


# ===========================================================================
# Additional: cathedral_confidential_tdx source is allowed
# ===========================================================================


def test_cathedral_confidential_tdx_source_allowed():
    """The cathedral_confidential_tdx source must be in the allowlist."""
    assert "cathedral_confidential_tdx" in external_scores.ALLOWED_ENDPOINT_SOURCES


def test_default_disabled(monkeypatch):
    """Default must be disabled. Live blending requires explicit fraction."""
    for k in (
        "CATHEDRAL_EXTERNAL_SCORES_ENABLED",
        "CATHEDRAL_EXTERNAL_SCORES_FRACTION",
    ):
        monkeypatch.delenv(k, raising=False)
    assert not weights.external_scores_enabled()


# ===========================================================================
# Executable Proof 1: Payable filtering pre-allocation preserves <=.10 external
# ===========================================================================


class FakeStoreWithPayable(FakeStore):
    """Extended store mock that supports metagraph_hotkeys queries for payability."""

    def __init__(self, *args, payable_hks=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._payable_hks = payable_hks or set()
        self._meta_updated = _iso(_now())

    def query(self, sql, params):
        if "FROM metagraph_hotkeys" in sql:
            # Mimic the metagraph query: returns fresh hotkeys within cutoff
            cutoff = params[2] if len(params) > 2 else ""
            return [
                {"hotkey": hk, "updated_at_iso": self._meta_updated}
                for hk in self._payable_hks
                if self._meta_updated > str(cutoff)
            ]
        return super().query(sql, params)


def test_payable_filter_pre_allocation_preserves_fraction(monkeypatch):
    """Payable filtering MUST occur pre-allocation. When base hotkeys are
    filtered out before allocation, the realized external share remains <=.10.
    Proof: if filtering happened post-blend, removing base-only hotkeys would
    inflate the external share artificially."""
    _enable_blend(monkeypatch, 0.10)
    monkeypatch.setenv("CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS", "filter")
    now = _now()

    # Setup: 50 base miners (all equal), 10 external miners (all equal)
    base_hks = [f"5BASE_{i:03d}_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" for i in range(50)]
    ext_hks = [f"5EXT_{i:02d}_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" for i in range(10)]
    all_hks = base_hks + ext_hks

    # Make 30 base miners unpayable (forced removal)
    payable = set(base_hks[20:]) | set(ext_hks)  # 20 base + 10 ext payable

    base = {hk: 1.0 / len(base_hks) for hk in base_hks}
    ext_list = [(hk, 1.0 / len(ext_hks)) for hk in ext_hks]

    fake = FakeStoreWithPayable(ext_list, list(all_hks), payable_hks=payable)

    result, meta = weights._apply_external_scores(fake, base, now=now)

    assert meta["blended"] is True, "Should blend with both mechanisms present"
    assert abs(meta["external_mass"] - 0.1) < 1e-9, (
        f"external mass {meta['external_mass']} != 0.1 (payable filtering should happen pre-allocation)"
    )

    # Verify only payable miners are in output
    for hk in result:
        assert hk in payable, f"unpayable hotkey {hk} appeared in output"


# ===========================================================================
# Executable Proof 2: cathedral_confidential_tdx requires complete=true
# ===========================================================================


def test_confidential_tdx_requires_complete_true(monkeypatch):
    """The cathedral_confidential_tdx source must never accept an incomplete
    (or legacy omitted-complete) report. This is the whole trust model:
    'the report is the full truth at its epoch.'"""
    now = _now()
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETWORK", "finney")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETUID", "39")

    # Attempt 1: complete omitted (None)
    with pytest.raises(
        external_scores.ExternalScoreError, match="complete_required_for_source"
    ):
        external_scores.normalize_report(
            {
                "source": "cathedral_confidential_tdx",
                "network": "finney",
                "netuid": 39,
                "epoch": 1,
                # complete omitted
                "generated_at": _iso(now),
                "scores": [{"miner_hotkey": "5A", "score": 0.5}],
            },
            now=now,
        )

    # Attempt 2: complete=false
    with pytest.raises(
        external_scores.ExternalScoreError, match="complete_required_for_source"
    ):
        external_scores.normalize_report(
            {
                "source": "cathedral_confidential_tdx",
                "network": "finney",
                "netuid": 39,
                "epoch": 1,
                "complete": False,
                "generated_at": _iso(now),
                "scores": [{"miner_hotkey": "5A", "score": 0.5}],
            },
            now=now,
        )

    # Attempt 3: complete=true succeeds (even with empty scores list)
    report = external_scores.normalize_report(
        {
            "source": "cathedral_confidential_tdx",
            "network": "finney",
            "netuid": 39,
            "epoch": 1,
            "complete": True,
            "generated_at": _iso(now),
            "scores": [],  # Empty: "revoke everyone" is valid for complete=true
        },
        now=now,
    )
    assert report["complete"] is True
    assert report["scores"] == []


# ===========================================================================
# Executable Proof 3: Dedicated source token authorizes ONLY that source
# ===========================================================================


def test_source_token_authorizes_only_that_source(monkeypatch):
    """A per-source dedicated token must authorize ONLY reports claiming
    that source label. The shared token cannot authorize a confidential
    report; the dedicated token cannot authorize another source."""
    # Configure a dedicated token for cathedral_confidential_tdx
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX",
        "dedicated_secret_token",
    )
    # Also set a shared token (should be ignored for confidential source)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared_token")

    # Test 1: Shared token does NOT authorize confidential_tdx
    assert not external_scores.bearer_authorized_for_source(
        "cathedral_confidential_tdx",
        authorization="Bearer shared_token",
    )

    # Test 2: Dedicated token DOES authorize confidential_tdx
    assert external_scores.bearer_authorized_for_source(
        "cathedral_confidential_tdx",
        authorization="Bearer dedicated_secret_token",
    )

    # Test 3: Dedicated token does NOT authorize violet_audio
    assert not external_scores.bearer_authorized_for_source(
        "violet_audio",
        authorization="Bearer dedicated_secret_token",
    )

    # Test 4: violet_audio falls back to shared token
    assert external_scores.bearer_authorized_for_source(
        "violet_audio",
        authorization="Bearer shared_token",
    )


# ===========================================================================
# Executable Proof 4: external_primary for legacy vs confidential source
# ===========================================================================


def test_external_primary_allowed_for_legacy_source(monkeypatch):
    """A legacy source like violet_audio can resolve to external_primary
    when both the mode is set AND EXTERNAL_SCORES_PRIMARY_CONFIRM=true."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "violet_audio")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "external_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", "true")
    assert weights.external_scores_mode() == "external_primary"


def test_external_primary_blocked_for_confidential_source(monkeypatch):
    """A confidential/attested source must NEVER resolve to external_primary,
    even if both the mode and confirm flag are set. It always stays in
    capped-blend mode."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "cathedral_confidential_tdx")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "external_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", "true")
    # Despite mode=external_primary and confirm=true, it must fall back to blend
    assert weights.external_scores_mode() == "blend"


def test_confidential_source_requires_explicit_fraction(monkeypatch):
    """Confidential sources must never silently inherit the legacy 50% default.
    An explicit CATHEDRAL_EXTERNAL_SCORES_FRACTION is REQUIRED; without one,
    the blend fails closed to base-only (fraction=0)."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "cathedral_confidential_tdx")
    # Do NOT set FRACTION
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", raising=False)
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT", raising=False)
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_WEIGHT", raising=False)

    # Must return (1.0, 0.0, 0.0) => base-only
    base_w, ext_w, frac = weights._external_blend_weights()
    assert abs(base_w - 1.0) < 1e-9, "base_weight should be 1.0"
    assert abs(ext_w - 0.0) < 1e-9, "external_weight should be 0.0"
    assert abs(frac - 0.0) < 1e-9, "fraction should be 0.0"


# ===========================================================================
# Executable Proof 5: status/active_score_count reflects latest complete snapshot
# ===========================================================================


def test_status_active_score_count_reflects_latest_snapshot(store):
    """The status API must only count active scores from the latest report,
    and only when it is both COMPLETE and fresher than the cutoff.
    A newer complete zero/omission snapshot revokes everyone -> active=0."""
    now = _now()
    source = "violet_audio"
    cutoff = _iso(now - timedelta(seconds=60))  # Snapshot within 1 min is fresh

    # Epoch 1: 5 miners with positive scores, complete=true
    hks_1 = [(f"5MINER_{i}", 0.5) for i in range(5)]
    r1 = _make_report(hks_1, source=source, epoch=1, complete=True, now=now)
    external_scores.store_report(store, r1)

    status1 = external_scores.status(store, source=source, since_iso=cutoff)
    assert status1["latest_complete"] is True
    assert status1["latest_fresh"] is True
    assert status1["active_score_count"] == 5, (
        "Should count 5 active miners from epoch 1"
    )

    # Epoch 2: Empty scores list with complete=true (revoke everyone)
    r2 = _make_report([], source=source, epoch=2, complete=True, now=now)
    external_scores.store_report(store, r2)

    status2 = external_scores.status(store, source=source, since_iso=cutoff)
    assert status2["latest_complete"] is True
    assert status2["latest_fresh"] is True
    assert status2["active_score_count"] == 0, (
        "Empty complete snapshot must zero active_score_count"
    )
    assert status2["latest_epoch"] == 2, "Should track epoch 2"


def test_status_ignores_stale_snapshot(store):
    """If the latest snapshot is stale (generated_at < since_iso), it should
    not update active_score_count, and latest_fresh should be False."""
    now = _now()
    source = "violet_audio"
    old_time = now - timedelta(seconds=7200)  # 2 hours ago
    cutoff = _iso(now - timedelta(seconds=1800))  # 30 min ago cutoff

    r = _make_report(
        [("5MINER_0", 0.8)],
        source=source,
        epoch=1,
        complete=True,
        now=old_time,
    )
    external_scores.store_report(store, r)

    status = external_scores.status(store, source=source, since_iso=cutoff)
    assert status["latest_complete"] is True
    assert status["latest_fresh"] is False, "Snapshot older than cutoff is stale"
    assert status["active_score_count"] == 0, (
        "Stale snapshot should not contribute to active count"
    )


# ===========================================================================
# Blocker 2: confidential_primary intent fails closed (never base, never blend)
# ===========================================================================


def test_confidential_primary_intent_preserved_wrong_source(monkeypatch):
    """mode=confidential_primary must NOT resolve to blend when the source is
    wrong/absent. The intent is preserved so composition degrades to a signed
    burn vector rather than re-admitting base scores."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "violet_audio")
    assert weights.external_scores_mode() == "confidential_primary"


def test_confidential_primary_intent_preserved_absent_source(monkeypatch):
    """Default source (violet_audio, i.e. no explicit confidential source) still
    keeps the confidential_primary intent rather than silently blending."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", raising=False)
    assert weights.external_scores_mode() == "confidential_primary"


def test_unknown_mode_fails_closed(monkeypatch):
    """An unknown nonempty mode string must raise, not silently become blend."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "totally_bogus")
    with pytest.raises(
        weights.VectorError, match="unknown CATHEDRAL_EXTERNAL_SCORES_MODE"
    ):
        weights.external_scores_mode()


def test_empty_and_unset_mode_default_blend(monkeypatch):
    """Unset or empty mode preserves the default 'blend' so unrelated
    deployments do not break."""
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_MODE", raising=False)
    assert weights.external_scores_mode() == "blend"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "   ")
    assert weights.external_scores_mode() == "blend"


def test_confidential_primary_wrong_source_degrades_to_burn(store, monkeypatch):
    """Wrong source under confidential_primary intent -> signed degraded empty
    vector (confidential_mass=0, reason=invalid_source), never base."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "violet_audio")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", "true")
    base = {"5BASE_MINER": 1.0}
    result, meta = weights._apply_external_scores(store, base, now=_now())
    assert result == {}, "must not re-admit base scores"
    cp = meta["confidential_primary"]
    assert cp["degradation_reason"] == "invalid_source"
    assert cp["confidential_mass"] == 0.0
    assert cp["base_mass"] == 0.0
    assert meta["blended"] is False


def test_confidential_primary_missing_confirm_degrades_to_burn(store, monkeypatch):
    """Correct source but PRIMARY_CONFIRM absent -> signed degraded empty vector
    (reason=primary_confirm_missing), never base."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "cathedral_confidential_tdx")
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", raising=False)
    base = {"5BASE_MINER": 1.0}
    result, meta = weights._apply_external_scores(store, base, now=_now())
    assert result == {}, "must not re-admit base scores"
    cp = meta["confidential_primary"]
    assert cp["degradation_reason"] == "primary_confirm_missing"
    assert cp["confidential_mass"] == 0.0
    assert cp["source"] == "cathedral_confidential_tdx"


def test_confidential_primary_disabled_ingestion_degrades_not_base(store, monkeypatch):
    """Even with ingestion disabled, primary intent degrades to burn, never
    falls through to a base-only blend."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "cathedral_confidential_tdx")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", "true")
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", raising=False)
    base = {"5BASE_MINER": 1.0}
    result, meta = weights._apply_external_scores(store, base, now=_now())
    assert result == {}, "disabled ingestion must not fall through to base"
    cp = meta["confidential_primary"]
    assert cp["confidential_mass"] == 0.0
    assert cp["degradation_reason"] is not None


def test_validated_supply_emitter_signs_90_10_revocation_contract(store, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_VALIDATED_SUPPLY_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "cathedral_confidential_tdx")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", "true")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_BURN_HOTKEY", "burn-hotkey")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_BURN_UID", "")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "10")

    vector = weights.build_signed_vector(store, signing_key_hex="11" * 32, now=_now())

    assert vector["policy_metadata"]["validated_supply"] == {
        "contract_version": "v2",
        "intel_tdx_allocation": 0.9,
        "fixed_burn_allocation": 0.1,
        "burn_hotkey": "burn-hotkey",
    }
    assert vector["burn_snapshot"] == {
        "burn_uid": None,
        "burn_hotkey": "burn-hotkey",
        "forced_burn_percentage": 10.0,
    }
    assert validator_thin.vector_to_uid_weights(
        vector,
        {"burn-hotkey": 17},
        require_policy=validator_thin.REQUIRE_POLICY_VALIDATED_SUPPLY_V1,
    ) == {17: 1.0}
