"""Tier-1 mechanism intake — the "signed score post" dogfood primitive.

Contract: deploy/MECHANISM_ROUTER_CONTRACT.md. This module is the *signed*
tier: a mechanism owner signs their own hotkey and posts a score vector for
the mechanism they own. It is deliberately the smallest possible on-ramp for
a new proof rail — a mechanism owner does not need to build an artifact
pipeline to get weight; they sign a claim. The router's `compose()` (real
impl in mechanism_router.py, stubbed here — see that module's docstring)
later blends this into the final weight vector at `weight_fraction`.

Wire contract
-------------
``POST /mechanisms/{mechanism_id}/scores``

Headers (same convention as scaffold/publisher/v2_bitset_submit.py /
scaffold/publisher/auth.py — sr25519 over canonical JSON bytes):
  * ``X-Cathedral-Hotkey``    — poster's ss58 address.
  * ``X-Cathedral-Signature`` — base64 sr25519 signature over the canonical
    bytes of the body (see ``canonical_score_post_bytes``).

Body (exactly these three top-level keys; extra/missing keys are rejected as
malformed)::

    {
      "mechanism_id": "<slug, must match the path segment>",
      "scores": {"<uid>": <nonneg finite number>, ...},   // uid as JSON string key
      "signed_at_ms": <positive int, epoch ms>
    }

Canonicalization: ``json.dumps(body, sort_keys=True, separators=(",", ":"))``
encoded as UTF-8 — the exact style used by
``v2_bitset_submit.canonical_submit_bytes`` / ``auth.canonical_claim_bytes``.
The signature is computed over those bytes, over the payload AS SENT (the
three allowed keys only, no ``signature`` field mixed in).

Response (200, ``accepted: true``)::

    {"mechanism_id": ..., "n_uids": <int>, "signed_at_ms": <int>,
     "accepted": true, "receipt_id": "<str>"}

Failure modes: 400 (malformed body / unknown mechanism / bad score values),
401 (signature does not verify), 403 (signer is not the mechanism's
``owner_pubkey``), 413 (body exceeds the configured cap — enforced BEFORE
JSON parsing), 404 (intake disabled — default OFF), 503 (no MechanismStore
wired by the integrator yet).

Mounting: this module exposes a plain ``APIRouter`` (``router``) — it does
NOT touch app.py. The integrator does::

    from scaffold.publisher import mechanism_intake
    mechanism_intake.set_mechanism_store(my_store)   # or app.dependency_overrides[...]
    app.include_router(mechanism_intake.router)

Guardrails: default OFF (``CATHEDRAL_MECHANISM_INTAKE_ENABLED`` unset ->
404 on every request), testnet mindset (no chain calls here at all — this
module only verifies a signature and writes to a store), no secrets in code
or logs, body size capped before any JSON parsing occurs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .auth import HotkeyVerifier, default_verifier
from .mechanism_router import MechanismStore, ScoreVector, ScoreVectorMeta

INTAKE_ENABLED_ENV = "CATHEDRAL_MECHANISM_INTAKE_ENABLED"
MAX_BODY_BYTES_ENV = "CATHEDRAL_MECHANISM_INTAKE_MAX_BODY_BYTES"
MAX_SCORES_ENV = "CATHEDRAL_MECHANISM_INTAKE_MAX_SCORES"

DEFAULT_MAX_BODY_BYTES = 65_536
DEFAULT_MAX_SCORES = 8_192

_ALLOWED_KEYS = {"mechanism_id", "scores", "signed_at_ms"}
_UID_RE = re.compile(r"^\d+$")
_RECEIPT_NS = b"cathedral:mechanism-intake:v1\0"

router = APIRouter()


class MechanismIntakeError(HTTPException):
    """HTTPException subclass so `raise` sites read as domain errors."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def intake_enabled() -> bool:
    return _env_bool(INTAKE_ENABLED_ENV, False)


def _max_body_bytes() -> int:
    return max(1024, _env_int(MAX_BODY_BYTES_ENV, DEFAULT_MAX_BODY_BYTES))


def _max_scores() -> int:
    return max(1, _env_int(MAX_SCORES_ENV, DEFAULT_MAX_SCORES))


# --- store / verifier wiring -------------------------------------------------
#
# The integrator mounts `router` and either calls `set_mechanism_store` /
# `set_hotkey_verifier` once at startup, or uses FastAPI's
# `app.dependency_overrides[get_mechanism_store] = lambda: real_store` (both
# are supported since these are plain functions used as `Depends` targets).

_store_override: MechanismStore | None = None
_verifier_override: HotkeyVerifier | None = None
_default_verifier_cache: HotkeyVerifier | None = None


def set_mechanism_store(store: MechanismStore | None) -> None:
    global _store_override
    _store_override = store


def get_mechanism_store() -> MechanismStore | None:
    return _store_override


def set_hotkey_verifier(verifier: HotkeyVerifier | None) -> None:
    global _verifier_override
    _verifier_override = verifier


def get_hotkey_verifier() -> HotkeyVerifier:
    global _default_verifier_cache
    if _verifier_override is not None:
        return _verifier_override
    if _default_verifier_cache is None:
        _default_verifier_cache = default_verifier()
    return _default_verifier_cache


# --- canonicalization + validation -------------------------------------------

