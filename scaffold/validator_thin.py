"""The thin v4 validator — the WHOLE validator, ~200 lines.

    fetch signed scores from the orchestrator
    -> verify Ed25519 signature against the pinned key
    -> sanity-check (finite, nonnegative, fresh, right subnet, no rollback)
    -> apply burn FROM THE SAME SIGNED PAYLOAD
    -> map hotkeys to uids against the live metagraph
    -> set weights

No local row database. No backfill. No rolling window. No score buckets.
Every scoring decision (recency, multi-lane composition, burn) lives
orchestrator-side and changes WITHOUT a validator release; this binary only
enforces that what it applies is exactly what the pinned key signed.

Run:  python -m scaffold.validator_thin --publisher-url https://api.cathedral.computer \
          --public-key-hex <pinned hex> [--once] [--broadcast]

Dry-run by default (computes + prints the uid vector, does not submit).
Rollback fence state persists in a small JSON file (--state-file), so a
publisher cannot re-serve an older policy_version after a restart.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import wire_vector as wire


def _ms_iso_now() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def fetch_vector(publisher_url: str, timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(
        publisher_url.rstrip("/") + "/v1/validator/weights/next",
        headers={"User-Agent": "cathedral-thin-validator/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# -- rollback fence ------------------------------------------------------------

def load_fence(state_file: Path) -> int:
    """FAIL CLOSED: only a genuinely absent state file means 'no fence yet'.
    A corrupt/unreadable file raises (the tick fails) instead of silently
    resetting the fence to -1 and reopening the rollback window."""
    if not state_file.exists():
        return -1
    return int(json.loads(state_file.read_text())["last_accepted_policy_version"])


def save_fence(state_file: Path, version: int, vector_id: str) -> None:
    """Atomic write (tmp + rename) so a crash mid-write can't corrupt the
    fence — which would brick the fail-closed load above."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "last_accepted_policy_version": version,
        "last_vector_id": vector_id,
        "accepted_at": _ms_iso_now(),
    }, indent=2))
    os.replace(tmp, state_file)


# -- burn + uid mapping ---------------------------------------------------------

def apply_burn(scores_by_uid: dict[int, float], *, burn_uid: int | None,
               forced_burn_percentage: float) -> dict[int, float]:
    """burn% of total mass to burn_uid, remainder split proportionally across
    miners; normalized to sum 1.0. Empty miner set -> everything to burn_uid."""
    burn_frac = forced_burn_percentage / 100.0
    if burn_uid is not None:
        # burn_uid must never double-collect (miner share + forced burn);
        # any score that mapped onto it is dropped before allocation.
        scores_by_uid = {u: v for u, v in scores_by_uid.items() if u != burn_uid}
    total = sum(scores_by_uid.values())
    if total <= 0 or not scores_by_uid:
        if burn_uid is None:
            raise wire.VectorError("no miner mass and no burn_uid fallback")
        return {burn_uid: 1.0}
    out = {uid: (v / total) * (1.0 - burn_frac) for uid, v in scores_by_uid.items()}
    if burn_uid is not None and burn_frac > 0:
        out[burn_uid] = out.get(burn_uid, 0.0) + burn_frac
    norm = sum(out.values())
    return {uid: v / norm for uid, v in out.items()}


def accept_vector(payload: dict[str, Any], *, public_key_hex: str, key_id: str,
                  network: str, netuid: int, fence_version: int) -> None:
    """Every check between 'bytes arrived' and 'safe to apply'. Raises on any
    failure — there is deliberately no partial acceptance."""
    wire.verify_signature(payload, public_key_hex=public_key_hex, expected_key_id=key_id)
    wire.invariant_check(payload, network=network, netuid=netuid, now_iso=_ms_iso_now())
    pv = int(payload["policy_version"])
    if pv < fence_version:
        raise wire.VectorError(
            f"rollback: vector policy_version {pv} < last accepted {fence_version}")


def vector_to_uid_weights(payload: dict[str, Any],
                          hotkey_to_uid: dict[str, int]) -> dict[int, float]:
    snap = payload["burn_snapshot"]
    scores: dict[int, float] = {}
    skipped = 0
    for w in payload["weights"]:
        uid = hotkey_to_uid.get(w["miner_hotkey"])
        if uid is None:
            skipped += 1          # deregistered since the vector was composed
            continue
        scores[uid] = scores.get(uid, 0.0) + float(w["weight"])
    if skipped:
        print(f"  ({skipped} hotkeys not in metagraph, skipped)")
    return apply_burn(scores, burn_uid=snap.get("burn_uid"),
                      forced_burn_percentage=float(snap["forced_burn_percentage"]))


# -- chain ----------------------------------------------------------------------

