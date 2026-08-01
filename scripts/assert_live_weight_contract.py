#!/usr/bin/env python3
"""Pre-cutover gate: does the LIVE weight feed satisfy the launch-locked contract?

Run this BEFORE upgrading a validator. Exit 0 means the running publisher emits a
vector the launch-locked validator will accept; non-zero means the cutover would
fail closed to the 100% burn vector from its first tick.

Why this exists (cathedral#400). The live publisher once signed `validated_supply`
contract v1 while the launch-locked validator and the pinned provenance contract
required v2. Upgrading the validator without upgrading the publisher in the same
window would have burned everything, and the cutover sequence had no step that
would have caught it. The publisher has since been upgraded, which is exactly why
this is worth having: the fix arrived out of band, so nothing today would notice
a regression back to v1 until miners stopped being paid.

It deliberately calls `validator_thin._validated_supply_meta` -- the validator's
OWN check -- rather than re-implementing the field list here. A second copy of a
contract is a second thing to keep in sync, and the failure mode of drift is that
this script says yes and the validator says no. Using the real function means a
pass here is the same computation the validator will perform.

Usage:
    python scripts/assert_live_weight_contract.py
    python scripts/assert_live_weight_contract.py --publisher https://api.cathedral.computer
    python scripts/assert_live_weight_contract.py --json          # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scaffold import validator_thin  # noqa: E402
from scaffold import wire_vector as wire  # noqa: E402

DEFAULT_PUBLISHER = "https://api.cathedral.computer"
WEIGHTS_PATH = "/v1/validator/weights/next"
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_SECS = 30


def fetch_vector(publisher: str) -> dict:
    url = publisher.rstrip("/") + WEIGHTS_PATH
    # An explicit User-Agent: the edge in front of this API answers 403 to
    # urllib's default, which would otherwise read as "the feed is down".
    request = urllib.request.Request(
        url, headers={"User-Agent": "cathedral-precutover-check/1", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECS) as response:
        if response.status != 200:
            raise SystemExit(f"FAIL  {url} returned HTTP {response.status}")
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise SystemExit(f"FAIL  {url} returned more than {MAX_BYTES} bytes")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SystemExit(f"FAIL  {url} did not return JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--publisher", default=DEFAULT_PUBLISHER)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        payload = fetch_vector(args.publisher)
    except (urllib.error.URLError, OSError) as exc:
        print(f"FAIL  could not reach {args.publisher}{WEIGHTS_PATH}: {exc}", file=sys.stderr)
        return 2

    try:
        policy = validator_thin._validated_supply_meta(payload)
    except wire.VectorError as exc:
        # This is verbatim what the validator would raise on its first tick.
        detail = {"ok": False, "error": str(exc)}
        print(json.dumps(detail) if args.as_json else f"FAIL  {exc}", file=sys.stderr)
        print(
            "\nDO NOT CUT OVER. The launch-locked validator refuses this vector, so "
            "every tick would fail closed to the 100% burn vector from the first one. "
            "Upgrade the weight-policy publisher first (cathedral#400).",
            file=sys.stderr,
        )
        return 1

    if policy is None:
        print("FAIL  the live vector carries no validated_supply policy metadata", file=sys.stderr)
        return 1

    snapshot = payload.get("burn_snapshot") or {}
    result = {
        "ok": True,
        "publisher": args.publisher,
        "contract_version": policy["contract_version"],
        "intel_tdx_allocation": policy["intel_tdx_allocation"],
        "fixed_burn_allocation": policy["fixed_burn_allocation"],
        "burn_hotkey": policy["burn_hotkey"],
        "forced_burn_percentage": snapshot.get("forced_burn_percentage"),
    }
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("PASS  the live feed satisfies the launch-locked contract")
        for key in (
            "contract_version", "intel_tdx_allocation", "fixed_burn_allocation",
            "burn_hotkey", "forced_burn_percentage",
        ):
            print(f"  {key:24} {result[key]}")
        print("\nChecked with the validator's own _validated_supply_meta, so a pass "
              "here is the computation the validator will perform.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
