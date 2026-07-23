#!/usr/bin/env python3
"""Validator release gate — read-only pre-deploy check for SN39 weight setting.

Runs the plan's validator release-gate checks (deploy/RELIABILITY_UPGRADE_PLAN.md,
"Validator release gate" + "Vector Freshness Thresholds") against:

  - ALL THREE public weight-feed URLs over HTTPS (no auth) — canonical,
    legacy-prefixed, and the read-service direct host — for 5xx, signed-vector
    age, and identical signed bytes across URLs, and
  - the finney metagraph (READ ONLY) for the Cathedral validator update age and the count of
    validators fresh within one tempo (360 blocks / 72 min).

Two required gate items cannot be verified by a read-only probe (burn-snapshot
matches intended policy; stale_fallback served when origin is down). They are
emitted as explicit MANUAL items for an operator to confirm out-of-band — not
silently passed, not dead code — and do not flip the automated exit code.

It NEVER sets weights and NEVER writes to chain. It is safe to run from CI or a
laptop before any mainnet-affecting deploy. Each check prints PASS / FAIL and the
process exits non-zero if any required (non-manual) check fails.

Usage:
    python scripts/validator_release_gate.py
    python scripts/validator_release_gate.py --feed-url https://api.cathedral.computer
    python scripts/validator_release_gate.py --read-url https://read.cathedral.computer
    python scripts/validator_release_gate.py --no-chain     # skip chain checks
    python scripts/validator_release_gate.py --json         # machine-readable

Design note: all decision logic lives in pure functions (evaluate_*) that take
already-fetched values, so the gate's parsing/threshold logic is unit-testable
with mocked inputs and never needs the network in tests.
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Reuse the SAME thresholds the live health endpoint uses, so the gate and the
# running service can never silently disagree on what "fresh" means. Load the
# module by its file path beside this script's repo so the gate always uses THIS
# checkout's thresholds, even if a differently-versioned `scaffold` package is
# installed in the environment.
import importlib.util
import os as _os

_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_HT_PATH = _os.path.join(_REPO_ROOT, "scaffold", "publisher", "health_thresholds.py")
try:
    _ht_spec = importlib.util.spec_from_file_location(
        "cathedral_health_thresholds", _HT_PATH)
    ht = importlib.util.module_from_spec(_ht_spec)
    _ht_spec.loader.exec_module(ht)
except Exception:
    # Last resort: fall back to the installed package (keeps the script usable
    # even if invoked from an odd layout).
    from scaffold.publisher import health_thresholds as ht

# Pinned bittensor major version for the chain read. The metagraph attribute
# surface (block / last_update / validator_permit) and the Subtensor constructor
# are version-sensitive, so the gate asserts the running bittensor major matches
# what this code was written/verified against (10.x — verified locally 10.4.1).
# A mismatch is surfaced as an explicit FAIL rather than silently reading the
# wrong fields. Bump this constant when the env is intentionally upgraded.
BITTENSOR_REQUIRED_MAJOR = 10

# Current Cathedral SN39 validator identity and live service cadence. The UID is
# only a CLI default: operators still resolve their validator by hotkey before
# any write. The cadence matches config/validator.toml and scaffold.cli.
DEFAULT_VALIDATOR_UID = 30
DEFAULT_VALIDATOR_INTERVAL_SECONDS = 1500.0
# One bounded allowance for block timing, RPC latency, finalization, and systemd
# scheduling. The gate still fails after one missed configured cycle.
UID_UPDATE_SCHEDULING_GRACE_SECONDS = 120.0
DEFAULT_FEED_BASE = "https://api.cathedral.computer"
DEFAULT_READ_BASE = "https://read.cathedral.computer"
WEIGHTS_PATH = "/v1/validator/weights/next"
DEFAULT_NETUID = 39

# All three validator weight-feed URLs that must keep working, per the plan's
# "Validator URL Compatibility (all three must keep working)" section. Each is
# probed for: 200 + no 5xx, signed-vector freshness, and identical signed bytes
# for the same tempo. `source` is the JSON key returned by the feed identifying
# canonical vs legacy-prefixed vs read-service (kept for the gate's output).
#   - canonical:       api.cathedral.computer/v1/validator/weights/next
#   - legacy-prefixed: api.cathedral.computer/api/cathedral/v1/validator/weights/next
#   - read-service:    read.cathedral.computer/v1/validator/weights/next
def compat_urls(feed_base: str, read_base: str) -> list[tuple[str, str]]:
    """Return (label, full_url) for all three validator weight-feed URLs."""
    feed_base = feed_base.rstrip("/")
    read_base = read_base.rstrip("/")
    return [
        ("canonical", feed_base + WEIGHTS_PATH),
        ("legacy_prefixed", feed_base + "/api/cathedral" + WEIGHTS_PATH),
        ("read_service", read_base + WEIGHTS_PATH),
    ]


# --------------------------------------------------------------------------
# Pure parsing / threshold logic (unit-tested with mocked inputs)
# --------------------------------------------------------------------------
def parse_iso(ts: str | None) -> float | None:
    """Parse an ISO-8601 timestamp (the feed's `generated_at`) to epoch secs."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def age_seconds(generated_at: str | None, *, now: float | None = None) -> float | None:
    """Age in seconds of an ISO timestamp relative to `now` (default: real now)."""
    parsed = parse_iso(generated_at)
    if parsed is None:
        return None
    ref = now if now is not None else datetime.now(timezone.utc).timestamp()
    return round(ref - parsed, 3)


def evaluate_feed_status(status_code: int | None, error: str | None) -> dict[str, Any]:
    """Check: weights endpoint returns no 5xx.

    A connection error (no status) is also a FAIL — validators cannot read the
    feed either way.
    """
    if error is not None and status_code is None:
        return {"name": "weights_feed_no_5xx", "passed": False,
                "detail": f"feed unreachable: {error}"}
    if status_code is None:
        return {"name": "weights_feed_no_5xx", "passed": False,
                "detail": "no status code returned"}
    passed = status_code < 500
    return {
        "name": "weights_feed_no_5xx",
        "passed": passed,
        "detail": f"status={status_code} (require < 500)",
    }


def evaluate_vector_age(age: float | None) -> dict[str, Any]:
    """Check: signed-vector age <= GATE_VECTOR_MAX_AGE_SECONDS (5 min)."""
    if age is None:
        return {"name": "signed_vector_age", "passed": False,
                "detail": "no generated_at / unparseable timestamp"}
    passed = age <= ht.GATE_VECTOR_MAX_AGE_SECONDS
    return {
        "name": "signed_vector_age",
        "passed": passed,
        "detail": f"age={age:.1f}s (require <= {ht.GATE_VECTOR_MAX_AGE_SECONDS:.0f}s)",
    }


def evaluate_uid_update_age(
    blocks_since_update: int | None,
    *,
    block_seconds: float = ht.BLOCK_SECONDS,
    uid: int = DEFAULT_VALIDATOR_UID,
    validator_interval_seconds: float = DEFAULT_VALIDATOR_INTERVAL_SECONDS,
    weights_rate_limit_blocks: int | None = None,
    scheduling_grace_seconds: float = UID_UPDATE_SCHEDULING_GRACE_SECONDS,
) -> dict[str, Any]:
    """Check the validator update age against a feasible one-cycle deadline.

    `blocks_since_update` comes from chain (current_block - last_update[uid]).
    The live chain can forbid updates longer than the historical static gate
    limit, so the deadline is the largest of the static floor, configured
    validator interval, and live weight-rate limit, plus bounded scheduling
    grace. This prevents an impossible gate while still detecting one missed
    configured cycle.
    """
    if blocks_since_update is None:
        return {"name": f"uid{uid}_update_age", "passed": False,
                "detail": "no chain data (uid not found / chain unavailable)"}
    numeric = (block_seconds, validator_interval_seconds, scheduling_grace_seconds)
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
        for value in numeric
    ):
        return {"name": f"uid{uid}_update_age", "passed": False,
                "detail": "invalid block/cadence/grace configuration"}
    if weights_rate_limit_blocks is not None and weights_rate_limit_blocks < 0:
        return {"name": f"uid{uid}_update_age", "passed": False,
                "detail": "invalid chain weights rate limit"}
    age = blocks_since_update * block_seconds
    chain_minimum = (
        weights_rate_limit_blocks * block_seconds
        if weights_rate_limit_blocks is not None else 0.0
    )
    deadline = max(
        ht.GATE_UID200_MAX_AGE_SECONDS,
        validator_interval_seconds,
        chain_minimum,
    ) + scheduling_grace_seconds
    passed = age <= deadline
    return {
        "name": f"uid{uid}_update_age",
        "passed": passed,
        "detail": (f"{blocks_since_update} blocks (~{age:.0f}s); "
                   f"require <= {deadline:.0f}s "
                   f"(interval={validator_interval_seconds:.0f}s, "
                   f"rate_limit={weights_rate_limit_blocks}, "
                   f"grace={scheduling_grace_seconds:.0f}s)"),
    }


