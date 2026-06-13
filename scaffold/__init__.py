"""Cathedral subnet scaffold — a minimalist, lane-based subnet.

Shared core (contract, registry, grading, verify, polaris, validator, pinning)
+ three independent lanes (sat_challenge, solver_docker, encoding). Each lane is
a self-contained {mint_challenge, validate_submission, score}; adding or removing
one touches neither the core nor the other lanes.

Run the end-to-end demo:  python -m scaffold.demo
"""
from __future__ import annotations

__version__ = "4.0.0-rc.4"

__all__ = ["registry", "validator", "contract"]
