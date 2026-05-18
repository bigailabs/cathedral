"""Oracle -- the validator-side hermetic patch runner.

The oracle is the only path that runs miner-submitted patches against
the hidden verification test. It is designed for a hard sub-200ms
budget and a 100% offline runtime: no docker, no firecracker, no
network, no host filesystem writes outside ``/dev/shm`` (or the
platform tmpfs equivalent).
"""

from cathedral.v4.oracle.patch_runner import (
    OracleError,
    PatchRunResult,
    run_patch_against_hidden_test,
)

__all__ = [
    "OracleError",
    "PatchRunResult",
    "run_patch_against_hidden_test",
]
