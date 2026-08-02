"""The writer, launcher, and public reader must agree on one safe stream."""

from __future__ import annotations

import importlib.util
import pathlib
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config" / "validator-mainnet-sn39.toml"
_STATUS_UNIT = _ROOT / "deploy" / "sn39" / "cathedral-sn39-public-status.service"
_VALIDATOR_UNIT = _ROOT / "deploy" / "sn39" / "cathedral-validator-sn39.service"
_LAUNCHER = _ROOT / "deploy" / "sn39" / "cathedral-sn39-release-launcher.py"
_PUBLISHER = _ROOT / "scripts" / "publish_sn39_validator_status.py"
_REQUIRED_WORKFLOW = _ROOT / ".github" / "workflows" / "two-mode-provenance.yml"

_spec = importlib.util.spec_from_file_location("_sn39_launcher_contract", _LAUNCHER)
_launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_launcher)


def _logs() -> dict:
    return tomllib.loads(_CONFIG.read_text(encoding="utf-8"))["logs"]


def test_config_writes_a_distinct_status_projection():
    logs = _logs()
    assert logs["jsonl"] != logs["status_jsonl"]
    assert logs["status_jsonl"].endswith("/validator-status.jsonl")


def test_public_unit_reads_exactly_the_configured_projection():
    unit = _STATUS_UNIT.read_text(encoding="utf-8")
    assert f"ConditionPathExists={_logs()['status_jsonl']}" in unit
    assert f"ReadOnlyPaths={_logs()['status_jsonl']}" in unit
    assert f"ReadOnlyPaths={_logs()['jsonl']}" not in unit
    assert "SupplementaryGroups=cathedral-validator-log" in unit


def test_publisher_source_is_the_projection_not_the_raw_journal():
    source = _PUBLISHER.read_text(encoding="utf-8")
    assert f'SOURCE = Path("{_logs()["status_jsonl"]}")' in source
    assert f'SOURCE = Path("{_logs()["jsonl"]}")' not in source


def test_launcher_grants_reader_group_only_to_projection():
    for mode in ("preflight", "launch", "continuous", "reconcile"):
        environment = _launcher._child_environment(mode)
        assert "CATHEDRAL_VALIDATOR_JSONL_GROUP" not in environment
        assert (
            environment["CATHEDRAL_VALIDATOR_STATUS_GROUP"] == "cathedral-validator-log"
        )
    status_environment = _launcher._child_environment("status")
    assert "CATHEDRAL_VALIDATOR_JSONL_GROUP" not in status_environment
    assert "CATHEDRAL_VALIDATOR_STATUS_GROUP" not in status_environment


def test_validator_unit_documents_the_same_access_split():
    unit = _VALIDATOR_UNIT.read_text(encoding="utf-8")
    assert re.search(
        r"^Environment=CATHEDRAL_VALIDATOR_STATUS_GROUP=cathedral-validator-log$",
        unit,
        re.M,
    )
    assert not re.search(r"^Environment=CATHEDRAL_VALIDATOR_JSONL_GROUP=", unit, re.M)


def test_required_workflow_runs_every_status_stream_regression():
    workflow = _REQUIRED_WORKFLOW.read_text(encoding="utf-8")
    for test_path in (
        "tests/thin/test_status_sanitization.py",
        "tests/thin/test_status_stream_contract.py",
        "tests/thin/test_status_stream_migration.py",
    ):
        assert test_path in workflow
