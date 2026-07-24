"""Capture a ``cathedral_candidate_snapshot_v1`` from finalized chain state.

This is the ONE supported producer command for candidate snapshots: it reads
the SN39 metagraph at a finalized block through the operator's own subtensor
connection, records exactly {network, netuid, block, block_hash, hotkeys},
and writes the document atomically. The confidential exporter then binds this
exact snapshot (digest, block, hash, full sorted hotkey set) into the signed
score report, and full validators re-verify the same facts against their own
historical chain queries.

Only hotkeys are recorded — never machine identity, endpoints, or stake.

Usage::

    cathedral-candidate-snapshot --network finney --netuid 39 \
        --output candidate-snapshot.json [--block N]

Without ``--block`` the metagraph's own current block is captured; pass an
explicitly finalized block for production epochs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "cathedral_candidate_snapshot_v1"
_BLOCK_HASH_RE = re.compile(r"^(0x)?[0-9a-f]{64}$")
MAX_HOTKEYS = 4096


class SnapshotError(ValueError):
    """The chain state cannot produce a valid candidate snapshot."""


def _default_subtensor_factory(network: str):
    from .chain import connection_target
    from .validator_thin import _bt_subtensor, _isolated_argv

    with _isolated_argv():
        import bittensor as bt

        return _bt_subtensor(bt)(network=connection_target(network))


def capture_candidate_snapshot(
    *,
    network: str,
    netuid: int,
    block: int | None = None,
    subtensor_factory=None,
) -> dict[str, Any]:
    """Read the metagraph at ``block`` (or its own current block) and return
    a validated cathedral_candidate_snapshot_v1 document."""
    if not isinstance(network, str) or not network:
        raise SnapshotError("network is invalid")
    if isinstance(netuid, bool) or not isinstance(netuid, int) or netuid < 0:
        raise SnapshotError("netuid is invalid")
    if block is not None and (
        isinstance(block, bool) or not isinstance(block, int) or block < 0
    ):
        raise SnapshotError("block must be a nonnegative integer")

    factory = subtensor_factory or _default_subtensor_factory
    subtensor = factory(network)
    metagraph = (
        subtensor.metagraph(netuid)
        if block is None
        else subtensor.metagraph(netuid, block=int(block))
    )

    metagraph_block = getattr(metagraph, "block", None)
    try:
        metagraph_block = int(metagraph_block)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(
            "the metagraph did not expose a usable block number; the "
            "requested-block binding cannot be proven"
        ) from exc
    if block is not None and metagraph_block != int(block):
        raise SnapshotError(
            f"the chain returned metagraph block {metagraph_block} for the "
            f"explicitly requested block {block}; refusing the unproven binding"
        )
    captured_block = metagraph_block
    if captured_block < 0:
        raise SnapshotError("the captured block number is invalid")

    block_hash = subtensor.get_block_hash(captured_block)
    if (
        not isinstance(block_hash, str)
        or _BLOCK_HASH_RE.fullmatch(block_hash.strip().lower()) is None
    ):
        raise SnapshotError(
            f"the chain returned no usable hash for block {captured_block}"
        )

    raw_hotkeys = list(getattr(metagraph, "hotkeys", None) or [])
    hotkeys: list[str] = []
    for hotkey in raw_hotkeys:
        if not isinstance(hotkey, str) or not 1 <= len(hotkey.encode("utf-8")) <= 512:
            raise SnapshotError("the metagraph returned a malformed hotkey")
        hotkeys.append(hotkey)
    if len(set(hotkeys)) != len(hotkeys):
        raise SnapshotError("the metagraph returned duplicate hotkeys")
    if len(hotkeys) > MAX_HOTKEYS:
        raise SnapshotError("the metagraph returned an unreasonable hotkey count")

    return {
        "schema": SNAPSHOT_SCHEMA,
        "network": network,
        "netuid": int(netuid),
        "block": captured_block,
        "block_hash": block_hash.strip().lower(),
        "hotkeys": sorted(hotkeys),
    }


def write_snapshot_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write the snapshot atomically: exclusive temp file, fsync, rename,
    parent fsync — a crash can never leave a torn or symlink-followed file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        if os.path.lexists(tmp) and not tmp.is_symlink():
            os.unlink(tmp)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(tmp, flags, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cathedral-candidate-snapshot",
        description=(
            "capture a cathedral_candidate_snapshot_v1 from finalized SN39 chain state"
        ),
    )
    parser.add_argument("--network", required=True)
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument(
        "--block",
        type=int,
        default=None,
        help="finalized block to anchor (default: the metagraph's own block)",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    try:
        document = capture_candidate_snapshot(
            network=args.network, netuid=args.netuid, block=args.block
        )
        write_snapshot_atomic(Path(args.output), document)
    except (SnapshotError, OSError) as exc:
        print(f"candidate snapshot failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "block": document["block"],
                "block_hash": document["block_hash"],
                "hotkeys": len(document["hotkeys"]),
                "output": str(Path(args.output)),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
