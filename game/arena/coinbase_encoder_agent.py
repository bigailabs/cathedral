"""Hermes-style encoder agent for the coinbase conservation oracle.

The real deployment runs this process inside operator-controlled TDX compute.
The process is intentionally simple: it receives a signed work packet, encodes
the canonical invariant, and emits the CNF, decode map, clause/source map, and
the expected TDX report_data binding for the work product.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from scaffold.lanes.coinbase_oracle import (
    attestation_report_data,
    build_coinbase_challenge,
)


SCHEMA_PACKET = "cathedral.hermes_encoder_packet.v1"
SCHEMA_RESULT = "cathedral.hermes_encoder_result.v1"


def run_encoder_agent(packet: dict[str, Any]) -> dict[str, Any]:
    if packet.get("schema_version") not in {"", None, SCHEMA_PACKET}:
        raise ValueError("encoder_packet_schema_mismatch")
    invariant_id = str(packet.get("invariant_id") or "")
    if invariant_id and invariant_id != "subtensor.run_coinbase.childkey_conservation.v1":
        raise ValueError("unsupported_invariant")
    width = int(packet.get("width", 64))
    challenge = build_coinbase_challenge(
        ckb_enabled=bool(packet.get("ckb_enabled")),
        width=width,
        agent_image_digest=str(packet.get("agent_image_digest") or ""),
        agent_id=str(packet.get("agent_id") or "hermes-coinbase-encoder-v1"),
        work_nonce=str(packet.get("work_nonce") or ""),
    )
    return {
        "schema_version": SCHEMA_RESULT,
        "agent_id": challenge.provenance["agent_id"],
        "agent_kind": "hermes_encoder",
        "invariant_id": challenge.invariant_id,
        "ckb_enabled": challenge.ckb_enabled,
        "width": challenge.width,
        "cnf_text": challenge.cnf_text,
        "cnf_sha256": challenge.cnf_sha256,
        "decode_map": challenge.decode_map,
        "clause_source_map": challenge.clause_source_map,
        "public_artifact": challenge.to_public_artifact(),
        "artifact_sha256": challenge.artifact_sha256,
        "tdx_report_data_hex": attestation_report_data(challenge),
        "trace": [
            {
                "tool": "coinbase_oracle.build_coinbase_challenge",
                "input": {
                    "ckb_enabled": challenge.ckb_enabled,
                    "width": challenge.width,
                    "invariant_id": challenge.invariant_id,
                },
                "output": {
                    "cnf_sha256": challenge.cnf_sha256,
                    "mapping_sha256": challenge.mapping_sha256,
                    "artifact_sha256": challenge.artifact_sha256,
                },
            }
        ],
    }


def main() -> int:
    packet = json.loads(sys.stdin.read() or "{}")
    sys.stdout.write(json.dumps(run_encoder_agent(packet), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