def evaluate_fresh_validators(
    blocks_since_update_by_uid: dict[int, int] | None,
    permits_by_uid: dict[int, bool] | None,
    *,
    tempo_blocks: int = ht.TEMPO_BLOCKS,
    min_fresh: int = 1,
) -> dict[str, Any]:
    """Check: count of permitted validators that set weights within 1 tempo.

    A validator is "fresh" if blocks_since_update <= tempo_blocks (360). The gate
    requires at least `min_fresh` permitted validators to be fresh (default 1 —
    at minimum our own must be; raise via flag to require quorum).
    """
    if not blocks_since_update_by_uid or not permits_by_uid:
        return {"name": "validators_fresh_within_tempo", "passed": False,
                "detail": "no chain data"}
    permitted = [uid for uid, ok in permits_by_uid.items() if ok]
    fresh = [
        uid for uid in permitted
        if blocks_since_update_by_uid.get(uid) is not None
        and blocks_since_update_by_uid[uid] <= tempo_blocks
    ]
    passed = len(fresh) >= min_fresh
    return {
        "name": "validators_fresh_within_tempo",
        "passed": passed,
        "detail": (f"{len(fresh)}/{len(permitted)} permitted validators fresh "
                   f"(<= {tempo_blocks} blocks); require >= {min_fresh}"),
        "fresh_uids": sorted(fresh),
        "permitted_count": len(permitted),
    }


