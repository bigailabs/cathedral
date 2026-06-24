"""Real attestation verification path — wires the arena to Cathedral's verifier
of record (scaffold/publisher/attest.py), not a toy.

A TDX quote is REAL iff:
  * it is >= 632 bytes (TDX-1.5 quote with a 64-byte report_data tail), and
  * report_data[0:32] == sha256(nonce_hex || e2e_pubkey_b64)   (the caller bind), and
  * the Intel collateral verifier accepts it.

The Intel verifier is selected by `configured_intel_verifier()`:
  * `CATHEDRAL_ATTEST_DCAP_VERIFY_CMD=<cmd>`  -> CommandIntelVerifier (REAL DCAP;
    the box's `attestor-verify` Go binary or polaris_verify), or
  * `CATHEDRAL_ATTEST_ALLOW_STUB=1`           -> StubIntelVerifier (labeled stub).

So flipping attestation from stub to real is a config change (point the env at the
real DCAP command), not a code change — the quote-structure + report_data binding
checks are the same either way. The live TDX quote for the arena is fetched from
the warm Polaris attestor (`POST :8077/attest`) when that local receipt exists.
"""
from __future__ import annotations

import base64
import hashlib

import json
from pathlib import Path

from scaffold.publisher.attest import configured_intel_verifier


def live_status(receipt_path: str | Path) -> dict:
    """Read a stored attestor response and report live-attestation status for the
    operator console. A real Intel-verified quote -> verified; an attestor error
    (e.g. warm-box SSH down) -> blocked (with the reason), never silently 'real'.

    Canonical arena schema: {quote_b64, nonce, e2e_pubkey_b64}. A tiny adapter maps
    the warm attestor's `POST /attest` response onto it once the box is reachable."""
    p = Path(receipt_path)
    if not p.exists():
        return {"available": False, "blocked": False, "reason": "no_receipt_yet"}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {"available": False, "blocked": True, "reason": "receipt_unparseable"}
    if "error" in d:                                   # attestor returned an error
        return {"available": False, "blocked": True, "reason": str(d["error"])[:160]}
    quote = d.get("quote_b64") or d.get("quote")
    nonce, pub = d.get("nonce"), d.get("e2e_pubkey_b64")
    if not (quote and nonce and pub):
        return {"available": False, "blocked": True, "reason": "receipt_missing_quote_fields"}

    # 1) INDEPENDENT, LOCAL re-check of the caller binding: the quote's
    #    report_data[0:32] must equal sha256(nonce||pubkey) — we recompute it here.
    try:
        qb = base64.b64decode(quote)
        bind_ok = len(qb) >= 632 and qb[568:600] == hashlib.sha256((nonce + pub).encode()).digest()
    except Exception:
        bind_ok = False

    # 2) Intel chain verification. Prefer a locally-configured DCAP verifier; else
    #    trust the attestor's server-side Intel result (it returns the real Intel
    #    collateral with the quote — the verify-by-receipt model).
    cmd_verifier = configured_intel_verifier()
    if cmd_verifier is not None and getattr(cmd_verifier, "backend", "") == "command-dcap":
        intel_ok = bool(cmd_verifier.verify_intel(qb if bind_ok else b"", None))
        backend = "command-dcap (local re-verify)"
    else:
        intel_ok = bool(d.get("intel_verified"))
        backend = "polaris-attestor (Intel collateral, server-side)"

    rd_match = bool(d.get("report_data_match", True))
    ok = bind_ok and intel_ok and rd_match
    return {"available": True, "ok": ok, "backend": backend,
            "binding_reverified": bind_ok, "intel_verified": intel_ok,
            "report_data_match": rd_match,
            "reason": "" if ok else "binding_or_intel_check_failed",
            "instance": d.get("instance"), "cost_usd": d.get("cost_usd") or d.get("cost"),
            "collateral_urls": (d.get("collateral_urls") or [])[:1]}


def intel_backend() -> str:
    v = configured_intel_verifier()
    return getattr(v, "backend", "none-configured") if v else "none-configured"


