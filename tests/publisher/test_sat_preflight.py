from __future__ import annotations

from cathedral.publisher.sat_preflight import run_synthetic_boolean_launch_preflight


def _seed_hex() -> str:
    return "11" * 32


def test_sat_launch_preflight_accepts_valid_operator_cnf(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")

    result = run_synthetic_boolean_launch_preflight(
        {
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH": str(cnf_path),
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID": "launch-toy-001",
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER": "3",
            "CATHEDRAL_EVAL_SIGNING_KEY": _seed_hex(),
            "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY": _seed_hex(),
        }
    )

    assert result.ok
    assert result.errors == ()
    assert result.details["challenge_id"] == "launch-toy-001"
    assert result.details["storage_mode"] == "sqlite_text"
    assert result.details["max_cnf_bytes_enforced"] is True
    assert result.details["tier"] == 3
    assert result.details["num_vars"] == 2
    assert result.details["num_clauses"] == 1
    assert result.details["cnf_file_bytes"] == cnf_path.stat().st_size


def test_sat_launch_preflight_accepts_file_backed_operator_cnf(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")

    result = run_synthetic_boolean_launch_preflight(
        {
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH": str(cnf_path),
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_STORAGE_MODE": "file",
            "CATHEDRAL_EVAL_SIGNING_KEY": _seed_hex(),
            "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY": _seed_hex(),
        }
    )

    assert result.ok
    assert result.details["storage_mode"] == "file"
    assert result.details["max_cnf_bytes_enforced"] is False
    assert result.details["num_vars"] == 2
    assert result.details["num_clauses"] == 1


def test_sat_launch_preflight_rejects_missing_signing_keys(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")

    result = run_synthetic_boolean_launch_preflight(
        {"CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH": str(cnf_path)}
    )

    assert not result.ok
    assert "CATHEDRAL_EVAL_SIGNING_KEY is required" in result.errors
    assert (
        "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY is required for signed remote weights"
        in result.errors
    )


def test_sat_launch_preflight_rejects_cnf_above_launch_limit(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")

    result = run_synthetic_boolean_launch_preflight(
        {
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH": str(cnf_path),
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_MAX_CNF_BYTES": "8",
            "CATHEDRAL_EVAL_SIGNING_KEY": _seed_hex(),
            "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY": _seed_hex(),
        }
    )

    assert not result.ok
    assert (
        "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH exceeds "
        "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_MAX_CNF_BYTES"
    ) in result.errors