def evaluate_url_compat(label: str, fetched: dict[str, Any]) -> dict[str, Any]:
    """Check one of the three validator URLs: reachable, no 5xx, fresh vector.

    `fetched` is a fetch_feed() result ({status, body, error}). This rolls the
    per-URL "returns 200 + signed vector" and "freshness within thresholds"
    matrix rows into a single pass/fail with detail, so every validator URL is
    probed, not just the canonical one (plan: URL Compatibility matrix).
    """
    name = f"url_compat[{label}]"
    status = fetched.get("status")
    error = fetched.get("error")
    if status is None:
        return {"name": name, "passed": False,
                "detail": f"unreachable: {error or 'no status'}"}
    if status >= 500:
        return {"name": name, "passed": False,
                "detail": f"status={status} (5xx — feed faulting)"}
    if status != 200:
        return {"name": name, "passed": False,
                "detail": f"status={status} (require 200 + signed vector)"}
    body = fetched.get("body") if isinstance(fetched.get("body"), dict) else None
    if not body or not body.get("signature"):
        return {"name": name, "passed": False,
                "detail": "200 but no signed vector (missing 'signature')"}
    age = age_seconds(body.get("generated_at"))
    if age is None:
        return {"name": name, "passed": False,
                "detail": "no generated_at / unparseable timestamp"}
    passed = age <= ht.GATE_VECTOR_MAX_AGE_SECONDS
    return {
        "name": name,
        "passed": passed,
        "detail": (f"status=200, age={age:.1f}s "
                   f"(require <= {ht.GATE_VECTOR_MAX_AGE_SECONDS:.0f}s)"),
        "signature": body.get("signature"),
    }


