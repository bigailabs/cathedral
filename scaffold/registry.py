"""Lane registry — the one seam by which lanes plug into the core.

The validator looks lanes up by family_id. Adding a lane = implement Lane,
append a register() call. Removing one = delete the call. Nothing else moves.
"""
from __future__ import annotations

from .contract import Lane

_REGISTRY: dict[str, Lane] = {}


def register(lane: Lane) -> None:
    if lane.family_id in _REGISTRY:
        raise ValueError(f"lane already registered: {lane.family_id}")
    _REGISTRY[lane.family_id] = lane


def lookup(family_id: str) -> Lane:
    try:
        return _REGISTRY[family_id]
    except KeyError as e:
        raise KeyError(f"unknown lane: {family_id}") from e


def active() -> list[Lane]:
    return list(_REGISTRY.values())


def install_default_lanes() -> None:
    """Eager registration of the three in-tree lanes. Import-on-call so the
    core has no static dependency on any lane."""
    from .lanes.sat_challenge import SatChallengeLane
    from .lanes.solver_docker import SolverDockerLane
    from .lanes.encoding import EncodingLane
    from .lanes.solver_arena import SolverArenaLane

    # SolverArenaLane (Lane S) is STATEFUL — its champion + registry persist
    # across rounds, so the core holds one instance for the validator's lifetime.
    for lane in (SatChallengeLane(), SolverDockerLane(), EncodingLane(),
                 SolverArenaLane()):
        if lane.family_id not in _REGISTRY:
            register(lane)
