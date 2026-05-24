from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_codeql_workflow_uploads_alerts_to_code_scanning() -> None:
    """CodeQL must publish SARIF so GitHub creates security alerts."""
    workflow = (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8")

    assert "github/codeql-action/analyze@v3" in workflow
    assert "security-events: write" in workflow
    assert "upload: never" not in workflow