def evaluate_same_signed_bytes(
    signatures_by_label: dict[str, str | None],
) -> dict[str, Any]:
    """Check: all reachable URLs serve the SAME signed bytes for the same vector.

    Compares the Ed25519 `signature` field across URLs (identical signature ==
    identical signed payload). Only URLs that actually returned a signature are
    compared; an unreachable URL is already failed by its own url_compat check,
    so we do not double-penalise it here. Fewer than two signatures present ->
    nothing to cross-check (not a divergence), so this passes vacuously.
    """
    present = {lbl: sig for lbl, sig in signatures_by_label.items() if sig}
    distinct = set(present.values())
    if len(present) < 2:
        return {"name": "same_signed_bytes", "passed": True,
                "detail": f"only {len(present)} URL(s) returned a signature; "
                          "nothing to cross-check"}
    passed = len(distinct) == 1
    return {
        "name": "same_signed_bytes",
        "passed": passed,
        "detail": (f"{len(present)} URLs, {len(distinct)} distinct signature(s) "
                   f"({'match' if passed else 'DIVERGED'})"),
        "labels": sorted(present),
    }


def manual_check(name: str, reason: str) -> dict[str, Any]:
    """A required gate item that cannot be verified by a read-only probe.

    Surfaced explicitly as ``manual`` (not silently passed, not dead code) so the
    operator sees it in the gate output and confirms it out-of-band before a
    mainnet deploy. ``manual`` items do NOT fail the automated gate, but they are
    listed under a MANUAL banner and counted, so they cannot be forgotten.
    """
    return {"name": name, "passed": None, "manual": True, "detail": reason}


def gate_passed(checks: list[dict[str, Any]]) -> bool:
    # `manual` checks have passed=None and are intentionally not auto-failed;
    # they are reported separately for an operator to confirm. Only concrete
    # pass/fail checks gate the exit code.
    return all(c.get("passed") for c in checks if c.get("passed") is not None)


