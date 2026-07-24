"""Post-signing drift safety for confidential global-cap v3 vectors."""
from __future__ import annotations

import math

import pytest

from scaffold import validator_thin


def _row(hotkey: str, base: float, external: float) -> dict[str, object]:
    return {
        "miner_hotkey": hotkey,
        "weight": base + external,
        "base_component": base,
        "external_component": external,
    }


def _v3_payload(
    rows: list[dict[str, object]],
    *,
    configured_fraction: object = 0.10,
    burn_uid: int | None = None,
    forced_burn_percentage: float = 0.0,
) -> dict[str, object]:
    cap: dict[str, object] = {"cap_version": "v3"}
    if configured_fraction is not None:
        cap["configured_fraction"] = configured_fraction
    return {
        "weights": rows,
        "burn_snapshot": {
            "burn_uid": burn_uid,
            "forced_burn_percentage": forced_burn_percentage,
        },
        "policy_metadata": {
            "confidential_tdx_cap": cap,
        },
    }


def test_full_map_preserves_exact_mixed_union_vector() -> None:
    payload = _v3_payload([
        _row("base", 0.45, 0.0),
        _row("overlap", 0.45, 0.05),
        _row("compute", 0.0, 0.05),
    ])

    result = validator_thin.vector_to_uid_weights(
        payload, {"base": 10, "overlap": 11, "compute": 12}
    )

    assert result == {10: 0.45, 11: 0.5, 12: 0.05}


def test_missing_base_hotkey_rebuilds_from_mapped_base_components() -> None:
    payload = _v3_payload([
        _row("missing-base", 0.45, 0.0),
        _row("overlap", 0.45, 0.05),
        _row("compute", 0.0, 0.05),
    ])

    result = validator_thin.vector_to_uid_weights(
        payload, {"overlap": 11, "compute": 12}
    )

    assert result == {11: 1.0}


def test_missing_compute_hotkey_drops_all_external_mass() -> None:
    payload = _v3_payload([
        _row("base", 0.45, 0.0),
        _row("overlap", 0.45, 0.05),
        _row("compute", 0.0, 0.05),
    ])

    result = validator_thin.vector_to_uid_weights(
        payload, {"base": 10, "overlap": 11}
    )

    assert result == {10: 0.5, 11: 0.5}


def test_incomplete_v3_fallback_applies_existing_burn_to_base_only() -> None:
    payload = _v3_payload(
        [
            _row("base", 0.45, 0.0),
            _row("overlap", 0.45, 0.05),
            _row("compute", 0.0, 0.05),
        ],
        burn_uid=99,
        forced_burn_percentage=20.0,
    )

    result = validator_thin.vector_to_uid_weights(
        payload, {"base": 10, "overlap": 11}
    )

    assert result == {10: 0.4, 11: 0.4, 99: 0.2}


def test_compute_only_row_is_retained_within_valid_base_union() -> None:
    payload = _v3_payload([
        _row("base", 0.9, 0.0),
        _row("compute", 0.0, 0.10),
    ])

    result = validator_thin.vector_to_uid_weights(
        payload, {"base": 10, "compute": 12}
    )

    assert result == {10: 0.9, 12: 0.1}


def test_duplicate_uid_fails_for_v3_even_when_rows_overlap() -> None:
    payload = _v3_payload([
        _row("base", 0.5, 0.0),
        _row("overlap", 0.4, 0.1),
    ])

    with pytest.raises(validator_thin.wire.VectorError, match="duplicate UID"):
        validator_thin.vector_to_uid_weights(payload, {"base": 10, "overlap": 10})


@pytest.mark.parametrize("configured_fraction", [None, 0.0, -0.1, 0.100001, math.nan])
def test_malformed_global_fraction_fails(configured_fraction: object) -> None:
    payload = _v3_payload(
        [_row("base", 0.9, 0.0), _row("compute", 0.0, 0.1)],
        configured_fraction=configured_fraction,
    )

    with pytest.raises(validator_thin.wire.VectorError, match="fraction"):
        validator_thin.vector_to_uid_weights(payload, {"base": 10, "compute": 12})


def test_base_empty_v3_vector_fails_before_normalization() -> None:
    payload = _v3_payload([_row("compute", 0.0, 0.10)])

    with pytest.raises(validator_thin.wire.VectorError, match="positive base"):
        validator_thin.vector_to_uid_weights(payload, {"compute": 12})


def test_duplicate_signed_hotkey_fails_before_uid_mapping() -> None:
    payload = _v3_payload([
        _row("same", 0.9, 0.0),
        _row("same", 0.0, 0.1),
    ])

    with pytest.raises(validator_thin.wire.VectorError, match="duplicate hotkey"):
        validator_thin.vector_to_uid_weights(payload, {"same": 12})


@pytest.mark.parametrize(
    "rows",
    [
        [{"miner_hotkey": "missing", "weight": 0.1, "base_component": 0.1}],
        [_row("nan", math.nan, 0.0)],
        [_row("negative", -0.1, 0.0)],
        [{**_row("mismatch", 0.1, 0.1), "weight": 0.1}],
    ],
    ids=["missing-component", "nonfinite", "negative", "weight-mismatch"],
)
def test_malformed_v3_attribution_fails(rows: list[dict[str, object]]) -> None:
    payload = _v3_payload(rows)

    with pytest.raises(validator_thin.wire.VectorError):
        validator_thin.vector_to_uid_weights(payload, {"missing": 1, "nan": 1,
                                                        "negative": 1, "mismatch": 1})


def test_legacy_vector_keeps_skip_and_duplicate_merge_behavior() -> None:
    payload = {
        "weights": [
            {"miner_hotkey": "first", "weight": 0.6},
            {"miner_hotkey": "second", "weight": 0.4},
            {"miner_hotkey": "missing", "weight": 0.2},
        ],
        "burn_snapshot": {"burn_uid": None, "forced_burn_percentage": 0.0},
    }

    result = validator_thin.vector_to_uid_weights(
        payload, {"first": 7, "second": 7}
    )

    assert result == {7: 1.0}
