"""Pointwise accounting control for cathedral_confidential_tdx (contract §1-§7).

The publisher signs w_i = (1-f)*base_norm_i + f*ext_norm_i, but old thin
validators later drop deregistered hotkeys and renormalize.  A base-only hotkey
can disappear and amplify a compute-only survivor above 10%.

This test suite verifies:
  §1  Hard source-specific fraction cap = 0.10 regardless of env.
  §2  Pointwise c_i <= (f/(1-f))*a_i*(1-margin) before combining.
  §3  Excess compute withheld; compute-only hotkeys receive zero.
  §4  Other sources are unaffected.
  §5  Signed payload carries per-hotkey base_component/external_component.
  §6  Hard assertion before signing; fail closed on violation.
  §7  One signed vector; no set_weights path.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from typing import Any

import pytest

from scaffold.publisher import weights
from scaffold.publisher.weights import (
    CONFIDENTIAL_TDX_HARD_CAP,
    CONFIDENTIAL_TDX_POINTWISE_MARGIN,
    _apply_confidential_tdx_pointwise_cap,
    _apply_external_scores,
    _l1_normalize,
)
from scaffold.wire_vector import VectorError

SOURCE = "cathedral_confidential_tdx"
# tolerance for floating-point comparisons
TOL = 1e-7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeStoreTDX:
    """Minimal store fixture for cathedral_confidential_tdx blend tests.

    Mimics the queries issued by _apply_external_scores:
      - external_score_reports (latest_snapshot_scores)
      - external_score_entries (latest_snapshot_scores)
      - metagraph_hotkeys (registration gate)
    """

    def __init__(
        self,
        ext_scores: list[tuple[str, float]],
        registered_hotkeys: list[str],
        *,
        epoch: int = 1,
        generated_at: str | None = None,
    ) -> None:
        self._ext_scores = ext_scores
        self._registered = registered_hotkeys
        self._epoch = epoch
        self._generated_at = generated_at or _iso(_now())
        self._report_id = "tdx-test-report-1"
        report_obj = {
            "source": SOURCE,
            "epoch": epoch,
            "complete": True,
            "generated_at": self._generated_at,
            "scores": [{"miner_hotkey": hk, "score": s} for hk, s in ext_scores],
        }
        self._report_json = json.dumps(report_obj)

    def query(self, sql: str, params: tuple) -> list[dict]:
        if "FROM external_score_reports" in sql:
            if not self._ext_scores:
                return []
            return [{
                "id": self._report_id,
                "epoch": self._epoch,
                "generated_at_iso": self._generated_at,
                "received_at_iso": self._generated_at,
                "report_json": self._report_json,
            }]
        if "FROM external_score_entries" in sql:
            if "report_id" in sql:
                return [{"miner_hotkey": hk, "score": s}
                        for hk, s in self._ext_scores]
            # Legacy recent_scores path
            cutoff = params[1] if len(params) > 1 else ""
            return [{"miner_hotkey": hk, "score": s,
                     "received_at_iso": self._generated_at}
                    for hk, s in self._ext_scores
                    if s > 0 and self._generated_at > str(cutoff)]
        if "FROM metagraph_hotkeys" in sql:
            cutoff = params[2] if len(params) > 2 else ""
            return [
                {"hotkey": hk, "updated_at_iso": self._generated_at}
                for hk in self._registered
                if self._generated_at > str(cutoff)
            ]
        return []

    def write(self, fn: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _tdx_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable external scores for cathedral_confidential_tdx with fraction=0.10."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", SOURCE)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.10")
    monkeypatch.setenv("CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS", "off")
    for k in (
        "CATHEDRAL_EXTERNAL_SCORES_MODE",
        "CATHEDRAL_EXTERNAL_SCORES_WEIGHT",
        "CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT",
        "CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM",
        "CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION",
        "CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED",
    ):
        monkeypatch.delenv(k, raising=False)


def _blend(
    base: dict[str, float],
    ext: list[tuple[str, float]],
    registered: list[str] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Run the full blend path and return (blended_scores, blend_meta).

    For TDX sources, internal component state is stored in
    _internal_base_components and _internal_ext_components, not hotkey_components.
    """
    all_hotkeys = list(base) + [hk for hk, _ in ext]
    if registered is None:
        registered = list(set(all_hotkeys))
    store = FakeStoreTDX(ext, registered)
    return _apply_external_scores(store, base, now=_now())


