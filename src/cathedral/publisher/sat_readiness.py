"""Public toy readiness probe for synthetic_boolean_v1 miners."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from cathedral.lanes.synthetic_boolean_v1.dimacs import (
    assignment_covers_all_vars,
    assignment_in_range,
    evaluate_assignment,
    parse_dimacs_cnf,
    parse_dimacs_solution,
)
from cathedral.lanes.synthetic_boolean_v1.toy_corpus import toy_instance_for

router = APIRouter()

_TOY = toy_instance_for(1)
_TOY_CNF = _TOY.cnf_text
_TOY_SHA256 = hashlib.sha256(_TOY_CNF.encode("utf-8")).hexdigest()
_PROBE_PATH = "/api/cathedral/v1/synthetic-boolean/readiness-probe"
_MAX_VERIFY_BODY_BYTES = 16 * 1024
_MAX_VERIFY_SOLUTION_BYTES = 8 * 1024


@router.get("/v1/synthetic-boolean/readiness-probe")
async def get_synthetic_boolean_readiness_probe() -> dict[str, Any]:
    """Return a non-scored toy SAT probe using the real prompt shape."""
    base = os.environ.get("CATHEDRAL_PUBLIC_BASE_URL", "https://api.cathedral.computer").rstrip(
        "/"
    )
    return {
        "capability": "synthetic_boolean_v1",
        "purpose": "readiness_probe",
        "emissions_eligible": False,
        "weighted_score": 0.0,
        "public_input": {
            "format": "dimacs",
            "cnf_url": f"{base}{_PROBE_PATH}/cnf",
            "cnf_sha256": _TOY_SHA256,
            "num_vars": _TOY.num_vars,
            "num_clauses": _TOY.num_clauses,
        },
        "answer_format": {
            "type": "FINAL_ANSWER",
            "json_keys": ["dimacs_solution"],
        },
    }


@router.get(
    "/v1/synthetic-boolean/readiness-probe/cnf",
    response_class=PlainTextResponse,
)
async def get_synthetic_boolean_readiness_cnf() -> PlainTextResponse:
    """Serve the toy readiness CNF as text/plain."""
    return PlainTextResponse(_TOY_CNF, media_type="text/plain; charset=utf-8")


@router.post("/v1/synthetic-boolean/readiness-probe/verify")
async def verify_synthetic_boolean_readiness_probe(request: Request) -> dict[str, Any]:
    """Verify a toy probe answer, but always return a zero weighted score."""
    body = await _read_verify_body(request)
    raw_solution = body["dimacs_solution"] if "dimacs_solution" in body else None
    if not isinstance(raw_solution, str):
        return _probe_result(False, "answer_missing_dimacs_solution")
    if set(body) != {"dimacs_solution"}:
        return _probe_result(False, "answer_unexpected_keys")
    if len(raw_solution.encode("utf-8")) > _MAX_VERIFY_SOLUTION_BYTES:
        raise HTTPException(status_code=413, detail="dimacs_solution too large")

    cnf = parse_dimacs_cnf(_TOY_CNF)
    if not cnf.ok:
        return _probe_result(False, cnf.rejection_reason or "cnf_unparseable")

    solution = parse_dimacs_solution(raw_solution)
    if not solution.ok:
        return _probe_result(False, solution.rejection_reason or "solution_unparseable")
    if not assignment_in_range(cnf, solution.assignment):
        return _probe_result(False, "solution_variable_out_of_range")
    if not assignment_covers_all_vars(cnf, solution.assignment):
        return _probe_result(False, "solution_incomplete_assignment")

    satisfied, satisfied_count, clause_count = evaluate_assignment(cnf, solution.assignment)
    return {
        "valid": satisfied,
        "rejection_reason": None if satisfied else "solution_unsatisfied",
        "clauses_satisfied": satisfied_count,
        "clause_count": clause_count,
        "weighted_score": 0.0,
        "emissions_eligible": False,
    }


async def _read_verify_body(request: Request) -> dict[str, Any]:
    """Read the public toy verifier body with a small pre-parse cap.

    The readiness verifier is unauthenticated and mounted publicly. Avoid a
    typed ``body: dict`` parameter here: FastAPI would JSON-parse the whole
    payload before this route can enforce the toy-probe limit.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_VERIFY_BODY_BYTES:
                raise HTTPException(status_code=413, detail="readiness verify body too large")
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_VERIFY_BODY_BYTES:
            raise HTTPException(status_code=413, detail="readiness verify body too large")
        chunks.append(chunk)

    try:
        parsed = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_invalid_json": True}
    if not isinstance(parsed, dict):
        return {"_invalid_json": True}
    return parsed


def _probe_result(valid: bool, rejection_reason: str) -> dict[str, Any]:
    return {
        "valid": valid,
        "rejection_reason": None if valid else rejection_reason,
        "weighted_score": 0.0,
        "emissions_eligible": False,
    }
