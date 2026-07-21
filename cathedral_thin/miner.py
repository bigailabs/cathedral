"""Reference Axon miner: validate a private CNF, run a local solver, respond."""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict, deque
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import bittensor as bt

from .bt_compat import listify, make_axon, make_subtensor, make_wallet
from .core import (
    ThinSubnetError,
    decode_cnf,
    encode_assignment,
    validate_challenge_envelope,
)
from .protocol import SatChallenge


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_dimacs_model(output: str, *, n_vars: int) -> list[int]:
    status: str | None = None
    literals: list[int] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("c"):
            continue
        if line.startswith("s "):
            upper = line.upper()
            if "UNSATISFIABLE" in upper:
                status = "unsat"
            elif "SATISFIABLE" in upper:
                status = "sat"
            continue
        if line == "SAT":
            status = "sat"
            continue
        if line == "UNSAT":
            status = "unsat"
            continue
        if line.startswith("v "):
            for token in line[2:].split():
                lit = int(token)
                if lit != 0:
                    literals.append(lit)
    if status != "sat":
        raise ThinSubnetError("solver did not report SAT")
    # encode_assignment performs the strict completeness/range/duplicate check.
    _ = encode_assignment(literals, n_vars=n_vars)
    return literals


def run_external_solver(
    command: str, *, dimacs: str, timeout_secs: float, n_vars: int
) -> tuple[list[int], str]:
    tokens = shlex.split(command)
    if not tokens:
        raise ThinSubnetError("solver command is empty")
    executable = tokens[0]
    if not os.path.isabs(executable) and shutil.which(executable) is None:
        raise ThinSubnetError(f"solver executable not found: {executable}")
    with tempfile.TemporaryDirectory(prefix="cathedral-thin-miner-") as tmpdir:
        cnf_path = os.path.join(tmpdir, "challenge.cnf")
        with open(cnf_path, "x", encoding="ascii") as handle:
            handle.write(dimacs)
        replaced = False
        argv: list[str] = []
        for token in tokens:
            if "{cnf}" in token:
                token = token.replace("{cnf}", cnf_path)
                replaced = True
            argv.append(token)
        if not replaced:
            argv.append(cnf_path)
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=max(0.1, timeout_secs),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ThinSubnetError("solver timeout") from exc
    if result.returncode not in (0, 10):
        raise ThinSubnetError(f"solver exited {result.returncode}")
    return parse_dimacs_model(result.stdout, n_vars=n_vars), os.path.basename(
        executable
    )


@dataclass(frozen=True)
class MinerConfig:
    hotkey: str
    solver_command: str
    solver_timeout_secs: float
    max_vars: int
    max_clauses: int


@dataclass(frozen=True)
class CachedResponse:
    challenge_id: str
    cnf_sha256: str
    assignment_b64: str
    solver_name: str
    error: str


