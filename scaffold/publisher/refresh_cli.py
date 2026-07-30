"""Run one artifact-tier refresh cycle from a cron / scheduler.

    python -m scaffold.publisher.refresh_cli                        # refresh scores only
    python -m scaffold.publisher.refresh_cli --publish \
        --netuid 2 --network test --signing-key-hex $HEX           # + compose+publish preview

Refresh keeps each enabled artifact-tier mechanism's scores fresh for ``compose``
(``mechanism_artifact_refresh.refresh_artifact_scores``). ``--publish`` additionally
composes the eligible vector and publishes the NEXT preview artifact via
``set_weights``, which is permanently DRY-RUN and **hard-refuses mainnet / finney /
SN39** — so this can never write real weights. The operator's scheduler owns the
cadence by how often it invokes this.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    from . import mechanism_artifact_refresh as arf
    from .mechanism_router import SqliteMechanismStore

    p = argparse.ArgumentParser(description="Refresh artifact-tier mechanism scores.")
    p.add_argument("--db", default=os.environ.get("CATHEDRAL_MECH_DB_PATH") or None,
                   help="mechanism sqlite path (default: $CATHEDRAL_MECH_DB_PATH / process default)")
    p.add_argument("--data-db", default=None,
                   help="publisher db holding the verified-work tables adapters read "
                        "(cybergym_scores, metagraph_hotkeys). A DIFFERENT database from "
                        f"--db. Default: ${arf.DATA_DB_PATH_ENV}")
    p.add_argument("--epoch", type=int, default=None, help="restrict to a single epoch")
    p.add_argument("--publish", action="store_true",
                   help="also compose + publish the preview vector (testnet only)")
    p.add_argument("--netuid", type=int, help="required with --publish")
    p.add_argument("--network", help="required with --publish (e.g. test/local)")
    p.add_argument("--signing-key-hex", help="required with --publish")
    args = p.parse_args(argv)

    store = SqliteMechanismStore(args.db)
    # Two databases: specs/scores in --db, the verified-work tables in --data-db.
    data_store = None
    if args.data_db:
        from .store import Store
        data_store = Store(args.data_db)

    if args.publish:
        missing = [f for f in ("netuid", "network", "signing_key_hex")
                   if getattr(args, f) in (None, "")]
        if missing:
            p.error("--publish requires " + ", ".join("--" + m.replace("_", "-") for m in missing))
        _, debug = arf.compose_and_publish(
            store, netuid=args.netuid, network=args.network,
            signing_key_hex=args.signing_key_hex, epoch=args.epoch,
            data_store=data_store)
        print(json.dumps({"refreshed_then_published": True,
                          "eligibility": debug.get("eligibility")}, default=str))
    else:
        refreshed = arf.refresh_artifact_scores(
            store, epoch=args.epoch, data_store=data_store)
        print(json.dumps({"refreshed": refreshed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
