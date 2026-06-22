"""Production resolver for Lane S solver execution.

The arena engine deliberately accepts an `adapter_for(spec)` callback. This
module is the safe production resolver for that seam:

- no env config -> no adapter, solver stays pending;
- `CATHEDRAL_ARENA_SOLVER_BIN` -> run a local installed solver binary;
- `CATHEDRAL_ARENA_RUNNER_CMD` -> run an operator-owned wrapper command that can
  pull/run the pinned container digest and emit DIMACS solver output.

The command path is intentionally operator-owned. Cathedral should not build a
Docker string from miner data inside the publisher. Use placeholders for miner
fields and let the wrapper enforce pull policy, digest pinning, and cache rules.
The safest interface is `{manifest_path}`: miner-controlled fields are written to
a JSON manifest and passed as data instead of option-like argv tokens.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
from pathlib import Path

from ..contract import Outcome
from ..lanes import sandbox
from ..lanes.solver_arena import AdapterOutput, SolverAdapter, SolverSpec, real_solver_adapter


def adapter_for_spec(spec: SolverSpec) -> SolverAdapter | None:
    """Resolve a production adapter for a registered solver, or None.

    Default is fail-closed: if no runner is configured, or containment is required
    but unavailable, the solver remains pending and no record-fall can happen.
    """
    require_containment = _truthy(os.environ.get(
        "CATHEDRAL_ARENA_REQUIRE_CONTAINMENT", "1"))
    if require_containment and not sandbox.sandbox_available():
        return None

    runner_cmd = os.environ.get("CATHEDRAL_ARENA_RUNNER_CMD", "").strip()
    if runner_cmd:
        try:
            return command_runner_adapter(
                spec, runner_cmd, contain=require_containment)
        except ValueError:
            return None

    solver_bin = os.environ.get("CATHEDRAL_ARENA_SOLVER_BIN", "").strip()
    if solver_bin:
        return real_solver_adapter(solver_bin)

    return None


def command_runner_adapter(
    spec: SolverSpec,
    command: str,
    *,
    contain: bool,
) -> SolverAdapter:
    """Build an adapter around an operator-supplied runner command.

    Supported placeholders:
      `{cnf_path}`, `{timeout_ms}`, `{timeout_s}`, `{container_digest}`,
      `{source_url}`, `{source_sha256}`, `{owner_hotkey}`, `{manifest_path}`.

    Prefer `{manifest_path}` for miner-controlled data. Direct miner placeholders
    are accepted only after a literal `--` argument, unless embedded inside an
    operator-owned option token such as `--digest={container_digest}`.

    If no placeholders are present, the CNF path is appended as the final arg.
    """
    _validate_runner_command(command)

    def _adapt(cnf: str, timeout_ms: float) -> AdapterOutput:
        timeout_s = max(0.001, timeout_ms / 1000.0)
        with tempfile.TemporaryDirectory() as d:
            cnf_path = Path(d) / "problem.cnf"
            cnf_path.write_text(cnf)
            manifest_path = Path(d) / "runner_manifest.json"
            _write_manifest(
                manifest_path,
                spec=spec,
                cnf_path=cnf_path,
                cnf=cnf,
                timeout_ms=timeout_ms,
                timeout_s=timeout_s,
            )
            argv = _runner_argv(
                command,
                cnf_path=str(cnf_path),
                timeout_ms=str(int(timeout_ms)),
                timeout_s=f"{timeout_s:.6f}",
                container_digest=spec.container_digest,
                source_url=spec.source_url,
                source_sha256=spec.source_sha256,
                owner_hotkey=spec.owner_hotkey,
                manifest_path=str(manifest_path),
            )
            run = sandbox.run_solver(argv, timeout_s=timeout_s, contain=contain)
        return _parse_solver_output(run)

    return _adapt


_PLACEHOLDERS = {
    "cnf_path",
    "timeout_ms",
    "timeout_s",
    "container_digest",
    "source_url",
    "source_sha256",
    "owner_hotkey",
    "manifest_path",
}
_MINER_PLACEHOLDERS = {
    "container_digest",
    "source_url",
    "source_sha256",
    "owner_hotkey",
}
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _validate_runner_command(command: str) -> None:
    argv = shlex.split(command, posix=(os.name != "nt"))
    if not argv:
        raise ValueError("empty_runner_command")

    names = set(_PLACEHOLDER_RE.findall(command))
    unknown = sorted(names - _PLACEHOLDERS)
    if unknown:
        raise ValueError(f"unsupported_runner_placeholder:{','.join(unknown)}")

    after_terminator = False
    for token in argv:
        if token == "--":
            after_terminator = True
            continue
        found = set(_PLACEHOLDER_RE.findall(token)) & _MINER_PLACEHOLDERS
        if not found:
            continue
        # A bare `{source_url}` before `--` can turn a miner value like
        # `--network=host` into an option consumed by the wrapper. Embedded forms
        # such as `--source={source_url}` remain one operator-owned argv token.
        if token in {f"{{{name}}}" for name in found} and not after_terminator:
            raise ValueError(
                f"unsafe_miner_placeholder_before_terminator:{token}")


def _write_manifest(
    path: Path,
    *,
    spec: SolverSpec,
    cnf_path: Path,
    cnf: str,
    timeout_ms: float,
    timeout_s: float,
) -> None:
    manifest = {
        "schema_version": 1,
        "cnf_path": str(cnf_path),
        "cnf_sha256": hashlib.sha256(cnf.encode()).hexdigest(),
        "timeout_ms": int(timeout_ms),
        "timeout_s": round(timeout_s, 6),
        "solver": {
            "source_url": spec.source_url,
            "container_digest": spec.container_digest,
            "source_sha256": spec.source_sha256,
            "owner_hotkey": spec.owner_hotkey,
            "commitment_id": spec.commitment_id,
        },
    }
    path.write_text(json.dumps(manifest, sort_keys=True))


def _runner_argv(command: str, **values: str) -> list[str]:
    _validate_runner_command(command)
    names = set(_PLACEHOLDER_RE.findall(command))
    unknown = sorted(names - set(values))
    if unknown:
        raise ValueError(f"missing_runner_placeholder_value:{','.join(unknown)}")
    has_placeholder = bool(names)
    argv = shlex.split(command, posix=(os.name != "nt"))
    if has_placeholder:
        allowed = "|".join(re.escape(k) for k in values)
        pat = re.compile(r"\{(" + allowed + r")\}")
        argv = [pat.sub(lambda m: values[m.group(1)], token) for token in argv]
    if not has_placeholder:
        argv.append(values["cnf_path"])
    return argv


def _parse_solver_output(run: sandbox.RunResult) -> AdapterOutput:
    if run.timed_out:
        return AdapterOutput(Outcome.TIMEOUT, [], "", run)
    model: list[int] = []
    sat = unsat = False
    for line in run.stdout.splitlines():
        if line.startswith("s ") and "UNSATISFIABLE" in line:
            unsat = True
        elif line.startswith("s ") and "SATISFIABLE" in line:
            sat = True
        elif line.startswith("v "):
            for raw in line[2:].split():
                if raw.lstrip("-").isdigit():
                    lit = int(raw)
                    if lit != 0:
                        model.append(lit)
    if sat:
        return AdapterOutput(Outcome.SAT, model, "", run)
    if unsat:
        return AdapterOutput(Outcome.UNSAT, [], run.stdout, run)
    return AdapterOutput(Outcome.TIMEOUT, [], "", run)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["adapter_for_spec", "command_runner_adapter"]
