"""Prepublish current + next V2 per-miner CNFs as immutable artifacts.

The driver uses the repository's existing Hippius presign helper when Hippius
credentials are configured, otherwise the existing S3/R2-compatible CNF bucket
backend.  It never reads the submit-token secret and never publishes assignment
manifests or tokens.

Usage:
    python3 -m scaffold.publisher.run_epoch_publish [hotkey ...] [--epoch N]

With no hotkeys, the current metagraph snapshot is used.  Every configured
per-miner allotment item is published for the selected epoch and epoch + 1;
partial runs remain explicitly not-ready and can be resumed idempotently.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from . import epoch_publisher
from . import per_miner as pm
from . import v2_pipeline
from . import weights as weights_mod
from .cnf_store import CNFStore
from .hippius_presign import HippiusPresign
from .store import Store


def _now_iso() -> str:
    value = datetime.now(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _store_from_env() -> Store:
    v2_database_path = (
        os.environ.get("CATHEDRAL_V2_DATABASE_URL", "").strip()
        or os.environ.get("CATHEDRAL_V2_DB_PATH", "").strip()
    )
    if v2_database_path and v2_pipeline.pm_payout_bridge_enabled():
        raise RuntimeError(
            "refusing split V2 store while the PM payout bridge is enabled"
        )
    if v2_database_path:
        return Store(v2_database_path, prefer_env_database_url=False)
    return Store(os.environ.get("CATHEDRAL_DB_PATH", "cathedral.db"))


def _artifact_io(store: Store):
    hip = HippiusPresign.from_env()
    if hip is not None:
        return (
            lambda key, data, content_type, cache_control: hip.put(
                key,
                data,
                content_type=content_type,
                cache_control=cache_control,
            ),
            lambda key: hip.get_if_exists(key),
            f"hippius:{hip.bucket}",
        )

    bucket = CNFStore(store)
    if bucket.immutable_object_backend_ready():
        return (
            bucket.put_immutable_object,
            bucket.get_immutable_object,
            "s3-compatible",
        )
    raise RuntimeError(
        "configure CATHEDRAL_HIPPIUS_TOKEN/CATHEDRAL_HIPPIUS_BUCKET or the "
        "existing CATHEDRAL_CNF_BACKEND=bucket S3/R2 credentials"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "hotkeys",
        nargs="*",
        help="raw miner hotkeys; omit to use the metagraph snapshot",
    )
    parser.add_argument(
        "--epoch", type=int, default=None, help="base epoch (default current)"
    )
    args = parser.parse_args()

    try:
        store = _store_from_env()
        put_object, get_object, backend_name = _artifact_io(store)
    except Exception as exc:
        print(f"E: artifact backend unavailable: {exc}", file=sys.stderr)
        return 2

    base_url = os.environ.get("CATHEDRAL_V2_CNF_ARTIFACT_BASE_URL", "").strip()
    if not base_url:
        print(
            "E: set CATHEDRAL_V2_CNF_ARTIFACT_BASE_URL to the stable public/CDN base",
            file=sys.stderr,
        )
        return 2

    with v2_pipeline.v2_pm_env():
        if not pm.seed_secret_configured():
            print("E: stable per-miner seed secret is required", file=sys.stderr)
            return 2
        raw_hotkeys = list(args.hotkeys)
        if not raw_hotkeys:
            rows = store.query("SELECT DISTINCT hotkey FROM metagraph_hotkeys")
            raw_hotkeys = [str(row["hotkey"]) for row in rows]
        identities = sorted(
            {
                weights_mod.scoring_identity_for_hotkey(
                    store, hotkey, require_mapped=False
                )
                or hotkey
                for hotkey in raw_hotkeys
                if hotkey
            }
        )
        if not identities:
            print("E: no assignment identities to publish", file=sys.stderr)
            return 2

        epoch = int(args.epoch) if args.epoch is not None else pm.current_epoch()
        allotment = {tier: pm.allotment_for(tier) for tier in pm.TIERS}
        progress_count = 0

        def progress(_event, data):
            nonlocal progress_count
            progress_count += 1
            if progress_count % 100 == 0:
                print(
                    f"  verified={progress_count} epoch={data['epoch']} "
                    f"tier={data['tier']} seq={data['seq']}",
                    flush=True,
                )

        print(
            f"Publishing epoch pair {epoch}/{epoch + 1}: identities={len(identities)} "
            f"allotment={allotment} backend={backend_name}",
            flush=True,
        )
        try:
            summary = epoch_publisher.prepublish_epoch_pair(
                store,
                epoch=epoch,
                assignment_identities=identities,
                allotment_by_tier=allotment,
                cnf_base_url=base_url,
                put_object=put_object,
                get_object=get_object,
                published_at=_now_iso(),
                on_progress=progress,
            )
        except Exception as exc:
            print(
                f"E: publication incomplete (readiness remains false): {exc}",
                file=sys.stderr,
            )
            return 1

    print(f"SUMMARY: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