class MinerService:
    def __init__(
        self,
        config: MinerConfig,
        *,
        max_concurrency: int = 1,
        response_cache_size: int = 4096,
    ):
        if max_concurrency <= 0 or response_cache_size <= 0:
            raise ValueError(
                "miner concurrency and response cache size must be positive"
            )
        self.config = config
        self._slots = asyncio.Semaphore(max_concurrency)
        self._cache_size = response_cache_size
        self._round_cache: OrderedDict[tuple[str, int], CachedResponse | None] = (
            OrderedDict()
        )
        self._cache_lock = asyncio.Lock()

    async def _reserve_round(
        self, synapse: SatChallenge, caller_hotkey: str
    ) -> CachedResponse | None:
        key = (caller_hotkey, synapse.round_id)
        async with self._cache_lock:
            if key in self._round_cache:
                cached = self._round_cache[key]
                self._round_cache.move_to_end(key)
                if cached is None:
                    raise ThinSubnetError("challenge already in progress")
                if cached.cnf_sha256 != synapse.cnf_sha256:
                    raise ThinSubnetError(
                        "validator already used a different formula this round"
                    )
                return cached
            self._round_cache[key] = None
            while len(self._round_cache) > self._cache_size:
                self._round_cache.popitem(last=False)
            return None

    async def _complete_round(
        self, synapse: SatChallenge, caller_hotkey: str, response: CachedResponse
    ) -> None:
        async with self._cache_lock:
            self._round_cache[(caller_hotkey, synapse.round_id)] = response
            self._round_cache.move_to_end((caller_hotkey, synapse.round_id))
            while len(self._round_cache) > self._cache_size:
                self._round_cache.popitem(last=False)

    @staticmethod
    def _apply_cached(synapse: SatChallenge, cached: CachedResponse) -> None:
        synapse.assignment_b64 = cached.assignment_b64
        synapse.solver_name = cached.solver_name
        synapse.error = cached.error

    async def solve(self, synapse: SatChallenge, *, caller_hotkey: str) -> SatChallenge:
        started = time.monotonic()
        reserved = False
        try:
            validate_challenge_envelope(
                synapse.model_dump(),
                now_ms=now_ms(),
                expected_miner=self.config.hotkey,
                expected_validator=caller_hotkey,
            )
            if (
                synapse.n_vars > self.config.max_vars
                or synapse.n_clauses > self.config.max_clauses
            ):
                raise ThinSubnetError("challenge exceeds configured miner bounds")
            remaining = (synapse.expires_at_ms - now_ms()) / 1000.0
            if remaining <= 0:
                raise ThinSubnetError("challenge expired")
            cached = await self._reserve_round(synapse, caller_hotkey)
            if cached is not None:
                self._apply_cached(synapse, cached)
                synapse.miner_elapsed_ms = round(
                    (time.monotonic() - started) * 1000.0, 3
                )
                return synapse
            reserved = True
            cnf = decode_cnf(
                synapse.cnf_b64,
                expected_sha256=synapse.cnf_sha256,
                expected_vars=synapse.n_vars,
                expected_clauses=synapse.n_clauses,
            )
            timeout = min(self.config.solver_timeout_secs, remaining)
            async with self._slots:
                assignment, solver_name = await asyncio.to_thread(
                    run_external_solver,
                    self.config.solver_command,
                    dimacs=cnf.to_dimacs(),
                    timeout_secs=timeout,
                    n_vars=cnf.n_vars,
                )
            synapse.assignment_b64 = encode_assignment(assignment, n_vars=cnf.n_vars)
            synapse.solver_name = solver_name
            synapse.error = ""
        except (ThinSubnetError, OSError, ValueError) as exc:
            synapse.assignment_b64 = ""
            synapse.solver_name = ""
            synapse.error = str(exc)[:240]
        if reserved:
            await self._complete_round(
                synapse,
                caller_hotkey,
                CachedResponse(
                    challenge_id=synapse.challenge_id,
                    cnf_sha256=synapse.cnf_sha256,
                    assignment_b64=synapse.assignment_b64,
                    solver_name=synapse.solver_name,
                    error=synapse.error,
                ),
            )
        synapse.miner_elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
        return synapse

    async def forward(self, synapse: SatChallenge) -> SatChallenge:
        caller = str(getattr(getattr(synapse, "dendrite", None), "hotkey", "") or "")
        return await self.solve(synapse, caller_hotkey=caller)


