"""Pure protocol, verification, scoring, and recovery primitives.

This module intentionally has no Bittensor or network imports.  The miner and
validator runtimes are thin adapters around these deterministic functions.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import math
import os
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_VERSION = 1
STATE_SCHEMA = 5
MAX_COMPRESSED_CNF_BYTES = 512_000
MAX_DIMACS_BYTES = 4_000_000


class ThinSubnetError(ValueError):
    """A fail-closed protocol, configuration, or state error."""


class HashRng:
    """Small HMAC-SHA256 counter PRNG.

    Mersenne Twister is deliberately avoided: a CNF exposes enough random
    choices that a non-cryptographic generator can leak future choices and the
    hidden planted assignment.
    """

    def __init__(self, key: bytes):
        if len(key) < 32:
            raise ThinSubnetError("rng key must contain at least 256 bits")
        self._key = key
        self._counter = 0
        self._buffer = bytearray()

    def _fill(self, count: int) -> None:
        while len(self._buffer) < count:
            block = hmac.new(
                self._key,
                b"cathedral-thin-rng-v1\x00" + self._counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest()
            self._counter += 1
            self._buffer.extend(block)

    def bytes(self, count: int) -> bytes:
        if count < 0:
            raise ThinSubnetError("negative byte request")
        self._fill(count)
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ThinSubnetError("randbelow upper bound must be positive")
        nbytes = max(1, (upper.bit_length() + 7) // 8)
        limit = (1 << (8 * nbytes)) - ((1 << (8 * nbytes)) % upper)
        while True:
            value = int.from_bytes(self.bytes(nbytes), "big")
            if value < limit:
                return value % upper

    def bit(self) -> bool:
        return bool(self.randbelow(2))

    def sample3(self, upper: int) -> tuple[int, int, int]:
        if upper < 3:
            raise ThinSubnetError("3-SAT needs at least three variables")
        a = self.randbelow(upper)
        b = self.randbelow(upper - 1)
        if b >= a:
            b += 1
        low, high = sorted((a, b))
        c = self.randbelow(upper - 2)
        if c >= low:
            c += 1
        if c >= high:
            c += 1
        return a + 1, b + 1, c + 1


@dataclass(frozen=True)
class Cnf:
    n_vars: int
    clauses: tuple[tuple[int, int, int], ...]

    @property
    def n_clauses(self) -> int:
        return len(self.clauses)

    def to_dimacs(self) -> str:
        body = "".join(f"{a} {b} {c} 0\n" for a, b, c in self.clauses)
        return f"p cnf {self.n_vars} {self.n_clauses}\n" + body


def derive_challenge_seed(
    master_secret: bytes,
    *,
    netuid: int,
    validator_hotkey: str,
    miner_hotkey: str,
    round_id: int,
    slot: int = 0,
) -> bytes:
    if len(master_secret) != 32:
        raise ThinSubnetError("master secret must be exactly 32 bytes")
    fields = [
        "cathedral-thin-challenge-v1",
        str(netuid),
        validator_hotkey,
        miner_hotkey,
        str(round_id),
        str(slot),
    ]
    if not validator_hotkey or not miner_hotkey or round_id < 0 or slot < 0:
        raise ThinSubnetError("invalid challenge derivation fields")
    return hmac.new(
        master_secret, "\x00".join(fields).encode(), hashlib.sha256
    ).digest()


def generate_ajm_cnf(
    seed: bytes, *, n_vars: int, n_clauses: int
) -> tuple[Cnf, tuple[int, ...]]:
    """Generate two-hidden-assignment 3-SAT without literal-frequency bias.

    The planted model is returned only so tests can prove generation. Runtime
    callers discard it and verify the miner's independently supplied witness.
    """
    if not 3 <= n_vars <= 100_000:
        raise ThinSubnetError("n_vars outside safe bounds")
    if not 1 <= n_clauses <= 1_000_000:
        raise ThinSubnetError("n_clauses outside safe bounds")
    rng = HashRng(seed)
    model = [False] + [rng.bit() for _ in range(n_vars)]
    clauses: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    attempts = 0
    max_attempts = max(10_000, n_clauses * 32)
    while len(clauses) < n_clauses:
        attempts += 1
        if attempts > max_attempts:
            raise ThinSubnetError("could not generate enough unique AJM clauses")
        variables = sorted(rng.sample3(n_vars))
        literals = tuple(v if rng.bit() else -v for v in variables)
        true_count = sum((lit > 0) == model[abs(lit)] for lit in literals)
        if true_count not in (1, 2) or literals in seen:
            continue
        seen.add(literals)
        clauses.append(literals)
    planted = tuple(i if model[i] else -i for i in range(1, n_vars + 1))
    return Cnf(n_vars=n_vars, clauses=tuple(clauses)), planted


def parse_dimacs_strict(
    text: str, *, expected_vars: int | None = None, expected_clauses: int | None = None
) -> Cnf:
    n_vars: int | None = None
    declared_clauses: int | None = None
    clauses: list[tuple[int, int, int]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("c"):
            continue
        if line.startswith("p "):
            if n_vars is not None:
                raise ThinSubnetError("multiple DIMACS headers")
            parts = line.split()
            if len(parts) != 4 or parts[1] != "cnf":
                raise ThinSubnetError("invalid DIMACS header")
            n_vars, declared_clauses = int(parts[2]), int(parts[3])
            continue
        if n_vars is None:
            raise ThinSubnetError("DIMACS clause precedes header")
        try:
            tokens = [int(token) for token in line.split()]
        except ValueError as exc:
            raise ThinSubnetError("non-integer DIMACS token") from exc
        if len(tokens) != 4 or tokens[-1] != 0:
            raise ThinSubnetError(
                "only one terminated 3-SAT clause per line is accepted"
            )
        clause = tuple(tokens[:3])
        if len({abs(lit) for lit in clause}) != 3:
            raise ThinSubnetError("clause variables must be distinct")
        if any(lit == 0 or abs(lit) > n_vars for lit in clause):
            raise ThinSubnetError("literal outside declared variable range")
        clauses.append(clause)  # type: ignore[arg-type]
    if n_vars is None or declared_clauses is None:
        raise ThinSubnetError("missing DIMACS header")
    if len(clauses) != declared_clauses:
        raise ThinSubnetError("DIMACS clause count mismatch")
    if expected_vars is not None and n_vars != expected_vars:
        raise ThinSubnetError("DIMACS variable count mismatch")
    if expected_clauses is not None and len(clauses) != expected_clauses:
        raise ThinSubnetError("DIMACS expected clause count mismatch")
    return Cnf(n_vars=n_vars, clauses=tuple(clauses))


def encode_cnf(cnf: Cnf) -> tuple[str, str]:
    raw = cnf.to_dimacs().encode("ascii")
    if len(raw) > MAX_DIMACS_BYTES:
        raise ThinSubnetError("DIMACS payload exceeds limit")
    compressed = zlib.compress(raw, level=6)
    if len(compressed) > MAX_COMPRESSED_CNF_BYTES:
        raise ThinSubnetError("compressed CNF exceeds limit")
    return base64.b64encode(compressed).decode("ascii"), hashlib.sha256(raw).hexdigest()


def decode_cnf(
    payload_b64: str,
    *,
    expected_sha256: str,
    expected_vars: int,
    expected_clauses: int,
    max_dimacs_bytes: int = MAX_DIMACS_BYTES,
) -> Cnf:
    if len(payload_b64) > ((MAX_COMPRESSED_CNF_BYTES + 2) // 3) * 4 + 8:
        raise ThinSubnetError("base64 CNF exceeds limit")
    try:
        compressed = base64.b64decode(payload_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ThinSubnetError("invalid base64 CNF") from exc
    if len(compressed) > MAX_COMPRESSED_CNF_BYTES:
        raise ThinSubnetError("compressed CNF exceeds limit")
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(compressed, max_dimacs_bytes + 1)
    except zlib.error as exc:
        raise ThinSubnetError("invalid compressed CNF") from exc
    if (
        len(raw) > max_dimacs_bytes
        or inflater.unconsumed_tail
        or not inflater.eof
        or inflater.unused_data
    ):
        raise ThinSubnetError("decompressed CNF exceeds limit or has trailing data")
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256.lower()):
        raise ThinSubnetError("CNF digest mismatch")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ThinSubnetError("DIMACS must be ASCII") from exc
    return parse_dimacs_strict(
        text,
        expected_vars=expected_vars,
        expected_clauses=expected_clauses,
    )


def verify_witness(cnf: Cnf, assignment: Iterable[int]) -> bool:
    values: dict[int, bool] = {}
    for raw_lit in assignment:
        if isinstance(raw_lit, bool) or not isinstance(raw_lit, int) or raw_lit == 0:
            return False
        var = abs(raw_lit)
        if var > cnf.n_vars or var in values:
            return False
        values[var] = raw_lit > 0
    if len(values) != cnf.n_vars or any(
        v not in values for v in range(1, cnf.n_vars + 1)
    ):
        return False
    return all(
        any(values[abs(lit)] == (lit > 0) for lit in clause) for clause in cnf.clauses
    )


def encode_assignment(assignment: Iterable[int], *, n_vars: int) -> str:
    values: dict[int, bool] = {}
    for raw_lit in assignment:
        if isinstance(raw_lit, bool) or not isinstance(raw_lit, int) or raw_lit == 0:
            raise ThinSubnetError("invalid assignment literal")
        var = abs(raw_lit)
        if var > n_vars or var in values:
            raise ThinSubnetError("duplicate or out-of-range assignment literal")
        values[var] = raw_lit > 0
    if len(values) != n_vars:
        raise ThinSubnetError("assignment must be complete")
    packed = bytearray((n_vars + 7) // 8)
    for var in range(1, n_vars + 1):
        if values[var]:
            packed[(var - 1) // 8] |= 1 << ((var - 1) % 8)
    return base64.b64encode(bytes(packed)).decode("ascii")


def decode_assignment(payload_b64: str, *, n_vars: int) -> tuple[int, ...]:
    expected_bytes = (n_vars + 7) // 8
    if len(payload_b64) > ((expected_bytes + 2) // 3) * 4 + 4:
        raise ThinSubnetError("assignment payload exceeds limit")
    try:
        packed = base64.b64decode(payload_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ThinSubnetError("invalid base64 assignment") from exc
    if len(packed) != expected_bytes:
        raise ThinSubnetError("assignment bitset length mismatch")
    if n_vars % 8 and packed and packed[-1] >> (n_vars % 8):
        raise ThinSubnetError("non-zero assignment padding bits")
    return tuple(
        var if packed[(var - 1) // 8] & (1 << ((var - 1) % 8)) else -var
        for var in range(1, n_vars + 1)
    )


@dataclass(frozen=True)
class Challenge:
    protocol_version: int
    challenge_id: str
    validator_hotkey: str
    miner_hotkey: str
    netuid: int
    round_id: int
    issued_at_ms: int
    expires_at_ms: int
    n_vars: int
    n_clauses: int
    cnf_b64: str
    cnf_sha256: str


def _challenge_commitment(fields: dict[str, Any]) -> str:
    body = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"cathedral-thin-envelope-v1\x00" + body).hexdigest()


def challenge_public_fields(challenge: Challenge | dict[str, Any]) -> dict[str, Any]:
    get = (
        challenge.get
        if isinstance(challenge, dict)
        else lambda key: getattr(challenge, key)
    )
    return {
        "protocol_version": int(get("protocol_version")),
        "validator_hotkey": str(get("validator_hotkey")),
        "miner_hotkey": str(get("miner_hotkey")),
        "netuid": int(get("netuid")),
        "round_id": int(get("round_id")),
        "issued_at_ms": int(get("issued_at_ms")),
        "expires_at_ms": int(get("expires_at_ms")),
        "n_vars": int(get("n_vars")),
        "n_clauses": int(get("n_clauses")),
        "cnf_sha256": str(get("cnf_sha256")),
    }


def validate_challenge_envelope(
    challenge: Challenge | dict[str, Any],
    *,
    now_ms: int,
    expected_miner: str | None = None,
    expected_validator: str | None = None,
) -> None:
    fields = challenge_public_fields(challenge)
    get = (
        challenge.get
        if isinstance(challenge, dict)
        else lambda key: getattr(challenge, key)
    )
    if fields["protocol_version"] != PROTOCOL_VERSION:
        raise ThinSubnetError("unsupported protocol version")
    if not fields["validator_hotkey"] or not fields["miner_hotkey"]:
        raise ThinSubnetError("missing challenge identity")
    if expected_miner is not None and fields["miner_hotkey"] != expected_miner:
        raise ThinSubnetError("challenge targets another miner")
    if (
        expected_validator is not None
        and fields["validator_hotkey"] != expected_validator
    ):
        raise ThinSubnetError("challenge claims another validator")
    if fields["expires_at_ms"] <= fields["issued_at_ms"]:
        raise ThinSubnetError("invalid challenge expiry")
    if now_ms > fields["expires_at_ms"]:
        raise ThinSubnetError("challenge expired")
    if fields["issued_at_ms"] > now_ms + 30_000:
        raise ThinSubnetError("challenge issue time is in the future")
    if not 3 <= fields["n_vars"] <= 100_000:
        raise ThinSubnetError("challenge variable count outside safe bounds")
    if not 1 <= fields["n_clauses"] <= 1_000_000:
        raise ThinSubnetError("challenge clause count outside safe bounds")
    digest = fields["cnf_sha256"]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
        raise ThinSubnetError("invalid CNF digest")
    expected_id = _challenge_commitment(fields)
    if not hmac.compare_digest(str(get("challenge_id")), expected_id):
        raise ThinSubnetError("challenge commitment mismatch")


def build_challenge(
    master_secret: bytes,
    *,
    netuid: int,
    validator_hotkey: str,
    miner_hotkey: str,
    round_id: int,
    issued_at_ms: int,
    ttl_ms: int,
    n_vars: int,
    n_clauses: int,
    slot: int = 0,
) -> tuple[Challenge, Cnf]:
    if ttl_ms <= 0:
        raise ThinSubnetError("challenge ttl must be positive")
    seed = derive_challenge_seed(
        master_secret,
        netuid=netuid,
        validator_hotkey=validator_hotkey,
        miner_hotkey=miner_hotkey,
        round_id=round_id,
        slot=slot,
    )
    cnf, _planted = generate_ajm_cnf(seed, n_vars=n_vars, n_clauses=n_clauses)
    cnf_b64, cnf_sha256 = encode_cnf(cnf)
    fields = {
        "protocol_version": PROTOCOL_VERSION,
        "validator_hotkey": validator_hotkey,
        "miner_hotkey": miner_hotkey,
        "netuid": netuid,
        "round_id": round_id,
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": issued_at_ms + ttl_ms,
        "n_vars": n_vars,
        "n_clauses": n_clauses,
        "cnf_sha256": cnf_sha256,
    }
    return Challenge(
        challenge_id=_challenge_commitment(fields), cnf_b64=cnf_b64, **fields
    ), cnf


def grade_response(
    challenge: Challenge,
    cnf: Cnf,
    *,
    response_challenge_id: str,
    response_miner_hotkey: str,
    assignment_b64: str,
    observed_ms: float,
    received_at_ms: int,
    reference_ms: float,
    correctness_share: float = 0.8,
) -> tuple[float, str]:
    if response_challenge_id != challenge.challenge_id:
        return 0.0, "challenge_mismatch"
    if response_miner_hotkey != challenge.miner_hotkey:
        return 0.0, "miner_identity_mismatch"
    if received_at_ms > challenge.expires_at_ms:
        return 0.0, "expired"
    if not math.isfinite(observed_ms) or observed_ms < 0:
        return 0.0, "invalid_observed_time"
    if not math.isfinite(reference_ms) or reference_ms <= 0:
        raise ThinSubnetError("reference_ms must be finite and positive")
    if not 0.0 <= correctness_share <= 1.0:
        raise ThinSubnetError("correctness_share must be in [0,1]")
    try:
        assignment = decode_assignment(assignment_b64, n_vars=cnf.n_vars)
    except ThinSubnetError as exc:
        return 0.0, str(exc)
    if not verify_witness(cnf, assignment):
        return 0.0, "witness_failed"
    speed = reference_ms / (reference_ms + observed_ms)
    score = correctness_share + (1.0 - correctness_share) * speed
    return round(max(0.0, min(1.0, score)), 9), "verified"


def update_ema(
    previous: dict[str, float],
    observations: dict[str, float],
    *,
    current_hotkeys: Iterable[str],
    alpha: float,
) -> dict[str, float]:
    if not math.isfinite(alpha) or not 0 < alpha <= 1:
        raise ThinSubnetError("EMA alpha must be in (0,1]")
    out: dict[str, float] = {}
    for hotkey in sorted(set(current_hotkeys)):
        old = float(previous.get(hotkey, 0.0))
        new = float(observations.get(hotkey, 0.0))
        if not math.isfinite(old) or old < 0 or not math.isfinite(new) or new < 0:
            raise ThinSubnetError("EMA input must be finite and non-negative")
        out[hotkey] = round((1.0 - alpha) * old + alpha * new, 12)
    return out


def gate_scores_by_current_verification(
    ema_scores: dict[str, float], observations: dict[str, float]
) -> dict[str, float]:
    """Retain history but make current verified work a weight prerequisite."""
    out: dict[str, float] = {}
    for hotkey, raw_ema in sorted(ema_scores.items()):
        ema = float(raw_ema)
        observation = float(observations.get(hotkey, 0.0))
        if (
            not math.isfinite(ema)
            or ema < 0
            or not math.isfinite(observation)
            or observation < 0
        ):
            raise ThinSubnetError("eligibility scores must be finite and non-negative")
        out[hotkey] = ema if observation > 0 else 0.0
    return out


def coldkey_collapsed_weights(
    scores: dict[str, float], coldkey_of: dict[str, str]
) -> dict[str, float]:
    groups: dict[str, list[tuple[str, float]]] = {}
    for hotkey, raw_score in sorted(scores.items()):
        score = float(raw_score)
        if not math.isfinite(score) or score < 0:
            raise ThinSubnetError("score must be finite and non-negative")
        coldkey = coldkey_of.get(hotkey)
        if not coldkey:
            raise ThinSubnetError(f"missing coldkey for {hotkey}")
        if score > 0:
            groups.setdefault(coldkey, []).append((hotkey, score))
    budgets = {
        coldkey: max(score for _, score in members)
        for coldkey, members in groups.items()
    }
    total_budget = sum(budgets.values())
    if total_budget <= 0:
        return {}
    out: dict[str, float] = {}
    for coldkey, members in groups.items():
        group_total = sum(score for _, score in members)
        budget = budgets[coldkey] / total_budget
        for hotkey, score in members:
            out[hotkey] = budget * score / group_total
    norm = sum(out.values())
    if norm <= 0:
        return {}
    return {hotkey: weight / norm for hotkey, weight in sorted(out.items())}


def vector_digest(
    uids: list[int],
    weights: list[float],
    hotkeys: list[str],
    provenance_digest: str | None = None,
    registration_ids: dict[str, str] | None = None,
) -> str:
    if len(uids) != len(weights) or len(uids) != len(hotkeys) or not uids:
        raise ThinSubnetError("weight vector must be non-empty and aligned")
    pairs = []
    seen: set[int] = set()
    for uid, weight, hotkey in zip(uids, weights, hotkeys):
        if (
            isinstance(uid, bool)
            or not isinstance(uid, int)
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not isinstance(hotkey, str)
        ):
            raise ThinSubnetError("invalid UID weight vector")
        uid_i = uid
        value = float(weight)
        hotkey_s = hotkey
        if (
            uid_i < 0
            or uid_i in seen
            or not math.isfinite(value)
            or value < 0
            or not hotkey_s
        ):
            raise ThinSubnetError("invalid UID weight vector")
        seen.add(uid_i)
        pairs.append([uid_i, hotkey_s, round(value, 12)])
    if provenance_digest is not None:
        if (
            not isinstance(provenance_digest, str)
            or not provenance_digest.startswith("sha256:")
            or len(provenance_digest) != 71
        ):
            raise ThinSubnetError("invalid vector provenance digest")
        try:
            bytes.fromhex(provenance_digest.removeprefix("sha256:"))
        except ValueError as exc:
            raise ThinSubnetError("invalid vector provenance digest") from exc
    if registration_ids is not None and not isinstance(registration_ids, dict):
        raise ThinSubnetError("invalid vector owner registration binding")
    registrations_raw = registration_ids or {}
    for class_id, registration_id in registrations_raw.items():
        if (
            not isinstance(class_id, str)
            or not class_id
            or not isinstance(registration_id, str)
            or not registration_id.startswith("sha256:")
            or len(registration_id) != 71
        ):
            raise ThinSubnetError("invalid vector owner registration binding")
        try:
            bytes.fromhex(registration_id.removeprefix("sha256:"))
        except ValueError as exc:
            raise ThinSubnetError("invalid vector owner registration binding") from exc
    registrations = dict(sorted(registrations_raw.items()))
    body = json.dumps(
        {
            "pairs": pairs,
            "provenance_digest": provenance_digest,
            "registration_ids": registrations,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(b"cathedral-thin-vector-v3\x00" + body).hexdigest()


@dataclass
class PendingVector:
    digest: str
    uids: list[int]
    weights: list[float]
    hotkeys: list[str]
    provenance_digest: str | None = None
    registration_ids: dict[str, str] = field(default_factory=dict)
    attempts: int = 0
    next_retry_at_ms: int = 0
    ambiguous: bool = False


@dataclass
class ValidatorState:
    schema: int
    config_fingerprint: str
    master_secret_hex: str
    last_completed_round: int = -1
    ema_scores: dict[str, float] = field(default_factory=dict)
    pending_vector: PendingVector | None = None
    confirmed_vector_digest: str | None = None
    confirmed_decision_digest: str | None = None
    class_checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    registration_checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def master_secret(self) -> bytes:
        try:
            value = bytes.fromhex(self.master_secret_hex)
        except ValueError as exc:
            raise ThinSubnetError("state master secret is not hex") from exc
        if len(value) != 32:
            raise ThinSubnetError("state master secret must be 32 bytes")
        return value


def config_fingerprint(config: dict[str, Any]) -> str:
    body = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"cathedral-thin-config-v1\x00" + body).hexdigest()


class StateStore:
    def __init__(self, path: str | Path, *, fingerprint: str):
        self.path = Path(path).expanduser()
        self.fingerprint = fingerprint

    def load_or_create(self, *, allow_config_change: bool = False) -> ValidatorState:
        if not self.path.exists():
            state = ValidatorState(
                schema=STATE_SCHEMA,
                config_fingerprint=self.fingerprint,
                master_secret_hex=os.urandom(32).hex(),
            )
            self.save(state)
            return state
        if self.path.is_symlink():
            raise ThinSubnetError("validator state must not be a symlink")
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise ThinSubnetError(
                "could not secure validator state permissions"
            ) from exc
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pending_raw = payload.get("pending_vector")
            pending = PendingVector(**pending_raw) if pending_raw is not None else None
            state = ValidatorState(
                schema=int(payload["schema"]),
                config_fingerprint=str(payload["config_fingerprint"]),
                master_secret_hex=str(payload["master_secret_hex"]),
                last_completed_round=int(payload.get("last_completed_round", -1)),
                ema_scores={
                    str(k): float(v) for k, v in payload.get("ema_scores", {}).items()
                },
                pending_vector=pending,
                confirmed_vector_digest=payload.get("confirmed_vector_digest"),
                confirmed_decision_digest=payload.get("confirmed_decision_digest"),
                class_checkpoints={
                    str(k): dict(v)
                    for k, v in payload.get("class_checkpoints", {}).items()
                },
                registration_checkpoints={
                    str(k): dict(v)
                    for k, v in payload.get("registration_checkpoints", {}).items()
                },
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThinSubnetError(f"validator state is corrupt: {self.path}") from exc
        if state.schema != STATE_SCHEMA:
            raise ThinSubnetError(f"unsupported validator state schema {state.schema}")
        _ = state.master_secret
        if state.config_fingerprint != self.fingerprint:
            if not allow_config_change:
                raise ThinSubnetError(
                    "config fingerprint changed; use explicit acceptance"
                )
            if state.pending_vector is not None:
                raise ThinSubnetError(
                    "cannot accept config change with a pending weight vector"
                )
            state.config_fingerprint = self.fingerprint
            state.last_completed_round = -1
            state.ema_scores = {}
            state.confirmed_vector_digest = None
            state.confirmed_decision_digest = None
            self.save(state)
        if state.pending_vector is not None:
            expected = vector_digest(
                state.pending_vector.uids,
                state.pending_vector.weights,
                state.pending_vector.hotkeys,
                state.pending_vector.provenance_digest,
                state.pending_vector.registration_ids,
            )
            if not hmac.compare_digest(expected, state.pending_vector.digest):
                raise ThinSubnetError("pending vector digest mismatch")
            if (
                isinstance(state.pending_vector.attempts, bool)
                or not isinstance(state.pending_vector.attempts, int)
                or state.pending_vector.attempts < 0
                or isinstance(state.pending_vector.next_retry_at_ms, bool)
                or not isinstance(state.pending_vector.next_retry_at_ms, int)
                or state.pending_vector.next_retry_at_ms < 0
                or not isinstance(state.pending_vector.ambiguous, bool)
            ):
                raise ThinSubnetError("invalid pending vector retry metadata")
        for class_id, checkpoint in state.class_checkpoints.items():
            if (
                not class_id
                or frozenset(checkpoint) != {"source_epoch", "report_id"}
                or isinstance(checkpoint["source_epoch"], bool)
                or not isinstance(checkpoint["source_epoch"], int)
                or checkpoint["source_epoch"] < 0
                or not isinstance(checkpoint["report_id"], str)
                or not checkpoint["report_id"].startswith("sha256:")
                or len(checkpoint["report_id"]) != 71
            ):
                raise ThinSubnetError("invalid score class checkpoint")
            try:
                bytes.fromhex(checkpoint["report_id"].removeprefix("sha256:"))
            except ValueError as exc:
                raise ThinSubnetError("invalid score class checkpoint") from exc
        for class_id, checkpoint in state.registration_checkpoints.items():
            if (
                not class_id
                or frozenset(checkpoint)
                != {
                    "owner_coldkey",
                    "delegate_hotkey",
                    "sequence",
                    "registration_id",
                }
                or not isinstance(checkpoint["owner_coldkey"], str)
                or not checkpoint["owner_coldkey"]
                or not isinstance(checkpoint["delegate_hotkey"], str)
                or not checkpoint["delegate_hotkey"]
                or isinstance(checkpoint["sequence"], bool)
                or not isinstance(checkpoint["sequence"], int)
                or checkpoint["sequence"] < 0
                or not isinstance(checkpoint["registration_id"], str)
                or not checkpoint["registration_id"].startswith("sha256:")
                or len(checkpoint["registration_id"]) != 71
            ):
                raise ThinSubnetError("invalid owner registration checkpoint")
            try:
                bytes.fromhex(checkpoint["registration_id"].removeprefix("sha256:"))
            except ValueError as exc:
                raise ThinSubnetError("invalid owner registration checkpoint") from exc
        for score in state.ema_scores.values():
            if not math.isfinite(score) or score < 0:
                raise ThinSubnetError("invalid score in validator state")
        return state

    def save(self, state: ValidatorState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if tmp.exists():
                tmp.unlink()

    def lease(self) -> "StateLease":
        return StateLease(self.path.with_name(self.path.name + ".lock"))


class StateLease:
    """Prevent two validators from racing one challenge secret/checkpoint."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "StateLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self._handle = os.fdopen(fd, "r+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise ThinSubnetError(
                f"validator state is already locked: {self.path}"
            ) from exc
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def mark_pending(
    state: ValidatorState,
    *,
    uids: list[int],
    weights: list[float],
    hotkeys: list[str],
    provenance_digest: str | None = None,
    registration_ids: dict[str, str] | None = None,
) -> PendingVector:
    if registration_ids is not None and not isinstance(registration_ids, dict):
        raise ThinSubnetError("invalid vector owner registration binding")
    bindings = dict(registration_ids or {})
    digest = vector_digest(uids, weights, hotkeys, provenance_digest, bindings)
    bindings = dict(sorted(bindings.items()))
    pending = PendingVector(
        digest=digest,
        uids=list(uids),
        weights=list(weights),
        hotkeys=list(hotkeys),
        provenance_digest=provenance_digest,
        registration_ids=bindings,
    )
    state.pending_vector = pending
    return pending