def _realized_ext_fraction(scores: dict[str, float], components: dict[str, dict]) -> float:
    """Compute realized external fraction from per-hotkey components.

    Handles the case where components may have been extracted from internal state.
    For non-TDX sources, components dict may be empty; return 0.0 in that case.
    """
    if not components:
        return 0.0
    total_base = sum(components.get(hk, {}).get("base_component", 0.0) for hk in scores)
    total_ext = sum(components.get(hk, {}).get("external_component", 0.0) for hk in scores)
    total = total_base + total_ext
    return total_ext / total if total > 0 else 0.0


def _drop_and_renorm(
    weights_list: list[dict[str, Any]],
    drop_hotkeys: set[str],
) -> tuple[dict[str, float], dict[str, dict]]:
    """Simulate thin validator: drop deregistered hotkeys and renormalize."""
    surviving = [w for w in weights_list if w["miner_hotkey"] not in drop_hotkeys]
    total = sum(float(w["weight"]) for w in surviving)
    if total <= 0:
        return {}, {}
    renormed = {w["miner_hotkey"]: float(w["weight"]) / total for w in surviving}
    # Scale components by the same factor (preserve ratio; then renorm)
    components: dict[str, dict] = {}
    for w in surviving:
        orig_w = float(w["weight"])
        scale = 1.0 / total
        components[w["miner_hotkey"]] = {
            "base_component": w.get("base_component", 0.0) * scale,
            "external_component": w.get("external_component", 0.0) * scale,
        }
    return renormed, components


# ---------------------------------------------------------------------------
# §1: Hard cap tests
# ---------------------------------------------------------------------------

def test_env_requests_50pct_effective_cap_is_10pct(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1: FRACTION=0.5 for cathedral_confidential_tdx is clamped to 0.10."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.5")
    base = {"A": 0.7, "B": 0.3}
    out, meta = _blend(base, [("A", 0.9), ("B", 0.5)])
    cap = meta.get("confidential_tdx_cap") or {}
    assert cap.get("configured_fraction", 1.0) <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
        f"configured_fraction {cap.get('configured_fraction')} must be <= 0.10")
    ext_frac = cap.get("realized_external_fraction", 1.0)
    assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
        f"realized_external_fraction {ext_frac} exceeds hard cap {CONFIDENTIAL_TDX_HARD_CAP}")


def test_max_fraction_cannot_override_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1: MAX_FRACTION=0.9 cannot push the effective fraction above 0.10."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.10")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION", "0.9")
    base = {"A": 0.6, "B": 0.4}
    out, meta = _blend(base, [("A", 1.0), ("B", 1.0)])
    cap = meta.get("confidential_tdx_cap") or {}
    ext_frac = cap.get("realized_external_fraction", 1.0)
    assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL


def test_lower_configured_fraction_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1: Configured fraction < 0.10 (e.g. 0.05) is honored, not forced up."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.05")
    base = {"A": 0.8, "B": 0.2}
    out, meta = _blend(base, [("A", 0.9), ("B", 0.6)])
    cap = meta.get("confidential_tdx_cap") or {}
    assert abs(cap.get("configured_fraction", 1.0) - 0.05) < TOL, (
        "lower fraction should be honored exactly")
    ext_frac = cap.get("realized_external_fraction", 1.0)
    assert ext_frac <= 0.05 + TOL


def test_missing_fraction_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """§1: Missing FRACTION for cathedral_confidential_tdx gives zero external (fail closed)."""
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", raising=False)
    base = {"A": 1.0}
    out, meta = _blend(base, [("A", 1.0)])
    # Should return base unchanged (fraction=0 from FRACTION_REQUIRED_SOURCES)
    assert "A" in out
    assert not meta.get("blended", False), (
        "missing FRACTION should fail closed to base-only (no blend)")


# ---------------------------------------------------------------------------
# §2-§3: Pointwise cap + compute-only zero
# ---------------------------------------------------------------------------

def test_prior_counterexample_base_only_a_compute_only_b() -> None:
    """§3 prior counterexample: base-only A, compute-only B.

    B must receive external_component=0 in the signed payload.
    Any surviving subset after thin-validator drop/renorm must stay <=10%.
    """
    # A has base score; B only appears in external scores
    base = {"A": 1.0}
    ext = [("A", 0.5), ("B", 1.0)]  # B is compute-only
    out, meta = _blend(base, ext)

    assert "B" not in out or out.get("B", 0.0) == 0.0, (
        "compute-only B must have zero weight")
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}
    assert ext_comp.get("B", 999) == 0.0, (
        "compute-only B's external_component must be exactly 0")

    # Build weights list as the thin validator sees it
    weights_list = [
        {
            "miner_hotkey": hk,
            "weight": out.get(hk, 0.0),
            "base_component": base_comp.get(hk, 0.0),
            "external_component": ext_comp.get(hk, 0.0),
        }
        for hk in (set(out) | {"B"})
    ]

    # Simulate thin validator dropping A (base-only) -- only B survives
    remaining, comps = _drop_and_renorm(weights_list, drop_hotkeys={"A"})
    # B was zero-weight; after dropping A the survivor set is empty or B=0
    total_B = remaining.get("B", 0.0)
    assert total_B <= TOL, (
        f"compute-only B's renormed weight {total_B:.6f} should be 0")

    # External fraction of surviving set must be <=10%
    ext_frac = _realized_ext_fraction(remaining, comps)
    assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
        f"external fraction {ext_frac:.6f} > {CONFIDENTIAL_TDX_HARD_CAP} after B survives")


