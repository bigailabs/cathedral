"""Smoke checks for the Lane S production runner resolver.

No Docker, no network, no paid resources. These checks exercise the safe
default, runner command hardening, wrapper failure behavior, and host timeouts.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from scaffold.contract import Outcome
from scaffold.lanes.solver_arena import Instance, SolverSpec, run_batch
from scaffold.publisher import arena_runner


@contextmanager
def _clean_env():
    keys = [
        "CATHEDRAL_ARENA_RUNNER_CMD",
        "CATHEDRAL_ARENA_SOLVER_BIN",
        "CATHEDRAL_ARENA_REQUIRE_CONTAINMENT",
    ]
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _script(body: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    try:
        f.write(body)
        return f.name
    finally:
        f.close()


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _spec() -> SolverSpec:
    return SolverSpec(
        source_url="--network=host https://example/solver.tgz",
        container_digest="--digest-looking-value",
        source_sha256="runner-smoke-source-hash",
        owner_hotkey="--owner-looking-hotkey",
    )


def _cnf() -> str:
    return "p cnf 1 1\n1 0\n"


def test_default_off() -> None:
    with _clean_env():
        assert arena_runner.adapter_for_spec(_spec()) is None


def test_manifest_runner_success() -> None:
    with _clean_env():
        runner = _script(
            "import json, sys\n"
            "manifest = json.load(open(sys.argv[1]))\n"
            "assert manifest['solver']['source_url'].startswith('--network=host')\n"
            "assert manifest['solver']['container_digest'] == '--digest-looking-value'\n"
            "assert manifest['solver']['owner_hotkey'] == '--owner-looking-hotkey'\n"
            "assert open(manifest['cnf_path']).read().startswith('p cnf 1 1')\n"
            "print('s SATISFIABLE')\n"
            "print('v 1 0')\n")
        try:
            os.environ["CATHEDRAL_ARENA_REQUIRE_CONTAINMENT"] = "0"
            os.environ["CATHEDRAL_ARENA_RUNNER_CMD"] = _join(
                [sys.executable, runner, "{manifest_path}"])
            adapter = arena_runner.adapter_for_spec(_spec())
            assert adapter is not None
            out = adapter(_cnf(), 1000.0)
            assert out.claimed == Outcome.SAT
            assert out.witness == [1]
            results = run_batch(adapter, [Instance("tiny", _cnf(), 1000.0)])
            assert len(results) == 1
            assert results[0].solved is True
            assert results[0].cert_ok is True
        finally:
            _remove(runner)


def test_runner_failure_is_non_solve() -> None:
    with _clean_env():
        runner = _script(
            "import sys\n"
            "print('wrapper failed before solver output')\n"
            "sys.exit(17)\n")
        try:
            adapter = arena_runner.command_runner_adapter(
                _spec(), _join([sys.executable, runner, "{cnf_path}"]),
                contain=False)
            out = adapter(_cnf(), 1000.0)
            assert out.claimed == Outcome.TIMEOUT
            assert out.run.returncode == 17
            results = run_batch(adapter, [Instance("fail", _cnf(), 1000.0)])
            assert results[0].outcome == Outcome.TIMEOUT
            assert results[0].reason == "solver_abstained"
        finally:
            _remove(runner)


def test_spawn_failure_is_non_solve() -> None:
    adapter = arena_runner.command_runner_adapter(
        _spec(), _join(["/definitely/not/a/runner", "{cnf_path}"]),
        contain=False)
    out = adapter(_cnf(), 1000.0)
    assert out.claimed == Outcome.TIMEOUT
    assert out.run.returncode is None
    assert "spawn_failed" in out.run.stderr


def test_runner_timeout_is_host_observed() -> None:
    runner = _script(
        "import time\n"
        "time.sleep(2)\n"
        "print('s SATISFIABLE')\n"
        "print('v 1 0')\n")
    try:
        adapter = arena_runner.command_runner_adapter(
            _spec(), _join([sys.executable, runner, "{cnf_path}"]),
            contain=False)
        out = adapter(_cnf(), 50.0)
        assert out.claimed == Outcome.TIMEOUT
        assert out.run.timed_out is True
        results = run_batch(adapter, [Instance("slow", _cnf(), 50.0)])
        assert results[0].outcome == Outcome.TIMEOUT
        assert results[0].reason == "host_observed_timeout"
    finally:
        _remove(runner)


def test_unsafe_direct_miner_placeholder_rejected() -> None:
    runner = _script("print('should not run')\n")
    try:
        unsafe = _join([sys.executable, runner, "{source_url}", "{cnf_path}"])
        try:
            arena_runner.command_runner_adapter(_spec(), unsafe, contain=False)
            raise AssertionError("unsafe direct source_url was accepted")
        except ValueError as e:
            assert "unsafe_miner_placeholder_before_terminator" in str(e)

        with _clean_env():
            os.environ["CATHEDRAL_ARENA_REQUIRE_CONTAINMENT"] = "0"
            os.environ["CATHEDRAL_ARENA_RUNNER_CMD"] = unsafe
            assert arena_runner.adapter_for_spec(_spec()) is None
    finally:
        _remove(runner)


def test_option_like_miner_values_after_terminator() -> None:
    runner = _script(
        "import sys\n"
        "assert sys.argv[1] == '--'\n"
        "assert sys.argv[2] == '--network=host https://example/solver.tgz'\n"
        "assert sys.argv[3] == '--digest-looking-value'\n"
        "assert sys.argv[4] == '--owner-looking-hotkey'\n"
        "assert open(sys.argv[5]).read().startswith('p cnf 1 1')\n"
        "print('s SATISFIABLE')\n"
        "print('v 1 0')\n")
    try:
        adapter = arena_runner.command_runner_adapter(
            _spec(),
            _join([
                sys.executable, runner, "--", "{source_url}",
                "{container_digest}", "{owner_hotkey}", "{cnf_path}",
            ]),
            contain=False,
        )
        out = adapter(_cnf(), 1000.0)
        assert out.claimed == Outcome.SAT
        assert out.witness == [1]
    finally:
        _remove(runner)


def test_unknown_placeholder_rejected() -> None:
    runner = _script("print('should not run')\n")
    try:
        try:
            arena_runner.command_runner_adapter(
                _spec(), _join([sys.executable, runner, "{bogus}"]),
                contain=False)
            raise AssertionError("unknown placeholder was accepted")
        except ValueError as e:
            assert "unsupported_runner_placeholder:bogus" in str(e)
    finally:
        _remove(runner)


def main() -> None:
    tests = [
        test_default_off,
        test_manifest_runner_success,
        test_runner_failure_is_non_solve,
        test_spawn_failure_is_non_solve,
        test_runner_timeout_is_host_observed,
        test_unsafe_direct_miner_placeholder_rejected,
        test_option_like_miner_values_after_terminator,
        test_unknown_placeholder_rejected,
    ]
    for test in tests:
        test()
    print(f"arena_runner_verify: ok ({len(tests)} checks)")


if __name__ == "__main__":
    main()
