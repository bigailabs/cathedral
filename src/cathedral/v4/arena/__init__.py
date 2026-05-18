"""Arena -- the miner-facing simulation surface for v4.

Exposes the three atomic tool operations a miner agent runtime is
allowed to call against a scrambled micro-repository workspace:

  * ``read_file``
  * ``write_patch``
  * ``run_local_compile``

Also exposes the ``IsomorphicScrambler``, which loads a base repo from
``vault/`` and produces a structurally-equivalent but lexically
scrambled workspace from a deterministic seed.
"""

from cathedral.v4.arena.sandbox import (
    ArenaError,
    IsomorphicScrambler,
    MinerArena,
    ScrambledRepo,
)

__all__ = [
    "ArenaError",
    "IsomorphicScrambler",
    "MinerArena",
    "ScrambledRepo",
]