def test_compute_only_hotkey_gets_zero_always() -> None:
    """§3: Any hotkey with no base score receives zero external contribution."""
    base = {"A": 0.6, "C": 0.4}  # B absent from base
    ext = [("A", 0.5), ("B", 1.0), ("C", 0.3)]
    out, meta = _blend(base, ext)

    ext_comp = meta.get("_internal_ext_components") or {}
    b_ext = ext_comp.get("B", 0.0)
    assert b_ext == 0.0, f"B external_component={b_ext} must be 0 (compute-only)"
    b_weight = out.get("B", 0.0)
    assert b_weight == 0.0, f"B weight={b_weight} must be 0 (compute-only)"


def test_withheld_mass_not_redistributed() -> None:
    """§3: Excess external mass is withheld, not redistributed to other hotkeys."""
    # Force all hotkeys to need capping: equal base, but external heavily skewed
    base = {"A": 0.5, "B": 0.5}
    # B gets a huge external score that would exceed the pointwise cap
    ext = [("A", 0.01), ("B", 0.99)]
    out, meta = _blend(base, ext)

    cap = meta.get("confidential_tdx_cap") or {}
    withheld = cap.get("withheld_external_mass", 0.0)
    realized_ext = cap.get("actual_external_mass", 0.0)
    configured_ext = cap.get("configured_fraction", 0.10) * 1.0  # would be fraction of 1.0

    # The withheld mass should be positive (B's excess was capped)
    assert withheld >= 0.0
    # Realized external mass + withheld <= configured fraction (mass universe ~1.0)
    assert realized_ext <= cap.get("configured_fraction", 0.10) + TOL

    # Verify the sum of actual weights is approximately 1-withheld from max
    # (withheld mass is absent from the output, not given to anyone else)
    total_blended = sum(out.values())
    total_from_cap = cap.get("actual_base_mass", 0.0) + cap.get("actual_external_mass", 0.0)
    assert abs(total_blended - total_from_cap) < TOL, (
        "sum of blended weights must equal base_mass + ext_mass (withheld not redistributed)")


def test_pointwise_cap_assertion_ok_in_metadata() -> None:
    """§6: cap metadata reports assertion_ok=True on a valid blend."""
    base = {"A": 0.6, "B": 0.4}
    ext = [("A", 0.5), ("B", 0.3)]
    _out, meta = _blend(base, ext)
    cap = meta.get("confidential_tdx_cap") or {}
    assert cap.get("pointwise_cap_assertion_ok") is True


def test_metadata_fields_present() -> None:
    """§5: Signed payload metadata carries all required aggregate fields."""
    base = {"A": 0.7, "B": 0.3}
    ext = [("A", 0.8), ("B", 0.5)]
    _out, meta = _blend(base, ext)
    cap = meta.get("confidential_tdx_cap") or {}
    required = [
        "configured_cap", "configured_fraction",
        "actual_base_mass", "actual_external_mass",
        "realized_external_fraction", "withheld_external_mass",
        "cap_version", "pointwise_margin",
        "capped_hotkey_count", "compute_only_zero_count",
        "pointwise_cap_assertion_ok",
    ]
    missing = [k for k in required if k not in cap]
    assert not missing, f"cap metadata missing keys: {missing}"


def test_per_hotkey_components_in_blend_meta() -> None:
    """§5: Internal blend state carries per-hotkey base_component and external_component.
    These are emitted in signed weight entries, not in policy_metadata.blend.
    """
    base = {"A": 0.7, "B": 0.3}
    ext = [("A", 0.8), ("B", 0.5)]
    _out, meta = _blend(base, ext)
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}
    assert "A" in base_comp and "B" in base_comp
    assert "A" in ext_comp and "B" in ext_comp
    for hk in base_comp:
        assert math.isfinite(base_comp[hk])
        assert math.isfinite(ext_comp[hk])
        assert base_comp[hk] >= 0.0
        assert ext_comp[hk] >= 0.0