class PermitAccess:
    def __init__(
        self,
        *,
        subtensor: Any,
        netuid: int,
        miner_hotkey: str,
        requests_per_minute: int = 4,
        window_secs: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if requests_per_minute <= 0 or window_secs <= 0:
            raise ValueError("rate limit and window must be positive")
        self.subtensor = subtensor
        self.netuid = netuid
        self.miner_hotkey = miner_hotkey
        self.requests_per_minute = requests_per_minute
        self.window_secs = window_secs
        self.clock = clock
        self.allowed: set[str] = set()
        self._requests: dict[str, deque[float]] = {}
        self._request_lock = threading.Lock()

    def refresh(self) -> None:
        metagraph = self.subtensor.metagraph(self.netuid, lite=True)
        hotkeys = [str(value) for value in listify(metagraph.hotkeys)]
        permits = [bool(value) for value in listify(metagraph.validator_permit)]
        if len(hotkeys) != len(permits):
            raise RuntimeError("metagraph hotkey/permit length mismatch")
        allowed = {hotkey for hotkey, permit in zip(hotkeys, permits) if permit}
        with self._request_lock:
            self.allowed = allowed
            self._requests = {
                hotkey: attempts
                for hotkey, attempts in self._requests.items()
                if hotkey in self.allowed
            }

    def refresh_preserving_snapshot(self) -> bool:
        """Refresh permits, retaining the last complete snapshot on RPC failure."""
        try:
            self.refresh()
        except Exception as exc:
            print(
                "validator permit refresh held previous snapshot "
                f"exception={type(exc).__name__}"
            )
            return False
        return True

    def _rate_limited(self, caller: str) -> bool:
        timestamp = self.clock()
        cutoff = timestamp - self.window_secs
        with self._request_lock:
            attempts = self._requests.setdefault(caller, deque())
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self.requests_per_minute:
                return True
            attempts.append(timestamp)
            return False

    async def blacklist(self, synapse: SatChallenge) -> tuple[bool, str]:
        caller = str(getattr(getattr(synapse, "dendrite", None), "hotkey", "") or "")
        if synapse.miner_hotkey != self.miner_hotkey:
            return True, "wrong_miner_target"
        if not caller or caller not in self.allowed:
            return True, "validator_permit_required"
        if synapse.validator_hotkey != caller:
            return True, "validator_identity_mismatch"
        if self._rate_limited(caller):
            return True, "validator_rate_limited"
        return False, "allowed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the owner-infrastructure-free Cathedral SAT miner"
    )
    parser.add_argument("--network", default=os.environ.get("BT_NETWORK", "finney"))
    parser.add_argument(
        "--netuid", type=int, default=int(os.environ.get("BT_NETUID", "39"))
    )
    parser.add_argument(
        "--wallet-name", default=os.environ.get("BT_WALLET_NAME", "miner")
    )
    parser.add_argument(
        "--wallet-hotkey", default=os.environ.get("BT_WALLET_HOTKEY", "default")
    )
    parser.add_argument("--wallet-path", default=os.environ.get("BT_WALLET_PATH", ""))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("BT_AXON_PORT", "8091"))
    )
    parser.add_argument("--external-ip", default=os.environ.get("BT_EXTERNAL_IP", ""))
    parser.add_argument(
        "--external-port",
        type=int,
        default=int(os.environ.get("BT_EXTERNAL_PORT", "0")),
    )
    parser.add_argument(
        "--solver-command",
        default=os.environ.get("CATHEDRAL_SOLVER_COMMAND", "kissat {cnf}"),
    )
    parser.add_argument(
        "--solver-timeout",
        type=float,
        default=float(os.environ.get("CATHEDRAL_SOLVER_TIMEOUT", "45")),
    )
    parser.add_argument(
        "--max-vars",
        type=int,
        default=int(os.environ.get("CATHEDRAL_MAX_VARS", "4096")),
    )
    parser.add_argument(
        "--max-clauses",
        type=int,
        default=int(os.environ.get("CATHEDRAL_MAX_CLAUSES", "20000")),
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=int(os.environ.get("CATHEDRAL_MINER_CONCURRENCY", "1")),
    )
    parser.add_argument(
        "--response-cache-size",
        type=int,
        default=int(os.environ.get("CATHEDRAL_RESPONSE_CACHE_SIZE", "4096")),
    )
    parser.add_argument(
        "--validator-rpm",
        type=int,
        default=int(os.environ.get("CATHEDRAL_VALIDATOR_RPM", "4")),
    )
    parser.add_argument("--refresh-secs", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.netuid < 0
        or args.port <= 0
        or args.max_concurrency <= 0
        or args.response_cache_size <= 0
        or args.validator_rpm <= 0
    ):
        raise SystemExit("invalid netuid, port, concurrency, cache, or rate limit")
    wallet = make_wallet(
        bt,
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=args.wallet_path or None,
    )
    subtensor = make_subtensor(bt, network=args.network)
    hotkey = str(wallet.hotkey.ss58_address)
    service = MinerService(
        MinerConfig(
            hotkey=hotkey,
            solver_command=args.solver_command,
            solver_timeout_secs=args.solver_timeout,
            max_vars=args.max_vars,
            max_clauses=args.max_clauses,
        ),
        max_concurrency=args.max_concurrency,
        response_cache_size=args.response_cache_size,
    )
    access = PermitAccess(
        subtensor=subtensor,
        netuid=args.netuid,
        miner_hotkey=hotkey,
        requests_per_minute=args.validator_rpm,
    )
    access.refresh()
    axon = make_axon(
        bt,
        wallet=wallet,
        port=args.port,
        external_ip=args.external_ip or None,
        external_port=args.external_port or None,
        max_workers=max(2, args.max_concurrency + 1),
    )
    axon.attach(forward_fn=service.forward, blacklist_fn=access.blacklist)
    axon.serve(netuid=args.netuid, subtensor=subtensor).start()
    print(
        f"cathedral-thin miner serving hotkey={hotkey} netuid={args.netuid} port={args.port}"
    )
    try:
        while True:
            time.sleep(max(30.0, args.refresh_secs))
            access.refresh_preserving_snapshot()
    except KeyboardInterrupt:
        axon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
