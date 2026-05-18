"""Tiny app surface that exercises calculator.

Deliberately stdlib-only so the miner-facing arena compile step works
on any host with python3 -- no pydantic / fastapi runtime dep needed
for the validator loop to verify the patch geometry.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.calculator import compute_discount


@dataclass(frozen=True)
class PriceRequest:
    price: float
    discount_percent: float


@dataclass(frozen=True)
class PriceResponse:
    final_price: float


def price_endpoint(req: PriceRequest) -> PriceResponse:
    """Pure function backing the /price route. Pulled out of the
    framework decorator so tests don't need an ASGI runtime.
    """
    final = compute_discount(req.price, req.discount_percent)
    return PriceResponse(final_price=final)