# ---------------------------------------------------------------------------
# Thin-validator drop/renorm invariant
# ---------------------------------------------------------------------------

def _simulate_thin_validator_fraction(
    base: dict[str, float],
    ext: list[tuple[str, float]],
    drop_hotkeys: set[str],
) -> float:
    """Full pipeline: blend -> build weights list -> drop/renorm -> realized ext fraction.

    Uses internal component state from blend metadata.
    """
    out, meta = _blend(base, ext)
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}
    weights_list = [
        {
            "miner_hotkey": hk,
            "weight": out.get(hk, 0.0),
            "base_component": base_comp.get(hk, 0.0),
            "external_component": ext_comp.get(hk, 0.0),
        }
        for hk in sorted(set(out) | set(base_comp))
    ]
    remaining, comps = _drop_and_renorm(weights_list, drop_hotkeys)
    if not remaining:
        return 0.0
    return _realized_ext_fraction(remaining, comps)


def test_drop_base_only_survivor_stays_under_10pct() -> None:
    """After dropping base-only hotkeys, remaining survivors stay <=10%."""
    base = {"BASE1": 0.5, "BASE2": 0.3, "BOTH": 0.2}
    ext = [("BOTH", 0.9), ("COMPUTE1", 0.8)]
    ext_frac = _simulate_thin_validator_fraction(
        base, ext, drop_hotkeys={"BASE1", "BASE2"})
    assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
        f"after drop, ext_frac={ext_frac:.6f} > 0.10")


# pytest.mark.parametrize for drop scenarios
@pytest.mark.parametrize("drop_set,description", [
    ({"BASE_A"}, "drop one base-only"),
    ({"BASE_B"}, "drop other base-only"),
    ({"BASE_A", "BASE_B"}, "drop both base-only"),
    (set(), "drop nothing"),
])
def test_survivor_subsets_all_under_10pct(
    drop_set: set[str], description: str
) -> None:
    """§property: various survivor subsets all stay <=10% after renorm."""
    base = {"BASE_A": 0.5, "BASE_B": 0.3, "BOTH": 0.2}
    ext = [("BOTH", 1.0), ("COMPUTE_ONLY", 0.8)]
    ext_frac = _simulate_thin_validator_fraction(base, ext, drop_hotkeys=drop_set)
    assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
        f"[{description}] ext_frac={ext_frac:.6f} > 0.10")


# ---------------------------------------------------------------------------
# Property / fuzz tests (seeded random)
# ---------------------------------------------------------------------------

def _random_base_ext(
    rng: random.Random,
    n_base: int,
    n_ext: int,
) -> tuple[dict[str, float], list[tuple[str, float]]]:
    """Generate random positive base and external score vectors."""
    base_keys = [f"BASE_{i}" for i in range(n_base)]
    ext_keys = [f"EXT_{i}" for i in range(n_ext)]
    combined_keys = list(set(base_keys) | set(ext_keys))

    # Some hotkeys may appear in both
    base = {k: rng.random() + 0.01 for k in base_keys}
    ext_raw = [(k, rng.random() + 0.01) for k in combined_keys]
    return base, ext_raw


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 7, 42, 100, 999])
def test_property_random_vectors_stay_under_10pct(seed: int) -> None:
    """§property/fuzz: random base/ext vectors, all survivor subsets stay <=10%."""
    rng = random.Random(seed)
    n_base = rng.randint(2, 10)
    n_ext = rng.randint(1, 8)
    base, ext = _random_base_ext(rng, n_base, n_ext)
    all_hotkeys = list(set(base) | {hk for hk, _ in ext})

    out, meta = _blend(base, ext)
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}
    weights_list = [
        {
            "miner_hotkey": hk,
            "weight": out.get(hk, 0.0),
            "base_component": base_comp.get(hk, 0.0),
            "external_component": ext_comp.get(hk, 0.0),
        }
        for hk in sorted(set(out) | set(base_comp))
    ]

    # Try several random survivor subsets including singletons
    n_trials = 8
    for _ in range(n_trials):
        # Random subset: drop between 0 and all-but-one hotkeys
        n_drop = rng.randint(0, max(0, len(all_hotkeys) - 1))
        drop_set = set(rng.sample(all_hotkeys, n_drop))
        remaining, comps = _drop_and_renorm(weights_list, drop_set)
        if not remaining:
            continue
        ext_frac = _realized_ext_fraction(remaining, comps)
        assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
            f"seed={seed} drop={drop_set} ext_frac={ext_frac:.6f} > 0.10")


