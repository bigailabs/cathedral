"""Smoke test the miner sees. Asserts the basic API surface compiles.

The miner-visible test deliberately does NOT pin numerical behaviour
-- that is the validator's hidden test job. The miner's local compile
step is meant to verify the patch did not break imports.
"""

from __future__ import annotations

from app.calculator import apply_tax, compute_discount


def test_compute_discount_callable() -> None:
    result = compute_discount(100.0, 10.0)
    assert isinstance(result, float)


def test_apply_tax_callable() -> None:
    result = apply_tax(100.0, 0.08)
    assert result == 108.0
