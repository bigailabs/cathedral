"""Lane registry. The publisher looks up ``TaskFamily`` implementations
by ``family_id``. Wiring into ``score_and_sign`` happens in a follow-up
PR; for now this is the lookup the contract test suite uses.

Adding a lane: implement ``TaskFamily`` in
``src/cathedral/lanes/<family_id>/__init__.py``, then register it here.
"""

from __future__ import annotations

from cathedral.lanes.contract import TaskFamily

_REGISTRY: dict[str, TaskFamily] = {}


def register(family: TaskFamily) -> None:
    if family.family_id in _REGISTRY:
        raise ValueError(f"lane already registered: {family.family_id}")
    _REGISTRY[family.family_id] = family


def lookup(family_id: str) -> TaskFamily:
    try:
        return _REGISTRY[family_id]
    except KeyError as e:
        raise KeyError(f"unknown lane: {family_id}") from e


def active() -> list[TaskFamily]:
    return list(_REGISTRY.values())


# --------------------------------------------------------------------------
# Eager registration of in-tree lanes. New lanes append here.
# --------------------------------------------------------------------------

# from cathedral.lanes.synthetic_maxsat_v1 import MaxsatV1
# register(MaxsatV1())