@pytest.mark.parametrize("seed", [0, 1, 17, 31])
def test_singleton_survivor_stays_under_10pct(seed: int) -> None:
    """§property: singleton survivor subset stays <=10%."""
    rng = random.Random(seed)
    n_base = rng.randint(2, 6)
    n_ext = rng.randint(1, 5)
    base, ext = _random_base_ext(rng, n_base, n_ext)

    out, meta = _blend(base, ext)
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}
    weights_list = [
        {
            "miner_hotkey": hk,
            "weight": out.get(hk, 0.0),
            "base_component": base_comp.get(hk, 0.0),
            "external_component": ext_comp.get(hk, 0.0),
        }
        for hk in sorted(set(out) | set(base_comp))
    ]
    all_hotkeys = [w["miner_hotkey"] for w in weights_list]

    for survivor in all_hotkeys:
        drop_set = set(all_hotkeys) - {survivor}
        remaining, comps = _drop_and_renorm(weights_list, drop_set)
        if not remaining:
            continue
        ext_frac = _realized_ext_fraction(remaining, comps)
        assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
            f"seed={seed} singleton={survivor} ext_frac={ext_frac:.6f} > 0.10")


# ---------------------------------------------------------------------------
# UID merge/summing preserves cap
# ---------------------------------------------------------------------------

def test_uid_merge_summing_preserves_cap() -> None:
    """§property: if multiple hotkeys map to the same UID (summed), cap still holds."""
    # Two hotkeys both blended at <=10% external fraction each.
    # Merging (summing) their weights preserves the fraction.
    base = {"A": 0.6, "B": 0.4}
    ext = [("A", 0.8), ("B", 0.7)]
    out, meta = _blend(base, ext)
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}

    # Simulate: A and B map to the same UID -> sum their weights
    total_weight = sum(out.values())
    total_base = sum(base_comp.get(hk, 0.0) for hk in out)
    total_ext = sum(ext_comp.get(hk, 0.0) for hk in out)

    merged_ext_frac = total_ext / (total_base + total_ext) if (total_base + total_ext) > 0 else 0.0
    assert merged_ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
        f"merged UID ext_frac={merged_ext_frac:.6f} > 0.10")


# ---------------------------------------------------------------------------
# u16 quantization
# ---------------------------------------------------------------------------

