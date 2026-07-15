#!/usr/bin/env python3
"""Pre-bake per-miner CNFs into the persistent v2_cnf_store.

Why: the V2 per-miner challenges/cnf handlers read through v2_cnf_store and
only generate on a miss. Generation is the expensive part (the 2026-07-08
reopen melt). Baking the first PREBAKE_DEPTH seqs per (hotkey, tier) for an
epoch makes reopen minute-zero reads cheap for every miner at once, which is
also the fair way to reopen: nobody waits on cold generation.

The allotment is a lazy virtual 10k/tier; miners only page as deep as they can
solve, so baking the head of each miner's set covers the hot window and the
read-through store fills the tail organically.

Runs on the sandbox with /home/polaris/cathedral/.env.sh sourced so generation
env (seed secret, shapes, real fraction) matches the serving processes exactly.
The store selection mirrors app.py: if a V2 DB is configured, bake there;
otherwise bake into the shared main store.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.environ.get("CATHEDRAL_REPO_DIR", "/home/polaris/cathedral"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int,
                    default=int(os.environ.get("CATHEDRAL_PREBAKE_DEPTH", "10")),
                    help="seqs to bake per (hotkey, tier)")
    ap.add_argument("--epoch", type=int, default=None,
                    help="exact per-miner epoch to bake; overrides --epoch-offset-secs")
    ap.add_argument("--epoch-offset-secs", type=int, default=300,
                    help="bake the epoch active this many seconds from now")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--cache-clear-every", type=int,
                    default=int(os.environ.get("CATHEDRAL_PREBAKE_CACHE_CLEAR_EVERY", "512")),
                    help="clear generation LRU caches after this many item attempts; 0 disables")
    args = ap.parse_args()

    from scaffold.publisher import v2_pipeline
    from scaffold.publisher import per_miner as pm
    from scaffold.publisher import v2_cnf_store
    from scaffold.publisher.store import Store

    v2_database_path = (
        os.environ.get("CATHEDRAL_V2_DATABASE_URL", "").strip()
        or os.environ.get("CATHEDRAL_V2_DB_PATH", "").strip()
    )
    if v2_database_path and v2_pipeline.pm_payout_bridge_enabled():
        print(
            "[prebake] refusing split V2 store while PM payout bridge is enabled; "
            "unset CATHEDRAL_V2_DATABASE_URL/CATHEDRAL_V2_DB_PATH or disable the bridge",
            file=sys.stderr,
            flush=True,
        )
        return 2
    if v2_database_path:
        store = Store(v2_database_path, prefer_env_database_url=False)
        store_source = "v2"
    else:
        store = Store(os.environ.get("CATHEDRAL_DB_PATH", "cathedral.db"))
        store_source = "main"
    hours = pm.epoch_bucket_hours()
    epoch = (
        int(args.epoch)
        if args.epoch is not None
        else (int(time.time()) + args.epoch_offset_secs) // (hours * 3600)
    )

    rows = store.query("SELECT DISTINCT hotkey FROM metagraph_hotkeys")
    hotkeys = sorted({str(r["hotkey"]) for r in rows})
    # V1/V2 parity: instances derive from the scoring identity (coldkey collapse
    # aware), not the raw hotkey. Bake what the handlers will actually serve.
    from scaffold.publisher import weights as weights_mod
    identities = sorted({
        weights_mod.scoring_identity_for_hotkey(store, hk, require_mapped=False) or hk
        for hk in hotkeys
    })
    mine = [ident for i, ident in enumerate(identities) if i % args.shards == args.shard]

    started = time.time()
    baked = skipped = failed = 0
    attempted = 0
    for hk in mine:  # hk here is the assignment identity
        for tier in pm.TIERS:
            depth = min(args.depth, pm.allotment_for(tier))
            for seq in range(depth):
                attempted += 1
                try:
                    cid = pm.instance_id(hk, epoch, tier, seq)
                    if v2_cnf_store.get(store, cid) is not None:
                        skipped += 1
                        continue
                    _cid, _sha, _nvars, _is_real, cnf_text = pm.item_meta(
                        hk, epoch, tier, seq)
                    v2_cnf_store.put(store, cid, cnf_text)
                    baked += 1
                except Exception as exc:  # keep going; a single bad item must not stop the bake
                    failed += 1
                    print(f"[prebake] item_failed hk={hk[:8]} tier={tier} seq={seq} err={exc!r}",
                          flush=True)
                finally:
                    if args.cache_clear_every > 0 and attempted % args.cache_clear_every == 0:
                        pm.item_meta.cache_clear()
                        try:
                            pm._gen_cached.cache_clear()
                        except AttributeError:
                            pass
    elapsed = time.time() - started
    print(f"[prebake] done shard={args.shard}/{args.shards} store={store_source} "
          f"backend={store.backend} epoch={epoch} "
          f"hotkeys={len(mine)} baked={baked} skipped={skipped} failed={failed} "
          f"elapsed={elapsed:.1f}s", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
