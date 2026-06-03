from cathedral.publisher.app import (
    _sat_autopilot_worker_enabled,
    _sat_fill_loop_enabled,
)


def test_sat_maintenance_worker_gates_default_on(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_SAT_AUTOPILOT_WORKER_ENABLED", raising=False)
    monkeypatch.delenv("CATHEDRAL_SAT_FILL_LOOP_ENABLED", raising=False)

    assert _sat_autopilot_worker_enabled() is True
    assert _sat_fill_loop_enabled() is True


def test_sat_maintenance_worker_gates_keep_explicit_kill_switch(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SAT_AUTOPILOT_WORKER_ENABLED", "false")
    monkeypatch.setenv("CATHEDRAL_SAT_FILL_LOOP_ENABLED", "0")

    assert _sat_autopilot_worker_enabled() is False
    assert _sat_fill_loop_enabled() is False
