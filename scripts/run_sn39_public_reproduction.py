#!/usr/bin/env python3
"""Reproduce the immutable, root-signed SN39 launch decision."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Direct execution sets sys.path[0] to ``scripts/`` rather than the pristine
# release root. Bind imports to the checkout that contains this reviewed
# runner; the reproducer subsequently verifies that checkout is the exact
# root-signed revision and has no tracked, untracked, or ignored changes.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.assert_sn39_public_reproduction import (  # noqa: E402
    ReproductionError,
    ReproductionNotProven,
    assert_public_reproduction,
)


def run(
    *,
    release_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify signed release, archive state, and frozen public evidence."""
    return assert_public_reproduction(release_result=release_result)


def main() -> int:
    if len(sys.argv) != 1:
        print("usage: run_sn39_public_reproduction.py", file=sys.stderr)
        return 2
    try:
        result = run()
    except ReproductionNotProven as exc:
        print(f"SN39 public reproduction: NOT_PROVEN: {exc}", file=sys.stderr)
        return 3
    except ReproductionError as exc:
        print(f"SN39 public reproduction: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "SN39 public reproduction: PASS "
        + json.dumps(result, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
