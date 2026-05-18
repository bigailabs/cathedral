"""Minimal calculator module with one injected logical fault.

The ``compute_discount`` function applies a percentage discount to a
price. The shipped version has an off-by-one: it adds the discount
instead of subtracting it. A correct miner patch flips ``+`` to ``-``.
"""

from __future__ import annotations


def compute_discount(price: float, percent: float) -> float:
    """Return ``price`` minus ``percent`` percent.

    Example: compute_discount(100, 10) == 90.0
    """
    factor = percent / 100.0
    # INJECTED FAULT: should be `price - price * factor`
    return price + price * factor


def apply_tax(amount: float, tax_rate: float) -> float:
    """Add a tax rate (already a fraction, e.g. 0.08) to ``amount``."""
    return amount + amount * tax_rate
