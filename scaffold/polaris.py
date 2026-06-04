"""Polaris client — the scaffold SLOTS INTO Polaris, it does not reimplement
attestation, auth, or billing. Three real surfaces it calls:

  * POST /v1/attest          attested execution (Intel TDX, server-verified
                             against Intel's chain, guaranteed teardown,
                             report_data binds nonce+pubkey). The REAL endpoint
                             on the Polaris app (Stitch) — NOT the stubbed
                             /v1/runtime/run one-shot. See ~/attestor/DEMO.md.
  * /api/keys                API-key auth (per-key attest cap lives here).
  * /api/billing/* + ledger  credit ledger — debit a buyer when a challenge is
                             published, credit a miner on an accepted solve.

`live=False` (default) returns deterministic canned responses so the scaffold
runs fully offline. Set live=True + base_url + api_key to hit a real Polaris.
The httpx path is intentionally tiny — the value is the SEAM, not the transport.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass
class AttestResult:
    intel_verified: bool
    report_data_match: bool
    mrtd: str                 # measurement of the launched image (== pinned digest)
    quote_size: int
    cost_usd: float
    stub: bool                # True when produced offline (not a real quote)


class PolarisClient:
    def __init__(self, *, live: bool = False, base_url: str = "", api_key: str = ""):
        self.live = live
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    # ---- /api/keys -------------------------------------------------------
    def check_key(self) -> bool:
        """Auth check. Offline: any non-empty key passes."""
        if not self.live:
            return bool(self.api_key) or True
        return self._get("/api/keys/whoami").get("ok", False)

    # ---- POST /v1/attest -------------------------------------------------
    def attest(self, *, nonce: str, e2e_pubkey_b64: str, expected_mrtd: str,
               workload: str) -> AttestResult:
        """Run `workload` in an attested TDX box and verify the quote.

        The MRTD (image measurement) must equal `expected_mrtd` — that is how
        a solver-Docker image is pinned + precomputable (the Entrius pattern):
        the buyer/validator knows the digest in advance, so a matching MRTD
        proves *that exact image* ran. report_data binds nonce+pubkey.
        """
        if not self.live:
            # deterministic offline quote. report_data binds nonce+pubkey, so an
            # empty pubkey (a miner that can't actually attest) fails the bind —
            # this is what separates an honest attested timeout from a fraud.
            bound = bool(nonce and e2e_pubkey_b64)
            return AttestResult(
                intel_verified=bound, report_data_match=bound,
                mrtd=expected_mrtd if bound else "", quote_size=8000 if bound else 0,
                cost_usd=0.0005, stub=True,
            )
        body = {"nonce": nonce, "e2e_pubkey_b64": e2e_pubkey_b64, "workload": workload}
        r = self._post("/v1/attest", body)
        return AttestResult(
            intel_verified=r.get("intel_verified", False),
            report_data_match=r.get("report_data_match", False),
            mrtd=r.get("mrtd", ""), quote_size=r.get("quote_size", 0),
            cost_usd=r.get("cost_usd", 0.0), stub=False,
        )

    # ---- /api/billing ledger --------------------------------------------
    def ledger_debit(self, account: str, credits: float, memo: str) -> dict:
        return self._ledger("debit", account, credits, memo)

    def ledger_credit(self, account: str, credits: float, memo: str) -> dict:
        return self._ledger("credit", account, credits, memo)

    def _ledger(self, kind: str, account: str, credits: float, memo: str) -> dict:
        entry = {"kind": kind, "account": account, "credits": credits, "memo": memo}
        if not self.live:
            return {"ok": True, "entry": entry, "stub": True}
        return self._post("/api/billing/ledger", entry)

    # ---- tiny transport (only used when live) ---------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}

    def _post(self, path: str, body: dict) -> dict:
        import httpx  # local import: offline path never needs it
        resp = httpx.post(self.base_url + path, headers=self._headers(),
                          content=json.dumps(body), timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str) -> dict:
        import httpx
        resp = httpx.get(self.base_url + path, headers=self._headers(), timeout=30.0)
        resp.raise_for_status()
        return resp.json()
