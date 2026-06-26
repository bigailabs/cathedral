"""Refresh Cathedral's hotkey->coldkey map from the live SN39 metagraph.

This is an operator tool for the PM-primary lane. Private per-miner challenges
use coldkey identity so one operator cannot multiply assignment/scoring history
by splitting across many hotkeys.

Examples:
  python scripts/refresh_coldkey_map.py --database "$DATABASE_URL"
  python scripts/refresh_coldkey_map.py --check-hotkey 5abc... --no-refresh
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

from scaffold.chain import CHAIN_ENDPOINT_ENV, connection_target
from scaffold.publisher.store import Store


def _iso_now() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _scalar(value: Any) -> int | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            return int(value.item())
        except Exception:
            pass
    try:
        return int(value)
    except Exception:
        return None


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        return list(value.tolist())
    return list(value)


def _subtensor_ctor(bt: Any):
    return getattr(bt, "subtensor", None) or bt.Subtensor


def read_metagraph_rows(*, network: str, netuid: int) -> tuple[list[dict[str, Any]], int | None]:
    import bittensor as bt  # lazy import; operator/runtime dependency

    sub = _subtensor_ctor(bt)(network=connection_target(network))
    mg = sub.metagraph(netuid)
    hotkeys = _list(getattr(mg, "hotkeys", []))
    coldkeys = _list(getattr(mg, "coldkeys", []))
    uids = [_scalar(v) for v in _list(getattr(mg, "uids", []))]
    block = _scalar(getattr(mg, "block", None))
    if not hotkeys:
        raise RuntimeError("metagraph returned no hotkeys")
    if len(coldkeys) != len(hotkeys):
        raise RuntimeError(
            f"metagraph coldkeys length mismatch: hotkeys={len(hotkeys)} coldkeys={len(coldkeys)}"
        )
    if len(uids) != len(hotkeys):
        uids = list(range(len(hotkeys)))
    rows = [
        {"uid": uids[i], "hotkey": str(hotkeys[i]), "coldkey": str(coldkeys[i])}
        for i in range(len(hotkeys))
        if str(hotkeys[i]) and str(coldkeys[i])
    ]
    return rows, block


def refresh_store(
    store: Store,
    *,
    network: str,
    netuid: int,
    rows: list[dict[str, Any]],
    block: int | None,
    prune: bool,
) -> dict[str, Any]:
    updated_at = _iso_now()
    hotkeys = {r["hotkey"] for r in rows}
    stale_hotkeys = []
    if prune:
        stale_hotkeys = [
            str(old["hotkey"])
            for old in store.query("SELECT hotkey FROM coldkey_map")
            if str(old["hotkey"]) not in hotkeys
        ]

    def _write(conn):
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                (row["hotkey"], row["coldkey"], updated_at),
            )
            conn.execute(
                "INSERT OR REPLACE INTO metagraph_hotkeys("
                "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (network, netuid, row["hotkey"], row["uid"], row["coldkey"], block, updated_at),
            )
        for hotkey in stale_hotkeys:
            conn.execute("DELETE FROM coldkey_map WHERE hotkey=?", (hotkey,))
        return len(stale_hotkeys)

    pruned = store.write(_write)
    return {
        "network": network,
        "netuid": netuid,
        "block": block,
        "updated_at_iso": updated_at,
        "rows": len(rows),
        "pruned": pruned,
    }


def check_hotkeys(store: Store, hotkeys: list[str]) -> list[dict[str, Any]]:
    out = []
    for hotkey in hotkeys:
        rows = store.query(
            "SELECT hotkey, coldkey, updated_at_iso FROM coldkey_map WHERE hotkey=? LIMIT 1",
            (hotkey,),
        )
        if rows:
            row = rows[0]
            out.append({
                "hotkey": hotkey,
                "mapped": True,
                "coldkey": str(row["coldkey"]),
                "updated_at_iso": str(row["updated_at_iso"]),
            })
        else:
            out.append({
                "hotkey": hotkey,
                "mapped": False,
                "reason": "coldkey_mapping_required",
            })
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=":memory:",
                        help="SQLite path or Postgres DATABASE_URL; DATABASE_URL env also works")
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=int, default=39)
    parser.add_argument("--no-refresh", action="store_true",
                        help="only check existing DB rows; do not read chain")
    parser.add_argument("--no-prune", action="store_true",
                        help="do not remove hotkeys missing from the fresh metagraph")
    parser.add_argument("--check-hotkey", action="append", default=[],
                        help="hotkey to check after refresh; repeatable")
    args = parser.parse_args(argv)

    store = Store(args.database)
    if not args.no_refresh:
        rows, block = read_metagraph_rows(network=args.network, netuid=args.netuid)
        summary = refresh_store(
            store,
            network=args.network,
            netuid=args.netuid,
            rows=rows,
            block=block,
            prune=not args.no_prune,
        )
        print(summary)
    if args.check_hotkey:
        for result in check_hotkeys(store, args.check_hotkey):
            print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
