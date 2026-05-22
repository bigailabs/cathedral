from __future__ import annotations

from cathedral.lanes.synthetic_boolean_v1.dimacs import MAX_ASSIGNMENT_VARIABLES
from cathedral.publisher.sat_preflight import run_synthetic_boolean_launch_preflight


def _seed_hex() -> str:
    return "11" * 32


def _launch_env(cnf_path) -> dict[str, str]:
    return {
        "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH": str(cnf_path),
        "CATHEDRAL_TASK_FAMILY_FEED_ENABLED": "true",
        "CATHEDRAL_TASK_FAMILY_IDS": "synthetic_boolean_v1",
        "CATHEDRAL_EVAL_MODE": "ssh-probe",
        "CATHEDRAL_PROBER_VERSION": "v2",
        "CATHEDRAL_PUBLIC_BASE_URL": "https://api.cathedral.test",
        "CATHEDRAL_EVAL_SIGNING_KEY": _seed_hex(),
        "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY": _seed_hex(),
    }


def test_sat_launch_preflight_accepts_valid_operator_cnf(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")

    env = _launch_env(cnf_path)
    env["CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID"] = "launch-toy-001"
    env["CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER"] = "3"
    result = run_synthetic_boolean_launch_preflight(env)

    assert result.ok
    assert result.errors == ()
    assert result.details["task_family_feed_enabled"] is True
    assert result.details["eval_mode"] == "ssh-probe"
    assert result.details["prober_version"] == "v2"
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

    env = _launch_env(cnf_path)
    env["CATHEDRAL_SYNTHETIC_BOOLEAN_V1_STORAGE_MODE"] = "file"
    result = run_synthetic_boolean_launch_preflight(env)

    assert result.ok
    assert result.details["storage_mode"] == "file"
    assert result.details["max_cnf_bytes_enforced"] is False
    assert result.details["num_vars"] == 2
    assert result.details["num_clauses"] == 1


def test_sat_launch_preflight_rejects_missing_signing_keys(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")

    env = _launch_env(cnf_path)
    env.pop("CATHEDRAL_EVAL_SIGNING_KEY")
    env.pop("CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY")
    result = run_synthetic_boolean_launch_preflight(env)

    assert not result.ok
    assert "CATHEDRAL_EVAL_SIGNING_KEY is required" in result.errors
    assert (
        "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY is required for signed remote weights"
        in result.errors
    )


def test_sat_launch_preflight_rejects_cnf_above_launch_limit(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")

    env = _launch_env(cnf_path)
    env["CATHEDRAL_SYNTHETIC_BOOLEAN_V1_MAX_CNF_BYTES"] = "8"
    result = run_synthetic_boolean_launch_preflight(env)

    assert not result.ok
    assert (
        "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH exceeds "
        "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_MAX_CNF_BYTES"
    ) in result.errors


def test_sat_launch_preflight_rejects_cnf_above_verifier_variable_bound(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(f"p cnf {MAX_ASSIGNMENT_VARIABLES + 1} 0\n", encoding="utf-8")

    result = run_synthetic_boolean_launch_preflight(_launch_env(cnf_path))

    assert not result.ok
    assert (
        "synthetic_boolean_v1 CNF is not valid DIMACS: cnf_too_many_vars"
        in result.errors
    )


def test_sat_launch_preflight_rejects_unsafe_challenge_id(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")

    env = _launch_env(cnf_path)
    env["CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID"] = "sat/bad"
    result = run_synthetic_boolean_launch_preflight(env)

    assert not result.ok
    assert any("challenge_id must be a non-empty RFC3986" in error for error in result.errors)


def test_sat_launch_preflight_rejects_invalid_storage_mode(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")

    env = _launch_env(cnf_path)
    env["CATHEDRAL_SYNTHETIC_BOOLEAN_V1_STORAGE_MODE"] = "sqllite"
    result = run_synthetic_boolean_launch_preflight(env)

    assert not result.ok
    assert (
        "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_STORAGE_MODE must be 'sqlite_text' or 'file'"
        in result.errors
    )


def test_sat_launch_preflight_rejects_missing_runtime_gates(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")

    result = run_synthetic_boolean_launch_preflight(
        {
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH": str(cnf_path),
            "CATHEDRAL_EVAL_SIGNING_KEY": _seed_hex(),
            "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY": _seed_hex(),
        }
    )

    assert not result.ok
    assert "CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true is required" in result.errors
    assert "CATHEDRAL_EVAL_MODE=ssh-probe is required" in result.errors
    assert "CATHEDRAL_PROBER_VERSION=v2 is required" in result.errors
    assert "CATHEDRAL_PUBLIC_BASE_URL is required for SAT cnf_url prompts" in result.errors


def test_sat_launch_preflight_allows_runtime_gate_override(tmp_path) -> None:
    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")

    result = run_synthetic_boolean_launch_preflight(
        {
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH": str(cnf_path),
            "CATHEDRAL_EVAL_SIGNING_KEY": _seed_hex(),
            "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY": _seed_hex(),
        },
        require_runtime_env=False,
    )

    assert result.ok
