"""Offline acceptance command for Polaris confidential-GPU evidence exports."""

from __future__ import annotations

import argparse
import json

from .cc_gpu_loader import load_cc_gpu_loader_config
from .core import ThinSubnetError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify one bounded Polaris CC GPU evidence export offline"
    )
    parser.add_argument("--loader-config", required=True)
    parser.add_argument("--export", action="append", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        loader = load_cc_gpu_loader_config(args.loader_config)
        verified = loader.load_paths(args.export)
        print(
            json.dumps(
                {
                    "accepted_receipt_ids": [item.receipt_id for item in verified],
                    "count": len(verified),
                    "launch_status": "NOT PROVEN",
                    "schema": "cathedral_cc_gpu_acceptance_result_v1",
                    "status": "PASS",
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ThinSubnetError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "launch_status": "NOT PROVEN",
                    "schema": "cathedral_cc_gpu_acceptance_result_v1",
                    "status": "FAIL",
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
