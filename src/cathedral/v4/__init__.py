"""Cathedral v4 -- publisher-side private challenge runtime.

**Architectural position (REVISED 2026-05-17):**

v4 is a publisher-side runtime, NOT an inline validator engine.

  * The publisher's worker host loads private synthetic rows,
    scrambles upstream vault repos, applies the synthetic bug
    server-side, builds the broken-state bundle the miner sees,
    runs the miner's fix patch under a bounded hermetic oracle,
    packages the signed envelope, and persists it.
  * The validator NEVER executes any of this. Validators only pull
    signed v4 rows from the publisher feed and verify the
    Ed25519 signature via ``cathedral.v4.verify.verify_v4_row``.
    No subprocess, no patch-runner, no file I/O on the validator
    side.

**Package import boundary (Finding 3, PR #133 review):**

This top-level package deliberately re-exports ONLY the
validator-safe surface plus wire schemas. Publisher-only symbols
(engine, arena, oracle, sign) must be imported by their full path
so a validator-only consumer cannot accidentally drag the patch
runner (and its subprocess / unshare / ctypes dependencies) into
its process. Concretely:

  * ``from cathedral.v4 import verify_v4_row``  -- OK on validator
  * ``from cathedral.v4 import CathedralEngine`` -- removed; use
    ``from cathedral.v4.cathedral_engine import CathedralEngine``
  * ``from cathedral.v4 import run_patch_against_hidden_test`` --
    removed; use
    ``from cathedral.v4.oracle.patch_runner import
    run_patch_against_hidden_test``

The wire schemas (``AgentTurn``, ``MinerTrajectory``,
``ValidationPayload``, ``MinerBundle``) re-export here because
both publisher and validator paths consume them and they pull no
heavy machinery.
"""

from cathedral.v4.schemas import (
    AgentTurn,
    MinerBundle,
    MinerTrajectory,
    ValidationPayload,
)
from cathedral.v4.verify import VerifyError, verify_v4_row

__all__ = [
    "AgentTurn",
    "MinerBundle",
    "MinerTrajectory",
    "ValidationPayload",
    "VerifyError",
    "verify_v4_row",
]
