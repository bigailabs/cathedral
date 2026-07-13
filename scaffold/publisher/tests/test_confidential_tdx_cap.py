"""Focused contract tests for the confidential TDX global 10% blend."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import pytest

from scaffold.publisher import weights
from scaffold.wire_vector import VectorError

SOURCE = "cathedral_confidential_tdx"
FRACTION = 0.10
TOL = 1e-12


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeStoreTDX:
    """Minimal external snapshot and metagraph fixture."""

    def __init__(
        self,
        ext_scores: list[tuple[str, float]],
        registered: list[str],
        *,
        payable: list[str] | None = None,
    ) -> None:
        generated_at = _iso(_now())
        self._report = {
            "source": SOURCE,
            "epoch": 1,
            "complete": True,
            "generated_at": generated_at,
            "scores": [
                {"miner_hotkey": hotkey, "score": score}
                for hotkey, score in ext_scores
            ],
        }
        self._ext_scores = ext_scores
        self._registered = set(registered)
        self._payable = set(payable if payable is not None else registered)
        self._generated_at = generated_at

    def query(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if "FROM external_score_reports" in sql:
            if not self._ext_scores:
                return []
            return [{
                "id": "tdx-report-1",
                "epoch": 1,
                "generated_at_iso": self._generated_at,
                "received_at_iso": self._generated_at,
                "report_json": json.dumps(self._report),
            }]
        if "FROM external_score_entries" in sql:
            return [
                {"miner_hotkey": hotkey, "score": score}
                for hotkey, score in self._ext_scores
            ]
        if "FROM metagraph_hotkeys" in sql:
            return [
                {"hotkey": hotkey, "updated_at_iso": self._generated_at}
                for hotkey in self._payable
            ]
        return []

    def write(self, fn: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _tdx_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", SOURCE)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", str(FRACTION))
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED", "0")
    monkeypatch.setenv("CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS", "off")


def _blend(
    monkeypatch: pytest.MonkeyPatch,
    base: dict[str, float],
    ext: list[tuple[str, float]],
    *,
    registered: list[str] | None = None,
    payable: list[str] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    all_hotkeys = list(base) + [hotkey for hotkey, _score in ext]
    registered = registered if registered is not None else list(set(all_hotkeys))
    store = FakeStoreTDX(ext, registered, payable=payable)
    return weights._apply_external_scores(store, base, now=_now())


def _components(meta: dict[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    return (
        meta.get("_internal_base_components") or {},
        meta.get("_internal_ext_components") or {},
    )


def test_global_union_exact_10_percent_and_v3_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {"base-only": 1.0, "overlap": 1.0}
    ext = [("overlap", 1.0), ("compute-only", 0.5)]
    out, meta = _blend(monkeypatch, base, ext)
    base_comp, ext_comp = _components(meta)
    cap = meta["confidential_tdx_cap"]

    assert set(out) == {"base-only", "overlap", "compute-only"}
    assert abs(sum(base_comp.values()) - 0.90) <= TOL
    assert abs(sum(ext_comp.values()) - 0.10) <= TOL
    assert abs(sum(ext_comp.values()) / sum(out.values()) - FRACTION) <= TOL
    assert cap["cap_version"] == "v3"
    assert cap["withheld_external_mass"] == 0.0
    assert cap["global_cap_assertion_ok"] is True
    assert "compute_only_zero_count" not in cap
    assert "pointwise_cap_assertion_ok" not in cap


def test_compute_only_payable_hotkey_gets_external_component_and_weight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, meta = _blend(
        monkeypatch,
        {"base": 1.0},
        [("compute-only", 1.0)],
    )
    base_comp, ext_comp = _components(meta)

    assert out["compute-only"] > 0.0
    assert ext_comp["compute-only"] == FRACTION
    assert base_comp["compute-only"] == 0.0
    assert out["compute-only"] == ext_comp["compute-only"]


def test_overlap_is_additive(monkeypatch: pytest.MonkeyPatch) -> None:
    out, meta = _blend(
        monkeypatch,
        {"overlap": 1.0, "base-only": 1.0},
        [("overlap", 1.0), ("external-only", 1.0)],
    )
    base_comp, ext_comp = _components(meta)

    assert base_comp["overlap"] == pytest.approx(0.45)
    assert ext_comp["overlap"] == pytest.approx(0.05)
    assert out["overlap"] == pytest.approx(0.50)
    assert out["overlap"] == pytest.approx(
        base_comp["overlap"] + ext_comp["overlap"]
    )


@pytest.mark.parametrize(
    ("base", "ext", "expected"),
    [
        ({"base": 0.7}, [], {"base": 0.7}),
        ({}, [("external", 1.0)], {}),
        ({}, [], {}),
    ],
    ids=["base-only", "external-only", "neither"],
)
def test_global_blend_state_matrix(
    monkeypatch: pytest.MonkeyPatch,
    base: dict[str, float],
    ext: list[tuple[str, float]],
    expected: dict[str, float],
) -> None:
    out, meta = _blend(monkeypatch, base, ext)
    assert out == expected
    if base and not ext:
        assert meta["base_mass"] == 1.0
        assert meta["external_mass"] == 0.0
    if not base and ext:
        assert meta["degraded"] == "external_only_fail_closed"


def test_payable_filter_keeps_compute_only_and_reallocates_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS", "filter")
    out, meta = _blend(
        monkeypatch,
        {"unpayable-base": 1.0, "overlap": 1.0},
        [("overlap", 1.0), ("compute-only", 1.0)],
        registered=["unpayable-base", "overlap", "compute-only"],
        payable=["overlap", "compute-only"],
    )
    base_comp, ext_comp = _components(meta)

    assert set(out) == {"overlap", "compute-only"}
    assert "unpayable-base" not in out
    assert out["compute-only"] > 0.0
    assert ext_comp["compute-only"] > 0.0
    assert abs(sum(ext_comp.values()) / sum(out.values()) - FRACTION) <= TOL


@pytest.mark.parametrize("configured", ["0", "-0.01", "0.1000001", "nan", "inf"])
def test_fraction_must_be_explicit_finite_and_at_most_10pct(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", configured)
    with pytest.raises(VectorError, match="confidential_tdx fraction"):
        _blend(monkeypatch, {"base": 1.0}, [("base", 1.0)])


def test_validator_rejects_nonfinite_negative_and_nonexact_aggregate() -> None:
    validate = weights._validate_confidential_tdx_components
    with pytest.raises(VectorError, match="non-finite"):
        validate({"A": math.inf}, {"A": 0.9}, {"A": 0.1}, FRACTION, context="test")
    with pytest.raises(VectorError, match="negative"):
        validate({"A": 1.0}, {"A": 1.1}, {"A": -0.1}, FRACTION, context="test")
    with pytest.raises(VectorError, match="aggregate"):
        validate({"A": 1.0}, {"A": 1.0}, {"A": 0.0}, FRACTION, context="test")


def test_global_components_validate_after_json_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, meta = _blend(
        monkeypatch,
        {"A": 0.7, "B": 0.3},
        [("A", 0.8), ("C", 0.2)],
    )
    base_comp, ext_comp = _components(meta)
    entries = [
        {
            "miner_hotkey": hotkey,
            "weight": out[hotkey],
            "base_component": base_comp[hotkey],
            "external_component": ext_comp[hotkey],
        }
        for hotkey in sorted(out)
    ]
    decoded = json.loads(json.dumps(entries))
    scores = {entry["miner_hotkey"]: entry["weight"] for entry in decoded}
    decoded_base = {entry["miner_hotkey"]: entry["base_component"] for entry in decoded}
    decoded_ext = {entry["miner_hotkey"]: entry["external_component"] for entry in decoded}
    totals = weights._validate_confidential_tdx_components(
        scores, decoded_base, decoded_ext, FRACTION, context="json"
    )
    assert abs(totals["external_fraction"] - FRACTION) <= TOL
