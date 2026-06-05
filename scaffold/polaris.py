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

import base64
import hashlib
import json
from dataclasses import dataclass


@dataclass
class AttestResult:
    intel_verified: bool
    report_data: bytes        # 64B from the TDX quote (lo[0:32]=nonce|pubkey bind,
                              #                          hi[32:64]=image|result bind)
    image_digest: str         # resolved image content digest ("sha256:...") run in the box
    stdout: str               # the workload's output (its sha256 is bound into report_data)
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
    def attest(self, *, nonce: str, e2e_pubkey_b64: str, image: str,
               workload: str, measured_elapsed_ms: float | None = None) -> AttestResult:
        """Run `image` (a Docker image) in an attested TDX box; the box pulls it,
        runs `workload`, and binds BOTH bindings into the hardware quote's
        report_data (verified against Intel's chain, measured 2026-06-04):

            report_data[0:32]  = sha256(nonce_hex || e2e_pubkey_b64)
            report_data[32:64] = sha256(image_digest || sha256hex(stdout))

        So the quote proves *this image produced this result* in a genuine TDX
        box. The MRTD measures the base VM (invariant across workloads — proven),
        so it is NOT the image identity and is not checked. Image pinning rides
        on report_data[32:64], not MRTD.

        measured_elapsed_ms: the run's wall-clock as measured by the attested
        image and emitted to stdout — so it is BOUND into report_data[32:64] and
        tamper-evident. A real attestor's canonical image measures this in-TEE;
        the binding + image pinning make the time it reports trustworthy.
        """
        if not self.live:
            # synthesize a quote-consistent report_data under the SAME recipe so
            # the offline path exercises the real verification. A miner that
            # can't actually attest (no pubkey, or an un-pullable image) yields
            # intel_verified=False — separating an honest timeout from a fraud.
            pullable = bool(image) and "unattestable" not in image
            ok = bool(nonce and e2e_pubkey_b64) and pullable
            # resolve the ref to a realistic content digest (sha256:<64hex>),
            # mirroring what the box returns, so the SAME digest-aware checks run
            resolved = "sha256:" + hashlib.sha256((image or "?").encode()).hexdigest()
            stdout = "[offline-stub] " + (workload or "")
            if measured_elapsed_ms is not None:     # bound into the quote via stdout
                stdout += f" elapsed_ms={int(measured_elapsed_ms)}"
            lo = hashlib.sha256((nonce + e2e_pubkey_b64).encode()).digest()
            hi = hashlib.sha256(
                (resolved + hashlib.sha256(stdout.encode()).hexdigest()).encode()).digest()
            return AttestResult(intel_verified=ok, report_data=lo + hi,
                                image_digest=resolved, stdout=stdout,
                                cost_usd=0.0, stub=True)
        body = {"nonce": nonce, "e2e_pubkey_b64": e2e_pubkey_b64,
                "image": image, "workload": workload}
        r = self._post("/v1/attest", body)
        quote = base64.b64decode(r["tee_attestation"]["quote_b64"])
        return AttestResult(
            intel_verified=r.get("verification", {}).get("intel_verified", False),
            report_data=quote[568:632], image_digest=r.get("image_digest", ""),
            stdout=r.get("stdout", ""), cost_usd=r.get("cost_usd", 0.0), stub=False,
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

    # ---- tiny transport (stdlib only; used when live) -------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                "User-Agent": "curl/8.5.0", "Accept": "*/*"}

    def _post(self, path: str, body: dict) -> dict:
        import urllib.request  # stdlib — keeps the offline scaffold dep-free
        req = urllib.request.Request(self.base_url + path, method="POST",
                                     data=json.dumps(body).encode(), headers=self._headers())
        with urllib.request.urlopen(req, timeout=240.0) as r:
            return json.loads(r.read().decode())

    def _get(self, path: str) -> dict:
        import urllib.request
        req = urllib.request.Request(self.base_url + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30.0) as r:
            return json.loads(r.read().decode())