def canonical_score_post_bytes(body: dict[str, Any]) -> bytes:
    """Byte-identical to the wire body: sort_keys + compact separators, UTF-8.

    Matches the style of v2_bitset_submit.canonical_submit_bytes /
    auth.canonical_claim_bytes so signers can reuse one canonicalization
    routine across Cathedral's signed-post surfaces.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _validate_shape(payload: Any, *, path_mechanism_id: str) -> tuple[bytes, dict[str, Any], int]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_body")
    extra = set(payload.keys()) - _ALLOWED_KEYS
    if extra:
        raise HTTPException(status_code=400, detail="unexpected_fields")
    missing = _ALLOWED_KEYS - set(payload.keys())
    if missing:
        raise HTTPException(status_code=400, detail="missing_fields")

    body_mechanism_id = payload.get("mechanism_id")
    if not isinstance(body_mechanism_id, str) or body_mechanism_id.strip() != path_mechanism_id:
        raise HTTPException(status_code=400, detail="mechanism_id_mismatch")

    scores_raw = payload.get("scores")
    if not isinstance(scores_raw, dict) or not scores_raw:
        raise HTTPException(status_code=400, detail="invalid_scores")
    if len(scores_raw) > _max_scores():
        raise HTTPException(status_code=400, detail="too_many_scores")

    signed_at_ms = payload.get("signed_at_ms")
    if isinstance(signed_at_ms, bool) or not isinstance(signed_at_ms, int) or signed_at_ms <= 0:
        raise HTTPException(status_code=400, detail="invalid_signed_at_ms")

    # Canonicalize the payload EXACTLY as received (post shape-validation, pre
    # value-validation) so signature verification checks what was actually
    # signed, not a server-side re-derivation of it.
    canonical = canonical_score_post_bytes(payload)
    return canonical, scores_raw, signed_at_ms


def _validate_scores(scores_raw: dict[str, Any]) -> ScoreVector:
    out: ScoreVector = {}
    for uid_str, score_val in scores_raw.items():
        if not isinstance(uid_str, str) or not _UID_RE.match(uid_str):
            raise HTTPException(status_code=400, detail="invalid_uid")
        uid = int(uid_str)
        if isinstance(score_val, bool) or not isinstance(score_val, (int, float)):
            raise HTTPException(status_code=400, detail="invalid_score")
        score = float(score_val)
        if not math.isfinite(score) or score < 0.0:
            raise HTTPException(status_code=400, detail="invalid_score")
        out[uid] = score
    return out


def _receipt_id(canonical: bytes) -> str:
    return "mech-" + hashlib.sha256(_RECEIPT_NS + canonical).hexdigest()[:32]


# --- route --------------------------------------------------------------

@router.post("/mechanisms/{mechanism_id}/scores")
async def post_scores(
    mechanism_id: str,
    request: Request,
    x_cathedral_hotkey: str = Header(...),
    x_cathedral_signature: str = Header(...),
    store: MechanismStore | None = Depends(get_mechanism_store),
    verifier: HotkeyVerifier = Depends(get_hotkey_verifier),
) -> dict[str, Any]:
    if not intake_enabled():
        raise HTTPException(status_code=404, detail="mechanism_intake_not_enabled")
    if store is None:
        raise HTTPException(status_code=503, detail="mechanism_store_not_configured")

    mechanism_id = mechanism_id.strip()
    if not mechanism_id or len(mechanism_id) > 128:
        raise HTTPException(status_code=400, detail="invalid_mechanism_id")

    # Cap body size BEFORE any parsing — both the declared Content-Length (fast
    # reject) and the actual bytes read (in case the header lies/is absent).
    max_body = _max_body_bytes()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_body:
                raise HTTPException(status_code=413, detail="score_post_body_too_large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_content_length")
    raw_body = await request.body()
    if len(raw_body) > max_body:
        raise HTTPException(status_code=413, detail="score_post_body_too_large")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")

    canonical, scores_raw, signed_at_ms = _validate_shape(payload, path_mechanism_id=mechanism_id)

    # (1) signature valid
    if not verifier.verify(x_cathedral_hotkey, canonical, x_cathedral_signature):
        raise HTTPException(status_code=401, detail="invalid_signature")

    # (2) signing key == the mechanism's owner_pubkey — only the owner posts
    # that mechanism's scores.
    spec = store.get_spec(mechanism_id)
    if spec is None:
        raise HTTPException(status_code=400, detail="unknown_mechanism_id")
    if spec.owner_pubkey != x_cathedral_hotkey:
        raise HTTPException(status_code=403, detail="not_mechanism_owner")

    # (3) scores are nonneg finite floats, uid ints
    scores = _validate_scores(scores_raw)

    meta = ScoreVectorMeta(
        mechanism_id=mechanism_id,
        signed_at_ms=signed_at_ms,
        sig_ok=True,
        source="signed_post",
    )
    try:
        store.put_scores(mechanism_id, scores, meta)
    except Exception:
        raise HTTPException(status_code=500, detail="score_store_failed")

    return {
        "mechanism_id": mechanism_id,
        "n_uids": len(scores),
        "signed_at_ms": signed_at_ms,
        "accepted": True,
        "receipt_id": _receipt_id(canonical),
    }
