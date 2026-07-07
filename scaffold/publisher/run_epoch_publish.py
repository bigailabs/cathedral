"""Runnable driver: precompute one epoch's per-miner assignment and publish it to
Hippius (presigned-URL backend). Miners then read the manifest + CNFs from the
edge instead of polling the origin.

Env:
  CATHEDRAL_HIPPIUS_TOKEN, CATHEDRAL_HIPPIUS_BUCKET  — the presigned backend
  CATHEDRAL_PERMINER_SEED_SECRET                     — deterministic generation
  CATHEDRAL_V2_SUBMIT_TOKEN_SECRET                   — token minting

Usage: python -m scaffold.publisher.run_epoch_publish <hotkey> [--epoch N]
       [--t1 N --t2 N]  (allotment per tier; keep small for a dogfood)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys

from . import epoch_publisher as EP
from . import per_miner as pm
from .hippius_presign import HippiusPresign


def _now_iso():
    from datetime import datetime, timezone
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _iso_plus(secs: int):
    from datetime import datetime, timezone, timedelta
    d = datetime.now(timezone.utc) + timedelta(seconds=secs)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hotkey")
    ap.add_argument("--epoch", type=int, default=None)
    ap.add_argument("--t1", type=int, default=8)
    ap.add_argument("--t2", type=int, default=8)
    ap.add_argument("--ttl", type=int, default=7200, help="presigned/token TTL secs")
    args = ap.parse_args()

    hip = HippiusPresign.from_env()
    if not hip:
        print("E: set CATHEDRAL_HIPPIUS_TOKEN + CATHEDRAL_HIPPIUS_BUCKET", file=sys.stderr)
        return 2
    token_secret = os.environ.get("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "").strip()
    if not token_secret:
        print("E: set CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", file=sys.stderr)
        return 2

    epoch = args.epoch if args.epoch is not None else pm.current_epoch()

    # Deterministic reveal nonce from (epoch, hotkey, secret) — no rng needed and
    # stable across re-runs of the same epoch (idempotent publish).
    nonce = hashlib.sha256(
        f"reveal:{epoch}:{args.hotkey}".encode() + token_secret.encode()
    ).hexdigest()[:24]

    published_at = _now_iso()
    expires_at = _iso_plus(args.ttl)

    # put_object mints a presigned url via Hippius and returns it.
    def put_object(key: str, data: bytes, content_type: str) -> str:
        return hip.put(key, data, content_type=content_type)

    # Manifests carry an ed25519 publisher signature; for the dogfood we sign with
    # a simple HMAC over the token secret (defense-in-depth only — submit re-verifies
    # the token + sha independently, so this is not the security root).
    def sign(body: bytes) -> str:
        import hmac
        return base64.b64encode(
            hmac.new(token_secret.encode(), body, hashlib.sha256).digest()
        ).decode()

    def progress(ev, data):
        print(f"  [{ev}] {data}")

    print(f"Publishing epoch {epoch} for {args.hotkey} "
          f"(t1={args.t1} t2={args.t2}) to bucket={hip.bucket} ...")
    summary = EP.publish_epoch(
        epoch=epoch, hotkeys=[args.hotkey], token_secret=token_secret,
        expires_at=expires_at, published_at=published_at, reveal_nonce=nonce,
        allotment_by_tier={1: args.t1, 2: args.t2},
        cnf_base_url="",  # presigned backend embeds per-item cnf_url instead
        put_object=put_object, sign=sign, on_progress=progress,
    )
    # Publish latest.json content already done inside publish_epoch; also print the
    # miner-facing pointer url so we can point the miner at it.
    pointer_url = hip.get_url("latest.json", expires_in=args.ttl)
    print("SUMMARY:", summary)
    print("POINTER_URL:", pointer_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