def set_weights_on_chain(uid_weights: dict[int, float], *, network: str, netuid: int,
                         wallet_name: str, wallet_hotkey: str, broadcast: bool) -> bool:
    ordered = sorted(uid_weights.items())
    print(f"  vector ({len(ordered)} uids): " +
          ", ".join(f"{u}={w:.4f}" for u, w in ordered[:12]) +
          (" …" if len(ordered) > 12 else ""))
    if not broadcast:
        print("  DRY-RUN — pass --broadcast to submit")
        return True
    import bittensor as bt
    wallet = _bt_wallet(bt)(name=wallet_name, hotkey=wallet_hotkey)
    sub = _bt_subtensor(bt)(network=network)
    uids = [u for u, _ in ordered]
    vals = [w for _, w in ordered]
    resp = sub.set_weights(wallet=wallet, netuid=netuid, uids=uids, weights=vals,
                           wait_for_inclusion=True)
    # newer bittensor returns an ExtrinsicResponse object (truthy even on
    # failure) — judge success by the field, not truthiness.
    ok = bool(getattr(resp, "success", resp))
    print(f"  set_weights submitted: success={ok} ({resp})")
    return ok


def _bt_subtensor(bt):
    """bittensor renamed `subtensor` -> `Subtensor` across major versions."""
    return getattr(bt, "subtensor", None) or bt.Subtensor


def _bt_wallet(bt):
    return getattr(bt, "wallet", None) or bt.Wallet


def metagraph_hotkey_to_uid(*, network: str, netuid: int) -> dict[str, int]:
    import bittensor as bt
    mg = _bt_subtensor(bt)(network=network).metagraph(netuid)
    return {hk: int(uid) for uid, hk in zip(mg.uids.tolist(), mg.hotkeys)}


# -- main loop --------------------------------------------------------------------

def tick(args) -> bool:
    payload = fetch_vector(args.publisher_url)
    fence = load_fence(Path(args.state_file))
    accept_vector(payload, public_key_hex=args.public_key_hex, key_id=args.key_id,
                  network=args.network, netuid=args.netuid, fence_version=fence)
    print(f"accepted vector {payload['vector_id'][:8]}… policy_version={payload['policy_version']} "
          f"miners={len(payload['weights'])} "
          f"burn={payload['burn_snapshot']['forced_burn_percentage']}%")
    if args.broadcast or not args.offline:
        hk2uid = metagraph_hotkey_to_uid(network=args.network, netuid=args.netuid)
    else:
        hk2uid = {w["miner_hotkey"]: i for i, w in enumerate(payload["weights"])}
        print("  (offline: synthetic uid map, dry-run only)")
    uid_weights = vector_to_uid_weights(payload, hk2uid)
    ok = set_weights_on_chain(uid_weights, network=args.network, netuid=args.netuid,
                              wallet_name=args.wallet_name, wallet_hotkey=args.wallet_hotkey,
                              broadcast=args.broadcast)
    if ok:
        save_fence(Path(args.state_file), int(payload["policy_version"]), payload["vector_id"])
    return ok


def run(args) -> int:
    """The validator loop, shared by `python -m scaffold.validator_thin` and the
    `cathedral-validator serve` console command. `args` is any object carrying
    the tick attributes (an argparse Namespace or a SimpleNamespace from the
    CLI's config loader)."""
    while True:
        try:
            tick(args)
        except Exception as e:
            print(f"tick failed: {e}")
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval_secs)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cathedral thin validator (v4)")
    p.add_argument("--publisher-url", default=os.environ.get(
        "CATHEDRAL_PUBLISHER_URL", "https://api.cathedral.computer"))
    p.add_argument("--public-key-hex", default=os.environ.get(
        "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY", ""), help="pinned Ed25519 public key (hex)")
    p.add_argument("--key-id", default=os.environ.get(
        "CATHEDRAL_WEIGHT_POLICY_KEY_ID", "cathedral-weight-policy"))
    p.add_argument("--network", default="finney")
    p.add_argument("--netuid", type=int, default=39)
    p.add_argument("--wallet-name", default=os.environ.get("BT_WALLET_NAME", "validator"))
    p.add_argument("--wallet-hotkey", default=os.environ.get("BT_WALLET_HOTKEY", "default"))
    p.add_argument("--state-file", default=os.environ.get(
        "CATHEDRAL_VALIDATOR_STATE", str(Path.home() / ".cathedral" / "thin_validator.json")))
    p.add_argument("--interval-secs", type=float, default=1500.0)
    p.add_argument("--once", action="store_true", help="single tick, then exit")
    p.add_argument("--offline", action="store_true",
                   help="no chain access: verify + print only (CI / smoke)")
    p.add_argument("--broadcast", action="store_true",
                   help="actually submit weights (default: dry-run)")
    return p


def main() -> int:
    p = build_parser()
    args = p.parse_args()
    if not args.public_key_hex:
        p.error("--public-key-hex (or CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY) is required — "
                "validators must pin the orchestrator's signing key")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
