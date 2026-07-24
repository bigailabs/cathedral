"""Default-off TEE GPU capacity intake for Chutes handoff.

This is deliberately an off-chain revenue lane, not an emissions lane. It only
writes tee_gpu_* tables, never eval_runs / lane_challenge_solves / per_miner_solves,
so the existing validator vector is unchanged.
"""
from __future__ import annotations

import hashlib
import html
import hmac
import json
import math
import os
import secrets
import shlex
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from .auth import canonical_claim_bytes, default_verifier
from .store import Store, new_uuid

try:
    from fastapi import Header, HTTPException, Query, Request
except Exception:  # lets storage/preflight helpers run without publisher deps
    def Header(*args, **kwargs):  # type: ignore[no-redef]
        return None

    def Query(default=None, *args, **kwargs):  # type: ignore[no-redef]
        return default

    class Request:  # type: ignore[no-redef]
        pass

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: Any):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

TEE_CARD_ID = "cathedral-tee-gpu-capacity-v1"
AUTHORIZATION_VERSION = "cathedral-secure-compute-capacity-v1"
AUTHORIZATION_TEXT = (
    "I authorize Cathedral to use this machine for secure compute and mining "
    "workloads while this capacity offer is active."
)
_SKEW_SECS = 300

# Live Chutes /nodes/supported snapshot checked 2026-06-19. The endpoint is the
# short-ref truth for add-node, but it does not expose a tee_capable flag.
_CHUTES_SUPPORTED = {
    "3090", "4090", "5090", "a4000", "a4000_ada", "a5000", "a6000",
    "a6000_ada", "a10", "a40", "a100", "a100_sxm", "a100_40gb",
    "a100_40gb_sxm", "b200", "b300", "h20", "h100", "h100_nvl",
    "h100_sxm", "h200", "h800", "l4", "l40", "l40s", "mi300x",
    "pro_6000",
}
# Live Chutes /servers/tee/measurements checked 2026-06-19. Public accepted TEE
# measurement profiles are 8x h200, 8x pro_6000, 8x b200, and 8x b300.
_TEE_CANDIDATES = {"h200", "b200", "b300", "pro_6000"}
_TEE_MEASUREMENT_GPU_COUNTS = {
    "h200": {8},
    "b200": {8},
    "b300": {8},
    "pro_6000": {8},
}
# Exploratory, non-emission, non-Chutes intake. These exist only so the public
# "tell us if you can provide Google TPU inference capacity" ask has a real
# place to land without weakening the TEE GPU gate.
_EXPLORATORY_TPU_REFS = {"google_tpu", "tpu_v5e", "tpu_v5p", "tpu_v6e"}
_DEFAULT_CHUTES_VALIDATOR = "5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ"
_STATUS = {"pending", "active", "paused", "rejected", "retired"}
_LISTABLE_STATUS = {"active"}
_REVIEWED_REOPEN_STATUS = {"active", "paused"}
_PROVIDER_ACCEPTED_STATUS = {"accepted", "active", "listed", "running", "ready"}
_EVIDENCE_CRYPTO_STATUS = "cryptographically_verified"
_EVIDENCE_ACCEPTED_STATUS = {"operator_reviewed", _EVIDENCE_CRYPTO_STATUS}
_EVIDENCE_REVIEW_STATUS = {"operator_reviewed", "needs_review", "rejected", _EVIDENCE_CRYPTO_STATUS}
_EVIDENCE_MANUAL_REVIEW_STATUS = {"operator_reviewed", "needs_review", "rejected"}
_DEFAULT_EVIDENCE_REQUEST_TTL_SECS = 600
_INTAKE_REQUIRE_CODE_ENV = "CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE"
_INTAKE_CODE_ENV = "CATHEDRAL_TEE_GPU_INTAKE_CODE"
_INTAKE_ALLOWLIST_ENV = "CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST"
_INTAKE_CODE_FIELDS = {"intake_code", "invite_code", "access_code"}
_MINER_REVIEW_FIELDS = (
    "gpu_short_ref", "gpu_count", "agent_api", "tee_kind",
    "tdx_claimed", "gpu_cc_claimed", "hourly_cost", "attestation_digest",
)


def tee_gpu_enabled() -> bool:
    return _truthy(os.environ.get("CATHEDRAL_TEE_GPU_ENABLED", ""))


def public_catalog_enabled() -> bool:
    return tee_gpu_enabled() and _truthy(
        os.environ.get("CATHEDRAL_TEE_GPU_PUBLIC_CATALOG_ENABLED", ""))


def intake_gate_status() -> dict[str, Any]:
    allowed = _intake_allowlist()
    code_configured = bool(os.environ.get(_INTAKE_CODE_ENV, "").strip())
    require_code = _truthy(os.environ.get(_INTAKE_REQUIRE_CODE_ENV, ""))
    configured = code_configured or bool(allowed)
    return {
        "required": True,
        "require_code": require_code,
        "code_configured": code_configured,
        "allowlist_count": len(allowed),
        "configured": configured,
        "accepting_invited_miners": configured,
    }


def require_admin(authorization: str | None) -> None:
    if not tee_gpu_enabled():
        raise HTTPException(404, "tee_gpu_disabled")
    token = os.environ.get("CATHEDRAL_TEE_GPU_ADMIN_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "tee_gpu_admin_token_not_configured")
    supplied = (authorization or "").strip()
    if supplied.lower().startswith("bearer "):
        supplied = supplied.split(" ", 1)[1].strip()
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "invalid_admin_token")


