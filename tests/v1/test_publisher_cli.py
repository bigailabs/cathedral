from __future__ import annotations

from typing import Any


def test_publisher_serve_disables_uvicorn_access_log(monkeypatch) -> None:
    """Access logs include query strings; CNF fetch tokens live in ?t=."""
    from cathedral.publisher import cli

    captured: dict[str, Any] = {}

    def fake_run(application: object, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "configure", lambda **kwargs: None)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    import cathedral.publisher as publisher_pkg

    monkeypatch.setattr(publisher_pkg, "from_settings", lambda database_path: object())

    cli.serve(
        database_path="data/test-publisher.db",
        host="127.0.0.1",
        port=9444,
        json_logs=True,
        log_level="info",
    )

    assert captured["access_log"] is False
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9444
