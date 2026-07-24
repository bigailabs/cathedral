"""Create an owner-authenticated score contributor registration artifact."""

from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import bittensor as bt

from .bt_compat import current_block, listify, make_subtensor, make_wallet
from .core import ThinSubnetError
from .report_cli import write_report
from .score_classes import (
    AssignmentPolicy,
    ExternalClassPolicy,
    OwnerRegistrationPolicy,
    format_time,
    sign_owner_registration,
    verify_owner_registration,
)


def _key_assignments(values: list[str]) -> dict[str, str]:
    output: dict[str, str] = {}
    for value in values:
        key, separator, encoded = value.partition("=")
        if not separator or not key or not encoded or key in output:
            raise ThinSubnetError(
                "each --report-key must be a unique KEY_ID=BASE64 value"
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ThinSubnetError("report public key is not canonical base64") from exc
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != encoded:
            raise ThinSubnetError("report public key must be exactly 32 bytes")
        output[key] = encoded
    return output


def registration_chain_preflight(
    *,
    subtensor: Any,
    source_netuid: int,
    target_netuid: int,
    owner_coldkey: str,
    delegate_hotkey: str,
    block: int,
) -> dict[str, str]:
    source = subtensor.subnet(source_netuid, block=block)
    current_owner = str(getattr(source, "owner_coldkey", "") or "")
    if current_owner != owner_coldkey:
        raise ThinSubnetError(
            "wallet coldkey is not the current on-chain source subnet owner"
        )
    metagraph = subtensor.metagraph(target_netuid, lite=True)
    hotkeys = [str(value) for value in listify(metagraph.hotkeys)]
    coldkeys = [str(value) for value in listify(metagraph.coldkeys)]
    if len(hotkeys) != len(coldkeys) or len(set(hotkeys)) != len(hotkeys):
        raise ThinSubnetError("target metagraph identity arrays are invalid")
    registered = dict(zip(hotkeys, coldkeys))
    if registered.get(delegate_hotkey) != owner_coldkey:
        raise ThinSubnetError(
            "wallet hotkey is not registered under the owner coldkey on the target subnet"
        )
    return registered


def build_registration_body(
    *,
    network: str,
    source_netuid: int,
    target_netuid: int,
    owner_coldkey: str,
    delegate_hotkey: str,
    source_id: str,
    class_ids: list[str],
    report_locations: list[str],
    report_keys: dict[str, str],
    sequence: int,
    previous_registration_id: str | None,
    block: int,
    valid_blocks: int,
    issued_at: datetime,
    valid_seconds: int,
) -> dict[str, Any]:
    if not class_ids or class_ids != sorted(set(class_ids)):
        raise ThinSubnetError("--class-id values must be sorted and unique")
    if not report_locations or len(set(report_locations)) != len(report_locations):
        raise ThinSubnetError("--report-location values must be unique")
    if not report_keys:
        raise ThinSubnetError("at least one --report-key is required")
    if sequence < 0 or block < 0 or valid_blocks <= 0 or valid_seconds <= 0:
        raise ThinSubnetError("sequence and validity bounds are invalid")
    if issued_at.tzinfo is None or issued_at.utcoffset() != UTC.utcoffset(issued_at):
        raise ThinSubnetError("registration issue time must be UTC")
    return {
        "schema": "cathedral_owner_score_registration_v1",
        "network": network,
        "source_netuid": source_netuid,
        "target_netuid": target_netuid,
        "owner_coldkey": owner_coldkey,
        "delegate_hotkey": delegate_hotkey,
        "source_id": source_id,
        "class_ids": class_ids,
        "report_locations": report_locations,
        "report_keys": report_keys,
        "sequence": sequence,
        "previous_registration_id": previous_registration_id,
        "issued_at": format_time(issued_at),
        "expires_at": format_time(issued_at + timedelta(seconds=valid_seconds)),
        "valid_from_block": block,
        "valid_until_block": block + valid_blocks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Register a source subnet owner as a score-class contributor; "
            "this creates a signed artifact and never submits weights"
        )
    )
    parser.add_argument("--network", default=os.environ.get("BT_NETWORK", "test"))
    parser.add_argument("--source-netuid", type=int, required=True)
    parser.add_argument("--target-netuid", type=int, required=True)
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", required=True)
    parser.add_argument("--wallet-path", default=os.environ.get("BT_WALLET_PATH", ""))
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--class-id", action="append", required=True)
    parser.add_argument("--report-location", action="append", required=True)
    parser.add_argument(
        "--report-key",
        action="append",
        required=True,
        help="delegated Ed25519 report key as KEY_ID=BASE64",
    )
    parser.add_argument("--sequence", type=int, default=0)
    parser.add_argument("--previous-registration-id", default="")
    parser.add_argument("--valid-seconds", type=int, default=86_400)
    parser.add_argument("--valid-blocks", type=int, default=7_200)
    parser.add_argument("--output", required=True)
    parser.add_argument("--replace-latest", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        wallet = make_wallet(
            bt,
            name=args.wallet_name,
            hotkey=args.wallet_hotkey,
            path=args.wallet_path or None,
        )
        owner_coldkey = str(wallet.coldkeypub.ss58_address)
        delegate_hotkey = str(wallet.hotkey.ss58_address)
        subtensor = make_subtensor(bt, network=args.network)
        block = current_block(subtensor)
        registered = registration_chain_preflight(
            subtensor=subtensor,
            source_netuid=args.source_netuid,
            target_netuid=args.target_netuid,
            owner_coldkey=owner_coldkey,
            delegate_hotkey=delegate_hotkey,
            block=block,
        )
        issued_at = datetime.now(UTC)
        report_keys = _key_assignments(args.report_key)
        body = build_registration_body(
            network=args.network,
            source_netuid=args.source_netuid,
            target_netuid=args.target_netuid,
            owner_coldkey=owner_coldkey,
            delegate_hotkey=delegate_hotkey,
            source_id=args.source_id,
            class_ids=sorted(args.class_id),
            report_locations=args.report_location,
            report_keys=report_keys,
            sequence=args.sequence,
            previous_registration_id=args.previous_registration_id or None,
            block=block,
            valid_blocks=args.valid_blocks,
            issued_at=issued_at,
            valid_seconds=args.valid_seconds,
        )
        raw = sign_owner_registration(body, wallet.coldkey)
        self_policy = ExternalClassPolicy(
            class_id=body["class_ids"][0],
            allocation=Decimal(1),
            source_id=body["source_id"],
            locations=tuple(body["report_locations"]),
            trusted_keys={},
            max_age_seconds=1,
            max_future_seconds=1,
            max_block_span=max(1, args.valid_blocks),
            require_evidence=False,
            assignment=AssignmentPolicy("asserted_score", None, "linear", None),
            owner_registration=OwnerRegistrationPolicy(
                source_netuid=args.source_netuid,
                locations=("self-check",),
                max_age_seconds=max(1, args.valid_seconds),
                max_future_seconds=1,
                max_block_span=max(1, args.valid_blocks),
                require_target_registration=True,
            ),
        )
        registration, _ = verify_owner_registration(
            raw,
            self_policy,
            network=args.network,
            netuid=args.target_netuid,
            current_block=block,
            current_owner_coldkey=owner_coldkey,
            registered_hotkeys=registered,
            now=issued_at,
        )
        output = write_report(
            args.output, raw, replace_latest=bool(args.replace_latest)
        )
        print(
            json.dumps(
                {
                    "delegate_hotkey": delegate_hotkey,
                    "output": str(output),
                    "owner_coldkey": owner_coldkey,
                    "registration_id": registration.registration_id,
                    "status": "registered_artifact_created",
                    "weights_submitted": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ThinSubnetError) as exc:
        print(json.dumps({"error": str(exc), "status": "rejected"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