def register_routes(app, store: Store) -> None:
    from fastapi.responses import HTMLResponse, JSONResponse

    verifier = default_verifier()

    @app.post("/v1/tee-gpu/offers")
    async def tee_gpu_submit_offer(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
    ):
        if not tee_gpu_enabled():
            raise HTTPException(404, "tee_gpu_disabled")
        body = await _json_body(request)
        payload_digest = _payload_digest(body)
        node_id = _required_str(body, "node_id")
        _verify_offer_claim(
            verifier, x_cathedral_hotkey, x_cathedral_signature,
            x_cathedral_submitted_at, node_id=node_id, payload_digest=payload_digest)
        require_miner_intake_gate(x_cathedral_hotkey, body)
        record = create_capacity(
            store, body, owner_hotkey=x_cathedral_hotkey,
            actor=x_cathedral_hotkey, event_type="submitted",
            preserve_admin_fields=True)
        return JSONResponse({"status": record["status"], "capacity": miner_record(record)})

    @app.get("/v1/tee-gpu/offers")
    def tee_gpu_my_offers(
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
    ):
        if not tee_gpu_enabled():
            raise HTTPException(404, "tee_gpu_disabled")
        _verify_offer_claim(
            verifier, x_cathedral_hotkey, x_cathedral_signature,
            x_cathedral_submitted_at, node_id="list", payload_digest="")
        rows = list_capacity(store, owner_hotkey=x_cathedral_hotkey)
        return {"items": [miner_record(r) for r in rows], "count": len(rows)}

    @app.post("/v1/tee-gpu/evidence-request")
    async def tee_gpu_evidence_request(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
    ):
        if not tee_gpu_enabled():
            raise HTTPException(404, "tee_gpu_disabled")
        body = await _json_body(request)
        node_id = _required_str(body, "node_id")
        _verify_offer_claim(
            verifier, x_cathedral_hotkey, x_cathedral_signature,
            x_cathedral_submitted_at, node_id=node_id,
            payload_digest=_payload_digest(body))
        require_miner_intake_gate(x_cathedral_hotkey, body)
        return create_evidence_request(
            store, owner_hotkey=x_cathedral_hotkey, node_id=node_id,
            actor=x_cathedral_hotkey, ttl_secs=_optional_int(body.get("ttl_secs")))

    @app.post("/v1/admin/tee-gpu/capacity")
    async def tee_gpu_admin_create(
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _json_body(request)
        owner = _required_str(body, "owner_hotkey")
        record = create_capacity(store, body, owner_hotkey=owner, actor="admin",
                                 event_type="admin_created", allow_requested_status=True)
        return JSONResponse({"capacity": admin_record(record, store=store)})

    @app.patch("/v1/admin/tee-gpu/capacity/{capacity_id}")
    async def tee_gpu_admin_update(
        capacity_id: str,
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _json_body(request)
        record = update_capacity_admin(store, capacity_id, body)
        if record is None:
            raise HTTPException(404, "capacity_not_found")
        return {"capacity": admin_record(record, store=store)}

    @app.post("/v1/admin/tee-gpu/capacity/{capacity_id}/attestation-review")
    async def tee_gpu_admin_attestation_review(
        capacity_id: str,
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _json_body(request)
        record = review_capacity_evidence(store, capacity_id, body)
        if record is None:
            raise HTTPException(404, "capacity_not_found")
        return {"capacity": admin_record(record, store=store)}

    @app.post("/v1/admin/tee-gpu/capacity/{capacity_id}/verify-evidence")
    async def tee_gpu_admin_verify_evidence(
        capacity_id: str,
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _optional_json_body(request)
        record = verify_capacity_evidence(store, capacity_id, body)
        if record is None:
            raise HTTPException(404, "capacity_not_found")
        return {"capacity": admin_record(record, store=store)}

    @app.post("/v1/admin/tee-gpu/capacity/{capacity_id}/provider-status")
    async def tee_gpu_admin_provider_status(
        capacity_id: str,
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _json_body(request)
        record = record_provider_status(store, capacity_id, body)
        if record is None:
            raise HTTPException(404, "capacity_not_found")
        return {"capacity": admin_record(record, store=store)}

    @app.post("/v1/admin/tee-gpu/capacity/{capacity_id}/health-receipt")
    async def tee_gpu_admin_health_receipt(
        capacity_id: str,
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _json_body(request)
        record = record_health_receipt(store, capacity_id, body)
        if record is None:
            raise HTTPException(404, "capacity_not_found")
        return {"capacity": admin_record(record, store=store)}

    @app.post("/v1/admin/tee-gpu/capacity/{capacity_id}/usage-receipt")
    async def tee_gpu_admin_usage_receipt(
        capacity_id: str,
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _json_body(request)
        record = record_usage_receipt(store, capacity_id, body)
        if record is None:
            raise HTTPException(404, "capacity_not_found")
        return {"capacity": admin_record(record, store=store)}

    @app.get("/v1/admin/tee-gpu/capacity")
    def tee_gpu_admin_list(
        status: str | None = Query(None),
        owner_hotkey: str | None = Query(None),
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        rows = list_capacity(store, status=status, owner_hotkey=owner_hotkey)
        return {"items": [admin_record(r, store=store) for r in rows], "count": len(rows)}

    @app.get("/v1/admin/tee-gpu/metrics")
    def tee_gpu_admin_metrics(authorization: str | None = Header(None)):
        require_admin(authorization)
        return capacity_metrics(store)

    @app.get("/v1/admin/tee-gpu/dashboard")
    def tee_gpu_admin_dashboard(authorization: str | None = Header(None)):
        require_admin(authorization)
        return HTMLResponse(
            tee_gpu_dashboard_html(store),
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/admin/tee-gpu/chutes-manifest")
    def tee_gpu_admin_chutes_manifest(
        status: str = Query("active"),
        include_blocked: bool = Query(False),
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        rows = list_capacity(store, status=status)
        eligible = [r for r in rows if include_blocked or r["preflight_status"] == "eligible"]
        return {
            "count": len(eligible),
            "omitted_blocked": len(rows) - len(eligible),
            "items": [chutes_manifest_item(r) for r in eligible],
        }

    @app.post("/v1/admin/tee-gpu/capacity/{capacity_id}/chutes-list")
    async def tee_gpu_admin_chutes_list(
        capacity_id: str,
        request: Request,
        authorization: str | None = Header(None),
    ):
        require_admin(authorization)
        body = await _optional_json_body(request)
        result = list_capacity_on_chutes(
            store,
            capacity_id,
            execute=_boolish(body.get("execute")),
            timeout_secs=_optional_int(body.get("timeout_secs")),
        )
        return result

    @app.get("/v1/tee-gpu/capacity")
    def tee_gpu_public_catalog():
        if not public_catalog_enabled():
            raise HTTPException(404, "tee_gpu_catalog_disabled")
        rows = [
            r for r in list_capacity(store, status="active")
            if capacity_launch_evidence(store, r["capacity_id"])["production_compute_ready"]
        ]
        return {"items": [public_record(r) for r in rows], "count": len(rows)}


def create_capacity(
    store: Store,
    body: dict[str, Any],
    *,
    owner_hotkey: str,
    actor: str,
    event_type: str,
    allow_requested_status: bool = False,
    preserve_admin_fields: bool = False,
) -> dict[str, Any]:
    now = _now_iso()
    node_id = _required_str(body, "node_id")
    gpu_short_ref = _required_str(body, "gpu_short_ref").lower()
    gpu_count = _int(body.get("gpu_count"), default=0)
    hourly_cost_raw = body.get("hourly_cost")
    if hourly_cost_raw is not None and not _is_finite_float(hourly_cost_raw):
        raise HTTPException(400, "invalid_hourly_cost")
    hourly_cost = _float(hourly_cost_raw, default=0.0)
    attestation_json = _attestation_input_json(
        body.get("attestation") or body.get("attestation_json") or {},
        allow_review=False,
    )
    if allow_requested_status and "attestation_review" in body:
        attestation_json = _attestation_review_json(
            attestation_json, body["attestation_review"], reviewed_by=actor)
    health_json = _json_blob(body.get("health") or body.get("health_json") or {})
    attestation_digest = _digest_text(attestation_json) if attestation_json != "{}" else ""
    evidence = evidence_summary(attestation_json)
    operator_use_authorized = _operator_use_authorized(body)
    if not allow_requested_status and not operator_use_authorized:
        raise HTTPException(400, {
            "detail": "operator_use_authorization_required",
            "required_field": "operator_use_authorized",
            "authorization_version": AUTHORIZATION_VERSION,
            "authorization_text": AUTHORIZATION_TEXT,
        })
    authorization_json = _authorization_json(
        body, owner_hotkey=owner_hotkey, node_id=node_id,
        accepted=operator_use_authorized,
        source="admin_record" if allow_requested_status else "signed_miner_offer",
        accepted_at_iso=now,
    )
    authorization_digest = _digest_text(authorization_json) if operator_use_authorized else ""
    tdx_claimed = _boolish(body.get("tdx_claimed") or body.get("tdx"))
    gpu_cc_claimed = _boolish(body.get("gpu_cc_claimed") or body.get("gpu_cc"))
    preflight = preflight_capacity(
        gpu_short_ref=gpu_short_ref, gpu_count=gpu_count, hourly_cost=hourly_cost,
        agent_api=str(body.get("agent_api") or ""), tee_kind=str(body.get("tee_kind") or ""),
        tdx_claimed=tdx_claimed, gpu_cc_claimed=gpu_cc_claimed,
        operator_use_authorized=operator_use_authorized,
        has_attestation=bool(attestation_digest),
        evidence_status=evidence["status"],
        evidence_acceptable=bool(evidence["acceptable"]),
    )
    status = str(body.get("status") or "pending").lower() if allow_requested_status else "pending"
    if status not in _STATUS:
        status = "pending"
    if status == "active" and preflight["status"] != "eligible":
        status = "pending"

    capacity_id = _capacity_id(owner_hotkey, node_id)
    if evidence["status"] == "operator_reviewed":
        request_id = _evidence_request_id(_loads(attestation_json))
        if not _evidence_request_exists(store, capacity_id, request_id):
            raise HTTPException(400, "evidence_request_not_found")
    row = {
        "capacity_id": capacity_id,
        "provider_ref": str(body.get("provider_ref") or body.get("provider") or ""),
        "owner_hotkey": owner_hotkey,
        "node_id": node_id,
        "region": str(body.get("region") or ""),
        "endpoint_url": str(body.get("endpoint_url") or ""),
        "agent_api": str(body.get("agent_api") or ""),
        "public_ip": str(body.get("public_ip") or ""),
        "gpu_short_ref": gpu_short_ref,
        "gpu_model": str(body.get("gpu_model") or ""),
        "gpu_count": gpu_count,
        "gpu_memory_gb": _optional_int(body.get("gpu_memory_gb")),
        "tee_kind": str(body.get("tee_kind") or ""),
        "tdx_claimed": 1 if tdx_claimed else 0,
        "gpu_cc_claimed": 1 if gpu_cc_claimed else 0,
        "hourly_cost": hourly_cost,
        "currency": str(body.get("currency") or "USD").upper(),
        "operator_use_authorized": 1 if operator_use_authorized else 0,
        "authorization_version": AUTHORIZATION_VERSION if operator_use_authorized else "",
        "authorization_digest": authorization_digest,
        "authorization_json": authorization_json,
        "attestation_digest": attestation_digest,
        "attestation_json": attestation_json,
        "health_json": health_json,
        "status": status,
        "preflight_status": preflight["status"],
        "preflight_json": _json_blob(preflight),
        "chutes_validator_hotkey": str(
            (body.get("chutes_validator_hotkey") if allow_requested_status else "")
            or os.environ.get("CATHEDRAL_TEE_GPU_CHUTES_VALIDATOR_HOTKEY")
            or _DEFAULT_CHUTES_VALIDATOR),
        "chutes_server_name": str(
            (body.get("chutes_server_name") if allow_requested_status else "") or ""),
        "chutes_server_id": str(body.get("chutes_server_id") or "") if allow_requested_status else "",
        "chutes_status": str(body.get("chutes_status") or "") if allow_requested_status else "",
        "emissions_eligible": 0,
        "admin_note": str(body.get("admin_note") or "") if allow_requested_status else "",
        "created_at_iso": now,
        "updated_at_iso": now,
        "last_heartbeat_iso": body.get("last_heartbeat_iso"),
    }

    def _do(conn):
        existing = conn.execute(
            "SELECT * FROM tee_gpu_capacity WHERE capacity_id=?",
            (capacity_id,)).fetchone()
        if existing:
            row["created_at_iso"] = existing["created_at_iso"]
            if preserve_admin_fields:
                for key in (
                    "status", "admin_note", "chutes_validator_hotkey",
                    "chutes_server_name", "chutes_server_id", "chutes_status",
                ):
                    row[key] = existing[key]
                material_changed = any(row[key] != existing[key] for key in _MINER_REVIEW_FIELDS)
                if material_changed:
                    if existing["status"] in _REVIEWED_REOPEN_STATUS:
                        row["status"] = "pending"
                    if existing["chutes_status"] in {"listed", "needs_relisting"}:
                        row["chutes_status"] = "needs_relisting"
                    elif existing["status"] in _REVIEWED_REOPEN_STATUS:
                        row["chutes_status"] = "needs_review"
                if row["status"] == "active" and row["preflight_status"] != "eligible":
                    row["status"] = "pending"
        conn.execute(
            "INSERT OR REPLACE INTO tee_gpu_capacity("
            "capacity_id, provider_ref, owner_hotkey, node_id, region, endpoint_url, "
            "agent_api, public_ip, gpu_short_ref, gpu_model, gpu_count, gpu_memory_gb, "
            "tee_kind, tdx_claimed, gpu_cc_claimed, hourly_cost, currency, "
            "operator_use_authorized, authorization_version, authorization_digest, authorization_json, "
            "attestation_digest, attestation_json, health_json, status, "
            "preflight_status, preflight_json, chutes_validator_hotkey, "
            "chutes_server_name, chutes_server_id, chutes_status, emissions_eligible, "
            "admin_note, created_at_iso, updated_at_iso, last_heartbeat_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(row[k] for k in (
                "capacity_id", "provider_ref", "owner_hotkey", "node_id", "region",
                "endpoint_url", "agent_api", "public_ip", "gpu_short_ref", "gpu_model",
                "gpu_count", "gpu_memory_gb", "tee_kind", "tdx_claimed",
                "gpu_cc_claimed", "hourly_cost", "currency", "operator_use_authorized",
                "authorization_version", "authorization_digest", "authorization_json", "attestation_digest",
                "attestation_json", "health_json", "status", "preflight_status",
                "preflight_json", "chutes_validator_hotkey", "chutes_server_name",
                "chutes_server_id", "chutes_status", "emissions_eligible",
                "admin_note", "created_at_iso", "updated_at_iso", "last_heartbeat_iso")))
        _insert_event(conn, capacity_id, actor, event_type, row)

    store.write(_do)
    return row


def update_capacity_admin(
    store: Store,
    capacity_id: str,
    body: dict[str, Any],
    *,
    preserve_embedded_review: bool = False,
) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM tee_gpu_capacity WHERE capacity_id=?", (capacity_id,))
    if not rows:
        return None
    body = dict(body)
    has_explicit_review = "attestation_review" in body
    if has_explicit_review:
        body["attestation_json"] = _attestation_review_json(
            body.get("attestation_json", rows[0]["attestation_json"]),
            body["attestation_review"],
            reviewed_by="admin",
        )
        summary = evidence_summary(body["attestation_json"])
        if summary["status"] == "operator_reviewed":
            request_id = _evidence_request_id(_loads(body["attestation_json"]))
            if not _evidence_request_exists(store, capacity_id, request_id):
                raise HTTPException(400, "evidence_request_not_found")
    updates: dict[str, Any] = {}
    allowed = {
        "provider_ref", "region", "endpoint_url", "agent_api", "public_ip",
        "gpu_model", "gpu_memory_gb", "tee_kind", "tdx_claimed", "gpu_cc_claimed",
        "hourly_cost", "currency", "operator_use_authorized", "status", "admin_note", "health_json",
        "last_heartbeat_iso", "attestation_json", "chutes_validator_hotkey",
        "chutes_server_name", "chutes_server_id", "chutes_status", "authorization_json",
    }
    for key in allowed:
        if key in body:
            val = body[key]
            if key in {"tdx_claimed", "gpu_cc_claimed"}:
                val = 1 if _boolish(val) else 0
            elif key == "operator_use_authorized":
                val = 1 if _boolish(val) else 0
            elif key == "gpu_memory_gb":
                val = _optional_int(val)
            elif key == "hourly_cost":
                if not _is_finite_float(val):
                    raise HTTPException(400, "invalid_hourly_cost")
                val = _float(val, default=0.0)
            elif key == "attestation_json":
                val = _attestation_input_json(
                    val,
                    allow_review=has_explicit_review or preserve_embedded_review,
                )
            elif key in {"health_json", "authorization_json"}:
                val = _json_blob(val)
            elif key == "status":
                val = str(val).lower()
                if val not in _STATUS:
                    raise HTTPException(400, "invalid_status")
            updates[key] = val
    if "attestation_json" in updates:
        updates["attestation_digest"] = (
            _digest_text(updates["attestation_json"])
            if updates["attestation_json"] != "{}" else "")
    if "operator_use_authorized" in updates or "authorization_json" in updates:
        accepted = bool(int(updates.get("operator_use_authorized", rows[0]["operator_use_authorized"])))
        updates["authorization_version"] = AUTHORIZATION_VERSION if accepted else ""
        if "authorization_json" not in updates:
            updates["authorization_json"] = _authorization_json(
                body, owner_hotkey=rows[0]["owner_hotkey"], node_id=rows[0]["node_id"],
                accepted=accepted, source="admin_update", accepted_at_iso=_now_iso())
        updates["authorization_digest"] = _digest_text(updates["authorization_json"]) if accepted else ""

    current = _row_to_dict(rows[0])
    merged = {**current, **updates}
    material_changed = any(
        key in updates and merged[key] != current[key]
        for key in _MINER_REVIEW_FIELDS
    )
    if material_changed:
        if current["status"] in _REVIEWED_REOPEN_STATUS and "status" not in updates:
            updates["status"] = "pending"
            merged["status"] = "pending"
        if current["chutes_status"] in {"listed", "needs_relisting"}:
            updates["chutes_status"] = "needs_relisting"
            merged["chutes_status"] = "needs_relisting"
        elif current["status"] in _REVIEWED_REOPEN_STATUS:
            updates["chutes_status"] = "needs_review"
            merged["chutes_status"] = "needs_review"
    evidence = evidence_summary(merged.get("attestation_json", "{}"))
    preflight = preflight_capacity(
        gpu_short_ref=str(merged["gpu_short_ref"]), gpu_count=int(merged["gpu_count"]),
        hourly_cost=float(merged["hourly_cost"]), agent_api=str(merged["agent_api"]),
        tee_kind=str(merged["tee_kind"]), tdx_claimed=bool(int(merged["tdx_claimed"])),
        gpu_cc_claimed=bool(int(merged["gpu_cc_claimed"])),
        operator_use_authorized=bool(int(merged["operator_use_authorized"])),
        has_attestation=bool(merged.get("attestation_digest")),
        evidence_status=evidence["status"],
        evidence_acceptable=bool(evidence["acceptable"]),
    )
    updates["preflight_status"] = preflight["status"]
    updates["preflight_json"] = _json_blob(preflight)
    if updates.get("status") == "active" and preflight["status"] != "eligible":
        raise HTTPException(400, {"detail": "preflight_not_eligible", "preflight": preflight})
    if (
        "status" not in updates
        and str(merged.get("status")) == "active"
        and preflight["status"] != "eligible"
    ):
        updates["status"] = "pending"
    updates["updated_at_iso"] = _now_iso()

    def _do(conn):
        set_clause = ", ".join(f"{k}=?" for k in updates)
        params = tuple(updates.values()) + (capacity_id,)
        conn.execute(f"UPDATE tee_gpu_capacity SET {set_clause} WHERE capacity_id=?", params)
        _insert_event(conn, capacity_id, "admin", "admin_updated", updates)

    store.write(_do)
    return _row_to_dict(store.query("SELECT * FROM tee_gpu_capacity WHERE capacity_id=?", (capacity_id,))[0])


def create_evidence_request(
    store: Store,
    *,
    owner_hotkey: str,
    node_id: str,
    actor: str,
    ttl_secs: int | None = None,
) -> dict[str, Any]:
    ttl = _bounded_evidence_request_ttl(ttl_secs)
    issued = datetime.now(timezone.utc)
    expires = issued + timedelta(seconds=ttl)
    request_id = secrets.token_urlsafe(32)
    capacity_id = _capacity_id(owner_hotkey, node_id)
    binding_input = f"{request_id}:{owner_hotkey}:{node_id}".encode("utf-8")
    payload = {
        "capacity_id": capacity_id,
        "owner_hotkey": owner_hotkey,
        "node_id": node_id,
        "request_id": request_id,
        "issued_at_iso": _format_iso(issued),
        "expires_at_iso": _format_iso(expires),
        "ttl_secs": ttl,
        "evidence_request_binding": {
            "recipe": "sha256(request_id || ':' || owner_hotkey || ':' || node_id)",
            "sha256_hex": hashlib.sha256(binding_input).hexdigest(),
        },
        "status": "issued",
        "note": (
            "operator-review evidence request only; not a single-use verifier nonce "
            "and not cryptographic proof until real TDX/GPU verification exists"
        ),
    }

    def _do(conn):
        _insert_event(conn, capacity_id, actor, "evidence_request_created", payload)

    store.write(_do)
    return payload


def review_capacity_evidence(
    store: Store,
    capacity_id: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    return update_capacity_admin(store, capacity_id, {"attestation_review": body})


def verify_capacity_evidence(
    store: Store,
    capacity_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM tee_gpu_capacity WHERE capacity_id=?", (capacity_id,))
    if not rows:
        return None
    row = _row_to_dict(rows[0])
    attestation = _loads(row["attestation_json"])
    if not isinstance(attestation, dict):
        raise HTTPException(400, "attestation_evidence_missing")
    if not _submitted_evidence_fields(attestation):
        raise HTTPException(400, "attestation_evidence_missing")
    request_id = _evidence_request_id(attestation)
    if not request_id:
        raise HTTPException(400, "evidence_request_missing")
    request_payload = _evidence_request_payload(store, capacity_id, request_id)
    if request_payload is None:
        raise HTTPException(400, "evidence_request_not_found")
    _require_fresh_evidence_request(request_payload)
    try:
        verifier_result = _run_evidence_verifier(
            attestation,
            request_payload,
            row,
            operator_body=body or {},
        )
    except HTTPException as e:
        _record_capacity_event(
            store,
            capacity_id,
            "admin",
            "attestation_verification_failed",
            _verification_error_payload(e),
        )
        raise
    _record_capacity_event(
        store,
        capacity_id,
        "admin",
        "attestation_verification_succeeded",
        _verification_success_payload(verifier_result),
    )
    verified_json = _attestation_verified_json(
        attestation,
        verifier_result,
        request_payload=request_payload,
        reviewed_by="admin",
    )
    return update_capacity_admin(
        store,
        capacity_id,
        {"attestation_json": verified_json},
        preserve_embedded_review=True,
    )


def record_provider_status(
    store: Store,
    capacity_id: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    row = _capacity_row(store, capacity_id)
    if row is None:
        return None
    _require_launch_ready_base(row)
    provider_status = _normal_provider_status(_required_str(body, "provider_status"))
    server_id = str(body.get("server_id") or body.get("chutes_server_id") or row["chutes_server_id"] or "").strip()
    server_name = str(body.get("server_name") or body.get("chutes_server_name") or row["chutes_server_name"] or "").strip()
    if not server_id:
        raise HTTPException(400, "provider_server_id_required")
    payload = {
        "provider": str(body.get("provider") or "chutes"),
        "provider_status": provider_status,
        "server_id": server_id,
        "server_name": server_name,
        "observed_at_iso": str(body.get("observed_at_iso") or _now_iso()),
        "source": str(body.get("source") or "operator_import"),
        "receipt_id": str(body.get("receipt_id") or ""),
        "raw_digest": _digest_text(_json_blob(body)),
        "accepted": provider_status in _PROVIDER_ACCEPTED_STATUS,
    }
    if not payload["accepted"]:
        raise HTTPException(400, {
            "detail": "provider_status_not_accepted",
            "accepted_statuses": sorted(_PROVIDER_ACCEPTED_STATUS),
        })
    new_server_name = server_name or row["chutes_server_name"]
    now = _now_iso()

    def _do(conn):
        conn.execute(
            "UPDATE tee_gpu_capacity "
            "SET status=?, chutes_status=?, chutes_server_id=?, "
            "chutes_server_name=?, updated_at_iso=? WHERE capacity_id=?",
            ("active", "listed", server_id, new_server_name, now, capacity_id),
        )
        _insert_event(
            conn,
            capacity_id,
            "admin",
            "provider_status_verified",
            payload,
        )

    store.write(_do)
    return _capacity_row(store, capacity_id)


def record_health_receipt(
    store: Store,
    capacity_id: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    row = _capacity_row(store, capacity_id)
    if row is None:
        return None
    _require_launch_ready_base(row)
    if not capacity_launch_evidence(store, capacity_id)["provider_listing_verified"]:
        raise HTTPException(400, "provider_listing_required")
    ok = _boolish(body.get("ok"))
    if not ok:
        raise HTTPException(400, "health_receipt_not_ok")
    payload = {
        "ok": True,
        "observed_at_iso": str(body.get("observed_at_iso") or _now_iso()),
        "source": str(body.get("source") or "operator_probe"),
        "probe_url": str(body.get("probe_url") or row["agent_api"] or ""),
        "latency_ms": _optional_float(body.get("latency_ms")),
        "response_digest": str(body.get("response_digest") or _digest_text(_json_blob(body))),
        "receipt_id": str(body.get("receipt_id") or ""),
    }
    health = _loads(row["health_json"])
    health = health if isinstance(health, dict) else {}
    health.update({
        "last_ok": True,
        "last_ok_at_iso": payload["observed_at_iso"],
        "last_probe_source": payload["source"],
        "last_response_digest": payload["response_digest"],
    })
    update_capacity_admin(
        store,
        capacity_id,
        {"health_json": health, "last_heartbeat_iso": payload["observed_at_iso"]},
    )
    _record_capacity_event(store, capacity_id, "admin", "health_receipt_verified", payload)
    return _capacity_row(store, capacity_id)


def record_usage_receipt(
    store: Store,
    capacity_id: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    row = _capacity_row(store, capacity_id)
    if row is None:
        return None
    _require_launch_ready_base(row)
    launch = capacity_launch_evidence(store, capacity_id)
    if not launch["provider_listing_verified"]:
        raise HTTPException(400, "provider_listing_required")
    if not launch["health_verified"]:
        raise HTTPException(400, "health_receipt_required")
    receipt_id = _required_str(body, "receipt_id")
    revenue_usd = _optional_float(body.get("revenue_usd"))
    usage_seconds = _optional_float(body.get("usage_seconds"))
    workload_count = _optional_int(body.get("workload_count") or body.get("workloads_completed"))
    if (
        (revenue_usd is None or revenue_usd <= 0.0)
        and (usage_seconds is None or usage_seconds <= 0.0)
        and (workload_count is None or workload_count <= 0)
    ):
        raise HTTPException(400, "usage_or_revenue_required")
    payload = {
        "receipt_id": receipt_id,
        "observed_at_iso": str(body.get("observed_at_iso") or _now_iso()),
        "source": str(body.get("source") or "operator_receipt"),
        "revenue_usd": revenue_usd,
        "usage_seconds": usage_seconds,
        "workload_count": workload_count,
        "currency": str(body.get("currency") or "USD").upper(),
        "raw_digest": _digest_text(_json_blob(body)),
    }
    _record_capacity_event(store, capacity_id, "admin", "usage_receipt_verified", payload)
    return _capacity_row(store, capacity_id)


def capacity_launch_evidence(store: Store, capacity_id: str) -> dict[str, Any]:
    row = _capacity_row(store, capacity_id)
    if row is None:
        return {
            "provider_listing_verified": False,
            "health_verified": False,
            "usage_or_revenue_verified": False,
            "production_compute_ready": False,
        }
    evidence = evidence_summary(row["attestation_json"])
    events = _capacity_events(store, capacity_id)
    provider = _latest_event(events, "provider_status_verified")
    health = _latest_event(events, "health_receipt_verified")
    usage = _latest_event(events, "usage_receipt_verified")
    provider_ok = (
        isinstance(provider, dict)
        and provider.get("provider_status") in _PROVIDER_ACCEPTED_STATUS
        and bool(provider.get("server_id"))
    )
    health_ok = isinstance(health, dict) and bool(health.get("ok"))
    usage_ok = isinstance(usage, dict) and (
        _positive_number(usage.get("revenue_usd"))
        or _positive_number(usage.get("usage_seconds"))
        or _positive_number(usage.get("workload_count"))
    )
    return {
        "cryptographically_verified": evidence["status"] == _EVIDENCE_CRYPTO_STATUS,
        "provider_listing_verified": provider_ok,
        "health_verified": health_ok,
        "usage_or_revenue_verified": usage_ok,
        "production_compute_ready": (
            row["status"] == "active"
            and row["chutes_status"] == "listed"
            and evidence["status"] == _EVIDENCE_CRYPTO_STATUS
            and provider_ok
            and health_ok
            and usage_ok
        ),
        "provider_status": provider or {},
        "health_receipt": health or {},
        "usage_receipt": usage or {},
    }


def _evidence_request_exists(store: Store, capacity_id: str, request_id: str) -> bool:
    return _evidence_request_payload(store, capacity_id, request_id) is not None


def _evidence_request_payload(
    store: Store,
    capacity_id: str,
    request_id: str,
) -> dict[str, Any] | None:
    if not request_id:
        return None
    rows = store.query(
        "SELECT event_json FROM tee_gpu_capacity_events "
        "WHERE capacity_id=? AND event_type='evidence_request_created' "
        "ORDER BY created_at_iso DESC",
        (capacity_id,),
    )
    for row in rows:
        payload = _loads(row["event_json"])
        if isinstance(payload, dict) and payload.get("request_id") == request_id:
            return payload
    return None


def _require_fresh_evidence_request(payload: dict[str, Any]) -> None:
    expires_at = str(payload.get("expires_at_iso") or "")
    expires_ts = _parse_iso(expires_at)
    if expires_ts is None:
        raise HTTPException(400, "evidence_request_invalid_expiry")
    if datetime.now(timezone.utc).timestamp() > expires_ts:
        raise HTTPException(400, "evidence_request_expired")


def _run_evidence_verifier(
    attestation: dict[str, Any],
    request_payload: dict[str, Any],
    capacity_row: dict[str, Any],
    *,
    operator_body: dict[str, Any],
) -> dict[str, Any]:
    command = os.environ.get("CATHEDRAL_TEE_GPU_VERIFY_CMD", "").strip()
    if not command:
        raise HTTPException(503, "tee_gpu_evidence_verifier_not_configured")
    timeout = _bounded_verify_timeout(_optional_int(operator_body.get("timeout_secs")))
    with tempfile.TemporaryDirectory(prefix="cathedral-tee-gpu-") as tmp:
        evidence_path = os.path.join(tmp, "evidence.json")
        request_path = os.path.join(tmp, "request.json")
        capacity_path = os.path.join(tmp, "capacity.json")
        result_path = os.path.join(tmp, "result.json")
        with open(evidence_path, "w", encoding="utf-8") as f:
            json.dump(attestation, f, sort_keys=True, separators=(",", ":"))
        with open(request_path, "w", encoding="utf-8") as f:
            json.dump(request_payload, f, sort_keys=True, separators=(",", ":"))
        with open(capacity_path, "w", encoding="utf-8") as f:
            json.dump(_verifier_capacity_context(capacity_row), f, sort_keys=True, separators=(",", ":"))

        args = _evidence_verifier_args(
            command,
            evidence_path=evidence_path,
            request_path=request_path,
            capacity_path=capacity_path,
            result_path=result_path,
        )
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise HTTPException(503, {
                "detail": "tee_gpu_evidence_verifier_not_found",
                "binary": e.filename,
            })
        except OSError as e:
            raise HTTPException(503, {
                "detail": "tee_gpu_evidence_verifier_error",
                "error": str(e),
            })
        except subprocess.TimeoutExpired as e:
            raise HTTPException(504, {
                "detail": "tee_gpu_evidence_verifier_timeout",
                "timeout_secs": timeout,
                "stdout": _tail(e.stdout),
                "stderr": _tail(e.stderr),
            })

        result = _load_verifier_result(result_path, proc.stdout)
        result.setdefault("returncode", proc.returncode)
        result.setdefault("stdout_tail", _tail(proc.stdout, limit=1200))
        result.setdefault("stderr_tail", _tail(proc.stderr, limit=1200))
        if proc.returncode != 0:
            raise HTTPException(400, {
                "detail": "tee_gpu_evidence_verifier_failed",
                "result": _public_verifier_summary(result),
            })
        if not _verifier_result_ok(result):
            raise HTTPException(400, {
                "detail": "tee_gpu_evidence_not_verified",
                "result": _public_verifier_summary(result),
            })
        missing = _missing_required_verifier_checks(result)
        if missing:
            raise HTTPException(400, {
                "detail": "tee_gpu_evidence_verifier_missing_required_checks",
                "missing": missing,
                "result": _public_verifier_summary(result),
            })
        return result


def _evidence_verifier_args(
    command: str,
    *,
    evidence_path: str,
    request_path: str,
    capacity_path: str,
    result_path: str,
) -> list[str]:
    mapping = {
        "evidence_path": evidence_path,
        "request_path": request_path,
        "capacity_path": capacity_path,
        "result_path": result_path,
    }
    if any("{" + key + "}" in command for key in mapping):
        try:
            rendered = command.format(**mapping)
            return shlex.split(rendered, posix=os.name != "nt")
        except (IndexError, KeyError, ValueError) as e:
            raise HTTPException(400, {
                "detail": "tee_gpu_evidence_verifier_command_invalid",
                "error": str(e),
            })
    return shlex.split(command, posix=os.name != "nt") + [
        evidence_path,
        request_path,
        capacity_path,
        result_path,
    ]


def _load_verifier_result(result_path: str, stdout: str) -> dict[str, Any]:
    raw = ""
    if os.path.exists(result_path):
        with open(result_path, "r", encoding="utf-8") as f:
            raw = f.read()
    if not raw.strip():
        raw = stdout or "{}"
    result = _loads(raw)
    if not isinstance(result, dict):
        raise HTTPException(400, "tee_gpu_evidence_verifier_result_not_json_object")
    return result


def _verifier_result_ok(result: dict[str, Any]) -> bool:
    return _boolish(result.get("ok")) or _boolish(result.get("verified"))


def _missing_required_verifier_checks(result: dict[str, Any]) -> list[str]:
    required = {
        "tdx_verified": ("tdx_verified", "tdx_quote_verified"),
        "gpu_verified": ("gpu_verified", "gpu_attestation_verified"),
        "report_data_match": ("report_data_match", "nonce_bound", "binding_verified"),
        "debug_disabled": ("debug_disabled", "tdx_debug_disabled"),
    }
    missing = []
    for label, aliases in required.items():
        if not any(_boolish(result.get(alias)) for alias in aliases):
            missing.append(label)
    gpu_claims_match = any(
        _boolish(result.get(alias))
        for alias in (
            "gpu_claims_match",
            "gpu_model_count_match",
            "capacity_gpu_match",
            "claimed_gpu_match",
        )
    ) or (
        _boolish(result.get("gpu_model_match"))
        and _boolish(result.get("gpu_count_match"))
    )
    if not gpu_claims_match:
        missing.append("gpu_claims_match")
    return missing


def _public_verifier_summary(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "ok", "verified", "verifier", "version", "policy", "reason", "proof",
        "tdx_verified", "tdx_quote_verified", "gpu_verified",
        "gpu_attestation_verified", "report_data_match", "nonce_bound",
        "binding_verified", "debug_disabled", "tdx_debug_disabled",
        "gpu_claims_match", "gpu_model_count_match", "capacity_gpu_match",
        "claimed_gpu_match", "gpu_model_match", "gpu_count_match",
        "gpu_model", "gpu_count", "measurement", "mrtd_hex", "returncode",
        "stdout_tail", "stderr_tail",
    }
    return {key: result[key] for key in sorted(allowed) if key in result}


def _verification_success_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = _public_verifier_summary(result)
    payload["verifier_command_digest"] = _verifier_command_digest()
    return payload


def _verification_error_payload(error: HTTPException) -> dict[str, Any]:
    detail = getattr(error, "detail", "")
    if isinstance(detail, dict):
        public = dict(detail)
        reason = str(public.get("detail") or "")
    else:
        public = {"detail": str(detail)}
        reason = str(detail)
    negative_control_reasons = {
        "tee_gpu_evidence_verifier_failed",
        "tee_gpu_evidence_not_verified",
        "tee_gpu_evidence_verifier_missing_required_checks",
    }
    return {
        "status_code": int(getattr(error, "status_code", 400)),
        "detail": reason,
        "result": public.get("result", {}),
        "missing": public.get("missing", []),
        "counts_as_bad_evidence_rejection": reason in negative_control_reasons,
        "verifier_command_digest": _verifier_command_digest(),
    }


def _verifier_command_digest() -> str:
    command = os.environ.get("CATHEDRAL_TEE_GPU_VERIFY_CMD", "").strip()
    return _digest_text(command) if command else ""


def _verifier_capacity_context(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "capacity_id": row["capacity_id"],
        "owner_hotkey": row["owner_hotkey"],
        "node_id": row["node_id"],
        "gpu_short_ref": row["gpu_short_ref"],
        "gpu_count": row["gpu_count"],
        "gpu_model": row["gpu_model"],
        "tee_kind": row["tee_kind"],
        "tdx_claimed": bool(int(row["tdx_claimed"])),
        "gpu_cc_claimed": bool(int(row["gpu_cc_claimed"])),
        "agent_api": row["agent_api"],
    }


def _require_launch_ready_base(row: dict[str, Any]) -> None:
    evidence = evidence_summary(row["attestation_json"])
    if evidence["status"] != _EVIDENCE_CRYPTO_STATUS:
        raise HTTPException(400, "cryptographic_attestation_required")
    if row["preflight_status"] != "eligible":
        raise HTTPException(400, {
            "detail": "preflight_not_eligible",
            "preflight": _loads(row["preflight_json"]),
        })
    if not bool(int(row["operator_use_authorized"])):
        raise HTTPException(400, "operator_use_not_authorized")


def _normal_provider_status(raw: str) -> str:
    status = raw.strip().lower().replace("-", "_")
    aliases = {
        "ok": "listed",
        "up": "running",
        "healthy": "running",
        "available": "ready",
        "provisioned": "accepted",
        "registered": "listed",
    }
    return aliases.get(status, status)


def list_capacity(
    store: Store,
    *,
    status: str | None = None,
    owner_hotkey: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if owner_hotkey:
        clauses.append("owner_hotkey=?")
        params.append(owner_hotkey)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = store.query(
        "SELECT * FROM tee_gpu_capacity" + where + " ORDER BY updated_at_iso DESC, capacity_id DESC",
        tuple(params))
    return [_row_to_dict(r) for r in rows]


def capacity_metrics(store: Store) -> dict[str, Any]:
    rows = list_capacity(store)
    by_status: dict[str, int] = {}
    admin_active_candidate_gpus = 0
    admin_active_candidate_hourly_cost = 0.0
    production_ready_gpus = 0
    production_ready_hourly_cost = 0.0
    provider_verified = 0
    health_verified = 0
    revenue_verified = 0
    production_ready = 0
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == "active":
            gpu_count = int(r["gpu_count"])
            admin_active_candidate_gpus += gpu_count
            admin_active_candidate_hourly_cost += float(r["hourly_cost"]) * gpu_count
        launch = capacity_launch_evidence(store, r["capacity_id"])
        provider_verified += 1 if launch["provider_listing_verified"] else 0
        health_verified += 1 if launch["health_verified"] else 0
        revenue_verified += 1 if launch["usage_or_revenue_verified"] else 0
        if r["status"] == "active" and launch["production_compute_ready"]:
            production_ready += 1
            gpu_count = int(r["gpu_count"])
            production_ready_gpus += gpu_count
            production_ready_hourly_cost += float(r["hourly_cost"]) * gpu_count
    return {
        "enabled": tee_gpu_enabled(),
        "count": len(rows),
        "by_status": by_status,
        "active_gpus": production_ready_gpus,
        "active_listed_hourly_cost": round(production_ready_hourly_cost, 6),
        "admin_active_candidate_gpus": admin_active_candidate_gpus,
        "admin_active_candidate_hourly_cost": round(admin_active_candidate_hourly_cost, 6),
        "production_ready_gpus": production_ready_gpus,
        "production_ready_hourly_cost": round(production_ready_hourly_cost, 6),
        "provider_verified": provider_verified,
        "health_verified": health_verified,
        "usage_or_revenue_verified": revenue_verified,
        "production_ready": production_ready,
        "emissions_eligible": False,
        "intake_gate": intake_gate_status(),
    }


def tee_gpu_dashboard_html(store: Store) -> str:
    rows = list_capacity(store)
    metrics = capacity_metrics(store)
    by_status = metrics.get("by_status", {})
    total_rows = len(rows)
    eligible = sum(1 for row in rows if row["preflight_status"] == "eligible")
    authorized = sum(1 for row in rows if bool(int(row["operator_use_authorized"])))
    ready = sum(1 for row in rows if chutes_manifest_item(row)["ready"])
    rows_html = "\n".join(
        _dashboard_row(row, capacity_launch_evidence(store, row["capacity_id"]))
        for row in rows
    ) or (
        "<tr><td colspan=11 class=empty>No capacity offers yet.</td></tr>"
    )
    return f"""<!doctype html>
<html lang=en>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta http-equiv=refresh content=15>
<title>Cathedral Compute Supply</title>
<style>
  :root {{
    --ink:#101318; --muted:#626b77; --line:#dfe4ea; --panel:#ffffff;
    --bg:#f5f7fa; --good:#0f8f5f; --warn:#a66300; --bad:#b42318;
    --blue:#275d8f; --gold:#8c6d1f;
  }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif; }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:22px; }}
  header {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:16px;
    align-items:end; padding:4px 0 18px; border-bottom:1px solid var(--line); }}
  h1 {{ margin:0; font-size:24px; letter-spacing:0; }}
  .sub {{ color:var(--muted); margin:6px 0 0; max-width:720px; }}
  .route {{ text-align:right; color:var(--muted); font-size:12px; }}
  .route b {{ color:var(--ink); }}
  .steps {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:18px 0; }}
  .step,.metric,.tablewrap {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
  .step {{ padding:12px 13px; min-height:92px; }}
  .step b {{ display:block; font-size:13px; margin-bottom:5px; }}
  .step span {{ color:var(--muted); font-size:12px; }}
  .metrics {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; margin:0 0 18px; }}
  .metric {{ padding:13px 14px; }}
  .metric .n {{ font-size:24px; font-weight:760; }}
  .metric .l {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em; }}
  .tablewrap {{ overflow:hidden; }}
  .tablehead {{ display:flex; justify-content:space-between; gap:10px; padding:14px 16px; border-bottom:1px solid var(--line); }}
  h2 {{ margin:0; font-size:15px; }}
  .hint {{ color:var(--muted); font-size:12px; }}
  table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
  th,td {{ padding:11px 12px; border-bottom:1px solid var(--line); vertical-align:top; text-align:left; }}
  th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em; background:#fafbfc; }}
  td {{ font-size:13px; }}
  .node {{ font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .meta,.small {{ color:var(--muted); font-size:12px; }}
  .pill {{ display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; font-size:12px; }}
  .ok {{ color:var(--good); border-color:#b7e2cf; background:#edf8f3; }}
  .warn {{ color:var(--warn); border-color:#f0d3a2; background:#fff7e8; }}
  .bad {{ color:var(--bad); border-color:#f3b8b3; background:#fff0ee; }}
  .code {{ margin-top:6px; font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
    color:#d8dee8; background:#111821; border-radius:6px; padding:7px 8px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .empty {{ color:var(--muted); text-align:center; padding:28px; }}
  @media (max-width:900px) {{
    header {{ grid-template-columns:1fr; }}
    .route {{ text-align:left; }}
    .steps,.metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
    table {{ min-width:900px; }}
    .tablewrap {{ overflow-x:auto; }}
  }}
  @media (max-width:560px) {{
    .wrap {{ padding:16px; }}
    .steps,.metrics {{ grid-template-columns:1fr; }}
  }}
</style>
<div class=wrap>
  <header>
    <div>
      <h1>List compute with Cathedral.</h1>
      <p class=sub>Miners offer authorized TEE GPU capacity. Cathedral preflights it, verifies cryptographic evidence, and only then emits operator-controlled provider handoff commands.</p>
    </div>
    <div class=route><b>Path:</b> offer -> review -> Chutes handoff -> revenue ops<br>default-off, publisher-only, no emissions writes</div>
  </header>
  <section class=steps>
    <div class=step><b>1. Miner signs offer</b><span>Hotkey-signed inventory plus explicit permission to use the machine for secure compute and mining workloads.</span></div>
    <div class=step><b>2. Cathedral preflights</b><span>Only current Chutes TEE measurement profiles pass: 8x H200, Pro 6000, B200, or B300.</span></div>
    <div class=step><b>3. Operator verifies</b><span>Set server name, health, notes, and require cryptographic TEE/GPU evidence before handoff.</span></div>
    <div class=step><b>4. Cathedral lists it</b><span>Run the audited Chutes handoff from the control plane; blocked, non-consented, or unverified rows never emit a command.</span></div>
  </section>
  <section class=metrics>
    <div class=metric><div class=n>{total_rows}</div><div class=l>offers</div></div>
    <div class=metric><div class=n>{metrics["production_ready_gpus"]}</div><div class=l>ready GPUs</div></div>
    <div class=metric><div class=n>${metrics["production_ready_hourly_cost"]:.2f}</div><div class=l>ready $/hour</div></div>
    <div class=metric><div class=n>{eligible}/{total_rows}</div><div class=l>preflight eligible</div></div>
    <div class=metric><div class=n>{ready}/{authorized}</div><div class=l>ready / authorized</div></div>
  </section>
  <section class=tablewrap>
    <div class=tablehead>
      <h2>Compute supply</h2>
      <div class=hint>Active {int(by_status.get("active", 0))} | Pending {int(by_status.get("pending", 0))} | Rejected {int(by_status.get("rejected", 0))} | Retired {int(by_status.get("retired", 0))}</div>
    </div>
    <table>
      <thead><tr><th>Machine</th><th>GPU</th><th>Status</th><th>Preflight</th><th>Authorization</th><th>Provider</th><th>Health</th><th>Usage</th><th>Ready</th><th>Handoff</th><th>Operator note</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </section>
</div>
</html>"""


def _dashboard_row(row: dict[str, Any], launch: dict[str, Any]) -> str:
    item = chutes_manifest_item(row)
    preflight = _loads(row["preflight_json"])
    reasons = preflight.get("reasons") or []
    warnings = preflight.get("warnings") or []
    status = str(row["status"])
    status_cls = "ok" if status == "active" else ("bad" if status in {"rejected", "retired"} else "warn")
    preflight_cls = "ok" if row["preflight_status"] == "eligible" else "bad"
    auth_ok = bool(int(row["operator_use_authorized"]))
    auth_cls = "ok" if auth_ok else "bad"
    provider_ok = bool(launch.get("provider_listing_verified"))
    health_ok = bool(launch.get("health_verified"))
    usage_ok = bool(launch.get("usage_or_revenue_verified"))
    prod_ok = bool(launch.get("production_compute_ready"))
    handoff_cls = "ok" if item["ready"] else "warn"
    handoff_text = "ready for Chutes" if item["ready"] else "needs " + ", ".join(item["missing"])
    cmd = f"<div class=code>{_h(item['command'])}</div>" if item["command"] else ""
    reason_text = ", ".join(str(x) for x in reasons) if reasons else "eligible"
    if warnings:
        reason_text += " | warnings: " + ", ".join(str(x) for x in warnings)
    hourly_total = float(row["hourly_cost"]) * int(row["gpu_count"])
    return (
        "<tr>"
        f"<td><div class=node>{_h(row['node_id'])}</div><div class=meta>{_h(row['owner_hotkey'])}</div></td>"
        f"<td><b>{_h(row['gpu_short_ref'])}</b> x {int(row['gpu_count'])}<div class=small>${float(row['hourly_cost']):.2f}/GPU/hr | ${hourly_total:.2f}/hr</div></td>"
        f"<td><span class='pill {status_cls}'>{_h(status)}</span></td>"
        f"<td><span class='pill {preflight_cls}'>{_h(row['preflight_status'])}</span><div class=small>{_h(reason_text)}</div></td>"
        f"<td><span class='pill {auth_cls}'>{'authorized' if auth_ok else 'not authorized'}</span></td>"
        f"<td><span class='pill {'ok' if provider_ok else 'warn'}'>{'verified' if provider_ok else 'missing'}</span></td>"
        f"<td><span class='pill {'ok' if health_ok else 'warn'}'>{'verified' if health_ok else 'missing'}</span></td>"
        f"<td><span class='pill {'ok' if usage_ok else 'warn'}'>{'verified' if usage_ok else 'missing'}</span></td>"
        f"<td><span class='pill {'ok' if prod_ok else 'warn'}'>{'ready' if prod_ok else 'not ready'}</span></td>"
        f"<td><span class='pill {handoff_cls}'>{_h(handoff_text)}</span>{cmd}</td>"
        f"<td>{_h(row['admin_note'])}<div class=small>{_h(row['region'])}</div></td>"
        "</tr>"
    )


def preflight_capacity(
    *,
    gpu_short_ref: str,
    gpu_count: int,
    hourly_cost: float,
    agent_api: str,
    tee_kind: str,
    tdx_claimed: bool,
    gpu_cc_claimed: bool,
    operator_use_authorized: bool,
    has_attestation: bool,
    evidence_status: str | None = None,
    evidence_acceptable: bool | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    is_tpu = gpu_short_ref in _EXPLORATORY_TPU_REFS or gpu_short_ref.startswith("tpu_")
    if is_tpu:
        warnings.append("google_tpu_exploratory_intake_only")
        warnings.append("not_chutes_listable")
        warnings.append("not_emissions_eligible")
    elif gpu_short_ref not in _CHUTES_SUPPORTED:
        reasons.append("gpu_short_ref_not_chutes_supported")
    elif gpu_short_ref not in _TEE_CANDIDATES:
        reasons.append("gpu_short_ref_not_tee_candidate")
    elif gpu_count not in _TEE_MEASUREMENT_GPU_COUNTS[gpu_short_ref]:
        reasons.append("gpu_count_not_tee_measurement_profile")
    if gpu_count < 1 or gpu_count > 10:
        reasons.append("gpu_count_out_of_range")
    if not math.isfinite(hourly_cost):
        reasons.append("hourly_cost_not_finite")
    elif hourly_cost < 0:
        reasons.append("hourly_cost_negative")
    if not agent_api.startswith(("http://", "https://")):
        reasons.append("agent_api_not_http")
    elif ":32000" not in agent_api and not is_tpu:
        warnings.append("agent_api_not_default_32000")
    if not is_tpu and not tdx_claimed:
        reasons.append("intel_tdx_not_claimed")
    if not is_tpu and not gpu_cc_claimed:
        reasons.append("nvidia_gpu_cc_not_claimed")
    if not operator_use_authorized:
        reasons.append("operator_use_not_authorized")
    if not is_tpu and tee_kind and "tdx" not in tee_kind.lower():
        reasons.append("tee_kind_not_intel_tdx")
    if evidence_status == "rejected":
        reasons.append("attestation_evidence_rejected")
    require_crypto = _truthy(os.environ.get("CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE", ""))
    if require_crypto and not is_tpu:
        if not has_attestation:
            reasons.append("attestation_evidence_required")
        elif evidence_status != _EVIDENCE_CRYPTO_STATUS:
            reasons.append("cryptographic_attestation_required")
    elif _truthy(os.environ.get("CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE", "")) and not is_tpu:
        if not has_attestation:
            reasons.append("attestation_evidence_required")
        elif not evidence_acceptable:
            reasons.append("attestation_evidence_review_required")
    return {
        "status": "exploratory" if is_tpu and not reasons else ("eligible" if not reasons else "blocked"),
        "reasons": reasons,
        "warnings": warnings,
        "capacity_kind": "google_tpu" if is_tpu else "tee_gpu",
        "evidence_status": evidence_status or ("submitted" if has_attestation else "missing"),
        "evidence_acceptable": bool(evidence_acceptable),
        "checked_at": _now_iso(),
        "note": (
            "google_tpu_exploratory_inactive_for_scoring"
            if is_tpu else
            "cryptographic_tdx_gpu_attestation_required"
            if require_crypto else "operator_reviewed_evidence_only_not_cryptographic_attestation"
        ),
    }


def admin_record(row: dict[str, Any], *, store: Store | None = None) -> dict[str, Any]:
    out = dict(row)
    capacity_id = row["capacity_id"]
    out["preflight"] = _loads(out.pop("preflight_json", "{}"))
    out["attestation"] = _loads(out.pop("attestation_json", "{}"))
    out["evidence"] = evidence_summary(out["attestation"])
    out["authorization"] = _loads(out.pop("authorization_json", "{}"))
    out["health"] = _loads(out.pop("health_json", "{}"))
    out["emissions_eligible"] = False
    out["chutes"] = chutes_manifest_item(row)
    out["launch_evidence"] = capacity_launch_evidence(store, capacity_id) if store else {}
    return out


def miner_record(row: dict[str, Any]) -> dict[str, Any]:
    out = public_record(row)
    out.update({
        "node_id": row["node_id"],
        "status": row["status"],
        "preflight": _loads(row["preflight_json"]),
        "operator_use_authorized": bool(int(row["operator_use_authorized"])),
        "authorization_version": row["authorization_version"],
        "evidence": evidence_summary(row["attestation_json"]),
        "updated_at_iso": row["updated_at_iso"],
    })
    return out


def public_record(row: dict[str, Any]) -> dict[str, Any]:
    preflight = _loads(row["preflight_json"])
    return {
        "capacity_id": row["capacity_id"],
        "capacity_kind": preflight.get("capacity_kind") or "tee_gpu",
        "gpu_short_ref": row["gpu_short_ref"],
        "gpu_model": row["gpu_model"],
        "gpu_count": row["gpu_count"],
        "gpu_memory_gb": row["gpu_memory_gb"],
        "region": row["region"],
        "provider_ref": row["provider_ref"],
        "tee_kind": row["tee_kind"],
        "operator_use_authorized": bool(int(row["operator_use_authorized"])),
        "preflight_status": row["preflight_status"],
        "evidence_status": evidence_summary(row["attestation_json"])["status"],
        "status": row["status"],
        "emissions_eligible": False,
    }


def chutes_manifest_item(row: dict[str, Any]) -> dict[str, Any]:
    validator = row["chutes_validator_hotkey"] or _DEFAULT_CHUTES_VALIDATOR
    miner_api = os.environ.get("CATHEDRAL_TEE_GPU_CHUTES_MINER_API", "http://127.0.0.1:32000")
    hotkey_path = os.environ.get("CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH", "").strip()
    cli = os.environ.get("CATHEDRAL_TEE_GPU_CHUTES_CLI", "chutes-miner").strip() or "chutes-miner"
    name = str(row["chutes_server_name"] or "").strip()
    agent_api = str(row["agent_api"] or "").strip()
    preflight = _loads(row["preflight_json"])
    missing = []
    if preflight.get("capacity_kind") == "google_tpu":
        missing.append("google_tpu_not_chutes_listable")
    if not name:
        missing.append("chutes_server_name")
    if not agent_api:
        missing.append("agent_api")
    if not hotkey_path:
        missing.append("CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH")
    if not bool(int(row["operator_use_authorized"])):
        missing.append("operator_use_not_authorized")
    if row["preflight_status"] != "eligible":
        missing.append("preflight_not_eligible")
    evidence = evidence_summary(row["attestation_json"])
    if evidence["status"] != _EVIDENCE_CRYPTO_STATUS:
        missing.append("cryptographic_attestation_required")
    if row["status"] not in _LISTABLE_STATUS:
        missing.append("status_not_listable")
    ready = not missing
    args = []
    if ready:
        args = [
            cli, "add-node",
            "--name", name,
            "--validator", validator,
            "--hourly-cost", str(row["hourly_cost"]),
            "--gpu-short-ref", row["gpu_short_ref"],
            "--hotkey", hotkey_path,
            "--agent-api", agent_api,
            "--miner-api", miner_api,
        ]
    payload = {
        "name": name,
        "validator": validator,
        "hourly_cost": row["hourly_cost"],
        "gpu_short_ref": row["gpu_short_ref"],
        "agent_api": agent_api,
        "hotkey": hotkey_path,
        "miner_api": miner_api,
    }
    return {
        "capacity_id": row["capacity_id"],
        "owner_hotkey": row["owner_hotkey"],
        "server_name": name,
        "ready": ready,
        "missing": missing,
        "payload": payload,
        "command_args": args,
        "command": " ".join(shlex.quote(str(x)) for x in args) if ready else None,
        "note": (
            "set missing operator fields before running chutes-miner add-node"
            if missing else "run from the Chutes miner control plane after k3s/agent setup"),
    }


def list_capacity_on_chutes(
    store: Store,
    capacity_id: str,
    *,
    execute: bool = False,
    timeout_secs: int | None = None,
) -> dict[str, Any]:
    rows = store.query("SELECT * FROM tee_gpu_capacity WHERE capacity_id=?", (capacity_id,))
    if not rows:
        raise HTTPException(404, "capacity_not_found")
    row = _row_to_dict(rows[0])
    item = chutes_manifest_item(row)
    blockers = list(item["missing"])

    def _block(reason: str) -> None:
        if reason not in blockers:
            blockers.append(reason)

    if not bool(int(row["operator_use_authorized"])):
        _block("operator_use_not_authorized")
    if row["preflight_status"] != "eligible":
        _block("preflight_not_eligible")
    if row["status"] not in _LISTABLE_STATUS:
        _block("status_not_listable")
    if blockers:
        raise HTTPException(400, {
            "detail": "chutes_listing_not_ready",
            "blockers": blockers,
            "manifest": item,
        })

    if execute and row["chutes_status"] == "listed":
        return {
            "capacity_id": capacity_id,
            "execute_requested": True,
            "executed": False,
            "ready": True,
            "command_args": item["command_args"],
            "command": item["command"],
            "status": "already_listed",
        }

    if execute and not _truthy(os.environ.get("CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED", "")):
        raise HTTPException(403, {
            "detail": "chutes_execution_disabled",
            "required_env": "CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED=1",
            "manifest": item,
        })

    result = {
        "capacity_id": capacity_id,
        "execute_requested": execute,
        "executed": False,
        "ready": True,
        "command_args": item["command_args"],
        "command": item["command"],
        "status": "dry_run",
    }
    if not execute:
        _record_chutes_listing(store, capacity_id, "chutes_list_dry_run", result)
        return result

    claim = _claim_chutes_listing(store, capacity_id, result)
    if claim == "already_listed":
        result.update({"executed": False, "status": "already_listed"})
        return result
    if claim == "listing_in_progress":
        raise HTTPException(409, "chutes_listing_in_progress")

    timeout = _bounded_timeout(timeout_secs)
    try:
        proc = subprocess.run(
            [str(x) for x in item["command_args"]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        result.update({
            "executed": True,
            "returncode": proc.returncode,
            "stdout": _tail(proc.stdout),
            "stderr": _tail(proc.stderr),
            "status": "listed" if proc.returncode == 0 else "list_failed",
        })
    except FileNotFoundError as e:
        result.update({
            "executed": False,
            "returncode": None,
            "error": f"cli_not_found: {e.filename}",
            "status": "list_failed",
        })
    except OSError as e:
        result.update({
            "executed": False,
            "returncode": None,
            "error": f"cli_error: {e}",
            "status": "list_failed",
        })
    except subprocess.TimeoutExpired as e:
        result.update({
            "executed": True,
            "returncode": None,
            "stdout": _tail(e.stdout),
            "stderr": _tail(e.stderr),
            "error": f"timeout_after_{timeout}s",
            "status": "list_failed",
        })

    event_type = "chutes_list_succeeded" if result["status"] == "listed" else "chutes_list_failed"

    def _do(conn):
        new_status = "active" if result["status"] == "listed" else row["status"]
        conn.execute(
            "UPDATE tee_gpu_capacity "
            "SET status=?, chutes_status=?, updated_at_iso=? WHERE capacity_id=?",
            (new_status, result["status"], _now_iso(), capacity_id))
        _insert_event(conn, capacity_id, "admin", event_type, result)

    store.write(_do)
    return result


def _claim_chutes_listing(store: Store, capacity_id: str, payload: dict[str, Any]) -> str:
    def _do(conn):
        row = conn.execute(
            "SELECT chutes_status, updated_at_iso FROM tee_gpu_capacity WHERE capacity_id=?",
            (capacity_id,),
        ).fetchone()
        if row is None:
            return "missing"
        if hasattr(row, "keys"):
            status = str(row["chutes_status"] or "")
            updated_at_iso = str(row["updated_at_iso"] or "")
        else:
            status = str(row[0] or "")
            updated_at_iso = str(row[1] or "")
        if status == "listed":
            return "already_listed"
        stale_listing = status == "listing" and _listing_claim_is_stale(updated_at_iso)
        if status == "listing" and not stale_listing:
            return "listing_in_progress"
        cur = conn.execute(
            "UPDATE tee_gpu_capacity SET chutes_status=?, updated_at_iso=? "
            "WHERE capacity_id=? AND COALESCE(chutes_status, '') NOT IN ('listed', 'listing')",
            ("listing", _now_iso(), capacity_id),
        )
        if cur.rowcount != 1 and stale_listing:
            cur = conn.execute(
                "UPDATE tee_gpu_capacity SET chutes_status=?, updated_at_iso=? "
                "WHERE capacity_id=? AND chutes_status='listing'",
                ("listing", _now_iso(), capacity_id),
            )
        if cur.rowcount != 1:
            return "listing_in_progress"
        started = dict(payload)
        started["status"] = "listing"
        if stale_listing:
            started["reclaimed_stale_listing"] = True
        _insert_event(conn, capacity_id, "admin", "chutes_list_started", started)
        return "claimed"

    claim = store.write(_do)
    if claim == "missing":
        raise HTTPException(404, "capacity_not_found")
    return str(claim)


def _listing_claim_is_stale(updated_at_iso: str) -> bool:
    ts = _parse_iso(updated_at_iso)
    if ts is None:
        return False
    timeout_with_margin = _bounded_timeout(None) + 60
    stale_secs = max(
        timeout_with_margin,
        _int(os.environ.get("CATHEDRAL_TEE_GPU_LISTING_STALE_SECS"), default=900),
    )
    return datetime.now(timezone.utc).timestamp() - ts > stale_secs


def evidence_summary(attestation: Any) -> dict[str, Any]:
    body = _loads(_json_blob(attestation))
    if not isinstance(body, dict) or not body:
        return {
            "status": "missing",
            "acceptable": False,
            "proof": "none",
            "reason": "no_attestation_evidence_submitted",
        }

    review = body.get("cathedral_review")
    review = review if isinstance(review, dict) else {}
    review_status = str(review.get("status") or "").strip().lower()
    if review_status in _EVIDENCE_REVIEW_STATUS:
        crypto = review_status == _EVIDENCE_CRYPTO_STATUS
        return {
            "status": review_status,
            "acceptable": review_status in _EVIDENCE_ACCEPTED_STATUS,
            "proof": str(
                review.get("proof")
                or ("tdx_dcap_and_nvidia_gpu_attestation" if crypto else "operator_review_only")
            ),
            "cryptographic_proof": crypto and _boolish(review.get("cryptographic_proof")),
            "reason": str(review.get("reason") or ""),
            "reviewed_at_iso": str(review.get("reviewed_at_iso") or ""),
            "verifier": str(review.get("verifier") or ""),
            "verifier_command_digest": str(review.get("verifier_command_digest") or ""),
            "verifier_result_digest": str(review.get("verifier_result_digest") or ""),
            "gpu_claims_match": crypto and _boolish(review.get("gpu_claims_match")),
            "note": (
                "cryptographic TDX/GPU attestation verified"
                if crypto else "operator review only; not cryptographic attestation"
            ),
        }

    fields = _submitted_evidence_fields(body)
    request_id = _evidence_request_id(body)
    if fields and request_id:
        status = "submitted"
        reason = "operator_review_required"
    elif fields:
        status = "submitted_without_request"
        reason = "fresh_evidence_request_required"
    else:
        status = "missing"
        reason = "no_supported_evidence_fields"
    return {
        "status": status,
        "acceptable": False,
        "proof": "unverified_submission",
        "reason": reason,
        "submitted_fields": fields,
        "evidence_request_present": bool(request_id),
        "note": "submitted evidence is pending operator review and real verifier integration",
    }


def _submitted_evidence_fields(body: dict[str, Any]) -> list[str]:
    candidates = (
        "tdx_quote_b64", "quote_b64", "raw_quote_b64", "collateral_b64",
        "collateral_json", "gpu_evidence_b64", "gpu_evidence_json",
        "nvidia_cc_evidence", "report_data_hex", "mrtd_hex", "rtmrs_json",
    )
    return [key for key in candidates if body.get(key) not in (None, "", {}, [])]


def _evidence_request_id(body: dict[str, Any]) -> str:
    return str(
        body.get("evidence_request_id")
        or body.get("request_id")
        or body.get("attestation_nonce")
        or body.get("nonce")
        or ""
    ).strip()


def _attestation_input_json(value: Any, *, allow_review: bool) -> str:
    body = _loads(_json_blob(value or {}))
    if not isinstance(body, dict):
        body = {"raw": body}
    if not allow_review:
        body.pop("cathedral_review", None)
        body.pop("review", None)
    return _json_blob(body)


def _attestation_review_json(attestation_json: Any, review: Any, *, reviewed_by: str) -> str:
    body = _loads(_json_blob(attestation_json or {}))
    if not isinstance(body, dict):
        body = {"raw": body}
    review_body = review if isinstance(review, dict) else {}
    status = _normal_review_status(str(review_body.get("status") or ""))
    if status == "operator_reviewed":
        fields = _submitted_evidence_fields(body)
        if not fields:
            raise HTTPException(400, "attestation_evidence_missing")
        if not _evidence_request_id(body):
            raise HTTPException(400, "evidence_request_missing")
    body["cathedral_review"] = {
        "status": status,
        "reason": str(review_body.get("reason") or ""),
        "reviewed_by": reviewed_by,
        "reviewed_at_iso": _now_iso(),
        "proof": "operator_review_only",
        "cryptographic_proof": False,
        "verifier": str(review_body.get("verifier") or "not_configured"),
        "note": "operator review only; this does not prove genuine TDX/GPU attestation",
    }
    return _json_blob(body)


def _attestation_verified_json(
    attestation_json: Any,
    verifier_result: dict[str, Any],
    *,
    request_payload: dict[str, Any],
    reviewed_by: str,
) -> str:
    body = _loads(_json_blob(attestation_json or {}))
    if not isinstance(body, dict):
        body = {"raw": body}
    fields = _submitted_evidence_fields(body)
    if not fields:
        raise HTTPException(400, "attestation_evidence_missing")
    request_id = _evidence_request_id(body)
    if not request_id:
        raise HTTPException(400, "evidence_request_missing")
    request_binding = request_payload.get("evidence_request_binding")
    request_binding = request_binding if isinstance(request_binding, dict) else {}
    summary = _public_verifier_summary(verifier_result)
    result_digest = _digest_text(_json_blob(verifier_result))
    body["cathedral_review"] = {
        "status": _EVIDENCE_CRYPTO_STATUS,
        "reason": str(verifier_result.get("reason") or "tdx_and_gpu_attestation_verified"),
        "reviewed_by": reviewed_by,
        "reviewed_at_iso": _now_iso(),
        "proof": str(verifier_result.get("proof") or "tdx_dcap_and_nvidia_gpu_attestation"),
        "cryptographic_proof": True,
        "verifier": str(verifier_result.get("verifier") or "configured_verifier"),
        "verifier_command_digest": _verifier_command_digest(),
        "verifier_result_digest": result_digest,
        "evidence_request_id": request_id,
        "evidence_request_binding_sha256": str(request_binding.get("sha256_hex") or ""),
        "tdx_verified": True,
        "gpu_verified": True,
        "gpu_claims_match": True,
        "report_data_match": True,
        "debug_disabled": True,
        "verifier_summary": summary,
        "note": "fresh TDX quote and NVIDIA GPU confidential-compute evidence verified by configured verifier",
    }
    return _json_blob(body)


def _normal_review_status(raw: str) -> str:
    status = raw.strip().lower().replace("-", "_")
    aliases = {
        "accepted": "operator_reviewed",
        "approved": "operator_reviewed",
        "pass": "operator_reviewed",
        "passed": "operator_reviewed",
        "fail": "rejected",
        "failed": "rejected",
        "deny": "rejected",
        "denied": "rejected",
    }
    status = aliases.get(status, status)
    if status not in _EVIDENCE_MANUAL_REVIEW_STATUS:
        raise HTTPException(400, {
            "detail": "invalid_attestation_review_status",
            "allowed": sorted(_EVIDENCE_MANUAL_REVIEW_STATUS),
        })
    return status


def _verify_offer_claim(
    verifier,
    hotkey: str,
    signature_b64: str,
    submitted_at: str,
    *,
    node_id: str,
    payload_digest: str,
) -> None:
    ts = _parse_iso(submitted_at)
    if ts is None:
        raise HTTPException(400, "invalid_submitted_at")
    if abs(datetime.now(timezone.utc).timestamp() - ts) > _SKEW_SECS:
        raise HTTPException(400, "submitted_at_outside_clock_skew")
    msg = canonical_claim_bytes(
        bundle_hash=_empty_bundle_hash(), card_id=TEE_CARD_ID,
        miner_hotkey=hotkey, submitted_at=submitted_at,
        challenge_id=node_id,
        dimacs_solution_sha256=payload_digest)
    if not verifier.verify(hotkey, msg, signature_b64):
        raise HTTPException(401, "invalid_hotkey_signature")


def require_miner_intake_gate(hotkey: str, body: dict[str, Any]) -> None:
    allowed = _intake_allowlist()
    if hotkey in allowed:
        return

    expected = os.environ.get(_INTAKE_CODE_ENV, "").strip()
    require_code = _truthy(os.environ.get(_INTAKE_REQUIRE_CODE_ENV, ""))
    if not expected:
        if allowed:
            raise HTTPException(403, "tee_gpu_hotkey_not_invited")
        detail = (
            "tee_gpu_intake_code_not_configured"
            if require_code else "tee_gpu_intake_gate_not_configured"
        )
        raise HTTPException(503, detail)

    supplied = str(
        body.get("intake_code")
        or body.get("invite_code")
        or body.get("access_code")
        or ""
    ).strip()
    if expected and hmac.compare_digest(supplied, expected):
        return
    raise HTTPException(403, "invalid_tee_gpu_intake_code")


def _intake_allowlist() -> set[str]:
    raw = os.environ.get(_INTAKE_ALLOWLIST_ENV, "")
    return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}


def _insert_event(conn, capacity_id: str, actor: str, event_type: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO tee_gpu_capacity_events"
        "(id, capacity_id, actor, event_type, event_json, created_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (new_uuid(), capacity_id, actor, event_type, _json_blob(payload), _now_iso()))


def _record_capacity_event(
    store: Store,
    capacity_id: str,
    actor: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    def _do(conn):
        _insert_event(conn, capacity_id, actor, event_type, payload)

    store.write(_do)


def _capacity_row(store: Store, capacity_id: str) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM tee_gpu_capacity WHERE capacity_id=?", (capacity_id,))
    return _row_to_dict(rows[0]) if rows else None


def _capacity_events(store: Store, capacity_id: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT event_type, event_json, created_at_iso FROM tee_gpu_capacity_events "
        "WHERE capacity_id=? ORDER BY created_at_iso ASC",
        (capacity_id,),
    )
    events = []
    for row in rows:
        payload = _loads(row["event_json"])
        if not isinstance(payload, dict):
            payload = {"raw": payload}
        payload = dict(payload)
        payload.setdefault("event_type", row["event_type"])
        payload.setdefault("created_at_iso", row["created_at_iso"])
        events.append(payload)
    return events


def _latest_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _record_chutes_listing(
    store: Store,
    capacity_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    def _do(conn):
        _insert_event(conn, capacity_id, "admin", event_type, payload)

    store.write(_do)


def _payload_digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _empty_bundle_hash() -> str:
    try:
        import blake3
        return blake3.blake3(b"").hexdigest()
    except Exception:
        return hashlib.sha256(b"").hexdigest()


def _capacity_id(owner_hotkey: str, node_id: str) -> str:
    return "tee-" + hashlib.sha256(f"{owner_hotkey}:{node_id}".encode()).hexdigest()[:16]


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected_json_body")
    if not isinstance(body, dict):
        raise HTTPException(400, "expected_json_object")
    return body


async def _optional_json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(400, "expected_json_object")
    return body


def _required_str(body: dict[str, Any], key: str) -> str:
    val = str(body.get(key) or "").strip()
    if not val:
        raise HTTPException(400, f"missing_{key}")
    return val


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _json_blob(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = {"raw": value}
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads(text: str) -> Any:
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def _h(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _operator_use_authorized(body: dict[str, Any]) -> bool:
    auth = body.get("authorization")
    auth_body = auth if isinstance(auth, dict) else {}
    return any(_boolish(v) for v in (
        body.get("operator_use_authorized"),
        body.get("capacity_use_authorized"),
        body.get("use_authorized"),
        auth_body.get("operator_use_authorized"),
        auth_body.get("capacity_use_authorized"),
    ))


def _authorization_json(
    body: dict[str, Any],
    *,
    owner_hotkey: str,
    node_id: str,
    accepted: bool,
    source: str,
    accepted_at_iso: str,
) -> str:
    supplied = body.get("authorization")
    supplied_body = supplied if isinstance(supplied, dict) else {}
    supplied_body = {
        key: value for key, value in supplied_body.items()
        if key not in _INTAKE_CODE_FIELDS
    }
    payload = {
        "version": AUTHORIZATION_VERSION,
        "text": AUTHORIZATION_TEXT,
        "accepted": accepted,
        "accepted_at_iso": accepted_at_iso if accepted else "",
        "owner_hotkey": owner_hotkey,
        "node_id": node_id,
        "source": source,
        "supplied": supplied_body,
    }
    return _json_blob(payload)


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(num) and num > 0.0


def _int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _int(value, default=0)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _is_finite_float(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _bounded_timeout(value: int | None) -> int:
    default = _int(os.environ.get("CATHEDRAL_TEE_GPU_CHUTES_TIMEOUT_SECS"), default=120)
    raw = value if value is not None else default
    return max(5, min(int(raw), 900))


def _bounded_verify_timeout(value: int | None) -> int:
    default = _int(os.environ.get("CATHEDRAL_TEE_GPU_VERIFY_TIMEOUT_SECS"), default=120)
    raw = value if value is not None else default
    return max(5, min(int(raw), 900))


def _bounded_evidence_request_ttl(value: int | None) -> int:
    default = _int(
        os.environ.get("CATHEDRAL_TEE_GPU_EVIDENCE_REQUEST_TTL_SECS"),
        default=_DEFAULT_EVIDENCE_REQUEST_TTL_SECS,
    )
    raw = value if value is not None else default
    return max(60, min(int(raw), 3600))


def _tail(value: Any, *, limit: int = 8000) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    return text[-limit:]


def _float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _now_iso() -> str:
    return _format_iso(datetime.now(timezone.utc))


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso(s: str) -> float | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None
