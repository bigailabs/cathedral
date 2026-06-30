#!/usr/bin/env python3
"""Cathedral V2 miner E2E smoke.

Flow:
  fetch V2 per-miner challenge -> fetch CNF -> solve locally -> upload solution
  blob to beta store -> submit signed manifest -> poll verified receipt -> check
  shadow weights and audit bundle.

This script writes only to the isolated V2 beta lane. It does not affect V1
production weights, rewards, or payouts.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

try:
    import requests
except Exception as exc:  # pragma: no cover - user environment guard
    raise SystemExit("missing dependency: pip install requests") from exc

try:
    from bittensor_wallet import Keypair
except Exception as exc:  # pragma: no cover - user environment guard
    raise SystemExit("missing dependency: pip install bittensor-wallet") from exc

CARD_ID = "synthetic_boolean_v1"
MANIFEST_SCHEMA = "cathedral.solution_manifest.v1"
# BLAKE3 hash of empty bytes, used by Cathedral's existing hotkey claim shape.
EMPTY_BUNDLE_HASH = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
DEFAULT_BASE = "https://v2-beta.cathedral.computer"
FALLBACK_SOLVERS = ("cadical153", "glucose3", "minisat22")


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def make_keypair() -> Keypair:
    uri = os.environ.get("CATHEDRAL_MINER_URI", "").strip()
    seed = os.environ.get("CATHEDRAL_MINER_SEED_HEX", "").strip()
    if uri:
        return Keypair.create_from_uri(uri)
    if seed:
        if seed.startswith("0x"):
            seed = seed[2:]
        return Keypair.create_from_seed("0x" + seed)
    # Ephemeral smoke-test key. The seed is intentionally not printed.
    return Keypair.create_from_seed("0x" + os.urandom(32).hex())


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def canonical_claim_bytes(*, miner_hotkey: str, submitted_at: str,
                          challenge_id: str | None = None,
                          dimacs_solution_sha256: str | None = None) -> bytes:
    payload: dict[str, Any] = {
        "bundle_hash": EMPTY_BUNDLE_HASH,
        "card_id": CARD_ID,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
    }
    if challenge_id is not None or dimacs_solution_sha256 is not None:
        payload["challenge_id"] = challenge_id or ""
        payload["dimacs_solution_sha256"] = dimacs_solution_sha256 or ""
    return canonical_json_bytes(payload)


def canonical_blob_upload_bytes(*, miner_hotkey: str, submitted_at: str,
                                blob_sha256: str, blob_bytes: int,
                                kind: str = "solution") -> bytes:
    return canonical_json_bytes({
        "schema": "cathedral.blob_upload.v1",
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
        "kind": kind,
        "blob_sha256": blob_sha256.lower(),
        "blob_bytes": int(blob_bytes),
    })


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return canonical_json_bytes({k: manifest[k] for k in sorted(manifest)})


def normalize_manifest(body: dict[str, Any], *, miner_hotkey: str, submitted_at: str) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "card_id": CARD_ID,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
        "challenge_id": str(body["challenge_id"]).strip(),
        "assignment_encoding": str(body["assignment_encoding"]).strip(),
        "solution_cid": str(body["solution_cid"]).strip(),
        "solution_sha256": str(body["solution_sha256"]).strip().lower(),
        "solution_bytes": int(body.get("solution_bytes") or 0),
        "cnf_sha256": str(body.get("cnf_sha256") or "").strip().lower(),
    }


def sign_b64(kp: Keypair, msg: bytes) -> str:
    return base64.b64encode(kp.sign(msg)).decode("ascii")


def read_headers(kp: Keypair) -> dict[str, str]:
    ts = now_iso()
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sign_b64(kp, canonical_claim_bytes(miner_hotkey=kp.ss58_address, submitted_at=ts, challenge_id="", dimacs_solution_sha256="")),
        "X-Cathedral-Submitted-At": ts,
    }


def upload_headers(kp: Keypair, blob: bytes) -> dict[str, str]:
    ts = now_iso()
    sha = hashlib.sha256(blob).hexdigest()
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sign_b64(kp, canonical_blob_upload_bytes(miner_hotkey=kp.ss58_address, submitted_at=ts, blob_sha256=sha, blob_bytes=len(blob))),
        "X-Cathedral-Submitted-At": ts,
        "X-Cathedral-Blob-Sha256": sha,
        "Content-Type": "application/octet-stream",
    }


def manifest_headers(kp: Keypair, body: dict[str, Any]) -> dict[str, str]:
    ts = now_iso()
    manifest = normalize_manifest(body, miner_hotkey=kp.ss58_address, submitted_at=ts)
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sign_b64(kp, canonical_manifest_bytes(manifest)),
        "X-Cathedral-Submitted-At": ts,
    }


def parse_dimacs(cnf_text: str) -> tuple[int, list[list[int]]]:
    clauses: list[list[int]] = []
    nvars = 0
    for raw in cnf_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("c"):
            continue
        if line.startswith("p "):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "cnf":
                nvars = int(parts[2])
            continue
        lits = [int(tok) for tok in line.split() if tok != "0"]
        if lits:
            clauses.append(lits)
            nvars = max(nvars, *(abs(x) for x in lits))
    return nvars, clauses


def solve_assignment(cnf_text: str, preferred_solver: str) -> tuple[list[int], str]:
    try:
        from pysat.formula import CNF
        from pysat.solvers import Solver
    except Exception as exc:  # pragma: no cover - user environment guard
        raise SystemExit("missing dependency: pip install python-sat") from exc

    nvars, clauses = parse_dimacs(cnf_text)
    cnf = CNF()
    cnf.nv = nvars
    for clause in clauses:
        cnf.append(clause)

    solvers = [preferred_solver] if preferred_solver else []
    for name in FALLBACK_SOLVERS:
        if name not in solvers:
            solvers.append(name)

    last_err: Exception | None = None
    for solver_name in solvers:
        try:
            with Solver(name=solver_name, bootstrap_with=cnf.clauses) as solver:
                if not solver.solve():
                    raise RuntimeError("solver returned UNSAT for assigned challenge")
                model = solver.get_model() or []
            vals: dict[int, bool] = {}
            for lit in model:
                v = abs(int(lit))
                if 1 <= v <= nvars and v not in vals:
                    vals[v] = lit > 0
            return [i if vals.get(i, False) else -i for i in range(1, nvars + 1)], solver_name
        except Exception as exc:  # try next installed solver
            last_err = exc
            continue
    raise RuntimeError(f"no SAT solver succeeded; last_error={last_err}")


def encode_bitset_assignment(assignment: list[int]) -> bytes:
    nvars = len(assignment)
    out = bytearray((nvars + 7) // 8)
    seen: set[int] = set()
    for lit in assignment:
        lit_i = int(lit)
        v = abs(lit_i)
        if v < 1 or v > nvars or v in seen:
            raise ValueError("invalid_assignment")
        seen.add(v)
        if lit_i > 0:
            out[(v - 1) // 8] |= 1 << ((v - 1) % 8)
    if len(seen) != nvars:
        raise ValueError("incomplete_assignment")
    return bytes(out)


def get_json(session: requests.Session, base: str, path: str, *, headers: dict[str, str] | None = None,
             timeout: float = 30.0) -> tuple[requests.Response, Any]:
    r = session.get(base.rstrip("/") + path, headers=headers or {}, timeout=timeout)
    try:
        return r, r.json()
    except Exception:
        return r, r.text


def main() -> int:
    ap = argparse.ArgumentParser(description="Run an isolated Cathedral V2 miner E2E smoke")
    ap.add_argument("--base", default=os.environ.get("CATHEDRAL_V2_BASE_URL", DEFAULT_BASE).rstrip("/"))
    ap.add_argument("--limit", type=int, default=2)
    ap.add_argument("--solver", default=os.environ.get("CATHEDRAL_MINER_SOLVER", "cadical153"))
    ap.add_argument("--poll-secs", type=float, default=90.0)
    ap.add_argument("--dry-run", action="store_true", help="fetch/solve but do not upload or submit")
    args = ap.parse_args()

    session = requests.Session()
    kp = make_keypair()
    base = args.base.rstrip("/")

    print(f"base={base}")
    print(f"hotkey={kp.ss58_address}")
    print("key_source=" + ("env" if os.environ.get("CATHEDRAL_MINER_URI") or os.environ.get("CATHEDRAL_MINER_SEED_HEX") else "ephemeral"))

    r, body = get_json(session, base, "/health/live", timeout=20)
    print(f"health_live={r.status_code}")
    if r.status_code != 200:
        print(body)
        return 1

    r, payload = get_json(
        session,
        base,
        f"/v2/synthetic-boolean/per-miner/challenges?limit={int(args.limit)}",
        headers=read_headers(kp),
        timeout=30,
    )
    print(f"challenges_status={r.status_code}")
    if r.status_code != 200 or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else str(payload)[:1000])
        return 1
    items = payload.get("items") or []
    print(f"epoch={payload.get('epoch')} count={len(items)}")
    if not items:
        print("no assigned challenges")
        return 1

    items = sorted(items, key=lambda x: (int(x.get("num_vars") or 10**9), int(x.get("tier") or 99), int(x.get("seq") or 10**9)))
    item = items[0]
    challenge_id = str(item["challenge_id"])
    tier = int(item.get("tier") or 1)
    seq = int(item.get("seq") or 0)
    epoch = int(payload.get("epoch") or 0)
    print(f"selected={challenge_id} tier={tier} seq={seq} vars={item.get('num_vars')} clauses={item.get('num_clauses')}")

    query = urlencode({"challenge_id": challenge_id, "tier": tier, "seq": seq})
    r = session.get(base + "/v2/synthetic-boolean/per-miner/cnf?" + query, headers=read_headers(kp), timeout=30)
    print(f"cnf_status={r.status_code} bytes={len(r.content)}")
    if r.status_code != 200:
        print(r.text[:1000])
        return 1
    cnf_text = r.text

    t0 = time.time()
    assignment, solver_used = solve_assignment(cnf_text, args.solver)
    solve_secs = time.time() - t0
    blob = encode_bitset_assignment(assignment)
    blob_sha = hashlib.sha256(blob).hexdigest()
    print(f"solved solver={solver_used} elapsed={solve_secs:.3f}s vars={len(assignment)} blob_bytes={len(blob)} blob_sha={blob_sha[:12]}...")

    if args.dry_run:
        print("dry_run=1")
        return 0

    r = session.post(base + "/v2/blobs/solutions", data=blob, headers=upload_headers(kp, blob), timeout=30)
    print(f"blob_upload_status={r.status_code}")
    if r.status_code != 200:
        print(r.text[:1000])
        return 1
    uploaded = r.json()
    print(f"solution_cid={uploaded.get('cid')}")

    manifest_body = {
        "schema": MANIFEST_SCHEMA,
        "card_id": CARD_ID,
        "challenge_id": challenge_id,
        "assignment_encoding": "bitset/v1",
        "solution_cid": uploaded["cid"],
        "solution_sha256": uploaded["sha256"],
        "solution_bytes": int(uploaded["bytes"]),
    }
    r = session.post(base + "/v2/agents/submit-manifest", json=manifest_body, headers=manifest_headers(kp, manifest_body), timeout=30)
    print(f"manifest_submit_status={r.status_code}")
    if r.status_code not in (200, 202):
        print(r.text[:1000])
        return 1
    receipt = r.json()
    receipt_id = receipt["receipt_id"]
    print(f"receipt_id={receipt_id}")

    deadline = time.time() + max(1.0, float(args.poll_secs))
    final: dict[str, Any] | None = None
    while time.time() < deadline:
        r, final_body = get_json(session, base, f"/v2/agents/submit-manifest/receipts/{receipt_id}", timeout=20)
        if r.status_code != 200 or not isinstance(final_body, dict):
            print(f"receipt_status={r.status_code} body={str(final_body)[:300]}")
            time.sleep(2)
            continue
        final = final_body
        print(f"receipt_state={final.get('status')} terminal={final.get('terminal')}")
        if final.get("terminal"):
            break
        time.sleep(2)

    if not final or final.get("status") != "verified":
        print("E2E_FAILED receipt_not_verified")
        print(json.dumps(final or {}, indent=2, sort_keys=True))
        return 1

    r, weights = get_json(session, base, "/v2/validator/weights/next", timeout=30)
    print(f"weights_status={r.status_code}")
    if r.status_code != 200 or not isinstance(weights, dict):
        print(weights)
        return 1
    row = next((w for w in weights.get("weights") or [] if w.get("miner_hotkey") == kp.ss58_address), None)
    if not row:
        print("E2E_FAILED hotkey_missing_from_shadow_weights")
        return 1
    print(f"shadow_weight={row.get('weight')} raw_score={row.get('raw_score')}")

    r, audit = get_json(session, base, f"/v2/audit/epochs/{epoch}", timeout=30)
    print(f"audit_status={r.status_code}")
    if r.status_code != 200 or not isinstance(audit, dict):
        print(audit)
        return 1
    if not any(x.get("id") == receipt_id for x in audit.get("receipts") or []):
        print("E2E_FAILED receipt_missing_from_audit")
        return 1

    print("E2E_OK " + json.dumps({
        "receipt_id": receipt_id,
        "status": final.get("status"),
        "hotkey": kp.ss58_address,
        "weight": row.get("weight"),
        "audit_count": audit.get("count"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
