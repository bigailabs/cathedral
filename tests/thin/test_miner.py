from __future__ import annotations

import asyncio
import shlex
import sys
import time
from types import SimpleNamespace

import pytest

from cathedral_thin import miner as miner_module
from cathedral_thin.core import (
    build_challenge,
    encode_assignment,
    generate_ajm_cnf,
    derive_challenge_seed,
)
from cathedral_thin.miner import (
    MinerConfig,
    MinerService,
    PermitAccess,
    parse_dimacs_model,
    run_external_solver,
)
from cathedral_thin.protocol import SatChallenge


def test_parse_solver_model_requires_sat_and_complete():
    assert parse_dimacs_model("s SATISFIABLE\nv 1 -2 3 0\n", n_vars=3) == [1, -2, 3]
    with pytest.raises(Exception, match="did not report SAT"):
        parse_dimacs_model("s UNSATISFIABLE\n", n_vars=3)
    with pytest.raises(Exception, match="complete"):
        parse_dimacs_model("s SATISFIABLE\nv 1 -2 0\n", n_vars=3)


def test_external_solver_process_contract(tmp_path):
    script = tmp_path / "solver.py"
    script.write_text(
        "print('s SATISFIABLE')\nprint('v 1 -2 3 0')\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {{cnf}}"
    assignment, solver_name = run_external_solver(
        command,
        dimacs="p cnf 3 1\n1 -2 3 0\n",
        timeout_secs=2.0,
        n_vars=3,
    )
    assert assignment == [1, -2, 3]
    assert solver_name


def test_miner_validates_transport_identity_and_ignores_planted_model(monkeypatch):
    secret = b"s" * 32
    issued = int(time.time() * 1000)
    challenge, cnf = build_challenge(
        secret,
        netuid=1,
        validator_hotkey="validator",
        miner_hotkey="miner",
        round_id=2,
        issued_at_ms=issued,
        ttl_ms=10_000,
        n_vars=16,
        n_clauses=60,
    )
    _, model = generate_ajm_cnf(
        derive_challenge_seed(
            secret,
            netuid=1,
            validator_hotkey="validator",
            miner_hotkey="miner",
            round_id=2,
        ),
        n_vars=16,
        n_clauses=60,
    )

    def fake_solver(_command, *, dimacs, timeout_secs, n_vars):
        assert dimacs == cnf.to_dimacs()
        assert timeout_secs > 0
        return list(model), "fake"

    monkeypatch.setattr(miner_module, "run_external_solver", fake_solver)
    service = MinerService(MinerConfig("miner", "fake {cnf}", 5.0, 100, 1000))
    response = asyncio.run(
        service.solve(SatChallenge.from_challenge(challenge), caller_hotkey="validator")
    )
    assert response.error == ""
    assert response.assignment_b64 == encode_assignment(model, n_vars=16)

    rejected = asyncio.run(
        service.solve(SatChallenge.from_challenge(challenge), caller_hotkey="attacker")
    )
    assert rejected.assignment_b64 == ""
    assert "another validator" in rejected.error


def test_miner_caches_exact_retry_and_refuses_second_challenge_for_round(monkeypatch):
    secret = b"r" * 32
    issued = int(time.time() * 1000)
    challenge, _ = build_challenge(
        secret,
        netuid=1,
        validator_hotkey="validator",
        miner_hotkey="miner",
        round_id=9,
        issued_at_ms=issued,
        ttl_ms=10_000,
        n_vars=16,
        n_clauses=60,
    )
    _, model = generate_ajm_cnf(
        derive_challenge_seed(
            secret,
            netuid=1,
            validator_hotkey="validator",
            miner_hotkey="miner",
            round_id=9,
        ),
        n_vars=16,
        n_clauses=60,
    )
    calls = 0

    def fake_solver(_command, *, dimacs, timeout_secs, n_vars):
        nonlocal calls
        calls += 1
        return list(model), "fake"

    async def exercise():
        service = MinerService(MinerConfig("miner", "fake", 5.0, 100, 1000))
        first = await service.solve(
            SatChallenge.from_challenge(challenge), caller_hotkey="validator"
        )
        retry = await service.solve(
            SatChallenge.from_challenge(challenge), caller_hotkey="validator"
        )
        refreshed_challenge, _ = build_challenge(
            secret,
            netuid=1,
            validator_hotkey="validator",
            miner_hotkey="miner",
            round_id=9,
            issued_at_ms=issued + 1,
            ttl_ms=10_000,
            n_vars=16,
            n_clauses=60,
        )
        refreshed = await service.solve(
            SatChallenge.from_challenge(refreshed_challenge),
            caller_hotkey="validator",
        )
        second_challenge, _ = build_challenge(
            b"q" * 32,
            netuid=1,
            validator_hotkey="validator",
            miner_hotkey="miner",
            round_id=9,
            issued_at_ms=issued + 2,
            ttl_ms=10_000,
            n_vars=16,
            n_clauses=60,
        )
        refused = await service.solve(
            SatChallenge.from_challenge(second_challenge),
            caller_hotkey="validator",
        )
        return first, retry, refreshed, refused

    monkeypatch.setattr(miner_module, "run_external_solver", fake_solver)
    first, retry, refreshed, refused = asyncio.run(exercise())
    assert calls == 1
    assert first.assignment_b64 == retry.assignment_b64
    assert first.assignment_b64 == refreshed.assignment_b64
    assert refused.assignment_b64 == ""
    assert "different formula this round" in refused.error


def test_validator_permit_access_is_rate_limited_per_hotkey():
    now = [100.0]

    class FakeSubtensor:
        def metagraph(self, _netuid, *, lite):
            assert lite
            return SimpleNamespace(
                hotkeys=["validator", "other"], validator_permit=[True, False]
            )

    access = PermitAccess(
        subtensor=FakeSubtensor(),
        netuid=1,
        miner_hotkey="miner",
        requests_per_minute=2,
        window_secs=60.0,
        clock=lambda: now[0],
    )
    access.refresh()
    request = SimpleNamespace(
        miner_hotkey="miner",
        validator_hotkey="validator",
        dendrite=SimpleNamespace(hotkey="validator"),
    )
    assert asyncio.run(access.blacklist(request)) == (False, "allowed")
    assert asyncio.run(access.blacklist(request)) == (False, "allowed")
    assert asyncio.run(access.blacklist(request)) == (True, "validator_rate_limited")
    now[0] += 61.0
    assert asyncio.run(access.blacklist(request)) == (False, "allowed")


def test_validator_permit_refresh_retains_last_snapshot_on_rpc_failure(capsys):
    class FlakySubtensor:
        def __init__(self):
            self.fail = False

        def metagraph(self, _netuid, *, lite):
            assert lite
            if self.fail:
                raise ConnectionError("temporary rpc failure")
            return SimpleNamespace(hotkeys=["validator"], validator_permit=[True])

    subtensor = FlakySubtensor()
    access = PermitAccess(
        subtensor=subtensor,
        netuid=1,
        miner_hotkey="miner",
    )
    access.refresh()
    subtensor.fail = True

    assert not access.refresh_preserving_snapshot()
    assert access.allowed == {"validator"}
    assert "held previous snapshot exception=ConnectionError" in capsys.readouterr().out