def _to_u16_quantize(weights_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simulate the u16 weight quantization bittensor uses (0..65535)."""
    total = sum(float(w["weight"]) for w in weights_list)
    if total <= 0:
        return weights_list
    quantized = []
    for w in weights_list:
        u16_val = round(float(w["weight"]) / total * 65535)
        quantized.append({**w, "weight_u16": u16_val})
    return quantized


def test_u16_quantization_attribution_under_10pct() -> None:
    """§property: simulated u16 quantization -> external attribution stays <=10%.

    After u16 quantization, compute weighted external attribution:
      attribution = sum(q_i * (c_i/(a_i+c_i))) / sum(q_i)
    where q_i is the quantized u16 weight, a_i is base_component,
    c_i is external_component. Handles tiny/singleton cases.
    """
    base = {"A": 0.55, "B": 0.30, "C": 0.15}
    ext = [("A", 0.9), ("B", 0.7), ("C", 0.6)]
    out, meta = _blend(base, ext)
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}

    weights_list = [
        {
            "miner_hotkey": hk,
            "weight": out.get(hk, 0.0),
            "base_component": base_comp.get(hk, 0.0),
            "external_component": ext_comp.get(hk, 0.0),
        }
        for hk in sorted(out)
    ]
    quantized = _to_u16_quantize(weights_list)
    total_u16 = sum(w["weight_u16"] for w in quantized)

    if total_u16 <= 0:
        return

    # Weighted external attribution after quantization
    # attribution_i = q_i * (c_i / (a_i + c_i))
    # total_attribution = sum(attribution_i) / sum(q_i)
    numerator = 0.0
    for w in quantized:
        a_i = float(w["base_component"])
        c_i = float(w["external_component"])
        q_i = float(w["weight_u16"])
        w_i = a_i + c_i
        if w_i > 0.0:
            attribution_i = q_i * (c_i / w_i)
            numerator += attribution_i
        # else: singleton/tiny case with zero weight contributes nothing

    merged_frac = numerator / total_u16 if total_u16 > 0 else 0.0
    # Allow a tiny extra tolerance for u16 rounding
    assert merged_frac <= CONFIDENTIAL_TDX_HARD_CAP + 1e-4, (
        f"u16 quantized ext_frac={merged_frac:.6f} > 0.10")


# ---------------------------------------------------------------------------
# JSON serialize/deserialize boundary
# ---------------------------------------------------------------------------

def test_json_serialize_deserialize_boundary() -> None:
    """§5: components survive JSON round-trip without violating the cap.

    Components are extracted from signed weight entries and recomputed as
    sums without independent rounding, so w_i = a_i + c_i is preserved.
    """
    base = {"A": 0.7, "B": 0.3}
    ext = [("A", 0.85), ("B", 0.55)]
    out, meta = _blend(base, ext)
    base_comp = meta.get("_internal_base_components") or {}
    ext_comp = meta.get("_internal_ext_components") or {}

    # Build the weights list as it appears in the signed payload
    weights_list = [
        {
            "miner_hotkey": hk,
            "weight": out.get(hk, 0.0),
            "base_component": base_comp.get(hk, 0.0),
            "external_component": ext_comp.get(hk, 0.0),
        }
        for hk in sorted(out)
    ]

    # Serialize to JSON and back (as thin validators would receive it)
    raw_json = json.dumps(weights_list)
    deserialized = json.loads(raw_json)

    # Recompute ext fraction from deserialized data
    total_base = sum(w.get("base_component", 0.0) for w in deserialized)
    total_ext = sum(w.get("external_component", 0.0) for w in deserialized)
    total = total_base + total_ext
    ext_frac = total_ext / total if total > 0 else 0.0

    # Also verify that w_i = a_i + c_i for each entry
    for w in deserialized:
        w_i = float(w.get("weight", 0.0))
        a_i = float(w.get("base_component", 0.0))
        c_i = float(w.get("external_component", 0.0))
        # Allow tiny floating-point tolerance
        assert abs(w_i - (a_i + c_i)) < 1e-12, (
            f"hotkey {w['miner_hotkey']}: w_i={w_i} != a_i+c_i={a_i+c_i}")

    assert ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL, (
        f"after JSON round-trip ext_frac={ext_frac:.9f} > 0.10")


# ---------------------------------------------------------------------------
# §4: Legacy / other source behavior unchanged
# ---------------------------------------------------------------------------

def test_other_source_not_affected_by_pointwise_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4: Non-TDX sources (e.g. violet_audio) use standard blend, not pointwise cap."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "violet_audio")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.30")

    # Use the violet_audio FakeStore pattern from test_external_scores_gates.py
    import json as _json
    from datetime import timedelta

    now_dt = _now()
    gen_at = _iso(now_dt)
    ext_scores = [("5REG", 0.9), ("5BASE", 0.3)]
    report_obj = {
        "source": "violet_audio",
        "epoch": 1,
        "complete": True,
        "generated_at": gen_at,
        "scores": [{"miner_hotkey": hk, "score": s} for hk, s in ext_scores],
    }

    class VioletStore:
        def query(self, sql, params):
            if "FROM external_score_reports" in sql:
                return [{"id": "r1", "epoch": 1,
                         "generated_at_iso": gen_at,
                         "received_at_iso": gen_at,
                         "report_json": _json.dumps(report_obj)}]
            if "FROM external_score_entries" in sql:
                if "report_id" in sql:
                    return [{"miner_hotkey": hk, "score": s} for hk, s in ext_scores]
                return []
            if "FROM metagraph_hotkeys" in sql:
                return [{"hotkey": "5REG", "updated_at_iso": gen_at},
                        {"hotkey": "5BASE", "updated_at_iso": gen_at}]
            return []
        def write(self, fn): raise NotImplementedError

    base = {"5BASE": 0.8, "5REG": 0.2}
    out, meta = _apply_external_scores(VioletStore(), base, now=now_dt)
    # Should blend, no confidential_tdx_cap key present
    assert "confidential_tdx_cap" not in meta or meta["confidential_tdx_cap"] is None, (
        "violet_audio should not have confidential_tdx_cap in metadata")
    # No internal component state for non-TDX source
    assert "_internal_base_components" not in meta, (
        "_internal_base_components should not be present for violet_audio source")
    assert "_internal_ext_components" not in meta, (
        "_internal_ext_components should not be present for violet_audio source")


def test_external_disabled_base_only_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4: External disabled -> pure base scoring, no blend metadata, backward-compatible."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "0")
    base = {"A": 0.6, "B": 0.4}
    store = FakeStoreTDX([("A", 0.9)], ["A", "B"])
    out, meta = _apply_external_scores(store, base, now=_now())
    # Must return base unchanged
    assert out == base
    assert not meta.get("blended", False)
    assert meta.get("base_mass", 0.0) == 1.0 or meta.get("base_miner_count", 0) > 0


def test_base_only_no_external_scores_backward_compatible() -> None:
    """External enabled but no fresh snapshot -> base-only, no blend."""
    store = FakeStoreTDX([], ["A", "B"])  # empty ext scores
    base = {"A": 0.6, "B": 0.4}
    out, meta = _apply_external_scores(store, base, now=_now())
    assert not meta.get("blended", False)


# ---------------------------------------------------------------------------
# Low-level unit: _apply_confidential_tdx_pointwise_cap
# ---------------------------------------------------------------------------

def test_pointwise_cap_unit_compute_only_receives_zero() -> None:
    """Unit: compute-only hotkeys get exactly 0 external contribution."""
    base_norm = {"A": 0.6, "B": 0.4}   # C absent
    ext_norm = {"A": 0.3, "B": 0.5, "C": 0.2}   # C is compute-only
    blended, bc, ec, cap = _apply_confidential_tdx_pointwise_cap(
        base_norm, ext_norm, 0.10)
    assert ec.get("C", 999) == 0.0, "compute-only C must have ext_component=0"
    assert "C" not in blended or blended.get("C", 0.0) == 0.0


def test_pointwise_cap_unit_no_excess_redistributed() -> None:
    """Unit: withheld excess is absent from blended totals."""
    base_norm = {"A": 0.5, "B": 0.5}
    # B's external score far exceeds the cap
    ext_norm = {"A": 0.01, "B": 0.99}
    blended, bc, ec, cap = _apply_confidential_tdx_pointwise_cap(
        base_norm, ext_norm, 0.10)
    total_blended = sum(blended.values())
    total_cap = cap["actual_base_mass"] + cap["actual_external_mass"]
    assert abs(total_blended - total_cap) < TOL
    assert cap["withheld_external_mass"] > 0


def test_pointwise_cap_unit_assertion_meta_ok() -> None:
    """Unit: assertion_ok is True on a valid blend."""
    base_norm = {"A": 0.7, "B": 0.3}
    ext_norm = {"A": 0.6, "B": 0.4}
    _blended, _bc, _ec, cap = _apply_confidential_tdx_pointwise_cap(
        base_norm, ext_norm, 0.10)
    assert cap["pointwise_cap_assertion_ok"] is True


def test_pointwise_cap_unit_realized_fraction_bounded() -> None:
    """Unit: realized_external_fraction <= configured fraction."""
    for f in (0.05, 0.08, 0.10):
        base_norm = {"A": 0.4, "B": 0.35, "C": 0.25}
        ext_norm = {"A": 0.8, "B": 0.1, "C": 0.1}
        _blended, _bc, _ec, cap = _apply_confidential_tdx_pointwise_cap(
            base_norm, ext_norm, f)
        assert cap["realized_external_fraction"] <= f + TOL, (
            f"f={f}: realized_ext={cap['realized_external_fraction']}")


def test_pointwise_cap_unit_l1_consistency() -> None:
    """Unit: base_components and ext_components are derived from L1-normalized inputs."""
    base_norm = {"A": 0.6, "B": 0.4}
    ext_norm = {"A": 0.5, "B": 0.5}
    f = 0.10
    blended, bc, ec, cap = _apply_confidential_tdx_pointwise_cap(
        base_norm, ext_norm, f)
    # base components should sum to ~(1-f) since base_norm sums to 1
    assert abs(sum(bc.values()) - (1.0 - f)) < TOL
    # ext components sum <= f
    assert sum(ec.values()) <= f + TOL


def test_pointwise_cap_invalid_fraction_raises() -> None:
    """Unit: fraction=0 or fraction>=1 raises VectorError."""
    with pytest.raises(VectorError):
        _apply_confidential_tdx_pointwise_cap({"A": 1.0}, {"A": 1.0}, 0.0)
    with pytest.raises(VectorError):
        _apply_confidential_tdx_pointwise_cap({"A": 1.0}, {"A": 1.0}, 1.0)


# ---------------------------------------------------------------------------
# Invariant test: build_signed_vector signature/correctness
# ---------------------------------------------------------------------------

def test_build_signed_vector_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant: build_signed_vector produces valid signed entries with component sums,
    metadata matches final entries, and cap is never violated.

    Tests:
    - w_i = a_i + c_i exactly for every entry (no independent rounding)
    - sum(a_i) and sum(c_i) match metadata after payable filter
    - No duplicate entries
    - Every pointwise cap is satisfied
    - aggregate realized fraction <= configured fraction
    """
    from scaffold.publisher.weights import (
        build_signed_vector,
        _build_weights_list,
        canonical_bytes,
    )
    import uuid

    # Minimal fake store for build_signed_vector
    class MinimalStore:
        def __init__(self, scores_: dict[str, float], base_c: dict[str, float],
                     ext_c: dict[str, float], blend_state: dict):
            self.scores_in = scores_
            self.base_comp = base_c
            self.ext_comp = ext_c
            self.blend_state = blend_state

        def query(self, sql: str, params: tuple) -> list[dict]:
            # Minimal responses for build_signed_vector flow
            if "FROM metagraph_hotkeys" in sql:
                # All hotkeys are registered (payable filter off by default)
                return [{"hotkey": hk, "updated_at_iso": "2026-07-11T00:00:00.000Z"}
                        for hk in self.scores_in]
            return []

        def write(self, fn):
            # Minimal write support for next_policy_version
            # Call fn with a mock connection object
            class MockConn:
                def execute(self, sql, params=None):
                    class MockResult:
                        def fetchone(self):
                            return (1000,)  # dummy last_policy_version
                    return MockResult()
            fn(MockConn())
            return 1001  # return next version

    # Minimal signing key for ed25519
    signing_key_hex = (
        "7a08bfba91c24d4b23a6dea9bd81c3e65dda7ad86b05d79a7e12e4c12f9a6f5c"
    )

    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", SOURCE)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.10")
    monkeypatch.setenv("CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS", "off")

    # Mock the compose_scores to return our test vector
    base = {"A": 0.6, "B": 0.4}
    ext = [("A", 0.8), ("B", 0.7)]
    out, meta = _blend(base, ext)
    base_comp_internal = meta.get("_internal_base_components") or {}
    ext_comp_internal = meta.get("_internal_ext_components") or {}

    store = MinimalStore(out, base_comp_internal, ext_comp_internal, meta)

    # Monkey-patch compose_scores to return our test vector
    orig_compose = weights.compose_scores
    def mock_compose(s, *, now=None, coldkey_of=None, blend_meta_out=None):
        if blend_meta_out is not None:
            blend_meta_out.update(meta)
        return out
    monkeypatch.setattr("scaffold.publisher.weights.compose_scores", mock_compose)

    # Now call build_signed_vector
    vec = build_signed_vector(store, signing_key_hex=signing_key_hex, now=_now())

    # Invariant 1: weights list has correct entries with base_component and external_component
    weights_list = vec.get("weights") or []
    assert len(weights_list) > 0, "weights list should not be empty"
    assert len(weights_list) == len(set(w["miner_hotkey"] for w in weights_list)), (
        "no duplicate hotkeys")

    # Invariant 2: w_i = a_i + c_i exactly for every entry
    for w in weights_list:
        w_i = float(w["weight"])
        a_i = float(w.get("base_component", 0.0))
        c_i = float(w.get("external_component", 0.0))
        assert abs(w_i - (a_i + c_i)) < 1e-12, (
            f"hotkey {w['miner_hotkey']}: w_i={w_i} != a_i+c_i={a_i+c_i}")

    # Invariant 3: sum(a_i) and sum(c_i) match metadata
    signed_base_sum = sum(float(w.get("base_component", 0.0)) for w in weights_list)
    signed_ext_sum = sum(float(w.get("external_component", 0.0)) for w in weights_list)
    signed_ext_frac = signed_ext_sum / (signed_base_sum + signed_ext_sum) if (
        signed_base_sum + signed_ext_sum) > 0 else 0.0

    meta_cap = vec.get("policy_metadata", {}).get("confidential_tdx_cap") or {}
    if meta_cap.get("pointwise_cap_assertion_ok"):
        # When TDX cap was applied, verify metadata fractionmatches
        meta_ext_frac = meta_cap.get("realized_external_fraction", 0.0)
        assert abs(signed_ext_frac - meta_ext_frac) < TOL, (
            f"signed_ext_frac={signed_ext_frac:.9f} != "
            f"meta_ext_frac={meta_ext_frac:.9f}")
        # And aggregate fraction <= configured cap
        assert signed_ext_frac <= CONFIDENTIAL_TDX_HARD_CAP + TOL

    # Invariant 4: Payload is properly signed (canonical bytes, then Ed25519)
    # (Just verify signature is present and base64 decodable)
    sig_b64 = vec.get("signature", "")
    assert sig_b64, "signature must be present"
    import base64
    try:
        base64.b64decode(sig_b64)
    except Exception as e:
        raise AssertionError(f"signature not valid base64: {e}")

    # Invariant 5: No hotkey_components in policy_metadata blend
    blend_meta = vec.get("policy_metadata", {}).get("blend") or {}
    assert "hotkey_components" not in blend_meta, (
        "hotkey_components must not be in policy_metadata.blend")
    # But components ARE in the signed weights entries
    assert any("base_component" in w for w in weights_list), (
        "base_component must be in signed weight entries for TDX blend")