def note_submission_failure(
    state: ValidatorState,
    *,
    now_ms: int,
    base_backoff_ms: int = 15_000,
    max_backoff_ms: int = 15 * 60_000,
    ambiguous: bool = False,
) -> None:
    if state.pending_vector is None:
        raise ThinSubnetError("no pending vector to fail")
    state.pending_vector.attempts += 1
    state.pending_vector.ambiguous = ambiguous
    delay = min(
        max_backoff_ms,
        base_backoff_ms * (2 ** min(10, state.pending_vector.attempts - 1)),
    )
    state.pending_vector.next_retry_at_ms = now_ms + delay


def note_submission_success(state: ValidatorState) -> None:
    if state.pending_vector is None:
        raise ThinSubnetError("no pending vector to confirm")
    state.confirmed_vector_digest = state.pending_vector.digest
    state.confirmed_decision_digest = state.pending_vector.provenance_digest
    state.pending_vector = None


def response_succeeded(response: Any) -> bool:
    """Normalize Bittensor 8/10 and test-double weight response shapes."""
    if isinstance(response, bool):
        return response
    if isinstance(response, tuple):
        return bool(response[0]) if response else False
    if response is None:
        return False
    for attribute in ("is_success", "success"):
        if hasattr(response, attribute):
            value = getattr(response, attribute)
            return bool(value() if callable(value) else value)
    return False


def submission_failure_is_ambiguous(response: Any) -> bool:
    """Conservatively classify whether a failed modern SDK response may have landed."""
    if response is None:
        return True
    if hasattr(response, "extrinsic_receipt"):
        if getattr(response, "extrinsic_receipt") is not None:
            return False
        if getattr(response, "error", None) is not None:
            return True
        if getattr(response, "extrinsic", None) is not None:
            return True
        return False
    return bool(getattr(response, "ambiguous", False))