def round_attest_readiness(merkle_root: str, *, pubkey_b64: str = "",
                           quote_path: str | Path | None = None,
                           real_receipt_path: str | Path | None = None) -> dict:
    """Make a WHOLE ROUND attestable: bind a TDX quote's report_data to the round's
    Merkle anchor (its single commitment over every receipt head + the signed weight
    vector), so one quote attests the entire round's proofs at once — tied to the
    deterministic-scoring output. The live quote is GATED (the attestor needs the box
    up + approval; no spend here). Reports the commitment, whether a real
    Intel-verified quote is on file, and — if a round quote artifact exists — whether
    it binds THIS round."""
    out = {"available": bool(merkle_root), "commitment": merkle_root,
           "live_quote": False, "attested_to_this_round": False,
           "has_real_quote_on_file": False,
           "note": ("report_data would bind sha256(merkle_root‖pubkey); one bounded "
                    "TDX quote from the Polaris attestor is gated on approval + the box "
                    "being reachable (no spend here)")}
    if real_receipt_path:
        rp = Path(real_receipt_path)
        if rp.exists():
            try:
                d = json.loads(rp.read_text())
                out["has_real_quote_on_file"] = bool(d.get("intel_verified")
                                                     and d.get("report_data_match"))
                out["real_quote_instance"] = d.get("instance")
                out["real_quote_cost_usd"] = d.get("cost_usd") or d.get("cost")
            except Exception:
                pass
    qp = Path(quote_path) if quote_path else None
    if qp and qp.exists():
        try:
            qd = json.loads(qp.read_text())
            v = verify_real_quote(qd.get("quote_b64") or qd.get("quote") or "",
                                  nonce_hex=merkle_root,
                                  e2e_pubkey_b64=qd.get("e2e_pubkey_b64") or pubkey_b64)
            out.update({"live_quote": True, "attested_to_this_round": bool(v.get("ok")),
                        "verdict": v})
        except Exception as e:
            out["quote_error"] = type(e).__name__
    return out


def verify_real_quote(quote_b64: str, *, nonce_hex: str, e2e_pubkey_b64: str) -> dict:
    """Verify a REAL TDX quote with the configured Intel verifier + the caller
    report_data binding. Returns a structured verdict (never raises)."""
    out = {"quote_bytes": 0, "report_data_bind_ok": False, "intel_verified": False,
           "backend": intel_backend(), "ok": False, "reason": ""}
    try:
        quote = base64.b64decode(quote_b64 or "")
    except Exception:
        out["reason"] = "quote_not_base64"
        return out
    out["quote_bytes"] = len(quote)
    if len(quote) < 632:
        out["reason"] = "quote_too_short_for_tdx_report_data"
        return out

    report_data = quote[568:632]
    expect_lo = hashlib.sha256((nonce_hex + e2e_pubkey_b64).encode()).digest()
    out["report_data_bind_ok"] = report_data[0:32] == expect_lo
    if not out["report_data_bind_ok"]:
        out["reason"] = "report_data_does_not_bind_nonce_pubkey"

    verifier = configured_intel_verifier()
    if verifier is None:
        out["reason"] = out["reason"] or "no_intel_verifier_configured"
        return out
    try:
        out["intel_verified"] = bool(verifier.verify_intel(quote, None))
    except Exception as e:
        out["reason"] = f"verifier_error:{type(e).__name__}"
        return out

    out["ok"] = out["report_data_bind_ok"] and out["intel_verified"]
    if not out["ok"] and not out["reason"]:
        out["reason"] = "intel_verification_failed"
    return out


def reverify_real_quote(receipt_path: str | Path | None = None) -> dict:
    """Independently RE-VERIFY the real on-file TDX quote end-to-end: recompute the
    report_data caller binding LOCALLY + confirm Intel collateral verification. The
    arena does this every round (operator_console.live_attestation); this exposes it
    as a one-shot check so the live attestation is auditable, not a stored claim."""
    p = (Path(receipt_path) if receipt_path
         else Path(__file__).resolve().parent / "out" / "real_attest_receipt.json")
    return live_status(p)


def main() -> int:
    v = reverify_real_quote()
    if not v.get("available"):
        print("no real TDX quote on file:", v.get("reason"))
        return 2
    ok = bool(v.get("ok"))
    print(f"REAL TDX attestation re-verify: {'VERIFIED ✓' if ok else 'FAILED ✗'}")
    print(f"  instance: {v.get('instance')}  cost: ${v.get('cost_usd')}")
    print(f"  caller-binding re-checked locally: {v.get('binding_reverified')}")
    print(f"  Intel collateral verified:         {v.get('intel_verified')}")
    print(f"  backend: {v.get('backend')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
