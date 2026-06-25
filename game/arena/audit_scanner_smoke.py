"""Smoke-test the production-style audit scanner bridge.

This exercises the `/v1/audit-scanner` surface with a real sr25519 signature,
but it does not touch chain state or payment weights. By default it builds the
publisher app in-process with the scanner gate enabled. Pass `--url` to probe a
running publisher instead.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bittensor_wallet import Keypair

from game.arena import scanner
from scaffold.publisher.app import _AUDIT_SCANNER_CARD, _empty_bundle_hash
from scaffold.publisher.auth import canonical_claim_bytes


def _now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _submission_from_payload(payload: dict[str, Any], hotkey: str) -> scanner.ScannerSubmission:
    return scanner.ScannerSubmission(
        task_id=str(payload.get("task_id", "")),
        miner_hotkey=hotkey,
        nonce=str(payload.get("nonce", "")),
        proof_family=str(payload.get("proof_family", "")),
        witness=payload.get("witness"),
        trace=payload.get("trace") if isinstance(payload.get("trace"), list) else [],
        claim=payload.get("claim") or {},
        report=str(payload.get("report", "")),
    )


def _signed_headers(payload: dict[str, Any], keypair: Keypair) -> dict[str, str]:
    sub = _submission_from_payload(payload, keypair.ss58_address)
    artifact_sha = scanner._sha(sub.as_artifact())
    submitted_at = _now_iso()
    msg = canonical_claim_bytes(
        bundle_hash=_empty_bundle_hash(),
        card_id=_AUDIT_SCANNER_CARD,
        miner_hotkey=keypair.ss58_address,
        submitted_at=submitted_at,
        challenge_id=sub.task_id,
        dimacs_solution_sha256=artifact_sha,
    )
    return {
        "X-Cathedral-Hotkey": keypair.ss58_address,
        "X-Cathedral-Submitted-At": submitted_at,
        "X-Cathedral-Signature": base64.b64encode(keypair.sign(msg)).decode("ascii"),
    }


class UrlTransport:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get(self, path: str) -> dict[str, Any]:
        with urlopen(self.base_url + path, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path: str, payload: dict[str, Any],
             headers: dict[str, str] | None = None) -> dict[str, Any]:
        req = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"POST {path} failed: HTTP {exc.code} {body}") from exc


class TestClientTransport:
    def __init__(self, client: Any) -> None:
        self.client = client

    def get(self, path: str) -> dict[str, Any]:
        resp = self.client.get(path)
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {path} failed: HTTP {resp.status_code} {resp.text}")
        return resp.json()

    def post(self, path: str, payload: dict[str, Any],
             headers: dict[str, str] | None = None) -> dict[str, Any]:
        resp = self.client.post(path, json=payload, headers=headers or {})
        if resp.status_code >= 400:
            raise RuntimeError(f"POST {path} failed: HTTP {resp.status_code} {resp.text}")
        return resp.json()


def run_smoke(transport: Any, keypair: Keypair) -> dict[str, Any]:
    status = transport.get("/v1/audit-scanner/status")
    if not status.get("enabled"):
        raise RuntimeError("audit scanner bridge is disabled")
    if status.get("payment_weights") is not False:
        raise RuntimeError("audit scanner smoke expected payment_weights=false")
    contract = transport.get("/v1/audit-scanner/schema")
    if (
        contract.get("schema") != "cathedral.audit_scanner.contract.v1"
        or contract.get("card_id") != _AUDIT_SCANNER_CARD
        or contract.get("payment_weights") is not False
        or contract.get("scoring", {}).get("reports_score") is not False
        or "witness" not in contract.get("submission_schema", {}).get("required_fields", [])
    ):
        raise RuntimeError(f"unexpected audit scanner contract: {contract}")

    catalog = transport.get("/v1/audit-scanner/catalog?limit=1")
    if catalog.get("count") != 1:
        raise RuntimeError(f"expected one catalog task, got {catalog.get('count')}")
    families = transport.get("/v1/audit-scanner/families")
    if (
        families.get("reward_gate") != "deterministic_replay"
        or families.get("category_scoring") != "claim_category_is_metadata_only"
        or not families.get("claim_categories")
    ):
        raise RuntimeError(f"unexpected families taxonomy: {families}")
    redacted_example = transport.get("/v1/audit-scanner/example?index=0")
    if (
        redacted_example.get("solution_exported") is not False
        or redacted_example.get("submission", {}).get("witness") is not None
        or redacted_example.get("redaction", {}).get("witness_exported") is not False
    ):
        raise RuntimeError(f"example endpoint leaked a solution: {redacted_example}")

    # The publisher example endpoint is redacted by default. The deterministic
    # scanner catalog lets this smoke build its own local proof without asking
    # a production server to reveal a solved witness.
    task = scanner.issue_task(0)
    catalog_task_id = catalog.get("tasks", [{}])[0].get("task_id")
    if catalog_task_id != task.task_id:
        raise RuntimeError(
            f"local scanner task does not match publisher catalog: "
            f"{task.task_id} != {catalog_task_id}"
        )
    payload = scanner.example_accepted_submission(
        task,
        miner_hotkey=keypair.ss58_address,
    ).as_artifact()

    headers = _signed_headers(payload, keypair)
    replay = transport.post("/v1/audit-scanner/replay", payload, headers)
    if replay.get("accepted") is not True or replay.get("ledger_written") is not False:
        raise RuntimeError(f"replay failed: {replay}")

    submit = transport.post("/v1/audit-scanner/submit", payload, headers)
    if submit.get("accepted") is not True:
        raise RuntimeError(f"submit failed: {submit}")
    if submit.get("payment_weights") is not False:
        raise RuntimeError("audit scanner submit unexpectedly changed payment weights")

    benchmark = transport.get("/v1/audit-scanner/benchmark")
    state = transport.get(f"/v1/audit-scanner/state?miner_hotkey={keypair.ss58_address}")
    submissions = transport.get("/v1/audit-scanner/submissions?limit=1")
    if state.get("accepted") != 1:
        raise RuntimeError(f"expected one accepted state row, got {state}")
    if (
        submissions.get("count") != 1
        or submissions.get("contains_witnesses") is not False
        or submissions.get("contains_trace_bodies") is not False
    ):
        raise RuntimeError(f"unexpected submissions view: {submissions}")
    if "artifact" in submissions.get("entries", [{}])[0]:
        raise RuntimeError(f"submissions view leaked raw artifact: {submissions}")

    traces = transport.get("/v1/audit-scanner/traces?limit=1")
    if (
        traces.get("count") != 1
        or traces.get("accepted") != 1
        or traces.get("contains_witnesses") is not False
        or traces.get("contains_trace_bodies") is not False
    ):
        raise RuntimeError(f"unexpected traces view: {traces}")
    if "artifact" in traces.get("traces", [{}])[0]:
        raise RuntimeError(f"traces view leaked raw artifact: {traces}")

    return {
        "status": status,
        "contract": contract,
        "task_id": payload["task_id"],
        "replay": replay,
        "submit": submit,
        "benchmark": benchmark,
        "families": families,
        "example": redacted_example,
        "state": state,
        "submissions": submissions,
        "traces": traces,
    }


def run_in_process(keypair: Keypair, ledger_path: str | None) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from scaffold.publisher import build_app
    from scaffold.publisher.keys import generate_test_key

    old_enabled = os.environ.get("CATHEDRAL_AUDIT_SCANNER_ENABLED")
    old_ledger = os.environ.get("CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH")
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if ledger_path is None:
            tmp = tempfile.TemporaryDirectory()
            ledger_path = str(Path(tmp.name) / "audit_scanner_smoke.jsonl")
        os.environ["CATHEDRAL_AUDIT_SCANNER_ENABLED"] = "1"
        os.environ["CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH"] = ledger_path
        app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())
        with TestClient(app) as client:
            return run_smoke(TestClientTransport(client), keypair)
    finally:
        if old_enabled is None:
            os.environ.pop("CATHEDRAL_AUDIT_SCANNER_ENABLED", None)
        else:
            os.environ["CATHEDRAL_AUDIT_SCANNER_ENABLED"] = old_enabled
        if old_ledger is None:
            os.environ.pop("CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH", None)
        else:
            os.environ["CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH"] = old_ledger
        if tmp is not None:
            tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test Cathedral's signed audit scanner bridge.",
    )
    parser.add_argument("--url", help="Running publisher base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--key-uri", default="//AuditScannerSmoke",
                        help="Development key URI used for the smoke signature.")
    parser.add_argument("--ledger-path", help="Local in-process ledger path.")
    parser.add_argument("--json", action="store_true", help="Print full JSON result.")
    args = parser.parse_args(argv)

    keypair = Keypair.create_from_uri(args.key_uri)
    if args.url:
        result = run_smoke(UrlTransport(args.url), keypair)
    else:
        result = run_in_process(keypair, args.ledger_path)

    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2, default=str))
    else:
        print("AUDIT SCANNER SMOKE: PASS")
        print(f"  hotkey: {keypair.ss58_address}")
        print(f"  task: {result['task_id']}")
        print("  schema: payment_weights=false reports_score=false")
        print(
            "  families: "
            f"{result['families']['count']} "
            "reward_gate=deterministic_replay"
        )
        print(
            "  example: "
            f"solution_exported={str(result['example']['solution_exported']).lower()}"
        )
        print(f"  replay: accepted={result['replay']['accepted']} ledger_written={result['replay']['ledger_written']}")
        print(f"  submit: accepted={result['submit']['accepted']} score={result['submit']['score']}")
        print(f"  benchmark: metric={result['benchmark']['metric']} accepted={result['state']['accepted']}")
        print(f"  submissions: total={result['submissions']['total']} contains_witnesses=false")
        print(
            "  traces: "
            f"total={result['traces']['total']} "
            f"accepted={result['traces']['accepted']} "
            "contains_trace_bodies=false"
        )
        print("  payment_weights: false")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
