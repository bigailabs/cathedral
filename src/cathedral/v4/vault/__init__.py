"""Pre-baked base repositories for the v4 patch validator.

Each subdirectory is a self-contained micro-repo (<5MB) used as input
to the ``IsomorphicScrambler``. The validator picks one per challenge,
scrambles it, and presents the result to a miner agent.

Repos:

  * ``python_fastapi_base/`` -- minimal FastAPI app (one route, one
    Pydantic model, one pytest). Canonical path used by the
    benchmark and the default integration tests.
  * ``ts_prisma_base/`` -- minimal TypeScript + Prisma app (one model,
    one bun test). Gated on ``bun`` being on PATH; the python repo is
    the always-on validator path.

Each repo MUST include a ``scramble.json`` manifest declaring:

  * ``language``: short string tag
  * ``renamable_identifiers``: list[str] of internal symbol names safe
    to mutate
  * ``renamable_files``: list[str] of repo-relative file paths whose
    basenames may be suffixed (reserved entry points are skipped)
  * ``string_rotations``: dict[str, list[str]] of safe literal strings
    that may be rotated per seed
  * ``compile_command``: list[str] argv invoked by
    ``MinerArena.run_local_compile``
  * ``test_entry``: repo-relative path of the canonical test file
  * ``hidden_test_template``: source code of the validator's hidden
    test, with ``{{rename:<original>}}`` placeholders that the engine
    rewrites against the scrambled rename_map before invoking the
    oracle
"""

VAULT_PACKAGE = True
