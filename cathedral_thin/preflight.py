"""Read-only registration and deployment preflight for thin subnet operators."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import bittensor as bt

from .bt_compat import listify, make_subtensor, make_wallet
from .core import ThinSubnetError


def registration_report(
    metagraph: Any,
    *,
    hotkey: str,
    role: str,
    require_serving: bool = False,
) -> dict[str, Any]:
    if role not in {"miner", "validator"}:
        raise ThinSubnetError("role must be miner or validator")
    hotkeys = [str(value) for value in listify(metagraph.hotkeys)]
    uids = [int(value) for value in listify(metagraph.uids)]
    permits = [bool(value) for value in listify(metagraph.validator_permit)]
    axons = list(metagraph.axons)
    if not (len(hotkeys) == len(uids) == len(permits) == len(axons)):
        raise ThinSubnetError("metagraph arrays have inconsistent lengths")
    indices = [index for index, value in enumerate(hotkeys) if value == hotkey]
    if len(indices) > 1:
        raise ThinSubnetError("metagraph contains duplicate hotkey")
    if not indices:
        return {
            "registered": False,
            "uid": None,
            "validator_permit": False,
            "axon_serving": False,
            "ready": False,
        }
    index = indices[0]
    axon = axons[index]
    port = int(getattr(axon, "port", 0) or 0)
    serving = bool(getattr(axon, "is_serving", port > 0)) and port > 0
    permitted = permits[index]
    ready = permitted if role == "validator" else (serving or not require_serving)
    return {
        "registered": True,
        "uid": uids[index],
        "validator_permit": permitted,
        "axon_serving": serving,
        "ready": bool(ready),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Cathedral thin registration/deployment preflight"
    )
    parser.add_argument("--network", default=os.environ.get("BT_NETWORK", "finney"))
    parser.add_argument(
        "--netuid", type=int, default=int(os.environ.get("BT_NETUID", "39"))
    )
    parser.add_argument(
        "--wallet-name", default=os.environ.get("BT_WALLET_NAME", "validator")
    )
    parser.add_argument(
        "--wallet-hotkey", default=os.environ.get("BT_WALLET_HOTKEY", "default")
    )
    parser.add_argument("--wallet-path", default=os.environ.get("BT_WALLET_PATH", ""))
    parser.add_argument("--role", choices=("miner", "validator"), required=True)
    parser.add_argument(
        "--require-serving",
        action="store_true",
        help="for miners, require a currently serving Axon",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.netuid < 0:
        raise SystemExit("netuid must be non-negative")
    wallet = make_wallet(
        bt,
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=args.wallet_path or None,
    )
    hotkey = str(wallet.hotkey.ss58_address)
    subtensor = make_subtensor(bt, network=args.network)
    metagraph = subtensor.metagraph(args.netuid, lite=True)
    report = registration_report(
        metagraph,
        hotkey=hotkey,
        role=args.role,
        require_serving=args.require_serving,
    )
    report.update(
        {
            "network": args.network,
            "netuid": args.netuid,
            "hotkey": hotkey,
            "role": args.role,
            "bittensor_version": str(getattr(bt, "__version__", "unknown")),
            "read_only": True,
        }
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
