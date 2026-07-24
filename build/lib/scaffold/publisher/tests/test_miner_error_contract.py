"""Guard: docs/MINER_ERROR_CONTRACT.md stays aligned with the codes the code
actually returns. If a maintainer adds/renames a miner-facing rejection reason
in app.py / sat_solution.py / worker.mjs without documenting it (or documents a
reason no code emits), this test fails.

Two directions are pinned:
  * forward  -- every documented reason appears verbatim in the source it
                claims to come from;
  * reverse  -- every miner-facing reason a source actually emits is documented
                (drift-check), scoped to the three miner-facing namespaces:
                the submit-path `X-Cathedral-Rejection-Reason` header values in
                app.py, the `solution_*` reasons in sat_solution.py, and the
                `*_origin_unavailable` reasons in worker.mjs.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "MINER_ERROR_CONTRACT.md"
APP = REPO / "scaffold" / "publisher" / "app.py"
SAT = REPO / "scaffold" / "publisher" / "sat_solution.py"
WORKER = REPO / "deploy" / "edge-router" / "worker.mjs"

# Reasons documented in the contract that MUST appear verbatim in app.py.
# (submit_admission_unavailable is intentionally excluded here: it is the
# planned Phase-4 code, not yet emitted by current app.py -- pinned separately
# by test_planned_admission_code_marked_planned.)
APP_REASONS = [
    # submit-path X-Cathedral-Rejection-Reason header values
    "submit_busy_retry",
    "rate_limited",
    "challenge_not_active",
    "challenge_already_locked",
    "already_solved",
    "replayed_signature",
    # signing / header / timestamp errors (raised as HTTPException detail)
    "invalid hotkey signature",
    "missing X-Cathedral-Submitted-At",
    "submitted_at outside acceptable clock-skew window",
    "invalid submitted_at",
    # challenge / CNF lookup
    "no_active_challenge",
    "not found",  # opaque CNF 404 on /v1/challenges/{id}/cnf
]
SOLUTION_REASONS = [
    "solution_missing_status",
    "solution_status_unsatisfiable",
    "solution_unknown_status",
    "solution_non_integer_literal",
    "solution_variable_out_of_range",
    "solution_contradictory_assignment",
    "solution_incomplete_assignment",
    "solution_unsatisfied",
]


def _doc():
    return DOC.read_text(encoding="utf-8")


def test_doc_exists():
    assert DOC.exists(), "docs/MINER_ERROR_CONTRACT.md is missing"


# --------------------------------------------------------------------------- #
# forward: documented -> emitted
# --------------------------------------------------------------------------- #
def test_app_reasons_documented_and_emitted():
    doc = _doc()
    app_src = APP.read_text(encoding="utf-8")
    for reason in APP_REASONS:
        assert reason in doc, f"{reason!r} not documented in contract"
        assert reason in app_src, f"{reason!r} documented but not emitted by app.py"


def test_solution_reasons_documented_and_emitted():
    doc = _doc()
    sat_src = SAT.read_text(encoding="utf-8")
    for reason in SOLUTION_REASONS:
        assert reason in doc, f"{reason!r} not documented in contract"
        assert reason in sat_src, f"{reason!r} documented but not emitted by sat_solution.py"


def test_origin_unavailable_documented():
    doc = _doc()
    worker_src = WORKER.read_text(encoding="utf-8")
    assert "read_origin_unavailable" in worker_src
    assert "_origin_unavailable" in doc
    assert "read_origin_unavailable" in doc


def test_receipt_not_found_real_and_documented():
    """receipt_not_found is now a REAL miner-facing 404 emitted by the durable
    submit-receipt lookup route GET /v1/agents/receipts/{receipt_id} (added by
    the submit-redesign slice). Source and doc must stay in lock-step: if the
    code emits it, the contract documents it; if the code drops it, the doc
    must not fabricate it.

    (Previously this guarded the OPPOSITE — that receipt_not_found was a doc-only
    fabrication with no backing route. The submit-redesign integration made the
    lookup route real, so the contract now documents its real shape.)"""
    app_src = APP.read_text(encoding="utf-8")
    in_src = "receipt_not_found" in app_src
    in_doc = "receipt_not_found" in _doc()
    assert in_src, (
        "receipt_not_found is no longer emitted by app.py -- remove the row from "
        "docs/MINER_ERROR_CONTRACT.md so the doc stops fabricating it"
    )
    # The backing route must be the durable receipt lookup, not something else.
    assert "/v1/agents/receipts/{receipt_id}" in app_src, (
        "receipt_not_found is emitted but the durable receipt lookup route is gone"
    )
    assert in_doc, (
        "app.py emits receipt_not_found (durable receipt lookup 404) but it is "
        "undocumented in the miner error contract"
    )


# --------------------------------------------------------------------------- #
# reverse: emitted -> documented (drift-check)
# --------------------------------------------------------------------------- #
def test_reverse_submit_header_reasons_documented():
    """Every static X-Cathedral-Rejection-Reason header value in app.py must be
    documented. Catches a new submit-path reason added without a doc row."""
    doc = _doc()
    app_src = APP.read_text(encoding="utf-8")
    emitted = set(
        re.findall(r'"X-Cathedral-Rejection-Reason":\s*"([^"]+)"', app_src)
    )
    assert emitted, "no static rejection-reason header values found -- regex stale?"
    for reason in sorted(emitted):
        assert reason in doc, (
            f"submit emits X-Cathedral-Rejection-Reason={reason!r} but it is "
            "undocumented in the miner error contract"
        )


def test_reverse_solution_reasons_documented():
    """Every solution_* reason sat_solution.py can emit must be documented."""
    doc = _doc()
    sat_src = SAT.read_text(encoding="utf-8")
    emitted = set(re.findall(r"solution_[a-z_]+", sat_src))
    assert emitted, "no solution_* reasons found in sat_solution.py -- regex stale?"
    for reason in sorted(emitted):
        assert reason in doc, (
            f"sat_solution.py emits {reason!r} but it is undocumented in the "
            "miner error contract"
        )


def test_reverse_origin_unavailable_reasons_documented():
    """Every *_origin_unavailable reason worker.mjs emits must be documented,
    either verbatim or covered by the generic `<role>_origin_unavailable` row."""
    doc = _doc()
    worker_src = WORKER.read_text(encoding="utf-8")
    emitted = set(re.findall(r"[a-z]+_origin_unavailable", worker_src))
    assert emitted, "no *_origin_unavailable reasons found in worker.mjs -- regex stale?"
    for reason in sorted(emitted):
        documented = reason in doc or "_origin_unavailable" in doc
        assert documented, (
            f"worker.mjs emits {reason!r} but it is undocumented in the miner "
            "error contract"
        )


def test_planned_admission_code_marked_planned():
    doc = _doc()
    app_src = APP.read_text(encoding="utf-8")
    assert "submit_admission_unavailable" in doc
    # It is genuinely not yet emitted; the doc must flag it as planned.
    assert "submit_admission_unavailable" not in app_src
    idx = doc.rindex("submit_admission_unavailable")
    row = doc[idx: idx + 400].lower()
    assert "planned" in row, "submit_admission_unavailable must be marked planned"


def test_status_codes_present():
    doc = _doc()
    for code in ("429", "503", "409", "401", "400", "404", "504"):
        assert f"**{code}**" in doc, f"status {code} not in contract table"


def test_retry_after_guidance_present():
    doc = _doc()
    assert "Retry-After" in doc
    assert "jitter" in doc.lower()