# --------------------------------------------------------------------------
# I/O layer (network) — kept thin so the logic above stays testable
# --------------------------------------------------------------------------
def fetch_feed(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """GET the weight feed. Returns {status, body(dict|None), error}. No raise."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "cathedral-release-gate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = None
            return {"status": int(resp.status), "body": body, "error": None}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = None
        return {"status": int(e.code), "body": body, "error": None}
    except Exception as e:
        return {"status": None, "body": None, "error": f"{type(e).__name__}: {e}"}


def check_bittensor_major(version: str | None,
                          *, required: int = BITTENSOR_REQUIRED_MAJOR) -> str | None:
    """Return a reason string if the bittensor major version is wrong, else None.

    Pure + unit-testable: the gate pins the major version it was verified against
    so a silently-upgraded env (different metagraph attribute surface) is caught
    rather than producing wrong update-age numbers.
    """
    if not version:
        return "bittensor.__version__ missing"
    try:
        major = int(str(version).split(".")[0])
    except Exception:
        return f"unparseable bittensor version {version!r}"
    if major != required:
        return (f"bittensor major {major} != pinned {required} "
                f"(installed {version}); update BITTENSOR_REQUIRED_MAJOR after "
                "re-verifying the metagraph attribute surface")
    return None


def fetch_chain(netuid: int, network: str = "finney") -> dict[str, Any]:
    """READ-ONLY metagraph snapshot for update-age checks. Never writes.

    Returns {available, current_block, blocks_since: {uid: int},
    permits: {uid: bool}, weights_rate_limit_blocks, reason}. Uses last_update
    (blocks of last weight set) per UID where exposed by the metagraph.
    """
    try:
        import bittensor  # lazy; not installed in every env
    except Exception as e:
        return {"available": False, "reason": f"bittensor not importable: {e}"}
    ver_reason = check_bittensor_major(getattr(bittensor, "__version__", None))
    if ver_reason is not None:
        return {"available": False, "reason": ver_reason}
    try:
        sub = None
        for ctor in ("subtensor", "Subtensor"):
            if hasattr(bittensor, ctor):
                try:
                    sub = getattr(bittensor, ctor)(network=network)
                    break
                except Exception:
                    sub = None
        if sub is None:
            from bittensor.core.subtensor import Subtensor
            sub = Subtensor(network=network)
        mg = sub.metagraph(netuid=netuid)
        current_block = int(getattr(mg, "block", 0) or 0)
        last_update = [int(x) for x in getattr(mg, "last_update", [])]
        permits = [bool(x) for x in getattr(mg, "validator_permit", [])]
        blocks_since = {
            uid: max(0, current_block - lu) for uid, lu in enumerate(last_update)
        }
        permits_by_uid = {uid: ok for uid, ok in enumerate(permits)}
        weights_rate_limit_blocks = int(sub.weights_rate_limit(netuid=netuid))
        return {
            "available": True,
            "current_block": current_block,
            "blocks_since": blocks_since,
            "permits": permits_by_uid,
            "weights_rate_limit_blocks": weights_rate_limit_blocks,
            "reason": None,
        }
    except Exception as e:
        return {"available": False, "reason": f"chain read failed: {e}"}


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def run_gate(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    context: dict[str, Any] = {}

    # All three validator URLs (canonical + legacy-prefixed + read-service) are
    # probed for reachability, no-5xx, and freshness, then cross-checked for
    # identical signed bytes — per the plan's URL Compatibility matrix.
    signatures_by_label: dict[str, str | None] = {}
    context["urls"] = {}
    canonical_feed: dict[str, Any] | None = None
    for label, url in compat_urls(args.feed_url, args.read_url):
        fetched = fetch_feed(url, timeout=args.timeout)
        if label == "canonical":
            canonical_feed = fetched
        context["urls"][label] = {
            "url": url, "status": fetched["status"], "error": fetched["error"],
        }
        compat = evaluate_url_compat(label, fetched)
        signatures_by_label[label] = compat.get("signature")
        # Drop the bulky signature out of the check itself; keep it in context.
        compat.pop("signature", None)
        checks.append(compat)
    checks.append(evaluate_same_signed_bytes(signatures_by_label))

    # Explicit canonical-feed 5xx + signed-vector-age checks (the highest-severity
    # signals get their own named gate items, not just the per-URL roll-up).
    feed = canonical_feed or {"status": None, "body": None, "error": "not fetched"}
    context["feed"] = {"status": feed["status"], "error": feed["error"]}
    checks.append(evaluate_feed_status(feed["status"], feed["error"]))

    body = feed.get("body") if isinstance(feed.get("body"), dict) else None
    generated_at = body.get("generated_at") if body else None
    context["generated_at"] = generated_at
    context["burn_snapshot"] = body.get("burn_snapshot") if body else None
    checks.append(evaluate_vector_age(age_seconds(generated_at)))

    if not args.no_chain:
        chain = fetch_chain(args.netuid, network=args.network)
        context["chain_available"] = chain.get("available")
        context["chain_reason"] = chain.get("reason")
        if chain.get("available"):
            blocks_since = chain["blocks_since"]
            permits = chain["permits"]
            context["current_block"] = chain.get("current_block")
            context["weights_rate_limit_blocks"] = chain.get(
                "weights_rate_limit_blocks")
            checks.append(evaluate_uid_update_age(
                blocks_since.get(args.uid),
                uid=args.uid,
                validator_interval_seconds=args.validator_interval_seconds,
                weights_rate_limit_blocks=chain.get("weights_rate_limit_blocks"),
            ))
            checks.append(evaluate_fresh_validators(
                blocks_since, permits, min_fresh=args.min_fresh_validators))
        else:
            checks.append({"name": f"uid{args.uid}_update_age", "passed": False,
                           "detail": f"chain unavailable: {chain.get('reason')}"})
            checks.append({"name": "validators_fresh_within_tempo", "passed": False,
                           "detail": f"chain unavailable: {chain.get('reason')}"})
    else:
        context["chain_skipped"] = True

    # Required gate items that a read-only probe cannot verify on its own. They
    # are reported as MANUAL (not silently passed, not dead code) so the operator
    # confirms them out-of-band before any mainnet-affecting deploy.
    burn = context.get("burn_snapshot")
    checks.append(manual_check(
        "burn_snapshot_matches_policy",
        "confirm burn_snapshot in the served vector matches intended policy "
        f"(no accidental burn/emission shift). served={burn!r}",
    ))
    checks.append(manual_check(
        "stale_fallback_when_origin_down",
        "kill/restart the read origin and confirm BOTH api.* routes still serve "
        "source=stale_fallback aged within 1 tempo (~72 min); read.* may depend "
        "on read-service health. Cannot be exercised from a read-only probe.",
    ))

    return checks, context


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SN39 validator release gate (read-only).")
    p.add_argument("--feed-url", default=DEFAULT_FEED_BASE,
                   help="base URL of the public weight feed (api.* host)")
    p.add_argument("--read-url", default=DEFAULT_READ_BASE,
                   help="base URL of the read-service direct host (read.*)")
    p.add_argument("--network", default="finney", help="bittensor network")
    p.add_argument("--netuid", type=int, default=DEFAULT_NETUID)
    p.add_argument("--uid", type=int, default=DEFAULT_VALIDATOR_UID,
                   help="validator UID for this read-only check (Cathedral = 30)")
    p.add_argument(
        "--validator-interval-seconds",
        type=float,
        default=DEFAULT_VALIDATOR_INTERVAL_SECONDS,
        dest="validator_interval_seconds",
        help="configured validator loop interval (default: 1500)",
    )
    p.add_argument("--min-fresh-validators", type=int, default=1, dest="min_fresh_validators",
                   help="minimum permitted validators fresh within one tempo")
    p.add_argument("--no-chain", action="store_true",
                   help="skip chain checks (feed-only)")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)
    if (
        not math.isfinite(args.validator_interval_seconds)
        or args.validator_interval_seconds <= 0
    ):
        p.error("--validator-interval-seconds must be positive")

    checks, context = run_gate(args)
    ok = gate_passed(checks)

    auto = [c for c in checks if c.get("passed") is not None]
    manual = [c for c in checks if c.get("passed") is None]

    if args.json:
        print(json.dumps(
            {"passed": ok, "checks": checks, "context": context}, indent=2))
    else:
        print("Validator release gate — read-only (no chain writes)")
        print(f"  feed: {args.feed_url}{WEIGHTS_PATH}")
        print(f"  read: {args.read_url}{WEIGHTS_PATH}")
        print("")
        for c in auto:
            tag = "PASS" if c.get("passed") else "FAIL"
            print(f"  [{tag}] {c['name']}: {c.get('detail', '')}")
        if manual:
            print("")
            print("  MANUAL (confirm out-of-band before deploy; not auto-gated):")
            for c in manual:
                print(f"  [MANUAL] {c['name']}: {c.get('detail', '')}")
        print("")
        print(f"GATE: {'PASS' if ok else 'FAIL'}"
              + (f"  (+{len(manual)} manual checks to confirm)" if manual else ""))

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
